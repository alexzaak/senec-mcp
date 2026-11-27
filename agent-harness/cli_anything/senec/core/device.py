"""Device data access + domain summaries (status, battery, pv, grid, system)."""

import time

from cli_anything.senec.utils import senec_backend as be

STATUS_VARS = {
    "ENERGY": ["GUI_BAT_DATA_FUEL_CHARGE", "GUI_BAT_DATA_POWER", "GUI_BAT_DATA_VOLTAGE",
               "GUI_BAT_DATA_CURRENT", "GUI_GRID_POW", "GUI_HOUSE_POW",
               "GUI_INVERTER_POWER", "STAT_STATE", "GUI_CHARGING_INFO",
               "GUI_BOOSTING_INFO", "SAFE_CHARGE_RUNNING"],
}

_STATE_CACHE = {}


def decode_section(raw_section):
    out = {}
    for k, v in raw_section.items():
        d = be.decode_value(v)
        if isinstance(d, list):
            d = [x for x in d if x != "END_OF_ARRAY"]
        out[k] = d
    return out


def decode_all(raw):
    return {s: decode_section(v) if isinstance(v, dict) else v for s, v in raw.items()}


def fetch(sections, conn):
    raw = be.read_sections(sections, **conn)
    missing = [s for s, v in raw.items() if not isinstance(v, dict)]
    if missing:
        raise be.SenecError(f"Device does not know section(s): {', '.join(missing)}")
    return raw, decode_all(raw)


def state_names(conn, lang="DE-de"):
    key = (conn["host"], lang)
    if key not in _STATE_CACHE:
        _STATE_CACHE[key] = be.fetch_state_names(conn["host"], lang, conn["verify"],
                                                 conn["timeout"])
    return _STATE_CACHE[key]


# ── pure summarizers (decoded data → dict) ─────────────────────────────

def summarize_status(energy, names=None):
    grid = energy.get("GUI_GRID_POW", 0.0)
    bat = energy.get("GUI_BAT_DATA_POWER", 0.0)
    code = energy.get("STAT_STATE")
    return {
        "state_code": code,
        "state": (names or {}).get(code, f"STATE_{code}"),
        "soc_percent": round(energy.get("GUI_BAT_DATA_FUEL_CHARGE", 0.0), 1),
        "pv_w": round(energy.get("GUI_INVERTER_POWER", 0.0), 1),
        "house_w": round(energy.get("GUI_HOUSE_POW", 0.0), 1),
        "grid_w": round(grid, 1),
        "grid_import_w": round(max(grid, 0.0), 1),
        "grid_export_w": round(max(-grid, 0.0), 1),
        "battery_w": round(bat, 1),
        "battery_charge_w": round(max(bat, 0.0), 1),
        "battery_discharge_w": round(max(-bat, 0.0), 1),
        "battery_voltage_v": round(energy.get("GUI_BAT_DATA_VOLTAGE", 0.0), 2),
        "battery_current_a": round(energy.get("GUI_BAT_DATA_CURRENT", 0.0), 2),
        "safe_charge_running": bool(energy.get("SAFE_CHARGE_RUNNING", 0)),
    }


def _per_module(bms, key, n):
    v = bms.get(key, [])
    return v[:n] if isinstance(v, list) else v


def summarize_battery(bms, energy=None):
    n = int(bms.get("MODULE_COUNT") or bms.get("NR_INSTALLED") or 0)
    modules = []
    for i in range(n):
        def g(k, i=i):
            v = bms.get(k)
            return v[i] if isinstance(v, list) and i < len(v) else None
        modules.append({
            "index": i, "serial": g("SERIAL"), "soc_percent": g("SOC"),
            "soh_percent": g("SOH"), "cycles": g("CYCLES"),
            "voltage_v": g("VOLTAGE"), "current_a": g("CURRENT"),
            "min_cell_mv": g("MIN_CELL_VOLTAGE"), "max_cell_mv": g("MAX_CELL_VOLTAGE"),
            "temp_min_c": g("TEMP_MIN"), "temp_max_c": g("TEMP_MAX"),
            "charged_energy": g("CHARGED_ENERGY"), "discharged_energy": g("DISCHARGED_ENERGY"),
            "firmware": g("FW"),
        })
    out = {
        "module_count": n,
        "system_soc_percent": (bms["SYSTEM_SOC"] / 10) if "SYSTEM_SOC" in bms else None,
        "system_soh_percent": (bms["SYSTEM_SOH"] / 10) if "SYSTEM_SOH" in bms else None,
        "bms_status": bms.get("BMS_STATUS"), "bms_error": bms.get("ERROR"),
        "modules": modules,
    }
    if energy:
        out["fuel_charge_percent"] = energy.get("GUI_BAT_DATA_FUEL_CHARGE")
        out["power_w"] = energy.get("GUI_BAT_DATA_POWER")
    return out


def summarize_cells(bms):
    n = int(bms.get("MODULE_COUNT") or 0)
    out = []
    for i, letter in enumerate("ABCD"[:n]):
        volts = bms.get(f"CELL_VOLTAGES_MODULE_{letter}", [])
        temps = bms.get(f"CELL_TEMPERATURES_MODULE_{letter}", [])
        active = [v for v in volts if v]
        out.append({
            "module": letter, "cell_mv": volts, "temps_c": temps,
            "min_mv": min(active) if active else None,
            "max_mv": max(active) if active else None,
            "spread_mv": (max(active) - min(active)) if active else None,
            "balancing": (bms.get("CELL_BALANCE_STATUS") or [0] * n)[i],
        })
    return out


def summarize_pv(pv):
    n = int(pv.get("MPP_AVAIL") or len(pv.get("MPP_POWER", [])))
    strings = [{"mpp": i + 1,
                "voltage_v": round(pv["MPP_VOL"][i], 1),
                "current_a": round(pv["MPP_CUR"][i], 2),
                "power_w": round(pv["MPP_POWER"][i], 1)}
               for i in range(min(n, len(pv.get("MPP_POWER", []))))]
    return {"total_w": round(sum(s["power_w"] for s in strings), 1),
            "power_ratio_percent": pv.get("POWER_RATIO"),
            "strings": strings,
            "inverters": [{"model": m, "serial": s, "version": v}
                          for m, s, v in zip(pv.get("INTERNAL_MD_MODEL", []),
                                             pv.get("INTERNAL_MD_SERIAL", []),
                                             pv.get("INTERNAL_MD_VERSION", []))]}


def summarize_grid(pm):
    phases = [{"phase": f"L{i + 1}", "power_w": round(p, 1), "voltage_v": round(u, 1),
               "current_a": round(c, 2)}
              for i, (p, u, c) in enumerate(zip(pm.get("P_AC", []), pm.get("U_AC", []),
                                                pm.get("I_AC", [])))]
    total = pm.get("P_TOTAL", 0.0)
    return {"total_w": round(total, 1), "direction": "import" if total >= 0 else "export",
            "frequency_hz": round(pm.get("FREQ", 0.0), 3), "phases": phases}


def summarize_system(wiz, fac, upd, rtc):
    mac = wiz.get("MAC_ADDRESS_BYTES", [])
    return {
        "device_id": fac.get("DEVICE_ID"),
        "firmware_version": wiz.get("FIRMWARE_VERSION"),
        "application_version": wiz.get("APPLICATION_VERSION"),
        "interface_version": wiz.get("INTERFACE_VERSION"),
        "npu_image_version": upd.get("NPU_IMAGE_VERSION"),
        "update_available": bool(upd.get("UPDATE_AVAILABLE")),
        "mac_address": ":".join(f"{b:02X}" for b in mac) if mac else None,
        "design_capacity_wh": fac.get("DESIGN_CAPACITY"),
        "max_charge_power_w": fac.get("MAX_CHARGE_POWER_DC"),
        "max_discharge_power_w": fac.get("MAX_DISCHARGE_POWER_DC"),
        "peak_pv_power_w": wiz.get("PWRCFG_PEAK_PV_POWER"),
        "device_time_utc": rtc.get("WEB_TIME"),
        "utc_offset_min": rtc.get("UTC_OFFSET"),
    }


def summarize_temps(temp, fan):
    return {"battery_c": temp.get("BATTERY_TEMP"), "case_c": temp.get("CASE_TEMP"),
            "mcu_c": temp.get("MCU_TEMP"), "fan_inv_lv": fan.get("INV_LV"),
            "fan_inv_hv": fan.get("INV_HV")}


# ── live fetch wrappers ────────────────────────────────────────────────

def get_status(conn, lang="DE-de"):
    raw = be.query({k: {n: "" for n in v} for k, v in STATUS_VARS.items()}, **conn)
    energy = decode_section(raw["ENERGY"])
    try:
        names = state_names(conn, lang)
    except be.SenecError:
        names = {}
    out = summarize_status(energy, names)
    out["timestamp"] = time.time()
    return out


def get_battery(conn):
    _, d = fetch(["BMS", "ENERGY"], conn)
    return summarize_battery(d["BMS"], d["ENERGY"])


def get_cells(conn):
    _, d = fetch(["BMS"], conn)
    return summarize_cells(d["BMS"])


def get_pv(conn):
    _, d = fetch(["PV1"], conn)
    return summarize_pv(d["PV1"])


def get_grid(conn):
    _, d = fetch(["PM1OBJ1"], conn)
    return summarize_grid(d["PM1OBJ1"])


def get_system(conn):
    _, d = fetch(["WIZARD", "FACTORY", "SYS_UPDATE", "RTC"], conn)
    return summarize_system(d["WIZARD"], d["FACTORY"], d["SYS_UPDATE"], d["RTC"])


def get_temps(conn):
    _, d = fetch(["TEMPMEASURE", "FAN_SPEED"], conn)
    return summarize_temps(d["TEMPMEASURE"], d["FAN_SPEED"])


def get_wallbox(conn):
    _, d = fetch(["WALLBOX"], conn)
    return d["WALLBOX"]


def list_sections(conn):
    raw = be.read_sections(be.KNOWN_SECTIONS, **conn)
    return [{"section": s, "variables": len(v) if isinstance(v, dict) else 0,
             "available": isinstance(v, dict)} for s, v in raw.items()]


def raw_get(section, names, conn, decode=True):
    section = section.upper()
    raw = (be.read_vars(section, [n.upper() for n in names], **conn) if names
           else be.read_sections([section], **conn))
    if not isinstance(raw.get(section), dict):
        raise be.SenecError(f"Unknown section {section}: {raw.get(section)}")
    missing = [k for k, v in raw[section].items() if v == "VARIABLE_NOT_FOUND"]
    if missing:
        raise be.SenecError(f"Unknown variable(s) in {section}: {', '.join(missing)}")
    return decode_section(raw[section]) if decode else raw[section]
