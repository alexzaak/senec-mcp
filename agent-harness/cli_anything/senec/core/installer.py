"""Installer / setup mode on the local device (LOG section of lala.cgi).

The web UI logs in by writing LOG.USERNAME / LOG.PASSWORD / LOG.LOG_IN_BUTT.
The device answers FORBIDDEN for these write-only variables even on success,
so success is verified by polling LOG.USER_LEVEL (bound to the GUI cookie).
"""

import os
import time

from cli_anything.senec.utils import senec_backend as be
from cli_anything.senec.utils.senec_cloud import parse_env_file

USER_KEYS = ("SENEC_INSTALL_USERNAME", "SENEC_INSTALL_USER")
PASSWORD_KEYS = ("SENEC_INSTALL_PASSWORD",)

# mirrors isInstallerUser / isProfessionalUser / isSuperUser in senec.min.js
ROLES = {0: "none", 1: "user", 2: "installer", 4: "professional", 8: "superuser",
         16: "superuser"}
LOGIN_MIN_LEVEL = 2
MAX_FAILED_ATTEMPTS = 5
MAX_USER_LEN, MAX_PASSWORD_LEN = 80, 23


def resolve_credentials(user=None, password=None, env_file=None, environ=None):
    """Explicit args > process env > .env file (SENEC_INSTALL_USERNAME/PASSWORD)."""
    environ = os.environ if environ is None else environ
    file_vals = {}
    path = env_file or (".env" if os.path.exists(".env") else None)
    if path:
        if not os.path.exists(path):
            raise FileNotFoundError(f"env file not found: {path}")
        with open(path) as f:
            file_vals = parse_env_file(f.read())

    def pick(explicit, keys):
        if explicit:
            return explicit
        for src in (environ, file_vals):
            for k in keys:
                if src.get(k):
                    return src[k]
        return None

    return {"user": pick(user, USER_KEYS), "password": pick(password, PASSWORD_KEYS)}


def summarize_log(log, cookie=None, now=None):
    """Decoded LOG section (+ stored cookie entry) → login state dict."""
    level = log.get("USER_LEVEL")
    level = int(level) if isinstance(level, (int, float)) else 0
    now = time.time() if now is None else now
    out = {"logged_in": level >= LOGIN_MIN_LEVEL, "user_level": level,
           "role": ROLES.get(level, f"level_{level}"),
           "setup_mode": level >= LOGIN_MIN_LEVEL,
           "superuser": level in (8, 16),
           "failed_attempts": log.get("LOG_IN_NOK_COUNT"),
           "has_cookie": bool(cookie)}
    if cookie and cookie.get("expires"):
        out["cookie_expires_in_s"] = max(0, int(cookie["expires"] - now))
    return out


def plan_login(user, password):
    if not user or not password:
        raise ValueError("Installer credentials missing: set SENEC_INSTALL_USERNAME and "
                         "SENEC_INSTALL_PASSWORD (env or .env) or pass --user/--password")
    return {"LOG": {"USER_LEVEL": "", "USERNAME": "st_" + user[:MAX_USER_LEN],
                    "LOG_IN_NOK_COUNT": "", "PASSWORD": "st_" + password[:MAX_PASSWORD_LEN],
                    "LOG_IN_BUTT": "u8_01"}}


LOGOUT_REQUEST = {"LOG": {"LOG_IN_NOK_COUNT": "", "LOG_OUT_BUTT": "u8_01"}}
STATE_REQUEST = {"LOG": {"USER_LEVEL": "", "LOG_IN_NOK_COUNT": ""}}


def _read_log(conn):
    raw = be.query(STATE_REQUEST, **conn).get("LOG", {})
    return {k: be.decode_value(v) for k, v in raw.items()}


def get_state(conn):
    return summarize_log(_read_log(conn), be.get_cookie(conn["host"]))


COOKIE_REQUEST = {"FACTORY": {"SYS_TYPE": "", "COUNTRY": "", "DEVICE_ID": ""}}


def _ensure_cookie(conn, sleep=time.sleep):
    # NPU >= 2411: the first call must hand out the GUI cookie before LOG works.
    # No cookie means a stale GUI session holds the slot: log it out and retry
    # (same recovery as the Home Assistant integration).
    if be.get_cookie(conn["host"]):
        return
    be.query(COOKIE_REQUEST, **conn)
    if not be.get_cookie(conn["host"]):
        be.query(LOGOUT_REQUEST, **conn)
        sleep(5.0)
        be.query(COOKIE_REQUEST, **conn)


def login(conn, user, password, wait=15.0, poll=1.0, resend=4.0, sleep=time.sleep):
    """Log in; resend while the device silently ignores the request (no failed
    attempt counted, e.g. another GUI session is still being released)."""
    request = plan_login(user, password)
    _ensure_cookie(conn, sleep)
    state = get_state(conn)
    if state["logged_in"]:
        return {**state, "already_logged_in": True}
    nok0 = state["failed_attempts"] or 0
    if nok0 >= MAX_FAILED_ATTEMPTS:
        raise be.SenecError(f"Login blocked by device after {nok0} "
                            "failed attempts; wait before retrying")
    start = last_sent = time.time()
    be.query(request, **conn)
    while True:
        sleep(poll)
        state = get_state(conn)
        now = time.time()
        if state["logged_in"] or (state["failed_attempts"] or 0) > nok0 or now - start >= wait:
            break
        if now - last_sent >= resend:
            be.query(request, **conn)
            last_sent = now
    if not state["logged_in"]:
        n = state["failed_attempts"] or 0
        hint = ("wrong username/password" if n > nok0 else
                "login possibly blocked by another active GUI session")
        raise be.SenecError(f"Installer login failed ({hint}; failed attempts: {n}/"
                            f"{MAX_FAILED_ATTEMPTS})")
    return {**state, "already_logged_in": False}


def logout(conn, sleep=time.sleep):
    be.query(LOGOUT_REQUEST, **conn)
    sleep(1.0)
    state = get_state(conn)
    be.clear_cookie(conn["host"])
    return {**state, "cookie_cleared": True}
