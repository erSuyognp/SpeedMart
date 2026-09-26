// Gate screen palette: the dark theme from web/css/app.css (the @media (prefers-color-scheme: dark) block),
// converted to RGB565. Keep in sync with app.css; the logo gradient colours come from web/assets/logo.svg.
#pragma once
#include <stdint.h>

#define C565(r, g, b) ((uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3)))
#define HEX565(hex) C565(((hex) >> 16) & 0xFF, ((hex) >> 8) & 0xFF, (hex) & 0xFF)

// Backgrounds and surfaces
const uint16_t C_BG        = HEX565(0x0b0d12);  // --bg        (also the colour logo.h is pre-blended against)
const uint16_t C_BG2       = HEX565(0x10131a);  // --bg-2
const uint16_t C_SURFACE   = HEX565(0x161a22);  // --surface
const uint16_t C_SURFACE2  = HEX565(0x1e232d);  // --surface-2
const uint16_t C_LINE      = HEX565(0x262c37);  // --line
const uint16_t C_LINE2     = HEX565(0x364050);  // --line-strong

// Text
const uint16_t C_TEXT      = HEX565(0xf2f4f8);  // --text
const uint16_t C_TEXT2     = HEX565(0xa2abb8);  // --text-2
const uint16_t C_TEXT3     = HEX565(0x6f7886);  // --text-3
const uint16_t C_WHITE     = HEX565(0xffffff);

// Brand
const uint16_t C_BRAND     = HEX565(0x3d7bf0);  // --brand
const uint16_t C_BRAND2    = HEX565(0x2f69dc);  // --brand-strong
const uint16_t C_BRAND_INK = HEX565(0x7fa6f8);  // --brand-ink  (accent for text on dark)
const uint16_t C_BRAND_SOFT= HEX565(0x172448);  // --brand-soft
const uint16_t C_LOGO_TOP  = HEX565(0x2e6ef5);  // logo.svg gradient top
const uint16_t C_LOGO_BOT  = HEX565(0x1a56db);  // logo.svg gradient bottom

// Status
const uint16_t C_GREEN     = HEX565(0x22c55e);  // --green
const uint16_t C_OK        = HEX565(0x15803d);  // --ok
const uint16_t C_AMBER     = HEX565(0xf59e0b);  // --amber
const uint16_t C_WARN      = HEX565(0xb45309);  // --warn
const uint16_t C_RED       = HEX565(0xef4444);  // --red
const uint16_t C_BAD       = HEX565(0xc62828);  // --bad
const uint16_t C_VIOLET    = HEX565(0x7c5cff);  // --violet
