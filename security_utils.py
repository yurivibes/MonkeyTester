"""Shared input-validation, path-safety and HTTP-hardening helpers for the viewer.

Kept apart from app.py so every route uses the same checks:

- safe_join(root, *parts): join request-derived path parts onto a root and
  refuse anything that could leave it ('..', absolute parts, NUL, backslashes,
  symlinks pointing outside). Raises UnsafePath (a ValueError).
- Allowlist validators for values that end up in a subprocess argv or a path
  (device ids, run names, bundle ids, branch names, test names).
- Request-body limits: the JSON cap and the upload cap (VT_MAX_UPLOAD_MB).
- harden_handler: a class decorator for the BaseHTTPRequestHandler subclass
  that adds security headers to every response, a socket timeout, a 500 JSON
  body (never a traceback) when a route raises, and a 404 when a GET route
  sends nothing.
- write_private_json: atomic 0600 JSON writes for files that hold tokens.

Stdlib only.
"""
import functools
import json
import os
import re
import sys
import threading
import traceback
import uuid
from pathlib import Path

# ─── Validators ───

# Device ids: Android serials (USB ids, IP:PORT) and iPhone UDIDs (hex with
# dashes). First character alphanumeric, so an id can never look like an option.
DEVICE_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z:._-]{3,127}$")
RUN_ID_RE = re.compile(r"^run_\d+$")
# Reverse-DNS app ids (iOS bundle ids, Android package names).
BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*$")
MAX_BUNDLE_ID_LEN = 255
# Git branch/ref names: the git check-ref-format rules that matter here, and
# no leading '-' (it would read as an option to any tool it reaches).
BRANCH_RE = re.compile(r"^(?![-/])(?!.*\.\.)(?!.*//)(?!.*@\{)"
                       r"[^\s~^:?*\[\\\x00-\x1f\x7f]{1,250}(?<![/.])(?<!\.lock)$")
# Test names come from .txt file names (optionally "<case>__<name>"): any
# printable text without path separators that does not start with a dot.
TEST_NAME_RE = re.compile(r"^(?!\.)[^/\\\x00-\x1f\x7f]{1,255}$")


def valid_device_id(value):
    return isinstance(value, str) and bool(DEVICE_ID_RE.fullmatch(value))


def valid_run_id(value):
    return isinstance(value, str) and bool(RUN_ID_RE.fullmatch(value))


def valid_bundle_id(value):
    return (isinstance(value, str) and 0 < len(value) <= MAX_BUNDLE_ID_LEN
            and bool(BUNDLE_ID_RE.fullmatch(value)))


def valid_branch(value):
    return isinstance(value, str) and bool(BRANCH_RE.fullmatch(value))


def valid_test_name(value):
    return isinstance(value, str) and bool(TEST_NAME_RE.fullmatch(value))


_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename(name, default="upload"):
    """A client-sent file name reduced to a plain basename of [A-Za-z0-9._-]
    that cannot be '.', '..' or hidden. Keeps the extension readable."""
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME_CHARS.sub("_", name).lstrip(".")
    return name[:200] or default


def check_http_url(url):
    """Return url when it is http(s); raise ValueError otherwise. Every
    outgoing urllib call goes through this first, so a configured or typed
    address can never make urllib open file:// or another scheme."""
    from urllib.parse import urlsplit
    scheme = urlsplit(str(url or "")).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme {scheme or '(none)'!r}: only http and https")
    return url


# ─── Paths ───

class UnsafePath(ValueError):
    """A request-derived path would leave its root (or is malformed)."""


def _check_part(part):
    if not isinstance(part, (str, os.PathLike)):
        raise UnsafePath("path part must be a string")
    text = os.fspath(part)
    if not text:
        raise UnsafePath("empty path part")
    if "\x00" in text or "\\" in text:
        raise UnsafePath("NUL or backslash in path")
    if text.startswith("/") or os.path.isabs(text):
        raise UnsafePath("absolute path")
    if ".." in text.split("/"):
        raise UnsafePath("'..' in path")
    return text


def safe_join(root, *parts):
    """root/parts... as a Path, guaranteed (after resolving symlinks) to lie
    inside root. Each part may contain '/' separators; '..' segments,
    absolute parts, NUL and backslashes are refused. Raises UnsafePath."""
    root_real = os.path.realpath(os.fspath(root))
    joined = os.path.join(root_real, *[_check_part(p) for p in parts]) if parts else root_real
    real = os.path.realpath(joined)
    try:
        inside = os.path.commonpath([root_real, real]) == root_real
    except ValueError:
        inside = False
    if not inside:
        raise UnsafePath("path leaves its root")
    return Path(real)


# ─── Request limits ───

MAX_JSON_BODY_BYTES = 1024 * 1024  # 1 MiB: every JSON API body is far smaller
DEFAULT_MAX_UPLOAD_MB = 600         # whole /api/run-test multipart body


def max_upload_bytes(env=None):
    """The /api/run-test body cap from VT_MAX_UPLOAD_MB (MiB, default 600).
    The body is read into memory, so the cap also bounds memory use."""
    env = os.environ if env is None else env
    raw = (env.get("VT_MAX_UPLOAD_MB") or "").strip()
    try:
        mb = int(raw) if raw else DEFAULT_MAX_UPLOAD_MB
    except ValueError:
        mb = DEFAULT_MAX_UPLOAD_MB
    if mb <= 0:
        mb = DEFAULT_MAX_UPLOAD_MB
    return mb * 1024 * 1024


class BodyError(Exception):
    """A request body that must be refused before it is read."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def body_length(headers, limit):
    """The validated Content-Length of a request (0 when there is no body).

    A body without a Content-Length (chunked transfer encoding) is 411, a
    malformed or negative length 400, more than `limit` bytes 413."""
    if (headers.get("Transfer-Encoding") or "").strip():
        raise BodyError(411, "Content-Length is required (chunked bodies are not accepted)")
    raw = headers.get("Content-Length")
    if raw is None or not str(raw).strip():
        return 0
    try:
        length = int(str(raw).strip())
    except ValueError:
        raise BodyError(400, "invalid Content-Length") from None
    if length < 0:
        raise BodyError(400, "invalid Content-Length")
    if length > limit:
        raise BodyError(413, f"request body too large (limit {limit} bytes)")
    return length


# ─── Secret files ───

def write_private_json(path, data):
    """Write JSON readable only by this user (0600), atomically (temp file in
    the same directory + rename), so readers never see a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}."
                         f"{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── HTTP handler hardening ───

SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
)
HANDLER_TIMEOUT_SECS = 60
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def harden_handler(cls):
    """Class decorator for a BaseHTTPRequestHandler subclass.

    - Every response carries SECURITY_HEADERS.
    - The socket gets a HANDLER_TIMEOUT_SECS timeout (per blocking socket
      operation, so long-lived streams that keep writing are unaffected; a
      client that stops reading or sending is dropped).
    - The Server header no longer names the Python version.
    - An exception in a do_* method is logged to stderr with its traceback
      and the client gets a 500 {"error": "internal server error"} (when no
      response was started) — never the traceback.
    - A GET/HEAD route that sends no response at all gets a 404.
    """
    cls.timeout = HANDLER_TIMEOUT_SECS
    cls.server_version = "MonkeyTester"
    cls.sys_version = ""
    cls.version_string = lambda self: self.server_version

    orig_send_response = cls.send_response
    orig_end_headers = cls.end_headers

    def send_response(self, code, message=None):
        self._vt_response_started = True
        orig_send_response(self, code, message)

    def end_headers(self):
        if not getattr(self, "_vt_security_headers_sent", False):
            self._vt_security_headers_sent = True
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
        orig_end_headers(self)

    cls.send_response = send_response
    cls.end_headers = end_headers

    def guard(method, not_found_if_silent):
        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            self._vt_response_started = False
            self._vt_security_headers_sent = False
            try:
                method(self, *args, **kwargs)
            except _CLIENT_GONE:
                self.close_connection = True
                return
            except Exception:
                print(f"Unhandled error in {self.command} {self.path.split('?', 1)[0]}:\n"
                      f"{traceback.format_exc()}", file=sys.stderr, flush=True)
                self.close_connection = True
                if not self._vt_response_started:
                    _send_plain_json(self, 500, {"error": "internal server error"})
                return
            if not_found_if_silent and not self._vt_response_started:
                _send_plain_json(self, 404, {"error": "not found"})
        return wrapper

    for name, silent_404 in (("do_GET", True), ("do_HEAD", True),
                             ("do_POST", False), ("do_OPTIONS", False)):
        method = cls.__dict__.get(name)
        if method is not None:
            setattr(cls, name, guard(method, silent_404))
    return cls


def _send_plain_json(handler, status, data):
    body = json.dumps(data).encode()
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)
    except OSError:  # client gone or timed out
        handler.close_connection = True
