/*
 * PROJECT : MPM-RAIV
 * VERSION : v64.1 (SMOOTH + DISTANCE + FIREBASE FINAL)
 *
 * MOTOR  : PCE5961-BC (5-Phase)
 * DRIVER : INS500 (Bi-Clock)
 * MCU    : ESP32
 */

#include <Arduino.h>
#include <WiFi.h>
#include <Firebase_ESP_Client.h>

#include "addons/TokenHelper.h"
#include "addons/RTDBHelper.h"

// ================================================================
// WIFI + FIREBASE
// ================================================================
#define WIFI_SSID       "4G_AP_1513"
#define WIFI_PASSWORD   "12345678"

#define API_KEY         "AIzaSyD69TabKnrT2AnF3ck6B1gHx5BlVNwYmXs"
#define DATABASE_URL    "mpm-raiv-default-rtdb.asia-southeast1.firebasedatabase.app"

// ================================================================
// PINS (INS500 BI-CLOCK)
// ================================================================
#define PIN_LF 4
#define PIN_LB 14
#define PIN_RF 26
#define PIN_RB 27

#define BTN_FWD 32
#define BTN_BWD 33

#define LED_RED    18
#define LED_ORANGE 19

// ================================================================
// CALIBRATION
// ================================================================
const float WHEEL_CIRC_M  = 0.3635;
const int   STEPS_PER_REV = 1000;
const float ERROR_RATIO  = 1.96;

const float METERS_PER_STEP =
  (WHEEL_CIRC_M / STEPS_PER_REV) * ERROR_RATIO;

// ================================================================
// TIMER
// ================================================================
hw_timer_t *stepTimer = nullptr;
portMUX_TYPE timerMux = portMUX_INITIALIZER_UNLOCKED;

// ================================================================
// STATE
// ================================================================
volatile bool isRunning = false;
volatile bool isForward = true;
volatile bool remoteRun = false;

volatile uint64_t currentSteps = 0;
volatile uint64_t targetSteps  = 0;

// Speed control
volatile uint32_t currentPeriodUs = 12000;
volatile uint32_t targetPeriodUs  = 12000;

// ================================================================
// FIREBASE
// ================================================================
FirebaseData fbDO;
FirebaseData streamDO;
FirebaseAuth auth;
FirebaseConfig config;

unsigned long lastSpeedPoll = 0;

// ================================================================
// SPEED FACTOR → PERIOD
// ================================================================
uint32_t speedFactorToPeriod(int sf) {
  if (sf <= 27)   return 12000;
  if (sf <= 55)   return 8000;
  if (sf <= 72)   return 5000;
  if (sf <= 90)   return 3500;
  if (sf <= 107)  return 2800;
  if (sf <= 125)  return 2200;
  if (sf <= 160)  return 1800;
  return 1500;
}

// ================================================================
// TIMER ISR – SMOOTH ENGINE
// ================================================================
void IRAM_ATTR onStep() {
  portENTER_CRITICAL_ISR(&timerMux);

  if (isRunning) {

    if (currentPeriodUs > targetPeriodUs)
      currentPeriodUs -= 20;
    else if (currentPeriodUs < targetPeriodUs)
      currentPeriodUs += 20;

    if (abs((int)currentPeriodUs - (int)targetPeriodUs) < 20)
      currentPeriodUs = targetPeriodUs;

    timerAlarm(stepTimer, currentPeriodUs, true, 0);

    if (isForward) {
      digitalWrite(PIN_LB, HIGH);
      digitalWrite(PIN_RB, HIGH);
      digitalWrite(PIN_LB, LOW);
      digitalWrite(PIN_RB, LOW);
    } else {
      digitalWrite(PIN_LF, HIGH);
      digitalWrite(PIN_RF, HIGH);
      digitalWrite(PIN_LF, LOW);
      digitalWrite(PIN_RF, LOW);
    }

    currentSteps++;

    if (targetSteps > 0 && currentSteps >= targetSteps) {
      isRunning = false;
      remoteRun = false;
      targetSteps = 0;
    }
  }

  portEXIT_CRITICAL_ISR(&timerMux);
}

// ================================================================
// FIREBASE STREAM CALLBACK
// ================================================================
void streamCallback(FirebaseStream data) {

  if (data.dataTypeEnum() != firebase_rtdb_data_type_string)
    return;

  String cmd = data.stringData();

  // STOP_xxxxx
  if (cmd.startsWith("STOP")) {
    portENTER_CRITICAL(&timerMux);
    isRunning = false;
    remoteRun = false;
    targetSteps = 0;
    portEXIT_CRITICAL(&timerMux);
    return;
  }

  // FWD_x_xxxxx or BWD_x_xxxxx
  if (cmd.startsWith("FWD_") || cmd.startsWith("BWD_")) {

    bool dir = cmd.startsWith("FWD_");

    int firstUnderscore = cmd.indexOf('_');
    int secondUnderscore = cmd.indexOf('_', firstUnderscore + 1);
    if (secondUnderscore == -1) return;

    float meters = cmd.substring(firstUnderscore + 1, secondUnderscore).toFloat();
    if (meters <= 0) return;

    portENTER_CRITICAL(&timerMux);

    isForward = dir;
    currentSteps = 0;
    targetSteps = (uint64_t)(meters / METERS_PER_STEP);

    remoteRun = true;
    isRunning = true;

    portEXIT_CRITICAL(&timerMux);
  }
}

// ================================================================
// SETUP
// ================================================================
void setup() {
  Serial.begin(115200);

  pinMode(PIN_LF, OUTPUT);
  pinMode(PIN_LB, OUTPUT);
  pinMode(PIN_RF, OUTPUT);
  pinMode(PIN_RB, OUTPUT);

  pinMode(BTN_FWD, INPUT_PULLUP);
  pinMode(BTN_BWD, INPUT_PULLUP);

  pinMode(LED_RED, OUTPUT);
  pinMode(LED_ORANGE, OUTPUT);

  stepTimer = timerBegin(1000000);
  timerAttachInterrupt(stepTimer, &onStep);
  timerAlarm(stepTimer, currentPeriodUs, true, 0);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) delay(200);

  config.api_key = API_KEY;
  config.database_url = DATABASE_URL;
  config.token_status_callback = tokenStatusCallback;

  Firebase.signUp(&config, &auth, "", "");
  Firebase.begin(&config, &auth);
  Firebase.reconnectWiFi(true);

  Firebase.RTDB.beginStream(&streamDO, "/command");
  Firebase.RTDB.setStreamCallback(&streamDO, streamCallback, nullptr);
}

// ================================================================
// LOOP
// ================================================================
void loop() {

  // SPEED FACTOR
  if (Firebase.ready() && millis() - lastSpeedPoll > 300) {
    if (Firebase.RTDB.getInt(&fbDO, "/speed_factor")) {
      portENTER_CRITICAL(&timerMux);
      targetPeriodUs = speedFactorToPeriod(fbDO.intData());
      portEXIT_CRITICAL(&timerMux);
    }
    lastSpeedPoll = millis();
  }

  // MANUAL BUTTONS (OVERRIDE)
  if (digitalRead(BTN_FWD) == LOW) {
    isForward = true;
    isRunning = true;
    remoteRun = false;
  }
  else if (digitalRead(BTN_BWD) == LOW) {
    isForward = false;
    isRunning = true;
    remoteRun = false;
  }
  else {
    isRunning = remoteRun;
  }

  // STATUS LED
  if (isRunning) {
    digitalWrite(LED_RED, LOW);
    digitalWrite(LED_ORANGE, (millis() / 300) % 2);
  } else {
    digitalWrite(LED_RED, HIGH);
    digitalWrite(LED_ORANGE, LOW);
  }

  delay(5);
}
