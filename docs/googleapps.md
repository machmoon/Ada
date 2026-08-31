# Google Workspace integration (`googleapps/`)

`python -m googleapps` delivers a finished pipeline run to Google Chat, Gmail,
and Google Calendar. It is stdlib-only — no `google-auth`, no
`google-api-python-client` — and calls `silkscreen.agents.generate_pcb`
exactly as the CLI does.

> **Honesty first:** this integration was written offline against the
> documented REST surface and has **never been run against live Google APIs**.
> The offline tests pin request construction — URLs, PKCE, MIME, the card
> payload — against a recorded transport. A wrong scope name or a payload
> field Google rejects would only show up on the first live run. Treat that
> run as the real test.

## What each destination needs

| Destination | Credential | Env var |
|---|---|---|
| Chat space card (`--chat`) | Incoming-webhook URL (the URL **is** the secret) | `GOOGLEAPPS_CHAT_WEBHOOK` |
| Gmail send (`--email`) | OAuth Desktop-app client + one-time `auth` | `GOOGLEAPPS_CLIENT_ID`, `GOOGLEAPPS_CLIENT_SECRET` |
| Calendar review event (`--schedule`) | same OAuth client and token | same |
| The pipeline itself (`run`) | Gemini API key | `GOOGLE_API_KEY` |

The three are independent: you can post Chat cards with no OAuth client at
all, or email results with no webhook. `python -m googleapps` reads `.env`
from the working directory through the same loader as `python -m silkscreen`,
so a repo-root `.env` serves both (the service still does not read `.env`).

## 1. Create (or reuse) a Google Cloud project

The hackathon project that already holds your `GOOGLE_API_KEY` works fine —
Gmail/Calendar OAuth and the Gemini key do not conflict. Otherwise create one
at <https://console.cloud.google.com/projectcreate>.

## 2. Enable the two APIs

**APIs & Services → Library**, enable:

- **Gmail API**
- **Google Calendar API**

(Chat webhooks need no API enablement — the webhook is created in the Chat
space itself, step 4.)

## 3. Create the OAuth client (type: Desktop app)

1. **APIs & Services → OAuth consent screen**: configure it once (External is
   fine for a personal account; add yourself as a test user while the app is
   unverified).
2. **APIs & Services → Credentials → Create credentials → OAuth client ID**,
   application type **Desktop app**.
3. Copy the **Client ID** into `GOOGLEAPPS_CLIENT_ID` and the **Client
   secret** into `GOOGLEAPPS_CLIENT_SECRET` (in `.env` or the environment —
   never into code). A Desktop-app client secret is not treated as
   confidential by Google's model — the flow's security comes from PKCE — but
   keep it out of the repo all the same.

The requested scopes are exactly two, and neither can read your mailbox:
`gmail.send` and `calendar.events`.

## 4. Create the Chat incoming webhook

In the Chat space that should receive run cards: **space name → Apps &
integrations → Webhooks → Add webhook**. Name it (e.g. `silkscreen`), copy
the generated URL into `GOOGLEAPPS_CHAT_WEBHOOK`.

The URL embeds the credential. Never commit it, paste it into a chat, or log
it — `check` and every error message show only its tail. The code refuses to
send to anything that is not `https://chat.googleapis.com/v1/spaces/…`.

## 5. Sign in

```bash
python -m googleapps auth
```

This opens Google's consent page in your browser, catches the redirect on a
`127.0.0.1` loopback port, exchanges the code (PKCE S256; the verifier never
touches disk), and writes the token to
`~/.config/silkscreen/google-token.json` (override with
`GOOGLEAPPS_TOKEN_PATH`) with mode **0600**. The access token refreshes
transparently on later runs; if the refresh token is ever revoked, the error
tells you to run `auth` again.

Check the result — this makes no network call:

```bash
python -m googleapps check
```

## 6. Run

```bash
python -m googleapps run "a 3.3V LDO board with an AMS1117-3.3" \
    -o out/board.kicad_pcb \
    --chat \
    --email lead@example.com --email james@example.com \
    --schedule --attendee lead@example.com
```

- `--chat` posts a cardsV2 card: verdict, stage timings, board size and
  solver status, review counts with blocker titles, and **every unrouted net
  named verbatim** with the router's reason. A card never says "board ready"
  over a ratsnest.
- `--email` sends a plain-text summary with the emitted `.kicad_pcb`
  attached (refused locally above 20 MB — Gmail's cap is 25 MB for the whole
  encoded message).
- `--schedule` creates a Calendar event titled after the board with a Meet
  link (`conferenceData.createRequest`) and your `--attendee` list — **only
  when the adversarial review found blockers**. A clean review prints
  "no blockers — no review event was created" and schedules nothing.

Delivery failures are independent: a rejected card does not stop the email,
and every failure is reported with the exit code reflecting it.

## Security properties, enforced by test

- Tokens, client secret, and the webhook URL are never printed or logged;
  displays are masked to a short tail.
- The token file is mode 0600, re-asserted on every rewrite.
- The PKCE `code_verifier` exists only in process memory.
- An exact-match host allowlist (`oauth2.googleapis.com`,
  `gmail.googleapis.com`, `www.googleapis.com`, `chat.googleapis.com`) is
  checked before any request is handed to the transport — a suffix-spoofed
  host like `chat.googleapis.com.evil.example` is refused.
- No credential exists anywhere in the code; everything arrives via the
  environment.
