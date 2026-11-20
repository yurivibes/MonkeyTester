"""Build & Test: MonkeyTester -> build plugin -> MonkeyTester.

The user picks a project (a repo on one build plugin), a branch, the
platforms and the test cases. The viewer asks that project's build plugin
(BUILD_PLUGIN_API.md) to build the branch: build-local fetches it from the code
host (Gitea or GitHub), runs the app repo's per-platform build scripts on its
own host and uploads the apps with the viewer's GET /ci/submit.py and the
build's request id, so the resulting run attaches to the build here.

The viewer never builds anything and never talks to a code host: the plugin
holds the code host URL and token. It starts a build only when a person asks
(never in reaction to an upload), so the two directions can't loop.

Build plugin registrations live in the viewer's Run/config.json `plugins` map,
in the same Settings → Plugins registry as the device plugins: a build entry is
{name: {"url", "token"}} (an object marks a build plugin; a device plugin is
the plain url string). `token` is the plugin's optional pairing token
(plaintext at rest, never sent to a browser). build_config.json next to app.py
(mode 0600, gitignored) keeps the projects only:
    {"projects": [{"repo", "plugin", "branch"}]}
A repo is in at most one project: run history is keyed by repo.

Standard library only.
"""
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import build_plugin_client as bpc

CONFIG_FILE = Path(__file__).parent / "build_config.json"
# The viewer's Run/config.json: the shared plugin registry (app.py repoints
# this at its own CONFIG_FILE). Build plugins are the object entries
# {name: {"url", "token"}} in its `plugins` map; device plugins are the
# plain url strings and live here only as registrations, not as state.
VIEWER_CONFIG_FILE = Path(__file__).parent / "Run" / "config.json"
# Settings of the viewer's old built-in Gitea client. Read once to migrate its
# repos; its URL + token are handed to the build-local plugin's /config once,
# then the file is renamed to gitea_config.json.migrated without the token.
LEGACY_GITEA_FILE = Path(__file__).parent / "gitea_config.json"
MIGRATED_PLUGIN = "build-local"
DEFAULT_BUILD_LOCAL_URL = "http://127.0.0.1:8094"
LEGACY_PUSH_RETRY = 300  # seconds between attempts to hand the token over
VIEWER_PORT = 8089       # app.py sets the port it listens on
CASES_DIR = "visual-tests"
PLATFORMS = ("android", "ios")

# owner/repo; neither part may be only dots (".." is a path, not a repo).
REPO_RE = re.compile(r"^(?!\.+/)[A-Za-z0-9_.-]+/(?!\.+$)[A-Za-z0-9_.-]+$")
BUILD_ID_RE = re.compile(r"^build_[0-9a-f]{12}$")
CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
# Git refs: no spaces, "..", "//", leading "/", trailing "/" or ".", or the
# other characters git forbids.
BRANCH_RE = re.compile(r"^(?!/)(?!.*\.\.)(?!.*//)[^\s~^:?*\[\\\x00-\x1f\x7f]{1,250}(?<![/.])$")
PLUGIN_NAME_RE = bpc.PLUGIN_NAME_RE

# A build the plugin keeps reporting as `pending` (accepted, never queued) is
# given up on after this.
PENDING_TIMEOUT = 180
REFRESH_INTERVAL = 4.0
HEALTH_TIMEOUT = 3
TERMINAL_STATES = {"passed", "failed", "build_failed", "dispatch_failed", "cancelled"}
CANCELLABLE_STATES = {"queued", "building"}
LOG_CHUNK = 256 * 1024  # most bytes of build log one /log call relays

_LOCK = threading.RLock()


class BuildError(Exception):
    def __init__(self, message, status=None, code=None, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra or {}


def _relay(e, prefix=""):
    """A BuildPluginError as a BuildError with the status the viewer answers:
    the plugin's 400/404/409 stay, everything else (unreachable, a rejected
    pairing token, forge trouble) is 502."""
    status = e.status if e.status in (400, 404, 409) else 502
    if e.code == "forge_refused" and e.status == 400:
        status = 400
    extra = {"plugin": e.plugin, "plugin_status": e.status}
    if e.forge_status is not None:
        extra["forge_status"] = e.forge_status
    return BuildError(f"{prefix}{e.message}", status, e.code, extra)


# ─── Private files ───

def write_private_json(path, data):
    """Write JSON readable only by this user (0600), atomically."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def tighten_mode(path):
    """Make an existing secrets file 0600 if it is readable by others."""
    try:
        if Path(path).exists() and Path(path).stat().st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass


# ─── Settings ───

def _empty():
    return {"plugins": {}, "projects": []}


def _clean(stored):
    """A stored build_config.json reduced to its known, well-formed parts."""
    cfg = _empty()
    if not isinstance(stored, dict):
        return cfg
    plugins = stored.get("plugins") if isinstance(stored.get("plugins"), dict) else {}
    for name, entry in plugins.items():
        if isinstance(name, str) and PLUGIN_NAME_RE.fullmatch(name) and isinstance(entry, dict) \
                and isinstance(entry.get("url"), str) and entry["url"]:
            cfg["plugins"][name] = {"url": entry["url"].rstrip("/"),
                                    "token": entry.get("token") if isinstance(entry.get("token"), str) else ""}
    seen = set()
    projects = stored.get("projects") if isinstance(stored.get("projects"), list) else []
    for p in projects:
        if not isinstance(p, dict):
            continue
        repo = str(p.get("repo") or "").strip()
        if not REPO_RE.match(repo) or repo in seen:
            continue
        seen.add(repo)
        branch = p.get("branch") if isinstance(p.get("branch"), str) else ""
        cfg["projects"].append({"repo": repo, "plugin": str(p.get("plugin") or ""), "branch": branch})
    return cfg


def _read_legacy_gitea():
    try:
        with open(LEGACY_GITEA_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def build_local_url(env=None):
    env = os.environ if env is None else env
    try:
        return bpc.normalize_url(env.get("VT_BUILD_LOCAL_URL") or DEFAULT_BUILD_LOCAL_URL)
    except bpc.BuildPluginError:
        print(f"VT_BUILD_LOCAL_URL is not an address, using {DEFAULT_BUILD_LOCAL_URL}",
              file=sys.stderr, flush=True)
        return DEFAULT_BUILD_LOCAL_URL


def migrate(env=None, log_ignored=False):
    """One-time setup of build_config.json, run at viewer start and on the
    first load: only while build_config.json doesn't exist yet.

    - gitea_config.json (the old built-in Gitea client) becomes the plugin
      build-local at VT_BUILD_LOCAL_URL (default http://127.0.0.1:8094, no
      pairing token) registered in the shared Settings → Plugins registry
      (config.json), and one project per saved repo with its branch pick.
      Its URL + token stay in the file until push_legacy_forge() has handed
      them to that plugin's /config.
    - VT_BUILD_PLUGINS=name=url[,name=url] is imported the same way
      VT_PLUGINS is for device plugins: once, into the same registry, then
      Settings owns the list.

    Nothing is written when there is nothing to import. Returns the new
    config, or None."""
    env = os.environ if env is None else env
    try:
        entries = bpc.parse_build_plugins(env.get("VT_BUILD_PLUGINS", ""))
    except bpc.BuildPluginError as e:
        print(f"VT_BUILD_PLUGINS is malformed, build plugins not imported: {e.message}",
              file=sys.stderr, flush=True)
        entries = []
    with _LOCK:
        if Path(CONFIG_FILE).exists():
            if entries and log_ignored:
                print("VT_BUILD_PLUGINS is ignored — Settings → Plugins owns the build plugin list now",
                      file=sys.stderr, flush=True)
            return None
        legacy = _read_legacy_gitea()
        cfg = _empty()
        url = build_local_url(env)
        if legacy is not None:
            cfg["plugins"][MIGRATED_PLUGIN] = {"url": url, "token": ""}
            picks = legacy.get("branches") if isinstance(legacy.get("branches"), dict) else {}
            for repo in legacy.get("repos") or []:
                repo = str(repo).strip().strip("/")
                if REPO_RE.match(repo) and all(p["repo"] != repo for p in cfg["projects"]):
                    branch = picks.get(repo) if isinstance(picks.get(repo), str) else ""
                    cfg["projects"].append({"repo": repo, "plugin": MIGRATED_PLUGIN, "branch": branch})
        for name, plugin_url in entries:
            cfg["plugins"][name] = {"url": plugin_url, "token": ""}
        if legacy is None and not entries:
            return None
        write_private_json(CONFIG_FILE, {"plugins": {}, "projects": cfg["projects"]})
    merge_registry(cfg["plugins"])
    if legacy is not None:
        print(f"migrated gitea_config.json: plugin {MIGRATED_PLUGIN} registered in Settings → Plugins "
              f"at {url}, {len(cfg['projects'])} project(s); its Gitea URL and token go to the "
              "plugin's /config the first time it answers unconfigured", file=sys.stderr, flush=True)
    if entries:
        print(f"imported {len(entries)} build plugin(s) from VT_BUILD_PLUGINS into Settings → Plugins; "
              "Settings owns the list from now on", file=sys.stderr, flush=True)
    return cfg


_legacy_push = {"at": 0.0}


def viewer_url_for(plugin_url, env=None):
    """The address a plugin should upload to: VT_PUBLIC_URL, else
    http://<this host's IP towards the plugin>:<viewer port>."""
    env = os.environ if env is None else env
    public = (env.get("VT_PUBLIC_URL") or "").strip().rstrip("/")
    if public:
        return public
    host = "127.0.0.1"
    try:
        parsed = urlparse(plugin_url)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((parsed.hostname or "127.0.0.1", parsed.port or 80))  # no packet is sent
            host = sock.getsockname()[0]
    except OSError:
        pass
    return f"http://{host}:{VIEWER_PORT}"


def retire_legacy_gitea(legacy):
    """Rename gitea_config.json to gitea_config.json.migrated without its
    token, so the viewer no longer holds a code host token."""
    kept = {k: v for k, v in legacy.items() if k != "token"}
    target = Path(LEGACY_GITEA_FILE).with_name(Path(LEGACY_GITEA_FILE).name + ".migrated")
    write_private_json(target, kept)
    try:
        os.unlink(LEGACY_GITEA_FILE)
    except OSError:
        pass
    return target


def push_legacy_forge(cfg, name, health, env=None):
    """Hand the old built-in Gitea client's URL + token to the build-local
    plugin that migrate() created, the first time it answers /health with
    forge.configured false: POST /config {forge_kind: "gitea", forge_url,
    token, viewer_url}. On success gitea_config.json is retired (renamed,
    token removed). A plugin that already has a code host token keeps it,
    and the file is retired as well. Tried at most every LEGACY_PUSH_RETRY s.
    Returns "pushed", "retired" or None."""
    if name != MIGRATED_PLUGIN or not health.get("ok"):
        return None
    with _LOCK:
        legacy = _read_legacy_gitea()
        if not legacy or not str(legacy.get("token") or "").strip():
            return None
        forge = health.get("forge") or {}
        if forge.get("configured"):
            retire_legacy_gitea(legacy)
            print(f"build plugin {name} already has a code host token: gitea_config.json retired "
                  "without handing its token over", file=sys.stderr, flush=True)
            return "retired"
        if _now() - _legacy_push["at"] < LEGACY_PUSH_RETRY:
            return None
        _legacy_push["at"] = _now()
    entry = cfg["plugins"].get(name) or {}
    payload = {"forge_kind": "gitea", "forge_url": str(legacy.get("url") or "").strip(),
               "token": str(legacy["token"]).strip(), "viewer_url": viewer_url_for(entry.get("url") or "", env)}
    try:
        plugin_client(cfg, name, timeout=30).save_config(payload)
    except (bpc.BuildPluginError, BuildError) as e:
        print(f"handing the Gitea token to build plugin {name} failed, retrying later: {e}",
              file=sys.stderr, flush=True)
        return None
    with _LOCK:
        if _read_legacy_gitea():
            retire_legacy_gitea(legacy)
    print(f"handed the Gitea URL and token to build plugin {name}; gitea_config.json renamed to "
          "gitea_config.json.migrated without the token", file=sys.stderr, flush=True)
    return "pushed"


def load_config():
    if not Path(CONFIG_FILE).exists():
        migrate()
    try:
        with open(CONFIG_FILE) as f:
            stored = _clean(json.load(f))
    except (OSError, ValueError):
        stored = _empty()
    reg = registry_plugins()
    # One-time move: build plugin entries an older build_config.json still
    # held go into the shared registry, and the file keeps its projects only.
    if stored["plugins"]:
        merge_registry({n: e for n, e in stored["plugins"].items() if n not in reg})
        reg.update(stored["plugins"])
        with _LOCK:
            save_config(stored)
    stored["plugins"] = reg
    return stored


def registry_plugins():
    """The build plugin entries in the viewer's config.json `plugins` map:
    {name: {\"url\", \"token\"}}. Plain url strings are device plugins and
    skipped; malformed entries are ignored."""
    try:
        with open(VIEWER_CONFIG_FILE) as f:
            config = json.load(f)
    except (OSError, ValueError):
        return {}
    raw = config.get("plugins") if isinstance(config, dict) \
        and isinstance(config.get("plugins"), dict) else {}
    out = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not PLUGIN_NAME_RE.fullmatch(name):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("url"), str) and entry["url"]:
            out[name] = {"url": entry["url"].rstrip("/"),
                         "token": entry.get("token") if isinstance(entry.get("token"), str) else ""}
    return out


def merge_registry(entries):
    """Merge build plugin entries into the viewer config.json `plugins` map.
    Writes the file only when something actually changes."""
    if not entries:
        return
    try:
        with open(VIEWER_CONFIG_FILE) as f:
            config = json.load(f)
    except (OSError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    reg = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    changed = False
    for name, entry in entries.items():
        if reg.get(name) != entry:
            reg[name] = entry
            changed = True
    if not changed:
        return
    config["plugins"] = reg
    write_private_json(VIEWER_CONFIG_FILE, config)


def save_config(cfg):
    """build_config.json keeps the projects only — build plugin entries live
    in the viewer registry (config.json `plugins`, object entries)."""
    with _LOCK:
        write_private_json(CONFIG_FILE, {"projects": _clean(cfg)["projects"]})


def token_info(token):
    token = token or ""
    return {"configured": bool(token), "last4": token[-4:] if len(token) >= 8 else ""}


def _env_token(name, env=None):
    env = os.environ if env is None else env
    key = "VT_PLUGIN_TOKEN_" + re.sub(r"[^0-9A-Za-z]", "_", name).upper()
    return (env.get(key) or "").strip() or (env.get("PLUGIN_TOKEN") or "").strip()


# The viewer sets this to its Settings lookup (viewer_auth.plugin_token): the
# pairing tokens live in service_tokens.json. Standalone use falls back to
# the environment.
TOKEN_LOOKUP = None


def _saved_token(name):
    return (TOKEN_LOOKUP(name) if TOKEN_LOOKUP else _env_token(name)) or ""


def plugin_client(cfg, name, timeout=10):
    """BuildPluginClient for a registered plugin, with its pairing token
    from Settings (TOKEN_LOOKUP)."""
    entry = cfg["plugins"].get(name)
    if not entry:
        raise BuildError(f"no build plugin {name!r} registered (Settings → Configuration → "
                         "Build & Test)", 400, "not_found")
    return bpc.BuildPluginClient(name, entry["url"], token=entry.get("token") or _saved_token(name) or None,
                                 timeout=timeout)


def project(cfg, repo):
    for p in cfg["projects"]:
        if p["repo"] == repo:
            return p
    raise BuildError(f"{repo} is not a project (Settings → Configuration → Build & Test)", 400)


def project_branch(cfg, repo):
    """The branch picked for repo in Settings, or "" (= the forge's default)."""
    for p in cfg["projects"]:
        if p["repo"] == repo:
            return p.get("branch") or ""
    return ""


def repos(cfg=None):
    return [p["repo"] for p in (cfg or load_config())["projects"]]


def _parallel(fn, items):
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        return list(pool.map(fn, items))


def plugin_health(cfg, name):
    """health_entry for a registered plugin, plus its address and token info."""
    client = plugin_client(cfg, name, timeout=HEALTH_TIMEOUT)
    entry = bpc.health_entry(client, timeout=HEALTH_TIMEOUT)
    if push_legacy_forge(cfg, name, entry) == "pushed":
        entry = bpc.health_entry(client, timeout=HEALTH_TIMEOUT)
    entry["token"] = token_info(cfg["plugins"][name].get("token") or _saved_token(name))
    return entry


def forge_label(health):
    forge = (health or {}).get("forge") or {}
    return forge.get("label") or forge.get("kind") or (health or {}).get("name") or ""


def recipe_paths(health):
    """{platform: path of its build script in the app repo} from /health."""
    paths = dict((health or {}).get("recipes") or {})
    for platform in PLATFORMS:
        paths.setdefault(platform, f".monkeytester/build-{platform}.sh")
    return paths


def check_project(cfg, p, health=None):
    """The plugin's GET /repos check for one project:
    {repo, plugin, ok, reason, default_branch, branch, recipes, platforms,
    recipe_paths, forge_label}. recipes says which build script the branch
    has (true/false/null per platform); platforms is what the plugin's host
    can build."""
    out = {"repo": p["repo"], "plugin": p["plugin"], "ok": False, "reason": "",
           "default_branch": "", "branch": p.get("branch") or "",
           "recipes": {x: None for x in PLATFORMS}, "platforms": list((health or {}).get("platforms") or []),
           "recipe_paths": recipe_paths(health), "forge_label": forge_label(health)}
    if p["plugin"] not in cfg["plugins"]:
        return {**out, "reason": f"build plugin {p['plugin'] or '(none)'} is not registered"}
    try:
        check = plugin_client(cfg, p["plugin"], timeout=20).check_repo(p["repo"], p.get("branch") or "")
    except bpc.BuildPluginError as e:
        return {**out, "reason": f"{e.message} ({e.code})"}
    return {**out, **check, "repo": p["repo"], "plugin": p["plugin"]}


def settings_with_checks(cfg=None):
    """Everything Settings → Build & Test shows: the plugins with their live
    /health (code host label, configured user, platforms), the projects and a
    live ✓/✗ per project. Never includes a token."""
    cfg = cfg or load_config()
    names = list(cfg["plugins"])
    healths = dict(zip(names, _parallel(lambda n: plugin_health(cfg, n), names)))
    checks = _parallel(lambda p: check_project(cfg, p, healths.get(p["plugin"])), cfg["projects"])
    return {"plugins": [healths[n] for n in names],
            "projects": [dict(p) for p in cfg["projects"]],
            "checks": checks}


def normalize_projects(raw, cfg):
    """[{repo, plugin, branch}] from a Settings save. Raises BuildError(400)."""
    if not isinstance(raw, list):
        raise BuildError("projects must be a list of {repo, plugin, branch}", 400)
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise BuildError("projects must be a list of {repo, plugin, branch}", 400)
        repo = str(item.get("repo") or "").strip().strip("/")
        plugin = str(item.get("plugin") or "").strip()
        branch = str(item.get("branch") or "").strip()
        if not REPO_RE.match(repo):
            raise BuildError(f"{repo!r} is not owner/repo", 400)
        if repo in seen:
            raise BuildError(f"{repo} is listed twice (a repo belongs to one project)", 400)
        seen.add(repo)
        if plugin not in cfg["plugins"]:
            raise BuildError(f"{repo}: build plugin {plugin or '(none)'} is not registered", 400)
        if branch and (len(branch) > 255 or not BRANCH_RE.match(branch)):
            raise BuildError(f"{branch!r} is not a valid branch name for {repo}", 400)
        out.append({"repo": repo, "plugin": plugin, "branch": branch})
    return out


def save_projects(data):
    """Settings save: {projects: [{repo, plugin, branch}]}. Each project is
    checked with its plugin first (the token can read the repo, the branch
    exists); any failure is a 400 {error, checks} and nothing is stored. A
    missing build script is reported but doesn't block (the repo is added
    first, the scripts committed after)."""
    with _LOCK:
        cfg = load_config()
        projects = normalize_projects(data.get("projects"), cfg)
    names = sorted({p["plugin"] for p in projects})
    healths = dict(zip(names, _parallel(lambda n: plugin_health(cfg, n), names)))
    checks = _parallel(lambda p: check_project(cfg, p, healths.get(p["plugin"])), projects)
    bad = [c for c in checks if not c["ok"]]
    if bad:
        raise BuildError("; ".join(f"{c['repo']}: {c['reason']}" for c in bad), 400,
                         extra={"checks": checks})
    with _LOCK:
        cfg = load_config()
        cfg["projects"] = projects
        save_config(cfg)
    out = settings_with_checks(cfg)
    out["checks"] = checks
    return out


def plugin_config(name):
    """GET the plugin's /config (the Configure panel), public shape."""
    try:
        return plugin_client(load_config(), name).config()
    except bpc.BuildPluginError as e:
        raise _relay(e) from None


def save_plugin_config(name, data):
    """POST the plugin's /config with {forge_kind?, forge_url?, token?,
    viewer_url?}: the plugin checks the token with the code host (and the
    viewer URL) before storing anything. The code host token passes through
    the viewer and is never stored or returned here."""
    payload = {k: data[k] for k in bpc.CONFIG_KEYS if k in data}
    if not payload:
        raise BuildError("nothing to save: send forge_kind, forge_url, token or viewer_url", 400)
    for key, value in payload.items():
        if value is not None and not isinstance(value, str):
            raise BuildError(f"{key} must be a string", 400)
        if value is None and key != "token":
            raise BuildError(f"{key} must be a string", 400)
    if payload.get("forge_kind") and payload["forge_kind"] not in bpc.FORGE_KINDS:
        raise BuildError(f"forge_kind must be one of {', '.join(bpc.FORGE_KINDS)}", 400)
    try:
        return {"ok": True, "config": plugin_client(load_config(), name, timeout=30).save_config(payload)}
    except bpc.BuildPluginError as e:
        raise _relay(e) from None


def template(name, platform):
    """(build script text, file name) from the plugin's GET /template?platform=."""
    if platform not in PLATFORMS:
        raise BuildError(f"platform must be one of {', '.join(PLATFORMS)}", 400, "bad_request")
    cfg = load_config()
    client = plugin_client(cfg, name)
    try:
        text = client.template(platform)
    except bpc.BuildPluginError as e:
        raise _relay(e) from None
    health = bpc.health_entry(client, timeout=HEALTH_TIMEOUT)
    filename = recipe_paths(health)[platform].rsplit("/", 1)[-1]
    return text, filename or f"build-{platform}.sh"


def _branches(cfg, repo, plugin):
    try:
        data = plugin_client(cfg, plugin, timeout=30).branches(repo)
    except bpc.BuildPluginError as e:
        raise _relay(e, "Listing branches failed: ") from None
    names, default = data["branches"], data["default"]
    picked = project_branch(cfg, repo)
    preferred = picked if picked in names else default
    names.sort(key=lambda n: (n != preferred, n != default, n.lower()))
    return {"repo": repo, "plugin": plugin, "default": default, "preferred": preferred,
            "picked": picked, "branches": names}


def list_branches(repo):
    """Branches of a project for Build & Test. preferred is the project's
    branch (picked in Settings, else the forge's default) and is listed first."""
    cfg = load_config()
    return _branches(cfg, repo, project(cfg, repo)["plugin"])


def settings_branches(repo, plugin=""):
    """Branches for the Settings project table, for a repo that may not be
    saved yet. plugin defaults to the project's plugin."""
    cfg = load_config()
    repo = (repo or "").strip().strip("/")
    if not REPO_RE.match(repo):
        raise BuildError(f"{repo!r} is not owner/repo", 400)
    plugin = (plugin or "").strip() or next((p["plugin"] for p in cfg["projects"] if p["repo"] == repo), "")
    if not plugin:
        raise BuildError("pick the project's build plugin first", 400)
    return _branches(cfg, repo, plugin)


def list_cases(repo, ref):
    """Test cases on that branch (so the list matches the code being built),
    which build scripts the branch has (recipes) and where they belong
    (recipe_paths), and the platforms the plugin's host can build."""
    if not BRANCH_RE.match(ref or ""):
        raise BuildError("invalid branch name", 400)
    cfg = load_config()
    p = project(cfg, repo)
    client = plugin_client(cfg, p["plugin"], timeout=30)
    try:
        data = client.cases(repo, ref)
    except bpc.BuildPluginError as e:
        if e.status == 404:
            raise BuildError(f"Branch {ref} not found in {repo}", 404, e.code) from None
        raise _relay(e, f"Reading {ref} failed: ") from None
    health = bpc.health_entry(client, timeout=HEALTH_TIMEOUT)
    data["cases"] = [c for c in data["cases"] if CASE_RE.match(c)]
    return {**data, "plugin": p["plugin"], "platforms": list(health.get("platforms") or []),
            "recipe_paths": recipe_paths(health), "capabilities": list(health.get("capabilities") or []),
            "forge_label": forge_label(health)}


# ─── Build records ───
# One JSON file per build under <runs dir>/.builds/. The viewer's Run/ is
# already gitignored and is what pr-test.sh copies per instance.

BUILDS_DIR = Path(__file__).parent / "Run" / ".builds"  # app.py points it at TEST_DIR / ".builds"


def _builds_dir():
    d = Path(BUILDS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_path(build_id):
    if not BUILD_ID_RE.match(build_id or ""):
        raise BuildError("unknown build", 404)
    return _builds_dir() / f"{build_id}.json"


def load_build(build_id):
    path = _build_path(build_id)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        raise BuildError("unknown build", 404) from None


def save_build(build):
    with _LOCK:
        write_private_json(_build_path(build["id"]), build)


def _now():
    return time.time()


def ci_fields(build):
    """(ci_state, ci_conclusion, ci_url) of a record; records written before
    build plugins carry gitea_status / gitea_conclusion / gitea_run_url."""
    return (build.get("ci_state") or build.get("gitea_status") or "",
            build.get("ci_conclusion") or build.get("gitea_conclusion") or "",
            build.get("ci_url") or build.get("gitea_run_url") or "")


def is_legacy(build):
    """A record from the built-in Gitea client: no plugin, Gitea fields."""
    return not build.get("plugin") and any(k.startswith("gitea_") for k in build)


def public_build(build):
    """A record as /api/builds* return it: the stored keys plus plugin,
    ci_url, ci_state, ci_conclusion and forge_label filled in for old records."""
    out = dict(build)
    state, conclusion, url = ci_fields(build)
    out.update(plugin=build.get("plugin") or "", ci_state=state, ci_conclusion=conclusion, ci_url=url,
               forge_label=build.get("forge_label") or ("Gitea" if is_legacy(build) else ""))
    out.setdefault("build_ref", None)
    out.setdefault("capabilities", [])
    return out


def create_build(repo, branch, platforms, cases, user=""):
    """Validate, record and start a build through the project's plugin.
    Returns the build record.

    A platform the plugin's host can't build is a 400 before anything is
    recorded. A start the plugin refuses (or a plugin that can't be reached)
    is recorded as dispatch_failed and still returned, so the user sees it in
    the list with the reason."""
    cfg = load_config()
    repo = (repo or "").strip()
    p = project(cfg, repo)
    branch = (branch or "").strip()
    if not BRANCH_RE.match(branch):
        raise BuildError("Pick a branch", 400)
    platforms = [x for x in PLATFORMS if x in set(platforms or [])]
    if not platforms:
        raise BuildError("Pick at least one platform", 400)
    cases = sorted({str(c).strip() for c in cases or [] if str(c).strip()})
    if not cases:
        raise BuildError("Pick at least one test case", 400)
    bad = [c for c in cases if not CASE_RE.match(c)]
    if bad:
        raise BuildError(f"invalid case name: {bad[0]}", 400)
    client = plugin_client(cfg, p["plugin"], timeout=30)
    health = bpc.health_entry(client, timeout=HEALTH_TIMEOUT)
    if health.get("ok") and health.get("platforms"):
        cannot = [x for x in platforms if x not in health["platforms"]]
        if cannot:
            raise BuildError(f"build plugin {p['plugin']} can't build {', '.join(cannot)} on its host "
                             f"(it builds {', '.join(health['platforms'])})", 400, "bad_request")
    label = p["plugin"]

    build = {
        "id": "build_" + uuid.uuid4().hex[:12],
        "repo": repo, "branch": branch, "platforms": platforms, "cases": cases,
        "user": user or "", "created": _now(),
        "state": "dispatching", "error": "",
        "plugin": p["plugin"], "forge_label": forge_label(health), "build_ref": None,
        "capabilities": list(health.get("capabilities") or []),
        "ci_url": "", "ci_state": "", "ci_conclusion": "",
        "jobs": {}, "runs": [], "checked": 0,
    }
    save_build(build)
    try:
        started = client.start_build(repo, branch, platforms, cases, build["id"])
    except bpc.BuildPluginError as e:
        reason = e.message
        if e.code == "bad_request":
            paths = recipe_paths(health)
            reason += " (build scripts: " + ", ".join(paths[x] for x in platforms) + ")"
        build["state"] = "dispatch_failed"
        build["error"] = f"{label} refused the build: {reason}"
    else:
        build["state"] = "queued"
        build["build_ref"] = started["build_ref"]
        build["ci_url"] = started["url"]
        build["ci_state"] = "pending"
    save_build(build)
    return public_build(build)


def attach_run(build_id, run_id, case="", platforms=()):
    """Record a run uploaded by the build's CI job. Unknown ids are ignored."""
    if not BUILD_ID_RE.match(build_id or ""):
        return False
    with _LOCK:
        try:
            build = load_build(build_id)
        except BuildError:
            return False
        build.setdefault("runs", [])
        if not any(r.get("run_id") == run_id for r in build["runs"]):
            build["runs"].append({"run_id": run_id, "case": case, "platforms": list(platforms),
                                  "attached": _now()})
        if build.get("state") in ("dispatching", "queued", "building"):
            build["state"] = "testing"
        save_build(build)
    return True


def job_platform(name):
    name = (name or "").lower()
    for platform in PLATFORMS:
        if name == platform or name.startswith(platform + " ") or name.startswith(platform + "-"):
            return platform
    return None


def map_jobs(jobs, platforms):
    """{platform: job} for a build's jobs, plus "upload" for the Upload job.

    Platforms are matched by name ("Android …" / "iOS …"); a job named
    otherwise (not Upload) covers the platforms left over, for a plugin that
    builds every platform in one job.
    """
    def entry(job):
        return {"name": job.get("name"), "status": job.get("status"),
                "conclusion": job.get("conclusion") or None,
                "url": job.get("url") or job.get("html_url") or ""}
    out, shared = {}, None
    for job in jobs:
        name = (job.get("name") or "").strip().lower()
        platform = job_platform(name)
        if platform:
            out[platform] = entry(job)
        elif name == "upload":
            out["upload"] = entry(job)
        elif shared is None:
            shared = entry(job)
    if shared:
        for platform in platforms:
            out.setdefault(platform, shared)
    return out


def derive_state(build, run_result):
    """Overall state from the plugin's build state and the attached runs.

    Plugin pending/queued -> queued; in_progress -> building, or testing once
    a run is attached; completed: cancelled -> cancelled, nothing attached ->
    build_failed, otherwise the runs decide (passed / failed). Every chosen
    platform failing to build is build_failed even when the plugin uploaded
    the errors as a run.

    run_result(run_id) -> {"status": "running"|"passed"|"failed"|...}.
    """
    if build["state"] in ("dispatching", "dispatch_failed"):
        return build["state"], build.get("error") or ""
    runs = build.get("runs") or []
    jobs = build.get("jobs") or {}
    ci_state, ci_conclusion, _ = ci_fields(build)
    results = [run_result(r["run_id"]) for r in runs]
    for r, res in zip(runs, results):
        r["status"] = res.get("status")
    any_running = any(res.get("status") == "running" for res in results)
    if ci_state != "completed":
        if runs:
            return "testing", ""
        if ci_state in ("in_progress", "running") or \
                any(j.get("status") in ("in_progress", "running") for j in jobs.values()):
            return "building", ""
        return "queued", ""
    if ci_conclusion == "cancelled":
        return "cancelled", "the build was cancelled"
    failed_builds = [p for p in build["platforms"]
                     if (jobs.get(p) or {}).get("conclusion") not in (None, "success", "skipped")]
    if not runs:
        if ci_conclusion == "success":
            return "build_failed", "the build finished without uploading anything to test"
        which = f" ({', '.join(failed_builds)})" if failed_builds else ""
        return "build_failed", f"the build failed{which}; see its log"
    if any_running:
        return "testing", ""
    if failed_builds and len(failed_builds) == len(build["platforms"]):
        return "build_failed", f"the build failed ({', '.join(failed_builds)}); see its log"
    note = f"build failed for {', '.join(failed_builds)}; see its log" if failed_builds else ""
    if not failed_builds and ci_conclusion == "success" and \
            all(res.get("status") == "passed" for res in results):
        return "passed", ""
    return "failed", note


def refresh_build(build, run_result, force=False):
    """Ask the build's plugin about a non-final build (at most every
    REFRESH_INTERVAL s) and work out its state. Records from before build
    plugins (no plugin, Gitea fields) aren't refreshed any more; their
    attached runs still update the state."""
    if build.get("state") in TERMINAL_STATES and not force:
        return public_build(build)
    if not force and _now() - (build.get("checked") or 0) < REFRESH_INTERVAL:
        return public_build(build)
    with _LOCK:
        try:
            build = load_build(build["id"])  # pick up runs attached meanwhile
        except BuildError:
            return public_build(build)
    if build.get("state") in ("dispatching", "dispatch_failed"):
        return public_build(build)
    if is_legacy(build) and build.get("state") in TERMINAL_STATES:
        return public_build(build)  # nothing left to ask about
    if build.get("plugin") and build.get("build_ref"):
        try:
            cfg = load_config()
            status = plugin_client(cfg, build["plugin"]).build(build["build_ref"])
        except BuildError as e:
            build["error"] = str(e)
            return public_build(build)
        except bpc.BuildPluginError as e:
            if e.status == 404 and build.get("state") not in TERMINAL_STATES:
                # The plugin lost the ref (e.g. its state dir was wiped).
                build["state"] = "dispatch_failed"
                build["error"] = f"build plugin {build['plugin']} no longer knows this build"
                save_build(build)
            else:
                build["error"] = f"{e.message} ({e.code})"
            return public_build(build)
        apply_status(build, status)
        if status["state"] == "pending" and _now() - build["created"] > PENDING_TIMEOUT:
            build["state"] = "dispatch_failed"
            build["error"] = (f"build plugin {build['plugin']} accepted the build but never queued it "
                              f"({PENDING_TIMEOUT} s)")
    if build["state"] != "dispatch_failed":
        build["state"], build["error"] = derive_state(build, run_result)
    build["checked"] = _now()
    save_build(build)
    return public_build(build)


def apply_status(build, status):
    """Store a plugin's GET /builds/<ref> answer on the record."""
    build["ci_state"] = status["state"]
    build["ci_conclusion"] = status["conclusion"] or ""
    build["ci_url"] = status["url"] or build.get("ci_url") or ""
    build["jobs"] = map_jobs(status["jobs"], build["platforms"])


def _plugin_build(build_id):
    """(record, client) for a build the plugin knows (has a build_ref)."""
    build = load_build(build_id)
    if not build.get("plugin") or not build.get("build_ref"):
        raise BuildError("this build was never started on a build plugin", 409, "no_build_ref")
    return build, plugin_client(load_config(), build["plugin"], timeout=30)


def build_log(build_id, offset=0):
    """The plugin's build log from byte offset: {text (bytes), size,
    complete}. At most LOG_CHUNK bytes per call; size is the log's total
    size, so a client asks again from offset + len(text)."""
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        raise BuildError("offset must be a number of bytes", 400, "bad_request") from None
    build, client = _plugin_build(build_id)
    try:
        log = client.log(build["build_ref"], offset)
    except bpc.BuildPluginError as e:
        raise _relay(e) from None
    text = log["text"][:LOG_CHUNK]
    complete = log["complete"] and offset + len(text) >= log["size"]
    return {"text": text, "size": log["size"], "complete": complete}


def cancel_build(build_id, run_result):
    """Ask the plugin to stop a queued or building build. The plugin's answer
    is stored and the state worked out again (cancelled once the plugin says
    so). A finished build is left as it is."""
    build, client = _plugin_build(build_id)
    if build.get("state") in TERMINAL_STATES:
        return public_build(build)
    try:
        status = client.cancel(build["build_ref"])
    except bpc.BuildPluginError as e:
        raise _relay(e, "Cancel failed: ") from None
    with _LOCK:
        build = load_build(build_id)
        apply_status(build, status)
        build["state"], build["error"] = derive_state(build, run_result)
        build["checked"] = _now()
        save_build(build)
    return public_build(build)


def list_builds(run_result, limit=30):
    files = sorted(_builds_dir().glob("build_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    builds = []
    for path in files:
        try:
            builds.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    builds.sort(key=lambda b: b.get("created") or 0, reverse=True)
    return [refresh_build(b, run_result) for b in builds[:limit]]
