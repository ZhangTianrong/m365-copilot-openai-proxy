# Token Refresh Automation Options

The `substrate.office.com` API requires a user JWT that expires in about one hour. Admin consent is blocked, so the practical automation path is still browser-backed token capture.

## Current implementation

This repository now includes a minimal Playwright refresher designed for Ubuntu or Docker-based deployments:

- `copilot-openai-proxy login`
  Creates a persistent browser profile, opens the Copilot page in headed mode, and saves the first token to `M365_ACCESS_TOKEN_FILE`.
- `copilot-openai-proxy login` with `M365_LOGIN_EMAIL`, `M365_LOGIN_PASSWORD`, and `M365_LOGIN_TOTP_SECRET`
  Attempts a fully headless Microsoft sign-in first, then captures the Copilot token once the browser reaches the chat UI.
- `copilot-openai-proxy refresh-token`
  Reuses that profile for a one-shot refresh, clicks the remembered Microsoft account tile when the profile lands on the account picker, and then nudges the Copilot chat input to provoke the token-bearing request.
- `copilot-openai-proxy refresh-daemon`
  Runs headless, refreshes before expiry, and updates the shared token file in place.
- `copilot-openai-proxy serve`
  Reads the token file on demand, so the API does not need to restart after refresh.

The default persistent paths are:

- token file: `.state/access_token.txt`
- Playwright profile: `.state/profile`

---

## Option A — Playwright persistent profile

This is the preferred path for Linux and Docker deployments.

How it works:

1. Do a one-time interactive sign-in with `copilot-openai-proxy login`.
   Or configure the credential env vars and let the refresher attempt headless email/password/TOTP sign-in first.
2. Persist the browser profile directory.
3. Run `copilot-openai-proxy refresh-daemon` headlessly. The refresher will click the remembered account tile if needed, then focus the Copilot input and type-then-delete a character while waiting for the next token capture.
4. Share the token file with the API service.

Why this is the right default:

- no Edge remote debugging requirement
- no dependence on your day-to-day desktop browser profile
- works naturally with a dedicated Docker volume or host directory
- keeps the automation scope narrow: just sign in once, then refresh the Copilot token

Tradeoffs:

- requires Playwright and a browser runtime
- fully headless first login is possible only when Microsoft accepts the browser flow with email, password, and TOTP alone
- Microsoft may still require interactive reauthentication later; if the saved profile reaches an `Enter password` prompt, headless refresh stops there

---

## Option B — Edge remote debugging (legacy Windows path)

The repository previously had a lightweight CDP-based refresh path aimed at a Windows-hosted Edge session started with `--remote-debugging-port=9222`.

That approach is still reasonable if all of the following are true:

- you are staying on Windows
- you want to reuse an already-open Edge session
- you do not mind launching Edge with a debugging flag

It is not the preferred path for Ubuntu or Docker deployments.

---

## Option C — Windows WAM / MSAL broker

`msal` with `allow_broker=True` was investigated earlier but is not sufficient here. The `substrate.office.com` resource still requires pre-authorization and cannot be cleanly reused through an external client ID.

---

## Option D — Admin consent

This remains the cleanest long-term fix if your IT admin is willing to help:

1. Register a dedicated Entra app and grant the required delegated permissions.
2. Or grant admin consent for an existing Microsoft first-party CLI app that exposes the needed resource.

Either path would remove the need for browser automation entirely.
