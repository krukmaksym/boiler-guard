# boiler-guard

A Cloudflare Python Worker that switches a Shelly smart plug (here: a bathroom
boiler) **off when a Deye hybrid inverter loses grid**, and back on when grid
returns, so the boiler never drains the battery during an outage.

It runs on a one-minute cron trigger, has no public URL, and uses only the Python
standard library plus Cloudflare's Workers SDK.

## How it works

Every minute:

1. Poll Deye Cloud `/v1.0/device/latest` and read `GridVoltageL1L2`.
   - If Deye is unreachable, the inverter is offline, or the reading is older
     than `MAX_DATA_AGE_SECONDS`, do nothing. The plug keeps its current state.
   - Only the grid key is used: `LoadVoltage*`/`ACVoltage*` are the inverter's own
     output and stay at ~230 V on battery.
2. Compute the desired plug state with hysteresis:
   - `>= ON_VOLTAGE` (210 V) → on
   - `< OFF_VOLTAGE` (205 V) → off
   - in between → keep the previous desired state
3. If the desired state is unchanged since the last run, stop. No Shelly call.
4. Otherwise poll Shelly Cloud `/device/status`, and send `/device/relay/control`
   only if the plug's real state differs. Then save the new desired state in KV.
   If Shelly is offline, nothing is saved, so the next run retries.

The Deye bearer token is cached in KV and refreshed automatically when it
expires or is rejected.

## Security

- **Use a read-only Deye user.** Create a dedicated Deye Cloud user with view-only
  access to your station, and create the developer app under that user. Deye then
  refuses settings changes for these credentials (`auth no operation permission`).
- **Deye is read-only in code too**: every Deye call goes through `logic.deye_url`,
  which allows only `/v1.0/account/token` and `/v1.0/device/latest` on
  `https://*.deyecloud.com`. All 18 endpoints in Deye's API that change settings,
  stations or accounts (`/order/*`, `/strategy/*`, logger and station management)
  are refused before any request is made, with a test for each.
- Credentials are **Worker Secrets** (encrypted, write-only, scoped to this Worker).
  KV (`STATE`) holds only the derived Deye token, a login back-off flag, the
  `on`/`off` state, the admin's manual-off flag and Telegram bookkeeping.
- The only HTTP surface is the optional Telegram webhook (see below). Without
  `TELEGRAM_WEBHOOK_SECRET` every request gets a 404; preview URLs are off.
- The Shelly auth key is sent in a POST body (never a URL) and only to `*.shelly.cloud`.
  Ideally use a Shelly account that holds only this plug: the key can't be scoped.
- Outbound requests time out after 15 s and never follow redirects.
- A failed Deye login backs off for 15 minutes (no account lockout from retry loops).
- Secrets are never logged. `.prod.vars`, `.dev.vars` and `.wrangler/` are git-ignored,
  and a pre-commit hook refuses commits containing secret files or real secret values.
- wrangler is pinned in `package-lock.json`; scripts use `npx --no-install`.

## Setup

Requirements: Node.js, Python 3.12+, a Cloudflare account, a Deye Cloud developer
app, and a Shelly plug on Shelly Cloud.

```bash
npm ci                                                  # pinned wrangler
python3 -m venv .venv && .venv/bin/pip install "pip>=25.1" && .venv/bin/pip install --group dev
git config core.hooksPath scripts/hooks                 # secret-blocking pre-commit hook
cp .prod.vars.example .prod.vars                        # then fill it in
```

`SHA_256_PASSWORD` is the SHA-256 hex digest of the Deye user's password, as the
Deye API expects.

## Deploy

```bash
npx --no-install wrangler login
npx --no-install wrangler kv namespace create STATE     # put the id in wrangler.toml
scripts/deploy.sh .prod.vars                            # code + secrets as one version
npx --no-install wrangler tail boiler-guard             # watch the minute-by-minute logs
```

To change only secrets later: `scripts/upload-secrets.sh .prod.vars` (also seeds
`BEARER_TOKEN` into KV if set).

Settings that aren't secret (thresholds, voltage and backup-load keys, data age, Shelly channel)
are in `[vars]` in `wrangler.toml`.

### CI/CD

`.github/workflows/ci.yml` runs on every pull request and every push to `main`:

| Job | Checks |
|---|---|
| Lint | `ruff check`, `ruff format --check`, `shellcheck`, `actionlint` |
| Unit tests | `unittest` on Python 3.12 and 3.13 |
| Secret scan | `scripts/hooks/pre-commit --all` over every tracked file |
| Build | `npm audit --audit-level=high`, `pywrangler deploy --dry-run` |
| Deploy | only on push to `main`, only after all of the above pass |

The deploy job ships code and `[vars]` only. Worker Secrets are set once from your
machine (`scripts/deploy.sh` / `scripts/upload-secrets.sh`) and are never touched by
deploys, so GitHub holds no Deye or Shelly credentials, just a Cloudflare API token.

One-time setup, in a GitHub environment named `production`:

- Secret `CLOUDFLARE_API_TOKEN`: a Cloudflare API token scoped to your account with
  **Workers Scripts: Edit**.
- Variable `CLOUDFLARE_ACCOUNT_ID`: your Cloudflare account ID.

Actions are pinned to commit SHAs; Dependabot proposes updates as PRs.

### Telegram notifications (optional)

The Worker can message a Telegram chat (texts are in Ukrainian) when:

- grid is lost and the boiler is switched off, and when grid returns and it's back on
  (saying how long the outage lasted);
- grid voltage drops below `OFF_VOLTAGE` (e.g. 204.2 V) and the boiler is switched off,
  and when it recovers to `ON_VOLTAGE`, with how long it lasted (below 100 V counts as an
  outage, not low voltage);
- Deye has given no trustworthy data for 5 minutes (protection paused), with the likely
  cause (Deye unreachable, inverter offline, stale upload, ...), and on recovery with how
  long the gap lasted. Every such run also logs the exact cause, e.g.
  `no trustworthy grid reading (last upload 4 min ago)`;
- the plug can't be switched because Shelly is unreachable;
- a run fails with an error (repeated at most hourly), and when it works again.

Each event is sent once, not every minute. Messages go out only after the plug logic
has run, with a 10 s timeout; a Telegram failure is logged and never affects the
boiler. Only `https://api.telegram.org` is contacted, through an allowlist of Bot API
methods, so the Worker still has no public URL.

Setup:

1. Create a bot with [@BotFather](https://t.me/BotFather) and add it to your chat
   (in a group, send `/start@<botname>` there once).
2. Put `TELEGRAM_BOT_TOKEN` in `.prod.vars`, then find the chat ID:
   `python3 -I scripts/telegram_chat_id.py` and add `TELEGRAM_CHAT_ID` too.
3. Upload: `scripts/upload-secrets.sh .prod.vars`.

For local tests, `--var NOTIFY_PREFIX:"🧪 ТЕСТ: "` marks messages as tests.

#### Asking the bot: `/status`

Send `/status` in the chat and the bot replies on its next run (within a minute) with
grid voltage, the boiler's real on/off state, its live power draw, battery charge,
the load on the inverter's backup output (`BACKUP_LOAD_KEY`, default `UPSLoadPower`)
and whether protection is active. `/start` or `/help` explain what it does.

The power line tells whether the boiler is actually heating: above 20 W it reads
"гріє воду", below it "вода нагріта" (the plug is on but the boiler's thermostat is
idle).

#### Keyboard under the input field (no command text in the chat)

In a group, commands show up as `/status@your_bot`. Instead, post a persistent
keyboard once; it stays under the input field for everyone in the chat:

```bash
python3 -I scripts/telegram_menu.py
```

- **📊 Покажи поточний статус**: status, for everyone in the chat.
- **🎛 Покажи панель керування**: the control panel, for admins only.

A press posts the button's text as the user's own message, and the bot answers it as
a reply. **The bot needs group privacy mode off** (@BotFather → `/setprivacy` →
Disable, then remove and re-add the bot to the group): with privacy on, Telegram
delivers only the first press after the keyboard is posted. With privacy off the bot
receives every message in the chat; the Worker acts only on commands and the exact
button texts, and stores or logs nothing else. The texts only ask for information;
the panel's on/off buttons keep their admin check and 10-minute expiry. Run the
script again only if the keyboard changes or someone hides it.

There is no webhook: each run fetches new messages with `getUpdates`, so the Worker
still has no public URL. Only the configured `TELEGRAM_CHAT_ID` is answered (anyone
else messaging the bot is ignored), and `/status` can't change anything. Shelly is
polled for the reply only when someone asks.

#### Controlling the boiler: `/boiler` (admin only)

`/boiler` replies with the current state and two buttons:

- **⛔ Вимкнути**: switches the boiler off and *holds* it off. Even when grid returns
  it stays off (the grid message says so) until the admin presses Увімкнути.
- **🔌 Увімкнути**: returns to automatic control. With grid present the boiler is
  switched on right away. On battery, or without fresh Deye data, it is **never**
  switched on from the bot; it comes on by itself when grid returns.

Each press is applied on the next run (within a minute) and confirmed in the chat.
Guards:

- Only Telegram user IDs in the `TELEGRAM_ADMIN_IDS` secret (comma-separated) can
  press the buttons. Telegram authenticates user IDs, so a leaked bot token can't
  impersonate the admin. Anyone else sending `/boiler` or pressing a panel button gets
  a reply with their own Telegram ID, which is also how you find yours.
- Presses aren't acknowledged with `answerCallbackQuery`: Telegram accepts that only
  within seconds, and a cron run sees the press up to a minute later. The button's
  spinner stops by itself, and the confirmation arrives as a chat message.
- Buttons expire 3 minutes after the panel was sent (1 minute in webhook mode), so
  a press stuck in Telegram's queue or an old panel in the chat history can't act
  later, and nobody acts on an out-of-date status.
- Updates are confirmed before they're handled, so a failing run never replays a press.
- A manual off can't be lost to a race with the cron: right before switching on, the
  cron re-reads the mode, and for 3 minutes after ⛔ it checks the plug really stayed
  off (and switches it off again if not). Outside that window the cron still touches
  Shelly only when grid changes.
- The manual mode is saved only after the plug is confirmed in the right state; if
  Shelly doesn't answer, nothing changes and the chat is told.
- Deye stays read-only: the buttons only drive the Shelly plug.

Setup: send `/boiler` in the chat, put the ID from the reply into `.prod.vars` as
`TELEGRAM_ADMIN_IDS`, and run `scripts/upload-secrets.sh .prod.vars`.

Register the command menu once: `python3 -I scripts/telegram_commands.py`.
The keyboard above is the friendlier alternative to typing commands.

#### Instant replies: Telegram webhook (optional)

By default the bot polls Telegram on each cron run, so replies take up to a minute.
In webhook mode Telegram POSTs every message and button press to the Worker, which
answers within a few seconds: status is read from Deye and Shelly on demand, and
button presses are acknowledged (the spinner stops; refusals show as a toast).

Every request must pass all of these before the Worker reads its body, or it gets
the same plain 404 (`src/webhook.py`):

- the `X-Telegram-Bot-Api-Secret-Token` header equals `TELEGRAM_WEBHOOK_SECRET`
  (32+ random characters, compared in constant time);
- the path, `/telegram/<hash of the secret>`, which nobody can guess without it;
- `POST` with a JSON body of at most 64 KB;
- the client IP is in Telegram's webhook ranges (`149.154.160.0/20`, `91.108.4.0/22`).

After that, the same chat and admin checks as in polling mode apply. Status and
the panel are each answered at most once per 10 s (they read Deye and Shelly), Telegram delivers one update at a time
(`max_connections=1`), an update is never handled twice (`update_id` is
remembered), and the Worker always answers 200 so Telegram doesn't retry an action.
The Worker can't change its own webhook: `setWebhook` is only in the setup script's
allowlist.

Switch on (order matters):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # put it in .prod.vars as TELEGRAM_WEBHOOK_SECRET
scripts/upload-secrets.sh .prod.vars                            # the Worker stops polling
python3 -I scripts/telegram_webhook.py set https://boiler-guard.<subdomain>.workers.dev
python3 -I scripts/telegram_webhook.py info                     # host, pending updates, last error
```

Switch back: `python3 -I scripts/telegram_webhook.py delete`, then remove the secret
from the Worker (`npx --no-install wrangler secret delete TELEGRAM_WEBHOOK_SECRET`)
and from `.prod.vars`.

### Test environment: boiler-guard-test

A second Worker, wired to a test bot and a test group, for verifying a change in
the real runtime before it reaches the family chat. It exists only while you test:

```bash
cp .test.vars.example .test.vars      # test bot, test group, own webhook secret; same Deye/Shelly
scripts/test-env.sh up 2              # deploy this checkout for 2 h, point the test bot at it
scripts/test-env.sh status
scripts/test-env.sh down              # remove the webhook and delete the Worker
```

- `up` stamps `EXPIRES_AT`; after it the Worker does nothing at all (no cron work,
  404 to every request), even if `down` is forgotten. The test environment refuses
  to run without an expiry.
- `down` deletes the Worker, so nothing is left to run or answer. Its KV namespace
  stays for next time.
- `test-env.sh` refuses to run if `.test.vars` shares the bot token, chat or webhook
  secret with `.prod.vars`, so it can never take over the production bot.
- It reads the real inverter and switches the **real plug**, like production.
- CI never deploys it; it's deployed from your checkout.

### Alerts

Cloudflare custom alerts (beta) can watch the Workers events dataset: alert when
`outcome` is not `ok` for script `boiler-guard`. That covers exceptions and
CPU-limit kills. A log-based alert can't detect the cron not running at all.

## Run locally

Hits the real Deye and Shelly APIs; KV is simulated locally. `.dev.vars` is
generated from `.test.vars` (test bot and test group), never from `.prod.vars`, so a
local run can't post to the family chat or race production for Telegram updates.
It leaves out `TELEGRAM_WEBHOOK_SECRET`: Telegram can't reach a local Worker, so it
polls. Local runs are where the fake Deye works (outages, low voltage, data gaps);
use `boiler-guard-test` for anything that needs the webhook.

```bash
python3 -I scripts/parse_env.py .test.vars dev-vars .dev.vars
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pywrangler dev --test-scheduled
```

Then trigger one cron run from another terminal:

```bash
curl -X POST "http://localhost:8787/cdn-cgi/local/explorer/api/local/scheduled?worker=boiler-guard" -H 'Content-Type: application/json' -d '{"cron":"* * * * *"}'
```

### Simulate a grid outage

Fakes Deye locally while Shelly stays real, so **the plug really switches**.

```bash
python3 -I scripts/mock_deye.py   # local Deye mock on :8790
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pywrangler dev --var DEYE_BASE_URL:http://localhost:8790
curl "http://localhost:8790/set?v=0"     # grid lost, then trigger a cron run
curl "http://localhost:8790/set?v=230"   # grid back, then trigger a cron run
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Limitations

- Cloud-dependent: if your internet goes down together with the grid, the Worker
  can't reach Shelly.
- Deye Cloud data can lag by a few minutes, so switching isn't instant.

## License

[MIT](LICENSE)
