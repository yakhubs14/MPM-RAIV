/*
 * PROJECT MPM-RAIV - FIRMWARE v24.0 (Calibrated Precision)
 * --------------------------------------------------------
 * FIXES:
 * 1. DISTANCE ACCURACY: Calibrated to 1.96 ratio based on 0.7m test result.
 * (This increases step count to hit exactly 1.0m).
 * 2. STABILITY: Watchdog timer removed completely to prevent reboot loops.
 * 3. BUTTONS: Direct polling for instant "Dead Man" safety stop.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <Firebase_ESP_Client.h>
#include <Wire.h>
#include <DFRobot_BMI160.h> 
#include <TinyGPS++.h> 
#include "time.h" 

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
const int PIN_TRIG = 5; const int PIN_ECHO = 23;
#define GPS_RX 16 
#define GPS_TX 17
#define GPS_BAUD 115200 
const int OBSTACLE_LIMIT_CM = 5; 

// ================================================================
// 3. PHYSICS TUNING
// ================================================================
const float WHEEL_CIRC_M    = 0.3635; 
const int STEPS_PER_REV     = 1000; 

// *** CALIBRATION FINAL ***
// Previous (2.8) -> Result 0.7m (Undershoot)
// Correction: 2.8 * 0.7 = 1.96
const float ERROR_RATIO     = 1.96; 

const float METERS_PER_STEP = (WHEEL_CIRC_M / STEPS_PER_REV) * ERROR_RATIO; 

// Speed Settings
const int PULSE_WIDTH       = 500;   
const int DELAY_MANUAL      = 2000;  // Smooth Manual Speed
const int DELAY_STARTUP     = 5000;  
const int RAMP_STEP         = 20;    

// ================================================================
// 4. SHARED VARIABLES
// ================================================================
portMUX_TYPE timerMux = portMUX_INITIALIZER_UNLOCKED;

volatile bool shared_run = false;
volatile bool shared_dir = true; // true=FWD
volatile long shared_steps_target = 0; 
volatile long shared_steps_taken = 0;
volatile bool shared_emergency = false;
volatile bool shared_job_done = false;

volatile int shared_target_delay = DELAY_STARTUP;

// ================================================================
// 5. GLOBAL OBJECTS
// ================================================================
FirebaseData fbDO; FirebaseAuth auth; FirebaseConfig config;
bool signupOK=false; bool wifiConnected=false; bool bmi160Ready=false;
TinyGPSPlus gps; HardwareSerial SerialGPS(2); DFRobot_BMI160 bmi160; const int8_t i2c_addr = 0x68;

String cloudCommand = "STOP";
String lastProcessedCmd = "";
unsigned long lastTel = 0;
unsigned long lastNet = 0;
float currentSpeedKmH = 0;

// ================================================================
// CORE 0: MOTOR ENGINE (FAILSAFE)
// ================================================================
void TaskEngine(void * pvParameters) {
  // 1. Setup Motor Pins (Active LOW)
  pinMode(PIN_LF, OUTPUT); pinMode(PIN_LB, OUTPUT); 
  pinMode(PIN_RF, OUTPUT); pinMode(PIN_RB, OUTPUT);
  digitalWrite(PIN_LF, HIGH); digitalWrite(PIN_LB, HIGH);
  digitalWrite(PIN_RF, HIGH); digitalWrite(PIN_RB, HIGH);

  // 2. Setup Buttons (Input Pullup)
  pinMode(BTN_FWD, INPUT_PULLUP);
  pinMode(BTN_BWD, INPUT_PULLUP);

  int currentDelay = DELAY_STARTUP;

  for(;;) { 
    // 3. READ HARDWARE SWITCHES
    bool btnF = (digitalRead(BTN_FWD) == LOW);
    bool btnB = (digitalRead(BTN_BWD) == LOW);
    
    // 4. DETERMINE ACTION
    if (shared_emergency) {
        // Emergency Override
        shared_run = false;
    }
    else if (btnF) {
        // Manual Forward (Press & Hold)
        portENTER_CRITICAL(&timerMux);
        shared_run = true; shared_dir = true; shared_steps_target = 0;
        shared_target_delay = DELAY_MANUAL;
        portEXIT_CRITICAL(&timerMux);
    } 
    else if (btnB) {
        // Manual Backward (Press & Hold)
        portENTER_CRITICAL(&timerMux);
        shared_run = true; shared_dir = false; shared_steps_target = 0;
        shared_target_delay = DELAY_MANUAL;
        portEXIT_CRITICAL(&timerMux);
    }
    else if (!btnF && !btnB && shared_target_delay == DELAY_MANUAL && shared_run) {
        // Manual Release -> STOP INSTANTLY
        portENTER_CRITICAL(&timerMux);
        shared_run = false;
        portEXIT_CRITICAL(&timerMux);
    }

    // 5. MOTOR PULSE LOOP
    if (shared_run) {
        // Check Auto-Distance Limit
        if (shared_steps_target > 0 && shared_steps_taken >= shared_steps_target) {
            portENTER_CRITICAL(&timerMux);
            shared_run = false; 
            shared_steps_target = 0;
            shared_job_done = true; // Signal done
            portEXIT_CRITICAL(&timerMux);
        } 
        else {
            // Ramp Speed
            if (currentDelay > shared_target_delay) currentDelay -= RAMP_STEP;
            else if (currentDelay < shared_target_delay) currentDelay += RAMP_STEP;

            // Step Motors
            if (shared_dir) {
              digitalWrite(PIN_LF, LOW); digitalWrite(PIN_RF, LOW);
              delayMicroseconds(PULSE_WIDTH);
              digitalWrite(PIN_LF, HIGH); digitalWrite(PIN_RF, HIGH);
            } else {
              digitalWrite(PIN_LB, LOW); digitalWrite(PIN_RB, LOW);
              delayMicroseconds(PULSE_WIDTH);
              digitalWrite(PIN_LB, HIGH); digitalWrite(PIN_RB, HIGH);
            }
            
            // Track Distance (Only for Auto Mode)
            if (shared_steps_target > 0) {
                portENTER_CRITICAL(&timerMux);
                shared_steps_taken++;
                portEXIT_CRITICAL(&timerMux);
            }
            
            delayMicroseconds(currentDelay);
        }
    } else {
        currentDelay = DELAY_STARTUP;
        vTaskDelay(1); // Yield to avoid starvation
    }
  }
}

// ================================================================
// CORE 1: LOGIC & CLOUD
// ================================================================
void setup() {
  Serial.begin(115200);
  Serial.println("\n--- MPM-RAIV v24.0 (CALIBRATED) ---");

  // Start Engine Task
  xTaskCreatePinnedToCore(TaskEngine, "Engine", 10000, NULL, 1, NULL, 0);

  pinMode(PIN_TRIG, OUTPUT); pinMode(PIN_ECHO, INPUT); digitalWrite(PIN_TRIG, LOW);
  pinMode(PIN_LIGHT_RED, OUTPUT); pinMode(PIN_LIGHT_ORANGE, OUTPUT);

  SerialGPS.begin(GPS_BAUD, SERIAL_8N1, GPS_RX, GPS_TX);
  if(bmi160.softReset() == BMI160_OK && bmi160.I2cInit(i2c_addr) == BMI160_OK) bmi160Ready = true;

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long s = millis();
  while(WiFi.status() != WL_CONNECTED && millis()-s < 10000) delay(200);
  
  if(WiFi.status() == WL_CONNECTED) {
    wifiConnected = true;
    configTime(0, 0, "pool.ntp.org"); 
    config.api_key = API_KEY; config.database_url = DATABASE_URL;
    config.timeout.wifiReconnect = 2000;
    if (Firebase.signUp(&config, &auth, "", "")) {
        signupOK = true;
        Firebase.RTDB.setString(&fbDO, "/command", "STOP");
    }
    Firebase.begin(&config, &auth);
    Firebase.reconnectWiFi(true);
  }
}

void loop() {
  while(SerialGPS.available()) gps.encode(SerialGPS.read());

  // A. ULTRASONIC SAFETY
  digitalWrite(PIN_TRIG, LOW); delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH); delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);
  long dur = pulseIn(PIN_ECHO, HIGH, 15000);
  float dist = (dur == 0) ? 999 : (dur * 0.034 / 2);
  
  portENTER_CRITICAL(&timerMux);
  shared_emergency = (dist > 0 && dist < OBSTACLE_LIMIT_CM);
  portEXIT_CRITICAL(&timerMux);

  // B. CLOUD SYNC
  bool manualActive = (digitalRead(BTN_FWD) == LOW || digitalRead(BTN_BWD) == LOW);

  if (!manualActive && wifiConnected && signupOK && Firebase.ready()) {
      
      // 1. Job Finished -> Reset Cloud
      if (shared_job_done) {
          Serial.println("Job Done. Clearing Cloud.");
          Firebase.RTDB.setString(&fbDO, "/command", "STOP");
          cloudCommand = "STOP";
          
          portENTER_CRITICAL(&timerMux);
          shared_job_done = false;
          shared_run = false; 
          portEXIT_CRITICAL(&timerMux);
      }

      // 2. Read Command
      if (millis() - lastNet > 150) {
          if (Firebase.RTDB.getString(&fbDO, "/command")) {
              String c = fbDO.stringData();
              if (c != cloudCommand) {
                  cloudCommand = c;
                  Serial.println("CMD: " + c);
                  
                  portENTER_CRITICAL(&timerMux);
                  if (c == "STOP") { 
                      shared_run = false; shared_steps_target = 0; 
                  }
                  else if (c == "FORWARD") { 
                      shared_run = true; shared_dir = true; shared_steps_target = 0; 
                  }
                  else if (c == "BACKWARD") { 
                      shared_run = true; shared_dir = false; shared_steps_target = 0; 
                  }
                  else if (c.startsWith("FWD_")) {
                      float m = c.substring(4).toFloat();
                      shared_dir = true; 
                      shared_steps_taken = 0; 
                      shared_steps_target = (long)(m / METERS_PER_STEP);
                      shared_run = true;
                  }
                  else if (c.startsWith("BWD_")) {
                      float m = c.substring(4).toFloat();
                      shared_dir = false;
                      shared_steps_taken = 0; 
                      shared_steps_target = (long)(m / METERS_PER_STEP);
                      shared_run = true;
                  }
                  portEXIT_CRITICAL(&timerMux);
              }
          }
          
          // 3. Speed Slider
          if (Firebase.RTDB.getInt(&fbDO, "/speed_factor")) {
              int pct = fbDO.intData();
              if (pct < 25) pct = 25;
              // 25% -> 8000us, 200% -> 750us
              // Map
              float speedMs = (pct - 20) / 35.0; if(speedMs < 0.1) speedMs = 0.1;
              float delayUs = (1.0 / ((speedMs / METERS_PER_STEP) / 1000000.0)) - PULSE_WIDTH;
              if(delayUs < 100) delayUs = 100;
              
              portENTER_CRITICAL(&timerMux);
              shared_target_delay = (int)delayUs;
              portEXIT_CRITICAL(&timerMux);
          }
          lastNet = millis();
      }
  }

  // C. Lights
  if (shared_emergency) {
     digitalWrite(PIN_LIGHT_RED, LOW); digitalWrite(PIN_LIGHT_ORANGE, (millis()/100)%2);
  } else if (shared_run) {
     digitalWrite(PIN_LIGHT_RED, LOW); digitalWrite(PIN_LIGHT_ORANGE, (millis()/500)%2);
  } else {
     digitalWrite(PIN_LIGHT_RED, HIGH); digitalWrite(PIN_LIGHT_ORANGE, LOW);
  }

  // D. Telemetry
  if (wifiConnected && millis() - lastTel > 800) {
      // Calculate real speed
      float stepTime = (PULSE_WIDTH + shared_target_delay)/1000000.0;
      float kmh = (shared_run ? (METERS_PER_STEP/stepTime)*3.6 : 0);
      
      FirebaseJson json;
      json.set("speed", kmh);
      json.set("status", shared_run?"MOVING":"STANDBY");
      json.set("lat", gps.location.isValid() ? gps.location.lat() : 0);
      json.set("lng", gps.location.isValid() ? gps.location.lng() : 0);
      
      if (bmi160Ready) {
         int16_t d[6]={0}; bmi160.getAccelGyroData(d);
         json.set("vibration", abs((d[2]/16384.0)-1.0)); 
         json.set("vertical", d[2]/16384.0);
      } else {
         json.set("vibration", 0); json.set("vertical", 0);
      }
      
      if (shared_steps_target > 0) {
          json.set("prog_dist", shared_steps_taken * METERS_PER_STEP);
          json.set("targ_dist", shared_steps_target * METERS_PER_STEP);
      }
      
      Firebase.RTDB.setJSON(&fbDO, "/telemetry", &json);
      lastTel = millis();
  }
  delay(10);
}
