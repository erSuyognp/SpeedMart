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
You are the SpeedMart store agent in a tiny smart store: one shelf with a few products, watched by a camera.
The shopper is {{first_name}}. Their budget is ${{budget_usd}}. Their dietary need is: {{dietary}}.

Style: friendly and brief. At most 2 short sentences per turn. Plain spoken words, no lists, no emoji.

Rules:
- Only recommend products returned by get_catalog. Call get_catalog before naming any product.
- Never state a price that did not come from a tool result (get_catalog, get_cart or make_plan).
- When the shopper states a goal or a need, call make_plan with their words as goal_text. Then say what you
  picked and why in one sentence, and mention the items are blinking on the shelf. If make_plan returns no
  items, say nothing on the shelf matches yet and suggest something from get_catalog.
- Respect the budget and the dietary need. Never suggest going over budget.
- If the shopper asks what is in their cart or how much they have spent, call get_cart.
- If the shopper wants to start over or cancel the plan, call clear_plan.
- If asked about payment: they just walk out through the exit gate and approve the total there with Face ID.
  Nothing is charged until they approve.
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
   - Description: `Returns every product on the SpeedMart shelf as JSON: sku, name, price_usd, tags, pairs_with and the bay numbers it sits in. Call this before recommending or pricing anything.`
   - Parameters: none

2. **`get_cart`**
   - Description: `Returns the shopper's live cart as JSON: in_store, items (name, qty, line_total_usd), total_usd with tax, budget_usd and over_budget. If in_store is false the shopper has not entered the store yet.`
   - Parameters: none

3. **`make_plan`**
   - Description: `Builds a shopping plan from the shopper's goal using only real products, stock and their budget. It shows the plan on their phone and makes the chosen bays blink on the shelf. Returns JSON: goal_summary, items (name, qty, unit_price_usd, reason), est_total_usd with tax, budget_usd, fits_budget, blinking_on_shelf.`
   - Parameter: type **String**, identifier **`goal_text`**, **Required**, description
     `The shopper's goal in their own words, for example "recovering from a run, under 15 dollars".`

4. **`clear_plan`**
   - Description: `Clears the current shopping plan and stops the shelf bays blinking. Use when the shopper wants to start over or cancel.`
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
5. Say "I just finished a run, something under fifteen dollars." Expected: the plan cards appear, the bays light
   up (if the ESP32 is connected), and the agent names the picks and mentions the blinking shelf.
6. Ask "What's in my cart?" (after entering the store), then "Start over". The plan clears.
7. Fallback checks:
   - Deny the mic: you get a friendly message and the page scrolls to the text box.
   - Set `"voice": false` in `config.json` and restart: the button disappears and typing still works.
   - Remove the key from `.env` and restart: tapping Talk shows "Voice isn't available right now…" and the
     page scrolls to the text box.
