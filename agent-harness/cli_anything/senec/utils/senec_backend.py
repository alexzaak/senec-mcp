"""Backend: talks to the real SENEC.Home device via its lala.cgi JSON endpoint."""

import json
import os
import re
import ssl
import struct
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "https://senec.home.arpa"
DEFAULT_TIMEOUT = 10.0

KNOWN_SECTIONS = [
    "ENERGY", "BMS", "PV1", "PM1OBJ1", "PM1", "TEMPMEASURE", "FAN_SPEED",
    "WIZARD", "FACTORY", "SYS_UPDATE", "RTC", "WALLBOX", "SOCKETS",
    "FEATURES", "GRIDCONFIG", "CASC", "LOG",
]

ERROR_MARKERS = {"FORBIDDEN", "VARIABLE_NOT_FOUND", "OBJECT_NOT_FOUND", "MALFORMED_VALUE"}


class SenecError(RuntimeError):
    pass


# ── value codec (mirrors Senec.jsonValUnpack / jsonValPack) ────────────

def decode_value(raw):
    """Decode a typed SENEC value string (or list of them)."""
    if isinstance(raw, list):
        return [decode_value(v) for v in raw]
    if isinstance(raw, dict):
        return {k: decode_value(v) for k, v in raw.items()}
    if not isinstance(raw, str):
        return raw
    if raw in ERROR_MARKERS:
        return None
    if "_" not in raw:
        raise SenecError(f"Malformed SENEC value: {raw!r}")
    prefix, payload = raw.split("_", 1)
    if prefix == "fl":
        return round(struct.unpack(">f", bytes.fromhex(payload.zfill(8)))[0], 4)
    if prefix in ("u8", "u1", "u3", "u6"):
        return int(payload, 16)
    bits = {"i8": 8, "i1": 16, "i3": 32}.get(prefix)
    if bits:
        v = int(payload, 16)
        return v - (1 << bits) if v & (1 << (bits - 1)) else v
    if prefix in ("st", "ch"):
        return payload
    if prefix == "er":
        raise SenecError(f"Device error value: {raw}")
    raise SenecError(f"Unknown SENEC value type: {raw!r}")


def encode_value(type_prefix, value):
    """Encode a Python value into SENEC typed string."""
    if type_prefix == "fl":
        return "fl_" + struct.pack(">f", float(value)).hex().upper()
    widths = {"u8": 2, "u1": 4, "u3": 8, "u6": 16, "i8": 2, "i1": 4, "i3": 8}
    if type_prefix in widths:
        v = int(value)
        w = widths[type_prefix]
        if type_prefix.startswith("u") and v < 0:
            raise SenecError(f"Negative value for unsigned type {type_prefix}")
        v &= (1 << (w * 4)) - 1
        return f"{type_prefix}_{v:0{w}X}"
    if type_prefix == "st":
        return "st_" + str(value)
    raise SenecError(f"Cannot encode type {type_prefix!r}")


def value_type(raw):
    if isinstance(raw, list):
        return value_type(raw[0]) if raw else None
    if isinstance(raw, str) and "_" in raw and raw not in ERROR_MARKERS:
        return raw.split("_", 1)[0]
    return None


# ── HTTP transport ─────────────────────────────────────────────────────

def _ssl_ctx(verify):
    if verify:
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ── GUI session cookie (needed for installer / setup mode) ─────────────
#
# The device web UI binds the login (LOG.USER_LEVEL) to the cookie
# `senec-homev3-gui`. It is rotated on login/logout, so every response's
# Set-Cookie is persisted per host and replayed on later requests.

COOKIE_NAME = "senec-homev3-gui"


def cookie_path():
    return os.environ.get("CLI_ANYTHING_SENEC_COOKIES") or os.path.join(
        os.environ.get("CLI_ANYTHING_SENEC_HOME", os.path.expanduser("~/.cli-anything-senec")),
        "device_cookies.json")


def _host_key(host):
    return host.rstrip("/").split("//")[-1].lower()


def _read_cookie_file():
    try:
        with open(cookie_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cookie_file(data):
    path = cookie_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


def parse_set_cookie(header):
    """Return (name, value, max_age or None) from a Set-Cookie header."""
    parts = [p.strip() for p in header.split(";")]
    if not parts or "=" not in parts[0]:
        return None, None, None
    name, value = parts[0].split("=", 1)
    max_age = None
    for p in parts[1:]:
        if p.lower().startswith("max-age="):
            try:
                max_age = int(p.split("=", 1)[1])
            except ValueError:
                pass
    return name.strip(), value.strip(), max_age


def get_cookie(host, now=None):
    """Return stored cookie entry for host if not expired, else None."""
    entry = _read_cookie_file().get(_host_key(host))
    now = time.time() if now is None else now
    if not entry or (entry.get("expires") and entry["expires"] <= now):
        return None
    return entry


def store_cookie(host, value, max_age=None, now=None):
    now = time.time() if now is None else now
    data = _read_cookie_file()
    data[_host_key(host)] = {"name": COOKIE_NAME, "value": value,
                             "expires": now + max_age if max_age else None, "updated": now}
    _write_cookie_file(data)
    return data[_host_key(host)]


def clear_cookie(host):
    data = _read_cookie_file()
    existed = data.pop(_host_key(host), None) is not None
    if existed:
        _write_cookie_file(data)
    return existed


def _http(host, path, data=None, verify=False, timeout=DEFAULT_TIMEOUT):
    url = host.rstrip("/") + path
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    cookie = get_cookie(host)
    if cookie:
        req.add_header("Cookie", f"{COOKIE_NAME}={cookie['value']}")
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(verify), timeout=timeout) as r:
            for h in r.headers.get_all("Set-Cookie") or []:
                name, value, max_age = parse_set_cookie(h)
                if name == COOKIE_NAME and value:
                    store_cookie(host, value, max_age)
            return r.read()
    except urllib.error.URLError as e:
        raise SenecError(
            f"Cannot reach SENEC device at {url}: {e}. "
            "Check --host / `session set-host` and that the device is on the network."
        ) from e


def query(request, host=DEFAULT_HOST, verify=False, timeout=DEFAULT_TIMEOUT):
    """Send a raw lala.cgi request dict, return raw (undecoded) response dict."""
    body = json.dumps(request).encode()
    raw = _http(host, "/lala.cgi", body, verify, timeout)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise SenecError(f"Invalid JSON from device: {raw[:200]!r}") from e


def read_sections(sections, host=DEFAULT_HOST, verify=False, timeout=DEFAULT_TIMEOUT):
    """Read complete sections. Returns raw dict."""
    return query({s: {} for s in sections}, host, verify, timeout)


def read_vars(section, names, host=DEFAULT_HOST, verify=False, timeout=DEFAULT_TIMEOUT):
    return query({section: {n: "" for n in names}}, host, verify, timeout)


def write_var(section, name, typed_value, host=DEFAULT_HOST, verify=False,
              timeout=DEFAULT_TIMEOUT):
    resp = query({section: {name: typed_value}}, host, verify, timeout)
    result = resp.get(section, {}).get(name)
    if result in ERROR_MARKERS:
        raise SenecError(f"Write {section}.{name} rejected by device: {result}")
    return resp


_STATE_RE = re.compile(r"system_state_name\s*=\s*\{(.*?)\}", re.S)


def parse_state_names(js_text):
    m = _STATE_RE.search(js_text)
    if not m:
        return {}
    return {int(k): v for k, v in re.findall(r'(\d+)\s*:\s*"([^"]*)"', m.group(1))}


def fetch_state_names(host=DEFAULT_HOST, lang="DE-de", verify=False, timeout=DEFAULT_TIMEOUT):
    """Load system state names from the device's own language file."""
    text = _http(host, f"/js/{lang}.js", None, verify, timeout).decode("utf-8", "replace")
    names = parse_state_names(text)
    if not names:
        raise SenecError(f"No system_state_name table in /js/{lang}.js")
    return names
