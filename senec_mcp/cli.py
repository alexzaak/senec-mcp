"""senec: agent-native CLI for the SENEC.Home battery storage web UI."""

import json
import shlex
import sys
import time

import click

from senec_mcp import __version__
from senec_mcp.core import cloud as cloud_core, control as control_core, device, export, installer, project, testpower
from senec_mcp.core.session import Session
from senec_mcp.utils import senec_backend as be
from senec_mcp.utils import senec_cloud as sc


class Ctx:
    def __init__(self, json_mode, session, host_override):
        self.json = json_mode
        self.session = session
        self.host_override = host_override

    @property
    def conn(self):
        c = self.session.conn()
        if self.host_override:
            c["host"] = self.host_override.rstrip("/")
        return c

    @property
    def lang(self):
        return self.session.config.get("lang", "DE-de")


def emit(ctx, data, human=None):
    if ctx.json:
        click.echo(json.dumps(data, indent=2, default=str))
    elif human:
        human(data)
    else:
        _print_kv(data)


def _print_kv(data, indent=0):
    pad = "  " * indent
    if isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                _print_kv(item, indent)
                click.echo(pad + "-")
            else:
                click.echo(f"{pad}- {item}")
        return
    for k, v in data.items():
        if isinstance(v, (dict, list)) and v and isinstance(next(iter(v)) if isinstance(v, dict) else v[0], (dict, list)):
            click.echo(f"{pad}{k}:")
            _print_kv(v, indent + 1)
        else:
            click.echo(f"{pad}{k}: {v}")


def fail(ctx, msg, code=1):
    if ctx.json:
        click.echo(json.dumps({"error": msg}))
    else:
        click.echo(f"Error: {msg}", err=True)
    sys.exit(code)


def handle(fn):
    """Decorator: turn domain exceptions into clean CLI errors."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        ctx = click.get_current_context().find_object(Ctx)
        try:
            return fn(*a, **kw)
        except (be.SenecError, ValueError, FileExistsError, FileNotFoundError) as e:
            fail(ctx, str(e))
    return wrapper


pass_ctx = click.make_pass_decorator(Ctx)


@click.group(invoke_without_command=True)
@click.option("--json", "json_mode", is_flag=True, help="Machine-readable JSON output.")
@click.option("--host", envvar="SENEC_HOST", default=None,
              help="Device URL (overrides session), e.g. https://senec.home.arpa")
@click.option("--session-file", envvar="SENEC_SESSION", default=None,
              help="Session JSON path (default ~/.senec/session.json).")
@click.version_option(__version__)
@click.pass_context
def cli(ctx, json_mode, host, session_file):
    """Stateful CLI for the SENEC.Home storage system (lala.cgi API)."""
    ctx.obj = Ctx(json_mode, Session(session_file), host)
    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


# ── status ─────────────────────────────────────────────────────────────

def _human_status(s):
    click.echo(f"State:    {s['state']} ({s['state_code']})")
    click.echo(f"SOC:      {s['soc_percent']} %")
    click.echo(f"PV:       {s['pv_w']:.0f} W")
    click.echo(f"House:    {s['house_w']:.0f} W")
    g = s["grid_w"]
    click.echo(f"Grid:     {abs(g):.0f} W {'import' if g >= 0 else 'export'}")
    b = s["battery_w"]
    click.echo(f"Battery:  {abs(b):.0f} W {'charging' if b >= 0 else 'discharging'}"
               f"  ({s['battery_voltage_v']} V, {s['battery_current_a']} A)")


@cli.command()
@pass_ctx
@handle
def status(ctx):
    """Live power flow, SOC and system state."""
    emit(ctx, device.get_status(ctx.conn, ctx.lang), _human_status)


# ── battery / pv / grid / system / wallbox ─────────────────────────────

@cli.group()
def battery():
    """Battery / BMS information."""


@battery.command("info")
@pass_ctx
@handle
def battery_info(ctx):
    """Per-module SOC, SOH, cycles, voltages."""
    emit(ctx, device.get_battery(ctx.conn))


@battery.command("cells")
@pass_ctx
@handle
def battery_cells(ctx):
    """Cell voltages (mV) and temperatures per module."""
    emit(ctx, device.get_cells(ctx.conn))


@cli.group()
def pv():
    """Photovoltaic generator information."""


@pv.command("info")
@pass_ctx
@handle
def pv_info(ctx):
    """MPP tracker strings and inverter details."""
    emit(ctx, device.get_pv(ctx.conn))


@cli.group()
def grid():
    """Grid power meter."""


@grid.command("info")
@pass_ctx
@handle
def grid_info(ctx):
    """Per-phase power/voltage/current and frequency."""
    emit(ctx, device.get_grid(ctx.conn))


@cli.group()
def system():
    """System info, temperatures, state names."""


@system.command("info")
@pass_ctx
@handle
def system_info(ctx):
    """Versions, device id, capacity, update status."""
    emit(ctx, device.get_system(ctx.conn))


@system.command("temps")
@pass_ctx
@handle
def system_temps(ctx):
    """Battery/case/MCU temperatures and fan speeds."""
    emit(ctx, device.get_temps(ctx.conn))


@system.command("states")
@pass_ctx
@handle
def system_states(ctx):
    """System state code → name table (from the device's language file)."""
    names = device.state_names(ctx.conn, ctx.lang)
    emit(ctx, {str(k): v for k, v in sorted(names.items())})


@cli.group()
def wallbox():
    """Wallbox information."""


@wallbox.command("info")
@pass_ctx
@handle
def wallbox_info(ctx):
    """Raw decoded WALLBOX section."""
    emit(ctx, device.get_wallbox(ctx.conn))


# ── raw ────────────────────────────────────────────────────────────────

@cli.group()
def raw():
    """Low-level lala.cgi access."""


@raw.command("sections")
@pass_ctx
@handle
def raw_sections(ctx):
    """List known sections and their variable counts."""
    emit(ctx, device.list_sections(ctx.conn))


@raw.command("get")
@click.argument("section")
@click.argument("variables", nargs=-1)
@click.option("--undecoded", is_flag=True, help="Return raw typed hex strings.")
@pass_ctx
@handle
def raw_get(ctx, section, variables, undecoded):
    """Read a SECTION (all vars) or selected VARIABLES."""
    emit(ctx, device.raw_get(section, list(variables), ctx.conn, decode=not undecoded))


@raw.command("decode")
@click.argument("value")
@pass_ctx
@handle
def raw_decode(ctx, value):
    """Decode a typed value like fl_42C80000 (offline)."""
    emit(ctx, {"raw": value, "type": be.value_type(value), "value": be.decode_value(value)})


@raw.command("encode")
@click.argument("type_prefix", type=click.Choice(["fl", "u8", "u1", "u3", "u6", "i8", "i1", "i3", "st"]))
@click.argument("value")
@pass_ctx
@handle
def raw_encode(ctx, type_prefix, value):
    """Encode VALUE as SENEC typed string (offline)."""
    emit(ctx, {"type": type_prefix, "value": value, "raw": be.encode_value(type_prefix, value)})


# ── snapshot (project) ─────────────────────────────────────────────────

@cli.group()
def snapshot():
    """Capture / inspect / diff / export device snapshots (project files)."""


@snapshot.command("capture")
@click.option("-o", "--output", required=True, help="Snapshot JSON path.")
@click.option("-s", "--section", "sections", multiple=True, help="Sections (default: core set).")
@click.option("--overwrite", is_flag=True)
@pass_ctx
@handle
def snapshot_capture(ctx, output, sections, overwrite):
    """Read the device and save a snapshot file."""
    try:
        names = device.state_names(ctx.conn, ctx.lang)
    except be.SenecError:
        names = None
    snap = project.capture(ctx.conn, list(sections) or None, names)
    res = project.save(snap, output, overwrite)
    ctx.session.last_snapshot = res["path"]
    ctx.session.record("snapshot capture", path=res["path"])
    ctx.session.save()
    emit(ctx, {**res, **project.info(snap)})


@snapshot.command("info")
@click.argument("path", required=False)
@pass_ctx
@handle
def snapshot_info(ctx, path):
    """Summarize a snapshot (defaults to the last captured)."""
    path = path or ctx.session.last_snapshot
    if not path:
        raise ValueError("No snapshot given and none captured in this session")
    emit(ctx, {"path": path, **project.info(project.load(path))})


@snapshot.command("diff")
@click.argument("old")
@click.argument("new")
@click.option("--tolerance", default=0.0, type=float, help="Ignore numeric deltas <= this.")
@click.option("--section", default=None, help="Only keys of this section.")
@pass_ctx
@handle
def snapshot_diff(ctx, old, new, tolerance, section):
    """Compare two snapshots."""
    d = project.diff(project.load(old), project.load(new), tolerance)
    if section:
        d["changes"] = [c for c in d["changes"] if c["key"].startswith(section.upper() + ".")]
        d["changed"] = len(d["changes"])
    emit(ctx, d)


@snapshot.command("export")
@click.argument("path")
@click.option("-o", "--output", required=True)
@click.option("-f", "--format", "fmt", type=click.Choice(export.FORMATS), default="csv")
@click.option("--overwrite", is_flag=True)
@pass_ctx
@handle
def snapshot_export(ctx, path, output, fmt, overwrite):
    """Flatten a snapshot to CSV/JSON key-value table."""
    emit(ctx, export.export_snapshot(project.load(path), output, fmt, overwrite))


# ── monitor ────────────────────────────────────────────────────────────

@cli.group()
def monitor():
    """Time-series sampling of live status."""


@monitor.command("sample")
@click.option("-n", "--count", default=5, type=int)
@click.option("-i", "--interval", default=2.0, type=float, help="Seconds between samples.")
@click.option("-o", "--output", default=None, help="Write samples to file.")
@click.option("-f", "--format", "fmt", type=click.Choice(export.FORMATS), default="csv")
@click.option("--overwrite", is_flag=True)
@pass_ctx
@handle
def monitor_sample(ctx, count, interval, output, fmt, overwrite):
    """Sample status COUNT times and report min/max/avg."""
    def progress(i, n, s):
        if not ctx.json:
            click.echo(f"[{i}/{n}] SOC {s['soc_percent']}%  PV {s['pv_w']:.0f}W  "
                       f"house {s['house_w']:.0f}W  grid {s['grid_w']:.0f}W  bat {s['battery_w']:.0f}W")
    samples = export.sample(ctx.conn, count, interval, ctx.lang, progress)
    res = export.stats(samples)
    if output:
        res["file"] = export.write_samples(samples, output, fmt, overwrite)
    if ctx.json:
        res["data"] = samples
    emit(ctx, res)


# ── control (writes) ───────────────────────────────────────────────────

@cli.group()
def control():
    """Write operations (require --yes; use --dry-run to preview)."""


def _run_plan(ctx, plan, yes, dry_run):
    if dry_run or not yes:
        plan = {**plan, "applied": False,
                "note": "dry run" if dry_run else "not applied: pass --yes to write to the device"}
        emit(ctx, plan)
        if not dry_run:
            sys.exit(2)
        return
    res = control_core.apply(plan, ctx.conn)
    ctx.session.record("control", section=plan["section"], variable=plan["variable"],
                       typed=plan["typed"])
    ctx.session.save()
    emit(ctx, res)


@control.command("safe-charge")
@click.argument("mode", type=click.Choice(["on", "off"]))
@click.option("--yes", is_flag=True, help="Actually write to the device.")
@click.option("--dry-run", is_flag=True)
@pass_ctx
@handle
def control_safe_charge(ctx, mode, yes, dry_run):
    """Start (on) or stop (off) a manual safety charge (Sicherheitsladung)."""
    _run_plan(ctx, control_core.plan_safe_charge(mode), yes, dry_run)


@control.command("set")
@click.argument("section")
@click.argument("variable")
@click.argument("value")
@click.option("--type", "type_prefix", default=None,
              type=click.Choice(["fl", "u8", "u1", "u3", "u6", "i8", "i1", "i3", "st"]),
              help="Value type (default: inferred from current value).")
@click.option("--yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
@pass_ctx
@handle
def control_set(ctx, section, variable, value, type_prefix, yes, dry_run):
    """Write SECTION.VARIABLE = VALUE (expert use)."""
    cur = control_core.current_raw(section, variable, ctx.conn)
    _run_plan(ctx, control_core.plan_set(section, variable, value, type_prefix, cur), yes, dry_run)


# ── cloud (mein-senec.de) ──────────────────────────────────────────────

class CloudCtx:
    def __init__(self, token_file, env_file, user, password, totp_secret, system):
        self.token_file = token_file or sc.DEFAULT_TOKEN_PATH
        self.env_file = env_file
        self.user, self.password, self.totp_secret = user, password, totp_secret
        self.system = system
        self._client = None

    def creds(self):
        return sc.resolve_credentials(self.user, self.password, self.totp_secret, self.env_file)

    def client(self, timeout):
        if self._client is None:
            self._client = sc.CloudClient(self.token_file, self.creds(), max(timeout, 20.0))
        return self._client


def _cloud(ctx):
    return click.get_current_context().find_object(CloudCtx)


def _cloud_system(ctx):
    """Return (client, system dict) honoring --system / session cloud_system."""
    cc = _cloud(ctx)
    client = cc.client(ctx.conn["timeout"])
    sel = cc.system or ctx.session.config.get("cloud_system")
    return client, cloud_core.select_system(client.systems(), sel)


@cli.group()
@click.option("--token-file", envvar="SENEC_CLOUD_TOKEN_FILE", default=None,
              help="Token cache (default ~/.senec/cloud_token.json).")
@click.option("--env-file", default=None,
              help="Credentials file (default ./.env if present).")
@click.option("--user", envvar="SENEC_USER", default=None, help="mein-senec.de e-mail.")
@click.option("--password", envvar="SENEC_PASSWORD", default=None)
@click.option("--totp-secret", envvar="SENEC_TOTP_SECRET", default=None,
              help="Base32 2FA secret (if 2FA is enabled).")
@click.option("--system", default=None,
              help="System id / control unit number / index (default: session or first).")
@click.pass_context
def cloud(cctx, token_file, env_file, user, password, totp_secret, system):
    """mein-senec.de cloud: login, systems, dashboard, history (read-only)."""
    cctx.obj = CloudCtx(token_file, env_file, user, password, totp_secret, system)


def _ctx():
    return click.get_current_context().find_root().obj


def cloud_cmd(fn):
    """Decorator: inject root Ctx and handle errors for cloud subcommands."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        ctx = _ctx()
        try:
            return fn(ctx, *a, **kw)
        except (be.SenecError, ValueError, FileExistsError, FileNotFoundError) as e:
            fail(ctx, str(e))
    return wrapper


@cloud.command("login")
@click.option("--otp", default=None, help="One-time 2FA code (if no --totp-secret).")
@cloud_cmd
def cloud_login(ctx, otp):
    """Log in via sso.senec.com and cache tokens (0600)."""
    cc = _cloud(ctx)
    cr = cc.creds()
    tok = sc.login(cr["user"], cr["password"], cr["totp_secret"], otp,
                   max(ctx.conn["timeout"], 20.0))
    sc.save_token(tok, cc.token_file)
    ctx.session.record("cloud login")
    ctx.session.save()
    emit(ctx, {"logged_in": True, **cloud_core.token_info(tok, cc.token_file)})


@cloud.command("logout")
@cloud_cmd
def cloud_logout(ctx):
    """Delete the cached cloud token."""
    cc = _cloud(ctx)
    emit(ctx, {"token_file": cc.token_file, "deleted": sc.delete_token(cc.token_file)})


@cloud.command("whoami")
@cloud_cmd
def cloud_whoami(ctx):
    """Show cached token state (offline)."""
    cc = _cloud(ctx)
    emit(ctx, cloud_core.token_info(sc.load_token(cc.token_file), cc.token_file))


@cloud.command("systems")
@cloud_cmd
def cloud_systems(ctx):
    """List systems registered to the account."""
    client = _cloud(ctx).client(ctx.conn["timeout"])
    emit(ctx, cloud_core.summarize_systems(client.systems()))


@cloud.command("use")
@click.argument("system")
@cloud_cmd
def cloud_use(ctx, system):
    """Persist default cloud SYSTEM (id/serial/index; 'auto' to clear)."""
    if system.lower() not in ("auto", "none"):
        client = _cloud(ctx).client(ctx.conn["timeout"])
        system = str(cloud_core.select_system(client.systems(), system)["id"])
    cfg = ctx.session.set_config("cloud_system", system)
    ctx.session.save()
    emit(ctx, cfg)


@cloud.command("details")
@click.option("--full", is_flag=True, help="Return the unmodified API payload.")
@cloud_cmd
def cloud_details(ctx, full):
    """Hardware, firmware, battery modules, warranty, installer."""
    client, sysd = _cloud_system(ctx)
    d = client.details(sysd["id"])
    emit(ctx, d if full else cloud_core.summarize_details(d))


@cloud.command("status")
@cloud_cmd
def cloud_status(ctx):
    """Operating state as seen by the cloud."""
    client, sysd = _cloud_system(ctx)
    emit(ctx, {"system_id": sysd["id"], **(client.status(sysd["id"]) or {})})


@cloud.command("abilities")
@cloud_cmd
def cloud_abilities(ctx):
    """Booked feature packages (peak shaving, SG-ready, …)."""
    client, sysd = _cloud_system(ctx)
    emit(ctx, {"system_id": sysd["id"], **(client.abilities(sysd["id"]) or {})})


@cloud.command("dashboard")
@cloud_cmd
def cloud_dashboard(ctx):
    """Current power flow + today's energy totals from the cloud."""
    client, sysd = _cloud_system(ctx)
    emit(ctx, cloud_core.summarize_dashboard(client.dashboard(sysd["id"])))


@cloud.command("availability")
@click.option("--tz", default=None, help="Timezone (default: system timezone).")
@cloud_cmd
def cloud_availability(ctx, tz):
    """Time span for which history data exists."""
    client, sysd = _cloud_system(ctx)
    d = client.availability(sysd["id"], tz or sysd.get("timezoneText") or "Europe/Berlin")
    iso = lambda ms: sc.iso_utc(ms / 1000) if ms else None  # noqa: E731
    emit(ctx, {"system_id": sysd["id"],
               "from": iso(d.get("periodStartDateInMilliseconds")),
               "to": iso(d.get("periodEndDateInMilliseconds"))})


@cloud.command("history")
@click.option("-r", "--resolution", type=click.Choice(sc.RESOLUTIONS, case_sensitive=False),
              default="HOUR", show_default=True)
@click.option("--from", "start", default="today", show_default=True,
              help="ISO time, now/today/yesterday or relative (6h, 7d, 2w).")
@click.option("--to", "end", default="now", show_default=True)
@click.option("-o", "--output", default=None, help="Write rows to file.")
@click.option("-f", "--format", "fmt", type=click.Choice(export.FORMATS), default="csv")
@click.option("--overwrite", is_flag=True)
@click.option("--totals-only", is_flag=True, help="Omit per-interval rows from output.")
@cloud_cmd
def cloud_history(ctx, resolution, start, end, output, fmt, overwrite, totals_only):
    """Energy history (kWh per interval, SOC, autarky) from the cloud."""
    t0, t1 = cloud_core.parse_time(start), cloud_core.parse_time(end)
    if t1 <= t0:
        raise ValueError("--to must be after --from")
    client, sysd = _cloud_system(ctx)
    data = client.measurements(sysd["id"], resolution.upper(), t0, t1)
    names, rows = cloud_core.measurement_rows(data)
    res = {"system_id": sysd["id"], "resolution": resolution.upper(),
           "from": sc.iso_utc(t0), "to": sc.iso_utc(t1), "intervals": len(rows),
           "totals": cloud_core.measurement_totals(names, rows)}
    if output:
        res["file"] = cloud_core.write_measurements(names, rows, output, fmt, overwrite)
    if not totals_only and (ctx.json or not output):
        res["rows"] = rows
    if not ctx.json and "rows" in res:
        rows_out = res.pop("rows")
        _print_kv(res)
        click.echo("date                  " + "  ".join(n[:12].rjust(12) for n in names))
        for r in rows_out:
            click.echo(f"{r['date']:<22}" + "  ".join(
                f"{r.get(n, ''):>12}" for n in names))
        return
    emit(ctx, res)


def _web(ctx):
    """Return (web client, app system dict, portal plant index, portal plant dict)."""
    cc = _cloud(ctx)
    _, sysd = _cloud_system(ctx)
    web = sc.WebClient(cc.creds(), max(ctx.conn["timeout"], 30.0))
    n, plant = web.find_plant(sysd["controlUnitNumber"])
    return web, sysd, n, plant


def _read_reserve(web, n):
    return {"reserve_percent": web.get_reserve(n)}


def _read_peak(web, n):
    s = cloud_core.summarize_peak_shaving(web.get_peak_shaving(n))
    return {"mode": s["mode"], "capacity_limit_percent": s["capacity_limit_percent"],
            "end_ms": s["end_ms"]}


def _run_cloud_plan(ctx, plan, yes, dry_run, write, read):
    if dry_run or not yes:
        emit(ctx, {**plan, "applied": False,
                   "note": "dry run" if dry_run else
                   "not applied: pass --yes to write to mein-senec.de"})
        if not dry_run:
            sys.exit(2)
        return
    write()
    back = read()
    ctx.session.record("cloud setting", setting=plan["setting"], old=plan["old"],
                       new=plan["new"])
    ctx.session.save()
    emit(ctx, {**plan, "applied": True, "read_back": back,
               "verified": cloud_core.verify(plan, back)})


@cloud.group("settings", invoke_without_command=True)
@click.pass_context
def cloud_settings(cctx):
    """Cloud settings (mein-senec.de): overview, reserve, peak-shaving."""
    if cctx.invoked_subcommand is None:
        cctx.invoke(cloud_settings_list)


@cloud_settings.command("list")
@cloud_cmd
def cloud_settings_list(ctx):
    """All known settings with support status and current values (read-only)."""
    web, sysd, n, plant = _web(ctx)
    probes = {}
    for name, fn in (("emergency_reserve", _read_reserve), ("peak_shaving", _read_peak)):
        try:
            probes[name] = fn(web, n)
        except be.SenecError as e:
            probes[name] = e
    emit(ctx, {"system_id": sysd["id"], "plant": n,
               "settings": cloud_core.settings_overview(plant, probes)})


@cloud_settings.command("reserve")
@cloud_cmd
def cloud_reserve_get(ctx):
    """Current emergency-power reserve (% of battery kept back)."""
    web, sysd, n, plant = _web(ctx)
    emit(ctx, {"system_id": sysd["id"], **_read_reserve(web, n),
               "range": list(cloud_core.RESERVE_RANGE)})


@cloud_settings.command("set-reserve")
@click.argument("percent")
@click.option("--yes", is_flag=True, help="Actually write to mein-senec.de.")
@click.option("--dry-run", is_flag=True)
@cloud_cmd
def cloud_reserve_set(ctx, percent, yes, dry_run):
    """Set emergency-power reserve to PERCENT (0..50)."""
    cloud_core.plan_reserve(0, 0, None, percent)  # validate before any network call
    web, sysd, n, plant = _web(ctx)
    plan = cloud_core.plan_reserve(sysd["id"], n, web.get_reserve(n), percent)
    _run_cloud_plan(ctx, plan, yes, dry_run,
                    lambda: web.set_reserve(n, plan["new"]["reserve_percent"]),
                    lambda: _read_reserve(web, n))


@cloud_settings.command("peak-shaving")
@cloud_cmd
def cloud_peak_get(ctx):
    """Current peak-shaving mode, capacity limit and end time."""
    web, sysd, n, plant = _web(ctx)
    emit(ctx, {"system_id": sysd["id"],
               **cloud_core.summarize_peak_shaving(web.get_peak_shaving(n))})


@cloud_settings.command("set-peak-shaving")
@click.option("--mode", type=click.Choice(cloud_core.PEAK_SHAVING_MODES, case_sensitive=False))
@click.option("--capacity", type=int, default=None, help="Capacity limit % (0..100).")
@click.option("--end", default=None, help="End time HH:MM (next occurrence) or ISO.")
@click.option("--yes", is_flag=True, help="Actually write to mein-senec.de.")
@click.option("--dry-run", is_flag=True)
@cloud_cmd
def cloud_peak_set(ctx, mode, capacity, end, yes, dry_run):
    """Set peak shaving; omitted fields keep their current value."""
    if capacity is not None and not 0 <= capacity <= 100:
        raise ValueError(f"Capacity limit {capacity}% out of range 0..100%")
    if end is not None:
        cloud_core.parse_end_time(end)
    web, sysd, n, plant = _web(ctx)
    plan = cloud_core.plan_peak_shaving(sysd["id"], n, web.get_peak_shaving(n), mode,
                                        capacity, end)
    p = plan["request"]["params"]
    _run_cloud_plan(ctx, plan, yes, dry_run,
                    lambda: web.set_peak_shaving(n, p["mode"], p["capacityLimit"],
                                                 p["endzeit"]),
                    lambda: _read_peak(web, n))


@cloud.command("compare")
@cloud_cmd
def cloud_compare(ctx):
    """Compare local device live values with the cloud dashboard."""
    client, sysd = _cloud_system(ctx)
    dash = cloud_core.summarize_dashboard(client.dashboard(sysd["id"]))
    local = device.get_status(ctx.conn, ctx.lang)
    emit(ctx, {"system_id": sysd["id"], **cloud_core.compare(local, dash)})


# ── setup (installer login on the device) ─────────────────────────────

@cli.group()
def setup():
    """Installer setup mode on the device (login/logout/status)."""


def _human_setup(s):
    click.echo(f"Setup mode: {'ON' if s['setup_mode'] else 'off'}  "
               f"(role {s['role']}, level {s['user_level']})")
    click.echo(f"Failed attempts: {s['failed_attempts']}")
    if "cookie_expires_in_s" in s:
        click.echo(f"Session expires in: {s['cookie_expires_in_s']} s")


@setup.command("status")
@pass_ctx
@handle
def setup_status(ctx):
    """Current device login level (installer = setup mode)."""
    emit(ctx, installer.get_state(ctx.conn), _human_setup)


@setup.command("login")
@click.option("--user", default=None, help="Installer user (default SENEC_INSTALL_USERNAME).")
@click.option("--password", default=None, help="Default SENEC_INSTALL_PASSWORD.")
@click.option("--env-file", default=None, help="Credentials file (default ./.env if present).")
@click.option("--wait", default=15.0, type=float, help="Seconds to wait for the login.")
@pass_ctx
@handle
def setup_login(ctx, user, password, env_file, wait):
    """Enter setup mode with the installer credentials."""
    cr = installer.resolve_credentials(user, password, env_file)
    res = installer.login(ctx.conn, cr["user"], cr["password"], wait=wait)
    ctx.session.record("setup login", user_level=res["user_level"])
    ctx.session.save()
    emit(ctx, res, _human_setup)


@setup.command("logout")
@pass_ctx
@handle
def setup_logout(ctx):
    """Leave setup mode and drop the stored GUI cookie."""
    res = installer.logout(ctx.conn)
    ctx.session.record("setup logout")
    ctx.session.save()
    emit(ctx, res, _human_setup)


def _state_names(ctx):
    try:
        return device.state_names(ctx.conn, ctx.lang)
    except be.SenecError:
        return {}


def _human_test(s):
    if "samples" in s:
        for x in s["samples"]:
            _human_tick(x)
    st = s.get("status", s)
    click.echo(f"Test: {'RUNNING ' + str(st['mode']) if st['running'] else 'off'}  "
               f"power {st['test_power_w']:.0f} W")
    click.echo(f"State: {st['state']}  SOC {st['soc_percent']} %  battery "
               f"{st['battery_w']:.0f} W  grid {st['grid_w']:.0f} W")


def _human_tick(x):
    click.echo(f"[{x['t_s']:>5}s] {x['state']}  battery {x['battery_w']:.0f} W  "
               f"grid {x['grid_w']:.0f} W  SOC {x['soc_percent']} %")


@setup.command("test-status")
@pass_ctx
@handle
def setup_test_status(ctx):
    """State of the installer test charge/discharge."""
    emit(ctx, testpower.get_status(ctx.conn, _state_names(ctx)), _human_test)


def _test_start(ctx, mode, watts, yes, dry_run, duration, min_soc, poll):
    testpower.parse_watts(watts)  # validate before any network call
    if duration is not None and duration <= 0:
        raise ValueError("--duration must be > 0 seconds")
    testpower.require_login(ctx.conn)
    names = _state_names(ctx)
    cur = testpower.get_status(ctx.conn, names)
    plan = testpower.plan_start(mode, watts, testpower.read_limits(ctx.conn),
                                cur["soc_percent"], min_soc,
                                cur["mode"] if cur["running"] else None)
    plan["duration_s"] = duration
    if dry_run or not yes:
        emit(ctx, {**plan, "applied": False,
                   "note": "dry run" if dry_run else
                   "not applied: pass --yes to start the test on the device"})
        if not dry_run:
            sys.exit(2)
        return
    ctx.session.record(f"setup test-{mode}", watts=plan["watts"], duration=duration)
    ctx.session.save()
    if duration is None:
        testpower.apply(plan, ctx.conn)
        time.sleep(2)
        emit(ctx, {**plan, "applied": True,
                   "status": testpower.get_status(ctx.conn, names),
                   "note": "running until `setup test-stop`"}, _human_test)
        return
    res = testpower.run_for(plan, ctx.conn, duration, poll,
                            None if ctx.json else _human_tick, names=names)
    time.sleep(2)
    ctx.session.record("setup test-stop", auto=True)
    ctx.session.save()
    emit(ctx, {**plan, "applied": True, **res, "stopped": True,
               "status": testpower.get_status(ctx.conn, names)}, _human_test)


def _test_options(fn):
    for opt in reversed([
        click.option("--yes", is_flag=True, help="Actually start the test (needs consent)."),
        click.option("--dry-run", is_flag=True),
        click.option("--duration", type=float, default=None,
                     help="Run SECONDS, then stop automatically (also on Ctrl-C)."),
        click.option("--poll", type=float, default=5.0, help="Sample interval with --duration."),
    ]):
        fn = opt(fn)
    return fn


@setup.command("test-charge")
@click.argument("watts")
@_test_options
@pass_ctx
@handle
def setup_test_charge(ctx, watts, yes, dry_run, duration, poll):
    """Installer test: charge the battery with WATTS ("Laden")."""
    _test_start(ctx, "charge", watts, yes, dry_run, duration, None, poll)


@setup.command("test-discharge")
@click.argument("watts")
@click.option("--min-soc", type=float, default=testpower.DEFAULT_MIN_SOC, show_default=True,
              help="Refuse if SOC is at/below this %.")
@_test_options
@pass_ctx
@handle
def setup_test_discharge(ctx, watts, min_soc, yes, dry_run, duration, poll):
    """Installer test: discharge the battery with WATTS ("Entladen")."""
    _test_start(ctx, "discharge", watts, yes, dry_run, duration, min_soc, poll)


@setup.command("test-stop")
@pass_ctx
@handle
def setup_test_stop(ctx):
    """Stop a running test (always safe; no --yes needed)."""
    testpower.require_login(ctx.conn)
    plan = testpower.plan_stop()
    testpower.stop(ctx.conn)
    ctx.session.record("setup test-stop")
    ctx.session.save()
    time.sleep(2)
    emit(ctx, {**plan, "applied": True,
               "status": testpower.get_status(ctx.conn, _state_names(ctx))}, _human_test)


# ── session ────────────────────────────────────────────────────────────

@cli.group()
def session():
    """Session state: device config, history, undo/redo."""


@session.command("show")
@pass_ctx
def session_show(ctx):
    """Show current session configuration."""
    emit(ctx, {"path": ctx.session.path, "config": ctx.session.config,
               "last_snapshot": ctx.session.last_snapshot,
               "history_len": len(ctx.session.history),
               "can_undo": bool(ctx.session.undo_stack),
               "can_redo": bool(ctx.session.redo_stack)})


@session.command("set")
@click.argument("key", type=click.Choice(Session.CONFIG_KEYS))
@click.argument("value")
@pass_ctx
@handle
def session_set(ctx, key, value):
    """Set host / verify_tls / timeout / lang."""
    cfg = ctx.session.set_config(key, value)
    ctx.session.save()
    emit(ctx, cfg)


@session.command("set-host")
@click.argument("host")
@pass_ctx
@handle
def session_set_host(ctx, host):
    """Shortcut for `session set host HOST`."""
    cfg = ctx.session.set_config("host", host)
    ctx.session.save()
    emit(ctx, cfg)


@session.command("ping")
@pass_ctx
@handle
def session_ping(ctx):
    """Check device reachability and latency."""
    t = time.time()
    be.read_vars("ENERGY", ["STAT_STATE"], **ctx.conn)
    emit(ctx, {"host": ctx.conn["host"], "reachable": True,
               "latency_ms": round((time.time() - t) * 1000, 1)})


@session.command("history")
@click.option("-n", "--limit", default=20, type=int)
@pass_ctx
def session_history(ctx, limit):
    """Show recent recorded commands."""
    emit(ctx, ctx.session.history[-limit:])


@session.command("undo")
@pass_ctx
@handle
def session_undo(ctx):
    """Undo last session config change."""
    cfg = ctx.session.undo()
    ctx.session.save()
    emit(ctx, cfg)


@session.command("redo")
@pass_ctx
@handle
def session_redo(ctx):
    """Redo last undone config change."""
    cfg = ctx.session.redo()
    ctx.session.save()
    emit(ctx, cfg)


# ── REPL ───────────────────────────────────────────────────────────────

@cli.command()
@pass_ctx
def repl(ctx):
    """Interactive REPL (default when no command is given)."""
    from senec_mcp.utils.repl_skin import ReplSkin

    skin = ReplSkin("senec", version=__version__)
    skin.print_banner()
    skin.info(f"Device: {ctx.conn['host']}")
    pt = skin.create_prompt_session()
    cmds = {name: (c.help or "").split("\n")[0] for name, c in cli.commands.items()}
    cmds.update({"help": "Show this help", "quit": "Exit"})
    while True:
        try:
            line = skin.get_input(pt, project_name=ctx.conn["host"].split("//")[-1])
        except (EOFError, KeyboardInterrupt):
            break
        line = (line or "").strip()
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            break
        if line == "help":
            skin.help(cmds)
            continue
        args = shlex.split(line)
        if ctx.json and "--json" not in args:
            args = ["--json"] + args
        if ctx.host_override:
            args = ["--host", ctx.host_override] + args
        args = ["--session-file", ctx.session.path] + args
        try:
            cli.main(args=args, prog_name="senec", standalone_mode=False,
                     obj=None)
        except SystemExit:
            pass
        except click.ClickException as e:
            skin.error(e.format_message())
        except Exception as e:  # keep REPL alive
            skin.error(str(e))
    skin.print_goodbye()


def main():
    cli()


if __name__ == "__main__":
    main()
