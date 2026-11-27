# TEST.md — senec

## Test Inventory Plan
- `test_core.py`: ~35 unit tests (synthetic data, no network)
- `test_full_e2e.py`: ~20 E2E tests against the **real device** (`SENEC_LOCAL_HOST`,
  default `https://senec.home.arpa`) incl. subprocess tests of the installed CLI
- Refine 1 (cloud): +22 unit tests in `test_core.py`, +21 E2E tests against the real
  mein-senec.de SSO/API in `test_full_e2e.py`
- Refine 2 (cloud settings): +14 unit tests (request building / validation), +7 E2E
  (reads, guards, ONE no-op write: reserve := current value)
- Refine 3 (installer setup mode): +20 unit tests (cookie store, credential
  resolution, login/logout state machine with a fake device), +3 E2E (real login/logout)
- Refine 4 (test charge/discharge): +21 unit tests (request building, caps, SOC guard,
  auto-stop / Ctrl-C / error stop, stop retry, stale-cookie recovery), +5 E2E (status,
  guards, dry-run, idle stop, login required). E2E never starts a test
- Refine 5 (MCP server): `test_mcp.py` +19 tests — 18 offline (exact tool list, no forbidden
  tools, `confirm` default false + warning text on write tools, dry-runs make no writes,
  confirm path writes + verifies with a fake portal, validation errors, unreachable device
  message, host/env-file config) and 1 E2E (spawns `senec-mcp` over stdio with the MCP
  client, lists tools, calls live `status` and a dry-run `safe_charge`; no live writes)

## Unit Test Plan
| Module | Tested | Edge cases |
|---|---|---|
| `utils/senec_backend.py` | `decode_value` for fl/u8/u1/u3/u6/i8/i1/i3/st, lists, error markers; `encode_value` round-trip; `value_type`; `parse_state_names` | negative signed values, malformed value, unknown type, negative unsigned, unreachable host → `SenecError` |
| `core/device.py` | `summarize_status`, `summarize_battery`, `summarize_cells`, `summarize_pv`, `summarize_grid`, `summarize_system`, `summarize_temps`, `decode_section` (END_OF_ARRAY stripping) | grid import vs export sign, unknown state code |
| `core/project.py` | `build_snapshot`, `save`/`load` round-trip, `info`, `flatten`, `diff` | overwrite protection, wrong format, tolerance filtering |
| `core/export.py` | `export_snapshot` csv/json, `write_samples`, `sample` (mocked fetch), `stats` | bad format, count < 1 |
| `core/control.py` | `plan_set` type inference/explicit, `plan_safe_charge` | uninferrable type, bad mode |
| `core/session.py` | defaults, `set_config` normalisation, undo/redo, persistence + locking | unknown key, empty undo/redo, bad timeout |
| `utils/senec_cloud.py` *(refine 1)* | `.env` parsing, credential precedence, RFC 6238 TOTP vector, PKCE, login-form / error parsing, token stamping/state, JWT claims, 0600 token file, `iso_utc` | `SENEC-MAIL` key, missing env file, no token + no creds, missing credentials |
| `core/cloud.py` *(refine 2)* | `plan_reserve`, `plan_peak_shaving`, `parse_end_time`, `summarize_peak_shaving`, `settings_overview`, `verify`; `WebClient` credential check | reserve -1/51/12.5/abc/None, capacity 101, bad mode, past MANUAL end, missing end, 25:00, failed probe |
| `utils/senec_backend.py` *(refine 3)* | `parse_set_cookie`, `store_cookie`/`get_cookie`/`clear_cookie` (per host, 0600) | expiry, host case/trailing slash, no Max-Age, corrupt file |
| `core/installer.py` *(refine 3)* | `resolve_credentials`, `plan_login`, `summarize_log`, `login`, `logout` (fake device) | password truncation (23), missing creds, already logged in, wrong password (no retry), ignored login → resend, other session timeout, ≥5 failed attempts → refuse |
| `core/testpower.py` *(refine 4)* | `parse_watts`, `plan_start`/`plan_stop` (same requests as the UI), `summarize`, `run_for`, `stop`, `apply`, `get_status`, `require_login` | 0/negative/fractional W, 2501/3751 W, SOC == min, unknown SOC, other mode running, Ctrl-C, error mid-run, FORBIDDEN response, not logged in |
| `core/cloud.py` *(refine 1)* | `parse_time`, `select_system`, `summarize_systems/details/dashboard`, `measurement_rows/totals`, `write_measurements`, `compare`, `token_info`, session `cloud_system` | unknown system, empty payloads, unknown series, overwrite, bad format, `auto` reset |

## E2E Test Plan (real SENEC device)
- Live reads: status, battery info/cells, pv, grid, system info/temps, state table, raw sections/get.
- Plausibility checks: SOC 0–100, grid voltages 180–260 V, frequency 49–51 Hz,
  cell voltages 2500–4300 mV, `house ≈ pv + grid − battery` energy balance (± tolerance).
- Snapshot capture → file valid JSON with `format` marker, every raw value decodes.
- Snapshot export CSV/JSON → parse back, row count matches flattened values.
- Monitor sampling → CSV with header + N rows.
- Writes: only **dry-run** (no state change on a production battery).
- **Cloud (refine 1, real sso.senec.com + app API, creds from `.env`)**: real login + token refresh,
  systems/details/status/abilities, dashboard freshness (< 1 h), measurements at 5 resolutions,
  sum of today's HOUR rows ≈ dashboard `today.house_kwh`, cloud vs local SOC within 15 %.

## Realistic Workflow Scenarios
1. **Daily health check** — status → battery info → cells → temps; verify SOH, cell spread, temps in range.
2. **Before/after diff** — capture snapshot A, wait, capture B, diff; verify changes are numeric deltas & `ENERGY` present.
3. **Energy logging** — monitor sample 3×1 s → CSV; verify rows/columns parse and stats bounds.
4. **Device migration** — session set-host → undo → redo; verify persistence across subprocess invocations.
5. **Agent pipeline** — every subprocess command with `--json` parses as JSON.
6. **Cloud reporting** *(refine 1)* — `cloud use ID` → `cloud history -r day --from 7d -o hist.csv --totals-only`; verify CSV rows = intervals, totals > 0.
7. **Cloud auth lifecycle** — `cloud login --env-file` → `logout` → `whoami` = missing; unauthenticated call fails with guidance.
9. **Settings safety** *(refine 2)* — `settings list` → `set-reserve <other>` without `--yes` (exit 2) → `--dry-run` → out-of-range rejected → value unchanged; then `set-reserve <current> --yes` (no-op) → old = new = read_back, verified.
10. **Setup mode** *(refine 3)* — `setup status` (off) → `raw get ENERGY` → `setup login --env-file .env` → `status` in a new process (cookie persisted) → second login = `already_logged_in` → `raw get ENERGY` shows more variables → `setup logout` → status off → history records login/logout.
11. **Test power safety** *(refine 4)*: login → `test-status` idle → `test-charge 500` (exit 2) → `--dry-run` → over-cap and SOC guard rejected → still idle → `test-stop` → logout (teardown always stops + logs out).
8. **Local vs cloud** — `cloud compare` combines `status` (lala.cgi) with the cloud dashboard.

## Test Results

Command: `devbox run test` (`pytest -v --tb=no`) against the live device https://senec.home.arpa
and the live mein-senec.de cloud/portal (credentials from `.env`).

```
============================= test session starts ==============================
platform darwin -- Python 3.12.16, pytest-9.2.0, pluggy-1.7.0 -- <repo>/.venv/bin/python
cachedir: .pytest_cache
rootdir: <repo>
collecting ... collected 196 items

senec_mcp/tests/test_core.py::TestCodec::test_float PASSED      [  0%]
senec_mcp/tests/test_core.py::TestCodec::test_float_negative PASSED [  1%]
senec_mcp/tests/test_core.py::TestCodec::test_unsigned PASSED   [  1%]
senec_mcp/tests/test_core.py::TestCodec::test_signed PASSED     [  2%]
senec_mcp/tests/test_core.py::TestCodec::test_string PASSED     [  2%]
senec_mcp/tests/test_core.py::TestCodec::test_list_and_dict PASSED [  3%]
senec_mcp/tests/test_core.py::TestCodec::test_error_markers PASSED [  3%]
senec_mcp/tests/test_core.py::TestCodec::test_malformed PASSED  [  4%]
senec_mcp/tests/test_core.py::TestCodec::test_roundtrip[u8-255] PASSED [  4%]
senec_mcp/tests/test_core.py::TestCodec::test_roundtrip[u1-3162] PASSED [  5%]
senec_mcp/tests/test_core.py::TestCodec::test_roundtrip[u3-123456] PASSED [  5%]
senec_mcp/tests/test_core.py::TestCodec::test_roundtrip[i8--5] PASSED [  6%]
senec_mcp/tests/test_core.py::TestCodec::test_roundtrip[i1--300] PASSED [  6%]
senec_mcp/tests/test_core.py::TestCodec::test_roundtrip[i3--70000] PASSED [  7%]
senec_mcp/tests/test_core.py::TestCodec::test_roundtrip[fl-12.5] PASSED [  7%]
senec_mcp/tests/test_core.py::TestCodec::test_roundtrip[st-abc] PASSED [  8%]
senec_mcp/tests/test_core.py::TestCodec::test_encode_format PASSED [  8%]
senec_mcp/tests/test_core.py::TestCodec::test_encode_negative_unsigned PASSED [  9%]
senec_mcp/tests/test_core.py::TestCodec::test_value_type PASSED [  9%]
senec_mcp/tests/test_core.py::TestCodec::test_parse_state_names PASSED [ 10%]
senec_mcp/tests/test_core.py::TestCodec::test_unreachable PASSED [ 10%]
senec_mcp/tests/test_core.py::TestDevice::test_status PASSED    [ 11%]
senec_mcp/tests/test_core.py::TestDevice::test_status_unknown_state PASSED [ 11%]
senec_mcp/tests/test_core.py::TestDevice::test_decode_section_strips_end_of_array PASSED [ 12%]
senec_mcp/tests/test_core.py::TestDevice::test_battery PASSED   [ 12%]
senec_mcp/tests/test_core.py::TestDevice::test_cells PASSED     [ 13%]
senec_mcp/tests/test_core.py::TestDevice::test_pv PASSED        [ 13%]
senec_mcp/tests/test_core.py::TestDevice::test_grid PASSED      [ 14%]
senec_mcp/tests/test_core.py::TestDevice::test_system PASSED    [ 14%]
senec_mcp/tests/test_core.py::TestDevice::test_temps PASSED     [ 15%]
senec_mcp/tests/test_core.py::TestProject::test_build PASSED    [ 15%]
senec_mcp/tests/test_core.py::TestProject::test_save_load PASSED [ 16%]
senec_mcp/tests/test_core.py::TestProject::test_overwrite_protection PASSED [ 16%]
senec_mcp/tests/test_core.py::TestProject::test_load_wrong_format PASSED [ 17%]
senec_mcp/tests/test_core.py::TestProject::test_info PASSED     [ 17%]
senec_mcp/tests/test_core.py::TestProject::test_flatten PASSED  [ 18%]
senec_mcp/tests/test_core.py::TestProject::test_diff PASSED     [ 18%]
senec_mcp/tests/test_core.py::TestProject::test_diff_tolerance PASSED [ 19%]
senec_mcp/tests/test_core.py::TestExport::test_csv_json PASSED  [ 19%]
senec_mcp/tests/test_core.py::TestExport::test_bad_format PASSED [ 20%]
senec_mcp/tests/test_core.py::TestExport::test_sample_mocked PASSED [ 20%]
senec_mcp/tests/test_core.py::TestExport::test_sample_bad_count PASSED [ 21%]
senec_mcp/tests/test_core.py::TestControl::test_plan_set_infer PASSED [ 21%]
senec_mcp/tests/test_core.py::TestControl::test_plan_set_explicit PASSED [ 22%]
senec_mcp/tests/test_core.py::TestControl::test_plan_set_uninferrable PASSED [ 22%]
senec_mcp/tests/test_core.py::TestControl::test_safe_charge PASSED [ 23%]
senec_mcp/tests/test_core.py::TestSession::test_defaults PASSED [ 23%]
senec_mcp/tests/test_core.py::TestSession::test_set_normalizes PASSED [ 24%]
senec_mcp/tests/test_core.py::TestSession::test_invalid PASSED  [ 25%]
senec_mcp/tests/test_core.py::TestSession::test_undo_redo_persist PASSED [ 25%]
senec_mcp/tests/test_core.py::TestSession::test_locked_save_shrinks PASSED [ 26%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_parse_env_file PASSED [ 26%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_resolve_credentials_precedence PASSED [ 27%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_totp_rfc6238 PASSED [ 27%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_pkce PASSED [ 28%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_parse_login_form PASSED [ 28%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_token_state_and_stamp PASSED [ 29%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_jwt_claims PASSED [ 29%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_token_file_roundtrip_private PASSED [ 30%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_client_without_token_or_creds PASSED [ 30%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_login_requires_credentials PASSED [ 31%]
senec_mcp/tests/test_core.py::TestCloudBackend::test_iso_utc PASSED [ 31%]
senec_mcp/tests/test_core.py::TestCloudCore::test_parse_time PASSED [ 32%]
senec_mcp/tests/test_core.py::TestCloudCore::test_select_system PASSED [ 32%]
senec_mcp/tests/test_core.py::TestCloudCore::test_summarize_systems PASSED [ 33%]
senec_mcp/tests/test_core.py::TestCloudCore::test_summarize_details PASSED [ 33%]
senec_mcp/tests/test_core.py::TestCloudCore::test_summarize_dashboard PASSED [ 34%]
senec_mcp/tests/test_core.py::TestCloudCore::test_measurement_rows_and_totals PASSED [ 34%]
senec_mcp/tests/test_core.py::TestCloudCore::test_write_measurements PASSED [ 35%]
senec_mcp/tests/test_core.py::TestCloudCore::test_compare PASSED [ 35%]
senec_mcp/tests/test_core.py::TestCloudCore::test_token_info PASSED [ 36%]
senec_mcp/tests/test_core.py::TestCloudCore::test_session_cloud_system PASSED [ 36%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_plan_reserve PASSED [ 37%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_plan_reserve_invalid[-1] PASSED [ 37%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_plan_reserve_invalid[51] PASSED [ 38%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_plan_reserve_invalid[12.5] PASSED [ 38%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_plan_reserve_invalid[abc] PASSED [ 39%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_plan_reserve_invalid[None] PASSED [ 39%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_plan_peak_keeps_current PASSED [ 40%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_plan_peak_manual PASSED [ 40%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_plan_peak_invalid PASSED [ 41%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_parse_end_time PASSED [ 41%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_summarize_peak PASSED [ 42%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_settings_overview PASSED [ 42%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_verify PASSED [ 43%]
senec_mcp/tests/test_core.py::TestCloudSettings::test_web_client_requires_credentials PASSED [ 43%]
senec_mcp/tests/test_core.py::TestCookieStore::test_parse_set_cookie PASSED [ 44%]
senec_mcp/tests/test_core.py::TestCookieStore::test_store_get_clear PASSED [ 44%]
senec_mcp/tests/test_core.py::TestCookieStore::test_no_expiry PASSED [ 45%]
senec_mcp/tests/test_core.py::TestCookieStore::test_corrupt_file PASSED [ 45%]
senec_mcp/tests/test_core.py::TestInstaller::test_resolve_credentials_precedence PASSED [ 46%]
senec_mcp/tests/test_core.py::TestInstaller::test_plan_login PASSED [ 46%]
senec_mcp/tests/test_core.py::TestInstaller::test_summarize_log[None-none-False-False] PASSED [ 47%]
senec_mcp/tests/test_core.py::TestInstaller::test_summarize_log[0-none-False-False] PASSED [ 47%]
senec_mcp/tests/test_core.py::TestInstaller::test_summarize_log[2-installer-True-False] PASSED [ 48%]
senec_mcp/tests/test_core.py::TestInstaller::test_summarize_log[4-professional-True-False] PASSED [ 48%]
senec_mcp/tests/test_core.py::TestInstaller::test_summarize_log[16-superuser-True-True] PASSED [ 49%]
senec_mcp/tests/test_core.py::TestInstaller::test_summarize_log[3-level_3-True-False] PASSED [ 50%]
senec_mcp/tests/test_core.py::TestInstaller::test_summarize_cookie_expiry PASSED [ 50%]
senec_mcp/tests/test_core.py::TestInstaller::test_login_success PASSED [ 51%]
senec_mcp/tests/test_core.py::TestInstaller::test_login_already PASSED [ 51%]
senec_mcp/tests/test_core.py::TestInstaller::test_login_wrong_password PASSED [ 52%]
senec_mcp/tests/test_core.py::TestInstaller::test_login_resends_when_ignored PASSED [ 52%]
senec_mcp/tests/test_core.py::TestInstaller::test_login_timeout_other_session PASSED [ 53%]
senec_mcp/tests/test_core.py::TestInstaller::test_login_blocked PASSED [ 53%]
senec_mcp/tests/test_core.py::TestInstaller::test_logout_clears_cookie PASSED [ 54%]
senec_mcp/tests/test_core.py::TestTestPower::test_parse_watts PASSED [ 54%]
senec_mcp/tests/test_core.py::TestTestPower::test_charge_request_matches_ui PASSED [ 55%]
senec_mcp/tests/test_core.py::TestTestPower::test_discharge_request_positive_offset PASSED [ 55%]
senec_mcp/tests/test_core.py::TestTestPower::test_500w_encoding PASSED [ 56%]
senec_mcp/tests/test_core.py::TestTestPower::test_hardware_caps PASSED [ 56%]
senec_mcp/tests/test_core.py::TestTestPower::test_soc_guard[10.0-10.0-False] PASSED [ 57%]
senec_mcp/tests/test_core.py::TestTestPower::test_soc_guard[5.0-10.0-False] PASSED [ 57%]
senec_mcp/tests/test_core.py::TestTestPower::test_soc_guard[10.1-10.0-True] PASSED [ 58%]
senec_mcp/tests/test_core.py::TestTestPower::test_soc_guard[5.0-4.0-True] PASSED [ 58%]
senec_mcp/tests/test_core.py::TestTestPower::test_soc_guard_not_for_charge_and_unknown_soc PASSED [ 59%]
senec_mcp/tests/test_core.py::TestTestPower::test_other_mode_running PASSED [ 59%]
senec_mcp/tests/test_core.py::TestTestPower::test_plan_stop PASSED [ 60%]
senec_mcp/tests/test_core.py::TestTestPower::test_summarize PASSED [ 60%]
senec_mcp/tests/test_core.py::TestTestPower::test_run_for_auto_stop PASSED [ 61%]
senec_mcp/tests/test_core.py::TestTestPower::test_run_for_ctrl_c_stops PASSED [ 61%]
senec_mcp/tests/test_core.py::TestTestPower::test_run_for_error_still_stops PASSED [ 62%]
senec_mcp/tests/test_core.py::TestTestPower::test_stop_retries PASSED [ 62%]
senec_mcp/tests/test_core.py::TestTestPower::test_apply_rejected PASSED [ 63%]
senec_mcp/tests/test_core.py::TestTestPower::test_status_requires_login PASSED [ 63%]
senec_mcp/tests/test_core.py::TestTestPower::test_require_login PASSED [ 64%]
senec_mcp/tests/test_core.py::TestEnsureCookie::test_stale_session_recovery PASSED [ 64%]
senec_mcp/tests/test_full_e2e.py::TestLiveReads::test_status_plausible PASSED [ 65%]
senec_mcp/tests/test_full_e2e.py::TestLiveReads::test_battery PASSED [ 65%]
senec_mcp/tests/test_full_e2e.py::TestLiveReads::test_cells PASSED [ 66%]
senec_mcp/tests/test_full_e2e.py::TestLiveReads::test_pv PASSED [ 66%]
senec_mcp/tests/test_full_e2e.py::TestLiveReads::test_grid PASSED [ 67%]
senec_mcp/tests/test_full_e2e.py::TestLiveReads::test_system_and_temps PASSED [ 67%]
senec_mcp/tests/test_full_e2e.py::TestLiveReads::test_state_names_from_device PASSED [ 68%]
senec_mcp/tests/test_full_e2e.py::TestLiveReads::test_raw PASSED [ 68%]
senec_mcp/tests/test_full_e2e.py::TestLiveReads::test_every_raw_value_decodes PASSED [ 69%]
senec_mcp/tests/test_full_e2e.py::TestSnapshotWorkflow::test_capture_export_roundtrip PASSED [ 69%]
senec_mcp/tests/test_full_e2e.py::TestSnapshotWorkflow::test_before_after_diff PASSED [ 70%]
senec_mcp/tests/test_full_e2e.py::TestSnapshotWorkflow::test_monitor_csv PASSED [ 70%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_help PASSED [ 71%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_status_human_and_json PASSED [ 71%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd0] PASSED [ 72%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd1] PASSED [ 72%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd2] PASSED [ 73%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd3] PASSED [ 73%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd4] PASSED [ 74%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd5] PASSED [ 75%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd6] PASSED [ 75%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd7] PASSED [ 76%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd8] PASSED [ 76%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd9] PASSED [ 77%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd10] PASSED [ 77%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd11] PASSED [ 78%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_json_commands[cmd12] PASSED [ 78%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_full_snapshot_workflow PASSED [ 79%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_monitor PASSED [ 79%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_control_is_guarded PASSED [ 80%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_session_undo_redo PASSED [ 80%]
senec_mcp/tests/test_full_e2e.py::TestCLISubprocess::test_unreachable_host_fails_loudly PASSED [ 81%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_token PASSED [ 81%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_refresh PASSED [ 82%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_systems PASSED [ 82%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_details_status_abilities PASSED [ 83%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_dashboard_plausible PASSED [ 83%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_measurements[FIVE_MINUTES-2h] PASSED [ 84%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_measurements[HOUR-1d] PASSED [ 84%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_measurements[DAY-7d] PASSED [ 85%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_measurements[MONTH-60d] PASSED [ 85%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_hour_sum_matches_dashboard_today PASSED [ 86%]
senec_mcp/tests/test_full_e2e.py::TestCloudLive::test_cloud_vs_local_soc PASSED [ 86%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_json_commands[cmd0] PASSED [ 87%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_json_commands[cmd1] PASSED [ 87%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_json_commands[cmd2] PASSED [ 88%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_json_commands[cmd3] PASSED [ 88%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_json_commands[cmd4] PASSED [ 89%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_json_commands[cmd5] PASSED [ 89%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_json_commands[cmd6] PASSED [ 90%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_login_with_env_file PASSED [ 90%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_use_and_history_export PASSED [ 91%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_compare PASSED [ 91%]
senec_mcp/tests/test_full_e2e.py::TestCloudCLISubprocess::test_errors PASSED [ 92%]
senec_mcp/tests/test_full_e2e.py::TestCloudSettingsLive::test_plant_mapping PASSED [ 92%]
senec_mcp/tests/test_full_e2e.py::TestCloudSettingsLive::test_reserve_read PASSED [ 93%]
senec_mcp/tests/test_full_e2e.py::TestCloudSettingsLive::test_peak_read PASSED [ 93%]
senec_mcp/tests/test_full_e2e.py::TestCloudSettingsLive::test_noop_peak_plan_unchanged PASSED [ 94%]
senec_mcp/tests/test_full_e2e.py::TestCloudSettingsCLI::test_list PASSED [ 94%]
senec_mcp/tests/test_full_e2e.py::TestCloudSettingsCLI::test_guards_and_validation PASSED [ 95%]
senec_mcp/tests/test_full_e2e.py::TestCloudSettingsCLI::test_noop_write_roundtrip_reserve PASSED [ 95%]
senec_mcp/tests/test_full_e2e.py::TestSetupModeCLI::test_login_workflow PASSED [ 96%]
senec_mcp/tests/test_full_e2e.py::TestSetupModeCLI::test_missing_credentials PASSED [ 96%]
senec_mcp/tests/test_full_e2e.py::TestSetupModeCLI::test_human_output PASSED [ 97%]
senec_mcp/tests/test_full_e2e.py::TestTestPowerCLI::test_status_idle PASSED [ 97%]
senec_mcp/tests/test_full_e2e.py::TestTestPowerCLI::test_charge_guards PASSED [ 98%]
senec_mcp/tests/test_full_e2e.py::TestTestPowerCLI::test_discharge_dry_run_and_soc_guard PASSED [ 98%]
senec_mcp/tests/test_full_e2e.py::TestTestPowerCLI::test_stop_is_safe_when_idle PASSED [ 99%]
senec_mcp/tests/test_full_e2e.py::test_testpower_requires_login PASSED [100%]

=============================== warnings summary ===============================
senec_mcp/tests/test_full_e2e.py::TestTestPowerCLI::test_status_idle
  <repo>/.venv/lib/python3.12/site-packages/_pytest/fixtures.py:1392: PytestRemovedIn10Warning: Class-scoped fixtures defined as instance methods are deprecated.
  Instance attributes set in the '_login' fixture will NOT be visible to test methods,
  as each test gets a new instance while the fixture runs only once per class.
  Use a @classmethod decorator below @pytest.fixture and set attributes on cls instead.
  See https://docs.pytest.org/en/stable/deprecations.html#class-scoped-fixture-as-instance-method
    fixturefunc = resolve_fixture_function(fixturedef, request)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
================== 196 passed, 1 warning in 105.94s (0:01:45) ==================
```

### Summary
- 196 tests, 196 passed (100%), ~105 s: 83 original + 43 refine 1 + 21 refine 2 + 23 refine 3 + 26 refine 4 (21 unit, 5 E2E)
- Refine 5 (MCP): `devbox run test` → **215 passed** in ~108 s (196 + 19 in `test_mcp.py`: 18 unit, 1 stdio E2E)

### Coverage Notes
- MCP server: confirm=true is exercised only against a fake portal client; the live E2E calls `status` and a dry-run `safe_charge` only.
- Device writes (`control ... --yes`) are not executed by tests (production battery); only plan/dry-run/guard paths and value encoding are tested.
- REPL is exercised manually (piped stdin), not by pytest.
- Wallbox/sockets only checked for parseable output (no wallbox installed).
- Cloud is read-only; the 2FA (OTP) login branch is unit-tested (TOTP vector, form parsing) but not live (account has no 2FA).
- Setup mode: real installer login/logout on the device each run (teardown always logs out); no installer-only variable is written.
- Test charge/discharge: automated tests never start a test. One manual live charge (500 W, 30 s) is documented in SENEC.md; discharge only offline/dry-run (SOC 1 %).
- Cloud settings: the only live write is a no-op (emergency reserve set to its current value); all other write paths are tested via dry-run, missing `--yes` and validation errors, and the value is asserted unchanged afterwards.
