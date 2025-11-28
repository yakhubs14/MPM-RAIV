/*
 * PROJECT MPM-RAIV - FINAL FIRMWARE v4.0
 * Hardware: ESP32, INS500 Drivers, LC76G GPS, BMI160 Gyro, Lights
 * Features: Auto Hold, Distance Travel, Telemetry, Cloud Control
 */

#include <Arduino.h>
#include <WiFi.h>
#include <Firebase_ESP_Client.h>
#include <Wire.h>
#include <BMI160Gen.h> // Library: BMI160Gen by Curio Res
#include <TinyGPS++.h> // Library: TinyGPSPlus

#include "addons/TokenHelper.h"
#include "addons/RTDBHelper.h"

// --- 1. CONFIGURATION ---
#define WIFI_SSID "Mpmunifi_2.4Ghz"
#define WIFI_PASSWORD "Mpmsb@2005"

// Your Specific Project Credentials
#define API_KEY "AIzaSyD69TabKnrT2AnF3ck6B1gHx5BlVNwYmXs"
#define DATABASE_URL "mpm-raiv-default-rtdb.asia-southeast1.firebasedatabase.app"

// --- 2. PIN DEFINITIONS ---
// Motor Pins (Active LOW logic)
const int PIN_LF = 4;   // Left Forward
const int PIN_LB = 14;  // Left Backward
const int PIN_RF = 26;  // Right Forward
const int PIN_RB = 27;  // Right Backward

// Physical Buttons
const int BTN_FWD = 32; 
const int BTN_BWD = 33; 

// Status Lights
const int PIN_LIGHT_GREEN  = 18; // Standby
const int PIN_LIGHT_ORANGE = 19; // Moving

// GPS (LC76G)
#define GPS_RX_PIN 16 // Connect to GPS TX
#define GPS_TX_PIN 17 // Connect to GPS RX
#define GPS_BAUD 115200 

// --- 3. PHYSICS CONSTANTS ---
// Wheel Dia = 11.57cm -> Circumference = 0.3635m
const float WHEEL_CIRC = 0.3635; 
const int STEPS_PER_REV = 500;   // INS500 Standard
const int PULSE_WIDTH = 500;     // Microseconds
const int SPEED_DELAY = 1500;    // Microseconds

// Distance per single step (approx 0.000727 meters)
const float METERS_PER_STEP = WHEEL_CIRC / STEPS_PER_REV;

// --- 4. OBJECTS ---
FirebaseData fbDO;
FirebaseAuth auth;
FirebaseConfig config;
bool signupOK = false;

TinyGPSPlus gps;
HardwareSerial SerialGPS(2); 

// Variables
String cloudCommand = "STOP"; 
unsigned long lastTelemetryTime = 0;
bool isMoving = false;
float currentSpeedKmH = 0.0;

// Distance Control Variables
float targetDistanceMeters = 0; // 0 means Manual Mode
float distanceCoveredSession = 0;

void setup() {
  Serial.begin(115200);
  
  // 1. Init GPS
  SerialGPS.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);
  
  // 2. Init BMI160
  Serial.println("Init BMI160...");
  BMI160.begin(BMI160Gen::I2C_MODE, 0x68);
  BMI160.setAccelerometerRange(8); // +/- 8g range for bumps
  
  // 3. Init Pins
  pinMode(PIN_LF, OUTPUT); pinMode(PIN_LB, OUTPUT);
  pinMode(PIN_RF, OUTPUT); pinMode(PIN_RB, OUTPUT);
  // Turn OFF motors initially (High = Off for Active Low)
  digitalWrite(PIN_LF, HIGH); digitalWrite(PIN_LB, HIGH);
  digitalWrite(PIN_RF, HIGH); digitalWrite(PIN_RB, HIGH);

  pinMode(PIN_LIGHT_GREEN, OUTPUT);
  pinMode(PIN_LIGHT_ORANGE, OUTPUT);
  pinMode(BTN_FWD, INPUT_PULLUP);
  pinMode(BTN_BWD, INPUT_PULLUP);

  // 4. WiFi Connection
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.println("\nWiFi Connected!");

  // 5. Firebase Connection
  config.api_key = API_KEY;
  config.database_url = DATABASE_URL;
  if (Firebase.signUp(&config, &auth, "", "")) {
    Serial.println("Firebase Ready");
    signupOK = true;
  }
  Firebase.begin(&config, &auth);
  Firebase.reconnectWiFi(true);
}

void loop() {
  // Always read GPS data
  while (SerialGPS.available() > 0) gps.encode(SerialGPS.read());

  // --- 1. READ COMMANDS ---
  if (Firebase.ready() && signupOK) {
    if (Firebase.RTDB.getString(&fbDO, "/command")) {
      String newCmd = fbDO.stringData();
      
      // Only process if command CHANGED
      if (newCmd != cloudCommand) {
        cloudCommand = newCmd;
        Serial.println("New CMD: " + cloudCommand);

        // PARSE COMMAND TYPE
        if (cloudCommand == "STOP") {
          isMoving = false;
          targetDistanceMeters = 0;
        }
        else if (cloudCommand == "FORWARD") {
          // Manual / Auto Hold Mode (Infinite)
          isMoving = true;
          targetDistanceMeters = 0; 
        }
        else if (cloudCommand == "BACKWARD") {
          isMoving = true;
          targetDistanceMeters = 0;
        }
        // Distance Parsing: FWD_10.5 -> Forward 10.5m
        else if (cloudCommand.startsWith("FWD_")) {
          isMoving = true;
          distanceCoveredSession = 0;
          targetDistanceMeters = cloudCommand.substring(4).toFloat();
        }
        else if (cloudCommand.startsWith("BWD_")) {
          isMoving = true;
          distanceCoveredSession = 0;
          targetDistanceMeters = cloudCommand.substring(4).toFloat();
        }
      }
    }
  }

  // --- 2. PHYSICAL OVERRIDE ---
  bool physFwd = (digitalRead(BTN_FWD) == LOW);
  bool physBwd = (digitalRead(BTN_BWD) == LOW);

  // --- 3. MOVEMENT LOGIC ---
  
  // Determine Direction (Physical OR Cloud)
  bool goingForward = (physFwd || cloudCommand == "FORWARD" || cloudCommand.startsWith("FWD_"));
  bool goingBackward = (physBwd || cloudCommand == "BACKWARD" || cloudCommand.startsWith("BWD_"));
  
  // Stop Condition: If cloud says STOP and no buttons pressed
  if (cloudCommand == "STOP" && !physFwd && !physBwd) {
    isMoving = false;
  }

  // AUTO STOP Logic (Distance Mode)
  if (targetDistanceMeters > 0 && distanceCoveredSession >= targetDistanceMeters) {
    isMoving = false;
    targetDistanceMeters = 0; // Reset target
    cloudCommand = "STOP";
    if(signupOK) Firebase.RTDB.setString(&fbDO, "/command", "STOP"); // Tell Web to stop
  }

  // EXECUTE MOTOR MOVEMENT
  if (isMoving) {
    if (goingForward) {
      pulseTwoPins(PIN_LF, PIN_RF);
    } else if (goingBackward) {
      pulseTwoPins(PIN_LB, PIN_RB);
    }
    
    // Track Distance
    distanceCoveredSession += METERS_PER_STEP;
    calculateSpeed();
    
    // Lights: Orange ON, Green OFF
    digitalWrite(PIN_LIGHT_ORANGE, HIGH);
    digitalWrite(PIN_LIGHT_GREEN, LOW);
  } 
  else {
    // STOPPED
    // Lights: Orange OFF, Green ON
    digitalWrite(PIN_LIGHT_ORANGE, LOW);
    digitalWrite(PIN_LIGHT_GREEN, HIGH);
    currentSpeedKmH = 0.0;
  }

  // --- 4. TELEMETRY (Send every 500ms) ---
  if (millis() - lastTelemetryTime > 500) {
    uploadTelemetry();
    lastTelemetryTime = millis();
  }
}

// Helper to pulse two motors simultaneously
void pulseTwoPins(int pin1, int pin2) {
  digitalWrite(pin1, LOW); // ON
  digitalWrite(pin2, LOW);
  delayMicroseconds(PULSE_WIDTH);
  digitalWrite(pin1, HIGH); // OFF
  digitalWrite(pin2, HIGH);
  delayMicroseconds(SPEED_DELAY);
}

// Virtual Speedometer Calculation
void calculateSpeed() {
  float stepTimeSec = (PULSE_WIDTH + SPEED_DELAY) / 1000000.0;
  float metersPerSec = METERS_PER_STEP / stepTimeSec;
  currentSpeedKmH = metersPerSec * 3.6;
}

// Upload Sensor Data to Firebase
void uploadTelemetry() {
  if (!signupOK) return;

  // Read BMI160 Accelerometer
  int ax, ay, az;
  BMI160.readAccelerometer(ax, ay, az);
  
  // Convert raw (32768 = 8g) to G-Force
  float az_g = az / 4096.0; 
  // Vibration = fluctuation around 1.0g (gravity)
  float vibration = abs(az_g - 1.0);

  // Read GPS
  double lat = gps.location.isValid() ? gps.location.lat() : 0.0;
  double lng = gps.location.isValid() ? gps.location.lng() : 0.0;

  // Build JSON Package
  FirebaseJson json;
  json.set("speed", currentSpeedKmH);
  json.set("vibration", vibration);
  json.set("vertical", az_g);
  json.set("lat", lat);
  json.set("lng", lng);
  json.set("status", isMoving ? "MOVING" : "STANDBY");
  
  // Send Progress if in Auto Mode
  if(targetDistanceMeters > 0) {
    json.set("prog_dist", distanceCoveredSession);
    json.set("targ_dist", targetDistanceMeters);
  }

  // Send to Cloud
  Firebase.RTDB.setJSON(&fbDO, "/telemetry", &json);
}
