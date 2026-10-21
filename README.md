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
- No public URL (`workers_dev = false`, `preview_urls = false`); `fetch` returns 404.
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

Settings that aren't secret (thresholds, voltage key, data age, Shelly channel)
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
- Deye has given no trustworthy data for 15 minutes (protection paused), with the likely
  cause (Deye unreachable, inverter offline, stale upload, ...), and on recovery with how
  long the gap lasted. Every such run also logs the exact cause, e.g.
  `no trustworthy grid reading (last upload 18 min ago)`;
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
grid voltage, the boiler's real on/off state, its live power draw, battery charge and
whether protection is active. `/start` or `/help` explain what it does.

The power line tells whether the boiler is actually heating: above 20 W it reads
"гріє воду", below it "вода нагріта" (the plug is on but the boiler's thermostat is
idle).

#### Pinned menu (no command text in the chat)

In a group, commands show up as `/status@your_bot`. To avoid that, post a menu message
with **📊 Статус** and **🎛 Керування** buttons once and pin it:

```bash
python3 -I scripts/telegram_menu.py
```

Pressing a button sends nothing into the chat; the bot's reply names who asked
("на запит: Олена"). 📊 Статус works for everyone in the chat, 🎛 Керування opens the
control panel for admins only. Button presses reach the bot even with Telegram's
privacy mode on, so the bot still doesn't see the family's other messages. The menu
buttons don't expire (they only ask for information); the panel's on/off buttons do.

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
  impersonate the admin. Anyone else sending `/boiler` gets a reply with their own
  Telegram ID, which is also how you find yours.
- Buttons expire 10 minutes after the panel was sent, so a press stuck in
  Telegram's queue or an old panel in the chat history can't act later.
- Updates are confirmed before they're handled, so a failing run never replays a press.
- The manual mode is saved only after the plug is confirmed in the right state; if
  Shelly doesn't answer, nothing changes and the chat is told.
- Deye stays read-only: the buttons only drive the Shelly plug.

Setup: send `/boiler` in the chat, put the ID from the reply into `.prod.vars` as
`TELEGRAM_ADMIN_IDS`, and run `scripts/upload-secrets.sh .prod.vars`.

Register the command menu once: `python3 -I scripts/telegram_commands.py`.
The pinned menu above is the friendlier alternative to typing commands.

### Alerts

Cloudflare custom alerts (beta) can watch the Workers events dataset: alert when
`outcome` is not `ok` for script `boiler-guard`. That covers exceptions and
CPU-limit kills. A log-based alert can't detect the cron not running at all.

## Run locally

Hits the real Deye and Shelly APIs; KV is simulated locally.

```bash
python3 -I scripts/parse_env.py .prod.vars dev-vars .dev.vars
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
