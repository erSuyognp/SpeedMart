// Gate screen (F12) for the LilyGO T-Display-S3: USB powered, nothing wired to it. Section 12.3 adapted.
// A dumb display and button hat, styled like a retail payment terminal: no cart logic lives here.
//
// Protocol (9.7), newline-terminated ASCII, unchanged:
//   in:  DISP,IDLE | DISP,WELCOME,<name> | DISP,TOTAL,<total>,<count> | DISP,PAID,<total>,<auth>
//        DISP,DECLINED | DISP,OCCUPIED,<name> | DISP,FIND,<bays>        the onboard LCD screens
//        DISP,DEMO                 cycles every screen with sample data every 4 s until the next DISP command
//        PING                      -> PONG
//   legacy, accepted and ignored (nothing is wired to the board):
//        LED,<bay>,ON|OFF   SHELF,GREEN|RED|IDLE   GATE,OPEN|CLOSED|IDLE   HILITE,<bay>,ON|OFF|ALL,OFF
//   out: READY (boot)   PONG   BTN,0 (GPIO 14 button held 1 s)
//
// Timing. The backend (backend/serial_bridge.py DisplayDirector) owns the timed screens and sends the next
// one itself: WELCOME -> TOTAL after 3 s, OCCUPIED -> back after 3 s, DECLINED -> TOTAL after 4 s,
// FIND -> TOTAL after 6 s, IDLE 3 s after the session closes. The firmware keeps its own fallbacks for the
// overlays (FIND 6 s, OCCUPIED 3 s, DECLINED 4 s) and returns PAID to IDLE after 5 s, so a lost line or a
// filming session without the backend never strands a screen. The same DISP line twice is a no-op, so the
// backend's command arriving after a fallback changes nothing.
//
// Rendering. Every frame is drawn into a full-screen Arduino_Canvas (PSRAM) and flushed at once over the
// 8-bit parallel bus: no flicker, no tearing. The loop is non-blocking: serial, button and timers run every
// iteration; a frame is rendered when 33 ms have passed (~30 fps). Screen changes cross-fade and slide over
// TRANSITION_MS; values tween (see Tween). Fonts are u8g2 bitmaps (U8g2 library, only the referenced fonts are linked), colours come from colors.h
// (web/css/app.css dark theme), the logo from logo.h (tools/png_to_rgb565.py).
//
// Hardware (verified against Xinyuan-LilyGO/T-Display-S3, examples/factory/pin_config.h): ST7789 170x320
// on the ESP32-S3 i80 8-bit bus, D0..D7 = 39 40 41 42 45 46 47 48, RES 5, CS 6, DC 7, WR 8, RD 9,
// backlight 38, peripheral power enable 15 (must be HIGH before the panel answers), BUTTON_2 on 14,
// 16 MB flash, 8 MB OPI PSRAM.

#include <Arduino.h>
#include <Arduino_GFX_Library.h>
#include <esp_heap_caps.h>
#include <math.h>

#include "colors.h"
// u8g2 fonts: Arduino_GFX renders them natively when the U8g2 library is present (lib_deps); the font
// arrays (u8g2_font_fub30_tr, u8g2_font_logisoso54_tr, ...) come from <U8g2lib.h>, which Arduino_GFX_Library.h includes.
#include "logo.h"

// ---------------------------------------------------------------------------------------------- pins
#define PIN_POWER_ON 15
#define PIN_LCD_BL   38
#define PIN_LCD_RES  5
#define PIN_LCD_CS   6
#define PIN_LCD_DC   7
#define PIN_LCD_WR   8
#define PIN_LCD_RD   9
#define BTN_PIN      14  // onboard BUTTON_2 (GPIO 0 is BOOT)

// ---------------------------------------------------------------------------------------------- tuning
#define SCR_W 320
#define SCR_H 170
#define FRAME_MS          33     // ~30 fps
#define TRANSITION_MS     250    // every screen change
#define WELCOME_HOLD_MS   3000   // backend WELCOME_HOLD_S: the progress line runs this long
#define FIND_HOLD_MS      6000   // backend FIND_HOLD_S
#define OCCUPIED_HOLD_MS  3000   // backend OCCUPIED_HOLD_S
#define DECLINED_HOLD_MS  4000   // backend DECLINED_HOLD_S
#define PAID_HOLD_MS      5000   // firmware only: PAID -> IDLE
#define DEMO_STEP_MS      4000
#define DIM_AFTER_MS      60000  // idle this long -> backlight to BL_DIM
#define BL_FULL 255
#define BL_DIM  77               // ~30 %
#define PWM_FREQ 5000
#define PWM_BITS 8
#define BL_CH    0               // LEDC channel (core 2.x API)

// ---------------------------------------------------------------------------------------------- display
Arduino_DataBus *bus = new Arduino_ESP32PAR8Q(PIN_LCD_DC, PIN_LCD_CS, PIN_LCD_WR, PIN_LCD_RD,
                                              39, 40, 41, 42, 45, 46, 47, 48);
// Rotation 1 = landscape (320 wide x 170 tall). The 170-px panel sits at column offset 35 in the ST7789's RAM.
Arduino_GFX *panel = new Arduino_ST7789(bus, PIN_LCD_RES, 1 /* rotation */, true /* IPS */,
                                        170 /* width */, 320 /* height */, 35, 0, 35, 0);

// Arduino_Canvas allocates its framebuffer with aligned_alloc (internal RAM). This puts it in PSRAM and
// falls back to the base class (internal RAM, 108 800 bytes) if PSRAM is missing.
class PsramCanvas : public Arduino_Canvas {
 public:
  PsramCanvas(int16_t w, int16_t h, Arduino_G *out) : Arduino_Canvas(w, h, out) {}
  bool inPsram = false;
  bool begin(int32_t speed = GFX_NOT_DEFINED) override {
    if (!_framebuffer) {
      _framebuffer = (uint16_t *)heap_caps_aligned_alloc(16, (size_t)_width * _height * 2, MALLOC_CAP_SPIRAM);
      inPsram = _framebuffer != nullptr;
    }
    return Arduino_Canvas::begin(speed);
  }
};
PsramCanvas *gfx = new PsramCanvas(SCR_W, SCR_H, panel);
bool displayOk = false;

// ---------------------------------------------------------------------------------------------- easing
static inline float clamp01(float t) { return t < 0 ? 0 : (t > 1 ? 1 : t); }
float easeOutCubic(float t) { t = clamp01(t); float u = 1 - t; return 1 - u * u * u; }
float easeInOut(float t)    { t = clamp01(t); return t < 0.5f ? 4 * t * t * t : 1 - powf(-2 * t + 2, 3) / 2; }
float easeOutBack(float t)  { t = clamp01(t); const float c1 = 1.70158f, c3 = c1 + 1; float u = t - 1; return 1 + c3 * u * u * u + c1 * u * u; }
// 0..1 phase of a repeating cycle, and a 0..1 sine wave over it (millis() % period keeps float precision).
static inline float phase(uint32_t now, uint32_t periodMs) { return (float)(now % periodMs) / periodMs; }
static inline float wave(uint32_t now, uint32_t periodMs) { return 0.5f + 0.5f * sinf(TWO_PI * phase(now, periodMs)); }
// 0 before startAt, 1 after startAt + dur, linear in between.
float ramp(uint32_t now, uint32_t startAt, uint32_t dur) {
  if ((int32_t)(now - startAt) <= 0) return 0;
  if (now - startAt >= dur) return 1;
  return (float)(now - startAt) / dur;
}

enum Ease { EASE_LINEAR, EASE_OUT_CUBIC, EASE_IN_OUT, EASE_OUT_BACK };
struct Tween {
  float from = 0, to = 0;
  uint32_t startAt = 0, dur = 0;
  Ease ease = EASE_OUT_CUBIC;
  void set(float v) { from = to = v; dur = 0; }
  void go(float target, uint32_t now, uint32_t d, Ease e = EASE_OUT_CUBIC) {
    from = value(now); to = target; startAt = now; dur = d; ease = e;
  }
  bool active(uint32_t now) const { return dur && now - startAt < dur; }
  float value(uint32_t now) const {
    if (!dur || now - startAt >= dur) return to;
    float t = (float)(now - startAt) / dur;
    switch (ease) {
      case EASE_OUT_CUBIC: t = easeOutCubic(t); break;
      case EASE_IN_OUT:    t = easeInOut(t); break;
      case EASE_OUT_BACK:  t = easeOutBack(t); break;
      default: break;
    }
    return from + (to - from) * t;
  }
};

// ---------------------------------------------------------------------------------------------- colours
uint16_t mix565(uint16_t a, uint16_t b, float t) {
  t = clamp01(t);
  int ar = (a >> 11) & 31, ag = (a >> 5) & 63, ab = a & 31;
  int br = (b >> 11) & 31, bg = (b >> 5) & 63, bb = b & 31;
  int r = ar + lroundf((br - ar) * t), g = ag + lroundf((bg - ag) * t), bl = ab + lroundf((bb - ab) * t);
  return (uint16_t)((r << 11) | (g << 5) | bl);
}

// Draw state for transitions: every helper offsets by (gDx, gDy) and fades colours toward gBase by gAlpha.
float gAlpha = 1;
int gDx = 0, gDy = 0;
uint16_t gBase = C_BG;
static inline uint16_t col(uint16_t c) { return gAlpha >= 1 ? c : mix565(gBase, c, gAlpha); }

// ---------------------------------------------------------------------------------------------- shapes
void fillR(int x, int y, int w, int h, uint16_t c)            { if (w > 0 && h > 0) gfx->fillRect(x + gDx, y + gDy, w, h, col(c)); }
void fillRR(int x, int y, int w, int h, int r, uint16_t c)    { if (w > 0 && h > 0) gfx->fillRoundRect(x + gDx, y + gDy, w, h, r, col(c)); }
void fillC(int cx, int cy, int r, uint16_t c)                 { if (r > 0) gfx->fillCircle(cx + gDx, cy + gDy, r, col(c)); }
void arc(int cx, int cy, int r1, int r2, float a0, float a1, uint16_t c) { gfx->drawArc(cx + gDx, cy + gDy, r1, r2, a0, a1, col(c)); }

void vGradient(int x, int y, int w, int h, uint16_t top, uint16_t bottom) {
  for (int i = 0; i < h; i++) gfx->drawFastHLine(x, y + i, w, mix565(top, bottom, h > 1 ? (float)i / (h - 1) : 0));
}
void hGradient(int x, int y, int w, int h, uint16_t left, uint16_t right) {
  for (int i = 0; i < w; i++) gfx->drawFastVLine(x + i, y, h, col(mix565(left, right, w > 1 ? (float)i / (w - 1) : 0)));
}
// Rounded rectangle filled with a vertical gradient (rows clipped to the corner radius).
void fillRRGradient(int x, int y, int w, int h, int r, uint16_t top, uint16_t bottom) {
  if (w <= 0 || h <= 0) return;
  r = min(r, min(w, h) / 2);
  for (int j = 0; j < h; j++) {
    float dy = j < r ? (r - j - 0.5f) : (j >= h - r ? (j - (h - r) + 0.5f) : 0);
    int inset = dy > 0 ? (int)ceilf(r - sqrtf(max(0.0f, (float)r * r - dy * dy))) : 0;
    int ww = w - 2 * inset;
    if (ww > 0) gfx->drawFastHLine(x + inset + gDx, y + j + gDy, ww, col(mix565(top, bottom, h > 1 ? (float)j / (h - 1) : 0)));
  }
}
// Soft glow: concentric, larger, dimmer copies of the shape. Draw before the shape itself.
void glowRoundRect(int x, int y, int w, int h, int r, uint16_t color, float strength, int layers = 3, int spread = 4) {
  for (int i = layers; i >= 1; i--) {
    float k = strength * (1.0f - (float)i / (layers + 1));
    int e = i * spread;
    fillRR(x - e, y - e, w + 2 * e, h + 2 * e, r + e, mix565(gBase, color, k * k * 2));
  }
}
void glowCircle(int cx, int cy, int r, uint16_t color, float strength, int layers = 3, int spread = 4) {
  for (int i = layers; i >= 1; i--) {
    float k = strength * (1.0f - (float)i / (layers + 1));
    fillC(cx, cy, r + i * spread, mix565(gBase, color, k * k * 2));
  }
}
// Thick line with round caps.
void stroke(float x0, float y0, float x1, float y1, int r, uint16_t c) {
  float dx = x1 - x0, dy = y1 - y0, len = sqrtf(dx * dx + dy * dy);
  int steps = max(1, (int)(len / 1.5f));
  for (int i = 0; i <= steps; i++) {
    float t = (float)i / steps;
    fillC(lroundf(x0 + dx * t), lroundf(y0 + dy * t), r, c);
  }
}
// Draws the first frac (0..1) of the total length of n segments {x0,y0,x1,y1}, in order: "stroke by stroke".
void strokeSegments(const float *seg, int n, float frac, int r, uint16_t c) {
  float total = 0;
  for (int i = 0; i < n; i++) total += hypotf(seg[i * 4 + 2] - seg[i * 4], seg[i * 4 + 3] - seg[i * 4 + 1]);
  float remain = clamp01(frac) * total;
  for (int i = 0; i < n && remain > 0; i++) {
    const float *s = seg + i * 4;
    float L = hypotf(s[2] - s[0], s[3] - s[1]);
    float f = L > 0 ? min(1.0f, remain / L) : 1;
    stroke(s[0], s[1], s[0] + (s[2] - s[0]) * f, s[1] + (s[3] - s[1]) * f, r, c);
    remain -= L;
  }
}
// The logo, alpha already pre-blended against C_BG; LOGO_KEY pixels are transparent. Fades with gAlpha.
void drawLogo(int x, int y) {
  x += gDx; y += gDy;
  if (gAlpha >= 1) { gfx->draw16bitRGBBitmapWithTranColor(x, y, (uint16_t *)LOGO, LOGO_KEY, LOGO_W, LOGO_H); return; }
  for (int j = 0; j < LOGO_H; j++)
    for (int i = 0; i < LOGO_W; i++) {
      uint16_t p = LOGO[j * LOGO_W + i];
      if (p != LOGO_KEY) gfx->drawPixel(x + i, y + j, col(p));
    }
}

// ---------------------------------------------------------------------------------------------- text
// u8g2 fonts through Arduino_GFX: the cursor y is the baseline. Font byte 13 is the ascent of 'A'.
int capHeight(const uint8_t *font) { return (int8_t)font[13]; }
int textWidth(const uint8_t *font, const String &s) {
  int16_t x1, y1; uint16_t w, h;
  gfx->setFont(font);
  gfx->getTextBounds(s.c_str(), 0, 0, &x1, &y1, &w, &h);
  return (int)w;
}
void drawText(const uint8_t *font, const String &s, int x, int baseline, uint16_t color) {
  gfx->setFont(font);
  gfx->setTextColor(col(color));
  gfx->setCursor(x + gDx, baseline + gDy);
  gfx->print(s);
}
int drawTextC(const uint8_t *font, const String &s, int cx, int baseline, uint16_t color) {
  int16_t x1, y1; uint16_t w, h;
  gfx->setFont(font);
  gfx->getTextBounds(s.c_str(), 0, 0, &x1, &y1, &w, &h);
  drawText(font, s, cx - (int)w / 2 - x1, baseline, color);
  return (int)w;
}
// Largest font in the chain that fits s into maxW. If even the last one does not, s is shortened with "...".
const uint8_t *fitText(const uint8_t *const *chain, int n, String &s, int maxW) {
  for (int i = 0; i < n; i++) if (textWidth(chain[i], s) <= maxW) return chain[i];
  const uint8_t *f = chain[n - 1];
  String base = s;
  while (base.length() > 1) {
    base.remove(base.length() - 1);
    base.trim();
    if (textWidth(f, base + "...") <= maxW) { s = base + "..."; return f; }
  }
  s = "...";
  return f;
}

const uint8_t *const NAME_FONTS[]  = {u8g2_font_fub35_tr, u8g2_font_fub30_tr, u8g2_font_fub25_tr, u8g2_font_fub20_tr, u8g2_font_fub17_tr};
const uint8_t *const HEAD_FONTS[]  = {u8g2_font_fub25_tr, u8g2_font_fub20_tr, u8g2_font_fub17_tr};
const uint8_t *const MONEY_FONTS[] = {u8g2_font_logisoso54_tr, u8g2_font_logisoso46_tr, u8g2_font_logisoso38_tr, u8g2_font_logisoso30_tr};
const uint8_t *const MONEY_SMALL[] = {u8g2_font_logisoso46_tr, u8g2_font_logisoso38_tr, u8g2_font_logisoso30_tr};
const uint8_t *const BODY_FONTS[]  = {u8g2_font_helvR14_tr, u8g2_font_helvR12_tr};
#define COUNT_OF(a) ((int)(sizeof(a) / sizeof((a)[0])))

// "$12.96" -> prefix "$", 12.96, 2 decimals. False when there is no number in it.
bool parseMoney(const String &s, String &prefix, float &value, int &decimals) {
  int i = 0, len = s.length();
  while (i < len && !isDigit(s[i])) i++;
  if (i == len) return false;
  prefix = s.substring(0, i);
  String num = s.substring(i);
  value = num.toFloat();
  int dot = num.indexOf('.');
  decimals = dot < 0 ? 0 : (int)num.length() - dot - 1;
  if (decimals > 4) decimals = 4;
  return true;
}
String fmtMoney(const String &prefix, float value, int decimals) {
  char buf[32];
  snprintf(buf, sizeof buf, "%s%.*f", prefix.c_str(), decimals, value);
  return String(buf);
}

// ---------------------------------------------------------------------------------------------- state
enum ScreenType { S_NONE, S_IDLE, S_WELCOME, S_TOTAL, S_FIND, S_PAID, S_DECLINED, S_OCCUPIED };
struct Screen {
  ScreenType type = S_NONE;
  String a, b;        // DISP arguments
  uint32_t since = 0; // millis() when it was shown
};
Screen cur, prev;
bool transitioning = false;
uint32_t transAt = 0;

String dispKey;          // the last DISP line applied; the same line again is a no-op
String totalKey;         // the last DISP,TOTAL since IDLE: where overlays return to
String findBackKey;      // the line FIND covered
String occupiedBackKey;  // the line OCCUPIED covered

// TOTAL: the number counts from the value on screen to the new one.
bool moneyNumeric = false;
String moneyPrefix;
int moneyDecimals = 2;
Tween moneyTween;
uint32_t moneyChangedAt = 0;

// DEMO
const char *const DEMO_STEPS[] = {
    "DISP,IDLE", "DISP,WELCOME,Maya", "DISP,TOTAL,$12.96,3", "DISP,FIND,2 and 4", "DISP,TOTAL,$18.45,4",
    "DISP,PAID,$18.45,A1B2C3", "DISP,DECLINED", "DISP,OCCUPIED,Maya",
};
bool demo = false;
int demoStep = 0;
uint32_t demoAt = 0;

// backlight
Tween blTween;
int blLevel = -1;
int blTarget = -1;
uint32_t lastActivityAt = 0;

// serial + button
String line;
unsigned long btnDownAt = 0;
bool btnSent = false;
uint32_t lastFrameAt = 0;

// ---------------------------------------------------------------------------------------------- backlight
void setBacklight(uint8_t v) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(PIN_LCD_BL, v);
#else
  ledcWrite(BL_CH, v);
#endif
}
void pollBacklight(uint32_t now) {
  if (!displayOk) return;
  bool wantDim = !demo && cur.type == S_IDLE && now - lastActivityAt > DIM_AFTER_MS;
  int target = wantDim ? BL_DIM : BL_FULL;
  if (target != blTarget) {
    blTarget = target;
    if (wantDim) blTween.go(BL_DIM, now, 900, EASE_IN_OUT);  // smooth down
    else blTween.set(BL_FULL);                                // instant up
  }
  int v = lroundf(blTween.value(now));
  if (v != blLevel) { blLevel = v; setBacklight((uint8_t)v); }
}

// ---------------------------------------------------------------------------------------------- screens
void showScreen(ScreenType type, const String &a, const String &b, uint32_t now) {
  if (type == S_TOTAL && cur.type == S_TOTAL) {  // live update: count to the new number, no slide
    cur.a = a; cur.b = b;
  } else {
    prev = cur;
    cur.type = type; cur.a = a; cur.b = b; cur.since = now;
    transitioning = prev.type != S_NONE;
    transAt = now;
  }
  if (type == S_TOTAL) {
    String prefix; float value; int decimals;
    if (parseMoney(a, prefix, value, decimals)) {
      bool animate = moneyNumeric && prefix == moneyPrefix && fabsf(value - moneyTween.to) > 0.0001f;
      moneyPrefix = prefix; moneyDecimals = decimals;
      if (animate) { moneyTween.go(value, now, 400, EASE_OUT_CUBIC); moneyChangedAt = now; }
      else moneyTween.set(value);
      moneyNumeric = true;
    } else {
      moneyNumeric = false;
    }
  }
}

ScreenType screenTypeOf(const String &name) {
  if (name == "IDLE") return S_IDLE;
  if (name == "WELCOME") return S_WELCOME;
  if (name == "TOTAL") return S_TOTAL;
  if (name == "FIND") return S_FIND;
  if (name == "PAID") return S_PAID;
  if (name == "DECLINED") return S_DECLINED;
  if (name == "OCCUPIED") return S_OCCUPIED;
  return S_NONE;
}
ScreenType screenTypeOfKey(const String &key) {  // "DISP,TOTAL,$1.00,1" -> S_TOTAL
  if (!key.startsWith("DISP,")) return S_NONE;
  int comma = key.indexOf(',', 5);
  return screenTypeOf(comma < 0 ? key.substring(5) : key.substring(5, comma));
}

void handleDisp(const String &cmd, bool fromDemo = false);

// Where an overlay returns to, mirroring DisplayDirector: the live TOTAL when the covered screen was part
// of a shopping session, else the covered screen itself, else IDLE.
String returnTarget(const String &backKey) {
  ScreenType t = screenTypeOfKey(backKey);
  bool inSession = t == S_TOTAL || t == S_WELCOME || t == S_OCCUPIED || t == S_FIND || t == S_DECLINED;
  if (inSession && totalKey.length()) return totalKey;
  if (backKey.length() && t != S_NONE) return backKey;
  return totalKey.length() ? totalKey : String("DISP,IDLE");
}

void applyDemoStep() { handleDisp(DEMO_STEPS[demoStep], true); }

// cmd is the whole line, e.g. "DISP,TOTAL,$12.96,3".
void handleDisp(const String &cmd, bool fromDemo) {
  uint32_t now = millis();
  if (cmd == "DISP,DEMO") {
    demo = true; demoStep = 0; demoAt = now;
    dispKey = "";  // so the first demo step always applies
    applyDemoStep();
    return;
  }
  if (!fromDemo) demo = false;  // any other DISP command ends the demo
  if (cmd == dispKey) return;
  String parts[3];
  int n = 0, start = 5;  // after "DISP,"
  while (n < 3) {
    int comma = cmd.indexOf(',', start);
    parts[n++] = comma < 0 ? cmd.substring(start) : cmd.substring(start, comma);
    if (comma < 0) break;
    start = comma + 1;
  }
  ScreenType type = screenTypeOf(parts[0]);
  if (type == S_NONE) return;  // unknown: keep what is shown
  if (type == S_FIND && cur.type != S_FIND) findBackKey = dispKey;
  if (type == S_OCCUPIED && cur.type != S_OCCUPIED) occupiedBackKey = dispKey;
  if (type == S_TOTAL) totalKey = cmd;
  else if (type == S_IDLE) { totalKey = ""; moneyNumeric = false; }
  dispKey = cmd;
  showScreen(type, parts[1], parts[2], now);
}

// Firmware-side fallbacks (see the header). Off during DEMO, which steps on its own clock.
void pollTimers(uint32_t now) {
  if (demo || cur.type == S_NONE) return;
  uint32_t age = now - cur.since;
  ScreenType before = cur.type;
  if (before == S_FIND && age >= FIND_HOLD_MS)              handleDisp(returnTarget(findBackKey));
  else if (before == S_OCCUPIED && age >= OCCUPIED_HOLD_MS) handleDisp(returnTarget(occupiedBackKey));
  else if (before == S_DECLINED && age >= DECLINED_HOLD_MS) handleDisp(totalKey.length() ? totalKey : String("DISP,IDLE"));
  else if (before == S_PAID && age >= PAID_HOLD_MS)         handleDisp("DISP,IDLE");
  else return;
  if (cur.type == before) handleDisp("DISP,IDLE");  // the target was the screen itself: never spin
}

void pollDemo(uint32_t now) {
  if (!demo || now - demoAt < DEMO_STEP_MS) return;
  demoAt = now;
  demoStep = (demoStep + 1) % COUNT_OF(DEMO_STEPS);
  applyDemoStep();
}

// ---------------------------------------------------------------------------------------------- drawing
void accentBar() { hGradient(0, 0, SCR_W, 3, C_BRAND2, C_BRAND_INK); }

void drawIdle(const Screen &s, uint32_t now) {
  (void)s;
  const int cx = SCR_W / 2, cy = 64;
  float breathe = wave(now, 8000);  // slow
  glowRoundRect(cx - 120, cy - 30, 240, 60, 30, C_BRAND, 0.32f + 0.22f * breathe, 4, 9);
  accentBar();

  const int gap = 14;
  int ww = textWidth(u8g2_font_fub30_tr, "SpeedMart");
  int x0 = (SCR_W - (LOGO_W + gap + ww)) / 2;
  drawLogo(x0, cy - LOGO_H / 2);
  int baseline = cy + capHeight(u8g2_font_fub30_tr) / 2;
  int tx = x0 + LOGO_W + gap;
  drawText(u8g2_font_fub30_tr, "Speed", tx, baseline, C_TEXT);
  drawText(u8g2_font_fub30_tr, "Mart", tx + ww - textWidth(u8g2_font_fub30_tr, "Mart"), baseline, C_BRAND_INK);

  // "Tap or scan to enter" with an NFC wave: three arcs lighting up outward, text shimmering and bobbing.
  const String hint = "Tap or scan to enter";
  int hw = textWidth(u8g2_font_helvR14_tr, hint);
  const int iconW = 24, iconGap = 12;
  int hx = (SCR_W - (iconW + iconGap + hw)) / 2 + iconW + iconGap;
  int hy = 141 + lroundf(1.5f * sinf(TWO_PI * phase(now, 4000)));
  float shimmer = wave(now, 5600);
  drawText(u8g2_font_helvR14_tr, hint, hx, hy, mix565(C_TEXT3, C_TEXT2, shimmer));
  int icx = hx - iconGap - iconW + 2, icy = hy - capHeight(u8g2_font_helvR14_tr) / 2;
  float ph = phase(now, 1600);
  fillC(icx, icy, 2, C_BRAND_INK);
  for (int i = 0; i < 3; i++) {
    float d = ph - i * 0.2f;
    float k = (d >= 0 && d < 0.45f) ? 1 - d / 0.45f : 0;
    int r = 7 + i * 5;
    arc(icx, icy, r + 1, r - 1, 315, 45, mix565(gBase, C_BRAND_INK, 0.25f + 0.75f * k));
  }
}

void drawWelcome(const Screen &s, uint32_t now) {
  accentBar();
  drawTextC(u8g2_font_helvR14_tr, "Welcome,", SCR_W / 2, 52, C_TEXT2);
  String name = s.a;
  const uint8_t *f = fitText(NAME_FONTS, COUNT_OF(NAME_FONTS), name, SCR_W - 40);
  float in = easeOutCubic(ramp(now, s.since, 500));
  int lift = lroundf((1 - in) * 26);
  gDy += lift;
  drawTextC(f, name, SCR_W / 2, 108, C_BRAND_INK);
  gDy -= lift;
  // progress line: the backend hands over to TOTAL when it reaches the right edge
  float p = ramp(now, s.since, WELCOME_HOLD_MS);
  fillR(0, SCR_H - 3, SCR_W, 3, C_SURFACE2);
  fillR(0, SCR_H - 3, lroundf(SCR_W * p), 3, C_BRAND);
}

void drawTotal(const Screen &s, uint32_t now) {
  accentBar();
  drawText(u8g2_font_helvB12_tr, "TOTAL", 24, 40, C_TEXT3);
  // live dot, top right
  float pulse = wave(now, 2000);
  fillC(296, 34, 8, mix565(gBase, C_GREEN, 0.10f + 0.25f * pulse));
  fillC(296, 34, 4, C_GREEN);
  // the number
  String money = moneyNumeric ? fmtMoney(moneyPrefix, moneyTween.value(now), moneyDecimals) : s.a;
  uint32_t age = now - moneyChangedAt;
  float pk = (moneyChangedAt && age < 450) ? sinf(age / 450.0f * PI) : 0;  // 0..1..0 pulse
  const uint8_t *f = fitText(MONEY_FONTS, COUNT_OF(MONEY_FONTS), money, SCR_W - 40);
  if (pk > 0.45f && f == u8g2_font_logisoso54_tr) f = u8g2_font_logisoso58_tr;
  if (pk > 0) glowRoundRect(SCR_W / 2 - 110, 66, 220, 56, 28, C_BRAND, 0.35f * pk, 3, 6);
  drawTextC(f, money, SCR_W / 2, 112, mix565(C_TEXT, C_BRAND_INK, pk));
  // item count
  long n = s.b.toInt();
  String items = String(n) + (n == 1 ? " item" : " items");
  drawTextC(u8g2_font_helvR14_tr, items, SCR_W / 2, 150, C_TEXT2);
}

void drawFind(const Screen &s, uint32_t now) {
  accentBar();
  drawTextC(u8g2_font_fub20_tr, "Find bay", SCR_W / 2, 44, C_TEXT);
  // "2 and 4" -> badges 2, 4 ("1 2 3 4 and 5" -> five badges)
  int bays[8], nb = 0;
  String rest = s.a + " ";
  while (nb < 8) {
    int sp = rest.indexOf(' ');
    if (sp < 0) break;
    String tok = rest.substring(0, sp);
    rest = rest.substring(sp + 1);
    if (!tok.length()) continue;
    bool digits = true;
    for (unsigned i = 0; i < tok.length(); i++) if (!isDigit(tok[i])) digits = false;
    if (digits && tok.length() <= 2) bays[nb++] = tok.toInt();
  }
  if (nb == 0) {  // no numbers: show the text as it came
    String t = s.a;
    const uint8_t *f = fitText(HEAD_FONTS, COUNT_OF(HEAD_FONTS), t, SCR_W - 32);
    drawTextC(f, t, SCR_W / 2, 112, C_BRAND_INK);
    return;
  }
  const int size = nb <= 3 ? 64 : 50, gap = nb <= 3 ? 18 : 12, cy = 104;
  const uint8_t *nf = nb <= 3 ? u8g2_font_logisoso38_tr : u8g2_font_logisoso30_tr;
  int x0 = (SCR_W - (nb * size + (nb - 1) * gap)) / 2;
  float glow = wave(now, 3000);
  for (int i = 0; i < nb; i++) {
    float t = easeOutBack(ramp(now, s.since + 120 + i * 90, 380));  // pop with a slight bounce
    if (t <= 0) continue;
    int sz = lroundf(size * t), cx = x0 + i * (size + gap) + size / 2;
    int x = cx - sz / 2, y = cy - sz / 2, r = max(2, sz / 4);
    glowRoundRect(x, y, sz, sz, r, C_BRAND, 0.42f + 0.18f * glow, 3, 5);
    fillRRGradient(x, y, sz, sz, r, C_LOGO_TOP, C_LOGO_BOT);  // the logo's gradient
    if (t > 0.6f) drawTextC(nf, String(bays[i]), cx, cy + capHeight(nf) / 2, C_WHITE);
  }
  drawTextC(u8g2_font_helvR12_tr, "Look for the number cards", SCR_W / 2, 162, C_TEXT3);
}

void drawPaid(const Screen &s, uint32_t now) {
  const int cx = 72, cy = 88;
  float disc = easeOutBack(ramp(now, s.since + 120, 420));
  int r = lroundf(42 * disc);
  if (r > 0) {
    glowCircle(cx, cy, r, C_GREEN, 0.45f, 3, 5);
    fillC(cx, cy, r, C_GREEN);
  }
  float chk = easeInOut(ramp(now, s.since + 480, 420));
  const float seg[] = {cx - 19.f, cy + 1.f, cx - 6.f, cy + 14.f,   // stroke 1
                       cx - 6.f, cy + 14.f, cx + 21.f, cy - 14.f}; // stroke 2
  if (chk > 0) strokeSegments(seg, 2, chk, 4, C_WHITE);
  const int x = 132, w = SCR_W - x - 14;
  drawText(u8g2_font_fub20_tr, "APPROVED", x, 62, C_WHITE);
  String total = s.a;
  const uint8_t *f = fitText(MONEY_SMALL, COUNT_OF(MONEY_SMALL), total, w);
  drawText(f, total, x, 116, C_WHITE);
  if (s.b.length()) drawText(u8g2_font_helvR12_tr, "Auth " + s.b, x, 148, mix565(C_WHITE, C_GREEN, 0.45f));
}

void drawDeclined(const Screen &s, uint32_t now) {
  uint32_t age = now - s.since;
  int shake = age < 520 ? lroundf(sinf(age / 520.0f * PI * 6) * 9 * (1 - age / 520.0f)) : 0;
  gDx += shake;
  const int cx = 72, cy = 88;
  float disc = easeOutCubic(ramp(now, s.since + 80, 360));
  int r = lroundf(42 * disc);
  if (r > 0) {
    glowCircle(cx, cy, r, C_RED, 0.45f, 3, 5);
    fillC(cx, cy, r, C_RED);
  }
  float x = easeInOut(ramp(now, s.since + 400, 420));
  const float seg[] = {cx - 14.f, cy - 14.f, cx + 14.f, cy + 14.f,
                       cx + 14.f, cy - 14.f, cx - 14.f, cy + 14.f};
  if (x > 0) strokeSegments(seg, 2, x, 4, C_WHITE);
  const int tx = 132, w = SCR_W - tx - 14;
  drawText(u8g2_font_fub20_tr, "DECLINED", tx, 70, mix565(C_RED, C_WHITE, 0.35f));
  String hint = "Try again on your phone";
  const uint8_t *f = fitText(BODY_FONTS, COUNT_OF(BODY_FONTS), hint, w);
  drawText(f, hint, tx, 104, C_TEXT);
  gDx -= shake;
}

void drawOccupied(const Screen &s, uint32_t now) {
  const String tail = " is shopping";
  String name = s.a;
  const uint8_t *f = u8g2_font_fub17_tr;
  for (int i = 0; i < COUNT_OF(HEAD_FONTS); i++)
    if (textWidth(HEAD_FONTS[i], name + tail) <= SCR_W - 32) { f = HEAD_FONTS[i]; break; }
  if (textWidth(f, name + tail) > SCR_W - 32) fitText(&f, 1, name, SCR_W - 32 - textWidth(f, tail));
  String whole = name + tail;
  int tw = textWidth(f, whole);
  int16_t x1, y1; uint16_t bw, bh;
  gfx->getTextBounds(whole.c_str(), 0, 0, &x1, &y1, &bw, &bh);
  int x = SCR_W / 2 - tw / 2 - x1;
  drawText(f, whole, x, 76, C_TEXT);
  drawText(f, name, x, 76, C_AMBER);  // the name over itself, in amber
  drawTextC(u8g2_font_helvR14_tr, "Please wait a moment", SCR_W / 2, 108, C_TEXT2);
  // progress pulse: a highlight gliding back and forth on a thin track
  const int trackX = 70, trackW = 180, trackY = 140, hl = 56;
  float ph = phase(now, 3000) * 2;
  float p = easeInOut(ph < 1 ? ph : 2 - ph);
  fillRR(trackX, trackY, trackW, 4, 2, mix565(gBase, C_WHITE, 0.14f));
  int hx = trackX + lroundf(p * (trackW - hl));
  fillRR(hx - 4, trackY - 2, hl + 8, 8, 4, mix565(gBase, C_AMBER, 0.25f));
  fillRR(hx, trackY, hl, 4, 2, C_AMBER);
}

void drawScreen(const Screen &s, uint32_t now) {
  switch (s.type) {
    case S_IDLE:     drawIdle(s, now); break;
    case S_WELCOME:  drawWelcome(s, now); break;
    case S_TOTAL:    drawTotal(s, now); break;
    case S_FIND:     drawFind(s, now); break;
    case S_PAID:     drawPaid(s, now); break;
    case S_DECLINED: drawDeclined(s, now); break;
    case S_OCCUPIED: drawOccupied(s, now); break;
    default: break;
  }
}

// Background tint per screen (top, bottom). PAID keeps the dark base and sweeps its green in.
void bgTint(ScreenType t, uint16_t &top, uint16_t &bottom) {
  switch (t) {
    case S_DECLINED: top = mix565(C_BG, C_BAD, 0.50f);  bottom = mix565(C_BG2, C_RED, 0.28f); break;
    case S_OCCUPIED: top = mix565(C_BG, C_WARN, 0.48f); bottom = mix565(C_BG2, C_AMBER, 0.20f); break;
    default:         top = C_BG; bottom = C_BG2; break;
  }
}

void renderFrame(uint32_t now) {
  float tt = transitioning ? ramp(now, transAt, TRANSITION_MS) : 1;
  if (tt >= 1) transitioning = false;
  float e = easeInOut(tt);

  uint16_t top, bottom, ptop = C_BG, pbottom = C_BG2;
  bgTint(cur.type, top, bottom);
  if (transitioning) {
    bgTint(prev.type, ptop, pbottom);
    top = mix565(ptop, top, e);
    bottom = mix565(pbottom, bottom, e);
  }
  gAlpha = 1; gDx = gDy = 0;
  vGradient(0, 0, SCR_W, SCR_H, top, bottom);
  gBase = mix565(top, bottom, 0.5f);
  if (cur.type == S_PAID) {  // green sweep, left to right
    float sw = easeOutCubic(ramp(now, cur.since, 420));
    uint16_t gt = mix565(C_BG, C_OK, 0.58f), gb = mix565(C_BG2, C_GREEN, 0.32f);
    int w = lroundf(SCR_W * sw);
    for (int i = 0; i < SCR_H && w > 0; i++) gfx->drawFastHLine(0, i, w, mix565(gt, gb, (float)i / (SCR_H - 1)));
    if (sw > 0.3f) gBase = mix565(gt, gb, 0.5f);  // the sweep has passed the checkmark by then
  }

  if (transitioning) {
    gAlpha = 1 - e; gDy = -lroundf(e * 24); gDx = 0;
    drawScreen(prev, now);
  }
  gAlpha = e; gDy = lroundf((1 - e) * 24); gDx = 0;
  drawScreen(cur, now);
  gAlpha = 1; gDx = gDy = 0;
  gfx->flush();
}

// ---------------------------------------------------------------------------------------------- serial
void handleCommand(const String &cmd) {
  lastActivityAt = millis();
  if (cmd == "PING") { Serial.println("PONG"); return; }
  if (cmd.startsWith("DISP,")) { handleDisp(cmd); return; }
  // Legacy LED commands: accepted, nothing to drive.
  if (cmd.startsWith("LED,") || cmd.startsWith("SHELF,") || cmd.startsWith("GATE,") || cmd.startsWith("HILITE,")) return;
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
  if (down && btnDownAt == 0) { btnDownAt = millis(); btnSent = false; lastActivityAt = millis(); }
  if (!down) btnDownAt = 0;
  if (down && !btnSent && millis() - btnDownAt > 1000) { Serial.println("BTN,0"); btnSent = true; }
}

// ---------------------------------------------------------------------------------------------- main
void setup() {
  pinMode(PIN_POWER_ON, OUTPUT);
  digitalWrite(PIN_POWER_ON, HIGH);

  Serial.begin(115200);
  pinMode(BTN_PIN, INPUT_PULLUP);

  // A dead LCD must never stop the serial protocol: drawing is skipped when begin() fails.
  displayOk = gfx->begin();
  if (displayOk) {
    gfx->setTextWrap(false);
    gfx->fillScreen(C_BG);
    gfx->flush();
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttach(PIN_LCD_BL, PWM_FREQ, PWM_BITS);
#else
    ledcSetup(BL_CH, PWM_FREQ, PWM_BITS);
    ledcAttachPin(PIN_LCD_BL, BL_CH);
#endif
    blTween.set(BL_FULL);
  }
  lastActivityAt = millis();
  Serial.println("READY");
  handleDisp("DISP,IDLE");
}

void loop() {
  pollSerial();
  pollButton();
  uint32_t now = millis();
  pollTimers(now);
  pollDemo(now);
  pollBacklight(now);
  if (displayOk && now - lastFrameAt >= FRAME_MS) {
    lastFrameAt = now;
    renderFrame(now);
  } else {
    delay(1);
  }
}
