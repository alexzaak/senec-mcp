"""Cloud backend: mein-senec.de / SENEC app API (Keycloak SSO + REST proxies).

Login uses the same OpenID Connect PKCE flow as the official SENEC app
(client ``endcustomer-app-frontend``). Tokens are cached in a 0600 JSON file
and refreshed automatically (access token ~5 min, refresh token ~30 days).
"""

import base64
import datetime as dt
import hashlib
import hmac
import html
import http.cookiejar
import json
import os
import re
import secrets
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from senec_mcp.utils.senec_backend import SenecError

SSO_URL = "https://sso.senec.com/realms/senec/protocol/openid-connect"
CLIENT_ID = "endcustomer-app-frontend"
REDIRECT_URI = "senec-app-auth://keycloak.prod"
SCOPE = "roles profile meinsenec"

SYSTEMS_URL = "https://senec-app-systems-proxy.prod.senec.dev/systems/api/v1"
MEASURE_URL = "https://senec-app-measurements-proxy.prod.senec.dev/measurements/api/v1/systems"
ABILITIES_URL = "https://senec-app-abilities-proxy.prod.senec.dev/abilities/api/v1/packages"
WALLBOX_URL = "https://senec-app-wallbox-proxy.prod.senec.dev/wallbox/api/v1/systems"

# Cloudflare in front of sso.senec.com rejects urllib's default UA (error 1010).
USER_AGENT = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")
DEFAULT_TIMEOUT = 20.0

RESOLUTIONS = ("FIVE_MINUTES", "QUARTER_HOUR", "HOUR", "DAY", "WEEK", "MONTH", "YEAR")

DEFAULT_TOKEN_PATH = os.path.join(
    os.environ.get("SENEC_HOME", os.path.expanduser("~/.senec")),
    "cloud_token.json",
)


class CloudAuthError(SenecError):
    pass


# ── credentials ───────────────────────────────────────────────────────

def parse_env_file(text):
    """Parse KEY=VALUE lines (optional `export`, quotes); tolerates `-` in keys."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][\w-]*)\s*=\s*(.*?)\s*$", line)
        if not m or line.lstrip().startswith("#"):
            continue
        val = m.group(2)
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[m.group(1)] = val
    return out


USER_KEYS = ("SENEC_USER", "SENEC_MAIL", "SENEC-MAIL", "SENEC_EMAIL", "SENEC_USERNAME")
PASSWORD_KEYS = ("SENEC_PASSWORD", "SENEC-PASSWORD")
TOTP_KEYS = ("SENEC_TOTP_SECRET", "SENEC-TOTP-SECRET")


def resolve_credentials(user=None, password=None, totp_secret=None, env_file=None,
                        environ=None):
    """Credentials precedence: explicit args > process env > .env file."""
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

    return {"user": pick(user, USER_KEYS), "password": pick(password, PASSWORD_KEYS),
            "totp_secret": pick(totp_secret, TOTP_KEYS)}


def totp_now(secret, at=None, digits=6, step=30):
    """RFC 6238 TOTP (SHA1) from a base32 secret."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // step)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def pkce_pair():
    verifier = secrets.token_urlsafe(48)[:64]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def parse_login_form(page):
    """Return (action_url, {'username','password','otp'} subset present) or None."""
    m = re.search(r'<form[^>]*action="([^"]+)"[^>]*>(.*?)</form>', page, re.S | re.I)
    if not m:
        return None
    body = m.group(2)
    fields = {f for f in ("username", "password", "otp")
              if re.search(rf'<input[^>]*name=["\']?{f}["\']?', body, re.I)}
    return html.unescape(m.group(1)), fields


def parse_login_error(page):
    m = re.search(r'id="input-error[^"]*"[^>]*>\s*(.*?)\s*<', page, re.S) or \
        re.search(r'class="[^"]*(?:alert-error|kc-feedback-text)[^"]*"[^>]*>\s*(.*?)\s*<', page, re.S)
    return html.unescape(m.group(1)).strip() if m else None


# ── token handling ────────────────────────────────────────────────────

def _stamp(tok, now=None):
    now = time.time() if now is None else now
    tok = dict(tok)
    if "expires_in" in tok:
        tok["expires_at"] = int(now + tok["expires_in"] - 10)
    if "refresh_expires_in" in tok:
        tok["refresh_expires_at"] = int(now + tok["refresh_expires_in"] - 10)
    return tok


def token_state(tok, now=None):
    now = time.time() if now is None else now
    if not tok or "access_token" not in tok:
        return "missing"
    if tok.get("expires_at", 0) > now:
        return "valid"
    if tok.get("refresh_token") and tok.get("refresh_expires_at", 0) > now:
        return "refreshable"
    return "expired"


def jwt_claims(token):
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def load_token(path=DEFAULT_TOKEN_PATH):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_token(tok, path=DEFAULT_TOKEN_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)
    os.chmod(path, 0o600)
    return path


def delete_token(path=DEFAULT_TOKEN_PATH):
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


# ── HTTP ──────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _open(opener, url, data=None, headers=None, method=None, timeout=DEFAULT_TIMEOUT):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except urllib.error.URLError as e:
        raise SenecError(f"Cannot reach {urllib.parse.urlparse(url).netloc}: {e}") from e


def _token_request(form, timeout=DEFAULT_TIMEOUT):
    st, _, body = _open(urllib.request.build_opener(), SSO_URL + "/token",
                        urllib.parse.urlencode(form).encode(),
                        {"Content-Type": "application/x-www-form-urlencoded",
                         "Accept": "application/json"}, timeout=timeout)
    try:
        data = json.loads(body)
    except ValueError:
        data = {}
    if st != 200 or "access_token" not in data:
        raise CloudAuthError(f"Token request failed (HTTP {st}): "
                             f"{data.get('error_description') or data.get('error') or body[:200]!r}")
    return _stamp(data)


def login(user, password, totp_secret=None, otp=None, timeout=DEFAULT_TIMEOUT):
    """Full PKCE login against sso.senec.com. Returns stamped token dict."""
    if not user or not password:
        raise CloudAuthError(
            "Missing credentials: set SENEC_USER/SENEC_MAIL and SENEC_PASSWORD "
            "(env or .env file) or pass --user/--password")
    verifier, challenge = pkce_pair()
    q = urllib.parse.urlencode({
        "redirect_uri": REDIRECT_URI, "client_id": CLIENT_ID, "response_type": "code",
        "prompt": "login", "state": secrets.token_urlsafe(16),
        "nonce": secrets.token_urlsafe(16), "scope": SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect)
    st, hdr, body = _open(opener, SSO_URL + "/auth?" + q, timeout=timeout)
    for _ in range(5):
        if st in (301, 302, 303):
            break
        page = body.decode("utf-8", "replace")
        if st != 200:
            raise CloudAuthError(f"SSO login page returned HTTP {st}: {page[:200]!r}")
        form = parse_login_form(page)
        if not form:
            raise CloudAuthError("SSO login: no login form found (page layout changed?)")
        action, fields = form
        data = {}
        if "username" in fields:
            data["username"] = user
        if "password" in fields:
            data["password"] = password
        if "otp" in fields:
            code = otp or (totp_now(totp_secret) if totp_secret else None)
            if not code:
                raise CloudAuthError("Account requires a 2FA code: pass --otp or set "
                                     "SENEC_TOTP_SECRET")
            data["otp"] = code
        if not data:
            raise CloudAuthError(f"SSO login failed: {parse_login_error(page) or 'unexpected form'}")
        st, hdr, body = _open(opener, action, urllib.parse.urlencode(data).encode(),
                              {"Content-Type": "application/x-www-form-urlencoded"},
                              timeout=timeout)
        if st == 200:
            err = parse_login_error(body.decode("utf-8", "replace"))
            if err and "password" in data:
                raise CloudAuthError(f"SSO login rejected: {err}")
    else:
        raise CloudAuthError("SSO login did not complete (too many steps)")
    loc = hdr.get("Location", "")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    if not loc.startswith(REDIRECT_URI) or "code" not in params:
        raise CloudAuthError(f"SSO login: unexpected redirect {loc[:120]!r}")
    return _token_request({"code": params["code"][0], "grant_type": "authorization_code",
                           "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
                           "client_id": CLIENT_ID}, timeout)


def refresh(tok, timeout=DEFAULT_TIMEOUT):
    new = _token_request({"refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
                          "client_id": CLIENT_ID}, timeout)
    return new


class CloudClient:
    """Authenticated client; transparently refreshes / re-logs in."""

    def __init__(self, token_path=DEFAULT_TOKEN_PATH, credentials=None,
                 timeout=DEFAULT_TIMEOUT):
        self.token_path = token_path
        self.credentials = credentials or {}
        self.timeout = timeout
        self.token = load_token(token_path)

    def ensure_token(self):
        state = token_state(self.token)
        if state == "valid":
            return self.token
        if state == "refreshable":
            try:
                self.token = refresh(self.token, self.timeout)
                save_token(self.token, self.token_path)
                return self.token
            except CloudAuthError:
                pass
        if self.credentials.get("user") and self.credentials.get("password"):
            self.token = login(self.credentials["user"], self.credentials["password"],
                               self.credentials.get("totp_secret"), timeout=self.timeout)
            save_token(self.token, self.token_path)
            return self.token
        raise CloudAuthError("Not logged in to mein-senec.de: run `cloud login` "
                             "(credentials via SENEC_USER/SENEC_PASSWORD or .env)")

    def get(self, url, method="GET"):
        tok = self.ensure_token()
        st, _, body = _open(urllib.request.build_opener(), url, None,
                            {"Authorization": "Bearer " + tok["access_token"],
                             "Accept": "application/json"}, method, self.timeout)
        if st == 401:
            self.token = dict(self.token, expires_at=0)
            tok = self.ensure_token()
            st, _, body = _open(urllib.request.build_opener(), url, None,
                                {"Authorization": "Bearer " + tok["access_token"],
                                 "Accept": "application/json"}, method, self.timeout)
        if st == 204 or (200 <= st < 300 and not body):
            return None
        if not 200 <= st < 300:
            raise SenecError(f"Cloud API {urllib.parse.urlparse(url).path} -> HTTP {st}: "
                             f"{body[:200].decode('utf-8', 'replace')}")
        try:
            return json.loads(body)
        except ValueError as e:
            raise SenecError(f"Invalid JSON from cloud API: {body[:200]!r}") from e

    # ── endpoints ──
    def systems(self):
        return self.get(SYSTEMS_URL)

    def details(self, sid):
        return self.get(f"{SYSTEMS_URL}/{sid}/details")

    def status(self, sid):
        return self.get(f"{SYSTEMS_URL}/status/{sid}")

    def abilities(self, sid):
        return self.get(f"{ABILITIES_URL}/{sid}")

    def dashboard(self, sid):
        return self.get(f"{MEASURE_URL}/{sid}/dashboard")

    def availability(self, sid, tz="Europe/Berlin"):
        return self.get(f"{MEASURE_URL}/{sid}/data-availability/timespan?"
                        + urllib.parse.urlencode({"timezone": tz}))

    def measurements(self, sid, resolution, start, end):
        return self.get(f"{MEASURE_URL}/{sid}/measurements?" + urllib.parse.urlencode(
            {"resolution": resolution, "from": iso_utc(start), "to": iso_utc(end)}))


def iso_utc(t):
    if isinstance(t, (int, float)):
        t = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    if t.tzinfo is None:
        t = t.astimezone()
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── mein-senec.de web portal (session cookie; settings endpoints) ──────

WEB_URL = os.environ.get("SENEC_CLOUD_URL", "https://mein-senec.de").rstrip("/")
WEB_API = WEB_URL + "/endkunde/api"


class WebClient:
    """Cookie-session client for the mein-senec.de portal (settings live here)."""

    def __init__(self, credentials, timeout=DEFAULT_TIMEOUT):
        self.credentials = credentials or {}
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged_in = False

    def _req(self, url, data=None, method=None):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.geturl(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, url, e.read()
        except urllib.error.URLError as e:
            raise SenecError(f"Cannot reach {urllib.parse.urlparse(url).netloc}: {e}") from e

    def login(self):
        user, pw = self.credentials.get("user"), self.credentials.get("password")
        if not user or not pw:
            raise CloudAuthError("Settings need mein-senec.de credentials: set SENEC_USER/"
                                 "SENEC_MAIL and SENEC_PASSWORD (env or .env)")
        st, url, body = self._req(WEB_URL)
        for _ in range(5):
            if url.startswith(WEB_URL):
                break
            page = body.decode("utf-8", "replace")
            form = parse_login_form(page)
            if st != 200 or not form:
                raise CloudAuthError(f"mein-senec.de login failed at {url[:80]} (HTTP {st})")
            action, fields = form
            data = {}
            if "username" in fields:
                data["username"] = user
            if "password" in fields:
                data["password"] = pw
            if "otp" in fields:
                secret = self.credentials.get("totp_secret")
                if not secret:
                    raise CloudAuthError("Account requires 2FA: set SENEC_TOTP_SECRET")
                data["otp"] = totp_now(secret)
            if not data:
                raise CloudAuthError(f"mein-senec.de login: {parse_login_error(page) or 'unexpected form'}")
            st, url, body = self._req(action, urllib.parse.urlencode(data).encode())
            if not url.startswith(WEB_URL):
                err = parse_login_error(body.decode("utf-8", "replace"))
                if err:
                    raise CloudAuthError(f"mein-senec.de login rejected: {err}")
        else:
            raise CloudAuthError("mein-senec.de login did not complete")
        self.logged_in = True

    def call(self, path, method="GET", params=None):
        if not self.logged_in:
            self.login()
        url = WEB_API + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        st, _, body = self._req(url, b"" if method == "POST" else None, method)
        if not 200 <= st < 300:
            raise SenecError(f"mein-senec.de {method} {path.split('?')[0]} -> HTTP {st}: "
                             f"{body[:200].decode('utf-8', 'replace')}")
        text = body.decode("utf-8", "replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return text

    def plant(self, index):
        return self.call(f"/context/getAnlageBasedNavigationViewModel?anlageNummer={int(index)}")

    def find_plant(self, control_unit, max_plants=15):
        """Map an app system (controlUnitNumber) to the portal's anlageNummer index."""
        for i in range(max_plants):
            try:
                p = self.plant(i)
            except SenecError:
                break
            if not isinstance(p, dict):
                break
            if str(p.get("steuereinheitnummer", "")).lower() == str(control_unit).lower():
                return i, p
        raise SenecError(f"System {control_unit} not found on mein-senec.de")

    def get_reserve(self, n):
        return int(self.call(f"/senec/{n}/emergencypower/reserve-in-percent"))

    def set_reserve(self, n, percent):
        return self.call(f"/senec/{n}/emergencypower", "POST",
                         {"reserve-in-percent": int(percent)})

    def get_peak_shaving(self, n):
        return self.call(f"/peakshaving/getSettings?anlageNummer={n}")

    def set_peak_shaving(self, n, mode, capacity, end_ms):
        return self.call(f"/peakshaving/saveSettings?anlageNummer={n}", "POST",
                         {"mode": mode, "capacityLimit": int(capacity), "endzeit": int(end_ms)})

    def get_sgready_state(self, n):
        return self.call(f"/senec/{n}/sgready/state")
