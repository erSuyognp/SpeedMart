# SpeedMart demo script (about 2 minutes)

Visa's framework is Discover, Decide, Transact, Continue. The agent line and the planner cover Discover, the live
cart and the permissions card cover Decide, Face ID on a scoped token covers Transact, and the camera-verified
return covers Continue.

## Pre-judge reset checklist

Run through this before **every** judge.

- [ ] **Admin reset:** `admin.html` → **Reset** (or hold the controller button 1 s). The store shows as not occupied.
- [ ] **Shelf full:** every item is back in its home bay. The bay order is whatever `config.json` `bays`
      says (today: hydration drink, energy drink, chips, water, vegan snack, left to right as bays 1 to 5),
      and `catalog.json` `units` gives each tag its home bay. No misplaced warnings on the admin page.
- [ ] **Bay cards:** the printed number cards 1 to 5 are taped to the shelf front, in order, each under its
      product (card 1 is bay id 0). Reprint with `python scripts\gen_bay_cards.py` if one is missing.
- [ ] **Overlay:** the `SpeedMart shelf cam` window is visible to the judge, all bays are green, and every tag ID is shown.
- [ ] **Vision age:** the admin badge is green (under 500 ms).
- [ ] **Gate screen:** serial badge connected, the T-Display at the gate shows the SpeedMart idle screen.
- [ ] **Kiosk:** the tablet runs the guide (`https://<domain>/kiosk.html?k=<KIOSK_TOKEN>`, **Start kiosk** tapped,
      microphone allowed): step 1 pulsing, the JOIN and ENTER codes, the shelf map with all five bays and their
      counts, nothing glowing, no name on screen. Volume up; the tour has played once since the last restart (so
      its audio is cached).
- [ ] **Tunnel:** `https://<domain>/api/health` loads on a phone using cellular data.
- [ ] **Agent:** admin health shows LLM on (or templates only, which is fine; say so if asked).
- [ ] **Voice:** on the demo phone, `intent.html` shows **Talk to SpeedMart**. One test tap: it greets by name. Then
      tap **Start over** so the bays stop glowing on the kiosk map. (If it's down, the typed box is the fallback.)
- [ ] **Stripe:** admin health shows `test` (or mock). **Force-decline is OFF.**
- [ ] **Review AI:** the admin health badge says `Vision · <model>` (or `Text only` / `Off`, which still works:
      a person decides). The review queue is empty and **Sound: on**. Keep `admin.html` open on the laptop with
      the volume up so the judge hears the new-dispute chime.
- [ ] **No return open:** the admin Cart panel does not say "returning" (Reset clears it).
- [ ] **Backup phone:** team phone logged in as Demo Shopper, charged, screen on, in case the judge's phone
      fails.
- [ ] **QR codes:** 1 · JOIN, 2 · ENTER, 3 · EXIT printed or on the tablet, in that order on the table.
- [ ] **Lighting:** no glare on the tags from overhead lights. Tilt the matte spares in if needed.
- [ ] **Sticky note:** a small blank note (no printing on it) ready for the Water's tag in step 7.
- [ ] **Spokesperson** knows the close line and the Q&A table below.

## First timer walkthrough (the kiosk guides them, nobody on the team helps)

Use this when a judge wants to try SpeedMart alone, or as the opening of the script below. Stand back; the kiosk
does the talking. (Details and the ElevenLabs calls: `docs/voice.md`, "Kiosk agent".)

1. **Walk up.** The judge steps to the shelf while the store is empty. The shelf camera sees the motion and the
   kiosk says "Hi! New here? Tap the screen, and I'll show you how SpeedMart works." The tour button pulses.
2. **Tour (30 s).** The judge taps **New here? Tap to learn how SpeedMart works**. The kiosk explains that the
   shelf camera builds your cart and you approve with your phone, then walks through **1 Join, 2 Enter, 3 Grab
   items, 4 Scan exit**, lighting up each step on the tracker as it speaks. Every line is also a big caption.
3. **Join and enter.** Following step 1 they scan JOIN, type a first name, set a budget ($10) and save a passkey;
   following step 2 they scan ENTER and use Face ID. The tracker ticks 1 and 2 and pulses 3. The codes make way for
   "Hi, Maya", **Budget left $10.00**, **Cart total $0.00**, **Items 0**, and the EXIT code.
4. **Welcome.** "Welcome to your first visit, Maya. Just grab what you want; the camera adds it to your cart. When
   you're done, scan the exit code." Then the agent: "Hi Maya, you have $10 to spend. What are you looking for
   today?"
5. **Ask.** "I just finished a run and I'm thirsty." The agent plans with the store's own tools (Hydration drink and
   Water), says "Look for bay 1 and bay 4", and those bays glow on the kiosk's shelf map. Their phone's voice
   button now reads **Talk to the kiosk** (one conversation at a time).
6. **Grab.** They take the Hydration drink: the panel updates at once and the agent mentions it in its next turn
   without cutting them off. If the conversation has ended (2 minutes of silence), the kiosk says it itself: "See,
   the Hydration drink is already in your cart. You're at $3.78."
7. **Over budget (optional).** Add the Energy drink and the Vegan snack: "That's $1.34 over your budget. Putting
   back the Vegan snack fixes it." Put it back: "Vegan snack is back on the shelf, removed from your cart. You're
   back under your budget."
8. **Linger.** A minute with items and no change: "When you're ready, scan the exit code to review and pay."
9. **Exit.** They scan the EXIT code on the kiosk (step 4 pulses, the conversation ends) and approve on the phone.
   All four steps get a check and the name and totals vanish from the screen at once.

Say it if a judge asks: the screen only ever shows the first name, budget left, cart total and item count; never a
card, a balance or a receipt. The voice agent can only use the store's catalog, cart and planner, and every price
the kiosk says comes from the cart or the budget policy.

If the kiosk is silent: captions still show every line (speech is optional). If the agent can't connect, the kiosk
greets and narrates on its own, and the phone's **Talk to SpeedMart** still works.

## Script

1. **Hook (10 s):** "Stores already watch shelves with cameras. SpeedMart lets that camera build your cart, but
   only you can say yes to the charge."
2. **Join (20 s):** the judge scans QR 1, types a name, slides the budget down to **$10** and uses Face ID. "That's a passkey. Your face never
   leaves your phone; your phone vouches for you."
3. **Enter (10 s):** scan QR 2, Face ID, the gate screen says "Welcome, <name>". "You're in. One shopper at a
   time, and no one else is tracked."
4. **Voice (20 s):** on the phone, tap **Tell us what you need**, then **Talk to SpeedMart**. The agent greets
   the judge by first name. The judge says "I just finished a run and I'm thirsty." The plan cards (Hydration
   drink and Water, $5.40) and a shelf map appear on the phone with bays 1 and 4 glowing, the kiosk map glows the
   same bays, the gate screen says "Find bay 1 and 4", and the agent says what it picked, why, and "Look for bay
   1 and bay 4, they're glowing on your screen." Point at the number cards on the shelf front. "It can only
   act through the store's own tools: the catalog, the planner and the cart." (If voice fails, type the same
   sentence in the box underneath. It's the same planner.)
5. **Pick (15 s):** the judge takes the chips from bay 3. Point at the overlay, then at the phone: the row
   appears, the gate screen total goes up, and the agent line suggests the energy drink and says it has
   caffeine ("Chips added. Energy drink has caffeine, pairs well at $3 and keeps you under budget.").
6. **Put back (10 s):** put it back and the row disappears. "It reads the shelf, not a script."
7. **Over budget (15 s):** take the hydration drink, the energy drink and the vegan snack: $11.34 with tax on a
   $10 budget. The budget bar turns red and the agent says "You're $1.34 over budget. Putting back the Vegan
   snack fixes it." Put the vegan snack back: $7.02, under budget again. Meanwhile a teammate sticks the blank
   note over the tag of one Water in bay 4 and leaves the bottle where it is: the camera loses the tag and the
   Water appears in the cart ($8.64). Leave it; it is the camera mistake the judge disputes in step 10. Tap
   **Your agent's permissions** on the cart page: what the agent can do, what only the judge can do, and what never happens.
   The $ limit on it is the judge's own budget.
8. **Exit (15 s):** scan QR 3. The permissions card is open at the top of the approval. Open "What the payment
   network sees": a scoped, single-use agent token and the intent sentence. Approve with Face ID. The gate screen
   shows APPROVED. The receipt shows the auth code, points, "In and out in N seconds" and "1 tap to pay".
9. **Return (15 s):** "Changed your mind? Put it back." On the receipt tap **Return an item**, Face ID, then put
   the hydration drink back on bay 1. The phone lists it as it lands; tap **Confirm refund** ($3.78). The gate screen shows
   REFUNDED and the agent says the refund is on its way to the Visa ending 4242. The receipt now has a "Refunded"
   line with the refund id.
10. **Dispute review (30 s):** two disputes on the receipt, one the AI settles and one a person settles.
    - **Instant AI refund (the Water):** tap **Report a problem** and pick the Water. The shelf camera still sees
      it gone (its tag is covered), so the AI reviews the clip and keyframes of bay 4: the bottle never moved. It
      clearly supports the judge and $1.62 is under the $2.00 auto refund line, so within about 20 s the phone
      says "Refunded $1.62 after a review of the shelf photos" and the admin card says "Auto approved by
      policy". Peel the note off the tag.
    - **Admin review queue (the Energy drink):** tap **Report a problem** again and pick the Energy drink. The
      phone says "The shelf camera still sees it gone" and shows the two bay photos; leave it open ("Not sure?
      Leave it: our team reviews every open case"). The laptop chimes and the **Review queue** badge turns red:
      the card shows the judge's first name, the item, the AI verdict with its confidence bar ("Supports the
      charge, 8 % for the shopper"; at $3.24 it is over the $2.00 line anyway, so it waits for a person), the
      observations linked to keyframes, and the 6 second clip of the shelf around the pick. Play the clip. Type
      a note ("Clip shows the pick at 10:14") and tap **Keep the charge**. The judge's receipt updates to
      **Charge confirmed** with the note.
    "The AI reads the clip and explains what it saw. It can only speed up small refunds; a person makes every
    other call, and the shopper sees the reason."
11. **Close (10 s):** "The shelf agent assembled the cart. The shopper authorized it, and the shelf proved the
    return. Agentic commerce you can hold."

If vision misbehaves, a teammate uses the admin override. If a judge asks, say plainly that it's the manual
fallback.

If the return misbehaves, tap **Cancel return** (nothing is refunded) and move on; admin **Reset** also ends it.

If the review model is slow or down, the card says "AI review unavailable" after about 20 s (one retry); decide
from the clip and the photos yourself. Nothing waits on the model: the queue, the clip and the buttons work
without it. A dispute under $2.00 (`config.json` `disputes.auto_refund_max_usd`; only the Water, $1.62, is under it)
that the AI clearly supports (80 % or more for the shopper) is refunded on its own and the card says "Auto
approved by policy"; every other dispute waits for a person.
The admin page's **Results today** card shows sessions, average time in store, exit scan to approval, and
refunds, if a judge asks for numbers.

If gates are still off on demo day, steps 3 and 8 use the **Start shopping** and **Checkout** buttons on the store
page instead of QR 2 and QR 3. Everything else is the same.

## Judge Q&A

| Question | Answer |
|---|---|
| Do you store faces? | No. Face ID happens on the phone through a passkey (WebAuthn). We store only a public key. |
| Is this my real card? | No. SpeedMart never asks for a card; every member gets a test Visa card in Stripe test mode, and no real money moves. |
| Is this real Visa? | Payments run through Stripe test mode with a test Visa card. The authorization object is modeled on Visa Intelligent Commerce concepts: an agent token scoped to merchant, amount and time, a user intent, and cardholder confirmation. It's labeled sandbox. |
| Why not Amazon Go? | Tracking which person took which item across a whole store is a research-scale problem. We scoped to shelf-level state and a one-shopper store so the trust and payment story is solid. |
| What if someone pockets an item? | It left the shelf, so it's in their cart and gets charged at exit. |
| Two shoppers? | Store lock: one shopper inside at a time. Multiple shoppers would need per-person association, which is future work. |
| What does the AI actually do? | A deterministic policy decides (budget, complements, misplaced items) and the LLM phrases it for the shopper. The LLM's line is checked (one line, 25 words max, no emoji, only our product names and prices). If the LLM is down or says something off, templates keep it working. |
| How accurate is detection? | Tags give identity, and motion freeze prevents hand flicker. If it's on, YOLO covers tags that are hidden. Show the 10/10 test. |
| What if the cart is wrong? | Tap **Not mine?** on the item (at the exit: tap the item). The store rechecks the shelf camera first: if it sees the item back on the shelf, it says "Our mistake, removed from your cart." If not, the phone shows two photos of that bay, "When you walked in" and "Now", with the missing item outlined, and you choose **Found it, keep it** or **Remove anyway**. Remove anyway is trusted (twice per visit, then "Please ask a staff member"), logged, and our team reviews the photos. After paying, **Report a problem** on the receipt refunds it through the same refund path as returns. The photos are crops of the shelf bay only, never the full camera view, and are deleted after the visit. |
| How do you know the item was really returned? | The camera confirms it on the shelf. A refund only counts units whose shelf count went up after the return started, never more than you bought, and anything else is shown as "not from this purchase". The refund is exactly those items plus their tax, on the original payment. |
| Can the AI deny a refund? | No. It reviews the shelf clips and can only speed up small refunds it clearly supports (under $2, set in `config.json`, and 80 % or more). Everything else, and every "keep the charge", is a person's decision on the admin page, with a note the shopper sees on the receipt. The rule is code, not the model. |
| What does the review AI actually see? | Crops of the shelf bays only, never faces: the photo from when you walked in, the latest one, and six keyframes from a short clip around the moment the bay changed, plus a timeline of tag ids, motion and cart changes. Its answer is strict JSON that we validate; loaded words are replaced before anyone reads it. Clips are deleted after the visit unless there's a dispute. |
| What can the AI do on its own? | Point at the permissions card. The agent can see the shelf, suggest items and build your cart. Only you can approve a payment with Face ID, go over your budget, or request a refund. Never: your face data leaving your phone, a reusable payment token, or a charge without your approval. |
| Can the voice agent make things up? | The voice agent can only act through the store's own tools, so it can't invent products or prices. It reads the catalog, asks the store's planner (which checks stock and budget) and reads the live cart. The ElevenLabs key stays on our server; the phone only gets a 15-minute signed link. |
