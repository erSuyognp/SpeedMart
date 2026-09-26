# SpeedMart demo script (about 2 minutes)

## Pre-judge reset checklist

Run through this before **every** judge.

- [ ] **Admin reset:** `admin.html` → **Reset** (or hold the controller button 1 s). The store shows as not occupied.
- [ ] **Shelf full:** every item is back in its home bay. The bay order is whatever `config.json` `bays`
      says (today: electrolytes, recovery drinks, protein bars, sparkling water, vegan trail mix, left to right),
      and `catalog.json` `units` gives each tag its home bay. No misplaced warnings on the admin page.
- [ ] **Bay cards:** the printed number cards 1 to 5 are taped to the shelf front, in order, each under its
      product (card 1 is bay id 0). Reprint with `python scripts\gen_bay_cards.py` if one is missing.
- [ ] **Overlay:** the `SpeedMart shelf cam` window is visible to the judge, all bays are green, and every tag ID is shown.
- [ ] **Vision age:** the admin badge is green (under 500 ms).
- [ ] **Gate screen:** serial badge connected, the T-Display at the gate shows the SpeedMart idle screen.
- [ ] **Kiosk:** the tablet shows the QR codes and the shelf map with all five bays and their counts, nothing
      glowing.
- [ ] **Tunnel:** `https://<domain>/api/health` loads on a phone using cellular data.
- [ ] **Agent:** admin health shows LLM on (or templates only, which is fine; say so if asked).
- [ ] **Voice:** on the demo phone, `intent.html` shows **Talk to SpeedMart**. One test tap: it greets by name. Then
      tap **Start over** so the bays stop glowing on the kiosk map. (If it's down, the typed box is the fallback.)
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
3. **Enter (10 s):** scan QR 2, Face ID, the gate screen says "Welcome, <name>". "You're in. One shopper at a
   time, and no one else is tracked."
4. **Voice (20 s):** on the phone, tap **Tell us what you need**, then **Talk to SpeedMart**. The agent greets
   the judge by first name. The judge says "I just finished a run, something under fifteen dollars." The plan
   cards and a shelf map appear on the phone with the matching bays glowing, the kiosk map glows the same bays,
   the gate screen says "Find bay 1 and 2", and the agent says what it picked, why, and "Look for bay 1 and
   bay 2, they're glowing on your screen." Point at the number cards on the shelf front. "It can only
   act through the store's own tools: the catalog, the planner and the cart." (If voice fails, type the same
   sentence in the box underneath. It's the same planner.)
5. **Pick (15 s):** the judge takes the electrolytes from bay 1. Point at the overlay, then at the phone: the
   row appears, the gate screen total goes up, and the agent line suggests the recovery drink.
6. **Put back (10 s):** put it back and the row disappears. "It reads the shelf, not a script."
7. **Real cart (10 s):** take the electrolytes and a protein bar. Show the budget bar.
8. **Exit (15 s):** scan QR 3. Open "What the payment network sees": a scoped, single-use agent token and the
   intent sentence. Approve with Face ID. The gate screen shows APPROVED. The receipt shows the auth code and
   points.
9. **Close (10 s):** "The shelf agent assembled the cart. The shopper authorized it. Agentic commerce you can
   hold."

If vision misbehaves, a teammate uses the admin override. If a judge asks, say plainly that it's the manual
fallback.

If gates are still off on demo day, steps 3 and 8 use the **Start shopping** and **Checkout** buttons on the store
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
| Can the voice agent make things up? | The voice agent can only act through the store's own tools, so it can't invent products or prices. It reads the catalog, asks the store's planner (which checks stock and budget) and reads the live cart. The ElevenLabs key stays on our server; the phone only gets a 15-minute signed link. |
