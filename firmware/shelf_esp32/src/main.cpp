// Shelf controller (F12) for the LilyGO T-Display-S3. Section 12.3 adapted to discrete LEDs.
// A dumb display and button hat: no cart logic. Protocol (9.7), newline-terminated ASCII:
//   in:  LED,<bay>,ON|OFF   SHELF,GREEN|RED|IDLE   GATE,OPEN|CLOSED|IDLE   PING
//   out: READY (boot)   PONG   BTN,0 (GPIO 14 button held 1 s)
// Timed effects (green for 3 s) are the backend's job: it sends SHELF,IDLE afterward.
// The onboard LCD is left alone (a later step drives it).

#include <Arduino.h>

#define PIN_POWER_ON 15  // peripheral power enable, must be HIGH first
#define BTN_PIN      14  // onboard BUTTON_2 (GPIO 0 is BOOT)
#define NUM_BAYS     3

const uint8_t BAY_PINS[NUM_BAYS] = {1, 2, 18};  // single-color LEDs, active HIGH

// Status RGB LED, common cathode: higher duty = brighter.
#define RGB_R_PIN 10
#define RGB_G_PIN 11
#define RGB_B_PIN 12
#define RGB_COMMON_ANODE 0  // set to 1 for a common-anode LED (inverts the duty)
#define PWM_FREQ 5000
#define PWM_BITS 8

#if ESP_ARDUINO_VERSION_MAJOR < 3
const uint8_t RGB_CH[3] = {0, 1, 2};  // LEDC channels for R, G, B (core 2.x API)
#endif
const uint8_t RGB_PINS[3] = {RGB_R_PIN, RGB_G_PIN, RGB_B_PIN};

enum ShelfMode { SHELF_IDLE, SHELF_GREEN, SHELF_RED };
enum GateMode  { GATE_IDLE, GATE_OPEN, GATE_CLOSED };
ShelfMode shelfMode = SHELF_IDLE;
GateMode gateMode = GATE_IDLE;
bool bayOn[NUM_BAYS] = {true, true, true};
String line;
unsigned long btnDownAt = 0;
bool btnSent = false;

void pwmWrite(int i, uint8_t duty) {
  if (RGB_COMMON_ANODE) duty = 255 - duty;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(RGB_PINS[i], duty);
#else
  ledcWrite(RGB_CH[i], duty);
#endif
}

void setRgb(uint8_t r, uint8_t g, uint8_t b) {
  pwmWrite(0, r);
  pwmWrite(1, g);
  pwmWrite(2, b);
}

void handleCommand(const String& cmd) {
  if (cmd == "PING") { Serial.println("PONG"); return; }
  if (cmd.startsWith("LED,")) {
    int comma = cmd.indexOf(',', 4);
    if (comma < 0) return;
    int bay = cmd.substring(4, comma).toInt();
    String state = cmd.substring(comma + 1);
    if (bay >= 0 && bay < NUM_BAYS && (state == "ON" || state == "OFF")) bayOn[bay] = state == "ON";
    return;
  }
  if (cmd == "SHELF,GREEN") shelfMode = SHELF_GREEN;
  else if (cmd == "SHELF,RED") shelfMode = SHELF_RED;
  else if (cmd == "SHELF,IDLE") shelfMode = SHELF_IDLE;
  else if (cmd == "GATE,OPEN") gateMode = GATE_OPEN;
  else if (cmd == "GATE,CLOSED") gateMode = GATE_CLOSED;
  else if (cmd == "GATE,IDLE") gateMode = GATE_IDLE;
}

void pollSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') { line.trim(); if (line.length()) handleCommand(line); line = ""; }
    else if (line.length() < 64) line += c;
  }
}

void pollButton() {
  bool down = digitalRead(BTN_PIN) == LOW;
  if (down && btnDownAt == 0) { btnDownAt = millis(); btnSent = false; }
  if (!down) btnDownAt = 0;
  if (down && !btnSent && millis() - btnDownAt > 1000) { Serial.println("BTN,0"); btnSent = true; }
}

void render() {
  for (int i = 0; i < NUM_BAYS; i++) digitalWrite(BAY_PINS[i], bayOn[i] ? HIGH : LOW);

  // A gate state overrides the shelf state; GATE,IDLE falls back to it.
  if (gateMode == GATE_OPEN)          setRgb(0, 200, 40);
  else if (gateMode == GATE_CLOSED)   setRgb(200, 0, 0);
  else if (shelfMode == SHELF_GREEN)  setRgb(0, 180, 40);
  else if (shelfMode == SHELF_RED)    setRgb(200, 0, 0);
  else {
    float breathe = (sin(millis() / 600.0) + 1.0) / 2.0;  // 0..1
    uint8_t w = 4 + (uint8_t)(36 * breathe);               // soft white
    setRgb(w, w, w);
  }
}

void setup() {
  pinMode(PIN_POWER_ON, OUTPUT);
  digitalWrite(PIN_POWER_ON, HIGH);

  Serial.begin(115200);
  pinMode(BTN_PIN, INPUT_PULLUP);
  for (int i = 0; i < NUM_BAYS; i++) pinMode(BAY_PINS[i], OUTPUT);
  for (int i = 0; i < 3; i++) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttach(RGB_PINS[i], PWM_FREQ, PWM_BITS);
#else
    ledcSetup(RGB_CH[i], PWM_FREQ, PWM_BITS);
    ledcAttachPin(RGB_PINS[i], RGB_CH[i]);
#endif
  }
  render();
  Serial.println("READY");
}

void loop() {
  pollSerial();
  pollButton();
  render();
  delay(10);
}
