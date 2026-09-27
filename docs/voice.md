# F19 · Voice store agent (ElevenLabs Agents)

Shoppers tap **Talk to SpeedMart** on `intent.html` and talk to an ElevenLabs agent. The agent can only act
through four client tools that run in the shopper's browser and call the store's own endpoints, so it cannot
invent products or prices. The typed intent flow (F18) on the same page is still there as the fallback.
The same agent also talks from the entrance kiosk (mode `"kiosk"`, see "Kiosk agent" below); one conversation
at a time per shopper.

- Flag: `config.json` → `features.voice` (default `true`). When it is `false` the button is hidden and
  `/api/voice/*` returns `404 not_available`. The text flow does not depend on voice at all.
- Keys: `ELEVENLABS_API_KEY` and `ELEVENLABS_AGENT_ID` in `.env`. The API key never leaves the backend. The
  browser only gets a signed URL, which is valid for 15 minutes.

## How it fits together

```
phone (intent.html + js/pages/voice.js)             backend                        ElevenLabs
  tap "Talk" ─ getUserMedia (mic prompt)
  GET /api/voice/session ───────────────────────▶  voice.py ─ GET get-signed-url ─▶ (xi-api-key)
            ◀── {signed_url, member_first_name, budget_usd, dietary}
  Conversation.startSession({signedUrl, dynamicVariables, clientTools}) ═══ websocket ═══▶ agent
  agent calls a client tool ──▶ voice.js ──▶ /api/voice/catalog | /api/store/current | POST/DELETE /api/intent
```

| Endpoint (new) | Auth | Returns |
|---|---|---|
| `GET /api/voice/session` | logged-in member | `{"signed_url", "member_first_name", "budget_usd", "remaining_usd", "dietary", "is_first_visit", "mode": "phone"}`. `401 not_logged_in`, `404 unknown_member`, `409 kiosk_conversation_active` while the kiosk is talking with this shopper, `503 voice_unavailable` if the keys are missing or ElevenLabs fails |
| `GET /api/voice/catalog` | logged-in member | `{"products": [{"sku","name","price_usd","tags","pairs_with","bays"}]}` for `get_catalog` |

These are new routes, so no row in Section 8 of the spec was changed. Events logged: `voice_session` and
`voice_session_error` (with `reason`). The signed URL is never logged because anyone holding it can open a session.

## ElevenLabs docs these calls come from

Checked on 2026-09-26.

- Signed URL endpoint: `GET https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id=...`,
  response `{"signed_url": "..."}`:
  https://elevenlabs.io/docs/api-reference/conversations/get-signed-url
- API key header `xi-api-key`: https://elevenlabs.io/docs/api-reference/authentication
- Private agents need authentication enabled, and signed URLs last 15 minutes:
  https://elevenlabs.io/docs/agents-platform/customization/authentication
- Browser SDK `@elevenlabs/client`: `Conversation.startSession({ signedUrl })`, the callbacks, `endSession()`
  and the mic permission advice: https://elevenlabs.io/docs/agents-platform/libraries/java-script
- Client tools. Names and parameters are case-sensitive and must match the code. "Wait for response" sends
  the return value back to the agent:
  https://elevenlabs.io/docs/agents-platform/customization/tools/client-tools
- Dynamic variables: `{{name}}` syntax, `dynamicVariables` option, string, number or boolean values:
  https://elevenlabs.io/docs/agents-platform/customization/personalization/dynamic-variables
- LLM choices for agents: https://elevenlabs.io/docs/agents-platform/customization/llm
- Max conversation duration (Advanced → Call limits):
  https://elevenlabs.io/docs/agents-platform/customization/conversation-flow

SDK version pinned: `@elevenlabs/client@1.25.0`, loaded as ESM from
`https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.25.0/+esm`. The options and callback shapes in
`voice.js` were checked against that version's `dist/types.d.ts` and `dist/utils/BaseConnection.d.ts`:

- `signedUrl` with `connectionType: "websocket"`
- `dynamicVariables: Record<string, string | number | boolean>`
- `clientTools: Record<string, (params) => string | number | void | Promise<...>>`
- `onMessage({message, role: "user" | "agent"})`
- `onModeChange({mode: "speaking" | "listening"})`
- `onStatusChange({status})`
- `onError(message)`
- `onDisconnect({reason: "error" | "agent" | "user"})`

## Dashboard setup (paste this)

Create an agent in the ElevenLabs Agents dashboard, then set the following.

### Agent → First message

```
Hi {{first_name}}, you have ${{remaining_usd}} to spend. What are you looking for today?
```

The phone and the kiosk both send `remaining_usd` (the budget minus the cart total, the whole budget before the
shopper has picked anything), so this reads "Hi Maya, you have $10 to spend. What are you looking for today?".

### Agent → System prompt

```
You are the SpeedMart store agent in a tiny smart store: one shelf with five numbered bays, watched by a camera.
The products are drinks and snacks (for example a hydration drink, an energy drink, chips, water and a vegan
snack), but always take names, prices and bays from get_catalog.
The shopper is {{first_name}}. Their budget is ${{budget_usd}} and ${{remaining_usd}} of it is left. Their dietary
need is: {{dietary}}. First visit to SpeedMart: {{is_first_visit}}. You are talking through: {{mode}}.

Style: friendly and brief. At most 2 short sentences per turn. Plain spoken words, no lists, no emoji.

Rules:
- Only recommend products returned by get_catalog. Call get_catalog before naming any product.
- Never state a price that did not come from a tool result (get_catalog, get_cart or make_plan).
- When the shopper states a goal or a need, call make_plan with their words as goal_text. Then say what you
  picked and why in one sentence, then tell them where to look using where_to_look from the result, for
  example "Look for bay 1 and bay 4, they're glowing on your screen." The bays have printed number cards on
  the shelf front; always say bay numbers exactly as the tools give them. If make_plan returns no items, say
  nothing on the shelf matches yet and suggest something from get_catalog.
- Respect the budget and the dietary need. Never suggest going over budget.
- Whenever you recommend a product whose tags include caffeine (the Energy drink), say that it has caffeine.
- If the shopper asks what is in their cart or how much they have spent, call get_cart.
- If the shopper wants to start over or cancel the plan, call clear_plan.
- If asked about payment: they just walk out through the exit gate and approve the total there with Face ID.
  Nothing is charged until they approve.
- If asked about cards or payment, explain it's a demo: a test Visa card is linked automatically and no real money moves.
- Do not ask for or collect personal information (no email, phone, address or card details).
- If a tool returns an error, say sorry briefly and suggest typing the request on the screen.

Kiosk mode (when {{mode}} is kiosk):
- You are the kiosk by the shelf. Speak to the shopper standing at the shelf in front of you, not to a phone.
- Keep every answer under two short sentences.
- Always say bay numbers ("bay 2 and bay 4") when you point at products; the shelf map on the kiosk screen glows
  them. Never say "your screen".
- If {{is_first_visit}} is true, once in the conversation say they just take items off the shelf, the camera adds
  them to their cart, and they scan the exit code when done.
- You will receive store updates about the shopper's cart as context (picked, put back, over budget, wrong bay,
  time to check out). Mention them briefly and naturally in your next turn; never read them out word for word and
  never interrupt the shopper to say them. They contain the only prices you may quote besides tool results.
- To check out they scan the exit code and approve on their phone. You cannot take payment.
```

### Agent → Dynamic variables (placeholders for testing in the dashboard)

| Name | Test value |
|---|---|
| `first_name` | `Maya` |
| `budget_usd` | `20` |
| `remaining_usd` | `20` |
| `dietary` | `none` |
| `is_first_visit` | `true` |
| `mode` | `phone` |

The phone and the kiosk always send all six, so no placeholder is ever used in a real conversation. `dietary` is
`"none"` when the member did not pick one; `mode` is `"phone"` or `"kiosk"`.

### Agent → Tools → Add tool → Client tool (create four)

Tick **Wait for response** on every tool. Names and parameter identifiers are case-sensitive.

1. **`get_catalog`**
   - Description: `Returns every product on the SpeedMart shelf as JSON: sku, name, price_usd, tags (for example drink, snack, hydration, caffeine, vegan), pairs_with and bays (the numbers printed on the shelf cards, starting at 1). Call this before recommending or pricing anything. If a product's tags include caffeine, say so when you recommend it.`
   - Parameters: none

2. **`get_cart`**
   - Description: `Returns the shopper's live cart as JSON: in_store, items (name, qty, line_total_usd), total_usd with tax, budget_usd, over_budget and payment (a note that the card is a demo test Visa card and no real money moves). If in_store is false the shopper has not entered the store yet.`
   - Parameters: none

3. **`make_plan`**
   - Description: `Builds a shopping plan from the shopper's goal using only real products, stock and their budget. It shows the plan and a shelf map with the chosen bays glowing (on their phone, or on the kiosk screen in kiosk mode). Returns JSON: goal_summary, items (name, qty, unit_price_usd, reason; the reason says when an item has caffeine), est_total_usd with tax, budget_usd, fits_budget, bays_to_find (bay card numbers) and where_to_look (a sentence to say, e.g. "Look for bay 1 and bay 4, they're glowing on your screen.").`
   - Parameter: type **String**, identifier **`goal_text`**, **Required**, description
     `The shopper's goal in their own words, for example "thirsty after a run, under 10 dollars" or "something for a study session".`

4. **`clear_plan`**
   - Description: `Clears the current shopping plan and stops the bays glowing on the shelf map. Use when the shopper wants to start over or cancel.`
   - Parameters: none

If a tool has a response timeout field, set it to about 10 s. `make_plan` can take up to ~3 s when the store's
own LLM planner is on.

### Recommended settings

- **LLM:** a fast, small model such as Gemini Flash (e.g. `Gemini 2.5 Flash`) or `Claude Haiku 4.5`. Test it
  in the dashboard and keep whichever answers faster with tool calls.
- **Latency:** choose the lowest-latency / turbo or flash TTS model the voice tab offers, and keep streaming
  latency optimization at a high setting.
- **Voice:** a warm, friendly voice from the library (a conversational, natural-sounding one, not a narrator).
- **Advanced → Call limits → Max conversation duration:** `300` seconds (the kiosk's cap is 5 minutes). The phone
  page still ends its own session after 2 minutes, and the kiosk ends its own after 5 minutes or 2 minutes of
  silence.
- **Security:** turn on **Enable authentication**. This makes the agent private, so it only works through a
  signed URL from our backend.
- Copy the agent id (`agent_...`) into `.env` as `ELEVENLABS_AGENT_ID`, and create an API key (Developers →
  API keys) for `ELEVENLABS_API_KEY`. If the key has scoped permissions, it needs access to the Agents platform.

## Testing on a phone over the tunnel (Windows)

The microphone needs a secure context, so use the https tunnel domain. Voice does not work over
`http://<laptop-ip>:8000`.

1. Fill in `.env`, then restart the stack so settings reload:
   ```powershell
   .\scripts\run_all.ps1
   ```
2. Check the key from the laptop without exposing it. Sign in on the laptop browser first, then open
   `https://<domain>/api/voice/session`. You should see JSON with `signed_url` starting with `wss://`, not a 503.
3. On the phone (cellular or venue WiFi), open `https://<domain>/`, sign in (or join), then open
   `https://<domain>/intent.html` (or tap **Tell us what you need** on the store page).
4. Tap **Talk to SpeedMart** and allow the microphone. The indicator goes Connecting → Listening, and the agent
   greets you by first name.
5. Say "I just finished a run and I'm thirsty, under ten dollars." Expected: the plan cards (Hydration drink and
   Water) and the shelf map appear with bays 1 and 4 glowing, the gate screen shows "Find bay 1 and 4" for 6 s
   (if it is connected), and the agent names the picks and says "Look for bay 1 and bay 4, they're glowing on
   your screen."
   Then say "Actually I need something for a study session." Expected: Energy drink and Chips, bays 2 and 3,
   and the agent says the energy drink has caffeine.
6. Ask "What's in my cart?" (after entering the store), then "Start over". The plan clears.
7. Fallback checks:
   - Deny the mic: you get a friendly message and the page scrolls to the text box.
   - Set `"voice": false` in `config.json` and restart: the button disappears and typing still works.
   - Remove the key from `.env` and restart: tapping Talk shows "Voice isn't available right now…" and the
     page scrolls to the text box.

---

## Safari on iPad and iPhone

Safari starts audio inside a tap only. The SDK (`@elevenlabs/client`) handles that itself: once loaded it listens
for any tap, unlocks its speaker then and keeps it for 30 s, and the next `startSession` inside that window uses
it. Both pages therefore fetch the SDK when they load, not at the first tap, and:

- **Phone (intent.html):** the session starts right after the microphone prompt, inside the Talk tap. Answer the
  prompt promptly: after 30 s the tap no longer counts and the agent stays silent (tap End, then Talk again).
- **Kiosk:** Safari will not start the conversation from the socket event that says a shopper walked in, so on an
  iPad or iPhone the guide panel shows **Tap to talk to me** while someone shops (step 3). The tap cuts the
  scripted welcome short and starts the conversation; the button comes back when a conversation ends while the
  visit goes on. Edge and Chrome on the Dell Venue still start the conversation on their own.
- Silent Mode (the bell in Control Center) mutes the agent's voice but not the kiosk's own spoken lines. Open the
  page in Safari itself, not as a Home Screen app, and always over the https tunnel.

# Kiosk agent (the entrance tablet)

The tablet at the door is opened as `https://<domain>/kiosk.html?k=<KIOSK_TOKEN>` and guides shoppers, first
timers especially, so they can use SpeedMart with no human help. Contract: spec 8.16. Code:
`backend/kiosk.py` (routes), `backend/kiosk_agent.py` (what it says and when), `backend/tts.py` (speech),
`web/js/kiosk_agent.js` (the screen).

## Security and setup

- `.env`: `KIOSK_TOKEN` (a long random string) and `ELEVENLABS_VOICE_ID` (the voice it speaks with; it uses the
  existing `ELEVENLABS_API_KEY`). `python scripts\check_env.py` checks both (the voice id with
  `GET /v1/voices/{voice_id}`, which costs no characters: https://elevenlabs.io/docs/api-reference/voices/get).
- Every kiosk-only endpoint checks `?k=` in constant time. Without a valid token (or with `KIOSK_TOKEN` empty) the
  tablet shows the public kiosk page exactly as before: QR codes, store status, shelf map, no voice and no shopper
  data. A socket opened with `role=kiosk` and a wrong token is an ordinary public socket.
- **Start kiosk:** a full screen button, once per page load. The tap unlocks audio playback (browsers block sound
  until the page has been tapped) and asks for the microphone (only when `features.voice` is on). After that the
  kiosk speaks on its own.
- **Public screen privacy:** the shopper panel shows the first name, budget left, cart total and item count, and
  nothing else: never the demo balance, the card or a receipt. It is cleared the moment the visit ends (paid, empty
  exit, cancelled). The captions of spoken lines name products and prices the shopper can already see on the shelf.
- The microphone needs a secure context: open the kiosk over the **https tunnel**, not `http://<laptop-ip>:8000`.

## Phase 1: guided screen and spoken tour

- **Step tracker** 1 Join, 2 Enter, 3 Grab items, 4 Scan exit. The current step pulses; done steps show a check.
  Driven by `kiosk_visit` messages on the kiosk socket (spec 8.4): entered / sync → step 3, exit_pending → step 4,
  paid → all four checked for 6 s, ended → back to step 1. While the store is free, step 1 is current.
- **Captions:** every spoken line is shown as a large caption. When nothing is being said, the panel shows a hint
  for the current step ("Scan a code with your phone camera to start.", "Grab what you want…", "Check your cart on
  your phone and approve.").
- **Attract mode:** "New here? Tap to learn how SpeedMart works" starts the tour: what SpeedMart is (the shelf camera
  builds your cart and you approve with your phone), then each step, highlighting that step on screen. About 75
  words, ~30 s. The wording follows the flags (Face ID or Confirm; exit code or the Checkout button; the demo
  shopper when signup is off).
- **Auto offer:** when the vision worker reports motion in any bay while the store is empty, the backend sends the
  public `shelf_activity` message (no data, at most one per 30 s). The kiosk then says "Hi! New here? Tap the
  screen, and I'll show you how SpeedMart works." and pulses the tour button, at most once every 2 minutes, and
  only when it is idle. Motion is only reported with `features.motion_freeze` on.
- During a visit the JOIN and ENTER codes make way for the shopper panel and (gates on) the EXIT code.

### Kiosk voice: ElevenLabs text to speech

Checked on 2026-09-26.

- Endpoint: `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128` with the
  `xi-api-key` header and body `{"text", "model_id"}`; the response body is the audio file:
  https://elevenlabs.io/docs/api-reference/text-to-speech/convert
- Model: `eleven_flash_v2_5`, the lowest latency model ("~75ms"), recommended for real-time use; the older
  `eleven_turbo_v2_5` is deprecated in its favour: https://elevenlabs.io/docs/models
- `backend/tts.py` never sends the key to the browser. The kiosk fetches audio from
  `GET /api/kiosk/tts/{clip_id}?k=`, and a clip id only exists for text the backend registered, so the browser can
  never make the kiosk say something else.
- **Fixed phrases** (no digits: the tour, the offer, the exit reminder, first visit greetings) are generated once
  and cached as files in `data/tts_cache/<hash>.mp3` (hash of voice id, model, format and text; `data/` is
  gitignored). The tour and fixed phrases are warmed in the background when the kiosk starts. A cache hit never
  calls ElevenLabs, even with the key removed.
- **Dynamic lines** (with numbers: prices, totals, budgets) are generated on demand with a **4 s timeout** and kept
  in memory only (the last 24).
- **Fallback:** speech off (`features.voice` false, no key or no voice id), a timeout or any error → the route
  answers 503 and the kiosk shows the line as a caption only, for reading time. The kiosk also gives up on a clip
  that is not ready within 5 s. Events: `tts_generated`, `tts_error` (`reason`).
- To use a new voice, change `ELEVENLABS_VOICE_ID` and restart: the cache key includes the voice id, so the old
  files are simply not used (delete `data\tts_cache` to reclaim the space).

## Phase 2: personalized shopper mode with narration

- **On entry** the kiosk gets `kiosk_visit` "entered", calls `GET /api/kiosk/shopper?k=` and shows the shopper
  panel: first name, budget left (bar: green, amber at 20 % left or less, red when over), cart total and item
  count. `kiosk_cart` keeps it live on every cart change. Nothing else about the shopper reaches the screen.
- **Greeting** (chosen by the backend from `visit_count`; returns and cancelled visits do not count):
  - first visit: "Welcome to your first visit, Maya. Just grab what you want; the camera adds it to your cart.
    When you're done, scan the exit code." (no numbers, so it is cached as a file after the first time)
  - returning: "Welcome back, Maya. You have $10 to spend." (generated on demand)
- **Narration** (`kiosk_agent.Narrator`, spec 8.16): the backend watches `cart_changed` and, once the cart has been
  still for 1.5 s, sends one `kiosk_say` line for the net change. A pick and a put back inside the window say
  nothing. Lines, first match wins:
  - misplaced: "Oops, the Water is in the wrong bay. Please put it back in bay 4." (the policy's `misplaced`)
  - over budget: "That's $1.34 over your budget. Putting back the Vegan snack fixes it." (the policy's
    `over_budget`, said on a pick, when crossing the budget, or when the amount over changes)
  - pick: "See, the Chips are already in your cart. You're at $2.70." (the cart's own total with tax)
  - put back: "Water is back on the shelf, removed from your cart." (+ "You're back under your budget." when it
    fixes an over budget cart)
  - exit reminder, after 60 s with items and no change, once: "When you're ready, scan the exit code to review
    and pay."
  Every amount comes from the cart snapshot or the agent policy (spec 7.4); the kiosk never builds a price.
- **Never talking over itself:** lines queue on the kiosk and a line that is playing is never cut off. A newer cart
  line replaces an older one that has not started yet. When the visit ends, queued lines about it are dropped, a
  line about it that is still playing stops, and the caption goes back to the idle hint.
- Narration stops at the exit scan (the cart is frozen) and starts again on "Keep shopping".

## Phase 3: kiosk conversation

When a shopper walks in, the kiosk starts a conversation with the same ElevenLabs agent, in mode `"kiosk"`.

```
kiosk (kiosk.html?k= + js/kiosk_agent.js)                  backend (kiosk.py)                     ElevenLabs
  kiosk_visit "entered" ─ GET /api/kiosk/shopper ─────────▶ first name, budget, greeting
  (first timer: the spoken welcome plays first)
  GET /api/kiosk/voice-session?k= ────────────────────────▶ voice.signed_url_or_503 ─ get-signed-url ─▶ (xi-api-key)
        ◀── {signed_url, dynamic_variables {first_name, budget_usd, remaining_usd, dietary, is_first_visit,
             mode: "kiosk"}, max_duration_s: 300, silence_timeout_s: 120}
  Conversation.startSession({signedUrl, dynamicVariables, clientTools}) ═══ websocket ═══▶ agent
  POST /api/kiosk/conversation?k= {"active": true} ────────▶ kiosk_voice {active: true} ─▶ the shopper's phone
  client tool ─▶ /api/kiosk/catalog | /api/kiosk/cart | POST, DELETE /api/kiosk/plan  (the CURRENT shopper)
  kiosk_say (cart line) ─▶ conversation.sendContextualUpdate(...) instead of text to speech
```

- **Routes** (all kiosk token only, spec 8.16): `GET /api/kiosk/voice-session` (404 `not_available` with voice
  off, 409 `no_shopper`, 503 `voice_unavailable`), `POST /api/kiosk/conversation {"active", "reason"?}`,
  `GET /api/kiosk/catalog`, `GET /api/kiosk/cart`, `POST /api/kiosk/plan {"goal_text"}`, `DELETE /api/kiosk/plan`.
  The API key stays on the backend; the kiosk only gets the 15 minute signed URL, which is never logged.
- **First line:** the agent's first message is "Hi {{first_name}}, you have ${{remaining_usd}} to spend. What are
  you looking for today?" A first timer hears the kiosk's fuller spoken welcome first (the conversation waits until
  the kiosk is quiet); for a returning shopper the short greeting is only shown as a caption, because the agent's
  first line says the same thing. If the conversation cannot start (voice off, no agent, network), the kiosk speaks
  the greeting itself and keeps narrating with text to speech.
- **Client tools** (same names and parameters as the phone, so the dashboard config does not change): `get_catalog`,
  `get_cart`, `make_plan(goal_text)`, `clear_plan`. They act on the shopper in the store through the kiosk token
  routes, never on whoever holds the tablet. `make_plan` uses the same planner as the phone (`intent.create_plan`,
  logged with `via: "kiosk"`) and its bays glow on every shelf map, the kiosk's included (`plan_bays`).
- **Cart lines during a conversation** are sent to the agent as contextual updates instead of being spoken by the
  kiosk, so the agent mentions them naturally and never talks over itself. ElevenLabs: a `contextual_update` is
  "incorporated as background information in the conversation" and "does not interrupt the current conversation
  flow": https://elevenlabs.io/docs/agents-platform/customization/events/client-to-server-events. The SDK method
  is `conversation.sendContextualUpdate(text)`, checked in `@elevenlabs/client@1.25.0`
  `dist/BaseConversation.d.ts` (it sends `{"type": "contextual_update", "text"}`).
- **Phone:** while the kiosk conversation is active, `intent.html`'s button reads **Talk to the kiosk** and is
  disabled (`kiosk_voice` on the shopper's socket, also sent on connect), and `/api/voice/session` answers 409
  `kiosk_conversation_active`. A phone conversation that is running when the kiosk starts is ended. When there is
  no kiosk conversation the phone voice works exactly as before.
- **Ending:** the kiosk ends the conversation when the shopper scans the exit, when the visit ends (paid, empty
  exit, cancelled), after 2 minutes of silence (no message from either side), and after 5 minutes at most. The
  backend also frees the phone on the exit scan and at the end of the visit, and forgets a conversation the kiosk
  never closed (a crashed tablet) 30 s after the 5 minute cap. Afterwards the kiosk narrates the cart with text to
  speech again. Events: `kiosk_voice_session`, `kiosk_voice_session_error` (`reason`), `kiosk_conversation`
  (`active`, `reason`, `duration_s`).
