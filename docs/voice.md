# F19 · Voice store agent (ElevenLabs Agents)

Shoppers tap **Talk to SpeedMart** on `intent.html` and talk to an ElevenLabs agent. The agent can only act
through four client tools that run in the shopper's browser and call the store's own endpoints, so it cannot
invent products or prices. The typed intent flow (F18) on the same page is still there as the fallback.

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
| `GET /api/voice/session` | logged-in member | `{"signed_url", "member_first_name", "budget_usd", "dietary"}`. `401 not_logged_in`, `404 unknown_member`, `503 voice_unavailable` if the keys are missing or ElevenLabs fails |
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
Hi {{first_name}}! I'm the SpeedMart store agent. What do you need today?
```

### Agent → System prompt

```
You are the SpeedMart store agent in a tiny smart store: one shelf with five numbered bays, watched by a camera.
The products are drinks and snacks (for example a hydration drink, an energy drink, chips, water and a vegan
snack), but always take names, prices and bays from get_catalog.
The shopper is {{first_name}}. Their budget is ${{budget_usd}}. Their dietary need is: {{dietary}}.

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
```

### Agent → Dynamic variables (placeholders for testing in the dashboard)

| Name | Test value |
|---|---|
| `first_name` | `Maya` |
| `budget_usd` | `20` |
| `dietary` | `none` |

The page always sends all three. `dietary` is `"none"` when the member did not pick one.

### Agent → Tools → Add tool → Client tool (create four)

Tick **Wait for response** on every tool. Names and parameter identifiers are case-sensitive.

1. **`get_catalog`**
   - Description: `Returns every product on the SpeedMart shelf as JSON: sku, name, price_usd, tags (for example drink, snack, hydration, caffeine, vegan), pairs_with and bays (the numbers printed on the shelf cards, starting at 1). Call this before recommending or pricing anything. If a product's tags include caffeine, say so when you recommend it.`
   - Parameters: none

2. **`get_cart`**
   - Description: `Returns the shopper's live cart as JSON: in_store, items (name, qty, line_total_usd), total_usd with tax, budget_usd, over_budget and payment (a note that the card is a demo test Visa card and no real money moves). If in_store is false the shopper has not entered the store yet.`
   - Parameters: none

3. **`make_plan`**
   - Description: `Builds a shopping plan from the shopper's goal using only real products, stock and their budget. It shows the plan and a shelf map on their phone with the chosen bays glowing. Returns JSON: goal_summary, items (name, qty, unit_price_usd, reason; the reason says when an item has caffeine), est_total_usd with tax, budget_usd, fits_budget, bays_to_find (bay card numbers) and where_to_look (a sentence to say, e.g. "Look for bay 1 and bay 4, they're glowing on your screen.").`
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
- **Advanced → Call limits → Max conversation duration:** `120` seconds. The page also ends the session itself
  after 2 minutes.
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
