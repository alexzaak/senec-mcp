# cli-anything-senec

Agent-native CLI for a **SENEC.Home** battery storage system. Talks to the
device's own web-UI backend (`POST /lala.cgi`) — the same API the browser GUI uses.

## Requirements
- A reachable SENEC device (default `https://senec.home.arpa`, self-signed TLS accepted)
- devbox (Python 3.12 + uv are provisioned by `devbox.json`)

## Install
```bash
cd agent-harness
devbox run -- uv venv .venv   # once
devbox run install            # uv pip install -e .[mcp] pytest
devbox run -- which cli-anything-senec
```

## Usage
```bash
cli-anything-senec                       # REPL (default)
cli-anything-senec status                # power flow, SOC, state
cli-anything-senec --json battery info   # per-module SOC/SOH/cycles
cli-anything-senec battery cells         # cell mV + temps
cli-anything-senec pv info | grid info | system info | system temps | system states
cli-anything-senec raw sections | raw get ENERGY STAT_STATE | raw decode fl_42C80000
cli-anything-senec snapshot capture -o a.json      # "project" file
cli-anything-senec snapshot diff a.json b.json --section ENERGY
cli-anything-senec snapshot export a.json -o a.csv
cli-anything-senec monitor sample -n 60 -i 10 -o log.csv
cli-anything-senec control safe-charge on --dry-run   # writes need --yes
cli-anything-senec session set-host https://192.168.1.50 ; session undo
```
Host precedence: `--host` > `$SENEC_HOST` > session (`~/.cli-anything-senec/session.json`,
override path with `--session-file` / `$CLI_ANYTHING_SENEC_SESSION`).

## Cloud (mein-senec.de)
Read-only access to the SENEC cloud (same SSO + REST API as the SENEC app):
history from day one, cloud-side status, hardware details, booked packages.

Credentials (precedence): `--user/--password` > env `SENEC_USER`/`SENEC_MAIL`,
`SENEC_PASSWORD` > `./.env` (or `--env-file`; `SENEC-MAIL=` is accepted too).
Optional 2FA: `--totp-secret`/`SENEC_TOTP_SECRET` (base32) or `cloud login --otp 123456`.
Tokens are cached in `~/.cli-anything-senec/cloud_token.json` (mode 0600,
override `--token-file`/`$SENEC_CLOUD_TOKEN_FILE`) and refreshed automatically
(access 5 min, refresh 30 days; re-login from credentials if both expire).

```bash
cli-anything-senec cloud login                 # SSO login, caches token
cli-anything-senec cloud whoami                # token state (offline)
cli-anything-senec cloud systems               # systems on the account
cli-anything-senec cloud use 123456            # default system (session; 'auto' clears)
cli-anything-senec cloud dashboard             # now (W) + today (kWh), autarky
cli-anything-senec cloud details [--full]      # firmware, modules, warranty, installer
cli-anything-senec cloud status | cloud abilities | cloud availability
cli-anything-senec cloud history -r HOUR --from today          # table + totals
cli-anything-senec --json cloud history -r DAY --from 30d -o month.csv --totals-only
cli-anything-senec cloud history -r MONTH --from 2025-01-01 --to 2026-01-01
cli-anything-senec cloud compare               # local lala.cgi vs cloud values
cli-anything-senec cloud logout
```
Resolutions: `FIVE_MINUTES QUARTER_HOUR HOUR DAY WEEK MONTH YEAR`. Times: ISO,
`now`/`today`/`yesterday`, or relative `30m 6h 7d 2w`. History columns: `pv_kwh,
house_kwh, grid_import_kwh, grid_export_kwh, battery_charge_kwh,
battery_discharge_kwh, battery_level_percent, autarky_percent, wallbox_kwh`.
Cloud data lags the device by a few minutes.

### Cloud settings (writes)
Settings live on the mein-senec.de portal (cookie login with the same credentials).
Writes follow the `control` safeguards: without `--yes` the planned request is
printed and the exit code is 2; `--dry-run` always just prints. Values are
validated before any request; the current value is read first and read back after
the write (`old`, `new`, `read_back`, `verified`).

```bash
cli-anything-senec cloud settings list                     # support status + current values
cli-anything-senec cloud settings reserve                  # emergency-power reserve %
cli-anything-senec cloud settings set-reserve 20           # plan only (exit 2)
cli-anything-senec cloud settings set-reserve 20 --yes     # write, range 0..50
cli-anything-senec cloud settings peak-shaving             # mode / capacity limit / end
cli-anything-senec cloud settings set-peak-shaving --mode MANUAL --capacity 60 --end 18:00 --yes
```
Peak-shaving modes: `DEACTIVATED`, `MANUAL`, `AUTO`; capacity 0..100 %; `--end HH:MM`
(next occurrence) or ISO; omitted fields keep their current value.
Not offered for this account (see `cloud settings list`): SG-ready, wallbox,
sockets, heating rod, maintenance charge, off-peak charging (portal flags false),
forecast charging (app API answers HTTP 400).

## Setup mode (installer login on the device)
The device's "Setup" area requires the installer login. Credentials come from
`--user/--password`, the environment, or `./.env` (`--env-file`):
```bash
export SENEC_INSTALL_USERNAME=installateur
export SENEC_INSTALL_PASSWORD=...
```
```bash
cli-anything-senec setup login      # enter setup mode (verifies LOG.USER_LEVEL >= 2)
cli-anything-senec setup status     # role / level / failed attempts / session expiry
cli-anything-senec raw get ENERGY   # now also returns installer-only variables
cli-anything-senec setup logout     # leave setup mode, drop the GUI cookie
```
The login is bound to the device's `senec-homev3-gui` cookie (valid 1 h), stored per
host in `~/.cli-anything-senec/device_cookies.json` (0600; override with
`CLI_ANYTHING_SENEC_COOKIES`) and sent with every device request, so all commands
(`raw get`, `control set`, …) run with installer rights while logged in. The device
blocks logins after 5 failed attempts; `setup login` refuses to try at that point.
Always `setup logout` when done — an active session can block the web UI.

### Test charge / discharge ("Testleistung (W)", Laden / Entladen)
Requires `setup login`. Writes need `--yes` **and the owner's consent**: without it the
plan is printed and the exit code is 2; `--dry-run` always just prints.
```bash
cli-anything-senec setup test-status                         # running? mode, W, state, battery W
cli-anything-senec setup test-charge 500 --dry-run           # show request
cli-anything-senec setup test-charge 500 --duration 30 --yes # run 30 s, then auto-stop (Ctrl-C stops too)
cli-anything-senec setup test-discharge 500 --min-soc 20 --duration 60 --yes
cli-anything-senec setup test-stop                           # stop (no --yes needed)
```
Watts: positive integer, capped at the live hardware limits `FACTORY.MAX_CHARGE_POWER_DC`
(2500 W) / `MAX_DISCHARGE_POWER_DC` (3750 W). Discharge is refused at/below `--min-soc`
(default 10 %). Without `--duration` the test runs until `setup test-stop` — the device
has no timeout. The battery reacts with a delay (~30 s in the live test).

Exit codes: 0 ok, 1 error (JSON `{"error": ...}` with `--json`), 2 write not applied (missing `--yes`).

Preview: not applicable (numeric data only).

## MCP server
`senec-mcp` exposes the harness as an MCP server (stdio, official `mcp` SDK / FastMCP).
It calls the core modules directly (no CLI subprocess). Installed by `devbox run install`
(extra `[mcp]`, pinned `mcp<2`).

```bash
.venv/bin/senec-mcp [--host URL] [--env-file /abs/path/.env] [--session-file F] [--token-file F]
```
Config: device host `--host` > `$SENEC_HOST` > session > `https://senec.home.arpa`
(self-signed TLS accepted); cloud credentials from env (`SENEC_USER`/`SENEC_MAIL`,
`SENEC_PASSWORD`, `SENEC_TOTP_SECRET`) or the `.env` given by `--env-file` / `$SENEC_ENV_FILE`
(use an absolute path, the server may run from any cwd). `$SENEC_CLOUD_SYSTEM` selects a system.

Tools:
- Read: `status`, `battery_info`, `battery_cells`, `pv`, `grid`, `system_info`, `system_temps`,
  `raw_get(section, vars)` (read-only), `cloud_dashboard`, `cloud_history(resolution, from_, to,
  totals_only)`, `cloud_availability`, `cloud_details`, `cloud_settings_list`, `cloud_reserve`,
  `cloud_peak_shaving`
- Write (`confirm=false` default → dry-run plan, nothing changed; `confirm=true` → execute,
  read back, return old/new/verified): `safe_charge(mode on|off)`, `set_reserve(percent 0..50)`,
  `set_peak_shaving(mode, capacity, end)`
- Deliberately **not** exposed: installer setup login, test charge/discharge, raw `control set`,
  `LI_STORAGE_MODE`.

OpenCode (global `~/.config/opencode/opencode.jsonc`, V2 schema):
```jsonc
"mcp": { "servers": { "senec": {
  "type": "local",
  "command": ["/abs/agent-harness/.venv/bin/senec-mcp"],
  "environment": { "SENEC_ENV_FILE": "/abs/agent-harness/.env" }
} } }
```

## Tests
```bash
devbox run test             # unit + live E2E (needs device + cloud credentials in .env)
devbox run test-installed   # force installed command
```
