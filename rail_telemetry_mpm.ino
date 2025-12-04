/*
 * PROJECT MPM-RAIV - FIRMWARE v32.0 (INSTANT STREAMING)
 * -----------------------------------------------------------
 * CHANGELOG:
 * - LATENCY FIX: Switched from Polling to STREAMING for Commands.
 * Reaction time reduced from ~30s to <1s.
 * - ARCHITECTURE: Uses separate Firebase objects for Stream (Command) 
 * and Poll (Speed/Telemetry) to ensure non-blocking operation.
 * - SAFETY: Watchdog (WDT) and Deceleration logic preserved.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <Firebase_ESP_Client.h>
#include "time.h" 
#include <esp_task_wdt.h> 

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
// 3. PHYSICS TUNING
// ================================================================
const float WHEEL_CIRC_M    = 0.3635; 
const int STEPS_PER_REV     = 1000; 

// CALIBRATION
const float ERROR_RATIO     = 1.96; 
const float METERS_PER_STEP = (WHEEL_CIRC_M / STEPS_PER_REV) * ERROR_RATIO; 

// Speed & Ramp Settings
const int PULSE_WIDTH       = 500;   
const int DELAY_MANUAL      = 2000;  
const int DELAY_STARTUP     = 5000;  
const int RAMP_STEP         = 20;    

// Deceleration Zone: Slow down 300 steps (~21cm) before target
const int DECEL_ZONE_STEPS  = 300;   

// ================================================================
// 4. SHARED VARIABLES (THREAD SAFE)
// ================================================================
portMUX_TYPE timerMux = portMUX_INITIALIZER_UNLOCKED;

volatile bool shared_run = false;
volatile bool shared_dir = true; // true=FWD
volatile bool shared_auto_mode = false; 
volatile long shared_steps_target = 0; 
volatile long shared_steps_taken = 0;
volatile bool shared_job_done = false;

volatile int shared_target_delay = DELAY_STARTUP;

// ================================================================
// 5. GLOBAL OBJECTS
// ================================================================
FirebaseData fbDO;      // For Speed Polling & Telemetry
FirebaseData streamDO;  // Dedicated for Instant Command Stream
FirebaseAuth auth; FirebaseConfig config;
bool signupOK=false; bool wifiConnected=false;

String cloudCommand = "STOP";
unsigned long lastTel = 0;
unsigned long lastSpeedPoll = 0;

// ================================================================
// CORE 0: MOTOR ENGINE (The Driver)
// ================================================================
void TaskEngine(void * pvParameters) {
  esp_task_wdt_config_t twdt_config = {
      .timeout_ms = 60000,        
      .idle_core_mask = (1 << 0), 
      .trigger_panic = true       
  };
  esp_task_wdt_init(&twdt_config); 
  esp_task_wdt_add(NULL);          

  pinMode(PIN_LF, OUTPUT); pinMode(PIN_LB, OUTPUT); 
  pinMode(PIN_RF, OUTPUT); pinMode(PIN_RB, OUTPUT);
  digitalWrite(PIN_LF, HIGH); digitalWrite(PIN_LB, HIGH);
  digitalWrite(PIN_RF, HIGH); digitalWrite(PIN_RB, HIGH);

  pinMode(BTN_FWD, INPUT_PULLUP);
  pinMode(BTN_BWD, INPUT_PULLUP);

  int currentDelay = DELAY_STARTUP;

  for(;;) { 
    esp_task_wdt_reset(); // Feed Watchdog

    // 1. READ BUTTONS
    bool btnF = (digitalRead(BTN_FWD) == LOW);
    bool btnB = (digitalRead(BTN_BWD) == LOW);
    
    // 2. CONTROL LOGIC
    if (shared_auto_mode) {
        // --- AUTO MODE ---
        if (shared_run) {
             // A. Check Target
             if (shared_steps_target > 0 && shared_steps_taken >= shared_steps_target) {
                portENTER_CRITICAL(&timerMux);
                shared_run = false; 
                shared_auto_mode = false; 
                shared_steps_target = 0;
                shared_job_done = true; 
                portEXIT_CRITICAL(&timerMux);
             }
             else {
                // B. Deceleration Logic
                int targetD = shared_target_delay;
                long stepsRem = shared_steps_target - shared_steps_taken;
                
                if (stepsRem < DECEL_ZONE_STEPS) {
                    targetD = targetD + (DECEL_ZONE_STEPS - stepsRem) * 20;
                    if (targetD > DELAY_STARTUP) targetD = DELAY_STARTUP;
                }

                if (currentDelay > targetD) currentDelay -= RAMP_STEP;
                else if (currentDelay < targetD) currentDelay += RAMP_STEP;

                // C. Pulse
                if (shared_dir) {
                  digitalWrite(PIN_LF, LOW); digitalWrite(PIN_RF, LOW);
                  delayMicroseconds(PULSE_WIDTH);
                  digitalWrite(PIN_LF, HIGH); digitalWrite(PIN_RF, HIGH);
                } else {
                  digitalWrite(PIN_LB, LOW); digitalWrite(PIN_RB, LOW);
                  delayMicroseconds(PULSE_WIDTH);
                  digitalWrite(PIN_LB, HIGH); digitalWrite(PIN_RB, HIGH);
                }
                
                portENTER_CRITICAL(&timerMux);
                shared_steps_taken++;
                portEXIT_CRITICAL(&timerMux);
                
                delayMicroseconds(currentDelay);
             }
        } else {
             shared_auto_mode = false;
        }
    } 
    else {
        // --- MANUAL MODE ---
        if (btnF) {
            portENTER_CRITICAL(&timerMux);
            shared_run = true; shared_dir = true; shared_target_delay = DELAY_MANUAL;
            portEXIT_CRITICAL(&timerMux);
        } 
        else if (btnB) {
            portENTER_CRITICAL(&timerMux);
            shared_run = true; shared_dir = false; shared_target_delay = DELAY_MANUAL;
            portEXIT_CRITICAL(&timerMux);
        }
        else if (shared_run) {
            portENTER_CRITICAL(&timerMux);
            shared_run = false;
            portEXIT_CRITICAL(&timerMux);
        }
        
        if (shared_run) {
             if (currentDelay > shared_target_delay) currentDelay -= RAMP_STEP;
             else if (currentDelay < shared_target_delay) currentDelay += RAMP_STEP;
             
             if (shared_dir) {
                  digitalWrite(PIN_LF, LOW); digitalWrite(PIN_RF, LOW);
                  delayMicroseconds(PULSE_WIDTH);
                  digitalWrite(PIN_LF, HIGH); digitalWrite(PIN_RF, HIGH);
             } else {
                  digitalWrite(PIN_LB, LOW); digitalWrite(PIN_RB, LOW);
                  delayMicroseconds(PULSE_WIDTH);
                  digitalWrite(PIN_LB, HIGH); digitalWrite(PIN_RB, HIGH);
             }
             delayMicroseconds(currentDelay);
        } else {
             currentDelay = DELAY_STARTUP;
             vTaskDelay(1); 
        }
    }
  }
}

// ================================================================
// STREAM CALLBACK (Executes Instantly on Data Change)
// ================================================================
void streamCallback(FirebaseStream data) {
  if (data.dataTypeEnum() == firebase_rtdb_data_type_string) {
      String c = data.stringData();
      Serial.println("STREAM CMD: " + c);
      
      // Update Cloud Command State
      cloudCommand = c;

      portENTER_CRITICAL(&timerMux);
      if (c == "STOP") { 
          shared_run = false; shared_auto_mode = false;
      }
      else if (c.startsWith("FWD_") || c.startsWith("BWD_")) {
          float m = c.substring(4).toFloat();
          shared_dir = c.startsWith("FWD_");
          shared_steps_taken = 0; 
          shared_steps_target = (long)(m / METERS_PER_STEP);
          shared_run = true;
          shared_auto_mode = true; 
      }
      portEXIT_CRITICAL(&timerMux);
  }
}

void streamTimeoutCallback(bool timeout) {
  if (timeout) Serial.println("Stream Timeout, Resuming...");
}

// ================================================================
// CORE 1: CLOUD & LOGIC
// ================================================================
void setup() {
  Serial.begin(115200);
  Serial.println("\n--- MPM-RAIV v32.0 (INSTANT STREAM) ---");

  xTaskCreatePinnedToCore(TaskEngine, "Engine", 12000, NULL, 1, NULL, 0);

  pinMode(PIN_LIGHT_RED, OUTPUT); pinMode(PIN_LIGHT_ORANGE, OUTPUT);
  pinMode(BTN_FWD, INPUT_PULLUP); pinMode(BTN_BWD, INPUT_PULLUP);

  // WiFi Setup
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long s = millis();
  while(WiFi.status() != WL_CONNECTED && millis()-s < 10000) delay(200);
  
  if(WiFi.status() == WL_CONNECTED) {
    wifiConnected = true;
    configTime(0, 0, "pool.ntp.org"); 
    
    // STREAMING CONFIG
    config.api_key = API_KEY; config.database_url = DATABASE_URL;
    config.timeout.wifiReconnect = 2000;
    
    // IMPORTANT: Keep connection alive for streaming
    // config.keep_alive = true; // (Deprecated in new lib, implied by beginStream)
    fbDO.setBSSLBufferSize(2048, 512); 
    streamDO.setBSSLBufferSize(2048, 512);

    config.token_status_callback = tokenStatusCallback; 

    if (Firebase.signUp(&config, &auth, "", "")) {
        signupOK = true;
        Firebase.RTDB.setString(&fbDO, "/command", "STOP");
    }
    Firebase.begin(&config, &auth);
    Firebase.reconnectWiFi(true);
    
    // START STREAMING ON /command
    if (!Firebase.RTDB.beginStream(&streamDO, "/command")) {
        Serial.printf("Stream Error: %s\n", streamDO.errorReason().c_str());
    }
    Firebase.RTDB.setStreamCallback(&streamDO, streamCallback, streamTimeoutCallback);
  }
}

void loop() {
  // Check buttons
  bool manualActive = (digitalRead(BTN_FWD) == LOW || digitalRead(BTN_BWD) == LOW);

  if (!manualActive && wifiConnected && signupOK && Firebase.ready()) {
      
      // 1. Job Done Report
      if (shared_job_done) {
          Serial.println("Target Reached. Resetting.");
          // Use fbDO for writes (streamDO is busy listening)
          Firebase.RTDB.setString(&fbDO, "/command", "STOP");
          cloudCommand = "STOP";
          portENTER_CRITICAL(&timerMux);
          shared_job_done = false; shared_run = false; shared_auto_mode = false;
          portEXIT_CRITICAL(&timerMux);
      }
      
      // 2. Poll Speed Factor (Every 1000ms) - Background Task
      // We poll this separately because commands are instant now.
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

  // Telemetry (Every 1000ms)
  if (wifiConnected && millis() - lastTel > 1000) {
      float stepTime = (PULSE_WIDTH + shared_target_delay)/1000000.0;
      float kmh = (shared_run ? (METERS_PER_STEP/stepTime)*3.6 : 0);
      
      FirebaseJson json;
      json.set("speed", kmh);
      json.set("status", shared_run?"MOVING":"STANDBY");
      // Fixed location for demo
      json.set("lat", 2.9582); 
      json.set("lng", 101.8236);
      json.set("vibration", 0); 
      json.set("vertical", 0);
      
      if (shared_steps_target > 0) {
          json.set("targ_dist", shared_steps_target * METERS_PER_STEP);
          json.set("prog_dist", shared_steps_taken * METERS_PER_STEP);
      }
      
      // Use fbDO for telemetry (streamDO is busy)
      Firebase.RTDB.setJSON(&fbDO, "/telemetry", &json);
      lastTel = millis();
  }
  delay(1); 
}
