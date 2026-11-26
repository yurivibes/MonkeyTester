"""Test helper: an in-process fake build plugin (BUILD_PLUGIN_API.md, contract_version 1).

It answers every contract endpoint from class-level state that tests set:
the code host config (kind, url, token, user, viewer_url), the platforms its
host builds, the repos with their branches, cases and build scripts, and the
builds it started with their logs. A code host token other than FORGE_TOKEN is
refused like a code host would (400 forge_refused). Not a test module itself
(no test_ prefix), so test discovery skips it. Also runnable on its
own for a scratch viewer: `python3 tests/fake_build_plugin.py <port>`.
"""
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORGE_TOKEN = "forge-token-0000abcd"
REPO = "blackjackfruit/TemperatureSensor-App"
RECIPES = {"android": ".monkeytester/build-android.sh", "ios": ".monkeytester/build-ios.sh"}
TEMPLATES = {"android": "#!/usr/bin/env bash\n# build-android.sh\n./gradlew assembleDebug\n",
             "ios": "#!/usr/bin/env bash\n# build-ios.sh\nxcodebuild archive\n"}
TEMPLATE = TEMPLATES["android"]


def default_repos():
    branches = ["master", "develop"] + [f"feat/b{i:02d}" for i in range(60)] + ["feat/x"]
    return {REPO: {
        "default": "master",
        "branches": branches,
        "readable": True,
        # branch -> {platform: has the script}; branches not listed have both.
        "recipes": {},
        "cases": ["eula", "ios-eula"],
    }}


class FakeBuildPlugin(BaseHTTPRequestHandler):
    contract = "monkeytester-build-plugin"
    contract_version = 1
    name = "build-local"
    pairing = None          # PLUGIN_TOKEN of the fake, or None
    forge = {}              # {forge_kind, forge_url, token, user, saved_at, viewer_url}
    platforms = []
    capabilities = []
    repos = {}
    builds = {}             # build_ref -> GET /builds body
    logs = {}               # build_ref -> bytes
    started = []            # POST /builds bodies
    cancelled = []          # build refs POSTed to /cancel
    calls = []              # (method, path, query or body, headers)
    start_status = None     # (status, code, message) to refuse POST /builds
    next_build = None       # GET /builds body for the next started build
    config_status = None    # (status, code, message) to refuse POST /config

    @classmethod
    def reset(cls):
        cls.contract, cls.contract_version = "monkeytester-build-plugin", 1
        cls.name = "build-local"
        cls.pairing = None
        cls.forge = {"forge_kind": "gitea", "forge_url": "http://gitea.example:3000", "token": FORGE_TOKEN,
                     "user": "monkeytester-bot", "saved_at": "2026-11-19T10:00:00",
                     "viewer_url": "http://viewer.example:8089"}
        cls.platforms = ["android", "ios"]
        cls.capabilities = ["logs", "cancel"]
        cls.repos = default_repos()
        cls.builds, cls.logs, cls.started, cls.cancelled, cls.calls = {}, {}, [], [], []
        cls.start_status = None
        cls.next_build = None
        cls.config_status = None

    @classmethod
    def recipes_on(cls, info, branch):
        have = info["recipes"].get(branch, {})
        return {p: bool(have.get(p, True)) for p in ("android", "ios")}

    def log_message(self, *args):
        pass

    # ── helpers ──

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def text(self, data, headers=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def error(self, status, code, message, **extra):
        return self.reply(status, {"error": message, "code": code, **extra})

    def authorized(self):
        if not FakeBuildPlugin.pairing:
            return True
        token = self.headers.get("X-Plugin-Token") or \
            (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        return token == FakeBuildPlugin.pairing

    def public_config(self):
        f = FakeBuildPlugin.forge
        token = f.get("token") or ""
        return {"forge_kind": f.get("forge_kind") or "", "forge_url": f.get("forge_url") or "",
                "token": {"configured": bool(token), "last4": token[-4:] if token else "",
                          "saved_at": f.get("saved_at") or ""},
                "user": f.get("user") or "", "viewer_url": f.get("viewer_url") or ""}

    def repo_of(self, parts):
        repo = f"{urllib.parse.unquote(parts[1])}/{urllib.parse.unquote(parts[2])}"
        return repo, FakeBuildPlugin.repos.get(repo)

    # ── routes ──

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        FakeBuildPlugin.calls.append(("GET", path, query, dict(self.headers)))
        cls = FakeBuildPlugin
        if path == "/health":
            kind = cls.forge.get("forge_kind") or ""
            return self.reply(200, {
                "ok": True, "contract": cls.contract, "contract_version": cls.contract_version,
                "plugin": {"name": cls.name, "version": "0.1.0"},
                "forge": {"kind": kind, "label": {"gitea": "Gitea", "github": "GitHub"}.get(kind, ""),
                          "url": cls.forge.get("forge_url") or "",
                          "configured": bool(cls.forge.get("token")), "user": cls.forge.get("user") or ""},
                "platforms": list(cls.platforms),
                "recipes": {p: RECIPES[p] for p in cls.platforms},
                "viewer_url": cls.forge.get("viewer_url") or "",
                "queue": {"running": None, "waiting": 0},
                "capabilities": list(cls.capabilities), "error": None})
        if not self.authorized():
            return self.error(401, "unauthorized", "pairing token required")
        if path == "/config":
            return self.reply(200, self.public_config())
        if path == "/template":
            platform = (query.get("platform") or [""])[0]
            if platform not in TEMPLATES:
                return self.error(400, "bad_request", "platform must be android or ios")
            return self.text(TEMPLATES[platform].encode())
        if path.startswith("/builds/") and path.endswith("/log"):
            if "logs" not in cls.capabilities:
                return self.error(404, "not_found", "no route")
            ref = urllib.parse.unquote(path[len("/builds/"):-len("/log")])
            if ref not in cls.builds:
                return self.error(404, "not_found", "unknown build")
            log = cls.logs.get(ref, b"")
            offset = int((query.get("offset") or ["0"])[0] or 0)
            done = cls.builds[ref].get("state") == "completed"
            return self.text(log[offset:], {"X-Log-Size": str(len(log)), "X-Log-Complete": "1" if done else "0"})
        if path.startswith("/builds/"):
            ref = urllib.parse.unquote(path[len("/builds/"):])
            if ref not in cls.builds:
                return self.error(404, "not_found", "unknown build")
            return self.reply(200, cls.builds[ref])
        parts = path.strip("/").split("/")
        if parts[0] == "repos" and len(parts) in (3, 4):
            if not cls.forge.get("token"):
                return self.error(409, "not_configured", "no code host token saved")
            repo, info = self.repo_of(parts)
            if len(parts) == 3:
                none = {"android": None, "ios": None}
                if info is None:
                    return self.reply(200, {"repo": repo, "ok": False, "reason": "not found",
                                            "default_branch": "", "branch": "", "recipes": none})
                branch = (query.get("branch") or [""])[0] or info["default"]
                if branch not in info["branches"]:
                    return self.reply(200, {"repo": repo, "ok": False, "reason": f"branch {branch} not found",
                                            "default_branch": info["default"], "branch": branch,
                                            "recipes": none})
                ok = info["readable"]
                return self.reply(200, {"repo": repo, "ok": ok,
                                        "reason": "" if ok else "the token can't read the repo",
                                        "default_branch": info["default"], "branch": branch,
                                        "recipes": cls.recipes_on(info, branch)})
            if info is None:
                return self.error(404, "not_found", f"{repo} not found")
            if parts[3] == "branches":
                rest = [b for b in info["branches"] if b != info["default"]]
                return self.reply(200, {"repo": repo, "default": info["default"],
                                        "branches": [info["default"]] + rest})
            if parts[3] == "cases":
                ref = (query.get("ref") or [""])[0]
                if ref not in info["branches"]:
                    return self.error(404, "not_found", f"branch {ref} not found")
                return self.reply(200, {"repo": repo, "ref": ref, "cases": sorted(info["cases"]),
                                        "recipes": cls.recipes_on(info, ref)})
        return self.error(404, "not_found", "no route")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self.error(400, "bad_request", "invalid JSON")
        FakeBuildPlugin.calls.append(("POST", parsed.path, body, dict(self.headers)))
        cls = FakeBuildPlugin
        if not self.authorized():
            return self.error(401, "unauthorized", "pairing token required")
        if parsed.path == "/config":
            if cls.config_status:
                status, code, message = cls.config_status
                return self.error(status, code, message)
            forge = dict(cls.forge)
            changing = any(body.get(k) and body[k] != forge.get(k) for k in ("forge_kind", "forge_url"))
            if changing and not body.get("token"):
                return self.error(400, "bad_request", "changing the code host needs the token again")
            for key in ("forge_kind", "forge_url", "viewer_url"):
                if body.get(key):
                    forge[key] = body[key]
            if "token" in body:
                if body["token"] is None:
                    forge.update(token="", user="", saved_at="")
                elif body["token"]:
                    if body["token"] != FORGE_TOKEN:
                        return self.error(400, "forge_refused", "the code host rejected the token",
                                          forge_status=401)
                    forge.update(token=body["token"], user="monkeytester-bot", saved_at="2026-11-19T11:00:00")
            cls.forge = forge
            return self.reply(200, {"ok": True, "config": self.public_config()})
        if parsed.path.startswith("/builds/") and parsed.path.endswith("/cancel"):
            if "cancel" not in cls.capabilities:
                return self.error(404, "not_found", "no route")
            ref = urllib.parse.unquote(parsed.path[len("/builds/"):-len("/cancel")])
            if ref not in cls.builds:
                return self.error(404, "not_found", "unknown build")
            cls.cancelled.append(ref)
            build = cls.builds[ref]
            if build.get("state") != "completed":
                build.update(state="completed", conclusion="cancelled")
                for job in build.get("jobs") or []:
                    if job.get("status") != "completed":
                        job.update(status="completed", conclusion="cancelled")
                cls.logs[ref] = cls.logs.get(ref, b"") + b"--- cancelled\n"
            return self.reply(200, build)
        if parsed.path == "/builds":
            cls.started.append(body)
            if not cls.forge.get("token") or not cls.forge.get("viewer_url"):
                return self.error(409, "not_configured", "no code host token or viewer URL saved")
            if cls.start_status:
                status, code, message = cls.start_status
                return self.error(status, code, message)
            info = cls.repos.get(body.get("repo"))
            if info is None or body.get("branch") not in info["branches"]:
                return self.error(404, "not_found", "unknown branch")
            for platform in body.get("platforms") or []:
                if platform not in cls.platforms:
                    return self.error(400, "bad_request", f"this host can't build {platform}")
                if not cls.recipes_on(info, body["branch"]).get(platform):
                    return self.error(400, "bad_request",
                                      f"{RECIPES[platform]} not found on {body['branch']}")
            ref = f"br_{len(cls.started):016x}"
            cls.builds[ref] = dict(cls.next_build or {"state": "queued", "conclusion": None,
                                                      "url": "", "jobs": []})
            cls.logs[ref] = f"--- fetch {body['repo']} {body['branch']}\n".encode()
            return self.reply(201, {"build_ref": ref, "url": ""})
        return self.error(404, "not_found", "no route")


FakeBuildPlugin.reset()


def start(port=0):
    """(server, base url) of a fake plugin on a free port, serving in a thread."""
    server = ThreadingHTTPServer(("127.0.0.1", port), FakeBuildPlugin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 0),
                        FakeBuildPlugin).serve_forever()
