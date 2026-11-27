# SENEC.Home — Protocol notes

Target: SENEC.Home battery storage web UI at `https://senec.home.arpa`
(self-signed TLS, no authentication for read access).

## Phase 1 — Analysis

### Backend engine
The web GUI is a static jQuery app (`/js/senec.min.js`). Every value shown in
the GUI is fetched from a single JSON-RPC-like endpoint:

```
POST /lala.cgi
{"ENERGY": {"GUI_BAT_DATA_FUEL_CHARGE": ""}, "BMS": {}}
```

* An empty object `{}` for a section returns **all** variables of that section.
* An empty string value requests a single variable.
* Supplying a typed value (e.g. `"u8_01"`) **writes** that variable.
* Unknown names return `VARIABLE_NOT_FOUND` / `OBJECT_NOT_FOUND`;
  protected values return `FORBIDDEN`.

### Data model — typed hex encoding (`Senec.jsonValUnpack` / `jsonValPack`)
| Prefix | Meaning |
|---|---|
| `fl_` | IEEE-754 float32 big-endian hex |
| `u8_`,`u1_`,`u3_`,`u6_` | unsigned 8/16/32/64-bit hex |
| `i8_`,`i1_`,`i3_` | signed 8/16/32-bit hex (two's complement) |
| `st_` | string |
| `ch_` | raw char |
| `er_` | error |

System state names (`ENERGY.STAT_STATE`) come from `system_state_name` in
`/js/<LANG>.js` (e.g. `/js/DE-de.js`); the CLI loads them from the device.

### GUI → API mapping
| GUI page | lala.cgi section/variables |
|---|---|
| Status / power flow | `ENERGY.GUI_*` (house, grid, inverter, battery power, SOC) , `STAT_STATE` |
| Battery / BMS | `BMS.*` (cell voltages/temps, cycles, SOH, current) |
| PV | `PV1.MPP_*`, `PV1.POWER_RATIO` |
| Grid meter | `PM1OBJ1.*` (per-phase P/U/I, freq) |
| Temperatures | `TEMPMEASURE.*`, `FAN_SPEED.*` |
| System info | `WIZARD.*` versions, `FACTORY.*`, `SYS_UPDATE.*`, `RTC.*` |
| Wallbox | `WALLBOX.*` |
| Safety charge (Sicherheitsladung) | write `ENERGY.SAFE_CHARGE_FORCE` / `SAFE_CHARGE_PROHIBIT` = `u8_01` |

### Existing CLI tools
None shipped. `curl` against `lala.cgi` is the "native CLI".

## Phase 2 — CLI design

`senec [--host URL] [--json] <group> <cmd>`

| Group | Commands |
|---|---|
| `status` | live summary (power flow, SOC, state) |
| `battery` | `info`, `cells` |
| `pv` | `info` |
| `grid` | `info` |
| `system` | `info`, `temps`, `states` |
| `wallbox` | `info` |
| `raw` | `sections`, `get SECTION [VAR…]`, `decode VALUE` |
| `snapshot` (project) | `capture -o`, `info`, `diff`, `export --format json/csv` |
| `monitor` | `sample -n -i -o csv` time series |
| `control` | `safe-charge on/off`, `set SECTION VAR VALUE` (all `--dry-run`/`--yes` guarded) |
| `session` | `show`, `set-host`, `history`, `undo`, `redo` |
| `repl` | interactive (default) |

State: `~/.senec/session.json` (host, TLS verify, history,
undo/redo stack of config changes), locked writes. Snapshots are the project
files: JSON with raw + decoded device data.

Preview: not applicable (no visual output; data is numeric).

## Refine 1 — Cloud (mein-senec.de)

### Backend
mein-senec.de and the SENEC app share a Keycloak SSO (`sso.senec.com`, realm
`senec`). The tool uses the app's public OIDC client
(`endcustomer-app-frontend`, redirect `senec-app-auth://keycloak.prod`,
scope `roles profile meinsenec`) with PKCE S256:

1. `GET /auth?...` → login form (username, then password on a 2nd page; optional `otp` page)
2. POST forms with a cookie jar, no redirect following, until `302 Location: senec-app-auth://…?code=`
3. `POST /token` (authorization_code + code_verifier) → access (300 s) + refresh (30 d) token
4. Refresh with `grant_type=refresh_token`; on failure re-login from stored credentials

Cloudflare blocks the default `Python-urllib` user agent (error 1010) → a browser UA is sent.
Everything stays stdlib (`urllib`, `http.cookiejar`, `hmac` for RFC 6238 TOTP).

### Endpoints (Bearer token)
| CLI | Endpoint |
|---|---|
| `cloud systems` | `senec-app-systems-proxy…/systems/api/v1` |
| `cloud details` | `…/systems/api/v1/{id}/details` |
| `cloud status` | `…/systems/api/v1/status/{id}` |
| `cloud abilities` | `senec-app-abilities-proxy…/abilities/api/v1/packages/{id}` |
| `cloud dashboard` | `senec-app-measurements-proxy…/measurements/api/v1/systems/{id}/dashboard` |
| `cloud availability` | `…/systems/{id}/data-availability/timespan?timezone=` |
| `cloud history` | `…/systems/{id}/measurements?resolution=&from=&to=` (all 7 resolutions verified) |

Measurement `values[]` align with the `measurements[]` name list (energies in kWh,
level/autarky in %); the core maps them to friendly column names and keeps unknown
future series (lower-cased).

### Coverage notes
- Read-only. Not wrapped (write endpoints, deliberately left out for a production
  system): wallbox mode/lock settings, peak-shaving / spare-capacity / SG-ready
  settings on mein-senec.de legacy `/endkunde/api/...` (session-cookie based).
  Candidates for a future refine with `--yes`/`--dry-run` guards.
- `online-state`, `forecast-charging-settings`, `wallboxes/search` returned 404/400
  for this account and are not exposed.
- Session gained `cloud_system` (undoable like other config keys).
- `cloud compare` composes the local `status` with the cloud dashboard.

## Refine 2 — Cloud settings

Settings are not in the app API for this account; they live on the legacy
**mein-senec.de portal** (`/endkunde/api/...`), authenticated by a session cookie:
`GET https://mein-senec.de` → SSO login form (client `meinsenec-login-portal`) →
POST username+password → redirects back with cookies. One login per CLI call.
The portal addresses systems by `anlageNummer` (0-based index); the tool maps the
app system's `controlUnitNumber` to it via `getAnlageBasedNavigationViewModel`,
whose `*Visible` flags tell which features exist.

| Setting | Read | Write | Validation |
|---|---|---|---|
| emergency reserve | `GET /senec/{n}/emergencypower/reserve-in-percent` (plain int) | `POST /senec/{n}/emergencypower?reserve-in-percent=X` | int 0..50 |
| peak shaving | `GET /peakshaving/getSettings?anlageNummer=n` | `POST /peakshaving/saveSettings?anlageNummer=n&mode=&capacityLimit=&endzeit=<ms>` | mode DEACTIVATED/MANUAL/AUTO, 0..100 %, end in future for MANUAL |

Note: the emergency reserve endpoint works although `emergencyPowerVisible=false`.

Write flow (`core/cloud.py` `plan_*` → CLI `_run_cloud_plan`): validate offline →
read current → plan (`request`, `old`, `new`, `changed`) → without `--yes` print and
exit 2 / `--dry-run` print and exit 0 → write → read back → `verified`. Recorded in
session history.

Skipped (reported by `cloud settings list`): SG-ready (`sgReadyVisible=false`, config
endpoint returns no settings), wallbox (`wallboxVisible=false`, no wallbox ids),
sockets, heating rod, maintenance charge, off-peak (flags false), forecast charging
(app `forecast-charging-settings` → HTTP 400).

## Refine 3 — Installer setup mode (local device)

GUI source: `login.html` + `senec.min.js` (`login()`, `logout()`, `isLoggedIn()`).
Login = one lala.cgi POST to the `LOG` section:
`{"LOG":{"USER_LEVEL":"","USERNAME":"st_<user>","LOG_IN_NOK_COUNT":"","PASSWORD":"st_<pw>","LOG_IN_BUTT":"u8_01"}}`
(user ≤ 80, password ≤ 23 chars); logout = `{"LOG":{"LOG_IN_NOK_COUNT":"","LOG_OUT_BUTT":"u8_01"}}`.

Findings on the live device (NPU 2608):
- The session is bound to the cookie `senec-homev3-gui` (Max-Age 3600). It is handed
  out on the first request and **rotated** on login/logout; requests without it never
  see a login. The backend (`utils/senec_backend._http`) therefore persists every
  `Set-Cookie` per host (`device_cookies.json`, 0600) and replays it.
- The write-only vars answer `FORBIDDEN` even when the login succeeds — success is
  verified by polling `LOG.USER_LEVEL` (`VARIABLE_NOT_FOUND` when logged out).
- Levels: 2 installer, 4 professional, 8/16 superuser (`isSuperUser`).
- `LOG_IN_NOK_COUNT` ≥ 5 blocks logins; `setup login` checks it first.
- Setup mode exposes extra variables (e.g. ENERGY 38 → 44, BMS 62 → 64, SYS_UPDATE 7 → 9).

Commands: `setup login | status | logout` (`core/installer.py`). Credentials:
`--user/--password` > env > `.env` (`SENEC_INSTALL_USERNAME`, `SENEC_INSTALL_PASSWORD`).
Login/logout are recorded in the session history.

## Refine 4 — Installer test charge / discharge

GUI source: `ksetup1.html` (setup page, block `.autorisiert`, installer only):
input `#itestpower` ("Testleistung (W)"), `ChargebtnClickEvent()` ("Laden"),
`DischargebtnClickEvent()` ("Entladen"), `sendTestPower(isCharge)`.

| Action | lala.cgi request (section ENERGY) |
|---|---|
| Laden P W | `GRID_POWER_OFFSET = [fl(-P/3)]*3` + `TEST_CHARGE_ENABLE = u8_01` |
| Entladen P W | `GRID_POWER_OFFSET = [fl(+P/3)]*3` + `TEST_CHARGE_ENABLE = u8_01` |
| Stop (click active button again) | `TEST_CHARGE_ENABLE = u8_00` + `GRID_POWER_OFFSET = [fl 0]*3` |
| Change power while running (Enter) | only `GRID_POWER_OFFSET` |
| State | `GUI_TEST_CHARGE_STAT`, `GUI_TEST_DISCHARGE_STAT` (1 = running) |

Mechanism: the test fakes a per-phase offset on the grid meter so the controller
sees export (charge) or import (discharge). These variables are only visible
(`VARIABLE_NOT_FOUND` otherwise) with an installer login.
UI limits: only "valid float > 0" (`lSetupTestPowerError`). No timeout, no stop on
page unload; stop-on-logout was not verified, so the tool always stops explicitly.
The CLI adds: integer W, cap at live `FACTORY.MAX_CHARGE_POWER_DC` (2500) /
`MAX_DISCHARGE_POWER_DC` (3750), SOC guard for discharge (default 10 %), refuse
switching modes while running, `--duration` with stop in `finally` (also Ctrl-C/errors,
3 stop retries).

Live test (SOC 1 %, state AKKU LEER, 500 W charge, 30 s): device accepted,
offset −500 W, `GUI_TEST_CHARGE_STAT=1` throughout. Battery stayed 0 W for ~27 s,
then at 31.6 s state LADEN, battery +767 W (with PV 2432 W). After stop: test off,
offset 0, battery 66 W → 0 W, state AKKU LEER (normal for 1 % SOC).

Also: when the device hands out no `senec-homev3-gui` cookie (stale GUI session),
`setup login` now sends a logout, waits 5 s and asks again (same as the HA integration).
