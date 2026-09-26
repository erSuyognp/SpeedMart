# SpeedMart: Devpost draft

**Name:** SpeedMart
**Tagline:** The shelf builds your cart. You approve the charge.
**Tracks:** The Shipyard (Hardware) · Visa challenge: Reimagine Shopping with Generative AI

## Inspiration

Agentic commerce is showing up in chat apps, and stores already have cameras. Nobody had connected "I picked
this up" to "I approve this charge" in a way shoppers can trust. Silent walk-out checkout is a liability.
Confirmation is the product.

## What it does

Shoppers join SpeedMart once with a passkey (Face ID). At the entry gate they verify and the store opens. An
overhead camera watches a three-bay shelf. Items that leave the shelf appear in the phone cart in about a second,
and items put back disappear. A store agent explains the cart in one sentence and suggests complements that fit
the shopper's budget. At the exit gate the shopper reviews the total and approves it with Face ID against a
scoped, single-use agent token. The shelf lights green.

## How we built it

- **Vision:** OpenCV ArUco detection. Each physical unit has its own tag. Units are assigned to bays by ROI,
  and motion freeze stops a hand over a bay from registering as a pick. The cart is state-based (cart = baseline
  minus what's on the shelf), so duplicate frames can't double count and put-backs just work. YOLO11 fusion is
  optional.
- **Backend:** FastAPI + SQLite + WebSockets. The backend is the single source of truth. The phone only
  renders snapshots.
- **Identity and payment:** WebAuthn passkeys for entry and approval, and Stripe test mode with a test Visa
  card. The authorization object is modeled on Visa Intelligent Commerce: an agent token scoped to merchant,
  amount and time, a user intent sentence, and cardholder confirmation.
- **Store agent:** a deterministic policy (budget, complements, misplaced items) decides what to say, and an
  LLM (Claude Haiku) phrases it. Its output is checked (one line, 25 words max, no emoji, only catalog products
  and known prices) and falls back to templates if anything is off.
- **Hardware:** an ESP32 shelf controller over USB serial drives the bay and gate/status LEDs. A long button
  press resets the demo.

**How the team worked:** we designed the architecture, the hardware, and the shelf and store rules ourselves,
and wrote them down as a step-by-step build spec with acceptance checks for every step. We then used Claude
Code to speed up implementation against that written spec, one step at a time. Every step was verified by
automated tests plus the physical checks in the spec (real picks on the real shelf) before we moved on.

## Challenges we ran into

Hands covering tags, venue lighting and camera exposure, keeping the passkey domain stable, and keeping our
scope honest: everything payment-related is clearly labeled sandbox.

## Accomplishments that we're proud of

Reliable pick and put-back, sub-second phone updates, biometric entry and approval, and real sandbox
transactions.

## What we learned

Computing state beats counting events. Human confirmation is what makes grab-and-go trustworthy.

## What's next for SpeedMart

Load-cell fusion, multi-shopper association, and a real issuer sandbox with the Visa Intelligent Commerce APIs.

## Built with

python · fastapi · sqlite · websockets · opencv · aruco · webauthn · stripe · anthropic-claude · esp32 · platformio · javascript

## Links

- Demo video: _TBD_
- Repo: https://github.com/erSuyognp/SpeedMart
