import base64
import hashlib
import json
import os

import pytest

from cli_anything.senec.core import control, device, export, project
from cli_anything.senec.core.session import Session
from cli_anything.senec.utils import senec_backend as be

ENERGY_RAW = {"GUI_BAT_DATA_FUEL_CHARGE": "fl_42480000",  # 50.0
              "GUI_BAT_DATA_POWER": "fl_C47A0000",        # -1000
              "GUI_BAT_DATA_VOLTAGE": "fl_424ACCCD",
              "GUI_BAT_DATA_CURRENT": "fl_C1A00000",      # -20
              "GUI_GRID_POW": "fl_C3480000",              # -200 (export)
              "GUI_HOUSE_POW": "fl_44BB8000",             # 1500
              "GUI_INVERTER_POWER": "fl_43C80000",        # 400
              "STAT_STATE": "u8_10",
              "SAFE_CHARGE_RUNNING": "u8_00"}


@pytest.fixture
def tmp_dir(tmp_path):
    return str(tmp_path)


# ── codec ──
class TestCodec:
    def test_float(self):
        assert be.decode_value("fl_42C80000") == 100.0

    def test_float_negative(self):
        assert be.decode_value("fl_C47A0000") == -1000.0

    def test_unsigned(self):
        assert be.decode_value("u8_0E") == 14
        assert be.decode_value("u1_0C5A") == 3162
        assert be.decode_value("u3_00007465") == 0x7465
        assert be.decode_value("u6_0000000000000010") == 16

    def test_signed(self):
        assert be.decode_value("i8_FF") == -1
        assert be.decode_value("i8_12") == 18
        assert be.decode_value("i1_FFFE") == -2
        assert be.decode_value("i3_0000003C") == 60
        assert be.decode_value("i3_FFFFFFFF") == -1

    def test_string(self):
        assert be.decode_value("st_V3 LV") == "V3 LV"
        assert be.decode_value("st_") == ""

    def test_list_and_dict(self):
        assert be.decode_value(["u8_01", "u8_02"]) == [1, 2]
        assert be.decode_value({"A": "u8_01"}) == {"A": 1}

    def test_error_markers(self):
        assert be.decode_value("VARIABLE_NOT_FOUND") is None
        assert be.decode_value("FORBIDDEN") is None

    def test_malformed(self):
        with pytest.raises(be.SenecError):
            be.decode_value("garbage")
        with pytest.raises(be.SenecError):
            be.decode_value("zz_00")

    @pytest.mark.parametrize("t,v", [("u8", 255), ("u1", 3162), ("u3", 123456),
                                     ("i8", -5), ("i1", -300), ("i3", -70000),
                                     ("fl", 12.5), ("st", "abc")])
    def test_roundtrip(self, t, v):
        assert be.decode_value(be.encode_value(t, v)) == v

    def test_encode_format(self):
        assert be.encode_value("u8", 1) == "u8_01"
        assert be.encode_value("fl", 100) == "fl_42C80000"

    def test_encode_negative_unsigned(self):
        with pytest.raises(be.SenecError):
            be.encode_value("u8", -1)

    def test_value_type(self):
        assert be.value_type("fl_00000000") == "fl"
        assert be.value_type(["u1_0000"]) == "u1"
        assert be.value_type("FORBIDDEN") is None

    def test_parse_state_names(self):
        js = 'var x=1,system_state_name={0:"INIT",14:"LADEN",16:"ENTLADEN"},y=2'
        assert be.parse_state_names(js) == {0: "INIT", 14: "LADEN", 16: "ENTLADEN"}
        assert be.parse_state_names("nothing") == {}

    def test_unreachable(self):
        with pytest.raises(be.SenecError, match="Cannot reach"):
            be.query({"ENERGY": {}}, host="http://127.0.0.1:9", timeout=1)


# ── device summaries ──
class TestDevice:
    def test_status(self):
        s = device.summarize_status(device.decode_section(ENERGY_RAW), {16: "ENTLADEN"})
        assert s["state"] == "ENTLADEN" and s["soc_percent"] == 50.0
        assert s["grid_export_w"] == 200.0 and s["grid_import_w"] == 0
        assert s["battery_discharge_w"] == 1000.0 and s["battery_charge_w"] == 0

    def test_status_unknown_state(self):
        assert device.summarize_status({"STAT_STATE": 999})["state"] == "STATE_999"

    def test_decode_section_strips_end_of_array(self):
        d = device.decode_section({"INV": ["st_a", "st_END_OF_ARRAY"]})
        assert d["INV"] == ["a"]

    def test_battery(self):
        bms = {"MODULE_COUNT": 2, "SOC": [20, 21], "SOH": [100, 99], "CYCLES": [5, 6],
               "SYSTEM_SOC": 205, "SYSTEM_SOH": 1000, "SERIAL": ["a", "b"]}
        b = device.summarize_battery(bms)
        assert b["module_count"] == 2 and b["system_soc_percent"] == 20.5
        assert b["modules"][1]["cycles"] == 6 and b["modules"][0]["serial"] == "a"

    def test_cells(self):
        bms = {"MODULE_COUNT": 1, "CELL_VOLTAGES_MODULE_A": [3300, 3310, 3290],
               "CELL_TEMPERATURES_MODULE_A": [18, 19]}
        c = device.summarize_cells(bms)
        assert c[0]["spread_mv"] == 20 and c[0]["min_mv"] == 3290

    def test_pv(self):
        pv = {"MPP_AVAIL": 2, "MPP_VOL": [400.0, 300.0], "MPP_CUR": [2.0, 1.0],
              "MPP_POWER": [800.0, 300.0], "POWER_RATIO": 100.0}
        p = device.summarize_pv(pv)
        assert p["total_w"] == 1100.0 and len(p["strings"]) == 2

    def test_grid(self):
        g = device.summarize_grid({"P_TOTAL": -50.0, "FREQ": 50.01,
                                   "P_AC": [1, 2, 3], "U_AC": [230, 231, 232],
                                   "I_AC": [0.1, 0.2, 0.3]})
        assert g["direction"] == "export" and g["phases"][2]["phase"] == "L3"

    def test_system(self):
        s = device.summarize_system({"MAC_ADDRESS_BYTES": [2, 0, 0, 0, 0, 1],
                                     "FIRMWARE_VERSION": "2307"},
                                    {"DEVICE_ID": "X"}, {"UPDATE_AVAILABLE": 0}, {})
        assert s["mac_address"] == "02:00:00:00:00:01" and s["update_available"] is False

    def test_temps(self):
        t = device.summarize_temps({"BATTERY_TEMP": 20.0}, {"INV_LV": 0})
        assert t["battery_c"] == 20.0 and t["fan_inv_lv"] == 0


# ── project / snapshots ──
class TestProject:
    def _snap(self, soc="fl_42480000", ts=1.0):
        return project.build_snapshot({"ENERGY": {**ENERGY_RAW, "GUI_BAT_DATA_FUEL_CHARGE": soc},
                                       "BMS": {"SOC": ["u1_0002", "u1_0003"]}}, "h", ts)

    def test_build(self):
        s = self._snap()
        assert s["format"] == project.FORMAT and s["summary"]["soc_percent"] == 50.0

    def test_save_load(self, tmp_dir):
        p = os.path.join(tmp_dir, "a.json")
        project.save(self._snap(), p)
        assert project.load(p)["decoded"]["BMS"]["SOC"] == [2, 3]

    def test_overwrite_protection(self, tmp_dir):
        p = os.path.join(tmp_dir, "a.json")
        project.save(self._snap(), p)
        with pytest.raises(FileExistsError):
            project.save(self._snap(), p)
        project.save(self._snap(), p, overwrite=True)

    def test_load_wrong_format(self, tmp_dir):
        p = os.path.join(tmp_dir, "x.json")
        with open(p, "w") as f:
            json.dump({"format": "other"}, f)
        with pytest.raises(ValueError):
            project.load(p)

    def test_info(self):
        i = project.info(self._snap())
        assert i["variables"] == len(ENERGY_RAW) + 1

    def test_flatten(self):
        f = project.flatten({"BMS": {"SOC": [2, 3], "X": 1}})
        assert f == {"BMS.SOC[0]": 2, "BMS.SOC[1]": 3, "BMS.X": 1}

    def test_diff(self):
        d = project.diff(self._snap(), self._snap("fl_42C80000", 2.0))
        assert d["changed"] == 1 and d["changes"][0]["delta"] == 50.0

    def test_diff_tolerance(self):
        d = project.diff(self._snap(), self._snap("fl_42C80000"), tolerance=60)
        assert d["changed"] == 0


# ── export ──
class TestExport:
    def test_csv_json(self, tmp_dir):
        s = TestProject()._snap()
        r = export.export_snapshot(s, os.path.join(tmp_dir, "o.csv"))
        assert r["rows"] == len(project.flatten(s["decoded"]))
        r2 = export.export_snapshot(s, os.path.join(tmp_dir, "o.json"), "json")
        assert json.load(open(r2["output"]))["values"]["BMS.SOC[0]"] == 2

    def test_bad_format(self, tmp_dir):
        with pytest.raises(ValueError):
            export.export_snapshot(TestProject()._snap(), os.path.join(tmp_dir, "o"), "xml")

    def test_sample_mocked(self, monkeypatch, tmp_dir):
        vals = iter(range(3))
        monkeypatch.setattr(device, "get_status",
                            lambda conn, lang: {"timestamp": 0, "soc_percent": next(vals)})
        s = export.sample({}, count=3, interval=0, sleep=lambda x: None)
        assert [x["soc_percent"] for x in s] == [0, 1, 2]
        st = export.stats(s)["stats"]["soc_percent"]
        assert st == {"min": 0, "max": 2, "avg": 1.0}
        r = export.write_samples(s, os.path.join(tmp_dir, "s.csv"))
        assert open(r["output"]).read().count("\n") == 4

    def test_sample_bad_count(self):
        with pytest.raises(ValueError):
            export.sample({}, count=0)


# ── control ──
class TestControl:
    def test_plan_set_infer(self):
        p = control.plan_set("energy", "zero_export", 1, current_raw="u8_00")
        assert p["typed"] == "u8_01" and p["request"] == {"ENERGY": {"ZERO_EXPORT": "u8_01"}}

    def test_plan_set_explicit(self):
        assert control.plan_set("X", "Y", 1.5, "fl")["typed"] == "fl_3FC00000"

    def test_plan_set_uninferrable(self):
        with pytest.raises(be.SenecError):
            control.plan_set("X", "Y", 1, current_raw="FORBIDDEN")

    def test_safe_charge(self):
        assert control.plan_safe_charge("on")["variable"] == "SAFE_CHARGE_FORCE"
        assert control.plan_safe_charge("off")["variable"] == "SAFE_CHARGE_PROHIBIT"
        with pytest.raises(ValueError):
            control.plan_safe_charge("maybe")


# ── session ──
class TestSession:
    def test_defaults(self, tmp_dir):
        s = Session(os.path.join(tmp_dir, "s.json"))
        assert s.config["host"] == be.DEFAULT_HOST and s.config["verify_tls"] is False

    def test_set_normalizes(self, tmp_dir):
        s = Session(os.path.join(tmp_dir, "s.json"))
        s.set_config("host", "10.0.0.5/")
        assert s.config["host"] == "https://10.0.0.5"
        s.set_config("verify_tls", "yes")
        assert s.config["verify_tls"] is True

    def test_invalid(self, tmp_dir):
        s = Session(os.path.join(tmp_dir, "s.json"))
        with pytest.raises(ValueError):
            s.set_config("nope", 1)
        with pytest.raises(ValueError):
            s.set_config("timeout", "0")
        with pytest.raises(ValueError):
            s.undo()
        with pytest.raises(ValueError):
            s.redo()

    def test_undo_redo_persist(self, tmp_dir):
        p = os.path.join(tmp_dir, "s.json")
        s = Session(p)
        s.set_config("host", "https://a")
        s.set_config("host", "https://b")
        s.undo()
        assert s.config["host"] == "https://a"
        s.save()
        s2 = Session(p)
        s2.redo()
        assert s2.config["host"] == "https://b"
        assert len(s2.history) == 4

    def test_locked_save_shrinks(self, tmp_dir):
        p = os.path.join(tmp_dir, "s.json")
        s = Session(p)
        s.history = [{"command": "x" * 1000}]
        s.save()
        s.history = []
        s.save()
        json.load(open(p))  # no trailing garbage


# ── cloud (mein-senec.de) ──

import datetime as dt  # noqa: E402

from cli_anything.senec.core import cloud  # noqa: E402
from cli_anything.senec.utils import senec_cloud as sc  # noqa: E402

SYSTEMS = [{"id": 11, "controlUnitNumber": "S100", "mode": "SLAVE",
            "product": {"name": "P"}, "wallboxIds": []},
           {"id": 22, "controlUnitNumber": "S200", "mode": "MASTER",
            "product": {"name": "SENEC.HOME V3", "systemType": "V3", "batteryType": "LFP"},
            "timezoneText": "Europe/Berlin", "wallboxIds": ["a"]}]
MEAS = {"measurements": ["POWER_GENERATION", "GRID_IMPORT", "POWER_CONSUMPTION",
                         "BATTERY_LEVEL_IN_PERCENT", "NEW_THING"],
        "timeSeries": [
            {"date": "2026-01-01T10:00:00Z",
             "measurements": {"durationInSeconds": 3600, "values": [1.0, 0.5, 2.0, 40.0, 7]}},
            {"date": "2026-01-01T11:00:00Z",
             "measurements": {"durationInSeconds": 3600, "values": [2.0, 0.5, 2.0, 60.0, 8]}}]}
DASH = {"currently": {"powerGenerationInW": 1000.04, "powerConsumptionInW": 500,
                      "gridFeedInInW": 100, "gridDrawInW": 0, "batteryChargeInW": 400,
                      "batteryDischargeInW": 0, "batteryLevelInPercent": 55.0,
                      "selfSufficiencyInPercent": 100, "wallboxInW": 0},
        "today": {"powerGenerationInWh": 12345, "gridDrawInWh": 1000,
                  "powerConsumptionInWh": 5000},
        "timestamp": "2026-01-01T10:00:00Z", "storageDeviceState": "CHARGING",
        "numberOfWallboxes": 0}


class TestCloudBackend:
    def test_parse_env_file(self):
        env = sc.parse_env_file('export SENEC-MAIL=a@b.de\n# c=1\nSENEC_PASSWORD="p w"\nX=\'q\'\n')
        assert env == {"SENEC-MAIL": "a@b.de", "SENEC_PASSWORD": "p w", "X": "q"}

    def test_resolve_credentials_precedence(self, tmp_dir):
        p = os.path.join(tmp_dir, ".env")
        open(p, "w").write("SENEC-MAIL=file@x\nSENEC_PASSWORD=filepw\n")
        c = sc.resolve_credentials(env_file=p, environ={})
        assert c == {"user": "file@x", "password": "filepw", "totp_secret": None}
        c = sc.resolve_credentials(env_file=p, environ={"SENEC_USER": "env@x"})
        assert c["user"] == "env@x" and c["password"] == "filepw"
        assert sc.resolve_credentials("arg@x", env_file=p, environ={})["user"] == "arg@x"
        with pytest.raises(FileNotFoundError):
            sc.resolve_credentials(env_file=os.path.join(tmp_dir, "nope"), environ={})

    def test_totp_rfc6238(self):
        # RFC 6238 SHA1 vector: secret "12345678901234567890", T=59 -> 94287082 (8 digits)
        secret = base64.b32encode(b"12345678901234567890").decode()
        assert sc.totp_now(secret, at=59, digits=8) == "94287082"
        assert len(sc.totp_now(secret)) == 6

    def test_pkce(self):
        v, c = sc.pkce_pair()
        assert 43 <= len(v) <= 128 and "=" not in c
        assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()

    def test_parse_login_form(self):
        page = ('<form id="kc" action="https://sso/x?a=1&amp;b=2" method="post">'
                '<input name="username"/><input type="password" name="password"/></form>')
        assert sc.parse_login_form(page) == ("https://sso/x?a=1&b=2", {"username", "password"})
        assert sc.parse_login_form('<form action="u"><input name="otp"></form>')[1] == {"otp"}
        assert sc.parse_login_form("<html>no form</html>") is None
        assert sc.parse_login_error('<span id="input-error" class="x">Invalid password.</span>') \
            == "Invalid password."

    def test_token_state_and_stamp(self):
        tok = sc._stamp({"access_token": "a", "expires_in": 300, "refresh_token": "r",
                         "refresh_expires_in": 1000}, now=1000)
        assert tok["expires_at"] == 1290 and tok["refresh_expires_at"] == 1990
        assert sc.token_state(tok, now=1100) == "valid"
        assert sc.token_state(tok, now=1500) == "refreshable"
        assert sc.token_state(tok, now=3000) == "expired"
        assert sc.token_state(None) == "missing"

    def test_jwt_claims(self):
        payload = base64.urlsafe_b64encode(json.dumps({"email": "a@b"}).encode()).rstrip(b"=")
        assert sc.jwt_claims("h." + payload.decode() + ".s") == {"email": "a@b"}
        assert sc.jwt_claims("garbage") == {}

    def test_token_file_roundtrip_private(self, tmp_dir):
        p = os.path.join(tmp_dir, "sub", "tok.json")
        sc.save_token({"access_token": "x"}, p)
        assert sc.load_token(p) == {"access_token": "x"}
        assert os.stat(p).st_mode & 0o777 == 0o600
        assert sc.delete_token(p) is True and sc.delete_token(p) is False
        assert sc.load_token(p) is None

    def test_client_without_token_or_creds(self, tmp_dir):
        c = sc.CloudClient(os.path.join(tmp_dir, "t.json"), {})
        with pytest.raises(sc.CloudAuthError, match="cloud login"):
            c.ensure_token()

    def test_login_requires_credentials(self):
        with pytest.raises(sc.CloudAuthError, match="Missing credentials"):
            sc.login(None, None)

    def test_iso_utc(self):
        assert sc.iso_utc(0) == "1970-01-01T00:00:00Z"
        t = dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone(dt.timedelta(hours=1)))
        assert sc.iso_utc(t) == "2026-01-01T11:00:00Z"


class TestCloudCore:
    NOW = dt.datetime(2026, 5, 10, 15, 30, tzinfo=dt.timezone.utc)

    def test_parse_time(self):
        assert cloud.parse_time("now", self.NOW) == self.NOW
        assert cloud.parse_time("today", self.NOW).hour == 0
        assert cloud.parse_time("yesterday", self.NOW).day == 9
        assert cloud.parse_time("6h", self.NOW) == self.NOW - dt.timedelta(hours=6)
        assert cloud.parse_time("2w", self.NOW) == self.NOW - dt.timedelta(weeks=2)
        assert cloud.parse_time("2026-01-31T10:00:00Z").tzinfo is not None
        assert cloud.parse_time("2026-01-31").tzinfo is not None
        with pytest.raises(ValueError, match="Bad time"):
            cloud.parse_time("soon")

    def test_select_system(self):
        assert cloud.select_system(SYSTEMS)["id"] == 22  # first MASTER
        assert cloud.select_system(SYSTEMS, "11")["id"] == 11
        assert cloud.select_system(SYSTEMS, "s200")["id"] == 22
        assert cloud.select_system(SYSTEMS, "0")["id"] == 11
        with pytest.raises(ValueError, match="not found"):
            cloud.select_system(SYSTEMS, "999")
        with pytest.raises(ValueError):
            cloud.select_system([])

    def test_summarize_systems(self):
        s = cloud.summarize_systems(SYSTEMS)[1]
        assert s["product"] == "SENEC.HOME V3" and s["wallboxes"] == 1 and s["mode"] == "MASTER"

    def test_summarize_details(self):
        d = cloud.summarize_details({
            "systemOverview": {"systemId": 1, "productName": "P"},
            "casing": {"temperatureInCelsius": 28.456},
            "mcu": {"firmwareVersion": "842", "mainControllerUnitState":
                    {"name": "LADEN", "severity": "INFO"}, "lastContact": {"timestamp": "t"}},
            "batteryPack": {"numberOfBatteryModules": 2, "maxCapacityInKwh": 10.0},
            "batteryModules": [{"ordinal": 1, "state": {"state": "OK"}}]})
        assert d["state"] == "LADEN" and d["case_temp_c"] == 28.46 and d["capacity_kwh"] == 10.0
        assert d["modules"][0]["state"] == "OK"
        assert cloud.summarize_details({})["modules"] == []

    def test_summarize_dashboard(self):
        d = cloud.summarize_dashboard(DASH)
        assert d["now"]["pv_w"] == 1000.0 and d["now"]["soc_percent"] == 55.0
        assert d["today"]["pv_kwh"] == 12.345 and d["today"]["battery_charge_kwh"] == 0
        assert d["storage_state"] == "CHARGING"

    def test_measurement_rows_and_totals(self):
        names, rows = cloud.measurement_rows(MEAS)
        assert names == ["pv_kwh", "grid_import_kwh", "house_kwh", "battery_level_percent",
                         "new_thing"]
        assert rows[0] == {"date": "2026-01-01T10:00:00Z", "duration_s": 3600, "pv_kwh": 1.0,
                           "grid_import_kwh": 0.5, "house_kwh": 2.0,
                           "battery_level_percent": 40.0, "new_thing": 7}
        t = cloud.measurement_totals(names, rows)
        assert t["pv_kwh"] == 3.0 and t["house_kwh"] == 4.0
        assert t["battery_level_percent"] == {"min": 40.0, "max": 60.0, "avg": 50.0}
        assert t["self_sufficiency_percent"] == 75.0
        assert cloud.measurement_rows({}) == ([], [])

    def test_write_measurements(self, tmp_dir):
        names, rows = cloud.measurement_rows(MEAS)
        p = os.path.join(tmp_dir, "h.csv")
        r = cloud.write_measurements(names, rows, p)
        lines = open(p).read().splitlines()
        assert r["rows"] == 2 and lines[0].startswith("date,duration_s,pv_kwh")
        with pytest.raises(FileExistsError):
            cloud.write_measurements(names, rows, p)
        j = cloud.write_measurements(names, rows, os.path.join(tmp_dir, "h.json"), "json")
        assert json.load(open(j["output"]))[1]["pv_kwh"] == 2.0
        with pytest.raises(ValueError):
            cloud.write_measurements(names, rows, os.path.join(tmp_dir, "x"), "xml")

    def test_compare(self):
        local = device.summarize_status(device.decode_section(ENERGY_RAW))
        local["timestamp"] = dt.datetime(2026, 1, 1, 10, 1, tzinfo=dt.timezone.utc).timestamp()
        c = cloud.compare(local, cloud.summarize_dashboard(DASH))
        assert c["cloud_age_s"] == 60.0
        assert c["fields"]["soc_percent"] == {"local": 50.0, "cloud": 55.0, "delta": -5.0}
        assert c["fields"]["grid_export_w"]["local"] == 200.0

    def test_token_info(self):
        assert cloud.token_info(None, "/p") == {"token_file": "/p", "state": "missing"}

    def test_session_cloud_system(self, tmp_dir):
        s = Session(os.path.join(tmp_dir, "s.json"))
        assert s.config["cloud_system"] is None
        s.set_config("cloud_system", "22")
        assert s.config["cloud_system"] == "22"
        s.set_config("cloud_system", "auto")
        assert s.config["cloud_system"] is None


# ── cloud settings (refine 2) ──

PEAK = {"einspeisebegrenzungKwpInPercent": 100, "peakShavingMode": "AUTO",
        "peakShavingCapacityLimitInPercent": 0, "peakShavingEndDate": 1795737600000}
PLANT = {"steuereinheitnummer": "S200", "peakShavingVisible": True,
         "emergencyPowerVisible": False, "sgReadyVisible": False, "wallboxVisible": False}


class TestCloudSettings:
    NOW = dt.datetime(2026, 5, 10, 15, 30, tzinfo=dt.timezone.utc)

    def test_plan_reserve(self):
        p = cloud.plan_reserve(22, 0, 15, "20")
        assert p["request"] == {"method": "POST",
                                "url": sc.WEB_API + "/senec/0/emergencypower",
                                "params": {"reserve-in-percent": 20}}
        assert p["old"] == {"reserve_percent": 15} and p["new"] == {"reserve_percent": 20}
        assert p["changed"] is True and cloud.plan_reserve(22, 0, 15, 15)["changed"] is False

    @pytest.mark.parametrize("bad", ["-1", "51", "12.5", "abc", None])
    def test_plan_reserve_invalid(self, bad):
        with pytest.raises(ValueError):
            cloud.plan_reserve(22, 0, 15, bad)

    def test_plan_peak_keeps_current(self):
        p = cloud.plan_peak_shaving(22, 1, PEAK, now=self.NOW)
        assert p["request"]["url"].endswith("saveSettings?anlageNummer=1")
        assert p["request"]["params"] == {"mode": "AUTO", "capacityLimit": 0,
                                          "endzeit": 1795737600000}
        assert p["changed"] is False

    def test_plan_peak_manual(self):
        p = cloud.plan_peak_shaving(22, 0, PEAK, "manual", 60, "18:00", now=self.NOW)
        end = dt.datetime.fromtimestamp(p["new"]["end_ms"] / 1000, dt.timezone.utc)
        assert p["new"]["mode"] == "MANUAL" and p["new"]["capacity_limit_percent"] == 60
        assert end == self.NOW.replace(hour=18, minute=0) and p["changed"]

    def test_plan_peak_invalid(self):
        with pytest.raises(ValueError, match="mode"):
            cloud.plan_peak_shaving(22, 0, PEAK, "TURBO", now=self.NOW)
        with pytest.raises(ValueError, match="0..100"):
            cloud.plan_peak_shaving(22, 0, PEAK, capacity=101, now=self.NOW)
        with pytest.raises(ValueError, match="future"):
            cloud.plan_peak_shaving(22, 0, PEAK, "MANUAL", end="2020-01-01", now=self.NOW)
        with pytest.raises(ValueError, match="end time"):
            cloud.plan_peak_shaving(22, 0, {"peakShavingMode": "AUTO",
                                            "peakShavingCapacityLimitInPercent": 0})

    def test_parse_end_time(self):
        assert cloud.parse_end_time("16:00", self.NOW) == int(
            self.NOW.replace(hour=16, minute=0).timestamp()) * 1000
        nxt = cloud.parse_end_time("10:00", self.NOW)  # already passed -> tomorrow
        assert nxt == int((self.NOW.replace(hour=10, minute=0)
                           + dt.timedelta(days=1)).timestamp()) * 1000
        with pytest.raises(ValueError):
            cloud.parse_end_time("25:00", self.NOW)

    def test_summarize_peak(self):
        s = cloud.summarize_peak_shaving(PEAK)
        assert s["mode"] == "AUTO" and s["end"] == "2026-11-27T00:00:00Z"
        assert s["feed_in_limit_kwp_percent"] == 100

    def test_settings_overview(self):
        ov = {s["setting"]: s for s in cloud.settings_overview(
            PLANT, {"emergency_reserve": {"reserve_percent": 15},
                    "peak_shaving": sc.SenecError("HTTP 500")})}
        assert ov["emergency_reserve"]["status"] == "supported"
        assert ov["peak_shaving"]["status"] == "skipped" and "500" in ov["peak_shaving"]["reason"]
        assert ov["sg_ready"]["reason"] == "portal flag sgReadyVisible=false"
        assert ov["forecast_charging"]["status"] == "skipped"

    def test_verify(self):
        p = cloud.plan_reserve(22, 0, 15, 20)
        assert cloud.verify(p, {"reserve_percent": 20})
        assert not cloud.verify(p, {"reserve_percent": 15})
        assert not cloud.verify(p, {})

    def test_web_client_requires_credentials(self):
        with pytest.raises(sc.CloudAuthError, match="credentials"):
            sc.WebClient({}).login()


# ── installer / setup mode (refine 3) ──

from cli_anything.senec.core import installer  # noqa: E402


@pytest.fixture
def cookie_file(tmp_dir, monkeypatch):
    path = os.path.join(tmp_dir, "cookies.json")
    monkeypatch.setenv("CLI_ANYTHING_SENEC_COOKIES", path)
    return path


class TestCookieStore:
    def test_parse_set_cookie(self):
        h = "senec-homev3-gui=abc123;Secure;HttpOnly;Max-Age=3600;SameSite=Strict"
        assert be.parse_set_cookie(h) == ("senec-homev3-gui", "abc123", 3600)
        assert be.parse_set_cookie("garbage") == (None, None, None)
        assert be.parse_set_cookie("a=b; Max-Age=x") == ("a", "b", None)

    def test_store_get_clear(self, cookie_file):
        be.store_cookie("https://senec.home.arpa/", "v1", 100, now=1000)
        assert be.get_cookie("https://SENEC.home.arpa", now=1050)["value"] == "v1"
        assert be.get_cookie("https://senec.home.arpa", now=1100) is None  # expired
        assert be.get_cookie("https://other.host", now=1050) is None
        assert oct(os.stat(cookie_file).st_mode & 0o777) == "0o600"
        assert be.clear_cookie("https://senec.home.arpa") is True
        assert be.clear_cookie("https://senec.home.arpa") is False

    def test_no_expiry(self, cookie_file):
        be.store_cookie("https://h", "v", None, now=0)
        assert be.get_cookie("https://h", now=10 ** 10)["value"] == "v"

    def test_corrupt_file(self, cookie_file):
        open(cookie_file, "w").write("{not json")
        assert be.get_cookie("https://h") is None


class TestInstaller:
    def test_resolve_credentials_precedence(self, tmp_dir):
        env = os.path.join(tmp_dir, ".env")
        open(env, "w").write("export SENEC_INSTALL_USERNAME=fileuser\n"
                             "export SENEC_INSTALL_PASSWORD='filepw'\n")
        assert installer.resolve_credentials(env_file=env, environ={}) == \
            {"user": "fileuser", "password": "filepw"}
        r = installer.resolve_credentials(env_file=env, environ={"SENEC_INSTALL_USERNAME": "e"})
        assert r == {"user": "e", "password": "filepw"}
        r = installer.resolve_credentials("x", "y", env_file=env, environ={})
        assert r == {"user": "x", "password": "y"}
        with pytest.raises(FileNotFoundError):
            installer.resolve_credentials(env_file=os.path.join(tmp_dir, "nope"), environ={})

    def test_plan_login(self):
        p = installer.plan_login("installateur", "p" * 40)["LOG"]
        assert p["USERNAME"] == "st_installateur" and p["LOG_IN_BUTT"] == "u8_01"
        assert p["PASSWORD"] == "st_" + "p" * 23 and p["USER_LEVEL"] == ""
        with pytest.raises(ValueError, match="SENEC_INSTALL_USERNAME"):
            installer.plan_login(None, "x")

    @pytest.mark.parametrize("level,role,on,su", [(None, "none", False, False),
                                                  (0, "none", False, False),
                                                  (2, "installer", True, False),
                                                  (4, "professional", True, False),
                                                  (16, "superuser", True, True),
                                                  (3, "level_3", True, False)])
    def test_summarize_log(self, level, role, on, su):
        log = {"LOG_IN_NOK_COUNT": 0} if level is None else {"USER_LEVEL": level,
                                                             "LOG_IN_NOK_COUNT": 0}
        s = installer.summarize_log(log)
        assert (s["role"], s["setup_mode"], s["superuser"]) == (role, on, su)
        assert s["has_cookie"] is False and "cookie_expires_in_s" not in s

    def test_summarize_cookie_expiry(self):
        s = installer.summarize_log({"USER_LEVEL": 2}, {"value": "v", "expires": 1100}, now=1000)
        assert s["has_cookie"] and s["cookie_expires_in_s"] == 100

    def _fake_device(self, monkeypatch, levels, nok=0, bad_password=False):
        sent = []
        seq = iter(levels)
        state = {"nok": nok}

        def query(req, **conn):
            sent.append(req)
            if bad_password and "LOG_IN_BUTT" in req.get("LOG", {}):
                state["nok"] += 1
            if "LOG" in req and "USER_LEVEL" in req["LOG"] and "LOG_IN_BUTT" not in req["LOG"]:
                lvl = next(seq)
                out = {"LOG_IN_NOK_COUNT": f"u8_{state['nok']:02X}"}
                out["USER_LEVEL"] = "VARIABLE_NOT_FOUND" if lvl is None else f"u8_{lvl:02X}"
                return {"LOG": out}
            return {k: {n: "FORBIDDEN" for n in v} for k, v in req.items()}
        monkeypatch.setattr(be, "query", query)
        return sent

    CONN = {"host": "https://h", "verify": False, "timeout": 1.0}

    def test_login_success(self, monkeypatch, cookie_file):
        sent = self._fake_device(monkeypatch, [None, None, 2])
        r = installer.login(self.CONN, "u", "p", wait=60, sleep=lambda s: None)
        assert r["logged_in"] and r["role"] == "installer" and r["already_logged_in"] is False
        assert sent[0] == {"FACTORY": {"SYS_TYPE": "", "COUNTRY": "", "DEVICE_ID": ""}}
        assert any("LOG_IN_BUTT" in q.get("LOG", {}) for q in sent)

    def test_login_already(self, monkeypatch, cookie_file):
        be.store_cookie("https://h", "v", 3600)
        sent = self._fake_device(monkeypatch, [2])
        r = installer.login(self.CONN, "u", "p", sleep=lambda s: None)
        assert r["already_logged_in"] and len(sent) == 1

    def test_login_wrong_password(self, monkeypatch, cookie_file):
        sent = self._fake_device(monkeypatch, [None] * 50, nok=1, bad_password=True)
        with pytest.raises(be.SenecError, match="wrong username/password.*2/5"):
            installer.login(self.CONN, "u", "bad", wait=60, resend=0, sleep=lambda s: None)
        assert sum("LOG_IN_BUTT" in q.get("LOG", {}) for q in sent) == 1  # no retry

    def test_login_resends_when_ignored(self, monkeypatch, cookie_file):
        sent = self._fake_device(monkeypatch, [None, None, None, 2])
        r = installer.login(self.CONN, "u", "p", wait=60, resend=0, sleep=lambda s: None)
        assert r["logged_in"]
        assert sum("LOG_IN_BUTT" in q.get("LOG", {}) for q in sent) == 3

    def test_login_timeout_other_session(self, monkeypatch, cookie_file):
        self._fake_device(monkeypatch, [None] * 50)
        with pytest.raises(be.SenecError, match="another active GUI session"):
            installer.login(self.CONN, "u", "p", wait=0, sleep=lambda s: None)

    def test_login_blocked(self, monkeypatch, cookie_file):
        sent = self._fake_device(monkeypatch, [None], nok=5)
        with pytest.raises(be.SenecError, match="blocked"):
            installer.login(self.CONN, "u", "p", sleep=lambda s: None)
        assert not any("LOG_IN_BUTT" in q.get("LOG", {}) for q in sent)

    def test_logout_clears_cookie(self, monkeypatch, cookie_file):
        be.store_cookie("https://h", "v", 3600)
        sent = self._fake_device(monkeypatch, [None])
        r = installer.logout(self.CONN, sleep=lambda s: None)
        assert r["cookie_cleared"] and not r["logged_in"]
        assert sent[0] == installer.LOGOUT_REQUEST and be.get_cookie("https://h") is None


# ── installer test charge / discharge (refine 4) ──

from cli_anything.senec.core import testpower  # noqa: E402

LIMITS = {"MAX_CHARGE_POWER_DC": 2500.0, "MAX_DISCHARGE_POWER_DC": 3750.0}


class TestTestPower:
    def test_parse_watts(self):
        assert testpower.parse_watts("500") == 500 and testpower.parse_watts("500.0") == 500
        for bad in ("0", "-5", "12.5", "abc", "", None):
            with pytest.raises(ValueError, match="positive integer"):
                testpower.parse_watts(bad)

    def test_charge_request_matches_ui(self):
        p = testpower.plan_start("charge", "600", LIMITS, soc=1.0)
        e = p["request"]["ENERGY"]
        assert e["TEST_CHARGE_ENABLE"] == "u8_01" and len(e["GRID_POWER_OFFSET"]) == 3
        assert all(be.decode_value(v) == -200.0 for v in e["GRID_POWER_OFFSET"])
        assert p["watts"] == 600 and p["limit_w"] == 2500 and p["min_soc_percent"] is None

    def test_discharge_request_positive_offset(self):
        p = testpower.plan_start("discharge", 900, LIMITS, soc=50.0)
        assert [be.decode_value(v) for v in p["request"]["ENERGY"]["GRID_POWER_OFFSET"]] \
            == [300.0] * 3

    def test_500w_encoding(self):
        assert testpower.offsets("charge", 500) == ["fl_C326AAAB"] * 3

    def test_hardware_caps(self):
        testpower.plan_start("charge", 2500, LIMITS)
        with pytest.raises(ValueError, match="2500 W"):
            testpower.plan_start("charge", 2501, LIMITS)
        testpower.plan_start("discharge", 3750, LIMITS, soc=80)
        with pytest.raises(ValueError, match="3750 W"):
            testpower.plan_start("discharge", 3751, LIMITS, soc=80)
        with pytest.raises(be.SenecError, match="unavailable"):
            testpower.plan_start("charge", 100, {})

    @pytest.mark.parametrize("soc,min_soc,ok", [(10.0, 10.0, False), (5.0, 10.0, False),
                                                (10.1, 10.0, True), (5.0, 4.0, True)])
    def test_soc_guard(self, soc, min_soc, ok):
        if ok:
            testpower.plan_start("discharge", 500, LIMITS, soc=soc, min_soc=min_soc)
        else:
            with pytest.raises(ValueError, match="at/below minimum"):
                testpower.plan_start("discharge", 500, LIMITS, soc=soc, min_soc=min_soc)

    def test_soc_guard_not_for_charge_and_unknown_soc(self):
        testpower.plan_start("charge", 500, LIMITS, soc=0.0)
        with pytest.raises(be.SenecError, match="SOC unknown"):
            testpower.plan_start("discharge", 500, LIMITS, soc=None)

    def test_other_mode_running(self):
        with pytest.raises(ValueError, match="test-stop"):
            testpower.plan_start("charge", 500, LIMITS, soc=50, running="discharge")
        testpower.plan_start("charge", 700, LIMITS, soc=50, running="charge")  # adjust power

    def test_plan_stop(self):
        e = testpower.plan_stop()["request"]["ENERGY"]
        assert e == {"TEST_CHARGE_ENABLE": "u8_00", "GRID_POWER_OFFSET": ["fl_00000000"] * 3}

    def test_summarize(self):
        s = testpower.summarize({"TEST_CHARGE_ENABLE": 1, "GRID_POWER_OFFSET": [-166.67] * 3,
                                 "GUI_TEST_CHARGE_STAT": 1, "GUI_TEST_DISCHARGE_STAT": 0,
                                 "STAT_STATE": 15, "GUI_BAT_DATA_POWER": 498.0},
                                {15: "AKKU LEER"})
        assert s["running"] and s["mode"] == "charge" and s["test_power_w"] == 500.0
        assert s["grid_offset_w"] == -500.0 and s["state"] == "AKKU LEER"
        off = testpower.summarize({"TEST_CHARGE_ENABLE": 0, "STAT_STATE": 4})
        assert not off["running"] and off["mode"] is None and off["test_power_w"] == 0

    CONN = {"host": "https://h", "verify": False, "timeout": 1.0}

    def _fake(self, monkeypatch, fail_on=None):
        sent = []

        def query(req, **conn):
            sent.append(req)
            e = req.get("ENERGY", {})
            if fail_on and fail_on(len(sent)):
                raise be.SenecError("boom")
            return {"ENERGY": {k: (["fl_00000000"] * 3 if isinstance(v, list) else "u8_00")
                               for k, v in e.items()}}
        monkeypatch.setattr(be, "query", query)
        monkeypatch.setattr(be, "read_vars", lambda s, names, **c: {"ENERGY": {
            "TEST_CHARGE_ENABLE": "u8_01", "GRID_POWER_OFFSET": ["fl_C326AAAB"] * 3,
            "GUI_TEST_CHARGE_STAT": "u8_01", "STAT_STATE": "u8_0F",
            "GUI_BAT_DATA_POWER": "fl_43FA0000"}})
        return sent

    @staticmethod
    def _clock():
        t = [0.0]
        return t, (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))

    def test_run_for_auto_stop(self, monkeypatch):
        sent = self._fake(monkeypatch)
        _, clock, sleep = self._clock()
        ticks = []
        r = testpower.run_for(testpower.plan_start("charge", 500, LIMITS), self.CONN, 30,
                              poll=5, on_tick=ticks.append, sleep=sleep, clock=clock)
        assert len(r["samples"]) == 6 == len(ticks) and r["elapsed_s"] == 30
        assert r["samples"][0]["battery_w"] == 500.0 and not r["interrupted"]
        assert sent[0]["ENERGY"]["TEST_CHARGE_ENABLE"] == "u8_01"
        assert sent[-1] == testpower.plan_stop()["request"]

    def test_run_for_ctrl_c_stops(self, monkeypatch):
        sent = self._fake(monkeypatch)
        _, clock, sleep = self._clock()

        def tick(s):
            raise KeyboardInterrupt
        r = testpower.run_for(testpower.plan_start("charge", 500, LIMITS), self.CONN, 30,
                              on_tick=tick, sleep=sleep, clock=clock)
        assert r["interrupted"] and sent[-1] == testpower.plan_stop()["request"]

    def test_run_for_error_still_stops(self, monkeypatch):
        sent = self._fake(monkeypatch)
        monkeypatch.setattr(testpower, "get_status",
                            lambda *a, **k: (_ for _ in ()).throw(be.SenecError("net")))
        _, clock, sleep = self._clock()
        with pytest.raises(be.SenecError, match="net"):
            testpower.run_for(testpower.plan_start("charge", 500, LIMITS), self.CONN, 30,
                              sleep=sleep, clock=clock)
        assert sent[-1] == testpower.plan_stop()["request"]

    def test_stop_retries(self, monkeypatch):
        sent = self._fake(monkeypatch, fail_on=lambda n: n < 3)
        testpower.stop(self.CONN, sleep=lambda s: None)
        assert len(sent) == 3

    def test_apply_rejected(self, monkeypatch):
        monkeypatch.setattr(be, "query", lambda req, **c: {"ENERGY": {
            "TEST_CHARGE_ENABLE": "FORBIDDEN", "GRID_POWER_OFFSET": ["fl_00000000"] * 3}})
        with pytest.raises(be.SenecError, match="rejected"):
            testpower.apply(testpower.plan_stop(), self.CONN)

    def test_status_requires_login(self, monkeypatch):
        monkeypatch.setattr(be, "read_vars", lambda s, n, **c: {"ENERGY": {
            k: "VARIABLE_NOT_FOUND" for k in n}})
        with pytest.raises(be.SenecError, match="setup login"):
            testpower.get_status(self.CONN)

    def test_require_login(self, monkeypatch):
        monkeypatch.setattr(installer, "get_state", lambda c: {"logged_in": False})
        with pytest.raises(be.SenecError, match="setup login"):
            testpower.require_login(self.CONN)


class TestEnsureCookie:
    def test_stale_session_recovery(self, monkeypatch, cookie_file):
        sent = []

        def query(req, **conn):
            sent.append(req)
            if req == installer.LOGOUT_REQUEST:
                return {}
            if len(sent) == 3:
                be.store_cookie(conn["host"], "fresh", 3600)
            return {}
        monkeypatch.setattr(be, "query", query)
        installer._ensure_cookie({"host": "https://h"}, sleep=lambda s: None)
        assert sent == [installer.COOKIE_REQUEST, installer.LOGOUT_REQUEST,
                        installer.COOKIE_REQUEST]
        assert be.get_cookie("https://h")["value"] == "fresh"
