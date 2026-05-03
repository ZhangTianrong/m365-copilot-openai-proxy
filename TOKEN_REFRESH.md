# Token Refresh Automation Options

The `substrate.office.com` API requires a user JWT that expires in about one hour. Admin consent is blocked, so the practical automation path is still browser-backed token capture.

## Current implementation

This repository now includes a minimal Playwright refresher designed for Ubuntu or Docker-based deployments:

- `copilot-openai-proxy login`
  Creates a persistent browser profile, opens the Copilot page in headed mode, and saves the first token to `M365_ACCESS_TOKEN_FILE`.
- `copilot-openai-proxy refresh-token`
  Reuses that profile for a one-shot refresh.
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
2. Persist the browser profile directory.
3. Run `copilot-openai-proxy refresh-daemon` headlessly.
4. Share the token file with the API service.

Why this is the right default:

- no Edge remote debugging requirement
- no dependence on your day-to-day desktop browser profile
- works naturally with a dedicated Docker volume or host directory
- keeps the automation scope narrow: just sign in once, then refresh the Copilot token

Tradeoffs:

- requires Playwright and a browser runtime
- the first login still needs a headed browser session

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
