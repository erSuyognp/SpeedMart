# SpeedMart demo script (about 2 minutes)

Visa's framework is Discover, Decide, Transact, Continue. The agent line and the planner cover Discover, the live
cart and the permissions card cover Decide, Face ID on a scoped token covers Transact, and the camera-verified
return covers Continue.

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
- [ ] **No return open:** the admin Cart panel does not say "returning" (Reset clears it).
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
7. **Real cart (10 s):** take the electrolytes and a protein bar. Show the budget bar. Tap **Your agent's
   permissions** on the cart page: what the agent can do, what only the judge can do, and what never happens.
   The $ limit on it is the judge's own budget.
8. **Exit (15 s):** scan QR 3. The permissions card is open at the top of the approval. Open "What the payment
   network sees": a scoped, single-use agent token and the intent sentence. Approve with Face ID. The gate screen
   shows APPROVED. The receipt shows the auth code, points, "In and out in N seconds" and "1 tap to pay".
9. **Return (15 s):** "Changed your mind? Put it back." On the receipt tap **Return an item**, Face ID, then put
   the protein bar back on its bay. The phone lists it as it lands; tap **Confirm refund**. The gate screen shows
   REFUNDED and the agent says the refund is on its way to the Visa ending 4242. The receipt now has a "Refunded"
   line with the refund id.
10. **Close (10 s):** "The shelf agent assembled the cart. The shopper authorized it, and the shelf proved the
    return. Agentic commerce you can hold."

If vision misbehaves, a teammate uses the admin override. If a judge asks, say plainly that it's the manual
fallback.

If the return misbehaves, tap **Cancel return** (nothing is refunded) and move on; admin **Reset** also ends it.
The admin page's **Results today** card shows sessions, average time in store, exit scan to approval, and
refunds, if a judge asks for numbers.

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
| What if the cart is wrong? | Tap **Not mine?** on the item (at the exit: tap the item). The store rechecks the shelf camera first: if it sees the item back on the shelf, it says "Our mistake, removed from your cart." If not, the phone shows two photos of that bay, "When you walked in" and "Now", with the missing item outlined, and you choose **Found it, keep it** or **Remove anyway**. Remove anyway is trusted (twice per visit, then "Please ask a staff member"), logged, and our team reviews the photos. After paying, **Report a problem** on the receipt refunds it through the same refund path as returns. The photos are crops of the shelf bay only, never the full camera view, and are deleted after the visit. |
| How do you know the item was really returned? | The camera confirms it on the shelf. A refund only counts units whose shelf count went up after the return started, never more than you bought, and anything else is shown as "not from this purchase". The refund is exactly those items plus their tax, on the original payment. |
| What can the AI do on its own? | Point at the permissions card. The agent can see the shelf, suggest items and build your cart. Only you can approve a payment with Face ID, go over your budget, or request a refund. Never: your face data leaving your phone, a reusable payment token, or a charge without your approval. |
| Can the voice agent make things up? | The voice agent can only act through the store's own tools, so it can't invent products or prices. It reads the catalog, asks the store's planner (which checks stock and budget) and reads the live cart. The ElevenLabs key stays on our server; the phone only gets a 15-minute signed link. |
