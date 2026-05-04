# Microsoft 365 Copilot OpenAI Proxy

A local proxy server that exposes your company's Microsoft 365 Copilot as an OpenAI-compatible API. No Azure app registration or admin consent required.

## How it works

The proxy connects to `substrate.office.com` — the same WebSocket API the M365 Copilot web UI uses — and wraps it in an OpenAI-compatible HTTP server. Authentication uses a short-lived token extracted from your browser session.

## Endpoints

- `GET /healthz`
- `GET /v1/models`
- `POST /v1/chat/completions` — OpenAI Chat Completions (streaming supported)
- `POST /v1/responses` — OpenAI Responses API (streaming supported)
- `POST /v1/messages` — Anthropic Messages API (non-streaming)

## Constraints

- Token expires in ~1 hour. A Playwright refresher is included for one-time login plus automatic refresh.
- By default, each request starts a new Copilot conversation.
- System prompts and conversation history are folded into the message as plain text.
- Tool calls and token usage are not supported.
- **Claude Code:** Agentic features (file reading, bash, code editing) require tool use, which this proxy does not support. Use the proxy for general Q&A only; keep Claude Code on the real Anthropic API for coding tasks.

## Multimodal Status

- OpenAI Chat Completions and OpenAI Responses accept `data:image/...;base64,...` image parts in user messages.
- Those images are uploaded to Copilot and forwarded as image attachments on the proxied request.
- Because each API call still becomes one fresh Copilot turn, earlier user-message images are replayed into that turn and referenced in the reconstructed plain-text transcript as `[Image N]`.
- Non-image attachments, remote image URLs, and unsupported image shapes are ignored instead of failing the whole request.
- Anthropic-style requests stay text-only for now. Any attachment parts are dropped.

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

This creates a persistent Playwright browser profile under `.state/profile`, waits for the Copilot page to authenticate, and saves the current token to `.state/access_token.txt`.

If `M365_LOGIN_EMAIL`, `M365_LOGIN_PASSWORD`, and `M365_LOGIN_TOTP_SECRET` are set, the Playwright login flow will first try a fully headless Microsoft sign-in using those credentials and the current TOTP code.

### 3. Start the refresher

```powershell
uv run copilot-openai-proxy refresh-daemon
```

The refresher reuses the persistent profile, refreshes the token before expiry, and updates the shared token file in place.

### 4. Start the server

```powershell
uv run copilot-openai-proxy serve
```

Server runs at `http://127.0.0.1:8000` by default.

```powershell
uv run copilot-openai-proxy serve --host 127.0.0.1 --port 8000
```

The API reads the token from `M365_ACCESS_TOKEN_FILE` on demand, so refreshed tokens are picked up without restarting the server.

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

### 6. Optional debug logging

Verbose proxy logging is off by default. To log sanitized request bodies, translated prompts, and conversation reuse routing decisions:

```bash
M365_DEBUG_LOGGING=true
```

With Docker Compose, set it in `.env` and inspect the API logs with:

```bash
docker compose logs -f api
```

---

## Docker Compose

The repository includes a two-service Compose stack for Ubuntu hosts:

```powershell
docker compose up -d --build
```

- `api` serves the OpenAI-compatible HTTP API.
- `token-refresher` runs the Playwright refresh daemon and persists its browser profile in `./state/profile`.

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
- focusing the Copilot chat box, typing a character, and deleting it without submitting

If a headed login still does not exit after sign-in, click into the chat box and type a character manually as a fallback.

If the saved profile lands on `Enter password` and no credential env vars are set, Microsoft is requiring interactive reauthentication and the headless refresher cannot complete that step by itself.

Once `Token saved to /data/access_token.txt.` is printed and the command exits, `docker compose up -d` is enough for normal headless operation.

---

## Manual fallback

If you want to bypass Playwright entirely, you can still paste a token manually:

```powershell
uv run copilot-openai-proxy set-token
```

Paste either the full WebSocket URL or just the `access_token` value. It will be written to the token file.

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
| `M365_DEBUG_LOGGING` | `false` | Emit sanitized proxy request, translation, and routing logs |
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

See [TOKEN_REFRESH.md](TOKEN_REFRESH.md) for the design tradeoffs between Playwright, Edge CDP, and the older manual flow.
