"""MCP server (stdio) for SENEC.Home: read tools + a few proven, confirm-gated writes.

Configuration (env):
  SENEC_LOCAL_HOST           device URL (overrides session; default https://senec.home.arpa)
  SENEC_CLOUD_URL            mein-senec.de portal URL (default https://mein-senec.de)
  SENEC_SESSION              session JSON (host / verify_tls / timeout / lang / cloud_system)
  SENEC_ENV_FILE             credentials .env (mein-senec.de) — use an absolute path
  SENEC_USER / SENEC_PASSWORD / SENEC_TOTP_SECRET, SENEC_CLOUD_TOKEN_FILE, SENEC_CLOUD_SYSTEM
"""

import argparse
import functools
import os

from mcp.server.fastmcp import FastMCP

from senec_mcp.core import cloud as cloud_core, control as control_core, device
from senec_mcp.core.session import Session
from senec_mcp.utils import senec_backend as be
from senec_mcp.utils import senec_cloud as sc

CONFIRM_NOTE = ("WRITE TOOL. With confirm=false (default) it only returns the planned request "
                "and changes nothing. Before calling with confirm=true you MUST show the plan "
                "to the user and get their explicit approval.")

READ_TOOLS = ("status", "battery_info", "battery_cells", "pv", "grid", "system_info",
              "system_temps", "raw_get", "cloud_dashboard", "cloud_history",
              "cloud_availability", "cloud_settings_list", "cloud_reserve",
              "cloud_peak_shaving", "cloud_details")
WRITE_TOOLS = ("safe_charge", "set_reserve", "set_peak_shaving")


class Config:
    def __init__(self, host=None, env_file=None, session_file=None, token_file=None):
        self.session = Session(session_file or os.environ.get("SENEC_SESSION"))
        cfg = self.session.config
        h = (host or os.environ.get("SENEC_LOCAL_HOST") or cfg.get("host") or be.DEFAULT_HOST)
        h = h.rstrip("/")
        if not h.startswith(("http://", "https://")):
            h = "https://" + h
        self.conn = {"host": h, "verify": bool(cfg.get("verify_tls")),
                     "timeout": float(cfg.get("timeout") or be.DEFAULT_TIMEOUT)}
        self.lang = cfg.get("lang") or "DE-de"
        self.env_file = env_file or os.environ.get("SENEC_ENV_FILE")
        self.token_file = (token_file or os.environ.get("SENEC_CLOUD_TOKEN_FILE")
                           or sc.DEFAULT_TOKEN_PATH)
        self.system = os.environ.get("SENEC_CLOUD_SYSTEM") or cfg.get("cloud_system")
        self._client = None

    def creds(self):
        if self.env_file and not os.path.exists(self.env_file):
            raise FileNotFoundError(f"SENEC_ENV_FILE not found: {self.env_file}")
        return sc.resolve_credentials(env_file=self.env_file)

    def client(self):
        if self._client is None:
            self._client = sc.CloudClient(self.token_file, self.creds(),
                                          max(self.conn["timeout"], 20.0))
        return self._client

    def cloud_system(self):
        c = self.client()
        return c, cloud_core.select_system(c.systems(), self.system)

    def web(self):
        _, sysd = self.cloud_system()
        web = sc.WebClient(self.creds(), max(self.conn["timeout"], 30.0))
        n, plant = web.find_plant(sysd["controlUnitNumber"])
        return web, sysd, n, plant


def _errors(where):
    """Turn backend exceptions into clear tool errors."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except sc.CloudAuthError as e:
                raise RuntimeError(f"Authentication failed ({where}): {e}") from e
            except be.SenecError as e:
                msg = str(e)
                if where == "device" and ("reach" in msg.lower() or "timed out" in msg.lower()):
                    raise RuntimeError(f"SENEC device unreachable: {msg}") from e
                raise RuntimeError(f"SENEC {where} error: {msg}") from e
            except OSError as e:
                if isinstance(e, FileNotFoundError):
                    raise RuntimeError(str(e)) from e
                raise RuntimeError(f"SENEC {where} unreachable: {e}") from e
        return wrapper
    return deco


def _read_peak(web, n):
    s = cloud_core.summarize_peak_shaving(web.get_peak_shaving(n))
    return {"mode": s["mode"], "capacity_limit_percent": s["capacity_limit_percent"],
            "end_ms": s["end_ms"]}


def build_server(cfg=None):
    cfg = cfg or Config()
    mcp = FastMCP("senec", instructions=(
        "SENEC.Home battery storage: local device (lala.cgi) and mein-senec.de cloud. "
        "Read tools are safe. Write tools (safe_charge, set_reserve, set_peak_shaving) default "
        "to a dry run; only pass confirm=true after the user explicitly approved the plan."))

    # ── device reads ──
    @mcp.tool()
    @_errors("device")
    def status() -> dict:
        """Live power flow summary: state, SOC %, PV/house/grid/battery W, safe-charge flag."""
        return device.get_status(cfg.conn, cfg.lang)

    @mcp.tool()
    @_errors("device")
    def battery_info() -> dict:
        """Battery/BMS: per-module SOC, SOH, cycles, voltages, temperatures."""
        return device.get_battery(cfg.conn)

    @mcp.tool()
    @_errors("device")
    def battery_cells() -> dict:
        """Per-module cell voltages (mV), temperatures, spread and balancing flags."""
        return {"modules": device.get_cells(cfg.conn)}

    @mcp.tool()
    @_errors("device")
    def pv() -> dict:
        """PV strings (MPP voltage/current/power) and inverter info."""
        return device.get_pv(cfg.conn)

    @mcp.tool()
    @_errors("device")
    def grid() -> dict:
        """Grid meter: total W (import/export), frequency, per-phase values."""
        return device.get_grid(cfg.conn)

    @mcp.tool()
    @_errors("device")
    def system_info() -> dict:
        """Device id, firmware versions, capacity, power limits, device time."""
        return device.get_system(cfg.conn)

    @mcp.tool()
    @_errors("device")
    def system_temps() -> dict:
        """Battery/case/MCU temperatures and fan speeds."""
        return device.get_temps(cfg.conn)

    @mcp.tool()
    @_errors("device")
    def raw_get(section: str, vars: list[str] | None = None) -> dict:
        """READ-ONLY raw lala.cgi read of SECTION (e.g. ENERGY, BMS) with optional variable
        names; values are decoded. Never writes."""
        return {"section": section.upper(),
                "values": device.raw_get(section, vars or [], cfg.conn)}

    # ── cloud reads ──
    @mcp.tool()
    @_errors("cloud")
    def cloud_dashboard() -> dict:
        """mein-senec.de dashboard: current power flow + today's energy totals."""
        client, sysd = cfg.cloud_system()
        return {"system_id": sysd["id"],
                **cloud_core.summarize_dashboard(client.dashboard(sysd["id"]))}

    @mcp.tool()
    @_errors("cloud")
    def cloud_history(resolution: str = "HOUR", from_: str = "today", to: str = "now",
                      totals_only: bool = False) -> dict:
        """Energy history from the cloud. resolution: FIVE_MINUTES/QUARTER_HOUR/HOUR/DAY/
        MONTH/YEAR (see CLI); from_/to: ISO, now/today/yesterday or relative (6h, 7d, 2w)."""
        res = resolution.upper()
        if res not in [r.upper() for r in sc.RESOLUTIONS]:
            raise ValueError(f"resolution must be one of {', '.join(sc.RESOLUTIONS)}")
        t0, t1 = cloud_core.parse_time(from_), cloud_core.parse_time(to)
        if t1 <= t0:
            raise ValueError("'to' must be after 'from'")
        client, sysd = cfg.cloud_system()
        names, rows = cloud_core.measurement_rows(
            client.measurements(sysd["id"], res, t0, t1))
        out = {"system_id": sysd["id"], "resolution": res, "from": sc.iso_utc(t0),
               "to": sc.iso_utc(t1), "intervals": len(rows),
               "totals": cloud_core.measurement_totals(names, rows)}
        if not totals_only:
            out["rows"] = rows
        return out

    @mcp.tool()
    @_errors("cloud")
    def cloud_availability() -> dict:
        """Time span for which cloud history data exists."""
        client, sysd = cfg.cloud_system()
        d = client.availability(sysd["id"], sysd.get("timezoneText") or "Europe/Berlin")
        iso = lambda ms: sc.iso_utc(ms / 1000) if ms else None  # noqa: E731
        return {"system_id": sysd["id"], "from": iso(d.get("periodStartDateInMilliseconds")),
                "to": iso(d.get("periodEndDateInMilliseconds"))}

    @mcp.tool()
    @_errors("cloud")
    def cloud_details() -> dict:
        """Cloud system details: state, firmware, battery modules, warranty, installer."""
        client, sysd = cfg.cloud_system()
        return cloud_core.summarize_details(client.details(sysd["id"]))

    @mcp.tool()
    @_errors("cloud")
    def cloud_settings_list() -> dict:
        """All known mein-senec.de settings with support status and current values."""
        web, sysd, n, plant = cfg.web()
        probes = {}
        for name, fn in (("emergency_reserve", lambda: {"reserve_percent": web.get_reserve(n)}),
                         ("peak_shaving", lambda: _read_peak(web, n))):
            try:
                probes[name] = fn()
            except be.SenecError as e:
                probes[name] = e
        return {"system_id": sysd["id"], "plant": n,
                "settings": cloud_core.settings_overview(plant, probes)}

    @mcp.tool()
    @_errors("cloud")
    def cloud_reserve() -> dict:
        """Current emergency-power reserve (% of battery kept back)."""
        web, sysd, n, _ = cfg.web()
        return {"system_id": sysd["id"], "reserve_percent": web.get_reserve(n),
                "range": list(cloud_core.RESERVE_RANGE)}

    @mcp.tool()
    @_errors("cloud")
    def cloud_peak_shaving() -> dict:
        """Current peak-shaving mode, capacity limit and end time."""
        web, sysd, n, _ = cfg.web()
        return {"system_id": sysd["id"],
                **cloud_core.summarize_peak_shaving(web.get_peak_shaving(n))}

    # ── writes (confirm-gated) ──
    @mcp.tool(description="Start (mode='on') or stop (mode='off') a manual safety charge "
                          "(Sicherheitsladung) on the local device. " + CONFIRM_NOTE)
    @_errors("device")
    def safe_charge(mode: str, confirm: bool = False) -> dict:
        plan = control_core.plan_safe_charge(str(mode).lower())
        if not confirm:
            return {**plan, "applied": False, "dry_run": True,
                    "note": "dry run: ask the user, then call again with confirm=true"}
        old = device.get_status(cfg.conn, cfg.lang)["safe_charge_running"]
        control_core.apply(plan, cfg.conn)
        new = device.get_status(cfg.conn, cfg.lang)["safe_charge_running"]
        return {**plan, "applied": True, "old": {"safe_charge_running": old},
                "new": {"safe_charge_running": new},
                "verified": new == (plan["mode"] == "on")}

    @mcp.tool(description="Set the emergency-power reserve on mein-senec.de to percent "
                          "(integer 0..50). " + CONFIRM_NOTE)
    @_errors("cloud")
    def set_reserve(percent: int, confirm: bool = False) -> dict:
        cloud_core.plan_reserve(0, 0, None, percent)  # validate before network
        web, sysd, n, _ = cfg.web()
        plan = cloud_core.plan_reserve(sysd["id"], n, web.get_reserve(n), percent)
        if not confirm:
            return {**plan, "applied": False, "dry_run": True}
        web.set_reserve(n, plan["new"]["reserve_percent"])
        back = {"reserve_percent": web.get_reserve(n)}
        return {**plan, "applied": True, "read_back": back,
                "verified": cloud_core.verify(plan, back)}

    @mcp.tool(description="Set peak shaving on mein-senec.de. mode: DEACTIVATED/MANUAL/AUTO; "
                          "capacity: limit % 0..100; end: 'HH:MM' (next occurrence) or ISO. "
                          "Omitted fields keep their current value. " + CONFIRM_NOTE)
    @_errors("cloud")
    def set_peak_shaving(mode: str | None = None, capacity: int | None = None,
                         end: str | None = None, confirm: bool = False) -> dict:
        if mode is not None and mode.upper() not in cloud_core.PEAK_SHAVING_MODES:
            raise ValueError(f"mode must be one of {', '.join(cloud_core.PEAK_SHAVING_MODES)}")
        if capacity is not None and not 0 <= int(capacity) <= 100:
            raise ValueError(f"Capacity limit {capacity}% out of range 0..100%")
        if end is not None:
            cloud_core.parse_end_time(end)
        web, sysd, n, _ = cfg.web()
        plan = cloud_core.plan_peak_shaving(sysd["id"], n, web.get_peak_shaving(n), mode,
                                            capacity, end)
        if not confirm:
            return {**plan, "applied": False, "dry_run": True}
        p = plan["request"]["params"]
        web.set_peak_shaving(n, p["mode"], p["capacityLimit"], p["endzeit"])
        back = _read_peak(web, n)
        return {**plan, "applied": True, "read_back": back,
                "verified": cloud_core.verify(plan, back)}

    return mcp


def main(argv=None):
    ap = argparse.ArgumentParser(prog="senec-mcp", description="SENEC MCP server (stdio)")
    ap.add_argument("--host", default=None, help="Device URL (else SENEC_LOCAL_HOST / session)")
    ap.add_argument("--env-file", default=None, help="Credentials .env (else SENEC_ENV_FILE)")
    ap.add_argument("--session-file", default=None)
    ap.add_argument("--token-file", default=None)
    a = ap.parse_args(argv)
    build_server(Config(a.host, a.env_file, a.session_file, a.token_file)).run("stdio")


if __name__ == "__main__":
    main()
