# Microsoft 365 Copilot OpenAI Proxy

A local proxy server that exposes Microsoft 365 Copilot as OpenAI-compatible and Anthropic-compatible APIs. No Azure app registration or admin consent required.

This repository is now a fork with behavior that intentionally diverges from the upstream project. The source of truth is the code in this repo and the constraints documented below, not upstream README text or examples.

## How it works

The proxy connects to `substrate.office.com` — the same WebSocket API the M365 Copilot web UI uses — and wraps it in an OpenAI-compatible HTTP server. Authentication uses a short-lived browser session plus a persisted auth snapshot captured from the web UI.

## Endpoints

- `GET /healthz`
- `GET /v1/models`
- `POST /v1/chat/completions` — OpenAI Chat Completions (streaming supported)
- `POST /v1/responses` — OpenAI Responses API (streaming supported)
- `POST /v1/messages` — Anthropic Messages API (streaming supported)

## Constraints

- Token expires in ~1 hour. A Playwright refresher is included for one-time login plus automatic refresh.
- By default, each request starts a new Copilot conversation.
- Optional conversation reuse can be enabled globally to continue the latest matching conversation state from a local SQLite cache.
- System prompts and conversation history are folded into the message as plain text.
- `m365-copilot` itself does not support native tool use translation. A second proxy profile, `m365-minis`, can normalize a trailing embedded JSON `function_call` array into OpenAI-style tool-call output for Chat Completions and Responses.
- Token usage is not supported.
- The public attachment surface is image-only. Explicit `file` / `input_file` parts are ignored.
- Remote image URLs are not fetched. Only `data:image/...;base64,...` parts are accepted.
- **Claude Code:** Agentic features (file reading, bash, code editing) require tool use, which this proxy does not support. Use the proxy for general Q&A only; keep Claude Code on the real Anthropic API for coding tasks.

## Multimodal Status

- OpenAI Chat Completions and OpenAI Responses accept `data:image/...;base64,...` image parts in user messages.
- Those images are uploaded to Copilot and forwarded as attachments on the proxied request.
- In `enterprise` mode, images use the Copilot image upload path.
- In `personal` mode, images are internally converted to PDF and sent through the file-upload path because the consumer UI does not expose the same image upload transport.
- Conversation reuse now applies to image requests as well. Images are no longer forced into stateless mode.
- Earlier user-message images are still represented in the reconstructed plain-text transcript as `[Image N]` markers.
- Explicit non-image file parts, remote image URLs, and unsupported image shapes are ignored instead of failing the whole request.
- Anthropic-style requests remain text-only from the public API perspective. Non-text attachment parts are dropped.

## Proxy Profiles

- `/v1/models` exposes two public aliases: `m365-copilot` and `m365-minis`.
- `m365-copilot` is the base profile. It keeps the current behavior and returns Copilot text directly.
- `m365-minis` uses the same underlying Copilot transport, auth, model probing, and Copilot model selection settings as `m365-copilot`.
- `m365-minis` adds a hidden Minis-specific system instruction before the request is sent to Copilot.
- If Copilot ends its reply with a JSON array of OpenAI Responses-style `function_call` items, `m365-minis` strips that suffix from the visible assistant prose and returns structured tool calls instead:
  - Chat Completions returns `tool_calls` with `finish_reason: "tool_calls"`.
  - Responses returns `function_call` output items.
- `m365-minis` streaming is normalized after the full Copilot reply is buffered, so correctness is favored over token-by-token latency.
- Anthropic `/v1/messages` does not use this customization layer.

---

## Setup

### 1. Install

```powershell
uv sync --extra refresh
```

### 2. One-time login

```powershell
uv run copilot-openai-proxy login
```

This creates a persistent Playwright browser profile under `.state/profile`, waits for the Copilot page to authenticate, saves an auth snapshot to `.state/auth_session.json`, and mirrors the current token to `.state/access_token.txt`.

Set the account mode explicitly before first login:

```bash
M365_ACCOUNT_MODE=enterprise
```

or:

```bash
M365_ACCOUNT_MODE=personal
```

If `M365_LOGIN_EMAIL`, `M365_LOGIN_PASSWORD`, and `M365_LOGIN_TOTP_SECRET` are set, the Playwright login flow will first try a fully headless Microsoft sign-in using those credentials and the current TOTP code.

Do not share the same `M365_PROFILE_DIR` or `M365_AUTH_STATE_FILE` between `enterprise` and `personal` runs. Keep separate state for each account mode.

In `personal` mode, the login automation follows the verified consumer path through `login.live.com`, including:

- `Other ways to sign in`
- `Use your password`
- `Stay signed in`

### 3. Start the refresher

```powershell
uv run copilot-openai-proxy refresh-daemon
```

The refresher reuses the persistent profile, refreshes the token before expiry, updates the auth snapshot first, and then mirrors the current token file in place.

### 4. Start the server

```powershell
uv run copilot-openai-proxy serve
```

Server runs at `http://127.0.0.1:8000` by default.

```powershell
uv run copilot-openai-proxy serve --host 127.0.0.1 --port 8000
```

The API reads the auth snapshot from `M365_AUTH_STATE_FILE` on demand, so refreshed tokens and personal-session transport metadata are picked up without restarting the server. `M365_ACCESS_TOKEN_FILE` remains as a compatibility mirror.

### 5. Optional conversation reuse

Conversation reuse is off by default. To let the proxy continue the latest matching Copilot thread instead of always starting a fresh one, enable:

```bash
M365_ENABLE_CONVERSATION_REUSE=true
```

When enabled, the proxy:

- hashes the request history excluding the current final user message
- reuses a stored Copilot conversation when that latest-history key matches
- otherwise falls back to the current stateless behavior and reconstructs prior history into the prompt prefix

The local cache is stored in SQLite and only tracks the latest checkpoint for each Copilot conversation.

Behavior differences by account mode:

- `enterprise`: reuse stores the Copilot conversation id only.
- `personal`: reuse stores both the resolved Copilot conversation id and a transport session id, because follow-up websocket turns require additional client session continuity.

### 6. Optional debug logging

Verbose proxy logging is off by default. To log sanitized request bodies, ignored tool-related fields, translated prompts, and conversation reuse routing decisions:

```bash
M365_DEBUG_LOGGING=true
```

With Docker Compose, set it in `.env` and inspect the API logs with:

```bash
docker compose logs -f api
```

### 7. Optional Copilot model selection

After a real Microsoft login or reauthentication, the Playwright refresher now probes the live Copilot UI and logs:

- the visible top-level model labels
- the visible nested `GPT` submenu labels
- the observed transport mapping for each label

That automatic probe is enabled by default:

```bash
M365_AUTO_PROBE_MODELS_ON_LOGIN=true
```

Set it to `false` to skip probing entirely.

The proxy can optionally request a specific Copilot conversation model by exact visible UI label:

```bash
M365_COPILOT_MODEL_NAME="GPT 5.5 Think Deeper"
```

The OpenAI-facing API model id still stays `m365-copilot`, and conversation reuse remains keyed only on request history, not on the selected Copilot model.

The live post-login probe is the authoritative source of truth. The proxy currently includes validated transport mappings for these observed labels:

- `Auto`
- `Quick Response`
- `Think Deeper`
- `GPT 5.5 Think Deeper`
- `GPT 5.3 Quick Response`
- `GPT 5.4 Think Deeper`
- `GPT 5.2 Quick Response`
- `GPT 5.2 Think Deeper`

Each of those currently maps to a `tone` value observed on the real websocket request. `threadLevelGptId` is left empty unless a future live probe proves otherwise.

If `M365_COPILOT_MODEL_NAME` does not exactly match a validated label, the proxy logs a warning and falls back to the default Copilot model selection.

`copilot-openai-proxy probe-models` is still available as a developer convenience, but the supported user path is the automatic post-login probe.

---

## Docker Compose

The repository includes a two-service Compose stack for Ubuntu hosts:

```powershell
docker compose up -d --build
```

- `api` serves the OpenAI-compatible HTTP API.
- `token-refresher` runs the Playwright refresh daemon and persists its browser profile in `./state/profile`.

If you use both `enterprise` and `personal` accounts on the same machine, do not point both stacks at the same `./state` directory. Keep separate host directories, profiles, and auth snapshots per account mode.

For a fully headless first login, set these environment variables for the `token-refresher` service through your shell or `.env` file before running Compose:

```bash
M365_LOGIN_EMAIL=...
M365_LOGIN_PASSWORD=...
M365_LOGIN_TOTP_SECRET=...
M365_TOKEN_CAPTURE_TIMEOUT_SECONDS=600
```

`docker compose up` does not accept `-e` the way `docker compose run` does. Use shell-prefixed environment variables or a `.env` file instead:

```bash
M365_LOGIN_EMAIL=... \
M365_LOGIN_PASSWORD=... \
M365_LOGIN_TOTP_SECRET=... \
M365_TOKEN_CAPTURE_TIMEOUT_SECONDS=600 \
docker compose up -d --build
```

For the first login, run a one-shot interactive browser session against the same shared volume. On an Ubuntu X11 desktop session, authorize the container's `root` user first:

```bash
xhost +SI:localuser:root
```

Then launch the login flow with the host display, X11 socket, and Xauthority file mounted into the container:

```bash
docker compose run --rm \
  -e DISPLAY=:0 \
  -e XAUTHORITY=/run/user/1000/gdm/Xauthority \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v /run/user/1000/gdm/Xauthority:/run/user/1000/gdm/Xauthority:ro \
  token-refresher \
  copilot-openai-proxy login --timeout 1800
```

If your desktop session uses a different Xauthority path, substitute the correct host path.

`docker compose up -d` runs the headless `refresh-daemon`. It is appropriate for automatic credential-based bootstrap, but it is not the command to use if you want to manually try alternate sign-in choices or observe whether Microsoft shows a "Stay signed in" prompt. For that, use the headed `copilot-openai-proxy login` command above.

After the Copilot page finishes loading, the login and refresh flow now tries to trigger token capture automatically by:

- submitting the Microsoft email, password, and TOTP steps when those env vars are configured
- clicking the remembered Microsoft account tile if the profile lands on the account picker
- on personal accounts, choosing `Other ways to sign in` and `Use your password` when required
- focusing the Copilot chat box, typing a character, and deleting it without submitting

If a headed login still does not exit after sign-in, click into the chat box and type a character manually as a fallback.

If the saved profile lands on `Enter password` and no credential env vars are set, Microsoft is requiring interactive reauthentication and the headless refresher cannot complete that step by itself.

Once the refresher logs `refresh.saved` or the login command logs `login.saved`, `docker compose up -d` is enough for normal headless operation.

When that login or reauthentication passes through the real Microsoft sign-in flow, the refresher also runs the model probe automatically and prints the discovered labels plus the observed websocket transport mapping. That probe is for discovery and logging only; a failure there does not fail token acquisition.

If `M365_ACCOUNT_MODE` does not match the actual sign-in flow, login fails fast instead of silently switching behavior. For example, a `personal` Outlook/consumer account must not be run with `M365_ACCOUNT_MODE=enterprise`.

---

## Manual fallback

If you want to bypass Playwright entirely, you can still paste credentials manually:

```powershell
uv run copilot-openai-proxy set-token
```

In `enterprise` mode, paste either the full WebSocket URL or just the `access_token` value.

In `personal` mode, paste the full Copilot WebSocket URL. A bare token is not enough, because personal sessions also require the captured websocket transport metadata. The auth snapshot will be written to `M365_AUTH_STATE_FILE`, and the token mirror will be written to `M365_ACCESS_TOKEN_FILE`.

---

## Using with AI coding tools

### OpenCode

```powershell
$env:OPENAI_BASE_URL = "http://127.0.0.1:8000"
$env:OPENAI_API_KEY = "dummy"
opencode
```

Select **OpenAI API** as the provider. Model: `m365-copilot`.

### Continue (VS Code extension)

Add to `~/.continue/config.json`:

```json
{
  "models": [
    {
      "title": "M365 Copilot",
      "provider": "openai",
      "model": "m365-copilot",
      "apiBase": "http://127.0.0.1:8000/v1",
      "apiKey": "dummy"
    }
  ]
}
```

### Claude Code

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8000"
$env:ANTHROPIC_API_KEY = "dummy"
claude
```

### Any OpenAI-compatible client

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:8000/v1` |
| API Key | `dummy` |
| Model | `m365-copilot` |

---

## Manual API examples

### Chat Completions

```powershell
$body = @{
  model = "m365-copilot"
  messages = @(
    @{ role = "system"; content = "Be concise." },
    @{ role = "user"; content = "hi" }
  )
} | ConvertTo-Json -Depth 10

$r = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/chat/completions" -ContentType "application/json" -Body $body
$r.choices[0].message.content
```

### Streaming

```powershell
$body = @{
  model = "m365-copilot"
  stream = $true
  messages = @(@{ role = "user"; content = "hi" })
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/chat/completions" -ContentType "application/json" -Body $body
```

### Anthropic-style

```powershell
$body = @{
  model = "m365-copilot"
  system = "Be concise."
  messages = @(@{ role = "user"; content = "hi" })
} | ConvertTo-Json -Depth 10

$r = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/messages" -ContentType "application/json" -Body $body
$r.content[0].text
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `M365_ACCESS_TOKEN` | unset | Fallback bearer token when no token file exists |
| `M365_ACCESS_TOKEN_FILE` | `.state/access_token.txt` | Shared token file used by the API and refresher |
| `M365_PROFILE_DIR` | `.state/profile` | Persistent Playwright browser profile |
| `M365_AUTO_PROBE_MODELS_ON_LOGIN` | `true` | After a real login or reauthentication, probe the live Copilot UI and log visible model labels plus observed transport mappings |
| `M365_COPILOT_MODEL_NAME` | unset | Optional exact visible Copilot UI label to request for new turns; falls back to default if the label has no validated transport mapping |
| `M365_ENABLE_CONVERSATION_REUSE` | `false` | Reuse the latest matching Copilot conversation from the local history DB |
| `M365_CONVERSATION_DB_PATH` | `.state/conversation_reuse.db` | SQLite database for conversation reuse state |
| `M365_CONVERSATION_MAX_CONVERSATIONS` | `500` | Maximum number of cached conversation rows before LRU eviction |
| `M365_LOGIN_URL` | `https://m365.cloud.microsoft/chat` | Page the refresher opens to obtain a token |
| `M365_BROWSER_CHANNEL` | unset | Optional Playwright browser channel such as `msedge` |
| `M365_TOKEN_CAPTURE_TIMEOUT_SECONDS` | `600` | How long login or daemon refresh attempts wait for Copilot token capture |
| `M365_TOKEN_REFRESH_BUFFER_SECONDS` | `300` | Refresh token this many seconds before expiry |
| `M365_TOKEN_REFRESH_RETRY_SECONDS` | `30` | Retry delay after refresh failures |
| `M365_TIME_ZONE` | `Asia/Tokyo` | Time zone sent with each request |
| `M365_MODEL_ALIAS` | `m365-copilot` | Model name returned by `/v1/models` |

---

## Token automation notes
