/*
 * PROJECT MPM-RAIV - FIRMWARE v44.0 (NON-BLOCKING MANUAL FIX)
 * -----------------------------------------------------------
 * CHANGELOG:
 * - FIX 1 (Manual Jerk): Replaced blocking delay loop with non-blocking 
 * 'micros()' timer. Allows OS tasks to run smoothly without stopping motors.
 * - FIX 2 (Distance Cap): Implemented strict Command ID locking to prevent 
 * cloud echoes from resetting the target distance mid-travel.
 * - GPS: Hardcoded Simulation Coordinates.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <Firebase_ESP_Client.h>
#include "time.h" 
#include <esp_task_wdt.h> 

#include "soc/soc.h"
#include "soc/rtc_cntl_reg.h"

#include "addons/TokenHelper.h"
#include "addons/RTDBHelper.h"

// ================================================================
// 1. CONFIGURATION
// ================================================================
#define WIFI_SSID       "4G_AP_1513"
#define WIFI_PASSWORD   "12345678"
#define API_KEY         "AIzaSyD69TabKnrT2AnF3ck6B1gHx5BlVNwYmXs"
#define DATABASE_URL    "mpm-raiv-default-rtdb.asia-southeast1.firebasedatabase.app"

// ================================================================
// 2. PINS
// ================================================================
const int PIN_LF = 4;   const int PIN_LB = 14;
const int PIN_RF = 26;  const int PIN_RB = 27;
const int BTN_FWD = 32; const int BTN_BWD = 33;
const int PIN_LIGHT_RED = 18; const int PIN_LIGHT_ORANGE = 19;

// ================================================================
// 3. PHYSICS & CALIBRATION
// ================================================================
const float WHEEL_CIRC_M    = 0.3635; 
const int STEPS_PER_REV     = 1000; 
const float ERROR_RATIO     = 1.96; 
const float METERS_PER_STEP = (WHEEL_CIRC_M / STEPS_PER_REV) * ERROR_RATIO; 
const int PULSE_WIDTH       = 500;   
const int RAMP_STEP         = 10; 
const double SIM_LAT = 3.128625;
const double SIM_LNG = 101.738602;

// ================================================================
// 4. SHARED VARIABLES
// ================================================================
portMUX_TYPE timerMux = portMUX_INITIALIZER_UNLOCKED;

volatile bool shared_run = false;
volatile bool shared_dir = true; // true=FWD
volatile bool shared_auto_mode = false; 
volatile uint64_t shared_steps_target = 0; 
volatile uint64_t shared_steps_taken = 0;
volatile bool shared_job_done = false;
volatile int shared_target_delay = 5000; 

// ================================================================
// 5. GLOBAL OBJECTS
// ================================================================
FirebaseData fbDO;      
FirebaseData streamDO;  
FirebaseAuth auth; FirebaseConfig config;
bool signupOK=false; bool wifiConnected=false;
String cloudCommand = "STOP";
unsigned long lastTel = 0;
unsigned long lastSpeedPoll = 0;

// ================================================================
// CORE 0: MOTOR ENGINE (Non-Blocking)
// ================================================================
void TaskEngine(void * pvParameters) {
  esp_task_wdt_config_t twdt_config = {
      .timeout_ms = 60000, .idle_core_mask = (1 << 0), .trigger_panic = true       
  };
  esp_task_wdt_init(&twdt_config); 
  esp_task_wdt_add(NULL);          

  pinMode(PIN_LF, OUTPUT); pinMode(PIN_LB, OUTPUT); 
  pinMode(PIN_RF, OUTPUT); pinMode(PIN_RB, OUTPUT);
  digitalWrite(PIN_LF, HIGH); digitalWrite(PIN_LB, HIGH);
  digitalWrite(PIN_RF, HIGH); digitalWrite(PIN_RB, HIGH);

  pinMode(BTN_FWD, INPUT_PULLUP);
  pinMode(BTN_BWD, INPUT_PULLUP);

  int currentDelay = 5000; 
  unsigned long lastStepMicros = 0;

  for(;;) { 
    esp_task_wdt_reset(); 

    // Read Buttons
    bool btnF = (digitalRead(BTN_FWD) == LOW);
    bool btnB = (digitalRead(BTN_BWD) == LOW);
    
    bool active = false;
    bool direction = true; 
    bool isAuto = false;

    // A. Priority: Cloud Auto Mode
    if (shared_auto_mode && shared_run) {
        active = true;
        direction = shared_dir;
        isAuto = true;
    }
    // B. Manual Hold (Simple Logic)
    else if (btnF) {
        active = true;
        direction = true;
        // Reduce manual speed by ~30% as requested (3000us delay)
        portENTER_CRITICAL(&timerMux);
        shared_target_delay = 3000; 
        portEXIT_CRITICAL(&timerMux);
    }
    else if (btnB) {
        active = true;
        direction = false;
        portENTER_CRITICAL(&timerMux);
        shared_target_delay = 3000;
        portEXIT_CRITICAL(&timerMux);
    }

    if (active) {
        // --- DISTANCE CHECK (Auto Only) ---
        if (isAuto) {
            if (shared_steps_target > 0 && shared_steps_taken >= shared_steps_target) {
                portENTER_CRITICAL(&timerMux);
                shared_run = false; 
                shared_auto_mode = false; 
                shared_steps_target = 0;
                shared_job_done = true; 
                portEXIT_CRITICAL(&timerMux);
                active = false; 
            }
        }

        if (active) {
            // Non-Blocking Pulse Timer
            unsigned long now = micros();
            if (now - lastStepMicros >= currentDelay) {
                lastStepMicros = now;

                // Ramp Speed
                int targetD = shared_target_delay;
                if (currentDelay > targetD) currentDelay -= RAMP_STEP;
                else if (currentDelay < targetD) currentDelay += RAMP_STEP;
                
                // Pulse
                if (direction) { 
                    digitalWrite(PIN_LF, LOW); digitalWrite(PIN_RF, LOW);
                    delayMicroseconds(PULSE_WIDTH);
                    digitalWrite(PIN_LF, HIGH); digitalWrite(PIN_RF, HIGH);
                } else { 
                    digitalWrite(PIN_LB, LOW); digitalWrite(PIN_RB, LOW);
                    delayMicroseconds(PULSE_WIDTH);
                    digitalWrite(PIN_LB, HIGH); digitalWrite(PIN_RB, HIGH);
                }

                if (isAuto) {
                    portENTER_CRITICAL(&timerMux);
                    shared_steps_taken++;
                    portEXIT_CRITICAL(&timerMux);
                }
            }
            // Yield briefly to let WDT/WiFi run
            // This 1 tick delay is critical for stability during loops
            vTaskDelay(1); 
        }
    } 
    else {
        currentDelay = 5000; 
        vTaskDelay(1); 
    }
  }
}

// ================================================================
// STREAM CALLBACK
// ================================================================
void streamCallback(FirebaseStream data) {
  if (data.dataTypeEnum() == firebase_rtdb_data_type_string) {
      String c = data.stringData();
      Serial.println("STREAM: " + c);
      
      // Strict Command Locking: Ignore redundant commands
      if (shared_run && shared_auto_mode && c == cloudCommand) return;
      
      cloudCommand = c;

      portENTER_CRITICAL(&timerMux);
      if (c == "STOP") { 
          shared_run = false; shared_auto_mode = false;
      }
      else if (c.startsWith("FWD_") || c.startsWith("BWD_")) {
          String distStr = c.substring(4);
          float m = distStr.toFloat();
          if (m > 0) {
              shared_dir = c.startsWith("FWD_");
              shared_steps_taken = 0; 
              shared_steps_target = (uint64_t)(m / METERS_PER_STEP);
              shared_run = true;
              shared_auto_mode = true; 
              Serial.printf("Target Steps: %llu\n", shared_steps_target);
          }
      }
      portEXIT_CRITICAL(&timerMux);
  }
}

void streamTimeoutCallback(bool timeout) {
  if (timeout) Serial.println("Stream Timeout...");
}

// ================================================================
// CORE 1: SETUP
// ================================================================
void setup() {
  WRITE_PERI_REG(RTC_CNTL_BROWN_OUT_REG, 0); 
  
  Serial.begin(115200);
  Serial.println("\n--- RAIV v44.0 (NON-BLOCKING) ---");

  xTaskCreatePinnedToCore(TaskEngine, "Engine", 12000, NULL, 1, NULL, 0);

  pinMode(PIN_LIGHT_RED, OUTPUT); pinMode(PIN_LIGHT_ORANGE, OUTPUT);
  pinMode(BTN_FWD, INPUT_PULLUP); pinMode(BTN_BWD, INPUT_PULLUP);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long s = millis();
  while(WiFi.status() != WL_CONNECTED && millis()-s < 10000) delay(200);
  
  if(WiFi.status() == WL_CONNECTED) {
    configTime(0, 0, "pool.ntp.org"); 
    config.api_key = API_KEY; config.database_url = DATABASE_URL;
    config.timeout.wifiReconnect = 2000;
    fbDO.setBSSLBufferSize(2048, 512); 
    streamDO.setBSSLBufferSize(2048, 512);
    config.token_status_callback = tokenStatusCallback; 

    if (Firebase.signUp(&config, &auth, "", "")) {
        signupOK = true;
        Firebase.RTDB.setString(&fbDO, "/command", "STOP");
    }
    Firebase.begin(&config, &auth);
    Firebase.reconnectWiFi(true);
    
    if (!Firebase.RTDB.beginStream(&streamDO, "/command")) {
        Serial.printf("Stream Error: %s\n", streamDO.errorReason().c_str());
    }
    Firebase.RTDB.setStreamCallback(&streamDO, streamCallback, streamTimeoutCallback);
  }
}

void loop() {
  bool manualActive = (digitalRead(BTN_FWD) == LOW || digitalRead(BTN_BWD) == LOW);

  if (!manualActive && wifiConnected && signupOK && Firebase.ready()) {
      
      // 1. Job Done Report
      if (shared_job_done) {
          Serial.println("Target Reached. Resetting.");
          Firebase.RTDB.setString(&fbDO, "/command", "STOP");
          cloudCommand = "STOP";
          portENTER_CRITICAL(&timerMux);
          shared_job_done = false; shared_run = false; shared_auto_mode = false;
          portEXIT_CRITICAL(&timerMux);
      }
      
      // 2. Poll Speed Factor
      if (millis() - lastSpeedPoll > 1000) {
          if (Firebase.RTDB.getInt(&fbDO, "/speed_factor")) {
              int pct = fbDO.intData();
              if (pct < 25) pct = 25;
              
              float selectedSpeedMs = (pct - 20) / 35.0; 
              // Expert Curve
              float actualSpeedMs = 0.1; 
              if      (selectedSpeedMs <= 0.3)  actualSpeedMs = selectedSpeedMs * 0.70;
              else if (selectedSpeedMs <= 0.75) actualSpeedMs = 0.20; 
              else if (selectedSpeedMs <= 1.25) actualSpeedMs = 0.40; 
              else if (selectedSpeedMs <= 1.75) actualSpeedMs = 0.60;
              else if (selectedSpeedMs <= 2.25) actualSpeedMs = 0.70;
              else if (selectedSpeedMs <= 2.75) actualSpeedMs = 0.75;
              else if (selectedSpeedMs <= 3.25) actualSpeedMs = 0.80;
              else if (selectedSpeedMs <= 3.75) actualSpeedMs = 0.85;
              else if (selectedSpeedMs <= 4.25) actualSpeedMs = 0.90;
              else if (selectedSpeedMs <= 4.75) actualSpeedMs = 0.95;
              else                              actualSpeedMs = 1.00;

              if(actualSpeedMs < 0.05) actualSpeedMs = 0.05;
              float delayUs = (METERS_PER_STEP / actualSpeedMs) * 1000000.0 - PULSE_WIDTH;
              if(delayUs < 100) delayUs = 100;
              
              portENTER_CRITICAL(&timerMux);
              shared_target_delay = (int)delayUs;
              portEXIT_CRITICAL(&timerMux);
          }
          lastSpeedPoll = millis();
      }
  }

  // Lights
  if (shared_run) {
     digitalWrite(PIN_LIGHT_RED, LOW); digitalWrite(PIN_LIGHT_ORANGE, (millis()/500)%2);
  } else {
     digitalWrite(PIN_LIGHT_RED, HIGH); digitalWrite(PIN_LIGHT_ORANGE, LOW);
  }

  // Telemetry 
  if (wifiConnected && millis() - lastTel > 2000) {
      float stepTime = (PULSE_WIDTH + shared_target_delay)/1000000.0;
      float kmh = (shared_run ? (METERS_PER_STEP/stepTime)*3.6 : 0);
      
      FirebaseJson json;
      json.set("speed", kmh);
      json.set("status", shared_run?"MOVING":"STANDBY");
      json.set("lat", SIM_LAT); 
      json.set("lng", SIM_LNG);
      json.set("vibration", 0); 
      json.set("vertical", 0);
      
      if (shared_steps_target > 0) {
          double t = (double)shared_steps_target * METERS_PER_STEP;
          double p = (double)shared_steps_taken * METERS_PER_STEP;
          json.set("targ_dist", t);
          json.set("prog_dist", p);
      }
      
      Firebase.RTDB.setJSON(&fbDO, "/telemetry", &json);
      lastTel = millis();
  }
  delay(1); 
}
