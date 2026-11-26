"""Accounts, sessions, the CI token and the service tokens of the viewer.

Login is always required. There is no self sign-up: when auth_config.json has
no users, ONE account admin/admin (role admin) is created, and the admin
creates every other account on Settings -> Users.

auth_config.json (next to app.py, git-ignored, 0600):
    {"users": {name: {"password_hash", "role": "admin"|"user", "created_at",
                      "default_password"?}},
     "sessions": {sha256(token): {"user": name, "created": ts}}}
An older single-user file {username, password_hash, sessions} is migrated
into this shape on first read (that user becomes admin).

service_tokens.json (next to auth_config.json, git-ignored, 0600) holds the
tokens the viewer sends to other services and the CI token it accepts:
    {"ci": {"token", "saved_at"},
     "controller": {"token", "saved_at"},
     "plugins": {name: {"token", "saved_at"}},
     "imported_env": true}
Plaintext at rest: a key kept on the same disk would add no protection.
The old environment variables (MT_CI_TOKEN, CONTROLLER_TOKEN /
IOS_CONTROLLER_TOKEN, PLUGIN_TOKEN / MT_PLUGIN_TOKEN_<NAME>) are imported
once, then ignored.
"""
import hashlib
import hmac
import os
import re
import secrets
import sys
import threading
import time
import json
from pathlib import Path

import builds

AUTH_CONFIG_FILE = Path(__file__).parent / "auth_config.json"
SERVICE_TOKENS_FILE = Path(__file__).parent / "service_tokens.json"

COOKIE_NAME = "mt_auth"
# A login never expires on the server; only Logout, a password change/reset
# or deleting the user ends it. Browsers cap cookie lifetimes at 400 days, so
# the cookie is re-sent on every page load (/api/auth/status).
COOKIE_AGE = 400 * 86400

DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "admin"
ROLES = ("admin", "user")
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$")
MIN_PASSWORD = 8

# scrypt: N=2^14, r=8, p=1 (~16 MB, tens of ms per check).
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1

# The runner subprocesses report press events back with this secret (env
# MT_RUNNER_TOKEN). Made fresh at every viewer start, never stored.
RUNNER_TOKEN = secrets.token_urlsafe(32)
RUNNER_HEADER = "X-MT-Runner-Token"

LEGACY_TOKEN_ENV = ("MT_CI_TOKEN", "CONTROLLER_TOKEN", "IOS_CONTROLLER_TOKEN", "PLUGIN_TOKEN")

_LOCK = threading.RLock()


class AuthError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ─── Passwords ───

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                            p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(stored, password):
    """(ok, needs_rehash). Accepts the scrypt form and, once, the old bare
    SHA-256 hex digest (needs_rehash=True). Plaintext is never accepted."""
    stored = stored or ""
    password = password or ""
    if stored.startswith("scrypt$"):
        try:
            _, n, r, p, salt, digest = stored.split("$")
            check = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                                   n=int(n), r=int(r), p=int(p), dklen=len(digest) // 2)
        except (ValueError, TypeError):
            return False, False
        return hmac.compare_digest(check.hex(), digest), False
    if re.fullmatch(r"[0-9a-f]{64}", stored):
        check = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(check, stored), True
    return False, False


# ─── auth_config.json ───

def _read_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _session_key(token):
    """Sessions are stored by hash, so auth_config.json holds no usable cookie."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _migrate(cfg):
    """(config in the multi-user shape, changed?)."""
    changed = False
    users = cfg.get("users")
    if not isinstance(users, dict):
        users = {}
        name = str(cfg.get("username") or "").strip()
        if name and cfg.get("password_hash"):
            users[name] = {"password_hash": cfg["password_hash"], "role": "admin",
                           "created_at": int(time.time())}
        sessions = {}
        old = cfg.get("sessions")
        if name and isinstance(old, dict):
            for key, created in old.items():
                sessions[key] = {"user": name,
                                 "created": created if isinstance(created, (int, float)) else 0}
        cfg = {"users": users, "sessions": sessions}
        changed = True
    if not isinstance(cfg.get("sessions"), dict):
        cfg["sessions"] = {}
        changed = True
    return cfg, changed


def load_auth():
    """The auth config, migrated and seeded (admin/admin when no users)."""
    with _LOCK:
        raw = _read_json(AUTH_CONFIG_FILE)
        cfg, changed = _migrate(raw)
        if not cfg["users"]:
            cfg["users"][DEFAULT_USER] = {"password_hash": hash_password(DEFAULT_PASSWORD),
                                          "role": "admin", "created_at": int(time.time()),
                                          "default_password": True}
            cfg["sessions"] = {}
            changed = True
            print(f"auth: no users in {AUTH_CONFIG_FILE.name}; created {DEFAULT_USER!r} with the "
                  f"default password {DEFAULT_PASSWORD!r}. Sign in and change it now.",
                  file=sys.stderr, flush=True)
        if changed:
            save_auth(cfg)
        return cfg


def save_auth(cfg):
    with _LOCK:
        builds.write_private_json(AUTH_CONFIG_FILE, cfg)


def startup_check():
    """Seed/migrate now, tighten the file modes and warn while an admin still
    has the default password. Returns the list of warnings printed."""
    builds.tighten_mode(AUTH_CONFIG_FILE)
    builds.tighten_mode(SERVICE_TOKENS_FILE)
    cfg = load_auth()
    warnings = []
    for name, user in cfg["users"].items():
        if user.get("role") == "admin" and default_password_in_use(user):
            warnings.append(f"WARNING: admin account {name!r} still has the default password "
                            f"{DEFAULT_PASSWORD!r} — change it in Settings now.")
    for w in warnings:
        print(w, file=sys.stderr, flush=True)
    return warnings


def default_password_in_use(user):
    if "default_password" in user:
        return bool(user["default_password"])
    return verify_password(user.get("password_hash"), DEFAULT_PASSWORD)[0]


def _admins(cfg):
    return [n for n, u in cfg["users"].items() if u.get("role") == "admin"]


def _end_sessions(cfg, username):
    cfg["sessions"] = {k: s for k, s in cfg["sessions"].items()
                       if not (isinstance(s, dict) and s.get("user") == username)}


def _new_session(cfg, username):
    token = secrets.token_urlsafe(32)
    cfg["sessions"][_session_key(token)] = {"user": username, "created": int(time.time())}
    return token


def session_user(token):
    """{"username", "role", "default_password"} for a live session, else None."""
    if not token:
        return None
    cfg = load_auth()
    sess = cfg["sessions"].get(_session_key(token))
    if not isinstance(sess, dict):
        return None
    user = cfg["users"].get(sess.get("user"))
    if not user:
        return None
    return {"username": sess["user"], "role": user.get("role", "user"),
            "default_password": bool(user.get("default_password"))}


def login(username, password):
    """Session token for valid credentials, else None. Rehashes an old
    SHA-256 password hash on success."""
    username = (username or "").strip()
    with _LOCK:
        cfg = load_auth()
        user = cfg["users"].get(username)
        if not user:
            # Same work as a real check, so timing doesn't reveal usernames.
            verify_password(_DUMMY_HASH, password)
            return None
        ok, rehash = verify_password(user.get("password_hash"), password)
        if not ok:
            return None
        if rehash:
            user["password_hash"] = hash_password(password)
        token = _new_session(cfg, username)
        save_auth(cfg)
        return token


def logout(token):
    if not token:
        return
    with _LOCK:
        cfg = load_auth()
        if cfg["sessions"].pop(_session_key(token), None) is not None:
            save_auth(cfg)


def _check_new_password(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise AuthError(f"The password must be at least {MIN_PASSWORD} characters")
    if password == DEFAULT_PASSWORD:
        raise AuthError("Pick a password other than the default")


def change_password(username, current, new):
    """Change one's own password: ends all of that user's sessions and
    returns a fresh session token for the browser that saved."""
    with _LOCK:
        cfg = load_auth()
        user = cfg["users"].get(username)
        if not user or not verify_password(user.get("password_hash"), current)[0]:
            raise AuthError("The current password is wrong", 403)
        _check_new_password(new)
        user["password_hash"] = hash_password(new)
        user.pop("default_password", None)
        _end_sessions(cfg, username)
        token = _new_session(cfg, username)
        save_auth(cfg)
        return token


def list_users():
    cfg = load_auth()
    return [{"username": n, "role": u.get("role", "user"), "created_at": u.get("created_at"),
             "default_password": bool(u.get("default_password"))}
            for n, u in sorted(cfg["users"].items())]


def create_user(username, password, role="user"):
    username = (username or "").strip() if isinstance(username, str) else ""
    if not USERNAME_RE.match(username):
        raise AuthError("Usernames use letters, digits and _ . @ - (up to 64)")
    if role not in ROLES:
        raise AuthError("role must be admin or user")
    _check_new_password(password)
    with _LOCK:
        cfg = load_auth()
        if username in cfg["users"]:
            raise AuthError(f"User {username!r} already exists", 409)
        cfg["users"][username] = {"password_hash": hash_password(password), "role": role,
                                  "created_at": int(time.time())}
        save_auth(cfg)


def reset_password(username, password):
    """An admin sets another user's password; that user's sessions end."""
    _check_new_password(password)
    with _LOCK:
        cfg = load_auth()
        user = cfg["users"].get(username)
        if not user:
            raise AuthError(f"No user {username!r}", 404)
        user["password_hash"] = hash_password(password)
        user.pop("default_password", None)
        _end_sessions(cfg, username)
        save_auth(cfg)


def delete_user(username, acting):
    with _LOCK:
        cfg = load_auth()
        user = cfg["users"].get(username)
        if not user:
            raise AuthError(f"No user {username!r}", 404)
        if username == acting:
            raise AuthError("You can't delete your own account")
        if user.get("role") == "admin" and len(_admins(cfg)) <= 1:
            raise AuthError("You can't delete the last admin")
        del cfg["users"][username]
        _end_sessions(cfg, username)
        save_auth(cfg)


def set_role(username, role):
    if role not in ROLES:
        raise AuthError("role must be admin or user")
    with _LOCK:
        cfg = load_auth()
        user = cfg["users"].get(username)
        if not user:
            raise AuthError(f"No user {username!r}", 404)
        if user.get("role") == "admin" and role != "admin" and len(_admins(cfg)) <= 1:
            raise AuthError("You can't demote the last admin")
        user["role"] = role
        save_auth(cfg)


_DUMMY_HASH = hash_password(secrets.token_hex(8))


# ─── Login throttle (per client IP) ───

class Throttle:
    """After FREE failures from one IP, each further failure doubles the wait
    (1 s, 2 s, 4 s ... up to MAX_WAIT). A success clears the IP."""
    FREE = 5
    MAX_WAIT = 900

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.state = {}
        self.lock = threading.Lock()

    def wait_left(self, ip):
        with self.lock:
            fails, until = self.state.get(ip, (0, 0))
            return max(0.0, until - self.clock())

    def failed(self, ip):
        with self.lock:
            fails, _ = self.state.get(ip, (0, 0))
            fails += 1
            until = 0
            if fails >= self.FREE:
                until = self.clock() + min(self.MAX_WAIT, 2 ** (fails - self.FREE))
            self.state[ip] = (fails, until)
            if len(self.state) > 10000:  # bound memory against address floods
                self.state.pop(next(iter(self.state)))

    def succeeded(self, ip):
        with self.lock:
            self.state.pop(ip, None)


THROTTLE = Throttle()


# ─── service_tokens.json ───

def load_tokens():
    data = _read_json(SERVICE_TOKENS_FILE)
    if not isinstance(data.get("plugins"), dict):
        data["plugins"] = {}
    return data


def save_tokens(data):
    with _LOCK:
        builds.write_private_json(SERVICE_TOKENS_FILE, data)


def token_info(entry):
    token = (entry or {}).get("token") or ""
    return {"configured": bool(token), "last4": token[-4:] if len(token) >= 8 else "",
            "saved_at": (entry or {}).get("saved_at") if token else None}


def _entry(token):
    return {"token": token, "saved_at": int(time.time())}


def ci_token_info():
    return token_info(load_tokens().get("ci"))


def generate_ci_token():
    """A new CI token (replaces the old one). Returned exactly once."""
    token = secrets.token_urlsafe(32)
    with _LOCK:
        data = load_tokens()
        data["ci"] = _entry(token)
        save_tokens(data)
    return token


def remove_ci_token():
    with _LOCK:
        data = load_tokens()
        data["ci"] = {"token": "", "saved_at": int(time.time())}
        save_tokens(data)


def ci_token_valid(header_value):
    """True for `Authorization: Bearer <the CI token>`."""
    expected = (load_tokens().get("ci") or {}).get("token") or ""
    value = header_value or ""
    if not expected or not value.startswith("Bearer "):
        return False
    return hmac.compare_digest(value[len("Bearer "):].strip().encode(), expected.encode())


def runner_token_valid(value):
    return bool(value) and hmac.compare_digest(value.encode(), RUNNER_TOKEN.encode())


def controller_token():
    return (load_tokens().get("controller") or {}).get("token") or ""


def plugin_token(name):
    return ((load_tokens()["plugins"].get(name) or {}).get("token")) or ""


def set_service_token(service, token):
    """service: "controller" or "plugin:<name>". token "" removes it."""
    with _LOCK:
        data = load_tokens()
        entry = _entry(token) if token else {"token": "", "saved_at": int(time.time())}
        if service == "controller":
            data["controller"] = entry
        elif service.startswith("plugin:"):
            if token:
                data["plugins"][service[len("plugin:"):]] = entry
            else:
                data["plugins"].pop(service[len("plugin:"):], None)
        else:
            raise AuthError(f"unknown service {service!r}")
        save_tokens(data)
    apply_to_environ()


def token_env_name(name):
    return "MT_PLUGIN_TOKEN_" + re.sub(r"[^0-9A-Za-z]", "_", name).upper()


def import_env_tokens(plugins, env=None):
    """One-time import of the old token environment variables into
    service_tokens.json. `plugins` = {name: registry entry} of the registered
    plugins (device plugins are url strings, build plugins {url, token}; a
    build entry's own saved token is moved here too). Returns the names of
    what was imported. After the first run (`imported_env`) the environment
    is ignored."""
    env = os.environ if env is None else env
    imported = []
    with _LOCK:
        data = load_tokens()
        if data.get("imported_env"):
            return imported
        ci = (env.get("MT_CI_TOKEN") or "").strip()
        if "ci" not in data and ci:
            data["ci"] = _entry(ci)
            imported.append("ci")
        ctl = (env.get("CONTROLLER_TOKEN") or "").strip() or (env.get("IOS_CONTROLLER_TOKEN") or "").strip()
        if "controller" not in data and ctl:
            data["controller"] = _entry(ctl)
            imported.append("controller")
        shared = (env.get("PLUGIN_TOKEN") or "").strip()
        for name, entry in (plugins or {}).items():
            if name in data["plugins"]:
                continue
            saved = entry.get("token") if isinstance(entry, dict) and isinstance(entry.get("token"), str) else ""
            token = saved.strip() or (env.get(token_env_name(name)) or "").strip() or shared
            if token:
                data["plugins"][name] = _entry(token)
                imported.append(f"plugin:{name}")
        data["imported_env"] = True
        save_tokens(data)
    for what in imported:
        print(f"auth: imported the {what} token into service_tokens.json; the environment "
              "variable is ignored from now on (Settings owns it)", file=sys.stderr, flush=True)
    return imported


def apply_to_environ(environ=None):
    """Make the process environment mirror Settings: the old token variables
    are dropped and CONTROLLER_TOKEN / MT_PLUGIN_TOKEN_<NAME> are set from
    service_tokens.json, so every client (and the runner, which inherits a
    copy) sends exactly the stored tokens."""
    environ = os.environ if environ is None else environ
    for key in list(environ):
        if key in LEGACY_TOKEN_ENV or key.startswith("MT_PLUGIN_TOKEN_"):
            environ.pop(key, None)
    data = load_tokens()
    ctl = (data.get("controller") or {}).get("token") or ""
    if ctl:
        environ["CONTROLLER_TOKEN"] = ctl
    for name, entry in data["plugins"].items():
        if (entry or {}).get("token"):
            environ[token_env_name(name)] = entry["token"]
    return environ
