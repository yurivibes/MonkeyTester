#!/usr/bin/env python3
"""Simple web viewer for MonkeyTester results."""
import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
import io
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote, unquote

import builds
import security_utils as sec
import viewer_auth as auth

# Build plugin pairing tokens come from Settings (service_tokens.json).
builds.TOKEN_LOOKUP = auth.plugin_token

sys.path.insert(0, str(Path(__file__).resolve().parent / "mt-tests" / "scripts"))

TEST_DIR = Path(__file__).parent / "Run"
CONFIG_FILE = TEST_DIR / "config.json"
CONF_DIR = Path(__file__).parent / "Conf"
CONF_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DEVICES_FILE = CONF_DIR / "device_names.json"
PAGE_DIR = Path(__file__).parent
FONT_DIR = PAGE_DIR / "fonts"
TEST_DIR.mkdir(parents=True, exist_ok=True)

# Secrets files written by older versions may be group/world-readable.
for _secrets_file in (builds.CONFIG_FILE, builds.LEGACY_GITEA_FILE, CONFIG_FILE):
    builds.tighten_mode(_secrets_file)

# Older viewers kept a GitHub Copilot sign-in here; the AI plugin owns it now.
_LEGACY_COPILOT_FILE = Path(__file__).parent / "copilot_config.json"
if _LEGACY_COPILOT_FILE.exists():
    print("DEPRECATED: copilot_config.json is no longer used — GitHub Copilot is signed in on "
          "the MonkeyTester-Plugin-AI plugin now. Delete the file (it holds a GitHub token).",
          file=sys.stderr, flush=True)


def _native_back():
    """True when MT_NATIVE_BACK is set — render a back button inside the HTML
    instead of relying on the browser's own navigation controls."""
    val = (os.environ.get("MT_NATIVE_BACK") or "").strip()
    return val.lower() not in ("", "0", "false", "off")


def _web_runs_disabled():
    """True when MT_DISABLE_WEB_RUNS is set: the nav hides "Build & Test". UI only: the pages and their APIs keep working."""
    val = (os.environ.get("MT_DISABLE_WEB_RUNS") or "").strip()
    return val.lower() not in ("", "0", "false", "off")


def _builds():
    """builds (Build & Test), with build records kept under the current runs
    folder; its plugin registry is the viewer's config.json."""
    builds.BUILDS_DIR = TEST_DIR / ".builds"
    builds.VIEWER_CONFIG_FILE = CONFIG_FILE
    return builds


def load_device_names():
    """Friendly device names: {device id: name}.

    An older file is a list of {path, name, adb_host} or a dict keyed by capture
    path. Only entries that name a device id still mean anything.
    """
    if not CONFIG_DEVICES_FILE.exists():
        return {}
    try:
        with open(CONFIG_DEVICES_FILE) as f:
            data = json.load(f)
    except Exception:
        return {}
    if isinstance(data, list):
        return {e["adb_host"]: e.get("name", "") for e in data if e.get("adb_host")}
    return {k: v for k, v in data.items() if not k.startswith("/dev/")}


def save_device_names(data):
    """Save device config list."""
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_DEVICES_FILE, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _controller_url():
    """Base URL of MonkeyTester-Plugin-Mobile-Controller, which owns every device.
    Returns '' when unconfigured. Settings → Plugins → Controller Plugin
    owns the address once Run/config.json has the controller_url key (the
    key's presence is the ownership marker, like the plugins map's): a
    saved "" means removed and the CONTROLLER_URL environment does NOT
    come back; the environment applies only while the key is absent (it is
    imported into the key once at viewer start, import_controller_url)."""
    cfg = load_config()
    if "controller_url" in cfg:
        return (cfg.get("controller_url") or "").strip().rstrip("/")
    return (os.environ.get("CONTROLLER_URL") or "").strip().rstrip("/")


def controller_plugin_state():
    """Settings → Plugins → Controller Plugin view:
    {configured, source ("settings" once config.json owns the slot, "env"
    before that, "" when unset), url, live: controller_status()}."""
    cfg = load_config()
    url = _controller_url()
    return {"configured": bool(url),
            "source": "settings" if "controller_url" in cfg else ("env" if url else ""),
            "url": url,
            "live": controller_status()}


def _controller_headers():
    headers = {}
    # Settings → Configuration → Service tokens (service_tokens.json).
    token = auth.controller_token()
    if token:
        headers["X-Controller-Token"] = token
    return headers


_CONTROLLER_UNSET = ("CONTROLLER_URL is not set — register the Mobile Controller "
                     "in Settings → Plugins → Controller Plugin")


def _joined_errors(payload):
    """One line from a controller device list's per-platform `errors`, or None."""
    errors = payload.get("errors") or {}
    return "; ".join(f"{name}: {text}" for name, text in sorted(errors.items())) or None


def get_controller_devices():
    """Devices the controller serves now. Returns (devices, error), where each
    device is {"id", "platform", "model"}."""
    base = _controller_url()
    if not base:
        return [], _CONTROLLER_UNSET
    import urllib.request
    try:
        req = urllib.request.Request(sec.check_http_url(base + "/devices"),
                                     headers=_controller_headers())
        with urllib.request.urlopen(req, timeout=25) as resp:  # nosec B310
            payload = json.loads(resp.read().decode() or "{}")
    except Exception as e:
        return [], f"Mobile Controller unreachable at {base}: {e}"
    devices = payload.get("devices") or []
    error = _joined_errors(payload)
    if error and not devices:
        return [], error
    return devices, None


def get_active_android_devices():
    """Return (Android device ids, error)."""
    devices, error = get_controller_devices()
    return [d["id"] for d in devices if d.get("platform") == "android"], error


def get_connected_ios_devices():
    """Return (iPhone UDIDs, error)."""
    devices, error = get_controller_devices()
    return [d["id"] for d in devices if d.get("platform") == "ios"], error


_controller_devices_cache = {"devices": None, "at": 0.0}

DEVICE_EVENTS_TIMEOUT_SECS = 45  # the controller writes at least every 15s
DEVICE_EVENTS_RETRY_SECS = 3


def cached_controller_devices():
    """Device list cached ~5s so run-page polling doesn't hammer the controller.
    Empty when the controller is unset or unreachable."""
    import time as _t
    now = _t.time()
    cache = _controller_devices_cache
    if cache["devices"] is None or now - cache["at"] >= 5:
        cache["devices"], _ = get_controller_devices()
        cache["at"] = now
    return cache["devices"]


def device_display_name(device):
    return (load_device_names().get(device["id"]) or device.get("model")
            or device["id"])


def list_devices_for_ui():
    """Controller devices with their friendly names, for the viewer's pages."""
    devices, error = get_controller_devices()
    if error:
        print(f"Error fetching devices: {error}", file=sys.stderr, flush=True)
    return [{**d, "friendly_name": device_display_name(d)} for d in devices]


def controller_device_events():
    """Follow the controller's GET /devices/events forever. Yields each event's
    payload, or None when there is nothing new but the subscriber should still
    be written to. Failures are yielded as payloads, and it reconnects."""
    import urllib.request
    while True:
        base = _controller_url()
        if not base:
            yield {"devices": [], "error": _CONTROLLER_UNSET}
        else:
            try:
                req = urllib.request.Request(sec.check_http_url(base + "/devices/events"),
                                             headers=_controller_headers())
                with urllib.request.urlopen(  # nosec B310
                        req, timeout=DEVICE_EVENTS_TIMEOUT_SECS) as upstream:
                    for line in upstream:
                        if line.startswith(b"data:"):
                            yield json.loads(line[len(b"data:"):])
                        elif line.startswith(b":"):
                            yield None
            except Exception as e:
                yield {"devices": [], "error": f"Mobile Controller unreachable at {base}: {e}"}
        time.sleep(DEVICE_EVENTS_RETRY_SECS)
        yield None


def device_event_for_ui(payload):
    """A controller device event with friendly names, for the viewer's pages."""
    return {"devices": [{**d, "friendly_name": device_display_name(d)}
                        for d in payload.get("devices") or []],
            "error": payload.get("error") or _joined_errors(payload)}


def fetch_device_frame(device_id):
    """Fetch one PNG screenshot from the controller. Returns (bytes, error)."""
    base = _controller_url()
    if not base:
        return None, _CONTROLLER_UNSET
    import urllib.request
    try:
        req = urllib.request.Request(
            sec.check_http_url(f"{base}/devices/{quote(device_id, safe=':')}/frame"),
            headers=_controller_headers())
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            return resp.read(), None
    except Exception as e:
        return None, f"controller frame fetch failed: {e}"


DEVICE_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z:._-]{3,}")

SERVICE_NAME = "monkeytester"
API_VERSION = 1  # of this viewer's /health and /api/status; see AGENTS.md
CONTROLLER_API_VERSION = 2
VISION_API_VERSION = 2  # MonkeyTester-Plugin-AI; other OpenAI-style endpoints have no /health


def _get_json(url, headers=None, timeout=5):
    """GET and parse JSON. Returns (status, body), with status None if unreachable."""
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(sec.check_http_url(url), headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return None, {"reason": str(e)}


def controller_status():
    """The Mobile Controller's state, for /api/status."""
    base = _controller_url()
    if not base:
        return {"ok": False, "error": _CONTROLLER_UNSET}
    status, body = _get_json(f"{base}/health")
    if status is None:
        return {"ok": False, "url": base, "error": f"unreachable: {body['reason']}"}
    version = body.get("api_version")
    if version != CONTROLLER_API_VERSION:
        return {"ok": False, "url": base, "api_version": version,
                "error": f"API version is {version}, expected {CONTROLLER_API_VERSION}; "
                         "update whichever is older"}
    return {"ok": bool(body.get("ok")), "url": base, "api_version": version,
            "appium": body.get("appium"), "adb": body.get("adb")}


_PLUGIN_SELECTED_CACHE = {}  # plugin name -> (selected model id, when)

_DEPRECATION_LOGGED = set()


def log_deprecation(msg):
    """Print a deprecation once per process, so old configs say it without spam."""
    if msg not in _DEPRECATION_LOGGED:
        _DEPRECATION_LOGGED.add(msg)
        print(f"DEPRECATED: {msg}", file=sys.stderr, flush=True)


def token_info(value):
    """Read-only view of a credential: {configured, last4} — never the value."""
    v = (value or "").strip()
    return {"configured": bool(v), "last4": v[-4:] if v else ""}


MANAGED_AI_MESSAGE = "managed by the MonkeyTester app"


def managed_ai_plugin(env=None):
    """(url, token) of the monkeytester-ai plugin when the MonkeyTester Mac
    app manages it ("managed mode"), else None.

    The Mac launcher picks the AI plugin's port and pairing token fresh on
    every launch and passes them as MT_MANAGED_AI_PLUGIN_URL and
    MT_MANAGED_AI_PLUGIN_TOKEN. Both are required: with either one missing
    the feature is off and config.json is the only source, as before. In
    managed mode the env values win over config.json (visual_plugin_url,
    visual_api_key) for every plugin call, and nothing writes them to
    config.json."""
    env = os.environ if env is None else env
    url = (env.get("MT_MANAGED_AI_PLUGIN_URL") or "").strip().rstrip("/")
    token = (env.get("MT_MANAGED_AI_PLUGIN_TOKEN") or "").strip()
    if url and token:
        return url, token
    return None


def ai_plugin_url(cfg=None):
    """The monkeytester-ai plugin's address: MT_MANAGED_AI_PLUGIN_URL in
    managed mode (managed_ai_plugin), else Settings → Device Plugins
    (config.json visual_plugin_url), '' when none is set. A monkeytester-ai
    entry in MT_PLUGINS is imported once at start (import_ai_plugin_address)
    and ignored after that."""
    managed = managed_ai_plugin()
    if managed:
        return managed[0]
    cfg = load_config() if cfg is None else cfg
    return (cfg.get("visual_plugin_url") or "").strip().rstrip("/")


def ai_pairing_token(cfg=None):
    """The pairing token for every call to the monkeytester-ai plugin:
    MT_MANAGED_AI_PLUGIN_TOKEN in managed mode, else config.json
    visual_api_key; '' when none is set."""
    managed = managed_ai_plugin()
    if managed:
        return managed[1]
    cfg = load_config() if cfg is None else cfg
    return (cfg.get("visual_api_key") or "").strip()


_AI_IMPORT_MESSAGE = ("imported monkeytester-ai address from MT_PLUGINS; "
                      "the MT_PLUGINS entry is ignored from now on")


def import_ai_plugin_address(env=None):
    """One-time move of the monkeytester-ai address from MT_PLUGINS into
    config.json, run when the viewer starts. Only when config.json has no
    visual_plugin_url yet and MT_PLUGINS has a monkeytester-ai entry: writes
    visual_plugin_url (+ visual_plugin) atomically, keeping every other key
    (legacy visual_endpoint included, so a config that still has it keeps
    working). Afterwards a monkeytester-ai entry in MT_PLUGINS is ignored and
    that is logged once. Returns the imported URL, or ''.
    Does nothing in managed mode (the launcher app supplies the address on every
    launch)."""
    env = os.environ if env is None else env
    if managed_ai_plugin(env):
        return ""
    pc = _plugin_client()
    try:
        entries = dict(pc.parse_plugins(env.get("MT_PLUGINS", "")))
    except pc.PluginError as e:
        print(f"MT_PLUGINS is malformed, monkeytester-ai address not imported: {e.message}",
              file=sys.stderr, flush=True)
        return ""
    env_url = (entries.get(pc.AI_PLUGIN_NAME) or "").strip().rstrip("/")
    if not env_url:
        return ""
    with _CONFIG_LOCK:
        cfg = load_config() if CONFIG_FILE.exists() else {}
        if ai_plugin_url(cfg):
            print(f"MT_PLUGINS has a {pc.AI_PLUGIN_NAME} entry; it is ignored — the address "
                  "comes from Settings → Device Plugins (config.json visual_plugin_url)",
                  file=sys.stderr, flush=True)
            return ""
        cfg["visual_plugin_url"] = env_url
        cfg["visual_plugin"] = pc.AI_PLUGIN_NAME
        save_config(cfg)
    print(f"{_AI_IMPORT_MESSAGE} ({env_url})", file=sys.stderr, flush=True)
    return env_url


def import_device_plugins(env=None):
    """One-time move of the device plugins from MT_PLUGINS into config.json
    (the `plugins` map {name: url}), run when the viewer starts — the same
    pattern as import_ai_plugin_address: the first start copies every entry,
    afterwards Settings → Device Plugins owns the list and the environment
    variable is ignored (that is logged once). monkeytester-ai is skipped —
    its own import handles it. Nothing is written when MT_PLUGINS has no
    device entry, so a later Settings add seeds from the still-live env then.
    Returns the imported map."""
    env = os.environ if env is None else env
    pc = _plugin_client()
    try:
        entries = {name: url for name, url in pc.parse_plugins(env.get("MT_PLUGINS", ""))
                   if name != pc.AI_PLUGIN_NAME}
    except pc.PluginError as e:
        print(f"MT_PLUGINS is malformed, device plugins not imported: {e.message}",
              file=sys.stderr, flush=True)
        return {}
    with _CONFIG_LOCK:
        cfg = load_config() if CONFIG_FILE.exists() else {}
        if isinstance(cfg.get("plugins"), dict):
            if entries:
                print("MT_PLUGINS device-plugin entries are ignored — Settings → Device "
                      "Plugins (config.json plugins) owns the list now",
                      file=sys.stderr, flush=True)
            return {}
        if not entries:
            return {}
        cfg["plugins"] = entries
        save_config(cfg)
    print(f"imported {len(entries)} device plugin(s) from MT_PLUGINS into config.json; "
          "Settings → Device Plugins owns the list from now on", file=sys.stderr, flush=True)
    return entries


def import_controller_url(env=None):
    """One-time move of CONTROLLER_URL into config.json's controller_url key,
    run when the viewer starts — the same pattern as import_device_plugins and
    import_ai_plugin_address: the first start copies the env address when the
    key is absent, afterwards Settings → Plugins → Controller Plugin owns the
    address and the environment variable is ignored (that is logged once).
    Nothing is written when the env has no address, so a later Settings add
    seeds the slot from scratch."""
    env = os.environ if env is None else env
    with _CONFIG_LOCK:
        cfg = load_config() if CONFIG_FILE.exists() else {}
        if "controller_url" in cfg:
            if (env.get("CONTROLLER_URL") or "").strip():
                print("CONTROLLER_URL is ignored — Settings → Plugins → "
                      "Controller Plugin (config.json controller_url) owns "
                      "the address", file=sys.stderr, flush=True)
            return ""
        url = (env.get("CONTROLLER_URL") or "").strip().rstrip("/")
        if not url:
            return ""
        cfg["controller_url"] = url
        save_config(cfg)
    print("imported CONTROLLER_URL into config.json controller_url; "
          "Settings → Plugins → Controller Plugin owns the address from now on",
          file=sys.stderr, flush=True)
    return url


def plugin_base_url(name, env=None):
    """Base URL of a plugin: monkeytester-ai's comes from Settings
    (config.json visual_plugin_url), any other from the plugin list in effect
    (config.json `plugins` once Settings owns it, else MT_PLUGINS); '' when
    unset."""
    pc = _plugin_client()
    if name == pc.AI_PLUGIN_NAME:
        return ai_plugin_url()
    try:
        owned = pc.parse_config_plugins(load_config())
    except pc.PluginError:
        owned = None
    if owned is not None:
        return owned.get(name, "")
    try:
        for pname, url in pc.parse_plugins((env if env is not None else os.environ).get("MT_PLUGINS", "")):
            if pname == name:
                return url.rstrip("/")
    except pc.PluginError:
        return ""
    return ""


def visual_plugin_name(cfg=None):
    """The AI plugin selected from the Device Plugins panel, or '' for a legacy
    endpoint config. Always monkeytester-ai in managed mode."""
    if managed_ai_plugin():
        return _plugin_client().AI_PLUGIN_NAME
    cfg = load_config() if cfg is None else cfg
    return (cfg.get("visual_plugin") or "").strip()


def plugin_selected(name, timeout=3):
    """The model the AI plugin currently has selected (GET /models), cached for
    10s. '' when the plugin is unreachable — the service ignores the model
    field anyway, this only fills the request."""
    now = time.monotonic()
    hit = _PLUGIN_SELECTED_CACHE.get(name)
    if hit and now - hit[1] < 10:
        return hit[0]
    sel = ""
    base = plugin_base_url(name)
    if base:
        try:
            pc = _plugin_client()
            token = ai_pairing_token() or None
            body = pc.ai_models(pc.PluginClient(name, base, token=token), timeout=timeout)
            sel = str(body.get("selected") or "")
        except _plugin_client().PluginError:
            sel = ""
    _PLUGIN_SELECTED_CACHE[name] = (sel, now)
    return sel


def vision_settings():
    """(endpoint, api_key, model), with what Settings saved winning over the
    environment. This is the same order run_on_device.py applies.

    The provider is plugin-based: config.json names the monkeytester-ai
    plugin (visual_plugin) and its endpoint is the plugin's address from
    Settings (visual_plugin_url). A config.json that still carries visual_endpoint/visual_model works
    as before and logs a deprecation once."""
    cfg = load_config()
    plugin = visual_plugin_name(cfg)
    legacy_copilot_selected(cfg)
    if plugin:
        base = plugin_base_url(plugin)
        if base:
            return (base + "/v1/chat/completions",
                    (ai_pairing_token(cfg) or os.environ.get("VISUAL_MODEL_API_KEY") or "").strip(),
                    plugin_selected(plugin))
    if plugin or cfg.get("visual_endpoint") or cfg.get("visual_model"):
        log_deprecation(
            "config.json uses visual_endpoint/visual_model for the local provider; "
            "Settings → Device Plugins now uses the monkeytester-ai plugin at the address "
            "set there (visual_plugin_url). Old setups keep working; remove the old keys "
            "to silence this.")
    return ((cfg.get("visual_endpoint") or os.environ.get("VISION_MODEL_API_URL")
             or os.environ.get("VISUAL_MODEL_ENDPOINT") or "").strip(),
            (cfg.get("visual_api_key") or os.environ.get("VISUAL_MODEL_API_KEY") or "").strip(),
            (cfg.get("visual_model") or os.environ.get("VISION_MODEL_NAME")
             or os.environ.get("VISUAL_MODEL_NAME") or "").strip())


COPILOT_MOVED_MESSAGE = (
    "GitHub Copilot is now configured on the MonkeyTester-Plugin-AI plugin: Settings → "
    "Device Plugins → monkeytester-ai → Configure → GitHub Copilot")


def legacy_copilot_selected(cfg=None):
    """True for a config.json from before Copilot moved into the AI plugin
    (visual_provider "copilot"). Logs a deprecation once; the runner refuses
    to start with such a config, and the Configure panel shows the same hint.
    Saving from the panel rewrites it."""
    cfg = load_config() if cfg is None else cfg
    if (cfg.get("visual_provider") or "").strip() != "copilot":
        return False
    log_deprecation('config.json selects visual_provider "copilot", which the viewer no '
                    "longer handles. " + COPILOT_MOVED_MESSAGE)
    return True


def models_url(chat_url):
    """The OpenAI-style /models URL beside a /chat/completions URL."""
    parts = urlparse(chat_url.strip())
    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    elif not path.endswith("/v1"):
        path = path + "/v1" if path else "/v1"
    return parts._replace(path=path + "/models", query="", fragment="").geturl()


def list_local_models(chat_url, api_key=""):
    """Model ids an OpenAI-style endpoint offers (llama-server, Ollama, MonkeyTester-Plugin-AI).

    llama-server's router marks which model is loaded; that is passed through so
    the page can preselect it.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    status, body = _get_json(models_url(chat_url), headers=headers, timeout=8)
    if status is None:
        return {"ok": False, "error": f"unreachable: {body.get('reason')}", "models": []}
    if status != 200:
        return {"ok": False, "error": f"HTTP {status} from {models_url(chat_url)}", "models": []}
    models = []
    for m in body.get("data") or body.get("models") or []:
        mid = m.get("id") or m.get("name") or m.get("model") if isinstance(m, dict) else None
        if mid:
            state = (m.get("status") or {}).get("value") if isinstance(m.get("status"), dict) else None
            models.append({"id": mid, "loaded": state == "loaded"})
    return {"ok": True, "models": models}


def vision_status():
    """The vision model's state, for /api/status. /health is MonkeyTester-Plugin-AI's;
    an endpoint without one (llama.cpp, hosted APIs) is reported as unchecked.
    The new plugin-based local provider is checked through the plugin's own
    /health on the plugin contract."""
    if legacy_copilot_selected():
        return {"ok": False, "provider": "copilot", "error": COPILOT_MOVED_MESSAGE}
    plugin = visual_plugin_name()
    if plugin:
        base = plugin_base_url(plugin)
        if not base:
            return {"ok": False, "plugin": plugin,
                    "error": f"no address set for visual plugin {plugin!r} "
                             "(Settings → Device Plugins)"}
        pc = _plugin_client()
        client = pc.PluginClient(plugin, base)
        entry, body = pc._health_entry(client, 3)
        if not entry["ok"]:
            return {"ok": False, "plugin": plugin, "endpoint": base,
                    "provider": (body or {}).get("provider"),
                    "error": entry.get("error") or "plugin reports not ok"}
        # The plugin's /health names the active provider (local or copilot) and
        # its active model; the viewer only shows what the plugin reports.
        return {"ok": True, "plugin": plugin, "endpoint": base, "checked": True,
                "api_version": body.get("api_version"),
                "provider": str(body.get("provider") or "local"),
                "model": str(body.get("model") or "") or plugin_selected(plugin)}
    endpoint, _, _ = vision_settings()
    if not endpoint:
        return {"ok": False, "error": "No vision model endpoint configured"}
    parts = urlparse(endpoint)
    status, body = _get_json(f"{parts.scheme}://{parts.netloc}/health")
    if status is None:
        return {"ok": False, "endpoint": endpoint, "error": f"unreachable: {body['reason']}"}
    if body.get("service") != "monkeytester-ai":
        return {"ok": True, "endpoint": endpoint, "checked": False}
    version = body.get("api_version")
    if version != VISION_API_VERSION:
        return {"ok": False, "endpoint": endpoint, "api_version": version,
                "error": f"API version is {version}, expected {VISION_API_VERSION}; "
                         "update whichever is older"}
    return {"ok": bool(body.get("ok")), "endpoint": endpoint, "checked": True,
            "api_version": version, "model": body.get("model"),
            "model_loaded": body.get("model_loaded")}


def _plugin_client():
    """The runner's plugin_client module (mt-tests/scripts), so the viewer reads
    MT_PLUGINS exactly the way PLUGIN steps do."""
    scripts = str(Path(__file__).resolve().parent / "mt-tests" / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import plugin_client
    return plugin_client


def plugins_status():
    """Each device plugin's /health, for /api/status: the plugin list in effect
    (config.json `plugins` once Settings owns it, else MT_PLUGINS) minus
    monkeytester-ai, plus monkeytester-ai at its Settings address. Plugins are
    optional (only tests with PLUGIN steps need them), so they don't change
    the top-level ok."""
    cfg = load_config()
    return _plugin_client().plugins_status(timeout=3, ai_url=ai_plugin_url(cfg),
                                           ai_token=ai_pairing_token(cfg),
                                           config=cfg)


def _ai_pairing_tokens():
    """{plugin name: pairing token} for the AI plugin, so its auth-gated /config
    is read with the credential it generated (the viewer's visual_api_key)."""
    key = ai_pairing_token()
    if not key:
        return {}
    return {name: key for name in {visual_plugin_name() or "monkeytester-ai", "monkeytester-ai"}}


def plugins_overview():
    """Active device plugins with live state and devices, for the Settings page,
    plus ai_plugin {name, url, configured, token:{configured,last4}}.

    The monkeytester-ai row is ALWAYS listed, even with no address configured:
    its Configure panel (plugin address + pairing token) is where the first
    address gets set, so an unconfigured row shows "not configured" and the
    Configure button. The synthesized entry has ok False, no config probe
    result and error AI_ROW_NOT_CONFIGURED."""
    pc = _plugin_client()
    cfg = load_config()
    url = ai_plugin_url(cfg)
    key = ai_pairing_token(cfg)
    managed = bool(managed_ai_plugin())
    out = pc.plugins_overview(timeout=3, tokens=_ai_pairing_tokens(), ai_url=url,
                              ai_token=key, config=cfg)
    if not any(p.get("name") == pc.AI_PLUGIN_NAME for p in out.get("plugins", [])):
        out["plugins"] = list(out.get("plugins", [])) + [{
            "name": pc.AI_PLUGIN_NAME, "url": "", "ok": False,
            "contract_version": None, "plugin": None, "source": "settings",
            "capabilities": [], "devices": [], "config": None,
            "error": AI_ROW_NOT_CONFIGURED}]
    for row in out["plugins"]:
        if row.get("name") == pc.AI_PLUGIN_NAME:
            # managed: the launcher app supplies the address and token; the panel
            # shows the address read-only and the server refuses edits (409).
            row["managed"] = managed
    # Build plugin registrations from the same registry (object entries in
    # config.json `plugins`), with their live /health — the Settings → Plugins
    # page's Build Plugin section lists them under kind "build".
    for name, entry in _builds().registry_plugins().items():
        try:
            row = _builds().plugin_health(_builds().load_config(), name)
        except builds.BuildError as e:
            row = {"name": name, "url": (entry or {}).get("url") or "", "ok": False,
                   "error": str(e)}
        row["source"] = "settings"
        row["kind"] = "build"
        out["plugins"].append(row)
    out["ai_plugin"] = {"name": pc.AI_PLUGIN_NAME, "url": url, "configured": bool(url),
                        "token": token_info(key), "managed": managed}
    return out


AI_NOT_CONFIGURED = ("no monkeytester-ai plugin configured — set its address first "
                     "in Settings → Device Plugins (its Configure panel)")

AI_ROW_NOT_CONFIGURED = ("not configured — set the plugin address in the row's "
                         "Configure panel")


def _changes_managed_ai_plugin(data):
    """True when a POST /api/config/visual-model body would change the AI
    plugin's address or pairing token, or remove the plugin: plugin_url
    (set or remove), set_token, generate_token, clear_token, and the legacy
    api_key / clear_api_key. Refused with 409 in managed mode."""
    if any(k in data for k in ("plugin_url", "set_token", "api_key")):
        return True
    return any(data.get(k) for k in ("generate_token", "clear_token", "clear_api_key"))


def _ai_plugin_address_op(url, api_key=None):
    """Set or remove the monkeytester-ai plugin's address (config.json
    visual_plugin_url + visual_plugin). Shared by POST /api/config/visual-model
    {plugin_url, api_key?} and the Settings → Plugins Add row (POST
    /api/config/plugins, a set op whose address is monkeytester-ai, or
    {"remove": "monkeytester-ai"}). Validate first — GET <url>/health must
    speak the plugin contract and name plugin monkeytester-ai, and GET
    <url>/config with the pairing token (api_key if given, else the stored
    one) must answer 200 — and store nothing when that fails. url "" removes
    the plugin. Caller holds _CONFIG_LOCK. Returns (http status, body)."""
    pc = _plugin_client()
    cfg = load_config()
    if not url.strip():
        cfg.pop("visual_plugin_url", None)
        if (cfg.get("visual_plugin") or "").strip() == pc.AI_PLUGIN_NAME:
            cfg.pop("visual_plugin", None)
        save_config(cfg)
        _PLUGIN_SELECTED_CACHE.clear()
        print("monkeytester-ai plugin address removed", file=sys.stderr, flush=True)
        return 200, {"saved": True, "plugin": "", "plugin_url": "",
                     "token": token_info(cfg.get("visual_api_key") or "")}
    given = api_key.strip() if isinstance(api_key, str) else ""
    token = given or (cfg.get("visual_api_key") or "").strip()
    try:
        url = pc.ai_validate_address(url, token=token or None, timeout=5)
    except pc.PluginError as e:
        status = 400 if e.code in ("invalid_request", "wrong_plugin", "contract") else 502
        return status, {"error": e.message, "code": e.code,
                        "plugin": pc.AI_PLUGIN_NAME, "plugin_status": e.status}
    cfg["visual_plugin_url"] = url
    cfg["visual_plugin"] = pc.AI_PLUGIN_NAME
    cfg.setdefault("visual_provider", "local")
    if given:
        cfg["visual_api_key"] = given
    save_config(cfg)
    _PLUGIN_SELECTED_CACHE.clear()
    print(f"monkeytester-ai plugin address set to {url}", file=sys.stderr, flush=True)
    return 200, {"saved": True, "plugin": pc.AI_PLUGIN_NAME, "plugin_url": url,
                 "token": token_info(cfg.get("visual_api_key") or "")}


def ai_plugin_client(cfg=None):
    """(plugin_client module, PluginClient) for the monkeytester-ai plugin at
    its Settings address (config.json visual_plugin_url), authenticated with
    the pairing token (visual_api_key). Raises PluginError(code=
    "not_configured") when no address is set."""
    pc = _plugin_client()
    cfg = load_config() if cfg is None else cfg
    url = ai_plugin_url(cfg)
    if not url:
        raise pc.PluginError(AI_NOT_CONFIGURED, "not_configured")
    key = ai_pairing_token(cfg)
    return pc, pc.PluginClient(pc.AI_PLUGIN_NAME, url, token=key or None)


def visual_plugin_info(timeout=6):
    """Live view of the AI plugin for the Device Plugins panel (GET /api/visual-model/plugin).

    The plugin owns its whole provider config (local models, remote server,
    Copilot, the pairing token): this checks the monkeytester-ai plugin at its
    Settings address (plugin contract /health plus a working GET /models) and
    proxies its /models and /config. Credentials (pairing token, remote API
    key, GitHub token) are only ever seen here as {configured, last4}.
    """
    pc = _plugin_client()
    cfg = load_config()
    url = ai_plugin_url(cfg)
    clients = {}
    if url:
        clients[pc.AI_PLUGIN_NAME] = pc.PluginClient(pc.AI_PLUGIN_NAME, url)

    def probe(client):
        entry, _body = pc._health_entry(client, 3)
        info = {"name": client.name, "url": client.url, "ok": False,
                "contract_version": entry["contract_version"],
                "plugin": entry["plugin"], "error": entry.get("error"),
                "models": [], "selected": "", "installed": []}
        if info["error"]:
            return info
        try:
            body = pc.ai_models(client, timeout=timeout)
        except pc.PluginError as e:
            info["error"] = (f"{e.message} — a plugin without /models is not "
                             "a monkeytester-ai plugin")
            return info
        for m in body.get("data") or body.get("models") or []:
            if isinstance(m, dict) and m.get("id"):
                info["models"].append(str(m["id"]))
            elif isinstance(m, str):
                info["models"].append(m)
        info["ok"] = True
        info["selected"] = str(body.get("selected") or "")
        info["installed"] = [str(x) for x in (body.get("installed") or [])]
        return info

    key = ai_pairing_token(cfg)
    for client in clients.values():
        client.token = key or None
    infos = pc._each_plugin(clients, probe)
    qualified = [i for i in infos if i["ok"]]
    saved = visual_plugin_name(cfg)
    chosen = None
    for want in [saved, "monkeytester-ai"]:
        if chosen:
            break
        for i in qualified:
            if i["name"] == want:
                chosen = i
                break
    if not chosen and len(qualified) == 1:
        chosen = qualified[0]
    token = token_info(key)
    provider, copilot, config_error = "local", pc.public_copilot({}), None
    remote = pc.public_remote({})
    if chosen:
        try:
            public = pc.ai_config(clients[chosen["name"]], timeout=timeout)
            token = public["token"]
            provider, copilot, remote = public["provider"], public["copilot"], public["remote"]
            model = public["model"]
            # /config's model block is the LOCAL provider's (Ollama) list and
            # selection; /models follows whichever provider is active, so with
            # Copilot active it lists Copilot models. Prefer /config here.
            chosen["selected"] = model["selected"] or chosen["selected"]
            chosen["installed"] = model["installed"] or chosen["installed"]
        except pc.PluginError as e:
            config_error = {"code": e.code, "message": e.message}  # keep what /models told us
    return {"ok": bool(chosen),
            "plugin": chosen["name"] if chosen else "",
            "url": chosen["url"] if chosen else "",
            "models": chosen["models"] if chosen else [],
            "selected": chosen["selected"] if chosen else "",
            "installed": chosen["installed"] if chosen else [],
            "token": token,
            "provider": provider,
            "copilot": copilot,
            "remote": remote,
            "config_error": config_error,
            "legacy_copilot": legacy_copilot_selected(),
            "legacy_hint": COPILOT_MOVED_MESSAGE if legacy_copilot_selected() else "",
            "plugins": infos,
            "configured": bool(url),
            "managed": bool(managed_ai_plugin()),
            "error": None if chosen else (
                (infos[0].get("error") or "monkeytester-ai is not answering") if infos
                else AI_NOT_CONFIGURED)}


def has_path_traversal(path):
    """True if a URL path could step outside the directory it's joined to.

    Run names and file paths from URLs are joined onto Run/, so this is checked once
    for every request, before any route. Encoded slashes count, because routes decode
    their segments and an absolute segment would replace the base path entirely.
    """
    decoded = unquote(path)
    lowered = path.lower()
    return ("\\" in decoded or "\0" in decoded or "%2f" in lowered or "%5c" in lowered
            or ".." in decoded.split("/"))


def resolve_run_targets(fields):
    """Resolve launch targets while preserving the legacy explicit device field."""
    mode = (fields.get("device_mode") or "specific").strip().lower()
    if mode not in {"specific", "android", "ios", "all"}:
        return None, "Invalid device mode"

    explicit = (fields.get("device") or "").split()
    if mode == "specific":
        if not explicit:
            return None, "At least one device is required"
        for device in explicit:
            # Device ids reach the runner's argv: allowlisted, never option-like.
            if not sec.valid_device_id(device):
                return None, f"Invalid device id {device[:64]!r}"
        android = [device for device in explicit if ":" in device]
        ios = []
        # IP:PORT is always Android. Anything else (a USB serial or an iPhone
        # UDID) has to be a device the controller serves, which gives its platform.
        others = [device for device in explicit if ":" not in device]
        if others:
            devices, _ = get_controller_devices()
            platforms = {d["id"]: d.get("platform") for d in devices}
            for device in others:
                if platforms.get(device) == "android":
                    android.append(device)
                elif platforms.get(device) == "ios":
                    ios.append(device)
                else:
                    return None, f"Device {device} is not connected to the Mobile Controller"
        return {"android": android, "ios": ios, "ios_placeholder": False}, None

    android = []
    if mode in {"android", "all"}:
        android, error = get_active_android_devices()
        if error:
            return None, error
        if not android and mode == "android":
            return None, "No active Android devices were found"

    ios = []
    if mode in {"ios", "all"}:
        ios, error = get_connected_ios_devices()
        if error:
            return None, error
        if not ios and mode == "ios":
            return None, "No connected iOS devices were found (connect an iPhone via USB)"

    return {
        "android": android,
        "ios": ios,
        "ios_placeholder": (mode in {"ios", "all"}) and not ios,
    }, None


MT_TESTS_DIR = Path(__file__).parent / "mt-tests"
RUN_ON_DEVICE_SCRIPT = MT_TESTS_DIR / "scripts" / "run_on_device.py"
UPLOADS_DIR = TEST_DIR / ".uploads"  # staging area, inside the already-gitignored Run/
MAX_APK_BYTES = 500 * 1024 * 1024
MAX_TXT_BYTES = 2 * 1024 * 1024
# SNAPSHOT baselines uploaded with a case: snapshots/<platform>/<W>x<H>@<scale>x/<name>.png
SNAPSHOT_PATH_RE = re.compile(
    r"^snapshots/(android|ios)/\d{2,5}x\d{2,5}@\d+(\.\d+)?x/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.png$")
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_BUILD_LOG_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_FILES = 1000
CASE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
# Payload files a case's tests reference with `@file:<rel>` (sent as `payloads`
# parts, staged next to the tests): each path segment is a safe name.
PAYLOAD_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}$")
MAX_PAYLOAD_BYTES = 4096
MAX_PAYLOAD_FILES_PER_CASE = 32
RUN_ID_PATTERN = re.compile(r"^run_\d+$")
RUN_EXIT_CODE_FILE = "exit_code"
_RUN_LAUNCH_LOCK = threading.Lock()
# Runs execute one after another in the order they were submitted (Android and
# iOS share the one ESP32 board): each new run is told the previous run's
# process and waits for it to finish before touching any device.
_PREVIOUS_RUN = {"proc": None, "run_id": None}

# ── Press-event overlay ──
# The test runner (action_executor / ios_action_executor) posts every
# tap/swipe to /api/press-event while a run is active. The run detail page
# polls /api/press-events/<run> and draws markers on the live stream at the
# exact device pixels where the test is pressing. Events are kept in memory
# only, per run, with a short TTL — they are a live visual aid, not history.
_PRESS_EVENTS = {}
_PRESS_LOCK = threading.Lock()
_PRESS_TTL_SECONDS = 30.0
_PRESS_MAX_PER_RUN = 200


def _press_prune_locked():
    """Drop expired events; runs with no events are removed entirely."""
    now = time.time()
    for run in list(_PRESS_EVENTS.keys()):
        evs = _PRESS_EVENTS[run]
        fresh = [e for e in evs if now - e.get("ts", 0) <= _PRESS_TTL_SECONDS]
        if fresh:
            _PRESS_EVENTS[run] = fresh[-_PRESS_MAX_PER_RUN:]
        else:
            del _PRESS_EVENTS[run]


def _press_add(event):
    run = event.get("run")
    if not run or not RUN_ID_PATTERN.match(run):
        return
    with _PRESS_LOCK:
        _press_prune_locked()
        _PRESS_EVENTS.setdefault(run, []).append(event)


# Read-modify-write of Run/config.json happens on several request threads
# (and once at start for the monkeytester-ai address import).
_CONFIG_LOCK = threading.RLock()


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"video_size": "100%"}


def save_config(data):
    """Write Run/config.json atomically (temp file + rename), so a runner
    reading it at import never sees a half-written file. Always mode 0600:
    it holds the AI pairing token and build-plugin tokens."""
    sec.write_private_json(CONFIG_FILE, data)


def detect_file_type(filepath):
    ext = filepath.suffix.lower()
    if ext in (".mp4",):
        return "video"
    elif ext in (".png", ".jpg", ".jpeg", ".webp"):
        return "image"
    elif ext == ".json":
        return "json"
    elif ext == ".txt":
        return "text"
    try:
        with open(filepath, "rb") as f:
            header = f.read(16)
        if header[:8] == b"\x89PNG\r\n\x1a\n":
            return "image"
        elif header[:6] in (b"\xff\xd8\xff",):
            return "image"
        elif header[:4] in (b"\x1a\x45df", b"ftyp", b"\x00\x00\x00\x18ftyp"):
            return "video"
    except Exception:
        pass
    return "other"


def parse_multipart_form(handler, max_body_bytes=None):
    """Hand-rolled multipart/form-data parser (stdlib only).

    Returns (fields, files):
      fields: {name: str}                                   -- plain form fields
      files:  {name: [{"filename": str, "content": bytes}]} -- file parts,
              grouped by field name (an <input multiple> produces several
              parts sharing the same name).
    """
    ctype = handler.headers.get("Content-Type", "")
    if not ctype.startswith("multipart/form-data"):
        raise ValueError("Expected multipart/form-data request")

    boundary = None
    for chunk in ctype.split(";"):
        chunk = chunk.strip()
        if chunk.startswith("boundary="):
            boundary = chunk[len("boundary="):].strip('"')
    if not boundary:
        raise ValueError("Missing multipart boundary")

    if max_body_bytes is None:
        max_body_bytes = sec.max_upload_bytes()
    try:
        length = sec.body_length(handler.headers, max_body_bytes)
    except sec.BodyError as e:
        raise ValueError(e.message) from None
    if length <= 0:
        raise ValueError("Empty request body")

    body = handler.rfile.read(length)
    delimiter = b"--" + boundary.encode()

    fields, files = {}, {}
    for part in body.split(delimiter):
        if part in (b"", b"--\r\n", b"--"):
            continue
        part = part[2:] if part[:2] == b"\r\n" else part  # strip leading CRLF after delimiter
        header_blob, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue  # not a real part (e.g. trailing "--")
        if content.endswith(b"\r\n"):
            content = content[:-2]

        headers = {}
        for line in header_blob.split(b"\r\n"):
            if b":" in line:
                k, _, v = line.partition(b":")
                headers[k.strip().lower().decode()] = v.strip().decode()

        disp = headers.get("content-disposition", "")
        name_m = re.search(r'name="([^"]*)"', disp)
        file_m = re.search(r'filename="([^"]*)"', disp)
        if not name_m:
            continue
        field = name_m.group(1)

        if file_m and file_m.group(1):
            files.setdefault(field, []).append({
                "filename": os.path.basename(file_m.group(1)),  # strip any client-sent path
                "path": file_m.group(1),  # as sent; only for parts that validate it (snapshots, payloads)
                "content": content,
            })
        else:
            fields[field] = content.decode("utf-8", errors="replace")

    return fields, files


def _payload_rel_error(rel):
    """Why `rel` is not an acceptable payload path inside a case, or None.
    It must be relative, posix, normalized, made of safe names, and not a
    `.txt` at the case root (the runner would run that as a test)."""
    if not rel or "\\" in rel or rel.startswith("/"):
        return "must be a relative posix path"
    segments = rel.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            return "must be normalized (no empty, '.' or '..' segments)"
        if not PAYLOAD_SEGMENT_RE.match(seg):
            return ("may only use letters, digits, _ . - in each path segment "
                    "(not starting with '.')")
    if len(segments) == 1 and seg.lower().endswith(".txt"):
        return "is a .txt at the case root, which would run as a test"
    return None


def _plan_payloads(payload_parts, multi, cases, staged_paths):
    """Check the `payloads` parts of an upload; return ([(rel_path, bytes)], None)
    or (None, error). In a multi-case upload each part's path is
    `<case>/<rel>` for an uploaded case, in a single-case upload `<rel>`."""
    files, per_case = [], {}
    taken = set(staged_paths)
    dirs = {"/".join(p.split("/")[:i]) for p in taken for i in range(1, p.count("/") + 1)}
    for f in payload_parts:
        sent = f.get("path") or ""
        if multi:
            case, sep, rel = sent.partition("/")
            if not sep or case not in cases:
                return None, f"Payload {sent!r} must be <case>/<path> for an uploaded case"
        else:
            case, rel = "", sent
        problem = _payload_rel_error(rel)
        if problem:
            return None, f"Payload {sent!r} {problem}"
        if len(f["content"]) > MAX_PAYLOAD_BYTES:
            return None, f"Payload {sent!r} is larger than {MAX_PAYLOAD_BYTES} bytes"
        if not f["content"]:
            return None, f"Payload {sent!r} is empty (boards stage 1-{MAX_PAYLOAD_BYTES} bytes)"
        dest = f"{case}/{rel}" if multi else rel
        parents = {"/".join(dest.split("/")[:i]) for i in range(1, dest.count("/") + 1)}
        if dest in taken or dest in dirs or parents & taken:
            return None, f"Payload {sent!r} collides with another uploaded file"
        per_case[case] = per_case.get(case, 0) + 1
        if per_case[case] > MAX_PAYLOAD_FILES_PER_CASE:
            where = f"case {case}" if multi else "the case"
            return None, f"At most {MAX_PAYLOAD_FILES_PER_CASE} payload files per case ({where})"
        taken.add(dest)
        dirs |= parents
        files.append((dest, f["content"]))
    return files, None


def plan_test_upload(txt_parts, snapshot_parts, payload_parts=()):
    """Check the test files of a /api/run-test upload and decide where each
    one is staged (relative to the run's tests dir).

    Two layouts:
    - single case: flat ``INSTRUCTION.txt`` + test ``.txt`` files, snapshots
      as ``snapshots/<platform>/<W>x<H>@<scale>x/<name>.png``;
    - multi-case (one run for every case of a CI build): every test file is
      sent as ``<case>/<file>.txt`` and every snapshot as
      ``<case>/snapshots/...``; each case needs its own INSTRUCTION.txt and at
      least one test.

    `payload_parts` are the files the tests reference with `@file:<rel>`:
    sent as ``<rel>`` (single case) or ``<case>/<rel>`` (multi-case) and
    staged at that path next to the case's tests (see _plan_payloads).

    Returns ({"cases": [names] or [], "files": [(rel_path, bytes)]}, None) or
    (None, error).
    """
    for f in txt_parts:
        if len(f["content"]) > MAX_TXT_BYTES:
            return None, f"{f['filename']} exceeds size limit"
    if len(snapshot_parts) > MAX_SNAPSHOT_FILES:
        return None, f"At most {MAX_SNAPSHOT_FILES} snapshot baselines per run"
    for f in snapshot_parts:
        if not (0 < len(f["content"]) <= MAX_SNAPSHOT_BYTES) or not f["content"].startswith(b"\x89PNG"):
            return None, f"Snapshot {f['path']} is not a PNG within the size limit"

    multi = any("/" in (f.get("path") or "") for f in txt_parts)
    files = []
    if not multi:
        names = [f["filename"] for f in txt_parts]
        if names.count("INSTRUCTION.txt") != 1:
            return None, "Exactly one INSTRUCTION.txt file is required"
        if len(names) < 2:
            return None, "At least one test .txt file (besides INSTRUCTION.txt) is required"
        for f in snapshot_parts:
            if not SNAPSHOT_PATH_RE.match(f["path"]):
                return None, (f"Snapshot {f['path']!r} must be "
                              f"snapshots/<android|ios>/<W>x<H>@<scale>x/<name>.png")
        files += [(f["filename"], f["content"]) for f in txt_parts]
        files += [(f["path"], f["content"]) for f in snapshot_parts]
        payloads, error = _plan_payloads(payload_parts, False, [], [p for p, _ in files])
        if error:
            return None, error
        return {"cases": [], "files": files + payloads}, None

    by_case = {}
    setup_root = []
    for f in txt_parts:
        path = f.get("path") or ""
        if path == "SETUP.txt" and f["filename"] == "SETUP.txt":
            # Run-wide suite order, staged at the tests root (runner reads
            # it to fix the case order).
            setup_root = [("SETUP.txt", f["content"])]
            continue
        case, sep, name = path.partition("/")
        if not sep or not CASE_NAME_RE.match(case) or name != f["filename"] or "/" in name:
            return None, f"Test file {f.get('path')!r} must be <case>/<file>.txt in a multi-case upload"
        by_case.setdefault(case, []).append(f)
    for case, parts in sorted(by_case.items()):
        names = [p["filename"] for p in parts]
        if names.count("INSTRUCTION.txt") != 1:
            return None, f"Case {case}: exactly one INSTRUCTION.txt file is required"
        if len(names) < 2:
            return None, f"Case {case}: at least one test .txt file (besides INSTRUCTION.txt) is required"
        files += [(f"{case}/{p['filename']}", p["content"]) for p in parts]
    for f in snapshot_parts:
        case, sep, rest = (f.get("path") or "").partition("/")
        if not sep or case not in by_case or not SNAPSHOT_PATH_RE.match(rest):
            return None, (f"Snapshot {f['path']!r} must be "
                          f"<case>/snapshots/<android|ios>/<W>x<H>@<scale>x/<name>.png for an uploaded case")
        files.append((f"{case}/{rest}", f["content"]))
    payloads, error = _plan_payloads(payload_parts, True, by_case, [p for p, _ in files])
    if error:
        return None, error
    return {"cases": sorted(by_case), "files": setup_root + files + payloads}, None


def _setup_log_url(run, run_dir, log, device_id=None):
    """URL of the log an UNAVAILABLE test links to: the run's console log, or
    a log file (e.g. the uploaded build log) inside the run dir."""
    if not log:
        return None
    if log == "console":
        return "/api/logs/" + quote(run)
    path = (run_dir / log).resolve()
    try:
        rel = path.relative_to(run_dir.resolve())
    except ValueError:
        return None
    return "/static/" + run + "/" + quote(str(rel)) if path.is_file() else None


def _read_exit_code(run):
    try:
        return int((TEST_DIR / run / RUN_EXIT_CODE_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def get_run_result(run):
    """Outcome of a run for CI: status is "running", "passed" or "failed".

    "passed" must be proven: finished, runner exit 0 when known, at least one
    test, and no failures. Anything else that has finished is "failed".
    """
    meta = get_run_metadata(run) or {}
    exit_code = _read_exit_code(run)
    counts = {key: int(meta.get(key) or 0)
              for key in ("passed", "failed", "cancelled", "unavailable", "total")}
    meta_status = meta.get("status")
    if meta_status == "running" and not _is_running(run):
        # Stale: _is_running stamped it interrupted — re-read the metadata.
        meta = get_run_metadata(run) or meta
        meta_status = meta.get("status")
    finished = meta_status in ("completed", "completed_with_failures", "failed",
                               "interrupted", "aborted")

    # The runner's exit is authoritative. Without it (viewer restarted during
    # the run, or a run started elsewhere) rely on finalized metadata.
    if exit_code is None and not finished:
        return {"status": "running", "reason": None, **counts}
    if exit_code not in (None, 0):
        reason = meta.get("error") or f"runner exited with code {exit_code}"
    elif meta_status != "completed":
        reason = meta.get("error") or f"run ended with status {meta_status}"
    elif counts["failed"]:
        reason = f"{counts['failed']} test(s) failed"
    elif counts["unavailable"]:
        reason = f"{counts['unavailable']} test(s) unavailable (setup failed)"
    elif not counts["total"]:
        reason = "no tests were executed"
    elif meta.get("record_snapshots"):
        # Record mode captured baselines without comparing: never a pass.
        return {"status": "recording",
                "reason": "record mode: snapshots were saved as new baselines, not compared",
                **counts}
    else:
        return {"status": "passed", "reason": None, **counts}
    return {"status": "failed", "reason": reason, **counts}


# A run whose metadata still says "running" but which has written nothing for
# this long has lost its runner (host shutdown, crash, kill -9): nothing will
# ever finalize it, so the watchdog stamps it "interrupted" and it reports as
# an error instead of hanging on "Running" forever.
STALE_RUN_SECS = int(os.environ.get("MT_STALE_RUN_SECS", "3600"))
RUN_TERMINAL_STATUSES = ("completed", "completed_with_failures", "failed",
                         "interrupted", "aborted")
RUN_TERMINAL_PHASES = ("completed", "completed_with_failures", "failed",
                       "aborted", "interrupted")


def _run_last_activity(name):
    """Most recent file-modification time (epoch) anywhere in the run dir.

    The runner touches console.log, pending_tests.json and result files at
    every step, so a run with no recent write has no live process behind it.
    """
    newest = None
    for root, _dirs, files in os.walk(TEST_DIR / name):
        for fname in files:
            try:
                mtime = os.stat(os.path.join(root, fname)).st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
    return newest


def _interrupt_stale_run(name):
    """Stamp a stalled "running" run as interrupted so its state sticks."""
    reason = ("interrupted: no run activity for over "
              f"{STALE_RUN_SECS // 60} minutes — the runner process died "
              "(host shutdown, crash or kill)")
    meta_path = TEST_DIR / name / "run_metadata.json"
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        meta = None
    if meta is not None and meta.get("status") == "running":
        meta["status"] = "interrupted"
        meta["error"] = reason
        meta["completed"] = time.strftime("%Y-%m-%d %H:%M:%S")
        for dev in meta.get("devices", []):
            if dev.get("status") == "running":
                dev["status"] = "interrupted"
                dev["completed"] = meta["completed"]
        try:
            meta_path.write_text(json.dumps(meta, indent=2))
        except OSError:
            pass
    # Terminalize the progress files so the run page stops rendering rows as
    # running: the run-root file and every device's own file.
    run_dir = TEST_DIR / name
    pending_files = [run_dir / "pending_tests.json"]
    devices_dir = run_dir / "devices"
    if devices_dir.is_dir():
        pending_files += sorted(devices_dir.glob("*/pending_tests.json"))
    for pf in pending_files:
        try:
            pt = json.loads(pf.read_text())
        except (OSError, ValueError):
            continue
        progress = pt.get("progress")
        if progress and progress.get("phase") not in RUN_TERMINAL_PHASES:
            progress["phase"] = "interrupted"
            progress["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                pf.write_text(json.dumps(pt, indent=2))
            except OSError:
                pass


def _is_running(name):
    """Return True if a run is still in progress (metadata or pending state).

    A "running" run with no file activity for STALE_RUN_SECS is interrupted on
    the spot (see _interrupt_stale_run) and reported as finished.
    """
    # Check run_metadata.json
    meta = get_run_metadata(name)
    if meta and meta.get("status") == "running":
        last = _run_last_activity(name)
        if last is None or time.time() - last > STALE_RUN_SECS:
            _interrupt_stale_run(name)
            return False
        return True
    # A finalized root metadata means the run is done, even if a stale
    # pending_tests.json still claims a non-terminal phase. This happens
    # with multi-device runs: progress writes go to devices/<id>/
    # pending_tests.json, leaving the root file stuck at the last phase
    # written before the device loop started.
    if meta and meta.get("status") in RUN_TERMINAL_STATUSES:
        return False
    # Check pending_tests.json
    pending_files = []
    devices_dir = TEST_DIR / name / "devices"
    if devices_dir.is_dir():
        # Multi-device run: progress is tracked per device. Device dirs are
        # named device_N for Android AND ios_device_N for iOS — glob "*" or
        # iOS progress becomes invisible.
        pending_files = sorted(devices_dir.glob("*/pending_tests.json"))
    root_pf = TEST_DIR / name / "pending_tests.json"
    if root_pf.exists() and not pending_files:
        pending_files = [root_pf]
    for pf in pending_files:
        try:
            pt = json.loads(pf.read_text())
            phase = pt.get("progress", {}).get("phase")
            if phase and phase not in RUN_TERMINAL_PHASES:
                # A pending file untouched since the stale threshold has no
                # live runner either (run killed before it wrote any
                # metadata): terminalize and do not count as running.
                try:
                    stale = time.time() - pf.stat().st_mtime > STALE_RUN_SECS
                except OSError:
                    stale = False
                if stale:
                    pt.setdefault("progress", {})["phase"] = "interrupted"
                    pt["progress"]["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        pf.write_text(json.dumps(pt, indent=2))
                    except OSError:
                        pass
                else:
                    return True
            for t in pt.get("tests", []):
                if t.get("status") in ("running", "pending"):
                    return True
        except Exception:
            pass
    return False


# ─── Projects ───
# A project is one repo (owner/repo) on one build plugin (Settings →
# Configuration → Build & Test). A run records its repo in run_metadata.json
# as `repo`; runs without one (uploads with no repo, and every run from
# before projects existed) are Unassigned.
UNASSIGNED_PROJECT = "_unassigned"
SUBMIT_SCRIPT = Path(__file__).parent / "ci" / "mt-submit" / "submit.py"


def run_repo(fields):
    """The repo a new run belongs to, or "".

    A run started by a viewer build takes the build's repo: the viewer chose
    it, so an upload can't file the run under another project. Otherwise the
    `repo` form field (submit.py --repo) is used when it looks like owner/repo.
    """
    build_request = (fields.get("build_request") or "").strip()
    if build_request:
        try:
            build = _builds().load_build(build_request)
        except Exception:
            build = None
        if build and build.get("repo"):
            return build["repo"]
    repo = (fields.get("repo") or "").strip().strip("/")
    return repo if builds.REPO_RE.match(repo) else ""


COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")
COMMIT_MESSAGE_MAX = 4000


def clean_commit_message(text):
    """A commit message fit for run_metadata.json and the runner's env:
    control characters (bar newline and tab) dropped, trailing blanks
    trimmed, capped at COMMIT_MESSAGE_MAX characters."""
    text = re.sub(r"[^\S\n]*\r?\n", "\n", str(text or ""))
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text.strip()[:COMMIT_MESSAGE_MAX]


def legacy_gitea_plugin(cfg, repo=""):
    """The build plugin the deprecated /api/config/gitea* routes act on: the
    repo's project plugin, else build-local when registered, else the only
    (or first) registered plugin, else "build-local"."""
    for p in cfg["projects"]:
        if repo and p["repo"] == repo:
            return p["plugin"]
    if builds.MIGRATED_PLUGIN in cfg["plugins"] or not cfg["plugins"]:
        return builds.MIGRATED_PLUGIN
    return next(iter(cfg["plugins"]))


def legacy_gitea_settings(settings):
    """/api/config/build's body plus the old /api/config/gitea fields
    (url, configured, user, last4, saved_at, repos, branches), read from the
    plugin the alias acts on. Deprecated."""
    cfg = builds.load_config()
    name = legacy_gitea_plugin(cfg)
    health = next((p for p in settings.get("plugins", []) if p.get("name") == name), {}) or {}
    forge = health.get("forge") or {}
    projects = settings.get("projects", [])
    return {**settings, "deprecated": "use /api/config/build",
            "url": forge.get("url") or "", "configured": bool(forge.get("configured")),
            "user": forge.get("user") or "", "last4": "", "saved_at": "",
            "repos": [p["repo"] for p in projects],
            "branches": {p["repo"]: p["branch"] for p in projects if p.get("branch")}}


def clean_test_variables(tv, label):
    """Validate a {KEY: value} map from Settings -> Test Variables.

    Returns (clean map with upper-cased keys, None) or (None, error message).
    """
    if not isinstance(tv, dict):
        return None, f"{label} must be a JSON object"
    clean = {}
    for key, value in tv.items():
        key = str(key).strip().upper()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            return None, f"bad variable name {key!r}"
        if not isinstance(value, str):
            return None, f"value of {key} must be a string"
        clean[key] = value
    return clean, None


def run_project(meta):
    """A run's repo from its metadata, or "" when Unassigned."""
    repo = str((meta or {}).get("repo") or "").strip()
    return repo if builds.REPO_RE.match(repo) else ""


def run_commit(meta):
    """{commit, commit_message} from a run's metadata; "" for runs that
    recorded none (older runs, uploads without a checkout)."""
    meta = meta or {}
    commit = str(meta.get("commit") or "").strip().lower()
    return {"commit": commit if COMMIT_RE.fullmatch(commit) else "",
            "commit_message": clean_commit_message(meta.get("commit_message"))}


def project_matches(meta, project):
    """True when a run belongs to the dashboard's project filter.

    project: "" (all), UNASSIGNED_PROJECT, or owner/repo.
    """
    if not project:
        return True
    repo = run_project(meta)
    if project == UNASSIGNED_PROJECT:
        return not repo
    return repo == project


def list_projects():
    """{projects: [...], unassigned: bool} for the dashboard filter.

    Projects from Settings -> Build & Test come first in their saved order,
    then any other repo a run recorded (e.g. CI from a repo not in the list).
    """
    try:
        configured = builds.repos()
    except Exception:
        configured = []
    seen, unassigned = [], False
    for d in TEST_DIR.iterdir():
        if not d.is_dir() or not re.match(r"^run_\d+$", d.name):
            continue
        repo = run_project(get_run_metadata(d.name))
        if not repo:
            unassigned = True
        elif repo not in seen:
            seen.append(repo)
    projects = configured + sorted(r for r in seen if r not in configured)
    return {"projects": projects, "unassigned": unassigned}


def get_runs(sort="newest", project=""):
    """Get list of run directories sorted by run index or name.
    Returns objects: {name, date, branch, repo, commit, commit_message,
    is_running, status}
    project filters like project_matches().
    """
    idx_pat = re.compile(r"^run_(\d+)$")
    all_dirs = []
    for d in TEST_DIR.iterdir():
        # Skip the viewer's own folders (.uploads, .locks, .builds).
        if not d.is_dir() or d.name.startswith("."):
            continue
        name = d.name
        m = idx_pat.match(name)
        if m:
            run_idx = int(m.group(1))
            meta = get_run_metadata(name)
            date_str = (meta or {}).get("timestamp", name)
            branch = (meta or {}).get("branch") or ""
        else:
            run_idx = -1
            meta = None
            date_str = name
            branch = ""
        if not project_matches(meta, project):
            continue
        all_dirs.append((name, run_idx, date_str, branch, run_project(meta), run_commit(meta)))

    # Sort
    if sort == "newest":
        all_dirs.sort(key=lambda x: x[1], reverse=True)
    elif sort == "oldest":
        all_dirs.sort(key=lambda x: x[1])
    elif sort == "name_asc":
        all_dirs.sort(key=lambda x: x[0])
    elif sort == "name_desc":
        all_dirs.sort(key=lambda x: x[0], reverse=True)

    # Determine running status and final status per run
    def _get_status(name):
        """Return status badge text: Running, Fail, Pass, or Error."""
        # If currently running
        if _is_running(name):
            return "Running"
        # Check metadata for final result
        meta = get_run_metadata(name)
        if meta:
            if meta.get("status") == "interrupted":
                return "Error"
            failed = meta.get("failed", 0)
            passed = meta.get("passed", 0)
            total = meta.get("total", 0)
            if failed > 0:
                return "Fail"
            if meta.get("record_snapshots") and (passed > 0 or total > 0):
                return "Recording"
            if passed > 0 or total > 0:
                return "Pass"
        return "Error"

    def _get_counts(name):
        """Test-case results summed over every device in the run.

        Multi-device runs keep the aggregate at the run root, so the root
        metadata is enough for both single- and multi-device runs.
        """
        meta = get_run_metadata(name) or {}

        def _int(key):
            try:
                return max(int(meta.get(key) or 0), 0)
            except (TypeError, ValueError):
                return 0

        passed, failed = _int("passed"), _int("failed")
        return {"passed": passed, "failed": failed, "total": max(_int("total"), passed + failed)}

    return [{"name": name, "date": date_str, "branch": branch, "repo": repo, **commit,
             "is_running": _is_running(name), "status": _get_status(name), **_get_counts(name)}
            for name, ts, date_str, branch, repo, commit in all_dirs]


def delete_runs(names):
    """Delete the given run directories from TEST_DIR.

    Returns {deleted: [...], skipped: [{name, reason}, ...]}.
    Guards: only deletes directories that actually live inside TEST_DIR
    (rejects path traversal / non-existent names) and refuses to delete a
    run that is still in progress.
    """
    deleted, skipped = [], []
    for name in names:
        if not sec.valid_run_id(name):
            # Only run_N directories: never the viewer's own .uploads/.builds/.locks.
            skipped.append({"name": str(name)[:100], "reason": "invalid name"})
            continue
        run_dir = (TEST_DIR / name).resolve()
        # Path-traversal guard: the resolved dir must be a direct child of TEST_DIR.
        if run_dir.parent != TEST_DIR.resolve() or not run_dir.is_dir():
            skipped.append({"name": name, "reason": "not a valid run directory"})
            continue
        if _is_running(name):
            skipped.append({"name": name, "reason": "run is still in progress"})
            continue
        try:
            shutil.rmtree(run_dir)
            deleted.append(name)
        except Exception as e:
            skipped.append({"name": name, "reason": str(e)})
    return {"deleted": deleted, "skipped": skipped}


def get_dashboard_data(period="days", range_=None, project=""):
    """Get dashboard data: all runs, for a fixed date range.

    Args:
        period: bucket granularity — 'days' or 'months' (legacy: 'years' → months)
        range_: '1y' | '6m' | '3m' | '1m' | '1w' — how far back to show.
                Defaults to '1m'. 'days' → '1m', 'months' → '6m', 'years' → '1y'.

    Returns:
        {
            'all_runs':     { 'labels': [...], 'passed': [...], 'failed': [...], 'running': [...] },
        }
    """
    # Normalize range
    range_map = {
        '1y': (365, 'months'), '6m': (182, 'days'), '3m': (91, 'days'),
        '1m': (30, 'days'), '1w': (7, 'days'),
        'days': (30, 'days'), 'months': (182, 'days'), 'years': (365, 'months'),
    }
    key = (range_ if range_ in range_map else period if period in range_map else '1m')
    days_back, bucket = range_map[key]

    from datetime import datetime, timedelta
    now = datetime.now()
    cutoff = (now - timedelta(days=days_back)).date()

    idx_pat = re.compile(r"^run_(\d+)$")
    all_runs = []

    # Collect all runs with metadata
    for d in TEST_DIR.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        m = idx_pat.match(name)
        if not m:
            continue

        meta = get_run_metadata(name)
        if not meta or not project_matches(meta, project):
            continue

        date_str = meta.get('timestamp', '')  # "YYYY-MM-DD HH:MM:SS" from metadata
        try:
            run_date = datetime.strptime(date_str[:10], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            continue

        # Only include runs within the selected range (or still running)
        status = meta.get('status', 'unknown')
        if run_date < cutoff and status != 'running':
            continue

        all_runs.append({
            'name': name,
            'ts': int(m.group(1)),
            'status': status,
            'passed': meta.get('passed', 0),
            'failed': meta.get('failed', 0),
            'total': meta.get('total', 0),
            'is_running': status == 'running',
            'date': run_date,
        })

    # Sort by run index
    all_runs.sort(key=lambda x: x['ts'])

    # Bucket label + key for a run date
    if bucket == 'days':
        def bucket_of(run):
            d = run['date']
            return (d.strftime('%m-%d'), d)
    else:
        def bucket_of(run):
            d = run['date']
            return (d.strftime('%Y-%m'), d)

    def aggregate(runs):
        """Aggregate runs into consecutive day/month buckets spanning the
        full selected window (cutoff → today); days without runs stay at
        zero so the chart scale reflects the selected range."""
        labels = {}
        if bucket == 'days':
            d = cutoff
            while d <= now.date():
                labels[d.strftime('%m-%d')] = {'passed': 0, 'failed': 0, 'running': 0}
                d += timedelta(days=1)
        else:
            d = cutoff.replace(day=1)
            while d <= now.date():
                labels[d.strftime('%Y-%m')] = {'passed': 0, 'failed': 0, 'running': 0}
                d = (d + timedelta(days=32)).replace(day=1)
        for run in runs:
            key, _ = bucket_of(run)
            if key in labels:
                labels[key]['passed'] += run['passed']
                labels[key]['failed'] += run['failed']
                if run['is_running']:
                    labels[key]['running'] += 1
        return labels

    def to_chart(aggregated):
        return {
            'labels': list(aggregated.keys()),
            'passed': [aggregated[k]['passed'] for k in aggregated],
            'failed': [aggregated[k]['failed'] for k in aggregated],
            'running': [aggregated[k]['running'] for k in aggregated],
        }

    return {
        'all_runs': to_chart(aggregate(all_runs)),
    }


def get_device_run_dir(run, device_id=None):
    """Return a selected multi-device artifact directory, if valid.

    A device id of "legacy" (single-device runs) maps to the run root, where
    the artifacts live.
    """
    if not sec.valid_run_id(run):
        return None
    run_dir = TEST_DIR / run
    if not device_id or device_id == "legacy":
        return run_dir
    if not re.fullmatch(r"(?:ios_)?device_\d+", device_id):
        return None
    device_dir = run_dir / "devices" / device_id
    return device_dir if device_dir.is_dir() else None


def get_pending_payload(run, device_id=None):
    """The /api/pending body: one device's pending_tests.json, with the run
    status panel's snapshot mismatches added to its progress."""
    run_dir = get_device_run_dir(run, device_id)
    pending_file = run_dir / "pending_tests.json" if run_dir else None
    if not pending_file or not pending_file.exists():
        return {"tests": []}
    try:
        data = json.loads(pending_file.read_text())
        progress = data.get("progress")
        if isinstance(progress, dict):
            order = [t.get("name") for t in data.get("tests", [])
                     if isinstance(t, dict)]
            progress["snapshot_mismatches"] = get_snapshot_mismatches(run_dir, order)
        return data
    except Exception:
        return {"tests": [], "error": "Failed to parse pending data"}


RUN_EVENTS_CHECK_SECS = 0.5   # how often /api/run-events looks for changes
RUN_EVENTS_KEEPALIVE_SECS = 15


def run_state_signature(run):
    """A cheap fingerprint of everything test.html shows for a run, on every
    device: metadata, pending tests, result files and which videos exist.
    It changes when any of them is written. console.log and screenshots are
    left out: they change constantly and the page doesn't show them."""
    if not sec.valid_run_id(run):
        return ()
    run_dir = TEST_DIR / run
    dirs = [run_dir]
    devices_dir = run_dir / "devices"
    if devices_dir.is_dir():
        dirs += sorted(d for d in devices_dir.iterdir() if d.is_dir())
    sig = []
    for d in dirs:
        files = [d / "run_metadata.json", d / "pending_tests.json"]
        results = d / "results"
        if results.is_dir():
            files += sorted(results.glob("*.json"))
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            sig.append((str(f.relative_to(run_dir)), st.st_mtime_ns, st.st_size))
        outputs = d / "outputs"
        if outputs.is_dir():
            # Name only: a recording grows while it's written.
            sig += sorted(str(f.relative_to(run_dir)) for f in outputs.glob("*.mp4"))
    return tuple(sig)


def get_snapshot_mismatches(artifact_dir, test_order=None):
    """SNAPSHOT steps whose capture did not match the baseline, for the Run
    status panel: [{test, label, name, screen_class, diff_percent, tolerance,
    message}]. diff_percent is None when the sizes differ (no pixel compare).
    Ordered like test_order (test names), then by file name."""
    results_dir = artifact_dir / "results" if artifact_dir else None
    if not results_dir or not results_dir.is_dir():
        return []
    rank = {name: i for i, name in enumerate(test_order or [])}
    found = []
    for result_file in sorted(results_dir.glob("*_result.json")):
        try:
            result = json.loads(result_file.read_text())
        except (OSError, ValueError):
            continue
        steps = result.get("steps") if isinstance(result, dict) else None
        if not isinstance(steps, list):
            continue
        test = result.get("test") or result_file.name[:-len("_result.json")]
        for step in steps:
            snap = step.get("snapshot") if isinstance(step, dict) else None
            if not isinstance(snap, dict) or snap.get("status") != "mismatch":
                continue
            found.append({
                "test": test,
                "label": result.get("label") or test,
                "name": snap.get("name"),
                "screen_class": snap.get("screen_class"),
                "diff_percent": snap.get("diff_percent"),
                "tolerance": snap.get("tolerance"),
                "message": snap.get("message"),
            })
    found.sort(key=lambda m: rank.get(m["test"], len(rank)))
    return found


def get_device_summary(run):
    """Return per-device run summaries, including a legacy single-device view."""
    metadata = get_run_metadata(run)
    if not metadata:
        return {"devices": [], "active_device_id": None}
    devices = metadata.get("devices")
    if isinstance(devices, list):
        return {
            "devices": [dict(d, name=_friendly_device_name(d),
                             log_url=_setup_log_url(run, TEST_DIR / run, d.get("log")))
                        for d in devices],
            "active_device_id": metadata.get("active_device_id"),
        }
    device = metadata.get("device") or {}
    serial = device.get("model") or device.get("serial") or "Test device"
    return {
        "devices": [{
            "id": "legacy",
            "name": _friendly_device_name(device) or serial,
            "serial": serial,
            "status": metadata.get("status", "unknown"),
            "passed": metadata.get("passed", 0),
            "failed": metadata.get("failed", 0),
            "total": metadata.get("total", 0),
            "started": metadata.get("timestamp"),
            "completed": metadata.get("completed") or metadata.get("end_time"),
        }],
        "active_device_id": "legacy" if metadata.get("status") == "running" else None,
    }


def get_case_summary(run):
    """Per test-case (suite) summaries, summed over the run's devices.

    Tests are grouped by the runner's `case` field (or the `<case>__` name
    prefix on old runs). A run with a single case — including legacy flat
    tests — returns single=True so the run page can keep its current flat
    row layout. Each entry counts one execution per device the run is on:
    {case, passed, failed, unavailable, cancelled, running, pending,
    total, status}.
    """
    run_dir = TEST_DIR / run
    if not sec.valid_run_id(run) or not run_dir.is_dir():
        return {"cases": [], "single": True}
    devices_dir = run_dir / "devices"
    scan_dirs = [d for d in sorted(devices_dir.iterdir()) if d.is_dir()] \
        if devices_dir.is_dir() else []
    if not scan_dirs:
        scan_dirs = [run_dir]
    per_case = {}

    def _bucket(case):
        return per_case.setdefault(case, {"passed": 0, "failed": 0,
                                          "unavailable": 0, "cancelled": 0,
                                          "running": 0, "pending": 0})

    def _case_of(name, entry=None):
        case = (entry or {}).get("case") if isinstance(entry, dict) else None
        if isinstance(case, str) and case:
            return case
        return name.split("__", 1)[0] if "__" in name else ""

    for scan in scan_dirs:
        # One status per test in THIS device dir: result file wins over
        # progress state.
        statuses = {}
        for rf in sorted(scan.glob("results/*_result.json")):
            try:
                data = json.loads(rf.read_text())
            except Exception:
                continue
            name = data.get("test") or rf.name[:-len("_result.json")]
            statuses[name] = (name, data, (data.get("status") or "").upper())
        pending_file = scan / "pending_tests.json"
        if pending_file.exists():
            try:
                pending = json.loads(pending_file.read_text())
            except Exception:
                pending = {}
            for t in pending.get("tests", []):
                name = t.get("name") if isinstance(t, dict) else None
                if not name or name in statuses:
                    continue
                st = (t.get("status") or "pending").upper()
                if st in ("COMPLETED", "FAILED"):
                    # Progress claims it ended but the result file is not
                    # here — no reliable status, skip rather than guess.
                    continue
                statuses[name] = (name, t, st)
        for name, entry, st in statuses.values():
            bucket = _bucket(_case_of(name, entry))
            if st == "PASS":
                bucket["passed"] += 1
            elif st == "FAIL":
                bucket["failed"] += 1
            elif st == "UNAVAILABLE":
                bucket["unavailable"] += 1
            elif st == "CANCELLED":
                bucket["cancelled"] += 1
            elif st == "RUNNING":
                bucket["running"] += 1
            else:
                bucket["pending"] += 1

    # Cases the run is queued for but has not reached yet still get a row
    # (from run_metadata.json's cases list).
    meta = get_run_metadata(run) or {}
    queued = [c for c in (meta.get("cases") or []) if isinstance(c, str) and c]
    order = list(queued)
    for case in sorted(per_case):
        if case not in order:
            order.append(case)

    cases = []
    for case in order:
        b = per_case.get(case, {"passed": 0, "failed": 0, "unavailable": 0,
                                "cancelled": 0, "running": 0, "pending": 0})
        total = (b["passed"] + b["failed"] + b["unavailable"] +
                 b["cancelled"] + b["running"] + b["pending"])
        done = total - b["running"] - b["pending"]
        if b["running"] or (b["pending"] and done):
            status = "running"
        elif b["pending"] or total == 0:
            status = "pending"
        elif b["failed"] or b["unavailable"] or b["cancelled"]:
            status = "fail"
        elif b["passed"] == total:
            status = "pass"
        else:
            status = "unknown"
        cases.append({"case": case, "status": status, **b, "total": total})
    return {"cases": cases, "single": len(cases) <= 1}


def get_run_files(run, device_id=None):
    """Get all files in a run directory.

    At the run root (no device_id) the devices/ subdirectory IS included so
    the run detail page can discover which tests ran (rows are grouped by
    result-file basename). get_test_data() separately refuses to resolve a
    test result from a device subdir for a root query.
    """
    run_dir = TEST_DIR / run
    scan_dir = get_device_run_dir(run, device_id)
    if not run_dir.is_dir() or not scan_dir:
        return []
    files = []
    for f in scan_dir.rglob("*"):
        if f.is_file():
            ftype = detect_file_type(f)
            rel = f.relative_to(run_dir)
            files.append({
                "name": str(rel),
                "type": ftype,
                "size": f.stat().st_size,
            })
    return files


def get_run_metadata(run, device_id=None):
    """Get run metadata from run_metadata.json."""
    run_dir = get_device_run_dir(run, device_id)
    if not run_dir:
        return None
    meta_file = run_dir / "run_metadata.json"
    if meta_file.exists():
        with open(meta_file) as f:
            return json.load(f)
    return None


def get_run_stream_device(run, device_id=None):
    """Return how the run page should show a run's device live.

    Android streams MJPEG; iOS has no stream, so the page polls one screenshot
    at a time.
    """
    metadata = get_run_metadata(run, device_id) or {}
    device = metadata.get("device") or {}
    serial = str(device.get("serial") or metadata.get("adb_host") or "")
    serial = serial[len("ios:"):] if serial.startswith("ios:") else serial
    live = next((d for d in cached_controller_devices() if d.get("id") == serial), None)
    if not live:
        return {"status": "unavailable",
                "reason": f"{serial or 'This device'} is not connected to the "
                          "Mobile Controller."}
    name = device_display_name(live)
    if live.get("platform") == "ios":
        return {"status": "ios", "serial": serial, "label": f"iOS · {name}",
                "reason": "iOS live view polls device screenshots."}
    return {"status": "mapped", "device": {"path": serial, "name": name}}


def get_run_summary(run, device_id=None):
    """Get run summary text."""
    run_dir = get_device_run_dir(run, device_id)
    if not run_dir:
        return ""
    for name in ("run_metadata.json", "summary.txt"):
        sf = run_dir / name
        if sf.exists():
            return sf.read_text()
    return ""


def _add_snapshot_urls(steps, run, run_dir, artifact_dir):
    """SNAPSHOT step paths are relative to the device dir; add /static/ URLs."""
    for step in steps:
        snap = step.get("snapshot") if isinstance(step, dict) else None
        if not isinstance(snap, dict):
            continue
        for key in ("actual", "baseline", "diff"):
            rel = snap.get(key)
            if not rel:
                continue
            path = (artifact_dir / rel).resolve()
            try:
                snap[key + "_url"] = "/static/" + run + "/" + quote(str(path.relative_to(run_dir.resolve())))
            except ValueError:
                pass


def _iter_result_files(run_dir):
    yield from sorted(run_dir.glob("results/*_result.json"))
    yield from sorted(run_dir.glob("devices/*/results/*_result.json"))


def build_snapshot_bundle(run):
    """Zip of the candidates in a run (missing, mismatched or recorded
    snapshots), laid out as mt-tests/<case>/snapshots/... so it unpacks
    straight into the app repo. Returns (bytes, count), or (None, 0)."""
    run_dir = TEST_DIR / run
    meta = get_run_metadata(run) or {}
    case = meta.get("case") if CASE_NAME_RE.match(str(meta.get("case") or "")) else "CASE"
    entries = {}
    for result_path in _iter_result_files(run_dir):
        try:
            result = json.loads(result_path.read_text())
        except (OSError, ValueError):
            continue
        device_dir = result_path.parent.parent
        # Multi-case runs record each test's case in its result.
        result_case = result.get("case") if CASE_NAME_RE.match(str(result.get("case") or "")) else case
        for step in result.get("steps") or []:
            snap = step.get("snapshot") if isinstance(step, dict) else None
            if not isinstance(snap, dict) or snap.get("status") not in ("missing", "mismatch", "recorded"):
                continue
            rel = f"snapshots/{snap.get('baseline_path', '')}"
            actual = (device_dir / str(snap.get("actual") or "")).resolve()
            if not SNAPSHOT_PATH_RE.match(rel) or not actual.is_file():
                continue
            try:
                actual.relative_to(run_dir.resolve())
            except ValueError:
                continue
            entries[f"mt-tests/{result_case}/{rel}"] = actual
    if not entries:
        return None, 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in sorted(entries.items()):
            zf.write(path, name)
    return buf.getvalue(), len(entries)


TEST_SOURCE_MAX_BYTES = 256 * 1024


def get_test_source(run_dir, test_name, case=None):
    """The .txt files a test ran from, as the runner saved them under
    <run>/tests/[<case>/]: {test_file, test, instruction}. ``instruction`` is
    the case's INSTRUCTION.txt or None. Returns None when the run has no
    saved copy (runs from before the runner saved them)."""
    root = run_dir / "tests"
    stem = test_name
    if case:
        if not CASE_NAME_RE.match(str(case)) or not test_name.startswith(f"{case}__"):
            return None
        root = root / case
        stem = test_name[len(case) + 2:]
    test_path = root / f"{stem}.txt"
    try:
        test_path.resolve().relative_to((run_dir / "tests").resolve())
    except ValueError:
        return None
    if not test_path.is_file() or test_path.name == "INSTRUCTION.txt":
        return None

    def read(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(TEST_SOURCE_MAX_BYTES)

    instruction = root / "INSTRUCTION.txt"
    try:
        return {
            "test_file": test_path.name,
            "test": read(test_path),
            "instruction": read(instruction) if instruction.is_file() else None,
        }
    except OSError:
        return None


def get_test_data(run, test_name, device_id=None):
    """Get parsed test data: status, steps, screenshots, video."""
    if not sec.valid_test_name(test_name):
        return {"status": "", "steps": [], "screenshots": [], "video": None,
                "result_file": None}
    files = get_run_files(run, device_id)
    run_dir = TEST_DIR / run
    artifact_dir = get_device_run_dir(run, device_id)

    # Find the result JSON
    result_file = None
    video_file = None
    screenshots = []

    for f in files:
         basename = f['name'].split("/").pop()
         # A root query (no device) must not resolve artifacts that belong to
         # a multi-device run's devices/<id> subdirectory — those are only
         # reachable via their per-device queries.
         if device_id is None and f["name"].startswith("devices/"):
             continue
         if basename == f"{test_name}_result.json":
             result_file = f
         elif basename == f"{test_name}.mp4":
             video_file = f
         elif f['type'] == "image" and basename.startswith(test_name):
             screenshots.append(f)

    # Also read screenshots from result JSON (primary source, set by runner)
    if result_file:
        result_path = run_dir / result_file["name"]
        try:
            with open(result_path) as f:
                result_json = json.load(f)
            if 'screenshots' in result_json and result_json['screenshots']:
                # Build file entries from the result JSON's screenshot list
                screenshots = []
                for ss_name in result_json['screenshots']:
                    candidates = (
                        artifact_dir / ss_name,
                        artifact_dir / "outputs" / ss_name,
                    )
                    ss_path = next((path for path in candidates if path.exists()), None)
                    if ss_path:
                        ftype = detect_file_type(ss_path)
                        screenshots.append({
                            'name': str(ss_path.relative_to(run_dir)),
                            'type': ftype,
                            'size': ss_path.stat().st_size,
                        })
        except Exception:
            pass

    data = {
        "status": "",
        "steps": [],
        "screenshots": screenshots,
        "video": video_file,
        "result_file": result_file,
    }

    if result_file:
        result_path = run_dir / result_file["name"]
        try:
            with open(result_path) as f:
                result_json = json.load(f)
            data["status"] = result_json.get("status", "")
            data["steps"] = result_json.get("steps", [])
            if result_json.get("error"):
                data["error"] = result_json["error"]
            data["log_url"] = _setup_log_url(run, run_dir, result_json.get("log"), device_id)
            _add_snapshot_urls(data["steps"], run, run_dir, artifact_dir)
            data["test_source"] = get_test_source(run_dir, test_name, result_json.get("case"))

            # Prefer the video path recorded by the runner (per-test capture file).
            # Fall back to the "<test_name>.mp4" guess for older runs without this field.
            video_rel = result_json.get("video")
            if video_rel:
                video_path = artifact_dir / video_rel
                if video_path.exists():
                    data["video"] = {
                        "name": str(video_path.relative_to(run_dir)),
                        "type": detect_file_type(video_path),
                        "size": video_path.stat().st_size,
                    }
        except Exception:
            pass

    return data


def configured_device_name(serial):
    """Name set for a device id on the Device Config section of /plugins, or ''.

    Run metadata records iOS serials as "ios:<udid>"; the config is keyed by
    the bare controller id, so the prefix is stripped before the lookup.
    """
    serial = str(serial or "").strip()
    if serial.lower().startswith("ios:"):
        serial = serial[4:]
    if not serial:
        return ""
    return (load_device_names().get(serial) or "").strip()


def _friendly_device_name(entry):
    """Resolve a display name for one device metadata entry.

    A name set on the Device Config section of /plugins for the device's serial wins, so a
    rename shows up on every run, old ones included. Otherwise use the name
    the runner recorded (ro.product.model, e.g. "SM-A155F"; iOS entries carry
    one too), then the serial.
    """
    configured = configured_device_name(entry.get("serial"))
    if configured:
        return configured
    name = (entry.get("name") or "").strip()
    if name:
        return name
    serial = (entry.get("serial") or "").strip()
    if not serial:
        return entry.get("id", "")
    if serial.lower().startswith("ios:"):
        return "iOS " + serial[4:8]
    return serial


def _scan_test_artifacts(artifact_dir, run_dir, test_name):
    """Read one test's result JSON + pending status from an artifact dir.

    Returns (test_status, pending_status, video_rel) where video_rel is
    relative to the RUN dir (for /static/ URLs) or None.
    """
    test_status = None
    pending_status = None
    video_rel = None

    pending_file = artifact_dir / "pending_tests.json"
    if pending_file.exists():
        try:
            pending = json.loads(pending_file.read_text())
            for t in pending.get("tests", []):
                if t.get("name") == test_name:
                    pending_status = t.get("status")
                    break
        except Exception:
            pass

    result_file = artifact_dir / "results" / f"{test_name}_result.json"
    if result_file.exists():
        try:
            result_json = json.loads(result_file.read_text())
            test_status = result_json.get("status")
            video = result_json.get("video")
            if video:
                video_path = artifact_dir / video
                if video_path.exists():
                    video_rel = str(video_path.relative_to(run_dir))
        except Exception:
            pass

    return test_status, pending_status, video_rel


def get_test_device_videos(run, test_name):
    """Per-device status + video for one test case.

    Multi-device runs: one entry per device in the aggregate metadata, each
    pointing at its own artifacts under devices/<id>/. Single-device (legacy)
    runs: one "legacy" entry for the run root. Used by the run detail page to
    list, on the right of each test row, the devices the test ran on and to
    swap the video when a device is selected.
    """
    run_dir = TEST_DIR / run
    if not sec.valid_run_id(run) or not sec.valid_test_name(test_name) or not run_dir.is_dir():
        return {"test": test_name, "devices": []}

    metadata = get_run_metadata(run) or {}
    devices = []
    entries = metadata.get("devices")

    if isinstance(entries, list):
        for entry in entries:
            device_id = entry.get("id") or ""
            device_dir = get_device_run_dir(run, device_id) if device_id else None
            test_status, pending_status, video_rel = (None, None, None)
            if device_dir is not None and device_dir.is_dir():
                test_status, pending_status, video_rel = _scan_test_artifacts(
                    device_dir, run_dir, test_name)
            devices.append({
                "id": device_id,
                "name": _friendly_device_name(entry),
                "platform": entry.get("platform", "android"),
                "serial": entry.get("serial", ""),
                "device_status": entry.get("status", "unknown"),
                "test_status": test_status,
                "pending_status": pending_status,
                "video": video_rel,
                "video_url": ("/static/" + run + "/" + video_rel) if video_rel else None,
            })
    else:
        # Legacy single-device run — artifacts live at the run root.
        device = metadata.get("device") or {}
        test_status, pending_status, video_rel = _scan_test_artifacts(
            run_dir, run_dir, test_name)
        legacy_entry = {"id": "legacy",
                        "name": device.get("name") or device.get("model") or "",
                        "serial": device.get("serial", "")}
        devices.append({
            "id": "legacy",
            "name": _friendly_device_name(legacy_entry) or "Test device",
            "platform": "android",
            "serial": device.get("serial", ""),
            "device_status": metadata.get("status", "unknown"),
            "test_status": test_status,
            "pending_status": pending_status,
            "video": video_rel,
            "video_url": ("/static/" + run + "/" + video_rel) if video_rel else None,
        })

    return {"test": test_name, "devices": devices}


def parse_summary_stats(summary):
    """Extract pass/fail/total counts from summary text."""
    passed = failed = total = 0
    for line in summary.split("\n"):
        line = line.strip()
        if "Passed:" in line:
            try:
                passed = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif "Failed:" in line:
            try:
                failed = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif "Total:" in line:
            try:
                total = int(line.split(":")[-1].strip())
            except ValueError:
                pass
    return passed, failed, total


# ─── Login, sessions and tokens (viewer_auth.py holds the stores) ───
# Login is always required. Open without a session: the login page and the
# fonts it uses, /health, /api/auth/status, POST /api/login, /api/logout,
# /api/theme, and GET /ci/submit.py (the upload script build plugins fetch;
# it is this repo's own source). The CI endpoints also take the CI token, and
# the runner posts press events with its own per-start token.
PUBLIC_GET = {"/health", "/login", "/login.html", "/api/auth/status", "/api/theme", "/ci/submit.py"}
PUBLIC_POST = {"/api/login", "/api/logout", "/api/theme"}
# Not pages: an unauthenticated request gets 401 JSON instead of a redirect.
API_PREFIXES = ("/api/", "/static/", "/stream/", "/convert-video/")


def _session_cookie(token):
    return (f"{auth.COOKIE_NAME}={token}; Path=/; Max-Age={auth.COOKIE_AGE}; "
            "HttpOnly; SameSite=Strict")


def _is_result_route(path):
    return path.startswith("/api/runs/") and path.endswith("/result")


def build_upload_allowed(build):
    """True while a build the viewer started still waits for its upload: not
    finished and no run attached yet. Its id then works once as the upload's
    credential (the build plugins upload with it and no CI token)."""
    return isinstance(build, dict) and build.get("state") not in builds.TERMINAL_STATES \
        and not build.get("runs")


def import_service_tokens(env=None):
    """One-time: CONTROLLER_TOKEN / IOS_CONTROLLER_TOKEN, PLUGIN_TOKEN /
    MT_PLUGIN_TOKEN_<NAME>, MT_CI_TOKEN and any build plugin token still kept
    in config.json move into service_tokens.json (Settings owns them)."""
    with _CONFIG_LOCK:
        config = load_config()
        plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
        auth.import_env_tokens(plugins, env)
        # Build plugin tokens never stay in config.json.
        moved = False
        for name, entry in plugins.items():
            if isinstance(entry, dict) and entry.get("token"):
                if not auth.plugin_token(name):
                    auth.set_service_token(f"plugin:{name}", str(entry["token"]))
                entry["token"] = ""
                moved = True
        if moved:
            save_config(config)


@sec.harden_handler
class TestViewerHandler(SimpleHTTPRequestHandler):

    # ─── Auth gate ───
    auth_user = None    # {"username", "role", "default_password"} of the session
    auth_via = None     # "session", "ci_token", "runner", "build_request", None

    def _session_token(self):
        """The mt_auth cookie's value, or ""."""
        for cookie in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = cookie.strip().partition("=")
            if name == auth.COOKIE_NAME:
                return value
        return ""

    def _origin_ok(self, required):
        """CSRF check: the Origin (else Referer) host must equal Host. With
        neither header, the answer is `not required`."""
        host = (self.headers.get("Host") or "").strip().lower()
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if value is not None:
                return bool(host) and urlparse(value.strip()).netloc.lower() == host
        return not required

    def _authorize(self, method):
        """Decide whether this request may go on; if not, answer it (302 to
        /login for pages, 401 for APIs, 403 for a cross-site write) and
        return False. Sets self.auth_user / self.auth_via."""
        path = urlparse(self.path).path.rstrip("/") or "/"
        self.auth_user = auth.session_user(self._session_token())
        self.auth_via = "session" if self.auth_user else None
        writes = method not in ("GET", "HEAD", "OPTIONS")
        public = (path in PUBLIC_POST) if writes else \
            (path in PUBLIC_GET or path == "/fonts" or path.startswith("/fonts/"))
        if public:
            if writes and not self._origin_ok(required=False):
                self.send_json_status(403, {"error": "cross-site request refused"})
                return False
            return True
        if method == "POST" and path == "/api/press-event" and self.headers.get(auth.RUNNER_HEADER):
            if auth.runner_token_valid(self.headers.get(auth.RUNNER_HEADER)):
                self.auth_via = "runner"
                return True
            self.send_json_status(401, {"error": "invalid runner token"})
            return False
        ci_route = (method == "GET" and _is_result_route(path)) or \
            (method == "POST" and path == "/api/run-test")
        if ci_route and self.headers.get("Authorization"):
            if auth.ci_token_valid(self.headers.get("Authorization")):
                self.auth_via = "ci_token"
                return True
            self.send_json_status(401, {"error": "invalid CI token"})
            return False
        if self.auth_user:
            if writes and not self._origin_ok(required=True):
                self.send_json_status(403, {"error": "cross-site request refused "
                                                     "(Origin/Referer must match the Host)"})
                return False
            return True
        if method == "POST" and path == "/api/run-test":
            # handle_run_test accepts it only for a build still waiting for
            # its upload (see build_upload_allowed).
            self.auth_via = "build_request"
            return True
        if method in ("GET", "HEAD") and not path.startswith(API_PREFIXES) and path != "/devices":
            target = self.path if path != "/" else ""
            self.send_response(302)
            self.send_header("Location", "/login" + ("?next=" + quote(target, safe="") if target else ""))
            self.end_headers()
            return False
        self.send_json_status(401, {"error": "Authentication required", "login": "/login"})
        return False

    def _admin_required(self):
        if (self.auth_user or {}).get("role") == "admin":
            return True
        self.send_json_status(403, {"error": "Only an admin can do this"})
        return False

    def _send_json_with_cookie(self, cookie_value, data, status=200):
        """JSON plus a Set-Cookie header (written before the body)."""
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Set-Cookie", cookie_value)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_text_status(self, status, text):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_login(self):
        """POST /api/login {username, password} -> session cookie. Per-IP
        throttle: after 5 failures each further one doubles the wait (429)."""
        ip = self.client_address[0]
        wait = auth.THROTTLE.wait_left(ip)
        if wait > 0:
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(int(wait) + 1))
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Too many failed sign-ins; try again in "
                                                  f"{int(wait) + 1} s"}).encode())
            return
        data = self._read_json_body()
        if data is None:
            return
        token = auth.login(str(data.get("username") or ""), str(data.get("password") or ""))
        if not token:
            auth.THROTTLE.failed(ip)
            self.send_json_status(401, {"error": "Invalid username or password"})
            return
        auth.THROTTLE.succeeded(ip)
        self._send_json_with_cookie(_session_cookie(token), {"success": True})

    def handle_logout(self):
        """End this browser's session (others stay signed in) and clear its cookie."""
        auth.logout(self._session_token())
        self._send_json_with_cookie(
            f"{auth.COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict", {"success": True})

    def handle_change_password(self):
        """POST /api/account/password {current_password, new_password}: any
        signed-in user, own account. Ends that user's other sessions; the
        browser that saved gets a fresh cookie."""
        data = self._read_json_body()
        if data is None:
            return
        try:
            token = auth.change_password(self.auth_user["username"],
                                         str(data.get("current_password") or ""),
                                         data.get("new_password"))
        except auth.AuthError as e:
            self.send_json_status(e.status, {"error": e.message})
            return
        self._send_json_with_cookie(_session_cookie(token), {"success": True})

    def handle_users(self):
        """POST /api/users (admin), ONE action per request:
        {"create": {username, password, role?}}, {"reset_password": {username,
        password}}, {"delete": username}, {"set_role": {username, role}}."""
        data = self._read_json_body()
        if data is None:
            return
        if len(data) != 1:
            self.send_json_status(400, {"error": "send exactly one of create, reset_password, "
                                                 "delete, set_role"})
            return
        (action, arg), = data.items()
        try:
            if action == "create" and isinstance(arg, dict):
                auth.create_user(arg.get("username"), arg.get("password"), arg.get("role") or "user")
            elif action == "reset_password" and isinstance(arg, dict):
                auth.reset_password(str(arg.get("username") or ""), arg.get("password"))
            elif action == "delete" and isinstance(arg, str):
                auth.delete_user(arg, self.auth_user["username"])
            elif action == "set_role" and isinstance(arg, dict):
                auth.set_role(str(arg.get("username") or ""), arg.get("role"))
            else:
                self.send_json_status(400, {"error": f"unknown or malformed action {action!r}"})
                return
        except auth.AuthError as e:
            self.send_json_status(e.status, {"error": e.message})
            return
        self.send_json({"ok": True, "users": auth.list_users()})

    def handle_ci_token(self):
        """POST /api/config/ci-token (admin): {"generate": true} -> the new
        token, shown this once; {"remove": true}."""
        data = self._read_json_body()
        if data is None:
            return
        if data.get("generate") is True:
            token = auth.generate_ci_token()
            self.send_json({"token": token, **auth.ci_token_info()})
        elif data.get("remove") is True:
            auth.remove_ci_token()
            self.send_json(auth.ci_token_info())
        else:
            self.send_json_status(400, {"error": 'send {"generate": true} or {"remove": true}'})

    def service_tokens_view(self):
        """GET /api/config/service-tokens: {configured, last4, saved_at} per
        service — never a token."""
        cfg = load_config()
        plugins = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
        data = auth.load_tokens()
        return {
            "controller": {"url": _controller_url(), **auth.token_info(data.get("controller"))},
            "plugins": [{"name": n, "kind": "build" if isinstance(e, dict) else "device",
                         "url": e.get("url") if isinstance(e, dict) else e,
                         **auth.token_info(data["plugins"].get(n))}
                        for n, e in sorted(plugins.items())],
        }

    def handle_service_tokens(self):
        """POST /api/config/service-tokens (admin):
        {"set": {"service": "controller"|"plugin:<name>", "token"}} — checked
        against the service's authenticated endpoint first (Mobile-Controller
        GET /devices with X-Controller-Token; a device plugin GET /devices, a
        build plugin GET /config with X-Plugin-Token); nothing is stored if
        the check fails. {"remove": "controller"|"plugin:<name>"}."""
        data = self._read_json_body()
        if data is None:
            return
        if set(data) == {"remove"} and isinstance(data["remove"], str):
            service = data["remove"]
            if not self._known_service(service):
                return
            auth.set_service_token(service, "")
            self.send_json(self.service_tokens_view())
            return
        op = data.get("set")
        if set(data) != {"set"} or not isinstance(op, dict) or not isinstance(op.get("service"), str) \
                or not isinstance(op.get("token"), str) or not op["token"].strip():
            self.send_json_status(400, {"error": 'send {"set": {"service", "token"}} or {"remove": service}'})
            return
        service, token = op["service"], op["token"].strip()
        entry = self._known_service(service)
        if not entry:
            return
        error = self._check_service_token(service, entry, token)
        if error:
            self.send_json_status(error[0], {"error": error[1]})
            return
        auth.set_service_token(service, token)
        self.send_json(self.service_tokens_view())

    def _known_service(self, service):
        """The registry entry for a service name (True for the controller),
        or answer 400 and return None."""
        if service == "controller":
            return True
        if service.startswith("plugin:"):
            plugins = load_config().get("plugins")
            entry = plugins.get(service[len("plugin:"):]) if isinstance(plugins, dict) else None
            if entry:
                return entry
        self.send_json_status(400, {"error": f"unknown service {service!r}"})
        return None

    def _check_service_token(self, service, entry, token):
        """None when `token` opens the service's authenticated endpoint, else
        (status, message)."""
        if service == "controller":
            base = _controller_url()
            if not base:
                return 400, "CONTROLLER_URL is not set, so the token can't be checked"
            status, _ = _get_json(f"{base}/devices", headers={"X-Controller-Token": token}, timeout=5)
            if status == 200:
                return None
            if status in (401, 403):
                return 400, "the Mobile-Controller rejected the token (GET /devices: %d)" % status
            return 502, f"the Mobile-Controller did not answer GET /devices (status {status})"
        name = service[len("plugin:"):]
        if isinstance(entry, dict):
            bpc = builds.bpc
            try:
                bpc.validate(name, entry.get("url") or "", token=token, timeout=builds.HEALTH_TIMEOUT + 2)
            except bpc.BuildPluginError as e:
                return (400 if e.status in (401, 403) or e.code == "unauthorized" else 502), e.message
            return None
        pc = _plugin_client()
        try:
            pc.PluginClient(name, entry, token=token, timeout=5).devices(timeout=5)
        except pc.PluginError as e:
            if e.status in (401, 403):
                return 400, f"plugin {name} rejected the token (GET /devices: {e.status})"
            return 502, e.message
        return None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        if has_path_traversal(parsed.path):
            self.send_json_status(400, {"error": "invalid path"})
            return
        if not self._authorize("GET"):
            return

        # For readiness checks: open to anyone, so it says nothing but that it's up.
        if path == "/health":
            self.send_json({"ok": True, "service": SERVICE_NAME, "api_version": API_VERSION})
            return
        if path == "/api/status":
            self.send_json(self.get_system_status())
            return

        # Serve HTML pages
        if path == "/" or path == "":
            self.serve_page("index.html")
        elif path == "/dashboard" or path == "/dashboard.html":
            self.serve_page("index.html")
        elif path == "/test.html":
            self.serve_page("test.html")
        elif path == "/test-b.html":
            self.serve_page("test.html")
        elif path == "/logs.html":
            self.serve_page("logs.html")
        elif path == "/player.html" or path == "/player":
            self.serve_page("player.html")
        elif path == "/device-config" or path == "/device-config.html":
            # Retired page: the Device Config options now live in their own
            # section on the Plugins page. Old bookmarks redirect, not 404.
            self.send_response(302)
            self.send_header("Location", "/plugins")
            self.end_headers()
        elif path == "/configuration.html" or path == "/configuration":
            # Old Device Config URL; the section now lives on /plugins.
            self.send_response(302)
            self.send_header("Location", "/plugins")
            self.end_headers()
        elif path == "/config" or path == "/config.html":
            # Configuration: Build & Test + Test Variables. The old single Settings
            # page was split into one page per settings section.
            self.serve_page("config.html")
        elif path == "/appearance" or path == "/appearance.html":
            # Retired: the Light Mode toggle now lives on the Configuration page.
            self.send_response(302)
            self.send_header("Location", "/config")
            self.end_headers()
        elif path == "/plugins" or path == "/plugins.html":
            self.serve_page("plugins.html")
        elif path == "/models" or path == "/models.html":
            # The model picker now lives inside the Device Plugins panel:
            # clicking the AI plugin row configures it there. Old bookmarks
            # redirect instead of 404.
            self.send_response(302)
            self.send_header("Location", "/plugins")
            self.end_headers()
        elif path == "/copilot" or path == "/copilot.html":
            # GitHub Copilot is configured on the AI plugin now (its Configure
            # panel inside Device Plugins). Old bookmarks redirect.
            self.send_response(302)
            self.send_header("Location", "/plugins")
            self.end_headers()
        elif path == "/run-status" or path == "/run-status.html":
            self.serve_page("run-status.html")
        elif path == "/settings.html" or path == "/settings":
            # Retired: the settings sections are one page each (see NAV_SETTINGS_LINKS).
            self.send_response(302)
            self.send_header("Location", "/config")
            self.end_headers()
        elif path == "/build.html" or path == "/build":
            # Builds start through the project's build plugin.
            self.serve_page("build.html")
        elif path == "/login" or path == "/login.html":
            # Public. Already signed in: straight on to the app.
            if self.auth_user:
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
            else:
                self.serve_page("login.html")
        elif path == "/users" or path == "/users.html":
            # Settings → Users: admins only.
            if self.auth_user.get("role") != "admin":
                self.send_text_status(403, "Only an admin can manage users.\n")
            else:
                self.serve_page("users.html")
        elif path == "/api/users":
            if self._admin_required():
                self.send_json({"users": auth.list_users()})
        elif path == "/api/config/ci-token":
            if self._admin_required():
                self.send_json(auth.ci_token_info())
        elif path == "/api/config/service-tokens":
            if self._admin_required():
                self.send_json(self.service_tokens_view())
        elif path == "/ci/submit.py":
            # The upload script build plugins run (curl ... && python3 ... --no-wait).
            self.send_file_bytes(SUBMIT_SCRIPT.read_bytes(), "text/x-python; charset=utf-8")

        # API endpoints
        elif path.startswith("/api/runs/") and path.endswith("/result"):
            run = path[len("/api/runs/"):-len("/result")]
            if not RUN_ID_PATTERN.match(run) or not (TEST_DIR / run).is_dir():
                self.send_json_status(404, {"error": "run not found"})
                return
            host = self.headers.get("Host", "")
            self.send_json({"run": run, "url": f"http://{host}/test.html?run={run[len('run_'):]}",
                            **get_run_result(run)})
        elif path.startswith("/api/runs/") and path.endswith("/snapshots.zip"):
            run = path[len("/api/runs/"):-len("/snapshots.zip")]
            if not RUN_ID_PATTERN.match(run) or not (TEST_DIR / run).is_dir():
                self.send_json_status(404, {"error": "run not found"})
                return
            body, count = build_snapshot_bundle(run)
            if params.get("count"):
                self.send_json({"count": count})
                return
            if not body:
                self.send_json_status(404, {"error": "this run has no new or changed snapshots"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{run}-snapshots.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Snapshot-Count", str(count))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/runs":
            sort = params.get("sort", ["newest"])[0]
            project = params.get("project", [""])[0]
            self.send_json(get_runs(sort, project))
        elif path == "/api/projects":
            self.send_json(list_projects())
        elif path.startswith("/api/run/"):
            run = path[len("/api/run/"):]
            device_id = params.get("device", [None])[0]
            self.send_json(get_run_files(run, device_id))
        elif path.startswith("/api/test/"):
            parts = path[len("/api/test/"):].split("/", 1)
            if len(parts) == 2 and sec.valid_run_id(parts[0]) and sec.valid_test_name(parts[1]):
                run, test = parts
                device_id = params.get("device", [None])[0]
                self.send_json(get_test_data(run, test, device_id))
            else:
                self.send_json_status(404, {"error": "unknown run or test"})
        elif path.startswith("/api/metadata/"):
            run = path[len("/api/metadata/"):]
            device_id = params.get("device", [None])[0]
            metadata = get_run_metadata(run, device_id)
            if isinstance(metadata, dict) and isinstance(metadata.get("device"), dict):
                device = metadata["device"]
                device["display_name"] = _friendly_device_name(device)
            self.send_json(metadata)
        elif path.startswith("/api/device-summary/"):
            run = path[len("/api/device-summary/"):]
            self.send_json(get_device_summary(run))
        elif path.startswith("/api/cases/"):
            run = path[len("/api/cases/"):]
            self.send_json(get_case_summary(run))
        elif path.startswith("/api/test-devices/"):
            parts = path[len("/api/test-devices/"):].split("/", 1)
            if len(parts) == 2 and sec.valid_run_id(parts[0]) and sec.valid_test_name(parts[1]):
                run, test = parts
                self.send_json(get_test_device_videos(run, test))
            else:
                self.send_json_status(404, {"error": "unknown run or test"})
        elif path.startswith("/api/stream-device/"):
            run = path[len("/api/stream-device/"):]
            device_id = params.get("device", [None])[0]
            self.send_json(get_run_stream_device(run, device_id))
        elif path.startswith("/api/summary/"):
            run = path[len("/api/summary/"):]
            device_id = params.get("device", [None])[0]
            summary = get_run_summary(run, device_id)
            if summary:
                self.send_text(summary)
            else:
                self.send_json({})
        elif path.startswith("/api/stats/"):
            run = path[len("/api/stats/"):]
            device_id = params.get("device", [None])[0]
            metadata = get_run_metadata(run, device_id) or {}
            if metadata:
                passed = metadata.get("passed", 0)
                failed = metadata.get("failed", 0)
                total = metadata.get("total", 0)
            else:
                summary = get_run_summary(run, device_id)
                passed, failed, total = parse_summary_stats(summary)
            self.send_json({"passed": passed, "failed": failed, "total": total})
        elif path.startswith("/api/press-events/"):
            run = path[len("/api/press-events/"):]
            if not RUN_ID_PATTERN.match(run):
                self.send_json_status(400, {"error": "invalid run name"})
                return
            with _PRESS_LOCK:
                _press_prune_locked()
                events = list(_PRESS_EVENTS.get(run, []))
            self.send_json({"run": run, "events": events})
        elif path.startswith("/api/frame/"):
            # /api/frame/<id> — one fresh screenshot from the controller.
            device_id = unquote(path[len("/api/frame/"):])
            if not DEVICE_ID_RE.fullmatch(device_id):
                self.send_json_status(400, {"error": "invalid device id"})
                return
            png, err = fetch_device_frame(device_id)
            if png is None:
                self.send_json_status(502, {"error": err or "screenshot failed"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)
        elif path.startswith("/api/pending/"):
            run = path[len("/api/pending/"):]
            device_id = params.get("device", [None])[0]
            self.send_json(get_pending_payload(run, device_id))
        elif path.startswith("/api/run-events/"):
            run = path[len("/api/run-events/"):]
            if not RUN_ID_PATTERN.match(run) or not (TEST_DIR / run).is_dir():
                self.send_json_status(404, {"error": "unknown run"})
                return
            self.stream_run_events(run, params.get("device", [None])[0])
        elif path.startswith("/api/logs/"):
            run = path[len("/api/logs/"):]
            device_id = params.get("device", [None])[0]
            run_dir = get_device_run_dir(run, device_id)
            log_file = run_dir / "console.log" if run_dir else None
            if log_file and log_file.exists():
                self.send_text(log_file.read_text())
            else:
                self.send_json({"error": "No console log found for this run"})

        # Device name configuration
        elif path == "/api/config/devices":
            self.send_json(self.get_device_names())

        # Controller Plugin: the Mobile Controller's registration
        elif path == "/api/config/controller":
            self.send_json(controller_plugin_state())

        # Static files (run outputs)
        elif path.startswith("/static/"):
            self.serve_run_file(path)

        # Self-hosted web fonts for the shared theme (fonts/*.woff2).
        # do_GET strips the trailing slash, so a bare "/fonts/" arrives as "/fonts".
        elif path == "/fonts" or path.startswith("/fonts/"):
            self.serve_font(path[len("/fonts/"):])

        # Serve converted video (for web compatibility)
        elif path.startswith("/convert-video/"):
            self.serve_converted_video(path)

        # Theme endpoint — public: the index/build pages read the saved theme
        elif path == "/api/theme":
            self.send_json({"theme": load_config().get("theme", "dark")})

        # Config endpoint
        elif path == "/api/config":
            self.send_json(load_config())

        # Dashboard endpoint
        elif path == "/api/dashboard":
            period = params.get("period", ["days"])[0]
            rng = params.get("range", [None])[0]
            project = params.get("project", [""])[0]
            self.send_json(get_dashboard_data(period, rng, project))

        # Live stream — proxied from the Mobile Controller
        elif path == "/stream.html":
            self.serve_page("stream.html")
        elif path.startswith("/stream/"):
            self.proxy_device_stream(unquote(path[len("/stream/"):]))

        elif path == "/devices":
            self.send_json(list_devices_for_ui())
        elif path == "/api/devices/events":
            self.stream_device_events()
        elif path == "/api/config/visual-model":
            self.send_json(self.get_visual_model_config())
        elif path == "/api/visual-model/plugin":
            self.send_json(visual_plugin_info())

        # Build & Test: build plugin settings, projects and builds
        elif path == "/api/config/build":
            self._build_call(lambda: _builds().settings_with_checks())
        elif path == "/api/config/build/branches":
            # Branch picker in the Settings project table; the repo may not
            # be saved yet.
            repo, plugin = params.get("repo", [""])[0], params.get("plugin", [""])[0]
            self._build_call(lambda: _builds().settings_branches(repo, plugin))
        elif path == "/api/config/build/template":
            self.serve_build_template(params.get("plugin", [""])[0], params.get("platform", [""])[0])
        elif path.startswith("/api/config/build/plugins/") and path.endswith("/config"):
            name = unquote(path[len("/api/config/build/plugins/"):-len("/config")])
            self._build_call(lambda: _builds().plugin_config(name))
        # Deprecated aliases of the built-in Gitea client's settings routes.
        elif path == "/api/config/gitea":
            self._build_call(lambda: legacy_gitea_settings(_builds().settings_with_checks()))
        elif path == "/api/config/gitea/branches":
            repo = params.get("repo", [""])[0]
            self._build_call(lambda: _builds().settings_branches(
                repo, legacy_gitea_plugin(_builds().load_config(), repo)))
        elif path == "/api/builds":
            self._build_call(lambda: {"builds": _builds().list_builds(get_run_result)})
        elif path == "/api/builds/repos":
            cfg = _builds().load_config()
            self.send_json({"configured": bool(cfg["projects"]),
                            "repos": [p["repo"] for p in cfg["projects"]],
                            "projects": [dict(p) for p in cfg["projects"]]})
        elif path == "/api/builds/branches":
            repo = params.get("repo", [""])[0]
            self._build_call(lambda: _builds().list_branches(repo))
        elif path == "/api/builds/cases":
            repo, ref = params.get("repo", [""])[0], params.get("ref", [""])[0]
            self._build_call(lambda: _builds().list_cases(repo, ref))
        elif path == "/api/builds/workflow-template":
            # Deprecated: the build plugins serve per-platform build scripts now.
            cfg = _builds().load_config()
            repo = params.get("repo", [""])[0]
            plugin = params.get("plugin", [""])[0] or legacy_gitea_plugin(cfg, repo)
            platform = params.get("platform", [""])[0] or "android"
            self.send_response(302)
            self.send_header("Location", "/api/config/build/template?plugin=" + quote(plugin, safe="") +
                             "&platform=" + quote(platform, safe=""))
            self.end_headers()
        elif path.startswith("/api/builds/") and path.endswith("/log"):
            self.serve_build_log(path[len("/api/builds/"):-len("/log")], params.get("offset", ["0"])[0])
        elif path.startswith("/api/builds/"):
            build_id = path[len("/api/builds/"):]
            self._build_call(lambda: _builds().refresh_build(
                _builds().load_build(build_id), get_run_result, force=True))

        # Llama-server loaded model info
        elif path == "/api/llama-loaded-model":
            self.send_json(self.get_llama_loaded_model())

        # Run status — active runs + queued tests
        elif path == "/api/run-status":
            self.send_json(self.get_run_status())

        # Active device plugins for the Settings page: same source as PLUGIN steps.
        elif path == "/api/plugins":
            self.send_json(plugins_overview())

        # Who is signed in, plus the viewer flags for the nav. Public (the
        # login page asks it too).
        elif path == "/api/auth/status":
            user = self.auth_user
            status = {
                "signed_in": bool(user),
                "username": user["username"] if user else "",
                "role": user["role"] if user else "",
                # Admins see a warning banner while their password is still
                # the seeded default (admin/admin).
                "default_password": bool(user and user["role"] == "admin"
                                         and user["default_password"]),
                # True when MT_NATIVE_BACK is set: the frontend shows an
                # in-page back button instead of relying on browser chrome.
                "native_back": _native_back(),
                # True when MT_DISABLE_WEB_RUNS is set: the nav hides
                # "Build & Test".
                "web_runs_disabled": _web_runs_disabled(),
            }
            if user:
                # Every page asks for this: re-send the cookie so the
                # browser's 400-day cap keeps moving and the login never ends.
                self._send_json_with_cookie(_session_cookie(self._session_token()), status)
            else:
                self.send_json(status)

    def add_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self.add_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        if not self._authorize("HEAD"):
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/" or path == "":
            self._head_page("index.html")
        elif path == "/stream.html" or path == "/stream":
            self._head_page("stream.html")
        else:
            self._head_page("index.html")

    def _head_page(self, filename):
        filepath = PAGE_DIR / filename
        if filepath.exists():
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(filepath.stat().st_size))
            self.add_cors_headers()
            self.end_headers()
        else:
            self.send_error(404)

    def serve_page(self, filename):
        filepath = PAGE_DIR / filename
        if filepath.exists():
            content_type = "text/html"
            body = filepath.read_text()
            body = body.replace("{{CSS}}", SHARED_CSS)
            body = body.replace("{{SETTINGS_CSS}}", SETTINGS_CSS)
            body = body.replace("{{JS}}", SHARED_JS)
            # Apply the saved theme before first paint: the server knows the
            # theme, so a light-mode page never flashes dark on page switches —
            # no dependence on this browser's localStorage.
            if load_config().get("theme", "dark") == "light":
                body = body.replace('<html lang="en">',
                                    '<html lang="en" data-theme="light">', 1)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.add_cors_headers()
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_error(404)

    def serve_font(self, name):
        """GET /fonts/<name>.woff2 — one of the woff2 files in fonts/.

        Only a bare file name present in fonts/ is served, so the route can't
        reach anything else on disk. Fonts never change between deploys
        without a file rename, so browsers may cache them for a day.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.woff2", name):
            self.send_error(404)
            return
        try:
            filepath = sec.safe_join(FONT_DIR, name)
        except sec.UnsafePath:
            self.send_error(404)
            return
        if not filepath.is_file():
            self.send_error(404)
            return
        data = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "font/woff2")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.add_cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def serve_run_file(self, rel_path):
        parts = [p for p in rel_path.split("/") if p]  # remove empty strings
        if not parts or parts[0] != "static":
            self.send_error(404)
            return

        # /static/<run>/<rest_of_path>: only inside a run_N directory.
        if len(parts) < 3 or not sec.valid_run_id(parts[1]):
            self.send_error(404)
            return
        try:
            filepath = sec.safe_join(TEST_DIR / parts[1], *parts[2:])
        except sec.UnsafePath:
            self.send_error(404)
            return

        if not filepath.is_file():
            self.send_error(404)
            return

        ext = filepath.suffix.lower()
        content_type = {
            ".mp4": "video/mp4",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".json": "application/json",
            ".txt": "text/plain",
        }.get(ext)

        if not content_type:
            try:
                with open(filepath, "rb") as f:
                    header = f.read(8)
                if header[:8] == b"\x89PNG\r\n\x1a\n":
                    content_type = "image/png"
                elif header[:6] in (b"\xff\xd8\xff",):
                    content_type = "image/jpeg"
            except Exception:
                pass
            if not content_type:
                content_type = "application/octet-stream"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", filepath.stat().st_size)
        self.send_header("Accept-Ranges", "bytes")
        self.add_cors_headers()
        self.end_headers()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def serve_converted_video(self, rel_path):
        """Convert Android MP4 to WebM for better Firefox compatibility."""
        import subprocess
        import hashlib
        import time
        
        parts = [p for p in rel_path.split("/") if p]
        if not parts or parts[0] != "convert-video":
            self.send_error(404)
            return

        # /convert-video/<run>/<video_filename>
        if len(parts) < 3 or not sec.valid_run_id(parts[1]):
            self.send_error(404)
            return
        try:
            video_path = sec.safe_join(TEST_DIR / parts[1], *parts[2:])
        except sec.UnsafePath:
            self.send_error(404)
            return


        if not video_path.is_file():
            self.send_error(404)
            return

        # Always convert to WebM (VP9 + Opus) for web compatibility
        cache_dir = TEST_DIR / ".cache"
        cache_dir.mkdir(exist_ok=True)
        
        # Create cache key
        stat = video_path.stat()
        cache_key = hashlib.md5(f"{video_path}{stat.st_mtime}{stat.st_size}".encode(),
                                usedforsecurity=False).hexdigest()
        cache_file = cache_dir / f"{cache_key}.webm"

        if not cache_file.exists() or os.path.getsize(cache_file) < 1000:
            # Full conversion to WebM
            # argv list, no shell; video_path is an absolute path under Run/.
            proc = subprocess.run(  # nosec B603,B607
                ['ffmpeg', '-nostdin', '-y', '-i', str(video_path),
                 '-c:v', 'libvpx-vp9', '-b:v', '150k', '-minrate', '50k', '-maxrate', '250k',
                 '-pix_fmt', 'yuv420p', '-c:a', 'libopus',
                 '-f', 'webm', str(cache_file)],
                capture_output=True, text=True, timeout=300
            )
            if proc.returncode != 0:
                print(f"CONVERT ERROR: {proc.stderr[:200]}", file=sys.stderr, flush=True)
                self.send_error(500)
                return

        # Serve converted WebM
        self.send_response(200)
        self.send_header("Content-Type", "video/webm")
        self.send_header("Content-Length", cache_file.stat().st_size)
        self.send_header("Accept-Ranges", "bytes")
        self.add_cors_headers()
        self.end_headers()
        with open(cache_file, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)


    def get_device_names(self):
        """Return current device name mappings."""
        return load_device_names()

    def save_device_names_from_request(self):
        """Handle POST to /api/config/devices — body: {device id: name}."""
        body = self._body()
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                self.send_json_status(400, {"error": "Expected a JSON object"})
                return
            save_device_names(data)
            self.send_json(data)
        except Exception:
            self.send_error(400)

    def stream_device_events(self):
        """Relay the controller's device list changes to a page as server-sent events."""
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last = None
        try:
            for payload in controller_device_events():
                body = None if payload is None else json.dumps(device_event_for_ui(payload))
                if body is None or body == last:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    self.wfile.write(f"event: devices\ndata: {body}\n\n".encode())
                    last = body
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # page closed

    def stream_run_events(self, run, device_id):
        """Push a run's state to test.html as server-sent events, instead of
        the page polling /api/pending. A `run` event carries the selected
        device's /api/pending body; one is sent on connect and again whenever
        any device's files for the run change. Otherwise a keepalive comment
        goes out every RUN_EVENTS_KEEPALIVE_SECS, which is also how a closed
        page is noticed."""
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last_sig = None
        last_write = 0.0
        try:
            while True:
                sig = run_state_signature(run)
                now = time.monotonic()
                if sig != last_sig:
                    body = json.dumps({"pending": get_pending_payload(run, device_id)})
                    self.wfile.write(f"event: run\ndata: {body}\n\n".encode())
                    self.wfile.flush()
                    last_sig, last_write = sig, now
                elif now - last_write >= RUN_EVENTS_KEEPALIVE_SECS:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_write = now
                time.sleep(RUN_EVENTS_CHECK_SECS)
        except (BrokenPipeError, ConnectionResetError):
            pass  # page closed

    def proxy_device_stream(self, device_id):
        """Proxy /stream/<id> to the controller's per-device MJPEG stream."""
        import urllib.request

        base = _controller_url()
        if not base or not DEVICE_ID_RE.fullmatch(device_id):
            self.send_error(502 if not base else 400, "Stream unavailable")
            return
        headers_sent = False
        try:
            req = urllib.request.Request(
                sec.check_http_url(f"{base}/devices/{quote(device_id, safe=':')}/stream"),
                headers=_controller_headers())
            with urllib.request.urlopen(req, timeout=15) as upstream:  # nosec B310
                self.send_response(200)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "multipart/x-mixed-replace"))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
                while True:
                    chunk = upstream.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # viewer closed the page
        except Exception as e:
            print(f"Device stream proxy error for {device_id}: {e}", file=sys.stderr, flush=True)
            if not headers_sent:
                self.send_error(502, "Stream proxy failed")

    def do_POST(self):
        if not self._authorize("POST"):
            return
        parsed = urlparse(self.path)
        self._mt_body = None
        if has_path_traversal(parsed.path):
            self.send_json_status(400, {"error": "invalid path"})
            return
        # Size check before anything is read: uploads have their own cap
        # (MT_MAX_UPLOAD_MB), every other POST is a small JSON body.
        limit = sec.max_upload_bytes() if parsed.path == "/api/run-test" else sec.MAX_JSON_BODY_BYTES
        try:
            self._mt_body_length = sec.body_length(self.headers, limit)
        except sec.BodyError as e:
            self.close_connection = True  # the unread body must not be parsed as a request
            self.send_json_status(e.status, {"error": e.message})
            return
        if parsed.path == "/api/theme":
            self.update_theme()
        elif parsed.path == "/api/login":
            self.handle_login()
        elif parsed.path == "/api/logout":
            self.handle_logout()
        elif parsed.path == "/api/account/password":
            self.handle_change_password()
        elif parsed.path == "/api/users":
            if self._admin_required():
                self.handle_users()
        elif parsed.path == "/api/config/ci-token":
            if self._admin_required():
                self.handle_ci_token()
        elif parsed.path == "/api/config/service-tokens":
            if self._admin_required():
                self.handle_service_tokens()
        # /api/config merges any key into Run/config.json, including the
        # visual model endpoint.
        elif parsed.path == "/api/config":
            self.update_config()
        elif parsed.path == "/api/config/devices":
            self.save_device_names_from_request()
        elif parsed.path == "/api/config/controller":
            # Settings → Plugins → Controller Plugin: add/edit/remove the
            # Mobile Controller's address (config.json controller_url).
            self.handle_controller_config()
        elif parsed.path == "/api/config/plugins":
            # Settings → Device Plugins: add/remove/edit device-plugin
            # registrations (config.json `plugins`).
            self.handle_plugin_config()
        elif parsed.path == "/api/config/visual-model":
            self.save_visual_model_config()
        elif parsed.path == "/api/visual-model/models":
            self.handle_local_models()
        elif parsed.path == "/api/visual-model/plugin/copilot/device/start":
            # GitHub Copilot sign-in lives on the AI plugin; the viewer only relays.
            self._ai_plugin_call(lambda pc, c: pc.ai_copilot_device_start(c))
        elif parsed.path == "/api/visual-model/plugin/copilot/device/poll":
            self._ai_plugin_call(lambda pc, c: pc.ai_copilot_device_poll(c))
        elif parsed.path == "/api/visual-model/plugin/copilot/remove":
            self._ai_plugin_call(lambda pc, c: {"copilot": pc.ai_copilot_remove(c)})
        elif parsed.path == "/api/visual-model/test":
            # Spends model calls with the saved API key.
            self.test_visual_model()
        elif parsed.path == "/api/run-test":
            self.handle_run_test()
        elif parsed.path == "/api/runs/delete":
            self.handle_delete_runs()
        elif parsed.path == "/api/press-event":
            self.handle_press_event()
        elif parsed.path == "/api/config/build":
            self.handle_build_projects()
        elif parsed.path.startswith("/api/config/build/plugins/") and parsed.path.endswith("/config"):
            name = unquote(parsed.path[len("/api/config/build/plugins/"):-len("/config")])
            data = self._read_json_body()
            if data is not None:
                self._build_call(lambda: _builds().save_plugin_config(name, data))
        # Deprecated aliases of the built-in Gitea client's settings routes.
        elif parsed.path == "/api/config/gitea":
            self.handle_legacy_gitea_settings()
        elif parsed.path == "/api/config/gitea/remove-token":
            self._build_call(lambda: _builds().save_plugin_config(
                legacy_gitea_plugin(_builds().load_config()), {"token": None}))
        elif parsed.path == "/api/builds":
            self.handle_start_build()
        elif parsed.path.startswith("/api/builds/") and parsed.path.endswith("/cancel"):
            build_id = parsed.path[len("/api/builds/"):-len("/cancel")]
            self._build_call(lambda: _builds().cancel_build(build_id, get_run_result))
        else:
            self.send_error(404)

    def _send_plugin_error(self, e):
        """A PluginError from the AI plugin, with the plugin's own code and message
        so the page can show them. A 400 from the plugin (e.g. Copilot picked
        while signed out) stays 400; everything else — including the plugin
        rejecting the pairing token — is 502, so it can't be mistaken for the
        viewer's own errors."""
        status = 400 if e.status == 400 or e.code in ("not_configured", "invalid_request",
                                                       "bad_request") else 502
        self.send_json_status(status, {"error": e.message, "code": e.code,
                                       "plugin": e.plugin, "plugin_status": e.status})

    def _ai_plugin_call(self, fn):
        """Send fn(plugin_client, AI plugin client)'s result as JSON."""
        pc = _plugin_client()
        try:
            _, client = ai_plugin_client()
            self.send_json(fn(pc, client))
        except pc.PluginError as e:
            self._send_plugin_error(e)

    def handle_local_models(self):
        """POST {endpoint, api_key?}: the models a local endpoint offers. The saved
        key is only sent to the saved endpoint, never to a URL typed on the page."""
        data = self._read_json_body()
        if data is None:
            return
        endpoint = (data.get("endpoint") or "").strip()
        cfg = load_config()
        if not endpoint:
            endpoint = (cfg.get("visual_endpoint") or os.environ.get("VISION_MODEL_API_URL") or "").strip()
        if urlparse(endpoint).scheme not in ("http", "https"):
            self.send_json_status(400, {"error": "Endpoint must be an http(s) URL"})
            return
        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            saved_ep, saved_key, _ = vision_settings()
            if endpoint == saved_ep:
                api_key = saved_key
        self.send_json(list_local_models(endpoint, api_key))

    def _build_call(self, fn):
        """Send fn()'s result as JSON; BuildError becomes its status (default
        502) with {error, code?, ...} (a failed project save adds `checks`)."""
        try:
            self.send_json(fn())
        except builds.BuildError as e:
            body = {"error": str(e), **e.extra}
            if e.code:
                body["code"] = e.code
            self.send_json_status(e.status or 502, body)

    def _body(self):
        """The request body, read once (its length was checked in do_POST)."""
        if getattr(self, "_mt_body", None) is None:
            length = getattr(self, "_mt_body_length", None)
            if length is None:
                length = sec.body_length(self.headers, sec.MAX_JSON_BODY_BYTES)
            self._mt_body = self.rfile.read(length) if length else b""
        return self._mt_body

    def _read_json_body(self):
        try:
            data = json.loads(self._body() or b"{}")
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            self.send_json_status(400, {"error": "Invalid JSON"})
            return None
        return data

    def handle_build_projects(self):
        """POST /api/config/build {projects: [{repo, plugin, branch}]}: each
        project is checked with its build plugin first; nothing is stored if
        any check fails (400 {error, checks})."""
        data = self._read_json_body()
        if data is None:
            return
        self._build_call(lambda: _builds().save_projects(data))

    def serve_build_template(self, plugin, platform):
        """GET /api/config/build/template?plugin=&platform= — the plugin's
        example build script for that platform (its GET /template), as a
        download named after the script's path in the app repo."""
        try:
            text, filename = _builds().template(plugin, platform)
        except builds.BuildError as e:
            body = {"error": str(e), **e.extra}
            self.send_json_status(e.status or 502, body)
            return
        self.send_file_bytes(text.encode(), "text/plain; charset=utf-8", filename)

    def serve_build_log(self, build_id, offset):
        """GET /api/builds/<id>/log?offset= — the build plugin's log from byte
        offset (text/plain), with X-Log-Size (total size) and X-Log-Complete
        (1 once the build has finished), so build.html can tail it."""
        try:
            log = _builds().build_log(build_id, offset)
        except builds.BuildError as e:
            body = {"error": str(e), **e.extra}
            if e.code:
                body["code"] = e.code
            self.send_json_status(e.status or 502, body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(log["text"])))
        self.send_header("X-Log-Size", str(log["size"]))
        self.send_header("X-Log-Complete", "1" if log["complete"] else "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(log["text"])

    def handle_legacy_gitea_settings(self):
        """Deprecated POST /api/config/gitea {url?, token?, repos, branches?}:
        url/token go to the alias plugin's /config (forge_kind gitea with a
        token), repos + branches become the projects (a repo keeps its
        plugin, new ones get the alias plugin)."""
        data = self._read_json_body()
        if data is None:
            return
        b = _builds()
        try:
            cfg = b.load_config()
            plugin = legacy_gitea_plugin(cfg)
            forge = {}
            if (data.get("token") or "").strip():
                forge["token"] = data["token"].strip()
                forge["forge_kind"] = "gitea"
            if (data.get("url") or "").strip():
                forge["forge_url"] = data["url"].strip()
            if forge:
                b.save_plugin_config(plugin, forge)
            if "repos" in data:
                repos = data["repos"]
                if isinstance(repos, str):
                    repos = re.split(r"[\s,]+", repos)
                picks = data.get("branches") if isinstance(data.get("branches"), dict) else None
                current = {p["repo"]: p for p in cfg["projects"]}
                projects = []
                for repo in repos or []:
                    repo = str(repo).strip().strip("/")
                    if not repo:
                        continue
                    old = current.get(repo, {})
                    branch = (picks or {}).get(repo, "") if picks is not None else old.get("branch", "")
                    projects.append({"repo": repo, "plugin": old.get("plugin") or plugin, "branch": branch})
                result = b.save_projects({"projects": projects})
            else:
                result = b.settings_with_checks()
        except builds.BuildError as e:
            body = {"error": str(e), **e.extra}
            if e.code:
                body["code"] = e.code
            self.send_json_status(e.status or 502, body)
            return
        self.send_json(legacy_gitea_settings(result))

    def handle_start_build(self):
        """Start a build: {repo, branch, platforms: [...], cases: [...]}."""
        data = self._read_json_body()
        if data is None:
            return
        user = (self.auth_user or {}).get("username", "")
        self._build_call(lambda: _builds().create_build(
            data.get("repo"), data.get("branch"), data.get("platforms"), data.get("cases"), user))

    def _wait_for_new_run_dir(self, existing_names, timeout=15.0):
        """Poll TEST_DIR for a run_N directory that didn't exist before
        we launched run_on_device.py. Directory creation happens in the first
        fraction of a second of run_on_device.py's startup (well before APK
        install/test execution), so a short poll is sufficient."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            after = {d.name for d in TEST_DIR.iterdir() if d.is_dir()}
            new = sorted(n for n in (after - existing_names) if RUN_ID_PATTERN.match(n))
            if new:
                return new[-1]
            time.sleep(0.2)
        return None

    def handle_run_test(self):
        """Accept an uploaded APK + test files (INSTRUCTION.txt + .txt test
        cases) and launch mt-tests/scripts/run_on_device.py as a detached
        background subprocess against a wireless-ADB device."""
        try:
            fields, files = parse_multipart_form(self, max_body_bytes=sec.max_upload_bytes())
        except ValueError as e:
            self.send_json_status(400, {"error": str(e)})
            return

        # Runs come only from a Build & Test build (the viewer's build, or CI
        # submitting for one). A manual upload of an app is not accepted.
        build_request = (fields.get("build_request") or "").strip()
        try:
            known_build = bool(build_request) and _builds().load_build(build_request)
        except builds.BuildError:
            known_build = False
        if not known_build:
            self.send_json_status(400, {
                "error": "Runs start from Build & Test: send a build_request naming a build"})
            return
        if self.auth_via == "build_request" and not build_upload_allowed(known_build):
            # No session and no CI token: only a build the viewer started that
            # is still waiting for its upload may send one (the build-local
            # plugin uploads with its build's id, which only it and the
            # viewer know). Anything else needs the CI token.
            self.send_json_status(401, {
                "error": "Authentication required: send the CI token "
                         "(Authorization: Bearer <token>, Settings → Configuration → CI token)"})
            return

        # App files: the "apk" part (an .apk, or for iOS-only runs an .ipa/.zip,
        # as before) and an optional "ios_app" part. One run can carry one
        # Android app and one iOS app; each platform's devices get its own.
        app_parts = (files.get("apk") or []) + (files.get("ios_app") or [])
        if not app_parts and not any((fields.get(f"build_failed_{p}") or "").strip()
                                     for p in ("android", "ios")):
            self.send_json_status(400, {"error": "An app file is required"})
            return
        android_app = ios_app = None
        for part in app_parts:
            name = part["filename"].lower()
            if name.endswith(".apk"):
                if android_app:
                    self.send_json_status(400, {"error": "Only one Android .apk per run"})
                    return
                android_app = part
            elif name.endswith((".ipa", ".zip")):
                if ios_app:
                    self.send_json_status(400, {"error": "Only one iOS app (.ipa or .zip) per run"})
                    return
                ios_app = part
            else:
                self.send_json_status(400, {
                    "error": "App file must be a .apk (Android), or a .ipa or .zip holding the .app (iOS)"})
                return
            if not (0 < len(part["content"]) <= MAX_APK_BYTES):
                self.send_json_status(400, {"error": f"{part['filename']} is empty or exceeds size limit"})
                return
        mode = (fields.get("device_mode") or "specific").strip().lower()
        wait_platforms = []
        build_failed = {}
        build_logs = {}
        if mode in {"android", "ios", "all"}:
            # Platform modes: the run itself picks the devices when its turn
            # comes, waiting up to MT_DEVICE_CONNECT_WAIT_SECS (5 min) for a
            # missing phone to connect. The upload is accepted either way.
            if mode == "android" and not android_app and not fields.get("build_failed_android"):
                self.send_json_status(400, {"error": "Android runs need an .apk"})
                return
            if mode == "ios" and not ios_app and not fields.get("build_failed_ios"):
                self.send_json_status(400, {
                    "error": "iOS runs need an .ipa or a .zip containing the .app bundle (not an APK)"})
                return
            if mode == "android" and ios_app or mode == "ios" and android_app:
                self.send_json_status(400, {
                    "error": f"device_mode {mode} was given an app for the other platform; use device_mode all"})
                return
            wait_platforms = [p for p, part in (("android", android_app), ("ios", ios_app)) if part]
            targets = {"android": [], "ios": [], "ios_placeholder": False}
            # CI: a platform whose app didn't build (e.g. signing) is sent as
            # build_failed_<platform>=<error>; its devices are shown failed.
            for platform, part in (("android", android_app), ("ios", ios_app)):
                message = (fields.get(f"build_failed_{platform}") or "").strip()
                if not message:
                    continue
                if part:
                    self.send_json_status(400, {
                        "error": f"build_failed_{platform} was given with an app for {platform}"})
                    return
                if mode not in ("all", platform):
                    self.send_json_status(400, {
                        "error": f"build_failed_{platform} needs device_mode all or {platform}"})
                    return
                build_failed[platform] = message[:2000]
            for platform in ("android", "ios"):
                logs = files.get(f"build_log_{platform}") or []
                if logs and platform not in build_failed:
                    self.send_json_status(400, {
                        "error": f"build_log_{platform} needs build_failed_{platform}"})
                    return
                if logs and len(logs[0]["content"]) > MAX_BUILD_LOG_BYTES:
                    # Keep the end: that's where xcodebuild/gradle report the error.
                    logs[0]["content"] = logs[0]["content"][-MAX_BUILD_LOG_BYTES:]
                if logs:
                    build_logs[platform] = logs[0]["content"]
        else:
            targets, target_error = resolve_run_targets(fields)
            if target_error:
                self.send_json_status(400, {"error": target_error})
                return
            if android_app and ios_app:
                if not targets.get("android") or not targets.get("ios"):
                    self.send_json_status(400, {
                        "error": "With an .apk and an iOS app, pick an Android device and an iPhone"})
                    return
            elif ios_app and targets.get("android"):
                self.send_json_status(400, {
                    "error": "Android devices need an .apk: upload one alongside the iOS app"})
                return
            elif ios_app and not targets.get("ios"):
                self.send_json_status(400, {"error": f"{ios_app['filename']} needs an iOS device target"})
                return
            elif android_app and targets.get("ios"):
                self.send_json_status(400, {
                    "error": "iOS runs need an .ipa or a .zip containing the .app bundle (not an APK)"})
                return

        txt_parts = [f for f in (files.get("tests") or []) if f["filename"].lower().endswith(".txt")]
        snapshot_parts = files.get("snapshots") or []
        payload_parts = files.get("payloads") or []
        layout, layout_error = plan_test_upload(txt_parts, snapshot_parts, payload_parts)
        if layout_error:
            self.send_json_status(400, {"error": layout_error})
            return
        case_name = (fields.get("case") or "").strip()
        if case_name and not CASE_NAME_RE.match(case_name):
            self.send_json_status(400, {"error": "case may only use letters, digits, _ . -"})
            return
        if layout["cases"]:
            case_name = ""  # a multi-case run records every case itself
        # Values that reach the runner's argv: allowlisted, never option-like.
        ios_bundle_id = (fields.get("ios_bundle_id") or "").strip()
        if ios_bundle_id and not sec.valid_bundle_id(ios_bundle_id):
            self.send_json_status(400, {"error": "ios_bundle_id must be a reverse-DNS id "
                                                 "(letters, digits, _ - separated by dots)"})
            return
        branch = (fields.get("branch") or "").strip()
        if branch and not sec.valid_branch(branch):
            self.send_json_status(400, {"error": "branch is not a valid git branch name"})
            return
        commit = (fields.get("commit") or "").strip().lower()
        if commit and not COMMIT_RE.fullmatch(commit):
            self.send_json_status(400, {"error": "commit must be a hex commit id"})
            return
        commit_message = clean_commit_message(fields.get("commit_message"))

        # Stage uploaded files under Run/.uploads/<upload_id>/
        upload_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        stage_dir = UPLOADS_DIR / upload_id
        tests_dir = stage_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        app_paths = {}
        try:
            for key, part in (("android", android_app), ("ios", ios_app)):
                if part:
                    (stage_dir / key).mkdir()
                    app_paths[key] = sec.safe_join(stage_dir / key, sec.safe_filename(part["filename"]))
                    app_paths[key].write_bytes(part["content"])
            for rel, content in layout["files"]:
                dest = sec.safe_join(tests_dir, rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
        except sec.UnsafePath as e:
            shutil.rmtree(stage_dir, ignore_errors=True)
            self.send_json_status(400, {"error": f"unsafe upload path: {e}"})
            return

        cmd = [
            sys.executable, str(RUN_ON_DEVICE_SCRIPT),
            "--test-dir", str(tests_dir),
            "--output-dir", str(TEST_DIR),
        ]
        if "android" in app_paths:
            cmd += ["--apk", str(app_paths["android"])]
        if "ios" in app_paths:
            cmd += ["--ios-app", str(app_paths["ios"])]
        all_targets = [*targets["android"], *targets.get("ios", [])]
        if all_targets:
            cmd += ["--device", *all_targets]
        for platform in wait_platforms:
            cmd += ["--wait-for-platform", platform]
        for platform, message in build_failed.items():
            cmd += ["--build-failed", f"{platform}={message}"]
        for platform, content in build_logs.items():
            log_path = stage_dir / f"build_log_{platform}.txt"
            log_path.write_bytes(content)
            cmd += ["--build-log", f"{platform}={log_path}"]
        if targets.get("ios_placeholder"):
            cmd.append("--ios-placeholder")
        if (targets.get("ios") or "ios" in wait_platforms) and ios_bundle_id:
            cmd += ["--ios-bundle-id", ios_bundle_id]
        if fields.get("debug") == "on":
            cmd.append("--debug")
        if fields.get("no_video") == "on":
            cmd.append("--no-video")
        if fields.get("record_snapshots") == "on":
            cmd.append("--record-snapshots")
        if case_name:
            cmd += ["--case", case_name]
        if branch:
            cmd += ["--branch", branch]
        repo = run_repo(fields)


        # Environment for the runner:
        #  - VIEWER_URL lets the runner report press events (tap/swipe markers)
        #    back to THIS viewer instance, so the overlay works whether the
        #    viewer runs on the same host or on a different machine.
        #  - CONTROLLER_URL is inherited from this process.
        #  - The service tokens come from Settings: os.environ mirrors
        #    service_tokens.json (viewer_auth.apply_to_environ).
        #  - MT_RUNNER_TOKEN lets the runner post press events back; it is
        #    made fresh at each viewer start and accepted only there.
        env = auth.apply_to_environ(os.environ.copy())
        env["MT_RUNNER_TOKEN"] = auth.RUNNER_TOKEN
        # Settings → Plugins → Controller Plugin owns the Mobile Controller's
        # address once config.json has the key; re-export the resolved value
        # so the runner talks to the same controller the viewer proxies.
        env["CONTROLLER_URL"] = _controller_url()
        try:
            port = self.server.server_address[1]
            env.setdefault("VIEWER_URL", f"http://127.0.0.1:{port}")
        except Exception:
            pass
        #  - MT_REPO names the run's project: recorded in run_metadata.json and
        #    used to pick that project's Test Variables. Never inherited, so a
        #    run with no repo is Unassigned.
        env.pop("MT_REPO", None)
        if repo:
            env["MT_REPO"] = repo
        #  - MT_COMMIT / MT_COMMIT_MESSAGE: the commit under test (submit.py
        #    reads it from the checkout), recorded in run_metadata.json and
        #    shown on the run list. Never inherited, like MT_REPO.
        for key, value in (("MT_COMMIT", commit), ("MT_COMMIT_MESSAGE", commit_message)):
            env.pop(key, None)
            if value:
                env[key] = value

        with _RUN_LAUNCH_LOCK:
            prev = _PREVIOUS_RUN["proc"]
            if prev is not None and prev.poll() is None:
                cmd += ["--after-pid", str(prev.pid)]
                if _PREVIOUS_RUN["run_id"]:
                    cmd += ["--after-run", _PREVIOUS_RUN["run_id"]]
            existing = {d.name for d in TEST_DIR.iterdir() if d.is_dir()}
            # argv list (no shell); every request-derived value above is
            # allowlisted (device ids, case, branch, bundle id) or a path we built.
            proc = subprocess.Popen(  # nosec B603
                cmd,
                cwd=str(RUN_ON_DEVICE_SCRIPT.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            run_id = self._wait_for_new_run_dir(existing, timeout=15.0)
            _PREVIOUS_RUN.update(proc=proc, run_id=run_id)

        # Clean up the staged upload once run_on_device.py exits — it only reads
        # from stage_dir at startup, nothing references it afterward. If the run
        # FAILED (non-zero exit), keep the staged files so the user can inspect
        # the exact APK/tests that were submitted.
        def _cleanup(p=proc, d=stage_dir, run=run_id):
            rc = p.wait()
            if run:
                try:
                    (TEST_DIR / run / RUN_EXIT_CODE_FILE).write_text(f"{rc}\n")
                except OSError as e:
                    print(f"Could not record exit code for {run}: {e}", flush=True)
            if rc == 0:
                shutil.rmtree(d, ignore_errors=True)
            else:
                print(f"Run failed (exit {rc}); keeping staged upload: {d}", flush=True)
        threading.Thread(target=_cleanup, daemon=True).start()

        build_request = (fields.get("build_request") or "").strip()
        if build_request and run_id:
            # This upload comes from a build started in the viewer.
            _builds().attach_run(build_request, run_id, case_name or ", ".join(layout["cases"]),
                                [p for p, part in (("android", android_app), ("ios", ios_app)) if part])

        if not run_id:
            self.send_json_status(202, {
                "run_id": None,
                "warning": "Test run started but its run directory wasn't detected yet; check the run list shortly.",
            })
            return

        self.send_json({"run_id": run_id})

    def handle_press_event(self):
        """Receive one tap/swipe from the test runner for the press overlay."""
        try:
            event = json.loads(self._body().decode() or "{}")
        except Exception:
            self.send_json_status(400, {"error": "invalid press event"})
            return
        if not isinstance(event, dict) or event.get("kind") not in ("tap", "swipe"):
            self.send_json_status(400, {"error": "kind must be tap or swipe"})
            return
        _press_add(event)
        self.send_json({"ok": True})

    def send_json_status(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.add_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def update_theme(self):
        """POST /api/theme — body: {"theme": "light"|"dark"}.

        Public: every page offers a theme toggle, and the theme is a
        cosmetic preference, not sensitive state.
        """
        body = self._body()
        try:
            data = json.loads(body)
            theme = data.get("theme")
            if theme not in ("light", "dark"):
                self.send_json_status(400, {"error": "theme must be 'light' or 'dark'"})
                return
            config = load_config()
            config["theme"] = theme
            save_config(config)
            self.send_json({"theme": theme})
        except Exception:
            self.send_error(400)

    def update_config(self):
        body = self._body()
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                self.send_json_status(400, {"error": "Expected a JSON object"})
                return
            # Settings -> Test Variables: `test_variables` is the one key with
            # a shape to check, so a mistyped save is rejected with a message
            # the settings page can show instead of poisoning every later run.
            if "test_variables" in data:
                clean, error = clean_test_variables(data["test_variables"], "test_variables")
                if error:
                    self.send_json_status(400, {"error": error})
                    return
                data["test_variables"] = clean
            # `project_variables` ({owner/repo: {KEY: value}}) is merged per
            # repo, so saving one project's variables leaves the others alone.
            # An empty map removes that project's entry.
            project_vars = None
            if "project_variables" in data:
                pv = data.pop("project_variables")
                if not isinstance(pv, dict):
                    self.send_json_status(400, {"error": "project_variables must be a JSON object"})
                    return
                project_vars = {}
                for repo, tv in pv.items():
                    repo = str(repo).strip().strip("/")
                    if not builds.REPO_RE.match(repo):
                        self.send_json_status(400, {"error": f"{repo!r} is not owner/repo"})
                        return
                    clean, error = clean_test_variables(tv, f"variables of {repo}")
                    if error:
                        self.send_json_status(400, {"error": error})
                        return
                    project_vars[repo] = clean
            with _CONFIG_LOCK:
                config = load_config()
                config.update(data)
                if project_vars is not None:
                    merged = dict(config.get("project_variables") or {})
                    for repo, clean in project_vars.items():
                        if clean:
                            merged[repo] = clean
                        else:
                            merged.pop(repo, None)
                    config["project_variables"] = merged
                save_config(config)
            self.send_json(config)
        except Exception:
            self.send_error(400)

    def handle_controller_config(self):
        """POST /api/config/controller — ONE action per request:
        {"set": {"url": u}} registers/updates the Mobile Controller — the
        address must answer GET <u>/devices with 200 (a 401/403 wall says
        the service token has to be saved first), nothing is stored on
        failure and the controller's own state shows in the reply, mirroring
        the strict plugin-save rule. The write adds the controller_url key,
        after which the CONTROLLER_URL environment is ignored.
        {"remove": true} forgets the address (writes "" — the key stays, so
        Settings keeps owning the slot and a later env value cannot sneak
        back in)."""
        try:
            data = json.loads(self._body())
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self.send_json_status(400, {"error": "Invalid JSON: expected an object",
                                        "code": "bad_request"})
            return

        def bad(msg, code="bad_request"):
            self.send_json_status(400, {"error": msg, "code": code})

        if set(data) == {"remove"}:
            if data["remove"] is not True:
                bad('remove must be true')
                return
            with _CONFIG_LOCK:
                config = load_config()
                if "controller_url" not in config and not _controller_url():
                    bad("no Mobile Controller is registered", "nothing_to_remove")
                    return
                config["controller_url"] = ""
                save_config(config)
            self.send_json({"ok": True, "removed": "mobile-controller",
                            "controller": controller_plugin_state()})
            return
        if set(data) == {"set"}:
            op = data["set"]
            if not isinstance(op, dict) or set(op) != {"url"}:
                bad('set must be {"url": ...}')
                return
            url = str(op["url"] or "").strip().rstrip("/")
            if not url or not url.startswith(("http://", "https://")):
                bad("url must be an http(s):// address", "bad_url")
                return
            status, body = _get_json(f"{url}/devices", headers=_controller_headers(),
                                     timeout=5)
            if status is None:
                bad(f"the Mobile-Controller at {url} did not answer GET /devices",
                    "unreachable")
                return
            if status in (401, 403):
                bad("the Mobile-Controller rejected the request (GET /devices: %d) — "
                    "save its token first in Settings → Configuration → Service tokens" % status,
                    "token_required")
                return
            if status != 200:
                bad(f"the Mobile-Controller answered GET /devices with status {status}",
                    "not_a_controller")
                return
            with _CONFIG_LOCK:
                config = load_config()
                config["controller_url"] = url
                save_config(config)
            self.send_json({"ok": True, "set": url,
                            "controller": controller_plugin_state()})
            return
        bad('one action per request: {"set": {"url": ...}} or {"remove": true}')

    def handle_plugin_config(self):
        """POST /api/config/plugins — one action per request, in the shared
        Settings → Plugins registry (config.json `plugins`):
        {"set": {"name": n, "url": u[, "token": t]}} registers/updates one
        plugin — its /health contract decides the kind: a device plugin
        (contract 'monkeytester-plugin') is stored as name: url, a build
        plugin (contract 'monkeytester-build-plugin') as name: {"url", "token"}
        (token: a string sets the pairing token, null clears it, absent or
        blank keeps the saved one; device-plugin tokens stay env-based).
        {"remove": name} forgets either entry; a build plugin still used by
        a project refuses. The first write seeds the map from the MT_PLUGINS
        entries still live then, and afterwards Settings owns the list.
        monkeytester-ai goes through the same Add row: a set op whose
        address's /health names plugin monkeytester-ai (or whose name is
        monkeytester-ai) is stored the AI way (_ai_plugin_address_op:
        visual_plugin_url + visual_plugin, token -> visual_api_key), and
        {"remove": "monkeytester-ai"} clears it; managed mode refuses (409)."""
        try:
            data = json.loads(self._body())
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self.send_json_status(400, {"error": "Invalid JSON: expected an object",
                                        "code": "bad_request"})
            return
        pc = _plugin_client()
        bpc = builds.bpc  # build_plugin_client, the build-plugin contract's

        def bad(msg, code="bad_request"):
            self.send_json_status(400, {"error": msg, "code": code})

        def safe_plugins(base):
            """The registry for the response: a build entry's raw pairing token
            never goes back out — only {configured, last4}, like every token view."""
            return {n: ({**{k: v for k, v in e.items() if k != "token"},
                          "token": builds.token_info(auth.plugin_token(n))}
                        if isinstance(e, dict) else e)
                    for n, e in base.items()}

        def env_entries():
            try:
                return {name: url for name, url in
                        pc.parse_plugins(os.environ.get("MT_PLUGINS", ""))
                        if name != pc.AI_PLUGIN_NAME}
            except pc.PluginError:
                return {}

        def base_and_save(config) -> dict:
            """The list to write on: config.json `plugins` when it owns the
            list (entries kept as-is: url strings AND build objects), else the
            still-live MT_PLUGINS entries (first write seeds)."""
            raw = config.get("plugins")
            return dict(raw) if isinstance(raw, dict) else env_entries()

        if set(data) == {"remove"}:
            name = data["remove"]
            if not isinstance(name, str) or not pc.PLUGIN_NAME_RE.fullmatch(name):
                bad(f"plugin name {name!r} must match {pc.PLUGIN_NAME_RE.pattern}")
                return
            if name == pc.AI_PLUGIN_NAME:
                # The AI row on Settings → Plugins: same as
                # /api/config/visual-model {"plugin_url": ""}.
                if managed_ai_plugin():
                    self.send_json_status(409, {"error": MANAGED_AI_MESSAGE})
                    return
                with _CONFIG_LOCK:
                    if not ai_plugin_url(load_config()):
                        bad(f"no plugin {name!r} registered", "nothing_to_remove")
                        return
                    status, body = _ai_plugin_address_op("")
                self.send_json({"ok": True, "removed": name, "ai": body})
                return
            with _CONFIG_LOCK:
                config = load_config()
                base = base_and_save(config)
                if name not in base:
                    bad(f"no plugin {name!r} registered")
                    return
                entry = base[name]
                if isinstance(entry, dict):
                    used = [p["repo"] for p in _builds().load_config()["projects"]
                            if p["plugin"] == name]
                    if used:
                        bad(f"{name} is used by {', '.join(used)}; "
                            "move or remove those projects first", "in_use")
                        return
                base.pop(name)
                config["plugins"] = base
                save_config(config)
            if auth.plugin_token(name):
                auth.set_service_token(f"plugin:{name}", "")
            self.send_json({"ok": True, "removed": name, "plugins": safe_plugins(base)})
            return
        if set(data) == {"set"}:
            op = data["set"]
            if not isinstance(op, dict) or not {"name", "url"} <= set(op) \
                    or set(op) - {"name", "url", "token"}:
                bad('set must be {"name": ..., "url": ...} (plus an optional "token")')
                return
            name, url = op["name"], op["url"]
            if not isinstance(name, str) or not pc.PLUGIN_NAME_RE.fullmatch(name):
                bad(f"plugin name {name!r} must match {pc.PLUGIN_NAME_RE.pattern}")
                return
            token_given = "token" in op
            token_val = op.get("token")
            if token_given and not (token_val is None or isinstance(token_val, str)):
                bad("token must be a string or null")
                return
            # One /health read decides which contract the address speaks.
            status, health = _get_json(f"{(url or '').strip().rstrip('/')}/health", timeout=5)
            contract = (health or {}).get("contract") if isinstance(health, dict) else None
            hplugin = (health or {}).get("plugin") if isinstance(health, dict) else None
            hname = hplugin.get("name") if isinstance(hplugin, dict) else hplugin
            if hname == pc.AI_PLUGIN_NAME or name == pc.AI_PLUGIN_NAME:
                # monkeytester-ai is added through the same Add row, but it is
                # stored the AI way (config.json visual_plugin_url +
                # visual_plugin, the op's token becomes visual_api_key) and
                # validated by ai_validate_address — never in `plugins`.
                if managed_ai_plugin():
                    self.send_json_status(409, {"error": MANAGED_AI_MESSAGE})
                    return
                if not isinstance(url, str) or not url.strip():
                    bad("url must be an http(s):// address", "bad_url")
                    return
                with _CONFIG_LOCK:
                    status, body = _ai_plugin_address_op(
                        url, token_val if isinstance(token_val, str) else None)
                if status != 200:
                    self.send_json_status(status, body)
                    return
                self.send_json({"ok": True, "ai": True,
                                "plugin": {"name": pc.AI_PLUGIN_NAME,
                                           "url": body["plugin_url"],
                                           "token": body["token"]}})
                return
            if contract == bpc.CONTRACT:
                with _CONFIG_LOCK:
                    config = load_config()
                    base = base_and_save(config)
                    saved_token = auth.plugin_token(name)
                    if not token_given or token_val == "":
                        use = saved_token
                    elif token_val is None:
                        use = ""
                    else:
                        use = str(token_val).strip()
                    try:
                        url, body = bpc.validate(name, url, token=use or None,
                                                 timeout=builds.HEALTH_TIMEOUT + 2)
                    except bpc.BuildPluginError as e:
                        bad(e.message, e.code)
                        return
                    # The pairing token lives in service_tokens.json (0600),
                    # never in config.json.
                    base[name] = {"url": url, "token": ""}
                    config["plugins"] = base
                    save_config(config)
                    if use != saved_token:
                        auth.set_service_token(f"plugin:{name}", use)
                self.send_json({"ok": True,
                                "plugin": {"name": name, "url": url,
                                           "ok": bool(body.get("ok")),
                                           "contract_version": body.get("contract_version"),
                                           "plugin": body.get("plugin"),
                                           "forge": body.get("forge"),
                                           "token": builds.token_info(use)},
                                "plugins": safe_plugins(base)})
                return
            saved_token = auth.plugin_token(name)
            if not token_given or token_val == "":
                token = saved_token
            elif token_val is None:
                token = ""
            else:
                token = str(token_val).strip()
            try:
                url, body = pc.validate_device_plugin(name, url, token=token or None)
                if token and token != saved_token:
                    # The token must open an authenticated route, not just /health.
                    pc.PluginClient(name, url, token=token, timeout=5).devices(timeout=5)
            except pc.PluginError as e:
                if e.status == 401:
                    bad("the plugin rejected the token (GET /devices: 401)", "unauthorized")
                else:
                    bad(e.message, e.code)
                return
            with _CONFIG_LOCK:
                config = load_config()
                base = base_and_save(config)
                base[name] = url
                config["plugins"] = base
                save_config(config)
            if token != saved_token:
                auth.set_service_token(f"plugin:{name}", token)
            self.send_json({"ok": True,
                            "plugin": {"name": name, "url": url,
                                       "ok": bool(body.get("ok")),
                                       "contract_version": body.get("contract_version"),
                                       "plugin": body.get("plugin"),
                                       "token": auth.token_info({"token": token})},
                            "plugins": safe_plugins(base)})
            return
        bad('expected exactly one of {"set": ...} / {"remove": ...}')

    def handle_delete_runs(self):
        """POST /api/runs/delete — body: {"runs": ["run_1", "run_2", ...]}.

        Deletes the named run directories.
        """
        data = self._read_json_body()
        if data is None:
            return
        runs = data.get("runs")
        if not isinstance(runs, list) or not runs:
            self.send_json_status(400, {"error": "Expected a non-empty 'runs' array"})
            return
        result = delete_runs(runs)
        self.send_json(result)

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_text(self, text):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def send_file_bytes(self, body, content_type, filename=None):
        """200 with a raw body; filename makes the browser save it."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

    def get_system_status(self):
        """Whether each component this viewer depends on is usable."""
        controller = controller_status()
        vision = vision_status()
        return {"ok": controller["ok"] and vision["ok"], "service": SERVICE_NAME,
                "api_version": API_VERSION, "controller": controller, "vision": vision,
                "plugins": plugins_status()}

    def get_visual_model_config(self):
        """Return the saved visual model config. default_endpoint is what's used
        when nothing is saved, such as the model the launcher app runs itself.

        The new provider stores no endpoint or model: it names the AI plugin
        (visual_plugin) whose Settings address (visual_plugin_url) is the
        endpoint. The pairing token is reported read-only as {configured,
        last4}. Legacy endpoint/model keys are still echoed for old configs."""
        cfg = load_config()
        plugin = visual_plugin_name(cfg)
        legacy = legacy_copilot_selected(cfg)
        return {
            "provider": "local",
            "legacy_copilot": legacy,
            "legacy_hint": COPILOT_MOVED_MESSAGE if legacy else "",
            "plugin": plugin,
            "plugin_url": ai_plugin_url(cfg),
            "managed": bool(managed_ai_plugin()),
            "token": token_info(ai_pairing_token(cfg)),
            "endpoint": cfg.get("visual_endpoint", ""),
            "model_name": cfg.get("visual_model", ""),
            "has_api_key": bool(ai_pairing_token(cfg)),
            "default_endpoint": (os.environ.get("VISION_MODEL_API_URL") or "").strip(),
        }

    def get_llama_loaded_model(self):
        """Query llama-server for the currently loaded model alias."""
        import urllib.request
        try:
            req = urllib.request.Request("http://127.0.0.1:8080/v1/models")
            with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
                data = json.loads(resp.read())
            for m in data.get("data", []):
                if m.get("status", {}).get("value") == "loaded":
                    return {"loaded_alias": m.get("id", ""), "loaded_model": m.get("meta", {}).get("n_params", 0)}
            return {"loaded_alias": "", "loaded_model": 0}
        except Exception:
            return {"loaded_alias": "", "loaded_model": 0}

    def save_visual_model_config(self):
        """Save the vision configuration. The AI plugin owns it, so most of a
        save is forwarded to the plugin's POST /config.

        Address shape: {plugin_url, api_key?} sets or removes the
        monkeytester-ai plugin's address (see _save_ai_plugin_address).
        Token shape {set_token}: first-time "Set pairing token" — stores only
        the viewer's copy (the service already holds it), no plugin call.

        New shape (the Device Plugins → monkeytester-ai → Configure panel):
        {provider?, model?, copilot_model?, remote_url?, remote_model?,
        remote_resize?, remote_api_key?, plugin?, generate_token?,
        clear_token?}. provider ("local" | "remote" | "copilot") picks which
        provider the plugin uses; model is its local Ollama model,
        copilot_model its GitHub Copilot model, remote_* the remote
        OpenAI-compatible server (the remote_* keys go in one POST, the API
        key is stored by the plugin and never comes back). Changes are POSTed
        to the plugin in the order model, copilot_model, remote_*, provider —
        so a provider switch comes last and the plugin refuses Copilot while
        signed out or Remote without an address and model (its 400 and
        message reach the page). config.json records only visual_plugin,
        visual_plugin_url and the pairing token copy (visual_api_key):
        generate_token asks the plugin to rotate it — the plugin returns it
        once and the viewer never echoes it; clear_token removes only the
        viewer's copy. The viewer stores no GitHub token.

        The old local shape ({endpoint, model_name, api_key, clear_api_key})
        still works for old setups and logs a deprecation.
        """
        data = self._read_json_body()
        if data is None:
            return
        if managed_ai_plugin() and _changes_managed_ai_plugin(data):
            # The launcher app supplies the address and token on every launch:
            # changing or removing them here would go stale (or break the
            # running plugin's pairing), so refuse instead of storing.
            self.send_json_status(409, {"error": MANAGED_AI_MESSAGE})
            return
        with _CONFIG_LOCK:
            if "plugin_url" in data:
                self._save_ai_plugin_address(data)
            elif "set_token" in data:
                self._set_ai_pairing_token(data)
            else:
                self._save_visual_model_config(data)

    def _set_ai_pairing_token(self, data):
        """POST /api/config/visual-model {set_token}: the Configure panel's
        first-time "Set pairing token". The AI service already holds the token
        (it printed it when installed); the viewer only pairs by keeping its
        own copy in config.json (visual_api_key) — no plugin call. Replacing a
        set token is generate_token (rotation via the plugin) and removing the
        copy is clear_token, both on the existing save path."""
        value = data.get("set_token")
        if not isinstance(value, str) or not value.strip():
            self.send_json_status(400, {"error": "set_token must be a non-empty string",
                                        "code": "invalid_request"})
            return
        cfg = load_config()
        cfg["visual_api_key"] = value.strip()
        save_config(cfg)
        print("pairing token set (viewer copy only, no plugin call)",
              file=sys.stderr, flush=True)
        self.send_json({"saved": True,
                        "plugin": (cfg.get("visual_plugin") or "").strip(),
                        "plugin_url": ai_plugin_url(cfg),
                        "token": token_info(cfg.get("visual_api_key") or "")})

    def _save_ai_plugin_address(self, data):
        """POST /api/config/visual-model {plugin_url, api_key?}: set the
        monkeytester-ai plugin's address (Settings → Device Plugins).
        Validate first — GET <url>/health must speak the plugin contract and
        name plugin monkeytester-ai, and GET <url>/config with the pairing
        token (api_key if given, else the stored one) must answer 200 — and
        store nothing when that fails. "plugin_url": "" removes the plugin
        (visual_plugin_url and visual_plugin)."""
        url = data.get("plugin_url")
        if not isinstance(url, str):
            self.send_json_status(400, {"error": "plugin_url must be a string",
                                        "code": "invalid_request"})
            return
        status, body = _ai_plugin_address_op(url, data.get("api_key"))
        if status == 200:
            self.send_json(body)
        else:
            self.send_json_status(status, body)

    def _save_visual_model_config(self, data):
        provider = (data.get("provider") or "").strip().lower() if "provider" in data else None
        if provider is not None and provider not in ("local", "remote", "copilot"):
            self.send_json_status(400, {"error": "provider must be local, remote or copilot",
                                        "code": "invalid_request"})
            return
        cfg = load_config()
        was_legacy_copilot = legacy_copilot_selected(cfg)
        plugin_name = (data.get("plugin") or "").strip()
        legacy_shape = any(k in data for k in ("endpoint", "model_name", "api_key", "clear_api_key"))
        remote_keys = _plugin_client().REMOTE_KEYS
        new_shape = plugin_name or any(k in data for k in (
            "model", "copilot_model", "generate_token", "clear_token", *remote_keys)) or (
            provider is not None and not legacy_shape)
        plugin_config = None
        if legacy_shape and provider in ("copilot", "remote"):
            self.send_json_status(400, {"error": COPILOT_MOVED_MESSAGE, "code": "invalid_request"})
            return
        if new_shape and not legacy_shape:
            pc = _plugin_client()
            if plugin_name and plugin_name != pc.AI_PLUGIN_NAME:
                self.send_json_status(400, {"error": f"plugin must be {pc.AI_PLUGIN_NAME}",
                                            "code": "invalid_request"})
                return
            try:
                _, client = ai_plugin_client(cfg)
            except pc.PluginError as e:
                self.send_json_status(400, {"error": e.message, "code": e.code})
                return
            name = client.name
            changes = []
            for field in ("model", "copilot_model"):
                if field in data:
                    value = (data.get(field) or "").strip()
                    if not value:
                        self.send_json_status(400, {"error": f"{field} must not be empty",
                                                    "code": "invalid_request"})
                        return
                    changes.append({field: value})
            # The remote server's settings go to the plugin in ONE request, so
            # it can validate remote_url with a remote_api_key sent alongside.
            remote = {}
            for field in remote_keys:
                if field not in data:
                    continue
                value = data[field]
                if field == "remote_resize":
                    if not isinstance(value, bool):
                        self.send_json_status(400, {"error": "remote_resize must be true or false",
                                                    "code": "invalid_request"})
                        return
                elif field == "remote_api_key":
                    if value is not None and not isinstance(value, str):
                        self.send_json_status(400, {"error": "remote_api_key must be a string or null",
                                                    "code": "invalid_request"})
                        return
                    value = value.strip() if isinstance(value, str) else None
                    value = value or None  # "" and null both remove the key
                else:
                    if not isinstance(value, str):
                        self.send_json_status(400, {"error": f"{field} must be a string",
                                                    "code": "invalid_request"})
                        return
                    value = value.strip()
                remote[field] = value
            if remote:
                changes.append(remote)
            if provider is not None:
                changes.append({"provider": provider})
            if data.get("generate_token"):
                changes.append({"generate": True})
            for change in changes:
                try:
                    plugin_config, new_token = pc.ai_save_config(client, change)
                except pc.PluginError as e:
                    # Earlier changes of this save already reached the plugin;
                    # nothing more is written locally.
                    self._send_plugin_error(e)
                    return
                if "generate" in change:
                    if not new_token:
                        self.send_json_status(502, {"error": "the plugin returned no new token",
                                                    "code": "bad_response"})
                        return
                    cfg["visual_api_key"] = new_token
                    client.token = new_token
                    print("pairing token rotated; shown by the plugin once", file=sys.stderr, flush=True)
            cfg["visual_plugin"] = name
            # The viewer always calls the plugin; "local" here means "through
            # the AI plugin" — the plugin itself decides local, remote or Copilot.
            cfg["visual_provider"] = "local"
            if was_legacy_copilot:
                log_deprecation('config.json visual_provider "copilot" was replaced; '
                                "the AI plugin now decides between its local model and Copilot")
            cfg.pop("copilot_model", None)
            for legacy_key in ("visual_endpoint", "visual_model"):
                if cfg.pop(legacy_key, None) is not None:
                    log_deprecation(
                        f"config.json key {legacy_key} was dropped; the monkeytester-ai "
                        "plugin now owns the vision endpoint and models")
            if data.get("clear_token"):
                cfg.pop("visual_api_key", None)
            _PLUGIN_SELECTED_CACHE.pop(name, None)
        else:
            # Old shape: works exactly as before for the local endpoint.
            cfg["visual_provider"] = "local"
            if "endpoint" in data or "model_name" in data:
                log_deprecation(
                    "saving visual_endpoint/visual_model for the local provider is deprecated; "
                    "Settings → Device Plugins now selects a model on the monkeytester-ai plugin")
            if "endpoint" in data:
                cfg["visual_endpoint"] = (data.get("endpoint") or "").strip()
            if "model_name" in data:
                cfg["visual_model"] = (data.get("model_name") or "").strip()
            key_val = (data.get("api_key") or "").strip()
            if key_val:
                cfg["visual_api_key"] = key_val
            elif data.get("clear_api_key"):
                cfg.pop("visual_api_key", None)
        save_config(cfg)
        plugin = visual_plugin_name(cfg)
        out = {
            "provider": plugin_config["provider"] if plugin_config else "local",
            "plugin": plugin,
            "plugin_url": plugin_base_url(plugin),
            "model_name": plugin_selected(plugin) if plugin else cfg.get("visual_model", ""),
            "endpoint": cfg.get("visual_endpoint", ""),
            "token": token_info(ai_pairing_token(cfg)),
            "has_api_key": bool(ai_pairing_token(cfg)),
            "saved": True,
        }
        if plugin_config:
            out["config"] = plugin_config
        self.send_json(out)

    def test_visual_model(self):
        """Proxy a test chat completions request to the configured visual model endpoint."""
        import urllib.request
        import urllib.error

        body = self._body()
        try:
            request = json.loads(body or b"{}")
        except ValueError:
            self.send_json_status(400, {"error": "Invalid JSON"})
            return
        if not isinstance(request, dict):
            self.send_json_status(400, {"error": "Invalid JSON: expected an object"})
            return
        # The AI plugin routes the request to its active provider (local or Copilot).
        endpoint, api_key, default_model = vision_settings()

        if not endpoint:
            self.send_json({"error": "No endpoint configured"})
            return

        try:
            data = json.loads(body)
            # Fill in model from config if not in request
            model = data.get("model") or default_model
            if not model:
                self.send_json({"error": "No model name provided"})
                return
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = "Bearer " + api_key
            req = urllib.request.Request(
                sec.check_http_url(endpoint),
                data=body,
                headers=headers,
                method="POST",
            )
            # Long enough for a model that has to load into memory first.
            with urllib.request.urlopen(req, timeout=120) as resp:  # nosec B310
                resp_data = json.loads(resp.read())
                # Strip streaming tokens for cleaner display
                if isinstance(resp_data, dict):
                    resp_data = {k: v for k, v in resp_data.items() if k not in ("usage", "system_fingerprint")}
                self.send_json({"ok": True, "status": resp.status, "response": resp_data})
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            self.send_json({"ok": False, "status": e.code, "error": err_body})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def get_run_status(self):
        """Return summary of active runs, queued tests, and run-level details."""
        idx_pat = re.compile(r"^run_(\d+)$")
        active_runs = 0
        active_tests = 0
        queued_tests = 0
        details = []

        for d in TEST_DIR.iterdir():
            if not d.is_dir():
                continue
            name = d.name
            m = idx_pat.match(name)
            if not m:
                continue

            meta = get_run_metadata(name)
            running = _is_running(name)

            if not running:
                continue

            active_runs += 1
            run_active = 0
            run_queued = 0
            run_tests = []
            run_phase = ""

            pf = d / "pending_tests.json"
            if pf.exists():
                try:
                    pt = json.loads(pf.read_text())
                    progress = pt.get("progress", {})
                    run_phase = progress.get("phase", "")
                    for t in pt.get("tests", []):
                        tstatus = t.get("status", "pending")
                        run_tests.append({
                            "name": t.get("name", ""),
                            "status": tstatus,
                        })
                        if tstatus == "running":
                            run_active += 1
                        elif tstatus == "pending":
                            run_queued += 1
                except Exception:
                    pass

            active_tests += run_active
            queued_tests += run_queued

            details.append({
                "run": name,
                "is_running": running,
                "active_steps": run_active,
                "queued_tests": run_queued,
                "phase": run_phase,
                "tests": run_tests,
            })

        return {
            "active_runs": active_runs,
            "active_tests": active_tests,
            "queued_tests": queued_tests,
            "details": details,
        }

    # ─── Shared CSS & JS template string for all pages ───

SHARED_CSS = r"""
/* Palette shared with the MonkeyTester website: ink, bone and orange.
   Dark is the default; light (Settings → Configuration) is the bone-paper version.
   --accent is the text-safe accent; --acid is the fill colour, with --on-acid
   as the text drawn on it. PASS keeps its own green so it never reads as
   "accent". */
:root {
    --bg: #060606;
    --surface: #0e0e0e;
    --card: #161616;
    --card-hover: #1f1f1f;
    --accent: #ff6a00;
    --accent-dim: #2a1400;
    --acid: #ff6a00;
    --on-acid: #060606;
    --text: #efece4;
    --text-muted: #a5a298;
    --border: #2e2e2e;
    --border-strong: #efece4;
    --shadow: #efece4;
    --grid: rgba(239,236,228,0.035);
    --pass: #3fb950;
    --pass-bg: rgba(63,185,80,0.15);
    --fail: #ff1f4f;
    --fail-bg: rgba(255,31,79,0.15);
    --skip: #a5a298;
    --skip-bg: rgba(165,162,152,0.15);
    --running: #60a5fa;
    --warn: #ffd400;
    --on-status: #060606;
    --pop: #ff6a00;
    --brand-2: #ff1f4f;
}
/* Light theme (opt-in via Settings → Light Mode) */
:root[data-theme="light"] {
    --bg: #efece4;
    --surface: #f7f5ef;
    --card: #e4e0d5;
    --card-hover: #d9d4c7;
    --accent: #9a3d00;
    --accent-dim: #ffd9bf;
    --acid: #ff6a00;
    --on-acid: #0b0b0b;
    --text: #0b0b0b;
    --text-muted: #5c5950;
    --border: #c9c4b6;
    --border-strong: #0b0b0b;
    --shadow: #0b0b0b;
    --grid: rgba(11,11,11,0.045);
    --pass: #136c2e;
    --pass-bg: rgba(19,108,46,0.12);
    --fail: #b3083a;
    --fail-bg: rgba(179,8,58,0.1);
    --skip: #5c5950;
    --skip-bg: rgba(92,89,80,0.15);
    --running: #1d5fd6;
    --warn: #7a5e00;
    --on-status: #ffffff;
    --pop: #ff6a00;
    --brand-2: #b3083a;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); }
.container { max-width: 1600px; margin: 0 auto; padding: 20px 24px; }
.site-header { padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 16px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
h1 { font-size: 1.3rem; font-weight: 600; background: linear-gradient(135deg, var(--accent), var(--brand-2)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.site-brand { flex-shrink: 0; }
.site-brand a { color: inherit; text-decoration: none; }
:root { --nav-width: 220px; }
:root[data-nav-collapsed="true"] { --nav-width: 60px; }
body:has(#site-nav) { padding-left: var(--nav-width); }
.site-nav { position: fixed; inset: 0 auto 0 0; z-index: 100; width: var(--nav-width); display: flex; flex-direction: column; gap: 4px; padding: 12px 10px; background: var(--surface); border-right: 1px solid var(--border); }
.site-nav-spacer { flex: 1; }
.site-nav-link { display: flex; align-items: center; gap: 12px; width: 100%; min-height: 40px; padding: 8px 10px; background: none; border: 0; border-radius: 6px; color: var(--text-muted); font: inherit; font-size: 0.85rem; text-align: left; text-decoration: none; white-space: nowrap; cursor: pointer; transition: background 0.15s, color 0.15s; }
.site-nav-link:hover { background: var(--card); color: var(--text); }
.site-nav-link:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.site-nav-link[aria-current="page"], .site-nav-link.has-current { background: var(--card); color: var(--accent); font-weight: 500; }
.site-nav-icon { width: 20px; height: 20px; flex-shrink: 0; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.site-nav-label { overflow: hidden; text-overflow: ellipsis; }
:root[data-nav-collapsed="true"] .site-nav > * > .site-nav-label,
:root[data-nav-collapsed="true"] .site-nav-settings > a > .site-nav-label { display: none; }
.site-nav-settings { position: relative; }
/* Submenu: a flyout to the right of the sidebar on hover or keyboard focus. */
.site-nav-submenu { display: none; flex-direction: column; gap: 2px; position: absolute; left: calc(100% + 10px); bottom: 0; min-width: 190px; padding: 6px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 6px 20px rgba(0,0,0,0.35); }
.site-nav-settings:hover .site-nav-submenu, .site-nav-settings:focus-within .site-nav-submenu { display: flex; }
/* Touch screens can't hover, so keep it open on its own pages to reach Device Config. */
@media (hover: none) { .site-nav-settings.in-section .site-nav-submenu { display: flex; } }
/* Invisible bridge so the pointer can cross the gap without closing the flyout. */
.site-nav-submenu::before { content: ''; position: absolute; top: 0; bottom: 0; left: -12px; width: 12px; }
/* Selecting an item collapses the flyout (data-submenu-dismissed is set by
   the nav JS on click and persists across pages until the pointer re-enters
   the Settings block or Settings itself is clicked again). */
:root[data-submenu-dismissed="true"] .site-nav-settings .site-nav-submenu { display: none; }
/* In-view back button (MT_NATIVE_BACK pages only): a floating arrow just
   right of the menu column, at the top-left of the page content itself.
   On the home page there is nothing to go back to, so it renders greyed
   out and disabled instead of being hidden. */
.mt-back-btn { position: fixed; top: 12px; left: calc(var(--nav-width) + 14px); z-index: 101; width: 36px; height: 36px; border-radius: 50%; display: flex; align-items: center; justify-content: center; background: var(--surface); border: 1px solid var(--border); box-shadow: 0 4px 12px rgba(0,0,0,0.3); color: var(--text-muted); cursor: pointer; transition: background 0.15s, color 0.15s; }
.mt-back-btn:hover { background: var(--card); color: var(--text); }
.mt-back-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.mt-back-btn svg { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.mt-back-btn[disabled] { opacity: 0.45; cursor: default; box-shadow: none; }
.mt-back-btn[disabled]:hover { background: var(--surface); color: var(--text-muted); }
body:has(#mt-back-btn) .site-brand { margin-left: 50px; }
.header-actions { margin-left: auto; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.page-title { margin-bottom: 16px; font-size: 1.1rem; font-weight: 600; }
.breadcrumb { font-size: 0.85rem; margin: 0 0 16px; }
.breadcrumb span { cursor: pointer; color: var(--accent); }
.breadcrumb span:hover { text-decoration: underline; }
.breadcrumb .sep { color: var(--text-muted); margin: 0 6px; }
@media (max-width: 700px) {
    /* Small screens always get the icon rail, so content keeps its width. */
    :root { --nav-width: 60px; }
    .site-nav > * > .site-nav-label, .site-nav-settings > a > .site-nav-label { display: none; }
    #site-nav-toggle { display: none; }
    .header-actions { margin-left: 0; }
}

/* Run list (index.html) */
.run-list { display: flex; flex-direction: column; gap: 6px; }
.run-item { background: var(--surface); padding: 12px 16px; border-radius: 8px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; border: 1px solid var(--border); transition: border-color 0.15s; }
.run-item:hover { border-color: var(--accent); }
.run-info { display: flex; flex-direction: column; gap: 2px; }
.run-id { font-weight: 600; font-size: 0.95rem; }
.run-date { font-size: 0.75rem; color: var(--text-muted); white-space: nowrap; }
.run-sub { display: flex; align-items: center; gap: 8px; }
.run-branch { font-size: 0.75rem; color: var(--accent); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: nowrap; }
.run-badge { flex-shrink: 0; }

/* Metadata & stats */
.run-meta-bar { background: var(--surface); padding: 10px 16px; border-radius: 8px; margin-bottom: 12px; display: flex; gap: 20px; flex-wrap: wrap; border: 1px solid var(--border); align-items: center; }
.run-meta-item { font-size: 0.8rem; color: var(--text-muted); }
.run-meta-item b { color: var(--accent); }

/* Shared secondary button (header actions, logs, etc.) */
.btn-secondary { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; cursor: pointer; }
.btn-secondary:hover { background: var(--card); }

.stats-bar { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.stat-item { background: var(--surface); padding: 6px 14px; border-radius: 6px; font-size: 0.85rem; border: 1px solid var(--border); }
.stat-item .count { font-weight: 700; }
.stat-item.pass .count { color: var(--pass); }
.stat-item.fail .count { color: var(--fail); }
.stat-item.total .count { color: var(--accent); }

/* Test panel layout */
.tests-panel { min-width: 0; }
.run-summary { grid-column: 1 / -1; }

/* Logs page */
.logs-full { margin-top: 8px; }
.logs-content {
    background: var(--surface); color: var(--text); font-family: var(--mono);
    font-size: 0.72rem; padding: 12px; border-radius: 8px;
    max-height: 80vh; overflow-y: auto; border: 1px solid var(--border);
    margin-top: 8px; width: 100%; white-space: pre-wrap; word-break: break-all;
}

.tests-main { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; min-height: 60vh; }

/* Video panel (inside tests-main, right column) */
.video-panel { align-self: start; position: sticky; top: 20px; }
.video-section { margin-top: 16px; display: block; }
.video-container { background: #000; border-radius: 10px; overflow: hidden; border: 1px solid var(--border); display: block; width: 100%; }
/* Standard 16:8 video box: every recording renders at one fixed height and
   width = 2 x height; the black background letterboxes sources of any aspect. */
.video-container video { width: 100%; aspect-ratio: 16 / 8; height: auto; display: block; background: #000; }

.test-body-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.test-content { min-width: 0; }

/* Test cases list */
.tests-list { display: flex; flex-direction: column; gap: 12px; }
.test-case { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: visible; }
.test-case-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; background: var(--card); border-bottom: 1px solid var(--border); }
.test-case-name { font-weight: 600; font-size: 0.95rem; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.5px; }
.badge.pass { background: var(--pass-bg); color: var(--pass); border: 1px solid color-mix(in srgb, var(--pass) 30%, transparent); }
.badge.fail { background: var(--fail-bg); color: var(--fail); border: 1px solid color-mix(in srgb, var(--fail) 30%, transparent); }
.badge.skip { background: var(--skip-bg); color: var(--skip); border: 1px solid color-mix(in srgb, var(--skip) 30%, transparent); }
.badge.running {
    background: color-mix(in srgb, var(--running) 20%, transparent);
    color: var(--running);
    border: 1px solid color-mix(in srgb, var(--running) 50%, transparent);
    animation: pulse-running 1.5s ease-in-out infinite;
}
@keyframes pulse-running {
    0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--running) 30%, transparent); }
    50% { box-shadow: 0 0 0 4px color-mix(in srgb, var(--running) 10%, transparent); }
}
.badge.error { background: var(--fail-bg); color: var(--fail); border: 1px solid color-mix(in srgb, var(--fail) 30%, transparent); }
.badge.unknown { background: color-mix(in srgb, var(--skip) 15%, transparent); color: var(--text-muted); border: 1px solid var(--border); }
.badge.cancelled { background: color-mix(in srgb, var(--skip) 15%, transparent); color: var(--text-muted); border: 1px dashed var(--border); }
.badge.unavailable { background: color-mix(in srgb, var(--warn) 15%, transparent); color: var(--warn); border: 1px dashed color-mix(in srgb, var(--warn) 50%, transparent); }
.setup-log-btn { display: inline-block; margin-top: 8px; padding: 4px 10px; border: 1px solid var(--border); border-radius: 6px; color: var(--accent); text-decoration: none; font-size: 0.85em; }
.setup-log-btn:hover { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.badge.recording { background: color-mix(in srgb, var(--warn) 15%, transparent); color: var(--warn); border: 1px solid color-mix(in srgb, var(--warn) 40%, transparent); }
.snapshot-recording { color: var(--warn); font-weight: 600; }
.snapshot-bundle-btn { margin-left: auto; text-decoration: none; display: inline-block; vertical-align: middle; }
.snapshot-result { margin-top: 6px; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.snapshot-result.match { border-color: color-mix(in srgb, var(--pass) 40%, transparent); }
.snapshot-result.mismatch, .snapshot-result.missing, .snapshot-result.error { border-color: color-mix(in srgb, var(--fail) 50%, transparent); }
.snapshot-result.recorded { border-color: color-mix(in srgb, var(--warn) 50%, transparent); }
.snapshot-head { display: flex; gap: 8px; align-items: baseline; font-size: 0.8rem; }
.snapshot-status { font-weight: 600; }
.snapshot-result.match .snapshot-status { color: var(--pass); }
.snapshot-result.mismatch .snapshot-status, .snapshot-result.missing .snapshot-status, .snapshot-result.error .snapshot-status { color: var(--fail); }
.snapshot-result.recorded .snapshot-status { color: var(--warn); }
.snapshot-class { font-family: monospace; color: var(--text-muted); }
.snapshot-message { font-size: 0.75rem; color: var(--text-muted); margin-top: 2px; word-break: break-word; }
.snapshot-panes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-top: 6px; }
.snapshot-pane { margin: 0; }
.snapshot-pane figcaption { font-size: 0.7rem; color: var(--text-muted); margin-bottom: 2px; }
.snapshot-pane img { width: 100%; border-radius: 4px; border: 1px solid var(--border); }
.snapshot-none { font-size: 0.7rem; color: var(--text-muted); padding: 12px 0; text-align: center; border: 1px dashed var(--border); border-radius: 4px; }
.step-layout { font-size: 0.75rem; margin-top: 2px; font-family: monospace; }
.step-layout.ok { color: var(--pass); }
.step-layout.bad { color: var(--fail); }

.test-case-body { padding: 14px 16px; }

/* Steps */
.steps-section { margin-bottom: 12px; }
.steps-section h3 { font-size: 0.75rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
.steps-list { display: flex; flex-direction: column; gap: 2px; }
.step-row { display: flex; align-items: flex-start; gap: 10px; padding: 6px 8px; border-radius: 4px; transition: background 0.1s; }
.step-row:hover { background: var(--card); }
.step-status { width: 8px; height: 8px; border-radius: 50%; margin-top: 5px; flex-shrink: 0; }
.step-status.pass { background: var(--pass); }
.step-status.fail { background: var(--fail); }
.step-status.skipped { background: var(--skip); }
.step-content { flex: 1; min-width: 0; }
.step-command { font-weight: 600; color: var(--accent); font-size: 0.8rem; }
.step-args { color: var(--text); font-size: 0.8rem; margin-top: 1px; }
.step-block { color: var(--text); font-size: 0.75rem; margin: 2px 0 0 0; padding: 4px 8px; background: var(--card); border-left: 2px solid var(--border); white-space: pre-wrap; word-break: break-word; }
.step-description { color: var(--text); font-size: 0.8rem; margin-top: 2px; }
.step-reason { color: var(--fail); font-style: italic; font-size: 0.75rem; margin-top: 2px; }

/* Test file view (Show test file) */
.source-details { margin-top: 12px; }
.source-details summary { cursor: pointer; color: var(--accent); font-size: 0.8rem; font-weight: 500; }
.source-label { margin-top: 8px; color: var(--text-muted); font-size: 0.72rem; font-weight: 600; }
.source-view { background: var(--bg); padding: 12px; border-radius: 6px; font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; font-size: 0.72rem; max-height: 300px; overflow: auto; margin-top: 4px; color: var(--text); border: 1px solid var(--border); line-height: 1.5; white-space: pre-wrap; }

/* Loading */
.loading { text-align: center; padding: 30px; color: var(--text-muted); font-size: 0.9rem; }

/* DL button */
.dl-btn { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 0.75rem; margin-top: 8px; display: inline-flex; align-items: center; gap: 4px; text-decoration: none; transition: all 0.15s; }
.dl-btn:hover { border-color: var(--accent); color: var(--accent); }

/* Summary (logs.html) */
.summary { background: var(--surface); padding: 16px; border-radius: 8px; margin-bottom: 20px; white-space: pre-wrap; font-family: monospace; font-size: 0.82rem; word-break: break-word; border: 1px solid var(--border); }

/* Scrollbar */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }



# ── Live stream page ──
.nav a { color: var(--text-muted); text-decoration: none; padding: 4px 12px; border-radius: 6px; font-size: 0.85rem; transition: all 0.15s; }
.nav a:hover, .nav a.active { color: var(--accent); background: var(--card); }

.stream-layout { display: grid; grid-template-columns: 280px 1fr; gap: 16px; }
.stream-controls { display: flex; flex-direction: column; gap: 12px; }
.stream-controls label { font-size: 0.8rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
.stream-controls select { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; font-size: 0.9rem; width: 100%; cursor: pointer; }
.stream-controls select:focus { outline: none; border-color: var(--accent); }
.device-info { font-size: 0.75rem; color: var(--text-muted); padding: 8px 0; border-top: 1px solid var(--border); }
.stream-status { display: flex; align-items: center; gap: 8px; font-size: 0.85rem; padding: 8px 0; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--text-muted); }
.status-dot.connected { background: var(--pass); box-shadow: 0 0 6px color-mix(in srgb, var(--pass) 40%, transparent); }
.status-dot.disconnected { background: var(--fail); }

.stream-viewport { display: flex; flex-direction: column; }
.stream-player { position: relative; background: #000; border-radius: 10px; overflow: hidden; border: 1px solid var(--border); width: 100%; aspect-ratio: 16 / 9; display: flex; align-items: center; justify-content: center; }
.stream-player video { width: 100%; height: auto; display: block; }
.stream-overlay { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; background: rgba(0,0,0,0.7); color: var(--text-muted); font-size: 0.9rem; }
.overlay-spinner { width: 32px; height: 32px; border: 3px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.stream-device-port { display: flex; flex-direction: column; gap: 4px; padding: 8px 0; border-top: 1px solid var(--border); }
.stream-device-port span { font-size: 0.8rem; color: var(--text); font-family: monospace; }
.stream-zoom-controls { margin-top: 10px; align-self: center; display: flex; align-items: center; gap: 8px; background: var(--surface); border: 1px solid var(--border); border-radius: 20px; padding: 4px 10px; }
.stream-zoom-controls button { background: var(--surface); border: 1px solid var(--border); color: var(--text); width: 24px; height: 24px; border-radius: 50%; cursor: pointer; font-size: 0.9rem; line-height: 1; display: flex; align-items: center; justify-content: center; padding: 0; }
.stream-zoom-controls button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.stream-zoom-controls button:disabled { opacity: 0.4; cursor: default; }
.stream-zoom-controls span { font-size: 0.75rem; color: var(--text-muted); min-width: 36px; text-align: center; }

/* ── Brutalist layer: the MonkeyTester website's look ──
   Fonts are served by the viewer from fonts/ (no external requests).
   Corners are square everywhere; only spinners and the press-overlay
   markers stay round. Raised things get hard offset shadows, never blur:
   --pop is the orange accent in both themes. */
@font-face { font-family: "Anton"; src: url(/fonts/anton.woff2) format("woff2"); font-display: swap; }
@font-face { font-family: "Space Grotesk"; src: url(/fonts/space-grotesk.woff2) format("woff2"); font-weight: 300 700; font-display: swap; }
@font-face { font-family: "JetBrains Mono"; src: url(/fonts/jetbrains-mono.woff2) format("woff2"); font-weight: 100 800; font-display: swap; }
:root {
    --display: "Anton", Impact, "Arial Narrow", sans-serif;
    --sans: "Space Grotesk", system-ui, -apple-system, "Segoe UI", sans-serif;
    --mono: "JetBrains Mono", ui-monospace, Menlo, monospace;
}
body {
    font-family: var(--sans);
    background-image: linear-gradient(var(--grid) 1px, transparent 1px), linear-gradient(90deg, var(--grid) 1px, transparent 1px);
    background-size: 80px 80px;
}
::selection { background: var(--acid); color: var(--on-acid); }
*, *::before, *::after, ::-webkit-scrollbar-thumb { border-radius: 0 !important; }
.overlay-spinner, .live-stream-overlay .spinner, .press-tap, .press-tap::after,
.press-swipe::before, .press-swipe::after { border-radius: 50% !important; }
input, select, textarea, button { font-family: inherit; }
::-webkit-scrollbar-thumb { background: var(--text-muted); }
::-webkit-scrollbar-thumb:hover { background: var(--text); }
code, pre, kbd, samp, .logs-content, .source-view, .summary, .snapshot-class, .step-layout,
.run-branch, .stream-device-port span, .step-block, .step-command { font-family: var(--mono); }

/* Type */
h1.site-brand { display: flex; align-items: center; gap: 10px; font-family: var(--display); font-weight: 400; font-size: 1.55rem; letter-spacing: 0.04em; text-transform: uppercase; background: none; -webkit-text-fill-color: currentColor; color: var(--text); }
h1.site-brand::before { content: ""; width: 14px; height: 14px; flex-shrink: 0; background: var(--acid); box-shadow: 3px 3px 0 var(--shadow); }
.page-title, .settings-section h3 { font-family: var(--display); font-weight: 400; letter-spacing: 0.03em; text-transform: uppercase; }
.page-title { font-size: 1.7rem; }
.settings-section h3 { font-size: 1.25rem; }
.run-id, .test-case-name { font-weight: 700; }
.site-nav-link, .badge, .btn-secondary, .dl-btn, .setup-log-btn, .steps-section h3, .stream-controls label,
.settings-field label, .source-label, .snapshot-pane figcaption, .source-details summary {
    font-family: var(--mono); text-transform: uppercase; letter-spacing: 0.08em;
}
.site-nav-link { font-size: 0.72rem; }
.badge { font-size: 0.64rem; padding: 3px 8px; }
/* Status badges are solid blocks, not tints, so they read at a glance. */
.badge.pass { background: var(--pass); border-color: var(--pass); color: var(--on-status); }
.badge.fail, .badge.error { background: var(--fail); border-color: var(--fail); color: var(--on-status); }
.badge.running { background: var(--running); border-color: var(--running); color: var(--on-status); }
.badge.unavailable, .badge.recording { background: var(--warn); border-color: var(--warn); color: var(--on-status); }
.badge.pending { background: transparent; border: 1px solid var(--warn); color: var(--warn); }
.badge.skip, .badge.unknown, .badge.cancelled { background: transparent; color: var(--text-muted); border: 1px dashed var(--text-muted); }
@keyframes pulse-running { 0%, 100% { box-shadow: 0 0 0 0 var(--running); } 50% { box-shadow: 3px 3px 0 0 var(--running); } }
input[type="checkbox"], input[type="radio"] { accent-color: var(--acid); }
.dashboard-title { font-family: var(--display); font-weight: 400; font-size: 1.7rem; letter-spacing: 0.03em; text-transform: uppercase; }
.chart-card { border-width: 2px; }
.chart-card h3, .legend-item { font-family: var(--mono); text-transform: uppercase; letter-spacing: 0.08em; }

/* Navigation */
.site-header { border-bottom: 2px solid var(--border-strong); }
.site-nav { border-right: 2px solid var(--border-strong); }
.site-nav-link[aria-current="page"], .site-nav-link.has-current { background: var(--acid); color: var(--on-acid); font-weight: 700; }
.site-nav-link:focus-visible { outline-color: var(--acid); }
.site-nav-submenu { border: 2px solid var(--border-strong); box-shadow: 6px 6px 0 var(--pop); }
.mt-back-btn { border: 2px solid var(--border-strong); box-shadow: 3px 3px 0 var(--pop); }
.mt-back-btn[disabled] { box-shadow: none; }

/* Surfaces */
.run-item, .test-case, .run-meta-bar, .stat-item, .summary, .logs-content, .source-view { border-width: 2px; }
.run-item { transition: border-color 0.12s, transform 0.12s, box-shadow 0.12s; }
.run-item:hover { border-color: var(--border-strong); box-shadow: 5px 5px 0 var(--pop); transform: translate(-2px, -2px); }
.test-case { box-shadow: 6px 6px 0 var(--border); }
.test-case-header { border-bottom-width: 2px; }
.video-container, .stream-player { border: 2px solid var(--border-strong); box-shadow: 6px 6px 0 var(--pop); }
.stat-item.total .count { color: var(--text); }
.run-meta-item b { color: var(--accent); }

/* Buttons: secondary = ink outline that inverts; primary = acid fill + hard shadow */
.btn-secondary, .dl-btn, .setup-log-btn { background: transparent; border: 2px solid var(--border-strong); color: var(--text); font-weight: 700; transition: background 0.12s, color 0.12s; }
.btn-secondary:hover, .dl-btn:hover, .setup-log-btn:hover { background: var(--text); border-color: var(--text); color: var(--bg); }
.btn-run, .settings-save-btn, .save-btn, .config-save-all button {
    font-family: var(--mono); font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
    box-shadow: 4px 4px 0 var(--shadow); transition: transform 0.12s, box-shadow 0.12s;
}
.btn-run:hover:not(:disabled), .settings-save-btn:hover:not(:disabled), .save-btn:hover:not(:disabled),
.config-save-all button:hover:not(:disabled) { transform: translate(-2px, -2px); box-shadow: 6px 6px 0 var(--shadow); }
.btn-run:disabled, .settings-save-btn:disabled, .save-btn:disabled, .config-save-all button:disabled { box-shadow: none; transform: none; }

/* Inputs */
.settings-field input, .stream-controls select { border-width: 2px; }
.settings-field input:focus, .stream-controls select:focus { border-color: var(--acid); box-shadow: 3px 3px 0 var(--pop); }

@media (prefers-reduced-motion: reduce) {
    .run-item, .btn-run, .settings-save-btn, .save-btn, .config-save-all button { transition: none; }
    .run-item:hover, .btn-run:hover:not(:disabled), .settings-save-btn:hover:not(:disabled),
    .save-btn:hover:not(:disabled), .config-save-all button:hover:not(:disabled) { transform: none; }
}
.mt-warning-banner { position: sticky; top: 0; z-index: 200; margin: 0 0 12px; padding: 10px 16px; background: color-mix(in srgb, var(--fail) 15%, var(--surface)); border: 1px solid var(--fail); color: var(--text); font-size: 0.85rem; }
.mt-warning-banner a { color: var(--fail); font-weight: 600; }
"""

# Shared by the settings pages (config, plugins, run-status). Each page adds only its own section's CSS on top.
SETTINGS_CSS = r"""
.settings-section { margin-bottom: 28px; }
.settings-section h3 { font-size: 0.95rem; font-weight: 600; margin-bottom: 12px; }
.settings-field { margin-bottom: 16px; }
.settings-field label { display: block; font-size: 0.8rem; color: var(--text-muted); margin-bottom: 4px; }
.settings-field input {
    width: 100%;
    max-width: 480px;
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 0.9rem;
}
.settings-field input:focus { outline: none; border-color: var(--accent); }
.settings-field input::placeholder { color: var(--text-muted); opacity: 0.6; }
.settings-field .hint { font-size: 0.75rem; color: var(--text-muted); margin-top: 4px; }
.settings-status { font-size: 0.75rem; min-height: 16px; margin-bottom: 12px; }
.settings-status.saved { color: var(--pass); }
.settings-status.error { color: var(--fail); }
.settings-actions { display: flex; gap: 10px; align-items: center; margin-bottom: 20px; }
.settings-actions button { padding: 8px 16px; }
"""

SHARED_JS = r"""
const currentSort = 'newest';
let videoSize = '100%';
let authConfig = { signed_in: false, username: '', role: '', native_back: false, web_runs_disabled: false };

// ── Who is signed in + viewer flags (from /api/auth/status) ──
function fetchAuthStatus() {
    return fetchJSON('/api/auth/status').then(function(cfg) {
        authConfig = cfg;
        renderDefaultPasswordBanner();
        return cfg;
    }).catch(function() {
        authConfig = {};
        return authConfig;
    });
}

// The session ended (logout elsewhere, password reset, user deleted).
function goToLogin() {
    const here = window.location.pathname + window.location.search;
    window.location.href = '/login' + (here && here !== '/' ? '?next=' + encodeURIComponent(here) : '');
}

function doLogout() {
    fetch('/api/logout', { method: 'POST' }).finally(function() {
        window.location.href = '/login';
    });
}

// Admins only, while the seeded admin/admin password is still in use.
function renderDefaultPasswordBanner() {
    let el = document.getElementById('mt-default-password-banner');
    if (!authConfig.default_password) {
        if (el) el.remove();
        return;
    }
    if (el) return;
    el = document.createElement('div');
    el.id = 'mt-default-password-banner';
    el.className = 'mt-warning-banner';
    el.setAttribute('role', 'alert');
    el.innerHTML = 'This admin account still uses the default password <code>admin</code>. ' +
        'Anyone on the network can sign in with it. <a href="/config#accountSection">Change it now</a>.';
    document.body.insertBefore(el, document.body.firstChild);
}

// ── Theme (dark by default; light mode is opt-in from Settings) ──
function applyTheme(theme) {
    if (theme === 'light') {
        document.documentElement.setAttribute('data-theme', 'light');
    } else {
        document.documentElement.removeAttribute('data-theme');
    }
}
// Apply instantly from localStorage (the head snippet already applied it
// pre-paint; this keeps it in sync within JS)
try { applyTheme(localStorage.getItem('mt-theme')); } catch (e) {}
// Sync with the server-side setting (authoritative)
Promise.resolve().then(() => fetchJSON('/api/config')).then(cfg => {
    applyTheme(cfg.theme);
}).catch(() => {});
function setTheme(theme) {
    theme = theme === 'light' ? 'light' : 'dark';
    applyTheme(theme);
    try { localStorage.setItem('mt-theme', theme); } catch (e) {}
    fetch('/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({theme: theme})
    }).catch(() => {});
}

// ── Shared navigation (left sidebar) ──
const NAV_ICONS = {
    dashboard: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    devices: '<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>',
    upload: '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 20h16"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
    configuration: '<path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h8M16 18h4"/><circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="14" cy="18" r="2"/>',
    collapse: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    back: '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>',
    build: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/>',
    plugins: '<rect x="3" y="9" width="8" height="8" rx="1"/><rect x="13" y="5" width="8" height="8" rx="1"/><path d="M11 11h4"/>',
    models: '<path d="M12 2 20 7 20 17 12 22 4 17 4 7Z"/><path d="M4 7 12 12 20 7M12 12v10"/>',
    status: '<path d="M3 12h4l3-6 4 12 3-6h5"/>',
    users: '<circle cx="9" cy="8" r="3"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14.2c2.8.4 5 2.8 5 5.8"/>',
};
const NAV_LINKS = [
    { href: '/dashboard', label: 'Dashboard', icon: 'dashboard', paths: ['/', '/dashboard', '/dashboard.html', '/test.html', '/test-b.html', '/logs.html', '/player.html', '/player'] },
    { href: '/stream.html', label: 'Preview Devices', icon: 'devices', paths: ['/stream.html', '/stream'] },
    { href: '/build.html', label: 'Build & Test', icon: 'build', paths: ['/build.html', '/build'], startsRun: true },
];
// The Settings sidebar opens its first entry.
const NAV_SETTINGS_LINKS = [
    { href: '/config', label: 'Configuration', icon: 'configuration', paths: ['/config', '/config.html'] },
    { href: '/plugins', label: 'Plugins', icon: 'plugins', paths: ['/plugins', '/plugins.html'] },
    { href: '/run-status', label: 'Run Status', icon: 'status', paths: ['/run-status', '/run-status.html'] },
    // Admins only (the server answers 403 to everyone else).
    { href: '/users', label: 'Users', icon: 'users', paths: ['/users', '/users.html'], adminOnly: true },
];

function navIcon(name) {
    return '<svg class="site-nav-icon" viewBox="0 0 24 24" aria-hidden="true">' + NAV_ICONS[name] + '</svg>';
}
function navLabel(label) {
    return '<span class="site-nav-label">' + escapeHtml(label) + '</span>';
}
function navLink(link, path) {
    return '<a class="site-nav-link"' + (link.paths.includes(path) ? ' aria-current="page"' : '') +
        ' href="' + link.href + '" title="' + escapeHtml(link.label) + '" aria-label="' + escapeHtml(link.label) + '">' +
        navIcon(link.icon) + navLabel(link.label) + '</a>';
}

function isNavCollapsed() {
    try { return localStorage.getItem('mt-nav-collapsed') === 'true'; } catch (e) { return false; }
}
function applyNavCollapsed(collapsed) {
    if (collapsed) document.documentElement.setAttribute('data-nav-collapsed', 'true');
    else document.documentElement.removeAttribute('data-nav-collapsed');
}
// Apply as early as possible so the page doesn't shift once the nav renders.
applyNavCollapsed(isNavCollapsed());

// The Settings flyout collapses once one of its items is clicked: the flag
// lives in localStorage so it survives the navigation to the settings page,
// and it is cleared when the pointer re-enters the Settings block (mouseenter),
// when keyboard focus enters it (focusin + :focus-visible), or when the
// Settings entry itself is clicked again.
function isSubmenuDismissed() {
    try { return localStorage.getItem('mt-submenu-dismissed') === 'true'; } catch (e) { return false; }
}
function setSubmenuDismissed(dismissed) {
    try { localStorage.setItem('mt-submenu-dismissed', dismissed ? 'true' : 'false'); } catch (e) {}
    if (dismissed) document.documentElement.setAttribute('data-submenu-dismissed', 'true');
    else document.documentElement.removeAttribute('data-submenu-dismissed');
}
// Apply on load so the flyout is already closed on the page we navigated to.
setSubmenuDismissed(isSubmenuDismissed());

function toggleNavCollapsed() {
    const collapsed = !isNavCollapsed();
    try { localStorage.setItem('mt-nav-collapsed', String(collapsed)); } catch (e) {}
    applyNavCollapsed(collapsed);
    renderNav();
}

// Flyout dismiss listeners are attached to the nav element only once per page
// (renderNav runs twice: the basic pass and the post-auth-status pass).
let navWired = false;

function renderNav() {
    const navEl = document.getElementById('site-nav');
    if (!navEl) return;
    const path = window.location.pathname;
    const collapsed = isNavCollapsed();
    const toggleLabel = collapsed ? 'Show labels' : 'Hide labels';
    const inSettings = NAV_SETTINGS_LINKS.some(l => l.paths.includes(path));
    // No back entry here: native-back pages get the floating in-view back
    // arrow instead (renderBackButton, below).
    navEl.innerHTML =
        NAV_LINKS.filter(l => !(l.startsRun && authConfig.web_runs_disabled))
            .map(l => navLink(l, path)).join('') +
        '<div class="site-nav-spacer"></div>' +
        '<button type="button" class="site-nav-link" id="site-nav-toggle" onclick="toggleNavCollapsed()" title="' + toggleLabel + '" aria-label="' + toggleLabel + '" aria-pressed="' + collapsed + '">' +
            navIcon('collapse') + navLabel(toggleLabel) + '</button>' +
        '<div class="site-nav-settings' + (inSettings ? ' in-section' : '') + '">' +
            '<a class="site-nav-link' + (inSettings ? ' has-current' : '') + '" href="' + NAV_SETTINGS_LINKS[0].href + '" title="Settings" aria-label="Settings">' +
                navIcon('settings') + navLabel('Settings') + '</a>' +
            '<div class="site-nav-submenu">' +
                NAV_SETTINGS_LINKS.filter(l => !l.adminOnly || authConfig.role === 'admin')
                    .map(l => navLink(l, path)).join('') +
            '</div>' +
        '</div>';
    // The click handler lives on the persistent nav element (attached once,
    // renderNav runs twice per page); the mouseenter listener must be re-attached
    // after every render because innerHTML replaces the settings div itself.
    if (!navWired) {
        navWired = true;
        navEl.addEventListener('click', (event) => {
            if (event.target.closest('.site-nav-submenu')) {
                // The user picked an item: collapse the flyout, on this page
                // and across the navigation to the settings page.
                setSubmenuDismissed(true);
            } else if (event.target.closest('.site-nav-settings > a')) {
                // The Settings entry itself was clicked: reopen for the next pick.
                setSubmenuDismissed(false);
            }
        });
    }
    const settingsEl = navEl.querySelector('.site-nav-settings');
    settingsEl.addEventListener('mouseenter', () => setSubmenuDismissed(false));
    // Keyboard: Enter on a link IS a click, so a keyboard pick also dismisses.
    // Tabbing back into the block (keyboard focus = :focus-visible) reopens it,
    // otherwise the override would hide it from keyboard users for good.
    settingsEl.addEventListener('focusin', (event) => {
        if (event.target.matches(':focus-visible')) setSubmenuDismissed(false);
    });
    if (typeof onNavReady === 'function') onNavReady();
}

// ── In-view back button (MT_NATIVE_BACK only) ──
// A floating arrow just right of the menu column, at the top-left of the
// page content itself — NOT inside the nav. The element is persistent
// (created once per page, reused by the second renderNav pass once the
// native_back flag is known). On the home page there is nothing to go
// back to, so the arrow renders greyed out and disabled rather than
// hidden, and its click handler stays inert.
function renderBackButton() {
    const path = window.location.pathname;
    if (!authConfig.native_back) {
        const stale = document.getElementById('mt-back-btn');
        if (stale) stale.remove();
        return;
    }
    const onHome = path === '/' || path === '/dashboard' || path === '/dashboard.html';
    let btn = document.getElementById('mt-back-btn');
    if (!btn) {
        btn = document.createElement('button');
        btn.id = 'mt-back-btn';
        btn.className = 'mt-back-btn';
        btn.type = 'button';
        btn.setAttribute('title', 'Back');
        btn.setAttribute('aria-label', 'Back');
        document.body.appendChild(btn);
    }
    btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + NAV_ICONS.back + '</svg>';
    if (onHome) btn.setAttribute('disabled', '');
    else btn.removeAttribute('disabled');
    btn.__vtEnabled = !onHome;
    if (!btn.__vtWired) {
        btn.__vtWired = true;
        btn.addEventListener('click', function() { if (btn.__vtEnabled) history.back(); });
    }
}

// Auto-detect current page and mark the matching nav link as active.
// renderNav() runs immediately for the basic nav; it runs again once
// fetchAuthStatus() resolves so the native_back flag is known (the back
// button needs it too, so renderBackButton() runs in both passes).
document.addEventListener('DOMContentLoaded', () => {
    renderNav();
    renderBackButton();
    fetchAuthStatus().then(() => { renderNav(); renderBackButton(); });
});

// Load saved config. Wrapped in Promise.resolve().then(...) so that even a
// *synchronous* throw from fetchJSON/fetch (not just a rejected request)
// becomes a rejection here instead of an uncaught exception that would abort
// the rest of this script — including each page's own DOMContentLoaded logic.
Promise.resolve().then(() => fetchJSON('/api/config')).then(cfg => {
    if (cfg.video_size) {
        videoSize = cfg.video_size;
        applyVideoSize(videoSize);
    }
}).catch(() => {});

function applyVideoSize(size) {
    videoSize = size;
    document.querySelectorAll('.video-size-btn').forEach(btn => {
        btn.classList.toggle('active', btn.textContent === size);
    });
    const vp = document.querySelector('.video-panel');
    if (!vp) return;
    const vw = size.replace('%', 'vw');
    vp.style.width = vw;
    vp.style.maxWidth = vw;
    vp.querySelectorAll('.video-container').forEach(container => {
        container.style.width = '100%';
        container.style.maxWidth = '100%';
    });
}

function setVideoSize(size) {
    applyVideoSize(size);
    fetch('/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({video_size: size})
    }).catch(() => {});
}

function fetchJSON(url) {
    return fetch(url).then(r => {
        if (r.status === 401) { goToLogin(); throw new Error('Signed out'); }
        return r.json();
    });
}
// Calls onChange with {devices, error} now and whenever the controller's list changes.
function watchDevices(onChange) {
  const source = new EventSource('/api/devices/events');
  source.addEventListener('devices', e => onChange(JSON.parse(e.data)));
  source.onerror = () => onChange({devices: [], error: 'Lost the connection to the viewer.'});
}
function fetchText(url) { return fetch(url).then(r => r.text()); }
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
    return (bytes/1048576).toFixed(1) + ' MB';
}
"""


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8089
    print(f"Serving MonkeyTester viewer on http://localhost:{port}")
    print(f"Reading from: {TEST_DIR}")
    # MT_VIEWER_BIND picks the listen address (default every interface).
    # One-time: move a monkeytester-ai MT_PLUGINS entry into config.json.
    import_ai_plugin_address()
    import_device_plugins()
    import_controller_url()
    # One-time: gitea_config.json / MT_BUILD_PLUGINS -> build_config.json.
    builds.VIEWER_PORT = port
    builds.migrate(log_ignored=True)
    # Accounts: seed admin/admin when there are none, migrate the old
    # single-user file, warn while the default password is in use.
    auth.startup_check()
    # One-time: the old token environment variables -> service_tokens.json,
    # then the environment mirrors Settings (the runner inherits it).
    import_service_tokens()
    auth.apply_to_environ()
    # LAN service by design (MT_VIEWER_BIND narrows it); see SECURITY.md.
    bind = os.environ.get("MT_VIEWER_BIND") or "0.0.0.0"  # nosec B104
    server = ThreadingHTTPServer((bind, port), TestViewerHandler)
    server.serve_forever()
