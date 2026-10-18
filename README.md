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
  KV (`STATE`) holds only the derived Deye token, a login back-off flag and the
  `on`/`off` state.
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
