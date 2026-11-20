#!/usr/bin/env python3
"""
HTTP client for MonkeyTester build plugins (BUILD_PLUGIN_API.md, contract_version 1).

A build plugin is a separate service that turns a branch of an app repo into the
apps Build & Test installs: build-local fetches the code from Gitea or GitHub and
builds it on its own host. The viewer never talks to a code host itself and
never sees its token: it asks the plugin to check a repo, list branches and test
cases, start, follow and cancel a build.

    GET  /health                          -> contract, plugin, forge, platforms, recipes, capabilities
    GET  /config  / POST /config          -> code host kind + URL, write-only token, viewer URL
    GET  /repos/<owner>/<repo>?branch=    -> the Settings check for one project (+ recipes)
    GET  /repos/<owner>/<repo>/branches   -> every branch, default first
    GET  /repos/<owner>/<repo>/cases?ref= -> visual-tests/<case>/ on that branch (+ recipes)
    POST /builds                          -> 201 {build_ref, url}
    GET  /builds/<build_ref>              -> {state, conclusion, url, jobs}
    GET  /builds/<build_ref>/log?offset=  -> text/plain, X-Log-Size, X-Log-Complete (capability logs)
    POST /builds/<build_ref>/cancel       -> the build's state (capability cancel)
    GET  /template?platform=              -> an example build script (text/plain)

Same style as visual-tests/scripts/plugin_client.py (the device plugin client).
Standard library only.
"""
import json
import re
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode

CONTRACT = "monkeytester-build-plugin"
SUPPORTED_CONTRACT_VERSIONS = (1,)

PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
STATES = ("pending", "queued", "in_progress", "completed")
CONCLUSIONS = ("success", "failure", "cancelled", "skipped")
PLATFORMS = ("android", "ios")
FORGE_KINDS = ("gitea", "github")
# POST /config keys the viewer passes on.
CONFIG_KEYS = ("forge_kind", "forge_url", "token", "viewer_url")


class BuildPluginError(Exception):
    """A build plugin call failed. `code` is the contract's error code
    (bad_request, unauthorized, not_configured, not_found, forge_unreachable,
    forge_refused, internal) or one of this client's: unreachable,
    bad_response, contract, config. `forge_status` is the forge's HTTP status
    for forge_refused."""

    def __init__(self, message, code="internal", status=None, plugin=None, forge_status=None):
        self.message = message
        self.code = code
        self.status = status
        self.plugin = plugin
        self.forge_status = forge_status
        prefix = f"build plugin {plugin}: " if plugin else ""
        where = f"HTTP {status}, " if status else ""
        super().__init__(f"{prefix}{message} ({where}{code})")


def _error_from_body(body, status):
    """(message, code, forge_status) from {"error", "code", "forge_status"?}."""
    if not isinstance(body, dict):
        return f"HTTP {status}", "internal", None
    error = body.get("error")
    if isinstance(error, dict):
        return (str(error.get("message") or f"HTTP {status}"), str(error.get("code") or "internal"),
                body.get("forge_status"))
    return str(error or f"HTTP {status}"), str(body.get("code") or "internal"), body.get("forge_status")


def normalize_url(url):
    """'http(s)://host[:port][/path]' without a trailing slash, or raise
    BuildPluginError(code="bad_request")."""
    url = (url or "").strip().rstrip("/")
    if not re.match(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?(/[^\s?#]*)?$", url):
        raise BuildPluginError(f"{url or '(empty)'} is not an address like http://host:port",
                               "bad_request")
    return url


def repo_path(repo):
    """'/repos/<owner>/<repo>' with each part quoted."""
    owner, _, name = (repo or "").partition("/")
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


class BuildPluginClient:
    def __init__(self, name, url, token=None, timeout=10):
        self.name = name
        self.url = url.rstrip("/")
        self.token = token or None
        self.timeout = timeout

    def __repr__(self):
        return f"BuildPluginClient({self.name!r}, {self.url!r})"

    # ── HTTP ──

    def _open(self, method, path, payload=None, timeout=None, accept="application/json"):
        status, raw, _ = self._open_full(method, path, payload, timeout, accept)
        return status, raw

    def _open_full(self, method, path, payload=None, timeout=None, accept="application/json"):
        """(status, body bytes, headers) of one request."""
        headers = {"Accept": accept}
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
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode() or "{}")
            except Exception:
                body = None
            finally:
                e.close()
            message, code, forge_status = _error_from_body(body, e.code)
            raise BuildPluginError(message, code, status=e.code, plugin=self.name,
                                   forge_status=forge_status) from None
        except Exception as e:
            raise BuildPluginError(f"unreachable at {self.url}: {getattr(e, 'reason', e)}",
                                   "unreachable", plugin=self.name) from None

    def _request(self, method, path, payload=None, timeout=None):
        _, raw = self._open(method, path, payload, timeout)
        try:
            body = json.loads(raw.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            raise BuildPluginError(f"{method} {path} did not return JSON", "bad_response",
                                   plugin=self.name) from None
        if not isinstance(body, dict):
            raise BuildPluginError(f"{method} {path} returned {type(body).__name__}, not an object",
                                   "bad_response", plugin=self.name)
        return body

    # ── Endpoints ──

    def health(self, timeout=None):
        """GET /health, checked against the contract this client speaks.
        Raises BuildPluginError(code="contract") for another contract or version."""
        body = self._request("GET", "/health", timeout=timeout)
        if body.get("contract") != CONTRACT:
            raise BuildPluginError(f"speaks contract {body.get('contract')!r}, expected {CONTRACT!r}",
                                   "contract", plugin=self.name)
        version = body.get("contract_version")
        if version not in SUPPORTED_CONTRACT_VERSIONS:
            raise BuildPluginError(
                f"contract_version is {version}, this MonkeyTester supports "
                f"{', '.join(map(str, SUPPORTED_CONTRACT_VERSIONS))}; update whichever is older",
                "contract", plugin=self.name)
        return body

    def config(self, timeout=None):
        """GET /config, public shape (the forge token only as configured/last4)."""
        return public_config(self._request("GET", "/config", timeout=timeout))

    def save_config(self, payload, timeout=30):
        """POST /config {forge_kind?, forge_url?, token?, viewer_url?}. A blank
        or missing token keeps the saved one, token None removes it. Returns
        the public config; a token the code host rejects raises code
        forge_refused."""
        unknown = set(payload) - set(CONFIG_KEYS)
        if unknown:
            raise BuildPluginError(f"unsupported /config keys: {', '.join(sorted(unknown))}",
                                   "bad_request", plugin=self.name)
        body = self._request("POST", "/config", payload, timeout=timeout)
        config = body.get("config") if isinstance(body.get("config"), dict) else body
        return public_config(config)

    def check_repo(self, repo, branch="", timeout=None):
        """GET /repos/<o>/<r>?branch= -> {repo, ok, reason, default_branch,
        branch, recipes: {platform: true|false|null}}."""
        query = "?" + urlencode({"branch": branch}) if branch else ""
        body = self._request("GET", repo_path(repo) + query, timeout=timeout)
        return {"repo": str(body.get("repo") or repo), "ok": bool(body.get("ok")),
                "reason": str(body.get("reason") or ""),
                "default_branch": str(body.get("default_branch") or ""),
                "branch": str(body.get("branch") or branch or body.get("default_branch") or ""),
                "recipes": recipe_flags(body.get("recipes"))}

    def branches(self, repo, timeout=30):
        """GET /repos/<o>/<r>/branches -> {repo, default, branches}."""
        body = self._request("GET", repo_path(repo) + "/branches", timeout=timeout)
        names = [str(b) for b in body.get("branches") or [] if isinstance(b, str) and b]
        return {"repo": repo, "default": str(body.get("default") or ""), "branches": names}

    def cases(self, repo, ref, timeout=30):
        """GET /repos/<o>/<r>/cases?ref= -> {repo, ref, cases, recipes}."""
        body = self._request("GET", repo_path(repo) + "/cases?" + urlencode({"ref": ref}),
                             timeout=timeout)
        return {"repo": repo, "ref": ref,
                "cases": sorted(str(c) for c in body.get("cases") or [] if isinstance(c, str)),
                "recipes": recipe_flags(body.get("recipes"))}

    def start_build(self, repo, branch, platforms, cases, request_id, timeout=30):
        """POST /builds -> {build_ref, url}."""
        body = self._request("POST", "/builds", {
            "repo": repo, "branch": branch, "platforms": list(platforms), "cases": list(cases),
            "request_id": request_id}, timeout=timeout)
        ref = body.get("build_ref")
        if not isinstance(ref, str) or not ref:
            raise BuildPluginError("POST /builds returned no build_ref", "bad_response",
                                   plugin=self.name)
        return {"build_ref": ref, "url": str(body.get("url") or "")}

    def build(self, build_ref, timeout=None):
        """GET /builds/<ref> -> {state, conclusion, url, jobs: [{name, status,
        conclusion, url}]}, with values outside the contract dropped."""
        body = self._request("GET", "/builds/" + quote(build_ref, safe=""), timeout=timeout)
        return build_status(body)

    def log(self, build_ref, offset=0, timeout=None):
        """GET /builds/<ref>/log?offset= -> {text, size, complete}: the log
        from byte offset, its total size (X-Log-Size) and whether the build
        has finished (X-Log-Complete: 1)."""
        offset = max(0, int(offset or 0))
        _, raw, headers = self._open_full(
            "GET", "/builds/" + quote(build_ref, safe="") + "/log?" + urlencode({"offset": offset}),
            timeout=timeout, accept="text/plain")
        try:
            size = int(headers.get("X-Log-Size") or 0)
        except ValueError:
            size = 0
        return {"text": raw, "size": max(size, offset + len(raw)),
                "complete": (headers.get("X-Log-Complete") or "").strip() == "1"}

    def cancel(self, build_ref, timeout=30):
        """POST /builds/<ref>/cancel -> the build's GET /builds/<ref> body."""
        body = self._request("POST", "/builds/" + quote(build_ref, safe="") + "/cancel", {},
                             timeout=timeout)
        return build_status(body)

    def template(self, platform, timeout=None):
        """GET /template?platform= -> the example build script as text."""
        if platform not in PLATFORMS:
            raise BuildPluginError(f"platform must be one of {', '.join(PLATFORMS)}", "bad_request",
                                   plugin=self.name)
        _, raw = self._open("GET", "/template?" + urlencode({"platform": platform}), timeout=timeout,
                            accept="text/plain")
        return raw.decode("utf-8", errors="replace")


def recipe_flags(raw):
    """{platform: True|False|None} from a recipes answer (None = unknown)."""
    raw = raw if isinstance(raw, dict) else {}
    return {p: raw.get(p) if isinstance(raw.get(p), bool) else None for p in PLATFORMS}


def build_status(body):
    """A GET /builds/<ref> body with values outside the contract dropped:
    {state, conclusion, url, jobs: [{name, status, conclusion, url}]}."""
    state = body.get("state") if body.get("state") in STATES else "pending"
    conclusion = body.get("conclusion") if body.get("conclusion") in CONCLUSIONS else None
    jobs = []
    for job in body.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        jobs.append({"name": str(job.get("name") or ""),
                     "status": job.get("status") if job.get("status") in STATES else "queued",
                     "conclusion": job.get("conclusion") if job.get("conclusion") in CONCLUSIONS else None,
                     "url": str(job.get("url") or "")})
    return {"state": state, "conclusion": conclusion if state == "completed" else None,
            "url": str(body.get("url") or ""), "jobs": jobs}


def public_config(body):
    """GET/POST /config of a build plugin reduced to what a browser may see:
    {forge_kind, forge_url, token: {configured, last4, saved_at}, user,
    viewer_url}. The code host token itself is never passed on, even if a
    plugin sent it."""
    body = body if isinstance(body, dict) else {}
    token = body.get("token") if isinstance(body.get("token"), dict) else {}
    return {"forge_kind": str(body.get("forge_kind") or ""),
            "forge_url": str(body.get("forge_url") or ""),
            "token": {"configured": bool(token.get("configured")),
                      "last4": str(token.get("last4") or ""),
                      "saved_at": str(token.get("saved_at") or "")},
            "user": str(body.get("user") or ""),
            "viewer_url": str(body.get("viewer_url") or "")}


def health_entry(client, timeout=3):
    """{name, url, ok, contract_version, plugin, forge, platforms, recipes,
    viewer_url, queue, capabilities, error} from GET /health; a plugin that
    doesn't answer (or speaks another contract) still gets an entry, with the
    error."""
    entry = {"name": client.name, "url": client.url, "ok": False, "contract_version": None,
             "plugin": None, "forge": None, "platforms": [], "recipes": {}, "viewer_url": "",
             "queue": None, "capabilities": [], "error": None}
    try:
        body = client.health(timeout=timeout)
    except BuildPluginError as e:
        entry["error"] = f"{e.message} ({e.code})"
        return entry
    raw = body.get("forge")
    forge = raw if isinstance(raw, dict) else {}
    recipes = body.get("recipes") if isinstance(body.get("recipes"), dict) else {}
    queue = body.get("queue") if isinstance(body.get("queue"), dict) else None
    entry.update(ok=bool(body.get("ok")), contract_version=body.get("contract_version"),
                 plugin=body.get("plugin"),
                 forge={"kind": str(forge.get("kind") or ""), "label": str(forge.get("label") or ""),
                        "url": str(forge.get("url") or ""),
                        "configured": bool(forge.get("configured")),
                        "user": str(forge.get("user") or "")},
                 platforms=[p for p in PLATFORMS if p in (body.get("platforms") or [])],
                 recipes={p: str(recipes[p]) for p in PLATFORMS if isinstance(recipes.get(p), str)},
                 viewer_url=str(body.get("viewer_url") or ""),
                 queue={"running": queue.get("running"), "waiting": queue.get("waiting")} if queue else None,
                 capabilities=[str(c) for c in body.get("capabilities") or [] if isinstance(c, str)])
    if not entry["ok"]:
        entry["error"] = str(body.get("error") or "plugin reports not ok")
    return entry


def validate(name, url, token=None, timeout=5):
    """Check a build-plugin address before saving it: GET /health speaks this
    contract at a supported version, and, when a pairing token is given,
    GET /config accepts it. Returns (normalised url, /health body); raises
    BuildPluginError with the reason (the caller stores nothing then)."""
    if not isinstance(name, str) or not PLUGIN_NAME_RE.fullmatch(name):
        raise BuildPluginError(f"plugin name {name!r} must match {PLUGIN_NAME_RE.pattern}",
                               "bad_request")
    url = normalize_url(url)
    client = BuildPluginClient(name, url, token=token, timeout=timeout)
    body = client.health(timeout=timeout)
    try:
        client._request("GET", "/config", timeout=timeout)
    except BuildPluginError as e:
        if e.status == 401:
            raise BuildPluginError("the plugin rejected the pairing token (GET /config: 401)",
                                   "unauthorized", status=401, plugin=name) from None
        raise
    return url, body


def parse_build_plugins(spec):
    """'name=url[,name=url...]' (VT_BUILD_PLUGINS) -> [(name, url)].
    Raises BuildPluginError(code="config")."""
    entries, names = [], set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, url = part.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not name or not url:
            raise BuildPluginError(f"VT_BUILD_PLUGINS entry {part!r} is not name=url", "config")
        if not PLUGIN_NAME_RE.fullmatch(name):
            raise BuildPluginError(f"VT_BUILD_PLUGINS name {name!r} must match "
                                   f"{PLUGIN_NAME_RE.pattern}", "config")
        try:
            url = normalize_url(url)
        except BuildPluginError as e:
            raise BuildPluginError(f"VT_BUILD_PLUGINS entry {part!r}: {e.message}", "config") from None
        if name in names:
            raise BuildPluginError(f"VT_BUILD_PLUGINS names plugin {name!r} twice", "config")
        names.add(name)
        entries.append((name, url))
    return entries
