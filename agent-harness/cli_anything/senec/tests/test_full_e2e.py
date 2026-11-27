"""E2E tests against the REAL SENEC device (no mocks, no skipping)."""

import csv
import json
import os
import subprocess
import sys

import pytest

from cli_anything.senec.core import device, export, project
from cli_anything.senec.utils import senec_backend as be

HOST = os.environ.get("SENEC_HOST", be.DEFAULT_HOST)
CONN = {"host": HOST, "verify": False, "timeout": 15.0}


@pytest.fixture
def tmp_dir(tmp_path):
    return str(tmp_path)


def _resolve_cli(name):
    """Resolve installed CLI command; falls back to python -m for dev.

    Set env CLI_ANYTHING_FORCE_INSTALLED=1 to require the installed command.
    """
    import shutil
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    module = name.replace("cli-anything-", "cli_anything.") + "." + name.split("-")[-1] + "_cli"
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m {module}")
    return [sys.executable, "-m", module]


class TestLiveReads:
    def test_status_plausible(self):
        s = device.get_status(CONN)
        assert 0 <= s["soc_percent"] <= 100
        assert isinstance(s["state"], str) and not s["state"].startswith("STATE_")
        balance = s["pv_w"] + s["grid_w"] - s["battery_w"]
        assert abs(balance - s["house_w"]) <= max(300, 0.15 * s["house_w"]), s
        print(f"\n  status: {s}")

    def test_battery(self):
        b = device.get_battery(CONN)
        assert b["module_count"] >= 1 and len(b["modules"]) == b["module_count"]
        for m in b["modules"]:
            assert 0 <= m["soh_percent"] <= 100 and m["cycles"] >= 0

    def test_cells(self):
        for mod in device.get_cells(CONN):
            active = [v for v in mod["cell_mv"] if v]
            assert active and all(2500 <= v <= 4300 for v in active)
            assert mod["spread_mv"] < 500

    def test_pv(self):
        p = device.get_pv(CONN)
        assert p["strings"] and all(s["power_w"] >= 0 for s in p["strings"])
        assert p["inverters"]

    def test_grid(self):
        g = device.get_grid(CONN)
        assert 49.0 <= g["frequency_hz"] <= 51.0
        assert len(g["phases"]) == 3
        assert all(180 <= ph["voltage_v"] <= 260 for ph in g["phases"])

    def test_system_and_temps(self):
        s = device.get_system(CONN)
        assert s["device_id"] and s["firmware_version"] and s["mac_address"].count(":") == 5
        t = device.get_temps(CONN)
        assert -20 < t["battery_c"] < 70 and -20 < t["case_c"] < 80

    def test_state_names_from_device(self):
        names = device.state_names(CONN)
        assert len(names) > 50 and 14 in names

    def test_raw(self):
        secs = {s["section"]: s for s in device.list_sections(CONN)}
        assert secs["ENERGY"]["available"] and secs["ENERGY"]["variables"] > 10
        v = device.raw_get("energy", ["stat_state"], CONN)
        assert isinstance(v["STAT_STATE"], int)
        with pytest.raises(be.SenecError):
            device.raw_get("ENERGY", ["NO_SUCH_VAR"], CONN)

    def test_every_raw_value_decodes(self):
        raw = be.read_sections(project.DEFAULT_SECTIONS, **CONN)
        decoded = device.decode_all(raw)
        assert set(decoded) == set(project.DEFAULT_SECTIONS)


class TestSnapshotWorkflow:
    def test_capture_export_roundtrip(self, tmp_dir):
        snap = project.capture(CONN)
        p = os.path.join(tmp_dir, "snap.json")
        project.save(snap, p)
        loaded = project.load(p)
        assert loaded["format"] == project.FORMAT and "ENERGY" in loaded["decoded"]
        csv_p = export.export_snapshot(loaded, os.path.join(tmp_dir, "snap.csv"))["output"]
        rows = list(csv.reader(open(csv_p)))
        assert rows[0] == ["key", "value"]
        assert len(rows) - 1 == len(project.flatten(loaded["decoded"]))
        assert any(r[0] == "ENERGY.STAT_STATE" for r in rows)
        print(f"\n  snapshot: {p} ({os.path.getsize(p):,} bytes)\n  CSV: {csv_p}")

    def test_before_after_diff(self):
        a = project.capture(CONN, ["ENERGY", "PM1OBJ1"])
        b = project.capture(CONN, ["ENERGY", "PM1OBJ1"])
        d = project.diff(a, b)
        assert d["to"] >= d["from"]
        assert all("delta" in c or not isinstance(c["old"], (int, float)) for c in d["changes"])

    def test_monitor_csv(self, tmp_dir):
        samples = export.sample(CONN, count=3, interval=1)
        r = export.write_samples(samples, os.path.join(tmp_dir, "m.csv"))
        rows = list(csv.DictReader(open(r["output"])))
        assert len(rows) == 3 and all(0 <= float(x["soc_percent"]) <= 100 for x in rows)
        print(f"\n  monitor CSV: {r['output']}")


class TestCLISubprocess:
    CLI_BASE = _resolve_cli("cli-anything-senec")

    @pytest.fixture(autouse=True)
    def _env(self, tmp_dir):
        self.env = {**os.environ, "SENEC_HOST": HOST,
                    "CLI_ANYTHING_SENEC_SESSION": os.path.join(tmp_dir, "session.json")}

    def _run(self, args, check=True):
        return subprocess.run(self.CLI_BASE + args, capture_output=True, text=True,
                              check=check, env=self.env)

    def _json(self, args):
        return json.loads(self._run(["--json"] + args).stdout)

    def test_help(self):
        assert "SENEC" in self._run(["--help"]).stdout

    def test_status_human_and_json(self):
        assert "SOC:" in self._run(["status"]).stdout
        assert 0 <= self._json(["status"])["soc_percent"] <= 100

    @pytest.mark.parametrize("cmd", [["battery", "info"], ["battery", "cells"], ["pv", "info"],
                                     ["grid", "info"], ["system", "info"], ["system", "temps"],
                                     ["system", "states"], ["wallbox", "info"],
                                     ["raw", "sections"], ["raw", "get", "TEMPMEASURE"],
                                     ["raw", "decode", "fl_42C80000"], ["session", "ping"],
                                     ["session", "show"]])
    def test_json_commands(self, cmd):
        assert self._json(cmd) is not None

    def test_full_snapshot_workflow(self, tmp_dir):
        a, b = os.path.join(tmp_dir, "a.json"), os.path.join(tmp_dir, "b.json")
        self._json(["snapshot", "capture", "-o", a])
        self._json(["snapshot", "capture", "-o", b, "-s", "ENERGY", "-s", "BMS"])
        info = self._json(["snapshot", "info"])  # last captured from session
        assert info["path"] == b and "BMS" in info["sections"]
        d = self._json(["snapshot", "diff", a, b, "--section", "ENERGY"])
        assert all(c["key"].startswith("ENERGY.") for c in d["changes"])
        out = os.path.join(tmp_dir, "a.json.out")
        r = self._json(["snapshot", "export", a, "-o", out, "-f", "json"])
        assert json.load(open(r["output"]))["values"]["ENERGY.STAT_STATE"] >= 0
        err = self._run(["--json", "snapshot", "capture", "-o", a], check=False)
        assert err.returncode == 1 and "exists" in json.loads(err.stdout)["error"]

    def test_monitor(self, tmp_dir):
        out = os.path.join(tmp_dir, "m.csv")
        r = self._json(["monitor", "sample", "-n", "2", "-i", "0.5", "-o", out])
        assert r["samples"] == 2 and len(r["data"]) == 2
        assert open(out).read().startswith("timestamp,")

    def test_control_is_guarded(self):
        r = self._run(["--json", "control", "safe-charge", "on"], check=False)
        assert r.returncode == 2 and json.loads(r.stdout)["applied"] is False
        d = self._json(["control", "set", "ENERGY", "ZERO_EXPORT", "0", "--dry-run"])
        assert d["typed"] == "u8_00" and d["applied"] is False

    def test_session_undo_redo(self):
        self._json(["session", "set-host", "https://example.invalid"])
        assert self._json(["session", "undo"])["host"] != "https://example.invalid"
        assert self._json(["session", "redo"])["host"] == "https://example.invalid"
        hist = self._json(["session", "history"])
        assert [h["command"] for h in hist][-2:] == ["undo", "redo"]

    def test_unreachable_host_fails_loudly(self):
        r = self._run(["--json", "--host", "http://127.0.0.1:9", "status"], check=False)
        assert r.returncode == 1 and "Cannot reach" in json.loads(r.stdout)["error"]


# ── mein-senec.de cloud (real SSO + API; credentials from env or ./.env) ──

import datetime as _dt  # noqa: E402

from cli_anything.senec.core import cloud  # noqa: E402
from cli_anything.senec.utils import senec_cloud as sc  # noqa: E402

_HARNESS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
ENV_FILE = os.environ.get("SENEC_ENV_FILE", os.path.join(_HARNESS_DIR, ".env"))


@pytest.fixture(scope="module")
def cloud_token_file(tmp_path_factory):
    """One real login per module; token cached to a temp file."""
    creds = sc.resolve_credentials(env_file=ENV_FILE if os.path.exists(ENV_FILE) else None)
    assert creds["user"] and creds["password"], \
        "cloud E2E needs SENEC_USER/SENEC_PASSWORD (env or .env)"
    path = str(tmp_path_factory.mktemp("cloud") / "token.json")
    sc.save_token(sc.login(creds["user"], creds["password"], creds["totp_secret"]), path)
    return path


@pytest.fixture(scope="module")
def client(cloud_token_file):
    return sc.CloudClient(cloud_token_file)


@pytest.fixture(scope="module")
def system(client):
    return cloud.select_system(client.systems())


class TestCloudLive:
    def test_token(self, cloud_token_file):
        info = cloud.token_info(sc.load_token(cloud_token_file), cloud_token_file)
        assert info["state"] == "valid" and info["user"]
        assert os.stat(cloud_token_file).st_mode & 0o777 == 0o600

    def test_refresh(self, cloud_token_file):
        tok = sc.refresh(sc.load_token(cloud_token_file))
        assert sc.token_state(tok) == "valid"
        sc.save_token(tok, cloud_token_file)

    def test_systems(self, client, system):
        s = cloud.summarize_systems(client.systems())
        assert s and all(x["id"] for x in s) and system["id"] in [x["id"] for x in s]

    def test_details_status_abilities(self, client, system):
        d = cloud.summarize_details(client.details(system["id"]))
        assert d["system_id"] == system["id"] and d["firmware_version"]
        assert d["battery_modules"] == len(d["modules"]) >= 1
        st = client.status(system["id"])
        assert st["name"] == d["state"] or st["name"]
        assert isinstance(client.abilities(system["id"])["packageTypes"], list)

    def test_dashboard_plausible(self, client, system):
        d = cloud.summarize_dashboard(client.dashboard(system["id"]))
        assert 0 <= d["now"]["soc_percent"] <= 100
        assert d["today"]["house_kwh"] >= 0 and d["today"]["pv_kwh"] >= 0
        age = _dt.datetime.now(_dt.timezone.utc) - cloud.parse_time(d["timestamp"])
        assert age < _dt.timedelta(hours=1), f"cloud data stale: {age}"
        print(f"\n  cloud dashboard: {d}")

    @pytest.mark.parametrize("res,span", [("FIVE_MINUTES", "2h"), ("HOUR", "1d"),
                                          ("DAY", "7d"), ("MONTH", "60d")])
    def test_measurements(self, client, system, res, span):
        data = client.measurements(system["id"], res, cloud.parse_time(span),
                                   cloud.parse_time("now"))
        names, rows = cloud.measurement_rows(data)
        assert "pv_kwh" in names and "house_kwh" in names and rows
        assert all(r["pv_kwh"] >= 0 and 0 <= r["battery_level_percent"] <= 100 for r in rows)

    def test_hour_sum_matches_dashboard_today(self, client, system):
        now = cloud.parse_time("now")
        tz = _dt.timezone(_dt.timedelta(seconds=now.utcoffset().total_seconds()))
        today = cloud.parse_time("today").astimezone(tz)
        names, rows = cloud.measurement_rows(
            client.measurements(system["id"], "HOUR", today, now))
        tot = cloud.measurement_totals(names, rows)
        dash = cloud.summarize_dashboard(client.dashboard(system["id"]))
        assert abs(tot.get("house_kwh", 0) - dash["today"]["house_kwh"]) <= \
            max(1.0, 0.1 * dash["today"]["house_kwh"]), (tot, dash["today"])

    def test_cloud_vs_local_soc(self, client, system):
        c = cloud.compare(device.get_status(CONN), cloud.summarize_dashboard(
            client.dashboard(system["id"])))
        assert abs(c["fields"]["soc_percent"]["delta"]) <= 15, c


class TestCloudCLISubprocess:
    CLI_BASE = _resolve_cli("cli-anything-senec")

    @pytest.fixture(autouse=True)
    def _env(self, tmp_dir, cloud_token_file):
        self.env = {**os.environ, "SENEC_HOST": HOST,
                    "CLI_ANYTHING_SENEC_SESSION": os.path.join(tmp_dir, "session.json"),
                    "SENEC_CLOUD_TOKEN_FILE": cloud_token_file}
        self.env.pop("SENEC_USER", None)

    def _run(self, args, check=True):
        return subprocess.run(self.CLI_BASE + args, capture_output=True, text=True,
                              check=check, env=self.env, cwd=_HARNESS_DIR)

    def _json(self, args):
        return json.loads(self._run(["--json"] + args).stdout)

    @pytest.mark.parametrize("cmd", [["whoami"], ["systems"], ["details"], ["status"],
                                     ["abilities"], ["dashboard"], ["availability"]])
    def test_json_commands(self, cmd):
        assert self._json(["cloud"] + cmd) is not None

    def test_login_with_env_file(self, tmp_dir):
        tok = os.path.join(tmp_dir, "t.json")
        r = self._json(["cloud", "--token-file", tok, "--env-file", ENV_FILE, "login"])
        assert r["logged_in"] and r["state"] == "valid" and os.path.exists(tok)
        assert self._json(["cloud", "--token-file", tok, "logout"])["deleted"] is True
        assert self._json(["cloud", "--token-file", tok, "whoami"])["state"] == "missing"

    def test_use_and_history_export(self, tmp_dir):
        sid = self._json(["cloud", "systems"])[0]["id"]
        assert self._json(["cloud", "use", str(sid)])["cloud_system"] == str(sid)
        out = os.path.join(tmp_dir, "hist.csv")
        r = self._json(["cloud", "history", "-r", "day", "--from", "7d", "-o", out,
                        "--totals-only"])
        assert r["system_id"] == sid and r["file"]["rows"] == r["intervals"] >= 7
        assert "rows" not in r and r["totals"]["house_kwh"] > 0
        rows = list(csv.DictReader(open(out)))
        assert len(rows) == r["intervals"] and "pv_kwh" in rows[0]
        print(f"\n  cloud history CSV: {out}")
        assert "date" in self._run(["cloud", "history", "--from", "3h"]).stdout

    def test_compare(self):
        c = self._json(["cloud", "compare"])
        assert set(c["fields"]) >= {"soc_percent", "pv_w", "house_w"}

    def test_errors(self, tmp_dir):
        r = self._run(["--json", "cloud", "history", "--from", "soon"], check=False)
        assert r.returncode == 1 and "Bad time" in json.loads(r.stdout)["error"]
        r = self._run(["--json", "cloud", "--system", "999999999", "status"], check=False)
        assert r.returncode == 1 and "not found" in json.loads(r.stdout)["error"]
        r = subprocess.run(self.CLI_BASE + ["--json", "cloud", "--token-file",
                                            os.path.join(tmp_dir, "none.json"), "systems"],
                           capture_output=True, text=True, cwd=tmp_dir,
                           env={k: v for k, v in self.env.items()
                                if k not in ("SENEC_PASSWORD", "SENEC_USER")})
        assert r.returncode == 1 and "cloud login" in json.loads(r.stdout)["error"]


# ── cloud settings (refine 2): reads only + ONE no-op write (reserve := current) ──

@pytest.fixture(scope="module")
def web(system):
    creds = sc.resolve_credentials(env_file=ENV_FILE if os.path.exists(ENV_FILE) else None)
    w = sc.WebClient(creds, 30.0)
    n, plant = w.find_plant(system["controlUnitNumber"])
    return w, n, plant


class TestCloudSettingsLive:
    def test_plant_mapping(self, web, system):
        _, n, plant = web
        assert plant["master"] and n >= 0

    def test_reserve_read(self, web):
        w, n, _ = web
        lo, hi = cloud.RESERVE_RANGE
        assert lo <= w.get_reserve(n) <= hi

    def test_peak_read(self, web):
        w, n, _ = web
        s = cloud.summarize_peak_shaving(w.get_peak_shaving(n))
        assert s["mode"] in cloud.PEAK_SHAVING_MODES and 0 <= s["capacity_limit_percent"] <= 100

    def test_noop_peak_plan_unchanged(self, web, system):
        w, n, _ = web
        assert cloud.plan_peak_shaving(system["id"], n, w.get_peak_shaving(n))["changed"] is False


class TestCloudSettingsCLI:
    CLI_BASE = _resolve_cli("cli-anything-senec")

    @pytest.fixture(autouse=True)
    def _env(self, tmp_dir, cloud_token_file):
        self.env = {**os.environ, "CLI_ANYTHING_SENEC_SESSION": os.path.join(tmp_dir, "s.json"),
                    "SENEC_CLOUD_TOKEN_FILE": cloud_token_file}

    def _run(self, args):
        return subprocess.run(self.CLI_BASE + ["--json", "cloud"] + args, capture_output=True,
                              text=True, env=self.env, cwd=_HARNESS_DIR)

    def test_list(self):
        r = json.loads(self._run(["settings", "list"]).stdout)
        st = {s["setting"]: s["status"] for s in r["settings"]}
        assert st["emergency_reserve"] == "supported" and st["peak_shaving"] == "supported"

    def test_guards_and_validation(self):
        cur = json.loads(self._run(["settings", "reserve"]).stdout)["reserve_percent"]
        other = 0 if cur else 1   # a different value: must never be applied here
        r = self._run(["settings", "set-reserve", str(other)])
        d = json.loads(r.stdout)
        assert r.returncode == 2 and d["applied"] is False and d["changed"] is True
        r = self._run(["settings", "set-reserve", str(other), "--dry-run", "--yes"])
        assert r.returncode == 0 and json.loads(r.stdout)["note"] == "dry run"
        r = self._run(["settings", "set-reserve", "99", "--yes"])
        assert r.returncode == 1 and "out of range" in json.loads(r.stdout)["error"]
        r = self._run(["settings", "set-peak-shaving", "--capacity", "150", "--yes"])
        assert r.returncode == 1 and "out of range" in json.loads(r.stdout)["error"]
        r = self._run(["settings", "set-peak-shaving", "--mode", "MANUAL", "--capacity", "50",
                       "--end", "23:59"])
        assert r.returncode == 2 and json.loads(r.stdout)["applied"] is False
        assert json.loads(self._run(["settings", "reserve"]).stdout)["reserve_percent"] == cur

    def test_noop_write_roundtrip_reserve(self):
        """The ONLY live write: set reserve to its current value."""
        cur = json.loads(self._run(["settings", "reserve"]).stdout)["reserve_percent"]
        r = self._run(["settings", "set-reserve", str(cur), "--yes"])
        d = json.loads(r.stdout)
        assert r.returncode == 0 and d["applied"] and d["changed"] is False
        assert d["old"] == d["new"] == d["read_back"] == {"reserve_percent": cur}
        assert d["verified"] is True


# ── installer setup mode (refine 3): login → extra vars visible → logout ──

def _have_install_creds():
    from cli_anything.senec.core import installer
    cr = installer.resolve_credentials(env_file=ENV_FILE if os.path.exists(ENV_FILE) else None)
    return cr["user"] and cr["password"]


class TestSetupModeCLI:
    CLI_BASE = _resolve_cli("cli-anything-senec")

    @pytest.fixture(autouse=True)
    def _env(self, tmp_dir):
        assert _have_install_creds(), "SENEC_INSTALL_USERNAME/PASSWORD missing in env/.env"
        self.env = {**os.environ, "CLI_ANYTHING_SENEC_SESSION": os.path.join(tmp_dir, "s.json"),
                    "CLI_ANYTHING_SENEC_COOKIES": os.path.join(tmp_dir, "cookies.json")}
        yield
        self._run(["setup", "logout"])  # never leave the device in setup mode

    def _run(self, args, check=False):
        return subprocess.run(self.CLI_BASE + ["--json"] + args, capture_output=True,
                              text=True, env=self.env, cwd=_HARNESS_DIR, check=check)

    def _json(self, args):
        r = self._run(args)
        assert r.returncode == 0, r.stdout + r.stderr
        return json.loads(r.stdout)

    def _var_count(self, section):
        return len(self._json(["raw", "get", section]))

    def test_login_workflow(self):
        before = self._json(["setup", "status"])
        assert before["setup_mode"] is False and before["user_level"] == 0
        n_energy = self._var_count("ENERGY")
        r = self._json(["setup", "login", "--env-file", ENV_FILE])
        assert r["setup_mode"] and r["user_level"] >= 2 and r["failed_attempts"] == 0
        s = self._json(["setup", "status"])           # cookie persisted across processes
        assert s["setup_mode"] and s["cookie_expires_in_s"] > 3000
        assert self._json(["setup", "login", "--env-file", ENV_FILE])["already_logged_in"]
        more = self._var_count("ENERGY")
        print(f"\n  ENERGY vars: {n_energy} → {more} in setup mode")
        assert more > n_energy
        out = self._json(["setup", "logout"])
        assert out["setup_mode"] is False and out["cookie_cleared"]
        assert self._json(["setup", "status"])["setup_mode"] is False
        hist = self._json(["session", "history"])
        assert [h["command"] for h in hist][-2:] == ["setup login", "setup logout"]

    def test_missing_credentials(self, tmp_dir):
        empty = os.path.join(tmp_dir, "empty.env")
        open(empty, "w").write("")
        env = {k: v for k, v in self.env.items() if not k.startswith("SENEC_INSTALL")}
        r = subprocess.run(self.CLI_BASE + ["--json", "setup", "login", "--env-file", empty],
                           capture_output=True, text=True, env=env, cwd=tmp_dir)
        assert r.returncode == 1 and "SENEC_INSTALL_USERNAME" in json.loads(r.stdout)["error"]

    def test_human_output(self):
        r = subprocess.run(self.CLI_BASE + ["setup", "status"], capture_output=True,
                           text=True, env=self.env, cwd=_HARNESS_DIR)
        assert r.returncode == 0 and "Setup mode:" in r.stdout


# ── installer test charge/discharge (refine 4): guards + stop only, never starts ──

class TestTestPowerCLI:
    CLI_BASE = _resolve_cli("cli-anything-senec")

    @pytest.fixture(autouse=True, scope="class")
    @classmethod
    def _login(cls, tmp_path_factory):
        d = str(tmp_path_factory.mktemp("tp"))
        env = {**os.environ, "CLI_ANYTHING_SENEC_SESSION": os.path.join(d, "s.json"),
               "CLI_ANYTHING_SENEC_COOKIES": os.path.join(d, "cookies.json")}
        cls.env = env
        import time as _t
        for _ in range(3):
            r = cls._run(["setup", "login", "--env-file", ENV_FILE, "--wait", "30"])
            if r.returncode == 0:
                break
            _t.sleep(30)
        assert r.returncode == 0, r.stdout
        yield
        cls._run(["setup", "test-stop"])   # never leave a test running
        cls._run(["setup", "logout"])

    @classmethod
    def _run(cls, args):
        return subprocess.run(cls.CLI_BASE + ["--json"] + args, capture_output=True,
                              text=True, env=cls.env, cwd=_HARNESS_DIR)

    def _json(self, args, rc=0):
        r = self._run(args)
        assert r.returncode == rc, r.stdout + r.stderr
        return json.loads(r.stdout)

    def test_status_idle(self):
        s = self._json(["setup", "test-status"])
        assert s["running"] is False and s["test_power_w"] == 0 and 0 <= s["soc_percent"] <= 100

    def test_charge_guards(self):
        p = self._json(["setup", "test-charge", "500"], rc=2)
        assert p["applied"] is False and p["limit_w"] == 2500
        assert p["request"]["ENERGY"]["GRID_POWER_OFFSET"] == ["fl_C326AAAB"] * 3
        d = self._json(["setup", "test-charge", "500", "--dry-run", "--yes", "--duration", "30"])
        assert d["note"] == "dry run" and d["duration_s"] == 30
        assert "exceeds" in self._json(["setup", "test-charge", "2501", "--yes"], rc=1)["error"]
        assert "positive" in self._json(["setup", "test-charge", "0", "--yes"], rc=1)["error"]
        assert self._json(["setup", "test-status"])["running"] is False

    def test_discharge_dry_run_and_soc_guard(self):
        soc = self._json(["setup", "test-status"])["soc_percent"]
        r = self._run(["setup", "test-discharge", "500", "--dry-run", "--min-soc", "0"])
        if soc > 0:
            d = json.loads(r.stdout)
            assert r.returncode == 0 and d["applied"] is False and d["limit_w"] == 3750
        r = self._run(["setup", "test-discharge", "500", "--yes", "--min-soc", "100"])
        assert r.returncode == 1 and "at/below minimum" in json.loads(r.stdout)["error"]
        assert "exceeds" in self._json(["setup", "test-discharge", "3751", "--dry-run",
                                        "--min-soc", "0"], rc=1)["error"]
        assert self._json(["setup", "test-status"])["running"] is False

    def test_stop_is_safe_when_idle(self):
        r = self._json(["setup", "test-stop"])
        assert r["applied"] and r["status"]["running"] is False


def test_testpower_requires_login(tmp_path):
    env = {**os.environ, "CLI_ANYTHING_SENEC_COOKIES": str(tmp_path / "none.json"),
           "CLI_ANYTHING_SENEC_SESSION": str(tmp_path / "s.json")}
    r = subprocess.run(_resolve_cli("cli-anything-senec") + ["--json", "setup",
                       "test-charge", "500", "--dry-run"], capture_output=True, text=True,
                       env=env, cwd=_HARNESS_DIR)
    assert r.returncode == 1 and "setup login" in json.loads(r.stdout)["error"]
