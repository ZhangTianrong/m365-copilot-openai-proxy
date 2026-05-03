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
- Each request starts a new Copilot conversation (no persistent sessions).
- System prompts and conversation history are folded into the message as plain text.
- Tool calls and token usage are not supported.
- **Claude Code:** Agentic features (file reading, bash, code editing) require tool use, which this proxy does not support. Use the proxy for general Q&A only; keep Claude Code on the real Anthropic API for coding tasks.

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

If you already have cookies exported from a logged-in Copilot browser session, you can bootstrap the persistent Playwright profile without a manual sign-in:

```powershell
uv run copilot-openai-proxy login --cookies cookies.json --headless
```

The cookie file must be JSON in either of these shapes:

- a top-level array of cookie objects
- an object with a top-level `cookies` array, such as a Playwright storage-state export

Cookie objects must include `name`, `value`, and either `url` or `domain`.

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

---

## Docker Compose

The repository includes a two-service Compose stack for Ubuntu hosts:

```powershell
docker compose up -d --build
```

- `api` serves the OpenAI-compatible HTTP API.
- `token-refresher` runs the Playwright refresh daemon and persists its browser profile in `./state/profile`.

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
  copilot-openai-proxy login
```

If your desktop session uses a different Xauthority path, substitute the correct host path.

After the Copilot page finishes loading, the token is usually captured automatically. If the command does not exit after sign-in, click into the Copilot chat box and type a character to trigger the WebSocket request that carries the token. You do not need to submit the message.

Once `Token saved to /data/access_token.txt.` is printed and the command exits, `docker compose up -d` is enough for normal headless operation.

After a successful headed login, you can snapshot the authenticated Playwright profile's cookies for later cookie-only bootstrap tests:

```bash
docker compose run --rm \
  token-refresher \
  copilot-openai-proxy export-cookies /data/cookies.json
```

If you already have exported cookies, you can seed the shared Playwright profile first:

```bash
docker compose run --rm \
  token-refresher \
  copilot-openai-proxy import-cookies /data/cookies.json
```

Then either run a headless token bootstrap:

```bash
docker compose run --rm \
  token-refresher \
  copilot-openai-proxy login --cookies /data/cookies.json --headless
```

Or start the normal daemon and let it refresh from the seeded profile.

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
| `M365_LOGIN_URL` | `https://m365.cloud.microsoft/chat` | Page the refresher opens to obtain a token |
| `M365_BROWSER_CHANNEL` | unset | Optional Playwright browser channel such as `msedge` |
| `M365_TOKEN_REFRESH_BUFFER_SECONDS` | `300` | Refresh token this many seconds before expiry |
| `M365_TOKEN_REFRESH_RETRY_SECONDS` | `30` | Retry delay after refresh failures |
| `M365_TIME_ZONE` | `Asia/Tokyo` | Time zone sent with each request |
| `M365_MODEL_ALIAS` | `m365-copilot` | Model name returned by `/v1/models` |

---

## Token automation notes

See [TOKEN_REFRESH.md](TOKEN_REFRESH.md) for the design tradeoffs between Playwright, Edge CDP, and the older manual flow.
