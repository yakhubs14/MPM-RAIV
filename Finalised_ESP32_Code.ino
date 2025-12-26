/*
 * PROJECT MPM-RAIV - FIRMWARE v54.0 (TORQUE & STARTUP FIX)
 * -----------------------------------------------------------
 * CHANGELOG:
 * - FIX (Stalling): Implemented "Soft Start" logic. Ramps up much slower 
 * from a high-torque low speed to prevent stalling.
 * - FIX (Resonance): Adjusted acceleration curve to be smoother.
 * - CORE: Maintained Interrupt-Driven Pulse for smoothness.
 */

#include <Arduino.h>
#include <WiFi.h>
#include <Firebase_ESP_Client.h>
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

// ================================================================
// 3. PHYSICS & CALIBRATION
// ================================================================
const float WHEEL_CIRC_M    = 0.3635; 
const int STEPS_PER_REV     = 1000; 
const float ERROR_RATIO     = 1.96; 
const float METERS_PER_STEP = (WHEEL_CIRC_M / STEPS_PER_REV) * ERROR_RATIO; 
const double SIM_LAT = 3.128625;
const double SIM_LNG = 101.738602;

// ================================================================
// 4. SHARED VARIABLES (VOLATILE FOR ISR)
// ================================================================
volatile bool isRunning = false;
volatile bool isForward = true;
volatile uint64_t targetSteps = 0;
volatile uint64_t currentSteps = 0;
volatile bool isManualHold = false;

// RAMPING VARIABLES
volatile int currentDelayUs = 5000; // Start very slow for torque
volatile int targetDelayUs = 5000;

// ISR LOCK
portMUX_TYPE timerMux = portMUX_INITIALIZER_UNLOCKED;
hw_timer_t * timer = NULL;

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
// INTERRUPT SERVICE ROUTINE (THE HEARTBEAT)
// ================================================================
void IRAM_ATTR onTimer() {
  portENTER_CRITICAL_ISR(&timerMux);
  
  if (isRunning) {
    // 1. Ramping Logic (Soft Start / Soft Stop)
    // Only ramp every 10th pulse to make it smoother
    static int rampCounter = 0;
    rampCounter++;
    if (rampCounter > 10) {
        rampCounter = 0;
        if (currentDelayUs > targetDelayUs) {
             currentDelayUs -= 10; // Accelerate slowly (decrease delay)
             if (currentDelayUs < targetDelayUs) currentDelayUs = targetDelayUs;
             // Update timer instantly
             timerAlarm(timer, currentDelayUs, true, 0); 
        } 
        else if (currentDelayUs < targetDelayUs) {
             currentDelayUs += 10; // Decelerate
             if (currentDelayUs > targetDelayUs) currentDelayUs = targetDelayUs;
             timerAlarm(timer, currentDelayUs, true, 0);
        }
    }

    // 2. Pulse Generation (SWAPPED LOGIC)
    if (isForward) {
       digitalWrite(PIN_LB, HIGH); digitalWrite(PIN_RB, HIGH);
       for(volatile int i=0; i<50; i++); 
       digitalWrite(PIN_LB, LOW); digitalWrite(PIN_RB, LOW);
    } else {
       digitalWrite(PIN_LF, HIGH); digitalWrite(PIN_RF, HIGH);
       for(volatile int i=0; i<50; i++); 
       digitalWrite(PIN_LF, LOW); digitalWrite(PIN_RF, LOW);
    }

    // 3. Count
    currentSteps++;

    // 4. Auto-Stop Check
    if (!isManualHold && targetSteps > 0) {
        if (currentSteps >= targetSteps) {
            isRunning = false; // Target Reached
        }
    }
  }
  
  portEXIT_CRITICAL_ISR(&timerMux);
}

// Function to update Target Frequency
void setSpeed(int delayUs) {
    if (delayUs < 500) delayUs = 500; 
    targetDelayUs = delayUs; // Update Target, ISR handles ramping
}

// Helper to INSTANTLY KILL MOTORS
void forceStop() {
    portENTER_CRITICAL(&timerMux);
    isRunning = false;
    targetSteps = 0;
    isManualHold = false;
    currentDelayUs = 5000; // Reset to slow start
    portEXIT_CRITICAL(&timerMux);
    Serial.println("!!! FORCE STOP !!!");
}

// ================================================================
// STREAM CALLBACK (WEB COMMANDS)
// ================================================================
void streamCallback(FirebaseStream data) {
  if (data.dataTypeEnum() == firebase_rtdb_data_type_string) {
      String c = data.stringData();
      Serial.println("STREAM: " + c);
      cloudCommand = c;

      // PRIORITY 1: STOP COMMAND
      if (c == "STOP" || c.indexOf("STOP") != -1) { 
          forceStop(); 
      }
      // PRIORITY 2: MOVE COMMAND
      else if (c.startsWith("FWD_") || c.startsWith("BWD_")) {
          int firstUnderscore = 3;
          int secondUnderscore = c.indexOf('_', 4);
          String distStr = (secondUnderscore != -1) ? c.substring(4, secondUnderscore) : c.substring(4);
          
          float m = distStr.toFloat();
          if (m > 0) {
              portENTER_CRITICAL(&timerMux);
              isForward = c.startsWith("FWD_");
              currentSteps = 0;
              targetSteps = (uint64_t)(m / METERS_PER_STEP);
              isManualHold = false;
              isRunning = true;
              currentDelayUs = 5000; // Reset Ramp for torque
              portEXIT_CRITICAL(&timerMux);
              
              // Initial slow timer to start torque
              timerAlarm(timer, 5000, true, 0); 
              Serial.printf("Target Steps: %llu\n", targetSteps);
          }
      }
  }
}

void streamTimeoutCallback(bool timeout) { if (timeout) Serial.println("Stream Timeout..."); }

// ================================================================
// SETUP
// ================================================================
void setup() {
  Serial.begin(115200);
  Serial.println("\n--- RAIV v54.0 (TORQUE FIX) ---");

  // Pins
  pinMode(PIN_LF, OUTPUT); pinMode(PIN_LB, OUTPUT); 
  pinMode(PIN_RF, OUTPUT); pinMode(PIN_RB, OUTPUT);
  digitalWrite(PIN_LF, LOW); digitalWrite(PIN_LB, LOW);
  digitalWrite(PIN_RF, LOW); digitalWrite(PIN_RB, LOW);

  pinMode(BTN_FWD, INPUT_PULLUP); pinMode(BTN_BWD, INPUT_PULLUP);
  pinMode(PIN_LIGHT_RED, OUTPUT); pinMode(PIN_LIGHT_ORANGE, OUTPUT);

  // --- TIMER SETUP ---
  timer = timerBegin(1000000);
  timerAttachInterrupt(timer, &onTimer);
  timerAlarm(timer, 5000, true, 0); // Start slow

  // WiFi
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while(WiFi.status() != WL_CONNECTED) { delay(200); Serial.print("."); }
  
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

// ================================================================
// LOOP
// ================================================================
void loop() {
  // 1. Manual Buttons
  bool btnF = (digitalRead(BTN_FWD) == LOW);
  bool btnB = (digitalRead(BTN_BWD) == LOW);

  if (btnF) {
      portENTER_CRITICAL(&timerMux);
      if (!isRunning || !isForward) { 
          isRunning = true;
          isForward = true;
          isManualHold = true;
          currentDelayUs = 5000; // Reset Ramp
          setSpeed(3000); 
          timerAlarm(timer, 5000, true, 0);
      }
      portEXIT_CRITICAL(&timerMux);
  } 
  else if (btnB) {
      portENTER_CRITICAL(&timerMux);
      if (!isRunning || isForward) {
          isRunning = true;
          isForward = false;
          isManualHold = true;
          currentDelayUs = 5000; 
          setSpeed(3000);
          timerAlarm(timer, 5000, true, 0);
      }
      portEXIT_CRITICAL(&timerMux);
  }
  else if (isManualHold) {
      forceStop();
  }

  // 2. Poll Speed Factor (Auto Mode Only)
  if (signupOK && !isManualHold && isRunning && millis() - lastSpeedPoll > 1000) {
      if (Firebase.RTDB.getInt(&fbDO, "/speed_factor")) {
          int pct = fbDO.intData();
          float selectedSpeedMs = (pct - 20) / 35.0; 
          
          if (selectedSpeedMs >= 1.0) {
               selectedSpeedMs = selectedSpeedMs * 0.70;
          }

          float actualSpeedMs = 0.1; 
          if (selectedSpeedMs <= 0.3)  actualSpeedMs = selectedSpeedMs * 0.70;
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
          int delayUs = (int)((METERS_PER_STEP / actualSpeedMs) * 1000000.0);
          
          portENTER_CRITICAL(&timerMux);
          setSpeed(delayUs);
          portEXIT_CRITICAL(&timerMux);
      }
      lastSpeedPoll = millis();
  }

  // 3. Telemetry
  if (signupOK && millis() - lastTel > 1000) {
      float kmh = isRunning ? 1.0 : 0.0; 
      
      FirebaseJson json;
      json.set("speed", kmh);
      json.set("status", isRunning?"MOVING":"STANDBY");
      json.set("lat", SIM_LAT); 
      json.set("lng", SIM_LNG);
      json.set("vibration", 0); 
      json.set("vertical", 0);
      
      if (!isManualHold && targetSteps > 0) {
           double t = (double)targetSteps * METERS_PER_STEP;
           double p = (double)currentSteps * METERS_PER_STEP;
           json.set("targ_dist", t);
           json.set("prog_dist", p);
      }
      
      if (!isRunning && !isManualHold && targetSteps > 0 && currentSteps >= targetSteps) {
           Firebase.RTDB.setString(&fbDO, "/command", "STOP");
           targetSteps = 0; 
      }
      
      Firebase.RTDB.setJSON(&fbDO, "/telemetry", &json);
      lastTel = millis();
  }
  
  // Lights
  if (isRunning) {
     digitalWrite(PIN_LIGHT_RED, LOW); digitalWrite(PIN_LIGHT_ORANGE, (millis()/500)%2);
  } else {
     digitalWrite(PIN_LIGHT_RED, HIGH); digitalWrite(PIN_LIGHT_ORANGE, LOW);
  }

  delay(10); 
}
