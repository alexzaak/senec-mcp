"""mein-senec.de cloud domain logic: system selection, summaries, measurement tables."""

import csv
import datetime as dt
import json
import os
import re

from senec_mcp.utils import senec_cloud as sc

MEASURE_KEYS = {
    "POWER_GENERATION": "pv_kwh",
    "POWER_CONSUMPTION": "house_kwh",
    "GRID_IMPORT": "grid_import_kwh",
    "GRID_EXPORT": "grid_export_kwh",
    "BATTERY_IMPORT": "battery_charge_kwh",
    "BATTERY_EXPORT": "battery_discharge_kwh",
    "BATTERY_LEVEL_IN_PERCENT": "battery_level_percent",
    "AUTARKY_IN_PERCENT": "autarky_percent",
    "WALLBOX_CONSUMPTION": "wallbox_kwh",
}
NON_SUMMABLE = {"battery_level_percent", "autarky_percent"}

_REL_RE = re.compile(r"^(\d+)([mhdw])$")
_REL_UNIT = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_time(value, now=None):
    """Accept ISO date/datetime, 'now', 'today', 'yesterday' or relative '6h', '7d', '2w'."""
    now = now or dt.datetime.now().astimezone()
    v = str(value).strip().lower()
    if v == "now":
        return now
    if v in ("today", "yesterday"):
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - dt.timedelta(days=1) if v == "yesterday" else day
    m = _REL_RE.match(v)
    if m:
        return now - dt.timedelta(**{_REL_UNIT[m.group(2)]: int(m.group(1))})
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"Bad time {value!r}: use ISO (2026-01-31[T12:00]), "
                         "now/today/yesterday or relative (30m, 6h, 7d, 2w)") from e
    return t if t.tzinfo else t.astimezone()


def select_system(systems, system=None):
    """Pick a system by id, controlUnitNumber or index; default: the only/first MASTER."""
    if not systems:
        raise ValueError("No SENEC systems registered for this account")
    if system is None:
        masters = [s for s in systems if s.get("mode", "MASTER") == "MASTER"]
        return (masters or systems)[0]
    sval = str(system)
    for s in systems:
        if str(s.get("id")) == sval or str(s.get("controlUnitNumber", "")).lower() == sval.lower():
            return s
    if sval.isdigit() and int(sval) < len(systems):
        return systems[int(sval)]
    raise ValueError(f"System {system!r} not found; available ids: "
                     f"{', '.join(str(s.get('id')) for s in systems)}")


def summarize_systems(systems):
    return [{"id": s.get("id"), "control_unit": s.get("controlUnitNumber"),
             "case": s.get("caseNumber"), "product": (s.get("product") or {}).get("name"),
             "system_type": (s.get("product") or {}).get("systemType"),
             "battery_type": (s.get("product") or {}).get("batteryType"),
             "mode": s.get("mode"), "timezone": s.get("timezoneText"),
             "wallboxes": len(s.get("wallboxIds") or [])} for s in systems]


def summarize_details(d):
    mcu = d.get("mcu") or {}
    pack = d.get("batteryPack") or {}
    state = mcu.get("mainControllerUnitState") or {}
    inst = d.get("installer") or {}
    return {
        "system_id": (d.get("systemOverview") or {}).get("systemId"),
        "product": (d.get("systemOverview") or {}).get("productName"),
        "installed": (d.get("systemOverview") or {}).get("installationDateTime"),
        "state": state.get("name"), "severity": state.get("severity"),
        "operating_mode": state.get("operatingMode"),
        "firmware_version": mcu.get("firmwareVersion"), "gui_version": mcu.get("guiVersion"),
        "last_contact": (mcu.get("lastContact") or {}).get("timestamp"),
        "case_temp_c": round((d.get("casing") or {}).get("temperatureInCelsius") or 0, 2),
        "battery_technology": pack.get("technology"),
        "battery_modules": pack.get("numberOfBatteryModules"),
        "battery_level_percent": pack.get("currentChargingLevelInPercent"),
        "capacity_kwh": pack.get("maxCapacityInKwh"),
        "modules": [{"ordinal": m.get("ordinal"), "serial": m.get("serialNumber"),
                     "state": (m.get("state") or {}).get("state"),
                     "firmware": m.get("firmwareVersion"),
                     "last_contact": (m.get("lastContact") or {}).get("timestamp")}
                    for m in d.get("batteryModules") or []],
        "warranty_end": (d.get("warranty") or {}).get("endDateTime"),
        "installer": inst.get("companyName"),
    }


def summarize_dashboard(d):
    cur, today = d.get("currently") or {}, d.get("today") or {}

    def r(v, n=1):
        return round(v, n) if isinstance(v, (int, float)) else v
    return {
        "timestamp": d.get("timestamp"),
        "storage_state": d.get("storageDeviceState"),
        "now": {"pv_w": r(cur.get("powerGenerationInW")),
                "house_w": r(cur.get("powerConsumptionInW")),
                "grid_import_w": r(cur.get("gridDrawInW")),
                "grid_export_w": r(cur.get("gridFeedInInW")),
                "battery_charge_w": r(cur.get("batteryChargeInW")),
                "battery_discharge_w": r(cur.get("batteryDischargeInW")),
                "soc_percent": r(cur.get("batteryLevelInPercent")),
                "autarky_percent": r(cur.get("selfSufficiencyInPercent")),
                "wallbox_w": r(cur.get("wallboxInW"))},
        "today": {"pv_kwh": r((today.get("powerGenerationInWh") or 0) / 1000, 3),
                  "house_kwh": r((today.get("powerConsumptionInWh") or 0) / 1000, 3),
                  "grid_import_kwh": r((today.get("gridDrawInWh") or 0) / 1000, 3),
                  "grid_export_kwh": r((today.get("gridFeedInInWh") or 0) / 1000, 3),
                  "battery_charge_kwh": r((today.get("batteryChargeInWh") or 0) / 1000, 3),
                  "battery_discharge_kwh": r((today.get("batteryDischargeInWh") or 0) / 1000, 3),
                  "autarky_percent": r(today.get("selfSufficiencyInPercent")),
                  "wallbox_kwh": r((today.get("wallboxInWh") or 0) / 1000, 3)},
        "wallboxes": d.get("numberOfWallboxes"),
    }


def measurement_rows(data):
    """Flatten cloud measurement response to rows keyed by friendly column names."""
    names = [MEASURE_KEYS.get(n, n.lower()) for n in data.get("measurements", [])]
    rows = []
    for e in data.get("timeSeries", []):
        m = e.get("measurements") or {}
        row = {"date": e.get("date"), "duration_s": m.get("durationInSeconds")}
        for n, v in zip(names, m.get("values") or []):
            row[n] = round(v, 4) if isinstance(v, float) else v
        rows.append(row)
    return names, rows


def measurement_totals(names, rows):
    tot = {}
    for n in names:
        vals = [r[n] for r in rows if isinstance(r.get(n), (int, float))]
        if not vals:
            continue
        if n in NON_SUMMABLE:
            tot[n] = {"min": round(min(vals), 2), "max": round(max(vals), 2),
                      "avg": round(sum(vals) / len(vals), 2)}
        else:
            tot[n] = round(sum(vals), 3)
    if tot.get("house_kwh"):
        self_use = tot["house_kwh"] - tot.get("grid_import_kwh", 0)
        tot["self_sufficiency_percent"] = round(100 * max(self_use, 0) / tot["house_kwh"], 1)
    return tot


def write_measurements(names, rows, path, fmt="csv", overwrite=False):
    if fmt not in ("csv", "json"):
        raise ValueError(f"Unknown format {fmt!r}; choose csv/json")
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(f"{path} exists (use --overwrite)")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        if fmt == "json":
            json.dump(rows, f, indent=2)
        else:
            w = csv.DictWriter(f, fieldnames=["date", "duration_s"] + names, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    return {"output": os.path.abspath(path), "format": fmt, "rows": len(rows),
            "file_size": os.path.getsize(path)}


def compare(local_status, cloud_dash):
    """Compare local lala.cgi live status with the cloud dashboard 'now' values."""
    now = cloud_dash["now"]
    pairs = {
        "soc_percent": (local_status["soc_percent"], now["soc_percent"]),
        "pv_w": (local_status["pv_w"], now["pv_w"]),
        "house_w": (local_status["house_w"], now["house_w"]),
        "grid_import_w": (local_status["grid_import_w"], now["grid_import_w"]),
        "grid_export_w": (local_status["grid_export_w"], now["grid_export_w"]),
        "battery_charge_w": (local_status["battery_charge_w"], now["battery_charge_w"]),
        "battery_discharge_w": (local_status["battery_discharge_w"], now["battery_discharge_w"]),
    }
    ts = cloud_dash.get("timestamp")
    age = None
    if ts and local_status.get("timestamp"):
        age = round(local_status["timestamp"] - parse_time(ts).timestamp(), 1)
    return {"cloud_timestamp": ts, "cloud_age_s": age,
            "fields": {k: {"local": a, "cloud": b,
                           "delta": round(a - b, 1) if None not in (a, b) else None}
                       for k, (a, b) in pairs.items()}}


def token_info(tok, path):
    state = sc.token_state(tok)
    out = {"token_file": path, "state": state}
    if tok and "access_token" in tok:
        claims = sc.jwt_claims(tok["access_token"])
        out.update({"user": claims.get("email") or claims.get("preferred_username"),
                    "access_expires_at": tok.get("expires_at"),
                    "refresh_expires_at": tok.get("refresh_expires_at")})
    return out


# ── settings (mein-senec.de portal) ────────────────────────────────────

RESERVE_RANGE = (0, 50)          # same bounds as the SENEC app slider / HA integration
PEAK_SHAVING_MODES = ("DEACTIVATED", "MANUAL", "AUTO")

# portal navigation flag -> setting; only settings whose flag is true (or whose read
# endpoint answers) are exposed.
FEATURE_FLAGS = {
    "emergency_reserve": "emergencyPowerVisible",
    "peak_shaving": "peakShavingVisible",
    "sg_ready": "sgReadyVisible",
    "wallbox": "wallboxVisible",
    "sockets": "steckdosenVisible",
    "heating_rod": "heizstaebeVisible",
    "maintenance_charge": "wartungsladungVisible",
    "offpeak_charging": "offpeakVisible",
}
SUPPORTED = ("emergency_reserve", "peak_shaving")


def settings_overview(plant, probes):
    """plant: portal navigation dict; probes: {setting: value | Exception}."""
    out = []
    for name, flag in FEATURE_FLAGS.items():
        visible = bool(plant.get(flag))
        res = probes.get(name)
        if name in SUPPORTED and res is not None and not isinstance(res, Exception):
            status, reason = "supported", None
        elif isinstance(res, Exception):
            status, reason = "skipped", f"read failed: {res}"
        elif name in SUPPORTED:
            status, reason = "skipped", "not probed"
        else:
            status = "skipped"
            reason = (f"portal flag {flag}=false" if not visible
                      else "no writable endpoint implemented")
        out.append({"setting": name, "status": status, "portal_flag": f"{flag}={visible}",
                    "current": None if isinstance(res, Exception) else res,
                    **({"reason": reason} if reason else {})})
    out.append({"setting": "forecast_charging", "status": "skipped",
                "portal_flag": None, "current": None,
                "reason": "app endpoint forecast-charging-settings answers HTTP 400 for this account"})
    return out


def summarize_peak_shaving(d):
    end = d.get("peakShavingEndDate")
    return {"mode": d.get("peakShavingMode"),
            "capacity_limit_percent": d.get("peakShavingCapacityLimitInPercent"),
            "end": sc.iso_utc(end / 1000) if end else None, "end_ms": end,
            "feed_in_limit_kwp_percent": d.get("einspeisebegrenzungKwpInPercent")}


def _plan(setting, system_id, plant_no, method, path, params, old, new):
    return {"setting": setting, "system_id": system_id, "plant": plant_no,
            "request": {"method": method, "url": sc.WEB_API + path, "params": params},
            "old": old, "new": new, "changed": old != new}


def plan_reserve(system_id, plant_no, current, value):
    lo, hi = RESERVE_RANGE
    try:
        v = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Reserve must be an integer percent, got {value!r}") from e
    if str(v) != str(value).strip():
        raise ValueError(f"Reserve must be an integer percent, got {value!r}")
    if not lo <= v <= hi:
        raise ValueError(f"Reserve {v}% out of range {lo}..{hi}%")
    return _plan("emergency_reserve", system_id, plant_no, "POST",
                 f"/senec/{plant_no}/emergencypower", {"reserve-in-percent": v},
                 {"reserve_percent": current}, {"reserve_percent": v})


def parse_end_time(value, now=None):
    """'HH:MM' -> next occurrence (local) in ms; ISO / relative via parse_time."""
    now = now or dt.datetime.now().astimezone()
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(value).strip())
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            raise ValueError(f"Bad end time {value!r}")
        t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if t <= now:
            t += dt.timedelta(days=1)
    else:
        t = parse_time(value, now)
    return int(t.timestamp()) * 1000


def plan_peak_shaving(system_id, plant_no, current, mode=None, capacity=None, end=None,
                      now=None):
    """Unspecified fields keep their current value (the portal always needs all three)."""
    cur = summarize_peak_shaving(current)
    mode = (mode or cur["mode"] or "").upper()
    if mode not in PEAK_SHAVING_MODES:
        raise ValueError(f"Peak shaving mode must be one of {', '.join(PEAK_SHAVING_MODES)}")
    cap = cur["capacity_limit_percent"] if capacity is None else capacity
    try:
        cap = int(cap)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Capacity limit must be an integer percent, got {capacity!r}") from e
    if not 0 <= cap <= 100:
        raise ValueError(f"Capacity limit {cap}% out of range 0..100%")
    end_ms = cur["end_ms"] if end is None else parse_end_time(end, now)
    if end_ms is None:
        raise ValueError("No end time known: pass --end HH:MM")
    if mode == "MANUAL" and end is not None:
        now_ms = (now or dt.datetime.now().astimezone()).timestamp() * 1000
        if end_ms <= now_ms:
            raise ValueError("--end must be in the future for MANUAL mode")
    return _plan("peak_shaving", system_id, plant_no, "POST",
                 f"/peakshaving/saveSettings?anlageNummer={plant_no}",
                 {"mode": mode, "capacityLimit": cap, "endzeit": end_ms},
                 {"mode": cur["mode"], "capacity_limit_percent": cur["capacity_limit_percent"],
                  "end_ms": cur["end_ms"]},
                 {"mode": mode, "capacity_limit_percent": cap, "end_ms": end_ms})


def verify(plan, read_back):
    """Compare read-back with plan['new'] (only keys present in both)."""
    keys = [k for k in plan["new"] if k in read_back]
    return all(read_back[k] == plan["new"][k] for k in keys) and bool(keys)
