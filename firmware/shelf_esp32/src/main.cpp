// Gate screen (F12) for the LilyGO T-Display-S3: USB powered, nothing wired to it. Section 12.3 adapted.
// A dumb display and button hat: no cart logic. Protocol (9.7), newline-terminated ASCII:
//   in:  DISP,IDLE | DISP,WELCOME,<name> | DISP,TOTAL,<total>,<count> | DISP,PAID,<total>,<auth>
//        DISP,DECLINED | DISP,OCCUPIED,<name>          onboard LCD as the store's gate display
//        DISP,FIND,<bays>                              "Find bay 2 and 4" for a new plan, 6 s, then back
//        PING
//   legacy, still accepted but no longer sent (the build has no bay or status LEDs):
//        LED,<bay>,ON|OFF   SHELF,GREEN|RED|IDLE   GATE,OPEN|CLOSED|IDLE   HILITE,<bay>,ON|OFF|ALL,OFF
//   out: READY (boot)   PONG   BTN,0 (GPIO 14 button held 1 s)
// Timed screens are the backend's job, except FIND, which also reverts here so a lost line can't strand it.
// DISP arguments never contain commas (the backend strips them).

#include <Arduino.h>
#include <Arduino_GFX_Library.h>

#define PIN_POWER_ON 15  // peripheral power enable (LCD too), must be HIGH first
#define BTN_PIN      14  // onboard BUTTON_2 (GPIO 0 is BOOT)
#define NUM_BAYS     5  // the one place the bay count lives; BAY_PINS must have exactly this many entries

// Single-color LEDs, active HIGH. Every pin is broken out on the side headers and free on the
// plain (non-touch, no SD shield) T-Display-S3: 1, 2 are general GPIO, and 18/17/21 are the
// board's IIC SDA / IIC SCL / Touch RES, which only the touch variant drives. None is an
// ESP32-S3 strapping pin (0, 3, 45, 46), USB (19, 20), battery sense (4), or an LCD pin.
const uint8_t BAY_PINS[NUM_BAYS] = {1, 2, 18, 17, 21};
static_assert(sizeof(BAY_PINS) / sizeof(BAY_PINS[0]) == NUM_BAYS, "BAY_PINS needs one pin per bay");

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

// --- Onboard LCD: ST7789 170x320 on the ESP32-S3 8-bit parallel (i80) bus. ---
// Pins from the official repo, Xinyuan-LilyGO/T-Display-S3 examples/factory/pin_config.h:
//   PIN_LCD_BL 38, PIN_LCD_D0..D7 39 40 41 42 45 46 47 48,
//   PIN_LCD_RES 5, PIN_LCD_CS 6, PIN_LCD_DC 7, PIN_LCD_WR 8, PIN_LCD_RD 9, PIN_POWER_ON 15.
#define PIN_LCD_BL  38
#define PIN_LCD_RES 5
#define PIN_LCD_CS  6
#define PIN_LCD_DC  7
#define PIN_LCD_WR  8
#define PIN_LCD_RD  9

Arduino_DataBus *bus = new Arduino_ESP32PAR8Q(PIN_LCD_DC, PIN_LCD_CS, PIN_LCD_WR, PIN_LCD_RD,
                                              39, 40, 41, 42, 45, 46, 47, 48);
// Rotation 1 = landscape (320 wide x 170 tall). The 170-px panel sits at column offset 35 in the ST7789's RAM.
Arduino_GFX *gfx = new Arduino_ST7789(bus, PIN_LCD_RES, 1 /* rotation */, true /* IPS */,
                                      170 /* width */, 320 /* height */, 35, 0, 35, 0);

#define SCR_W 320
#define SCR_H 170
#define MK565(r, g, b) ((uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3)))
const uint16_t C_BG     = MK565(10, 12, 18);
const uint16_t C_TEXT   = MK565(240, 240, 240);
const uint16_t C_DIM    = MK565(140, 145, 155);
const uint16_t C_ACCENT = MK565(255, 140, 20);   // SpeedMart orange
const uint16_t C_GREEN  = MK565(40, 200, 90);
const uint16_t C_RED    = MK565(220, 30, 30);
const uint16_t C_AMBER  = MK565(255, 190, 40);

bool displayOk = false;
String dispKey;       // the last DISP command drawn; the same command again is a no-op (no flicker)
String dispScreen;    // IDLE, WELCOME, ... (drives the idle animation)
unsigned long lastAnimAt = 0;
uint16_t dotColor[5];
#define FIND_HOLD_MS 6000
String totalKey;      // the last DISP,TOTAL drawn since IDLE: where FIND returns to
String findBackKey;   // the screen FIND covered, used when there is no TOTAL yet (e.g. IDLE)
unsigned long findAt = 0;

enum ShelfMode { SHELF_IDLE, SHELF_GREEN, SHELF_RED };
enum GateMode  { GATE_IDLE, GATE_OPEN, GATE_CLOSED };
ShelfMode shelfMode = SHELF_IDLE;
GateMode gateMode = GATE_IDLE;
bool bayOn[NUM_BAYS];              // every bay starts lit; set in setup()
bool bayHilite[NUM_BAYS];          // blinks over bayOn; OFF falls back to bayOn (zero-init = false)
#define HILITE_HALF_PERIOD_MS 250                   // 250 ms on + 250 ms off = 2 Hz
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

// --- display drawing: filled rectangles + the built-in 6x8 font scaled up. ---

// Largest text size in [minSize, maxSize] whose width fits maxW.
uint8_t fitSize(const String& s, int maxW, uint8_t maxSize, uint8_t minSize = 1) {
  uint8_t size = maxSize;
  while (size > minSize && (int)s.length() * 6 * size > maxW) size--;
  return size;
}

// Draw s horizontally centered in [x0, x0 + w) with its top at y. Returns the text height.
int drawCentered(const String& s, int x0, int w, int y, uint8_t size, uint16_t color) {
  int tw = s.length() * 6 * size - size;  // the last glyph's spacing column is blank
  gfx->setTextSize(size);
  gfx->setTextColor(color);
  gfx->setCursor(x0 + (w - tw) / 2, y);
  gfx->print(s);
  return 8 * size;
}

int drawFit(const String& s, int x0, int w, int y, uint8_t maxSize, uint16_t color) {
  return drawCentered(s, x0, w, y, fitSize(s, w - 8, maxSize), color);
}

void thickLine(int x0, int y0, int x1, int y1, int r, uint16_t color) {
  for (int dx = -r; dx <= r; dx++)
    for (int dy = -r; dy <= r; dy++)
      if (dx * dx + dy * dy <= r * r) gfx->drawLine(x0 + dx, y0 + dy, x1 + dx, y1 + dy, color);
}

void drawIdle() {
  gfx->fillRect(0, 0, SCR_W, 6, C_ACCENT);
  // "SpeedMart" at size 5 (30 px per glyph), drawn in two colors, centered as one word.
  int x = (SCR_W - (9 * 30 - 5)) / 2;
  gfx->setTextSize(5);
  gfx->setCursor(x, 38);
  gfx->setTextColor(C_TEXT);
  gfx->print("Speed");
  gfx->setTextColor(C_ACCENT);
  gfx->print("Mart");
  drawCentered("Tap or scan to enter", 0, SCR_W, 104, 2, C_DIM);
  for (int i = 0; i < 5; i++) dotColor[i] = 0;  // force the animation to paint every dot
}

// Soft idle animation: a glow wave across five dots. Only dots whose color changed are repainted.
void animateIdle() {
  float t = millis() / 450.0;
  for (int i = 0; i < 5; i++) {
    float k = (sin(t - i * 0.9) + 1.0) / 2.0;  // 0..1
    uint16_t c = MK565((uint8_t)(40 + 215 * k), (uint8_t)(40 + 100 * k), (uint8_t)(45 - 25 * k));
    if (c == dotColor[i]) continue;
    dotColor[i] = c;
    gfx->fillCircle(SCR_W / 2 + (i - 2) * 24, 146, 5, c);
  }
}

void drawWelcome(const String& name) {
  gfx->fillRect(0, 0, SCR_W, 10, C_GREEN);
  drawCentered("Welcome,", 0, SCR_W, 34, 3, C_TEXT);
  drawFit(name, 0, SCR_W, 74, 5, C_GREEN);
  drawCentered("Happy shopping", 0, SCR_W, 136, 2, C_DIM);
}

void drawTotal(const String& total, const String& count) {
  gfx->fillRect(0, 0, SCR_W, 6, C_ACCENT);
  drawCentered("Your cart", 0, SCR_W, 18, 2, C_DIM);
  drawFit(total, 0, SCR_W, 50, 7, C_TEXT);
  long n = count.toInt();
  String items = String(n) + (n == 1 ? " item" : " items");
  drawCentered(items, 0, SCR_W, 122, 3, C_ACCENT);
}

void drawPaid(const String& total, const String& auth) {
  gfx->fillRect(0, 0, SCR_W, 10, C_GREEN);
  int cx = 64, cy = 90;
  gfx->fillCircle(cx, cy, 44, C_GREEN);
  thickLine(cx - 22, cy + 2, cx - 6, cy + 18, 4, C_BG);
  thickLine(cx - 6, cy + 18, cx + 24, cy - 16, 4, C_BG);
  const int x0 = 116, w = SCR_W - x0;
  drawFit("APPROVED", x0, w, 30, 3, C_GREEN);
  drawFit(total, x0, w, 70, 5, C_TEXT);
  if (auth.length()) drawFit("Auth " + auth, x0, w, 128, 2, C_DIM);
}

void drawDeclined() {
  gfx->fillRect(0, 0, SCR_W, 10, C_RED);
  gfx->fillRect(0, SCR_H - 10, SCR_W, 10, C_RED);
  drawCentered("DECLINED", 0, SCR_W, 44, 5, C_RED);
  drawCentered("Try again on your phone", 0, SCR_W, 116, 2, C_TEXT);
}

void drawOccupied(const String& name) {
  gfx->fillRect(0, 0, SCR_W, 10, C_AMBER);
  drawFit(name + " is shopping", 0, SCR_W, 46, 3, C_TEXT);
  drawCentered("Please wait", 0, SCR_W, 104, 4, C_AMBER);
}

void drawFind(const String& bays) {
  gfx->fillRect(0, 0, SCR_W, 10, C_ACCENT);
  drawCentered("Find bay", 0, SCR_W, 26, 4, C_TEXT);
  drawFit(bays, 0, SCR_W, 70, 7, C_ACCENT);  // "2 and 4" at size 7; "1 2 3 4 and 5" shrinks to fit
  drawCentered("Look for the number cards", 0, SCR_W, 140, 2, C_DIM);
}

// cmd is the whole line, e.g. "DISP,TOTAL,$12.96,3". Redraws only when it differs from the last one.
void handleDisp(const String& cmd) {
  if (cmd == dispKey) return;
  String parts[3];
  int n = 0, start = 5;  // after "DISP,"
  while (n < 3) {
    int comma = cmd.indexOf(',', start);
    parts[n++] = comma < 0 ? cmd.substring(start) : cmd.substring(start, comma);
    if (comma < 0) break;
    start = comma + 1;
  }
  const String& screen = parts[0];
  if (screen != "IDLE" && screen != "WELCOME" && screen != "TOTAL" && screen != "PAID" &&
      screen != "DECLINED" && screen != "OCCUPIED" && screen != "FIND") return;  // unknown: keep what is shown
  if (screen == "FIND") {
    if (dispScreen != "FIND") findBackKey = dispKey;
    findAt = millis();
  }
  if (screen == "TOTAL") totalKey = cmd;
  else if (screen == "IDLE") totalKey = "";
  dispKey = cmd;
  dispScreen = screen;
  if (!displayOk) return;
  gfx->fillScreen(C_BG);
  if (screen == "IDLE")          drawIdle();
  else if (screen == "WELCOME")  drawWelcome(parts[1]);
  else if (screen == "TOTAL")    drawTotal(parts[1], parts[2]);
  else if (screen == "PAID")     drawPaid(parts[1], parts[2]);
  else if (screen == "DECLINED") drawDeclined();
  else if (screen == "OCCUPIED") drawOccupied(parts[1]);
  else if (screen == "FIND")     drawFind(parts[1]);
}

// FIND_HOLD_MS after a FIND: back to the cart total (or whatever FIND covered). The backend sends the same
// TOTAL at the same moment; handleDisp ignores whichever arrives second.
void pollFind() {
  if (dispScreen != "FIND" || millis() - findAt < FIND_HOLD_MS) return;
  String back = totalKey.length() ? totalKey : findBackKey;
  handleDisp(back.length() ? back : String("DISP,IDLE"));
}

void renderDisplay() {
  if (!displayOk || dispScreen != "IDLE") return;
  if (millis() - lastAnimAt < 40) return;
  lastAnimAt = millis();
  animateIdle();
}

void handleCommand(const String& cmd) {
  if (cmd == "PING") { Serial.println("PONG"); return; }
  if (cmd.startsWith("DISP,")) { handleDisp(cmd); return; }
  if (cmd.startsWith("HILITE,")) {
    int comma = cmd.indexOf(',', 7);
    if (comma < 0) return;
    String target = cmd.substring(7, comma);
    String state = cmd.substring(comma + 1);
    if (state != "ON" && state != "OFF") return;
    if (target == "ALL") {
      if (state == "OFF") for (int i = 0; i < NUM_BAYS; i++) bayHilite[i] = false;
      return;
    }
    int bay = target.toInt();
    if (bay >= 0 && bay < NUM_BAYS && (bay > 0 || target == "0")) bayHilite[bay] = state == "ON";
    return;
  }
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
    else if (line.length() < 96) line += c;
  }
}

void pollButton() {
  bool down = digitalRead(BTN_PIN) == LOW;
  if (down && btnDownAt == 0) { btnDownAt = millis(); btnSent = false; }
  if (!down) btnDownAt = 0;
  if (down && !btnSent && millis() - btnDownAt > 1000) { Serial.println("BTN,0"); btnSent = true; }
}

void render() {
  bool blinkOn = (millis() / HILITE_HALF_PERIOD_MS) % 2 == 0;
  for (int i = 0; i < NUM_BAYS; i++) digitalWrite(BAY_PINS[i], (bayHilite[i] ? blinkOn : bayOn[i]) ? HIGH : LOW);

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
  renderDisplay();
}

void setup() {
  pinMode(PIN_POWER_ON, OUTPUT);
  digitalWrite(PIN_POWER_ON, HIGH);

  Serial.begin(115200);
  pinMode(BTN_PIN, INPUT_PULLUP);
  for (int i = 0; i < NUM_BAYS; i++) { pinMode(BAY_PINS[i], OUTPUT); bayOn[i] = true; }
  for (int i = 0; i < 3; i++) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttach(RGB_PINS[i], PWM_FREQ, PWM_BITS);
#else
    ledcSetup(RGB_CH[i], PWM_FREQ, PWM_BITS);
    ledcAttachPin(RGB_PINS[i], RGB_CH[i]);
#endif
  }

  // A dead LCD must never stop the LEDs and serial protocol: drawing is skipped when begin() fails.
  displayOk = gfx->begin();
  if (displayOk) {
    gfx->fillScreen(C_BG);
    pinMode(PIN_LCD_BL, OUTPUT);
    digitalWrite(PIN_LCD_BL, HIGH);
  }
  render();
  Serial.println("READY");
  handleDisp("DISP,IDLE");
}

void loop() {
  pollSerial();
  pollButton();
  pollFind();
  render();
  delay(10);
}
