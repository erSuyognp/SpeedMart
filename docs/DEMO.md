# SpeedMart demo script (90 to 120 s)

## Pre-judge reset checklist

Run through this before **every** judge.

- [ ] **Admin reset:** `admin.html` → **Reset** (or hold the controller button 1 s). The store shows as not occupied.
- [ ] **Shelf full:** every item is back in its home bay. The bay order is whatever `config.json` `bays`
      says (today: electrolytes, recovery drinks, protein bars, sparkling water, vegan trail mix, bays 0 to 4),
      and `catalog.json` `units` gives each tag its home bay. No misplaced warnings on the admin page.
- [ ] **Overlay:** the `SpeedMart shelf cam` window is visible to the judge, all bays are green, and every tag ID is shown.
- [ ] **Vision age:** the admin badge is green (under 500 ms).
- [ ] **LEDs:** serial badge connected, every bay LED on, gate LED idle.
- [ ] **Tunnel:** `https://<domain>/api/health` loads on a phone using cellular data.
- [ ] **Agent:** admin health shows LLM on (or templates only, which is fine; say so if asked).
- [ ] **Stripe:** admin health shows `test` (or mock). **Force-decline is OFF.**
- [ ] **Backup phone:** team phone logged in as Demo Shopper, charged, screen on, in case the judge's phone
      fails.
- [ ] **QR codes:** 1 · JOIN, 2 · ENTER, 3 · EXIT printed or on the tablet, in that order on the table.
- [ ] **Lighting:** no glare on the tags from overhead lights. Tilt the matte spares in if needed.
- [ ] **Spokesperson** knows the close line and the Q&A table below.

## Script

1. **Hook (10 s):** "Stores already watch shelves with cameras. SpeedMart lets that camera build your cart, but
   only you can say yes to the charge."
2. **Join (20 s):** the judge scans QR 1, types a name and uses Face ID. "That's a passkey. Your face never
   leaves your phone; your phone vouches for you."
3. **Enter (10 s):** scan QR 2, Face ID, the gate LED turns green. "You're in. One shopper at a time, and no
   one else is tracked."
4. **Pick (15 s):** the judge takes the electrolytes. Point at the overlay, then at the phone: the row appears,
   the bay LED goes dark, and the agent line suggests the recovery drink.
5. **Put back (10 s):** put it back and the row disappears. "It reads the shelf, not a script."
6. **Real cart (10 s):** take the electrolytes and a protein bar. Show the budget bar.
7. **Exit (15 s):** scan QR 3. Open "What the payment network sees": a scoped, single-use agent token and the
   intent sentence. Approve with Face ID. The shelf flashes green. The receipt shows the auth code and points.
8. **Close (10 s):** "The shelf agent assembled the cart. The shopper authorized it. Agentic commerce you can
   hold."

If vision misbehaves, a teammate uses the admin override. If a judge asks, say plainly that it's the manual
fallback.

If gates are still off on demo day, steps 3 and 7 use the **Start shopping** and **Checkout** buttons on the store
page instead of QR 2 and QR 3. Everything else is the same.

## Judge Q&A

| Question | Answer |
|---|---|
| Do you store faces? | No. Face ID happens on the phone through a passkey (WebAuthn). We store only a public key. |
| Is this real Visa? | Payments run through Stripe test mode with a test Visa card. The authorization object is modeled on Visa Intelligent Commerce concepts: an agent token scoped to merchant, amount and time, a user intent, and cardholder confirmation. It's labeled sandbox. |
| Why not Amazon Go? | Tracking which person took which item across a whole store is a research-scale problem. We scoped to shelf-level state and a one-shopper store so the trust and payment story is solid. |
| What if someone pockets an item? | It left the shelf, so it's in their cart and gets charged at exit. |
| Two shoppers? | Store lock: one shopper inside at a time. Multiple shoppers would need per-person association, which is future work. |
| What does the AI actually do? | A deterministic policy decides (budget, complements, misplaced items) and the LLM phrases it for the shopper. The LLM's line is checked (one line, 25 words max, no emoji, only our product names and prices). If the LLM is down or says something off, templates keep it working. |
| How accurate is detection? | Tags give identity, and motion freeze prevents hand flicker. If it's on, YOLO covers tags that are hidden. Show the 10/10 test. |
