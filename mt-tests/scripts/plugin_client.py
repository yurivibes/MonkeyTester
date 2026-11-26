#!/usr/bin/env python3
"""
HTTP client for MonkeyTester device plugins (PLUGIN_API.md, contract_version 1).

A device plugin is a separate service that owns one kind of hardware (ESP32
boards, ...). Everything a test asks of it is an action run as a job:

    POST /jobs {device, action, params, timeout_ms}  -> 202 {job}
    GET  /jobs/<id>?wait=<s>                         -> job, loop until terminal
    POST /jobs/<id>/cancel                           -> {job}

Environment:
  MT_PLUGINS                — plugins tests may call: name=url[,name=url...]
                              (e.g. esp32=http://127.0.0.1:8091). Once Run/config.json
                              has a `plugins` map {name: url} — Settings → Device
                              Plugins owns the list then and the viewer imports the
                              env entries into it once — this variable is IGNORED:
                              see load_plugins() for the precedence.
  PLUGIN_TOKEN              — sent as X-Plugin-Token to every plugin, if set
  MT_PLUGIN_TOKEN_<NAME>    — token for one plugin (NAME upper-cased, non-alphanumerics
                              as _), overrides PLUGIN_TOKEN
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from urllib.parse import quote

CONTRACT = "monkeytester-plugin"
SUPPORTED_CONTRACT_VERSIONS = (1,)

TERMINAL_STATES = ("passed", "failed", "timeout", "error", "cancelled")

# Longest single GET /jobs/<id>?wait= this client asks for (the contract allows 60).
MAX_WAIT_S = 30

# Added to a job's own timeout before the client gives up on it locally, so the
# plugin's own timeout (which ends the job in `timeout` with a log) wins.
LOCAL_TIMEOUT_MARGIN_S = 15

# Used when neither the step nor the action gives a timeout.
FALLBACK_TIMEOUT_S = 300

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


class PluginError(Exception):
    """A plugin call failed. `code` is the contract's error code (bad_request,
    unauthorized, not_found, unknown_action, unknown_device, device_busy,
    internal) or one of this client's: unreachable, bad_response, contract,
    config."""

    def __init__(self, message, code="internal", status=None, plugin=None):
        self.message = message
        self.code = code
        self.status = status
        self.plugin = plugin
        prefix = f"plugin {plugin}: " if plugin else ""
        where = f"HTTP {status}, " if status else ""
        super().__init__(f"{prefix}{message} ({where}{code})")


def _error_from_body(body, status):
    """(message, code) from a plugin error body, {"error": "...", "code": "..."}.
    Also accepts {"error": {"code", "message"}}, the Mobile Controller's shape."""
    if not isinstance(body, dict):
        return f"HTTP {status}", "internal"
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or f"HTTP {status}"), str(error.get("code") or "internal")
    return str(error or f"HTTP {status}"), str(body.get("code") or "internal")


def coerce_value(name, raw, spec):
    """Convert a `key=value` string from a test step to the type the action's
    param schema asks for. Values that aren't strings are returned unchanged."""
    if not isinstance(raw, str) or not isinstance(spec, dict):
        return raw
    kind = spec.get("type", "string")
    try:
        if kind == "int":
            return int(raw, 0) if raw.lower().startswith(("0x", "0o", "0b")) else int(raw)
        if kind == "float":
            return float(raw)
        if kind == "bool":
            low = raw.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError(f"not a bool (use true/false)")
        if kind == "json":
            return json.loads(raw)
        if kind == "enum":
            values = spec.get("values") or []
            if values and raw not in values:
                raise ValueError(f"must be one of {', '.join(map(str, values))}")
            return raw
    except ValueError as e:
        raise PluginError(f"param {name}={raw!r} is not a valid {kind}: {e}", "bad_request")
    return raw  # string, path, and types this client doesn't know


class PluginClient:
    def __init__(self, name, url, token=None, timeout=10):
        self.name = name
        self.url = url.rstrip("/")
        self.token = token or None
        self.timeout = timeout
        self._actions = None

    def __repr__(self):
        return f"PluginClient({self.name!r}, {self.url!r})"

    # ── HTTP ──

    def _request(self, method, path, payload=None, timeout=None):
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Plugin-Token"] = self.token
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            if req.type not in ("http", "https"):
                raise urllib.error.URLError("only http(s) URLs are allowed")
            with urllib.request.urlopen(  # nosec B310
                    req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode() or "{}")
            except Exception:
                body = None
            finally:
                e.close()
            message, code = _error_from_body(body, e.code)
            raise PluginError(message, code, status=e.code, plugin=self.name) from None
        except Exception as e:
            raise PluginError(f"unreachable at {self.url}: {e}", "unreachable",
                              plugin=self.name) from None
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            raise PluginError(f"{method} {path} did not return JSON", "bad_response",
                              plugin=self.name) from None
        if not isinstance(body, dict):
            raise PluginError(f"{method} {path} returned {type(body).__name__}, not an object",
                              "bad_response", plugin=self.name)
        return body

    # ── Discovery ──

    def health(self, timeout=None):
        """GET /health, checked against the contract this client speaks.
        Raises PluginError(code="contract") for another contract or version."""
        body = self._request("GET", "/health", timeout=timeout)
        if body.get("contract") != CONTRACT:
            raise PluginError(f"speaks contract {body.get('contract')!r}, expected {CONTRACT!r}",
                              "contract", plugin=self.name)
        version = body.get("contract_version")
        if version not in SUPPORTED_CONTRACT_VERSIONS:
            raise PluginError(
                f"contract_version is {version}, this MonkeyTester supports "
                f"{', '.join(map(str, SUPPORTED_CONTRACT_VERSIONS))}; update whichever is older",
                "contract", plugin=self.name)
        return body

    def ensure_compatible(self):
        """health() once per client; later calls reuse the answer."""
        if not getattr(self, "_health", None):
            self._health = self.health()
        return self._health

    def devices(self, timeout=None):
        """GET /devices -> list of {id, kind, model, state, details}."""
        body = self._request("GET", "/devices", timeout=timeout)
        if body.get("error"):
            raise PluginError(str(body["error"]), "internal", plugin=self.name)
        return body.get("devices") or []

    def actions(self, refresh=False):
        """GET /actions as {name: action}, cached per client."""
        if self._actions is None or refresh:
            body = self._request("GET", "/actions")
            self._actions = {a["name"]: a for a in body.get("actions") or []
                             if isinstance(a, dict) and a.get("name")}
        return self._actions

    def action(self, name):
        action = self.actions().get(name)
        if action is None:
            known = ", ".join(sorted(self.actions())) or "none"
            raise PluginError(f"unknown action {name!r} (actions: {known})", "unknown_action",
                              plugin=self.name)
        return action

    def coerce_params(self, action, params):
        """Convert string values to the types in the action's param schema.
        Params the schema doesn't list are left as strings for the plugin to judge."""
        schema = self.action(action).get("params") or {}
        return {key: coerce_value(key, value, schema.get(key)) for key, value in params.items()}

    # ── Jobs ──

    def start_job(self, action, params=None, device=None, timeout_ms=None):
        """POST /jobs -> the new job."""
        payload = {"action": action, "params": params or {}}
        if device:
            payload["device"] = device
        if timeout_ms is not None:
            payload["timeout_ms"] = int(timeout_ms)
        body = self._request("POST", "/jobs", payload)
        job = body.get("job")
        if not isinstance(job, dict) or not job.get("id"):
            raise PluginError("POST /jobs returned no job id", "bad_response", plugin=self.name)
        return job

    def get_job(self, job_id, wait=0):
        wait = max(0, min(int(wait), 60))
        return self._request("GET", f"/jobs/{quote(str(job_id), safe='')}?wait={wait}",
                             timeout=wait + self.timeout)

    def cancel(self, job_id):
        """POST /jobs/<id>/cancel -> the job."""
        body = self._request("POST", f"/jobs/{quote(str(job_id), safe='')}/cancel", {})
        return body.get("job") if isinstance(body.get("job"), dict) else body

    def wait_job(self, job_id, overall_timeout, on_log=None):
        """Poll GET /jobs/<id>?wait= until the job is terminal or overall_timeout
        (seconds) passes. Returns the last job seen, which isn't terminal if time
        ran out. on_log(line) is called once for each new log line."""
        deadline = time.monotonic() + overall_timeout
        seen = 0
        job = None
        while True:
            remaining = deadline - time.monotonic()
            wait = max(0, min(MAX_WAIT_S, int(remaining)))
            job = self.get_job(job_id, wait=wait)
            seen = _emit_new_log(job, seen, on_log)
            if job.get("state") in TERMINAL_STATES:
                return job
            if time.monotonic() >= deadline:
                return job
            if wait == 0:
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))

    def run(self, action, params=None, device=None, timeout_s=None, on_log=None):
        """Start an action, wait for it, and return the finished job.

        String params are coerced with the action's schema. If the job isn't
        terminal LOCAL_TIMEOUT_MARGIN_S after its timeout, it is cancelled and
        returned with state "timeout" and error code "client_timeout"."""
        self.ensure_compatible()
        spec = self.action(action)
        params = self.coerce_params(action, params or {})
        timeout_ms = int(timeout_s * 1000) if timeout_s is not None else None
        budget = timeout_s if timeout_s is not None else (
            (spec.get("default_timeout_ms") or FALLBACK_TIMEOUT_S * 1000) / 1000)
        job = self.start_job(action, params, device=device, timeout_ms=timeout_ms)
        seen = _emit_new_log(job, 0, on_log)
        if job.get("state") in TERMINAL_STATES:
            return job
        overall = budget + LOCAL_TIMEOUT_MARGIN_S
        job = self.wait_job(job["id"], overall, on_log=_skip_first(seen, on_log))
        if job.get("state") in TERMINAL_STATES:
            return job
        try:
            cancelled = self.cancel(job["id"]) or job
        except PluginError:
            cancelled = job
        if cancelled.get("state") in ("passed", "failed", "error", "timeout"):
            return cancelled  # it finished on its own just before the cancel
        result = dict(cancelled)
        result["state"] = "timeout"
        result["error"] = {"code": "client_timeout",
                           "message": f"job {job['id']} not finished after {overall:g}s; cancelled"}
        return result


def _emit_new_log(job, seen, on_log):
    log = job.get("log") or []
    if len(log) < seen:  # the plugin trimmed its log; don't skip what's new
        seen = 0
    if on_log:
        for line in log[seen:]:
            on_log(str(line))
    return len(log)


def _skip_first(count, on_log):
    """on_log that ignores the first `count` lines, already printed from POST /jobs."""
    if not on_log or not count:
        return on_log
    state = {"left": count}

    def emit(line):
        if state["left"] > 0:
            state["left"] -= 1
            return
        on_log(line)
    return emit


def _token_env_name(name):
    return "MT_PLUGIN_TOKEN_" + re.sub(r"[^0-9A-Za-z]", "_", name).upper()


def parse_plugins(spec):
    """'name=url[,name=url...]' -> [(name, url)]. Raises PluginError(code="config")."""
    entries = []
    names = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, url = part.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not name or not url:
            raise PluginError(f"MT_PLUGINS entry {part!r} is not name=url", "config")
        if not re.match(r"^https?://", url):
            raise PluginError(f"MT_PLUGINS entry {part!r}: url must start with http:// or https://",
                              "config")
        if name in names:
            raise PluginError(f"MT_PLUGINS names plugin {name!r} twice", "config")
        names.add(name)
        entries.append((name, url))
    return entries


def load_plugins_from_env(env=None):
    """{name: PluginClient} for the plugins in MT_PLUGINS. Deprecated for the
    viewer's views: once config.json has a `plugins` map the list comes from
    there — use load_plugins(config, env). The runner keeps this one because
    run_on_device.py re-exports the config.json map into MT_PLUGINS."""
    env = os.environ if env is None else env
    shared = (env.get("PLUGIN_TOKEN") or "").strip()
    plugins = {}
    for name, url in parse_plugins(env.get("MT_PLUGINS", "")):
        token = (env.get(_token_env_name(name)) or "").strip() or shared
        plugins[name] = PluginClient(name, url, token=token or None)
    return plugins


PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def parse_config_plugins(config):
    """The config.json `plugins` map {name: url} -> {name: normalised url}, or
    None when the key is absent (MT_PLUGINS is still the source of the list).
    Raises PluginError(code="config") on a malformed entry. monkeytester-ai
    must NOT be listed here — its address is visual_plugin_url and its
    Configure panel owns it."""
    if not isinstance(config, dict) or "plugins" not in config:
        return None
    raw = config["plugins"]
    if not isinstance(raw, dict):
        raise PluginError('config.json "plugins" must be a JSON object of name: url', "config")
    out = {}
    for name, url in raw.items():
        if not isinstance(name, str) or not PLUGIN_NAME_RE.fullmatch(name):
            raise PluginError(f"config plugin name {name!r} must match {PLUGIN_NAME_RE.pattern}",
                              "config")
        if name == AI_PLUGIN_NAME:
            raise PluginError(f'config.json "plugins" lists {AI_PLUGIN_NAME!r}; its address '
                              "lives in visual_plugin_url (Settings → Device Plugins)", "config")
        if isinstance(url, dict):
            # A build plugin registered in the same Settings → Plugins
            # registry ({"url", "token"}): tests never drive it with PLUGIN
            # steps — it belongs to Build & Test.
            continue
        try:
            url = normalize_plugin_url(url)
        except PluginError as e:
            raise PluginError(f"config plugin {name!r}: {e.message}", e.code) from None
        out[name] = url
    return out


def load_plugins(config=None, env=None):
    """{name: PluginClient} for the device plugins in effect: the config.json
    `plugins` map once it exists (Settings owns the list then and MT_PLUGINS
    is ignored), otherwise the MT_PLUGINS entries. monkeytester-ai never
    joins — the viewer adds it from visual_plugin_url via viewer_plugins().
    Tokens stay env-based (PLUGIN_TOKEN, MT_PLUGIN_TOKEN_<NAME>)."""
    env = os.environ if env is None else env
    entries = parse_config_plugins(config)
    if entries is None:
        entries = {name: url for name, url in parse_plugins(env.get("MT_PLUGINS", ""))
                   if name != AI_PLUGIN_NAME}
    shared = (env.get("PLUGIN_TOKEN") or "").strip()
    return {name: PluginClient(name, url,
                               token=(env.get(_token_env_name(name)) or "").strip()
                               or shared or None)
            for name, url in entries.items()}


def _health_entry(client, timeout):
    """(entry, /health body or None): {ok, url, contract_version, plugin, error?}."""
    entry = {"ok": False, "url": client.url, "contract_version": None, "plugin": None}
    try:
        body = client.health(timeout=timeout)
    except PluginError as e:
        entry["error"] = f"{e.message} ({e.code})"
        return entry, None
    entry.update(ok=bool(body.get("ok")), contract_version=body.get("contract_version"),
                 plugin=body.get("plugin"))
    if not entry["ok"]:
        entry["error"] = str(body.get("error") or "plugin reports not ok")
    return entry, body


def _each_plugin(plugins, check):
    """check(client) for every plugin at once, results in MT_PLUGINS order, so one
    slow or dead plugin costs its own timeout rather than adding to the others'."""
    if not plugins:
        return []
    with ThreadPoolExecutor(max_workers=len(plugins)) as pool:
        return list(pool.map(check, plugins.values()))


# The vision-model plugin. Its address is set in Settings → Device Plugins
# (config.json visual_plugin_url), not in MT_PLUGINS: the viewer's plugin list
# is MT_PLUGINS minus this name, plus this plugin at the configured address.
AI_PLUGIN_NAME = "monkeytester-ai"


def viewer_plugins(env=None, ai_url="", ai_token=None, config=None):
    """{name: PluginClient} for the viewer's views (/api/status, /api/plugins,
    the Device Plugins panel): the device plugins in effect (config.json
    `plugins` once Settings owns the list, else MT_PLUGINS) without any
    monkeytester-ai entry, plus monkeytester-ai at ai_url when one is
    configured. Raises PluginError(code="config") for a malformed list."""
    env = os.environ if env is None else env
    plugins = load_plugins(config, env)
    if ai_url:
        token = (ai_token or "").strip() or (env.get(_token_env_name(AI_PLUGIN_NAME)) or "").strip() \
            or (env.get("PLUGIN_TOKEN") or "").strip()
        plugins[AI_PLUGIN_NAME] = PluginClient(AI_PLUGIN_NAME, ai_url, token=token or None)
    return plugins


def _viewer_plugins_or_error(env, ai_url, ai_token, config=None):
    """(plugins, error message or None). A malformed plugin list doesn't hide
    the AI plugin, whose address comes from config.json."""
    try:
        return viewer_plugins(env, ai_url, ai_token, config), None
    except PluginError as e:
        plugins = {}
        if ai_url:
            plugins[AI_PLUGIN_NAME] = PluginClient(AI_PLUGIN_NAME, ai_url, token=ai_token or None)
        return plugins, e.message


def normalize_plugin_url(url):
    """'http(s)://host[:port][/path]' with no trailing slash, or raise
    PluginError(code="invalid_request")."""
    url = (url or "").strip().rstrip("/")
    if not re.match(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?(/[^\s?#]*)?$", url):
        raise PluginError(f"{url or '(empty)'} is not an address like http://host:port",
                          "invalid_request")
    return url


def ai_validate_address(url, token=None, timeout=5):
    """Check that `url` is a monkeytester-ai plugin the pairing token opens:
    GET /health speaks the plugin contract and names plugin monkeytester-ai,
    and GET /config with the token answers 200. Returns the normalised url;
    raises PluginError with the reason (nothing is stored by the caller then)."""
    url = normalize_plugin_url(url)
    client = PluginClient(AI_PLUGIN_NAME, url, token=token or None, timeout=timeout)
    body = client.health(timeout=timeout)
    plugin = body.get("plugin")
    name = plugin.get("name") if isinstance(plugin, dict) else plugin
    if name != AI_PLUGIN_NAME:
        raise PluginError(f"{url} is plugin {name!r}, not {AI_PLUGIN_NAME!r}", "wrong_plugin",
                          plugin=AI_PLUGIN_NAME)
    try:
        client._request("GET", "/config", timeout=timeout)
    except PluginError as e:
        if e.status == 401:
            raise PluginError("the plugin rejected the pairing token (GET /config: 401)",
                              "unauthorized", status=401, plugin=AI_PLUGIN_NAME) from None
        raise
    return url


def validate_device_plugin(name, url, token=None, timeout=5):
    """Check a device-plugin address before saving it: `url` answers GET /health
    speaking the plugin contract at a supported version (the registration name
    is the viewer's own choice, so the plugin's /health name is NOT compared to
    it). Returns (normalised url, /health body); raises PluginError with the
    reason (the caller stores nothing then)."""
    url = normalize_plugin_url(url)
    client = PluginClient(name, url, token=token or None, timeout=timeout)
    body = client.health(timeout=timeout)
    return url, body


def plugins_status(env=None, timeout=3, ai_url="", ai_token=None, config=None):
    """{name: {ok, url, contract_version, plugin, error?}} for /api/status."""
    plugins, error = _viewer_plugins_or_error(env, ai_url, ai_token, config)
    entries = _each_plugin(plugins, lambda client: _health_entry(client, timeout)[0])
    out = dict(zip(plugins, entries))
    if error:
        out["MT_PLUGINS"] = {"ok": False, "url": None, "contract_version": None,
                             "plugin": None, "error": error}
    return out


def _visual_config(client, timeout):
    """{selected, installed, token:{configured,last4}, provider, copilot} if GET
    /config carries model info — else None. This is the capability signal the
    Device Plugins panel uses: a plugin whose /config reports {model, token}
    owns its own configuration UI, so its row gets a click-through Configure
    panel. The token is only ever read back as {configured, last4}."""
    try:
        body = client._request("GET", "/config", timeout=timeout)
    except PluginError:
        return None
    model = body.get("model") if isinstance(body.get("model"), dict) else None
    token = body.get("token") if isinstance(body.get("token"), dict) else None
    if not (model or token):
        return None
    cfg = public_ai_config(body)
    return {"selected": cfg["model"]["selected"], "installed": cfg["model"]["installed"],
            "token": cfg["token"], "provider": cfg["provider"], "copilot": cfg["copilot"],
            "remote": cfg["remote"]}


# ── MonkeyTester-Plugin-AI plugin configuration (GET/POST /config, /copilot/*) ──
# The AI plugin owns the whole vision-provider configuration: its local Ollama
# model, the remote OpenAI-compatible server (address, model, resize switch,
# API key), the GitHub Copilot sign-in and which of the three answers vision calls.
# These helpers are the only place the viewer speaks that API; every reply is
# reduced to a public shape, so no credential (pairing token, GitHub token,
# device code) is ever passed on to a browser.

AI_PROVIDERS = ("local", "remote", "copilot")
REMOTE_KEYS = ("remote_url", "remote_model", "remote_resize", "remote_api_key")


def public_remote(block):
    """The AI plugin's remote block reduced to {url, model, models:[{id}],
    resize, key:{configured, last4}, error}. The API key itself is never
    passed on, even if a plugin sent it."""
    block = block if isinstance(block, dict) else {}
    models = []
    for m in block.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else m
        if isinstance(mid, str) and mid:
            models.append({"id": mid})
    key = block.get("key") if isinstance(block.get("key"), dict) else {}
    resize = block.get("resize")
    return {"url": str(block.get("url") or ""),
            "model": str(block.get("model") or ""),
            "models": models,
            "resize": True if resize is None else bool(resize),
            "key": {"configured": bool(key.get("configured")),
                    "last4": str(key.get("last4") or "")},
            "error": block.get("error") or None}


def public_copilot(block):
    """The AI plugin's copilot block reduced to {configured, user, last4,
    saved_at, model, models:[{id,name,vendor}], error}."""
    block = block if isinstance(block, dict) else {}
    models = []
    for m in block.get("models") or []:
        if isinstance(m, dict) and m.get("id"):
            models.append({"id": str(m["id"]), "name": str(m.get("name") or m["id"]),
                           "vendor": str(m.get("vendor") or "")})
        elif isinstance(m, str) and m:
            models.append({"id": m, "name": m, "vendor": ""})
    return {"configured": bool(block.get("configured")),
            "user": str(block.get("user") or ""),
            "last4": str(block.get("last4") or ""),
            "saved_at": block.get("saved_at") or "",
            "model": str(block.get("model") or ""),
            "models": models,
            "error": block.get("error") or None}


def public_ai_config(body):
    """GET/POST /config of the AI plugin reduced to what a browser may see."""
    body = body if isinstance(body, dict) else {}
    model = body.get("model") if isinstance(body.get("model"), dict) else {}
    token = body.get("token") if isinstance(body.get("token"), dict) else {}
    provider = str(body.get("provider") or "local")
    return {"provider": provider if provider in AI_PROVIDERS else "local",
            "model": {"selected": str(model.get("selected") or ""),
                      "installed": [str(x) for x in (model.get("installed") or [])]},
            "token": {"configured": bool(token.get("configured")),
                      "last4": str(token.get("last4") or "")},
            "copilot": public_copilot(body.get("copilot")),
            "remote": public_remote(body.get("remote")),
            "api_version": body.get("api_version"),
            "service": body.get("service")}


def ai_models(client, timeout=None):
    """GET /models of the AI plugin: {data:[{id}], selected, installed, provider?}
    for the active provider."""
    return client._request("GET", "/models", timeout=timeout)


def ai_config(client, timeout=None):
    """GET /config of the AI plugin, public shape."""
    return public_ai_config(client._request("GET", "/config", timeout=timeout))


def _wire_config(payload):
    """The plugin's POST /config spelling of one change. A rotation is
    {"token": {"generate": true}} on the wire (MonkeyTester-Plugin-AI API.md); a bare
    {"generate": true} is refused there with "unknown setting: generate"."""
    if "generate" in payload:
        rest = {k: v for k, v in payload.items() if k != "generate"}
        if payload["generate"]:
            rest["token"] = {"generate": True}
        return rest
    return payload


def ai_save_config(client, payload, timeout=10):
    """POST /config with one change: {model} (local Ollama model), {provider},
    {copilot_model}, {generate: True} (rotate the pairing token) or the remote
    server's keys {remote_url?, remote_model?, remote_resize?, remote_api_key?}
    (sent together, so the plugin validates the address with the key given in
    the same request). Returns (public config, new_token or ""). The new
    token is the plugin's one-time reply to a rotation; callers store it and
    never echo it."""
    allowed = {"model", "provider", "copilot_model", "generate", *REMOTE_KEYS}
    unknown = set(payload) - allowed
    if unknown:
        raise PluginError(f"unsupported /config keys: {', '.join(sorted(unknown))}",
                          "bad_request", plugin=client.name)
    if "provider" in payload and payload["provider"] not in AI_PROVIDERS:
        raise PluginError("provider must be local, remote or copilot", "invalid_request",
                          plugin=client.name)
    body = client._request("POST", "/config", _wire_config(payload), timeout=timeout)
    new_token = body.get("new_token") or body.get("token") or ""
    if isinstance(new_token, dict):  # tolerate {token: {...}} replies
        new_token = new_token.get("token") or new_token.get("new") or ""
    if not isinstance(new_token, str):
        new_token = ""
    config = body.get("config") if isinstance(body.get("config"), dict) else body
    return public_ai_config(config), new_token


def ai_copilot_device_start(client, timeout=20):
    """POST /copilot/device/start → {user_code, verification_uri, interval, expires_in}.
    The device code stays inside the plugin."""
    body = client._request("POST", "/copilot/device/start", {}, timeout=timeout)
    if not body.get("user_code") or not body.get("verification_uri"):
        raise PluginError("device/start returned no user_code", "bad_response",
                          plugin=client.name)
    out = {"user_code": str(body["user_code"]),
           "verification_uri": str(body["verification_uri"])}
    for key in ("interval", "expires_in"):
        if isinstance(body.get(key), (int, float)):
            out[key] = body[key]
    return out


def ai_copilot_device_poll(client, timeout=20):
    """POST /copilot/device/poll → {state, interval?, error?, copilot?}.
    state is pending | slow_down | done | expired | denied | error; on done the
    plugin's public copilot config comes back as `copilot`."""
    body = client._request("POST", "/copilot/device/poll", {}, timeout=timeout)
    state = str(body.get("state") or "error")
    out = {"state": state}
    if isinstance(body.get("interval"), (int, float)):
        out["interval"] = body["interval"]
    if body.get("error"):
        err = body["error"]
        out["error"] = str(err.get("message") or err) if isinstance(err, dict) else str(err)
    if state == "done":
        block = body.get("copilot") if isinstance(body.get("copilot"), dict) else body
        out["copilot"] = public_copilot(block)
    return out


def ai_copilot_remove(client, timeout=10):
    """POST /copilot/remove → public copilot config (the plugin switches back to
    the local provider if Copilot was active)."""
    body = client._request("POST", "/copilot/remove", {}, timeout=timeout)
    block = body.get("copilot") if isinstance(body.get("copilot"), dict) else body
    return public_copilot(block)


def plugins_overview(env=None, timeout=3, tokens=None, ai_url="", ai_token=None, config=None):
    """{plugins: [{name, url, source, ok, contract_version, plugin, capabilities,
    devices, config, error?}], error?} for the Settings page. The list is the
    device plugins in effect (config.json `plugins` once Settings owns it —
    source "config" —, else MT_PLUGINS minus monkeytester-ai), plus monkeytester-ai
    at ai_url (its address from Settings, source "settings"); error is set when
    the active list is malformed. A plugin that does not answer still lists,
    with its error. config is the _visual_config probe: non-null only for
    plugins whose GET /config carries model info (the monkeytester-ai shape).
    tokens ({name: token}) overrides the env token for some plugins."""
    plugins, error = _viewer_plugins_or_error(env, ai_url, ai_token, config)
    for name, token in (tokens or {}).items():
        if token and name in plugins:
            plugins[name].token = token

    def check(client):
        entry, body = _health_entry(client, timeout)
        source = ("settings" if client.name == AI_PLUGIN_NAME else
                  ("config" if isinstance((config or {}).get("plugins"), dict) else "MT_PLUGINS"))
        entry = dict(name=client.name, **entry, source=source, capabilities=[], devices=[],
                     config=None)
        if body is not None:
            entry["capabilities"] = body.get("capabilities") or []
            # The optional /health `docs` path, raw: the viewer validates it
            # and passes only a docs true/false on to the browser.
            if isinstance(body.get("docs"), str):
                entry["docs"] = body["docs"]
            try:
                entry["devices"] = client.devices(timeout=timeout)
            except PluginError:
                pass
            entry["config"] = _visual_config(client, timeout)
        return entry

    out = {"plugins": _each_plugin(plugins, check)}
    if error:
        out["error"] = (f"config.json plugins is invalid: {error}"
                        if isinstance((config or {}).get("plugins"), dict)
                        else f"MT_PLUGINS is malformed: {error}")
    return out
