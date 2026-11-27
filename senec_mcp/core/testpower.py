"""Installer test charge / discharge ("Testleistung (W)" + Laden / Entladen).

Reverse-engineered from the device's ksetup1.html (setup page, installer only):
  start: ENERGY.GRID_POWER_OFFSET = [fl(P/3)] * 3 (per phase; charge -W/3,
         discharge +W/3) and ENERGY.TEST_CHARGE_ENABLE = u8_01 (one request)
  stop:  ENERGY.TEST_CHARGE_ENABLE = u8_00 and GRID_POWER_OFFSET = [fl 0] * 3
  state: ENERGY.GUI_TEST_CHARGE_STAT / GUI_TEST_DISCHARGE_STAT (1 = running)
The UI only checks "value is a float > 0"; it has no timeout, so the CLI adds
hardware caps, a SOC guard and --duration with guaranteed stop.
"""

import time

from senec_mcp.core import installer
from senec_mcp.utils import senec_backend as be

MODES = ("charge", "discharge")
DEFAULT_MIN_SOC = 10.0
LIMIT_VARS = {"charge": "MAX_CHARGE_POWER_DC", "discharge": "MAX_DISCHARGE_POWER_DC"}
STATUS_VARS = ["TEST_CHARGE_ENABLE", "GRID_POWER_OFFSET", "GUI_TEST_CHARGE_STAT",
               "GUI_TEST_DISCHARGE_STAT", "STAT_STATE", "GUI_BAT_DATA_FUEL_CHARGE",
               "GUI_BAT_DATA_POWER", "GUI_GRID_POW", "GUI_HOUSE_POW", "GUI_INVERTER_POWER"]
ZERO_OFFSET = ["fl_00000000"] * 3


def parse_watts(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Test power must be a positive integer (W), got {value!r}")
    if f != int(f) or f <= 0:
        raise ValueError(f"Test power must be a positive integer (W), got {value!r}")
    return int(f)


def offsets(mode, watts):
    per_phase = watts / 3.0
    return [be.encode_value("fl", -per_phase if mode == "charge" else per_phase)] * 3


def plan_start(mode, watts, limits, soc=None, min_soc=DEFAULT_MIN_SOC, running=None):
    """Validate and build the start request. limits = {MAX_CHARGE_POWER_DC: W, ...}."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    watts = parse_watts(watts)
    cap = limits.get(LIMIT_VARS[mode])
    if not cap:
        raise be.SenecError(f"Hardware limit FACTORY.{LIMIT_VARS[mode]} unavailable")
    if watts > cap:
        raise ValueError(f"{watts} W exceeds hardware {mode} limit {int(cap)} W "
                         f"(FACTORY.{LIMIT_VARS[mode]})")
    if mode == "discharge":
        if soc is None:
            raise be.SenecError("SOC unknown; refusing discharge test")
        if soc <= min_soc:
            raise ValueError(f"SOC {soc} % is at/below minimum {min_soc} % for a "
                             "discharge test (override with --min-soc)")
    if running and running != mode:
        raise ValueError(f"A {running} test is running; run `setup test-stop` first")
    return {"action": f"test-{mode}", "mode": mode, "watts": watts, "limit_w": int(cap),
            "soc_percent": soc, "min_soc_percent": min_soc if mode == "discharge" else None,
            "request": {"ENERGY": {"GRID_POWER_OFFSET": offsets(mode, watts),
                                   "TEST_CHARGE_ENABLE": "u8_01"}}}


def plan_stop():
    return {"action": "test-stop",
            "request": {"ENERGY": {"TEST_CHARGE_ENABLE": "u8_00",
                                   "GRID_POWER_OFFSET": list(ZERO_OFFSET)}}}


def summarize(energy, names=None):
    off = energy.get("GRID_POWER_OFFSET") or [0.0, 0.0, 0.0]
    total = round(sum(off), 1)
    charge, discharge = bool(energy.get("GUI_TEST_CHARGE_STAT")), \
        bool(energy.get("GUI_TEST_DISCHARGE_STAT"))
    code = energy.get("STAT_STATE")
    return {"running": charge or discharge or bool(energy.get("TEST_CHARGE_ENABLE")),
            "mode": "charge" if charge else "discharge" if discharge else
            ("pending" if energy.get("TEST_CHARGE_ENABLE") else None),
            "enable": energy.get("TEST_CHARGE_ENABLE"),
            "test_power_w": abs(total), "grid_offset_w": total,
            "state_code": code, "state": (names or {}).get(code, f"STATE_{code}"),
            "soc_percent": energy.get("GUI_BAT_DATA_FUEL_CHARGE"),
            "battery_w": round(energy.get("GUI_BAT_DATA_POWER") or 0.0, 1),
            "grid_w": round(energy.get("GUI_GRID_POW") or 0.0, 1),
            "house_w": round(energy.get("GUI_HOUSE_POW") or 0.0, 1),
            "pv_w": round(energy.get("GUI_INVERTER_POWER") or 0.0, 1)}


# ── live ───────────────────────────────────────────────────────────────

def require_login(conn):
    st = installer.get_state(conn)
    if not st["logged_in"]:
        raise be.SenecError("Installer login required: run `setup login` first")
    return st


def read_limits(conn):
    raw = be.read_vars("FACTORY", list(LIMIT_VARS.values()), **conn)["FACTORY"]
    return {k: be.decode_value(v) for k, v in raw.items()}


def get_status(conn, names=None):
    raw = be.read_vars("ENERGY", STATUS_VARS, **conn)["ENERGY"]
    if raw.get("TEST_CHARGE_ENABLE") == "VARIABLE_NOT_FOUND":
        raise be.SenecError("Test variables not visible: installer login required "
                            "(`setup login`)")
    return summarize({k: be.decode_value(v) for k, v in raw.items()}, names)


def apply(plan, conn):
    resp = be.query(plan["request"], **conn)
    bad = {k: v for k, v in resp.get("ENERGY", {}).items()
           if isinstance(v, str) and v in be.ERROR_MARKERS}
    if bad:
        raise be.SenecError(f"Device rejected {plan['action']}: {bad}")
    return resp


def run_for(plan, conn, duration, poll=5.0, on_tick=None, sleep=time.sleep,
            clock=time.monotonic, names=None):
    """Start, sample every `poll` s for `duration` s, ALWAYS stop (also on Ctrl-C)."""
    samples, interrupted = [], False
    t0 = clock()
    try:
        apply(plan, conn)
        while clock() - t0 < duration:
            sleep(min(poll, max(0.0, duration - (clock() - t0))))
            s = get_status(conn, names)
            s["t_s"] = round(clock() - t0, 1)
            samples.append(s)
            if on_tick:
                on_tick(s)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop(conn, sleep)
    return {"samples": samples, "interrupted": interrupted,
            "elapsed_s": round(clock() - t0, 1)}


def stop(conn, sleep=time.sleep, attempts=3):
    """Send the stop request, retrying on transient errors."""
    for i in range(attempts):
        try:
            return apply(plan_stop(), conn)
        except be.SenecError:
            if i == attempts - 1:
                raise
            sleep(1.0)
