---
name: "senec"
description: "Read live data (SOC, PV, grid, battery cells, temps, system info) from a SENEC.Home storage system and history/dashboard/system details from the mein-senec.de cloud (incl. guarded emergency-reserve / peak-shaving settings), capture/diff/export snapshots, log time series, and perform guarded writes via its lala.cgi API."
---

# senec

Install: `devbox run install`. Device default: `https://senec.home.arpa`
(override `--host URL` or `$SENEC_LOCAL_HOST`).

Always pass `--json` (global, before the subcommand) for machine output.

| Group | Commands |
|---|---|
| `status` | live: state, soc_percent, pv_w, house_w, grid_w (+import/−export), battery_w (+charge/−discharge) |
| `battery` | `info`, `cells` |
| `pv` / `grid` / `wallbox` | `info` |
| `system` | `info`, `temps`, `states` |
| `raw` | `sections`, `get SECTION [VAR…] [--undecoded]`, `decode VALUE`, `encode TYPE VALUE` |
| `snapshot` | `capture -o F [-s SECTION…] [--overwrite]`, `info [F]`, `diff A B [--section S] [--tolerance X]`, `export F -o OUT -f csv/json` |
| `monitor` | `sample -n N -i SEC [-o FILE -f csv/json]` |
| `control` | `safe-charge on/off`, `set SECTION VAR VALUE [--type]` — require `--yes`, use `--dry-run` first |
| `setup` | Installer setup mode on the device: `login [--user U --password P --env-file F --wait S]` (default creds `SENEC_INSTALL_USERNAME`/`SENEC_INSTALL_PASSWORD` from env or `.env`), `status`, `logout`, `test-status`, `test-charge W`, `test-discharge W [--min-soc PCT]` (`--duration S` auto-stops, `--dry-run`, `--yes`), `test-stop`. While logged in, all device commands use installer rights (more variables in `raw get`). Always `setup logout` afterwards. |
| `session` | `show`, `set KEY VAL` (host, verify_tls, timeout, lang, cloud_system), `set-host`, `ping`, `history`, `undo`, `redo` |
| `cloud` | mein-senec.de (read-only): `login [--otp]`, `logout`, `whoami`, `systems`, `use ID`, `details [--full]`, `status`, `abilities`, `dashboard`, `availability`, `history -r RES --from T --to T [-o F -f csv/json --totals-only]`, `compare`, `settings list`, `settings reserve`, `settings set-reserve PCT`, `settings peak-shaving`, `settings set-peak-shaving [--mode DEACTIVATED/MANUAL/AUTO] [--capacity PCT] [--end HH:MM]` (writes need `--yes`, `--dry-run` first) |

Cloud: group options go before the subcommand (`cloud --system ID --env-file F history …`).
Credentials from `SENEC_USER`/`SENEC_MAIL` + `SENEC_PASSWORD` (env or `./.env`); token cached
and auto-refreshed. If you get "Not logged in", run `cloud login`. Resolutions:
FIVE_MINUTES, QUARTER_HOUR, HOUR, DAY, WEEK, MONTH, YEAR; times ISO / now / today /
yesterday / 6h / 7d / 2w. Use `cloud history` for past energy (kWh) and `status` for
real-time local values (cloud lags a few minutes).

Agent guidance:
- Errors: exit 1 and `{"error": "..."}`; unapplied write: exit 2 with the plan.
- Never run `setup test-charge/test-discharge --yes` without explicit user consent (moves real power); prefer `--duration`, and always `setup test-stop` + `setup logout` afterwards.
- Never run `control ... --yes` or `cloud settings set-* --yes` without explicit user consent — they change a live battery. Reserve range 0..50 %, peak-shaving capacity 0..100 %.
- Value encoding: `fl_` float32 hex, `u8/u1/u3/u6_` unsigned, `i8/i1/i3_` signed, `st_` string.

Example: `senec --json status` → `{"state":"LADEN","soc_percent":3.0,"pv_w":3884.0,...}`
