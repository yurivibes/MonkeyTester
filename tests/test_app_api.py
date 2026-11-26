"""The viewer's real HTTP server: security, health and component status."""
import http.client
import json
import os
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
import builds
import viewer_auth as auth
from tests.build_fixture import BUILD_ID, add_build


class Peer(BaseHTTPRequestHandler):
    """Stands in for the Mobile Controller or MonkeyTester-Plugin-AI."""
    health = {}
    status = 200
    paths = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Peer.paths.append(self.path)
        data = json.dumps(Peer.health).encode()
        self.send_response(Peer.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# Login is required: every ServerCase test runs signed in as this admin
# (ServerCase.setUp writes the account and session into a scratch
# auth_config.json). Tests of the gate itself pass auth=False.
TEST_USER = "tester"
TEST_PASSWORD = "tester-password"
TEST_SESSION = "test-session-token"
# The old shared cookie name: still accepted, so it works on any port.
TEST_COOKIE = f"{auth.LEGACY_COOKIE_NAME}={TEST_SESSION}"
TEST_PASSWORD_HASH = auth.hash_password(TEST_PASSWORD)


def write_signed_in_auth(path):
    """auth_config.json with TEST_USER (admin) and TEST_SESSION."""
    auth.save_auth({"users": {TEST_USER: {"password_hash": TEST_PASSWORD_HASH, "role": "admin",
                                          "created_at": 1}},
                    "sessions": {auth._session_key(TEST_SESSION): {"user": TEST_USER,
                                                                     "created": 1}}})


class SignedInHandler(urllib.request.BaseHandler):
    """Adds the test session cookie and a same-origin Origin to every urllib
    request to the viewer under test (unless the request sets its own)."""
    handler_order = 100

    def __init__(self, port):
        self.host = f"127.0.0.1:{port}"

    def http_request(self, req):
        if req.host == self.host:
            if not req.has_header("Cookie"):
                req.add_unredirected_header("Cookie", TEST_COOKIE)
            if not req.has_header("Origin"):
                req.add_unredirected_header("Origin", f"http://{self.host}")
        return req


def raw_get(port, path, auth=True):
    """Send the path exactly as written; urllib and browsers normalise `..` away."""
    cookie = f"Cookie: {TEST_COOKIE}\r\n" if auth else ""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(f"GET {path} HTTP/1.0\r\nHost: x\r\n{cookie}\r\n".encode())
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.TestViewerHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.peer = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
        cls.peer_url = f"http://127.0.0.1:{cls.peer.server_address[1]}"
        threading.Thread(target=cls.peer.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.peer):
            server.shutdown()
            server.server_close()

    def setUp(self):
        Peer.health, Peer.status, Peer.paths = {}, 200, []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runs = self.root / "Run"
        (self.runs / "run_1").mkdir(parents=True)
        (self.runs / "run_1" / "shot.png").write_bytes(b"\x89PNG")
        # Build & Test settings live next to app.py; keep each test's apart.
        for patcher in (patch.object(app, "TEST_DIR", self.runs),
                        patch.object(app, "CONFIG_FILE", self.runs / "config.json"),
                        patch.object(builds, "CONFIG_FILE", self.root / "build_config.json"),
                        patch.object(builds, "VIEWER_CONFIG_FILE", self.runs / "config.json"),
                        patch.object(builds, "LEGACY_GITEA_FILE", self.root / "gitea_config.json"),
                        patch.dict(os.environ, {"CONTROLLER_URL": "", "VISION_MODEL_API_URL": "",
                                                "MT_BUILD_PLUGINS": "",
                                                "VISUAL_MODEL_ENDPOINT": "", "VISUAL_MODEL_API_KEY": "",
                                                "MT_MANAGED_AI_PLUGIN_URL": "",
                                                "MT_MANAGED_AI_PLUGIN_TOKEN": ""}),
                        patch.object(auth, "AUTH_CONFIG_FILE", self.root / "auth_config.json"),
                        patch.object(auth, "SERVICE_TOKENS_FILE", self.root / "service_tokens.json")):
            patcher.start()
            self.addCleanup(patcher.stop)
        write_signed_in_auth(self.root / "auth_config.json")
        auth.THROTTLE.state.clear()
        previous = urllib.request._opener
        urllib.request.install_opener(urllib.request.build_opener(SignedInHandler(self.port)))
        self.addCleanup(urllib.request.install_opener, previous)

    def get_json(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
            return json.loads(resp.read())


class PathTraversalTests(ServerCase):
    SECRET_PATHS = (
        "/static/../gitea_config.json",
        "/static/run_1/../../gitea_config.json",
        "/static/%2e%2e/gitea_config.json",
        "/static/..%2f/gitea_config.json",
        "/static/../build_config.json",
        "/convert-video/../gitea_config.json",
        "/api/logs/..%2f..%2fetc",
        "/api/summary/%2fetc%2fpasswd",
    )

    def test_cannot_leave_the_run_directory(self):
        for path in self.SECRET_PATHS:
            status, body = raw_get(self.port, path)
            self.assertEqual(status, 400, path)
            self.assertNotIn(b"hash", body, path)

    def test_files_inside_a_run_are_still_served(self):
        status, body = raw_get(self.port, "/static/run_1/shot.png")
        self.assertEqual((status, body), (200, b"\x89PNG"))

    def test_detector(self):
        self.assertTrue(app.has_path_traversal("/a/../b"))
        self.assertTrue(app.has_path_traversal("/a/%2E%2E/b"))
        self.assertTrue(app.has_path_traversal("/a%2Fb"))
        self.assertFalse(app.has_path_traversal("/api/test/run_1/my%20test..txt"))
        self.assertFalse(app.has_path_traversal("/api/frame/10.0.0.9%3A5555"))


class HealthTests(ServerCase):
    def test_health_is_open_and_reveals_nothing_else(self):
        self.assertEqual(self.get_json("/health"),
                         {"ok": True, "service": "monkeytester", "api_version": app.API_VERSION})

    def test_status_is_open_without_any_cookie(self):
        self.assertIn("controller", self.get_json("/api/status"))


class StatusTests(ServerCase):
    def status_of(self, **env):
        with patch.dict(os.environ, env):
            return self.get_json("/api/status")

    def test_nothing_configured(self):
        status = self.status_of()
        self.assertFalse(status["ok"])
        self.assertIn("CONTROLLER_URL", status["controller"]["error"])
        self.assertIn("No vision", status["vision"]["error"])
        self.assertEqual(status["plugins"], {})

    def test_plugins_are_reported_without_changing_ok(self):
        Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": {"name": "esp32", "version": "0.1.0"}}
        status = self.status_of(MT_PLUGINS=f"esp32={self.peer_url},gone=http://127.0.0.1:9")
        self.assertEqual(status["plugins"]["esp32"],
                         {"ok": True, "url": self.peer_url, "contract_version": 1,
                          "plugin": {"name": "esp32", "version": "0.1.0"}})
        self.assertFalse(status["plugins"]["gone"]["ok"])
        self.assertIn("error", status["plugins"]["gone"])
        self.assertIn("controller", status)

    def test_both_healthy(self):
        Peer.health = {"ok": True, "api_version": 2, "service": "monkeytester-ai", "appium": True,
                       "adb": True, "model": "m", "model_loaded": True}
        status = self.status_of(CONTROLLER_URL=self.peer_url,
                                VISION_MODEL_API_URL=self.peer_url + "/v1/chat/completions")
        self.assertTrue(status["ok"])
        self.assertTrue(status["vision"]["checked"])
        self.assertEqual(status["vision"]["model"], "m")
        self.assertEqual(Peer.paths, ["/health", "/health"])

    def test_controller_on_another_api_version_says_so(self):
        Peer.health = {"ok": True, "api_version": 1}
        status = self.status_of(CONTROLLER_URL=self.peer_url)
        self.assertFalse(status["controller"]["ok"])
        self.assertIn("update whichever is older", status["controller"]["error"])

    def test_unreachable_controller(self):
        status = self.status_of(CONTROLLER_URL="http://127.0.0.1:1")
        self.assertIn("unreachable", status["controller"]["error"])

    def test_other_vision_endpoints_are_reported_unchecked(self):
        Peer.status = 404
        status = self.status_of(VISION_MODEL_API_URL=self.peer_url + "/v1/chat/completions")
        self.assertEqual(status["vision"]["checked"], False)
        self.assertTrue(status["vision"]["ok"])

    def test_saved_endpoint_beats_the_environment(self):
        (self.runs / "config.json").write_text(json.dumps({"visual_endpoint": "http://saved/v1"}))
        with patch.dict(os.environ, {"VISION_MODEL_API_URL": "http://env/v1"}):
            self.assertEqual(app.vision_settings()[0], "http://saved/v1")

    def test_environment_is_the_default_shown_to_settings(self):
        with patch.dict(os.environ, {"VISION_MODEL_API_URL": "http://env/v1"}):
            config = self.get_json("/api/config/visual-model")
        self.assertEqual((config["endpoint"], config["default_endpoint"]), ("", "http://env/v1"))


class PluginSettingsTests(ServerCase):
    """GET /api/plugins: the Settings page's read-only view of active device plugins."""

    def test_open_without_any_cookie(self):
        self.assertIn("plugins", self.get_json("/api/plugins"))

    def test_nothing_configured_lists_only_the_ai_row(self):
        with patch.dict(os.environ, {"MT_PLUGINS": ""}):
            out = self.get_json("/api/plugins")
        entry = out["plugins"][0]  # the always-listed monkeytester-ai row
        self.assertEqual((entry["name"], entry["source"], entry["url"], entry["ok"],
                          entry["config"]),
                         ("monkeytester-ai", "settings", "", False, None))
        self.assertIn("not configured", entry["error"])
        self.assertEqual(out["ai_plugin"], {"name": "monkeytester-ai", "url": "", "configured": False,
                                            "token": {"configured": False, "last4": ""},
                                            "managed": False})
        self.assertFalse(entry["managed"])

    def test_live_plugin_state_is_reported(self):
        Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": {"name": "esp32", "version": "0.1.0"},
                       "capabilities": ["flash"],
                       "devices": [{"id": "ab:cd", "model": "C3", "state": "ready"}]}
        with patch.dict(os.environ, {"MT_PLUGINS": f"esp32={self.peer_url}"}):
            out = self.get_json("/api/plugins")
        by_name = {p["name"]: p for p in out["plugins"]}
        self.assertEqual(sorted(by_name), ["esp32", "monkeytester-ai"])
        self.assertEqual(by_name["esp32"], {"name": "esp32", "url": self.peer_url, "ok": True,
                                           "source": "MT_PLUGINS",
                                           "contract_version": 1,
                                           "plugin": {"name": "esp32", "version": "0.1.0"},
                                           "capabilities": ["flash"],
                                           "config": None, "docs": False,
                                           "devices": [{"id": "ab:cd", "model": "C3",
                                                        "state": "ready"}]})
        self.assertFalse(by_name["monkeytester-ai"]["ok"])  # listed, not configured
        self.assertEqual(by_name["monkeytester-ai"]["source"], "settings")

    def test_dead_plugin_still_lists_with_its_error(self):
        with patch.dict(os.environ, {"MT_PLUGINS": "gone=http://127.0.0.1:9"}):
            out = self.get_json("/api/plugins")
        by_name = {p["name"]: p for p in out["plugins"]}
        self.assertEqual(sorted(by_name), ["gone", "monkeytester-ai"])
        self.assertFalse(by_name["gone"]["ok"])
        self.assertIn("error", by_name["gone"])

    def test_malformed_spec_says_so(self):
        with patch.dict(os.environ, {"MT_PLUGINS": "broken"}):
            out = self.get_json("/api/plugins")
        self.assertIn("MT_PLUGINS", out["error"])
        self.assertEqual([p["name"] for p in out["plugins"]], ["monkeytester-ai"])

    def test_plugins_are_checked_at_the_same_time(self):
        plugin_client = app._plugin_client()
        health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1}

        def slow_health(client, timeout=None):
            time.sleep(0.6)
            return health

        spec = ",".join(f"p{i}=http://127.0.0.1:9" for i in range(3))
        with patch.object(plugin_client.PluginClient, "health", slow_health), \
                patch.object(plugin_client.PluginClient, "devices", lambda c, timeout=None: []), \
                patch.dict(os.environ, {"MT_PLUGINS": spec}):
            started = time.monotonic()
            out = self.get_json("/api/plugins")
            elapsed = time.monotonic() - started
        self.assertEqual([p["name"] for p in out["plugins"]], ["p0", "p1", "p2", "monkeytester-ai"])
        self.assertTrue(all(p["ok"] for p in out["plugins"] if p["source"] == "MT_PLUGINS"))
        self.assertLess(elapsed, 1.5)  # one after another would take 1.8 s


class PluginManageTests(ServerCase):
    """POST /api/config/plugins: Settings → Device Plugins adds/edits/removes
    device-plugin registrations; the saved `plugins` map then owns the list."""

    def post(self, path, payload):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            e.close()
            raise

    @staticmethod
    def health(name="esp32-sensor", **extra):
        return {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                "plugin": {"name": name, "version": "0.1"}, **extra}

    def refused(self, payload):
        """(status, body) of a POST that must fail."""
        status, _, body = request(self.port, "POST", "/api/config/plugins", payload)
        self.assertNotEqual(status, 200, body)
        return status, body

    def registry(self):
        return json.loads((self.runs / "config.json").read_text())["plugins"]

    def write_config(self, cfg):
        (self.runs / "config.json").write_text(json.dumps(cfg))

    def test_set_needs_a_live_contract_plugin(self):
        # No name in the op: the plugin's /health name is the registry key.
        Peer.health = self.health("esp32-sensor")
        out = self.post("/api/config/plugins", {"set": {"url": self.peer_url + "/"}})
        self.assertEqual(out["plugin"], {"name": "esp32-sensor", "url": self.peer_url,
                                         "ok": True, "contract_version": 1,
                                         "plugin": {"name": "esp32-sensor",
                                                    "version": "0.1"},
                                         "token": {"configured": False, "last4": "",
                                                   "saved_at": None}})
        self.assertNotIn("renamed_from", out)
        cfg = json.loads((self.runs / "config.json").read_text())
        self.assertEqual(cfg["plugins"], {"esp32-sensor": self.peer_url})
        listed = {p["name"]: p for p in self.get_json("/api/plugins")["plugins"]}
        self.assertEqual(listed["esp32-sensor"]["source"], "config")

    def test_string_plugin_form_names_the_plugin_too(self):
        Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": "hub"}
        out = self.post("/api/config/plugins", {"set": {"url": self.peer_url}})
        self.assertEqual(out["plugin"]["name"], "hub")
        self.assertEqual(self.registry(), {"hub": self.peer_url})

    def test_name_in_set_is_refused(self):
        Peer.health = self.health("hub")
        status, body = self.refused({"set": {"name": "hub", "url": self.peer_url}})
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        self.assertIn("/health", body["error"])
        self.assertFalse((self.runs / "config.json").exists())

    def test_health_without_a_usable_name_is_no_plugin_name(self):
        for plugin in (None, {"version": "1"}, {"name": "Bad Name"}, "x!"):
            Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1}
            if plugin is not None:
                Peer.health["plugin"] = plugin
            status, body = self.refused({"set": {"url": self.peer_url}})
            self.assertEqual((status, body["code"]), (400, "no_plugin_name"), plugin)
        self.assertFalse((self.runs / "config.json").exists())

    def test_taken_name_at_another_address_is_refused(self):
        self.write_config({"plugins": {"esp32-sensor": "http://127.0.0.1:9"}})
        Peer.health = self.health("esp32-sensor")
        status, body = self.refused({"set": {"url": self.peer_url}})
        self.assertEqual((status, body["code"]), (400, "name_taken"))
        self.assertIn("http://127.0.0.1:9", body["error"])
        self.assertEqual(self.registry(), {"esp32-sensor": "http://127.0.0.1:9"})

    def test_same_name_same_address_is_an_idempotent_update(self):
        Peer.health = self.health("esp32-sensor")
        self.post("/api/config/plugins", {"set": {"url": self.peer_url}})
        out = self.post("/api/config/plugins", {"set": {"url": self.peer_url + "/",
                                                        "token": "tok-abcd"}})
        self.assertEqual(out["plugin"]["token"]["last4"], "abcd")
        self.assertEqual(self.registry(), {"esp32-sensor": self.peer_url})
        self.assertEqual(auth.plugin_token("esp32-sensor"), "tok-abcd")

    def test_edit_with_row_and_same_name_updates_the_address(self):
        self.write_config({"plugins": {"esp32-sensor": "http://127.0.0.1:9", "z": "http://z:1"}})
        Peer.health = self.health("esp32-sensor")
        out = self.post("/api/config/plugins",
                        {"set": {"url": self.peer_url}, "row": "esp32-sensor"})
        self.assertNotIn("renamed_from", out)
        self.assertEqual(self.registry(), {"esp32-sensor": self.peer_url, "z": "http://z:1"})

    def test_edit_renames_the_row_and_moves_its_token(self):
        self.write_config({"plugins": {"a": "http://a:1", "old-name": "http://127.0.0.1:9",
                                       "z": "http://z:1"}})
        auth.set_service_token("plugin:old-name", "old-token-1234")
        Peer.health = self.health("new-name")
        out = self.post("/api/config/plugins",
                        {"set": {"url": self.peer_url}, "row": "old-name"})
        self.assertEqual(out["renamed_from"], "old-name")
        self.assertEqual(out["plugin"]["name"], "new-name")
        self.assertEqual(out["plugin"]["token"]["last4"], "1234")
        # Same place in the list, under the new key.
        self.assertEqual(list(self.registry().items()),
                         [("a", "http://a:1"), ("new-name", self.peer_url), ("z", "http://z:1")])
        self.assertEqual(auth.plugin_token("new-name"), "old-token-1234")
        self.assertEqual(auth.plugin_token("old-name"), "")

    def test_rename_with_a_new_token_uses_it(self):
        self.write_config({"plugins": {"old-name": "http://127.0.0.1:9"}})
        auth.set_service_token("plugin:old-name", "old-token-1234")
        Peer.health = self.health("new-name")
        self.post("/api/config/plugins",
                  {"set": {"url": self.peer_url, "token": "new-token-5678"}, "row": "old-name"})
        self.assertEqual(auth.plugin_token("new-name"), "new-token-5678")
        self.assertEqual(auth.plugin_token("old-name"), "")

    def test_rename_onto_another_existing_row_is_name_taken(self):
        self.write_config({"plugins": {"one": "http://127.0.0.1:9", "two": "http://two:1"}})
        Peer.health = self.health("two")
        status, body = self.refused({"set": {"url": self.peer_url}, "row": "one"})
        self.assertEqual((status, body["code"]), (400, "name_taken"))
        self.assertEqual(self.registry(), {"one": "http://127.0.0.1:9", "two": "http://two:1"})

    def test_edit_of_an_unregistered_row_is_nothing_to_edit(self):
        Peer.health = self.health("esp32-sensor")
        status, body = self.refused({"set": {"url": self.peer_url}, "row": "ghost"})
        self.assertEqual((status, body["code"]), (400, "nothing_to_edit"))
        self.assertFalse((self.runs / "config.json").exists())

    def test_set_seeds_the_map_from_live_env_entries(self):
        Peer.health = self.health("new")
        with patch.dict(os.environ, {"MT_PLUGINS": "seed=http://127.0.0.1:9"}):
            out = self.post("/api/config/plugins", {"set": {"url": self.peer_url}})
        self.assertEqual(out["plugins"], {"seed": "http://127.0.0.1:9",
                                          "new": self.peer_url})

    def test_env_ignored_once_the_map_exists(self):
        Peer.health = self.health("saved")
        self.post("/api/config/plugins", {"set": {"url": self.peer_url}})
        with patch.dict(os.environ, {"MT_PLUGINS": "stale=http://127.0.0.1:9"}):
            names = [p["name"] for p in self.get_json("/api/plugins")["plugins"]]
        self.assertEqual(sorted(names), ["monkeytester-ai", "saved"])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/config/plugins", {"remove": "stale"})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_set_refused_when_health_does_not_speak_the_contract(self):
        Peer.health = {}
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/config/plugins", {"set": {"url": self.peer_url}})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        self.assertFalse((self.runs / "config.json").exists())

    def test_set_refused_when_the_address_is_dead(self):
        status, body = self.refused({"set": {"url": "http://127.0.0.1:9"}})
        self.assertEqual((status, body["code"]), (400, "unreachable"))

    def test_set_rejects_bad_reserved_or_malformed_input(self):
        Peer.health = self.health("hub")
        bad = [{"set": {"name": "monkeytester-ai", "url": self.peer_url}},
               {"set": {"url": "ftp://a"}},
               {"set": {"url": ""}},
               {"set": {}},
               {"set": "hub"},
               {"set": {"url": self.peer_url, "extra": 1}},
               {"set": {"url": self.peer_url}, "row": "Bad Name"},
               {"set": {"url": self.peer_url}, "remove": "other"}]
        for payload in bad:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("/api/config/plugins", payload)
            self.assertEqual(caught.exception.code, 400, payload)
            caught.exception.close()
        self.assertFalse((self.runs / "config.json").exists())

    def test_remove_forgets_a_registration(self):
        Peer.health = self.health("gone")
        self.post("/api/config/plugins", {"set": {"url": self.peer_url}})
        out = self.post("/api/config/plugins", {"remove": "gone"})
        self.assertEqual(out["plugins"], {})
        names = [p["name"] for p in self.get_json("/api/plugins")["plugins"]]
        self.assertEqual(names, ["monkeytester-ai"])


class DeviceListTests(ServerCase):
    def test_partial_failure_keeps_the_working_platform(self):
        payload = {"devices": [{"id": "a:1", "platform": "android", "model": None}],
                   "errors": {"ios": "no Xcode"}}
        event = app.device_event_for_ui(payload)
        self.assertEqual(event["error"], "ios: no Xcode")
        self.assertEqual([d["id"] for d in event["devices"]], ["a:1"])


def request(port, method, path, body=None, headers=None, auth=True):
    """Return (status, headers, json-or-None) without following redirects.
    auth=True sends the test session cookie and a same-origin Origin."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(headers or {})
    if auth:
        headers.setdefault("Cookie", TEST_COOKIE)
        headers.setdefault("Origin", f"http://127.0.0.1:{port}")
    if data:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return resp.status, dict(resp.getheaders()), parsed


class LoginRequiredTests(ServerCase):
    """Login is always required (no off switch, no localhost exemption).
    Leftover MT_APP_TOKEN values from an old launcher app open
    nothing. More auth tests: test_viewer_auth.py."""

    def test_signed_in_every_page_and_api_answers(self):
        for path in ("/", "/config", "/plugins", "/users",
                     "/run-status", "/api/status", "/api/runs", "/static/run_1/shot.png"):
            status, _, _ = request(self.port, "GET", path)
            self.assertEqual(status, 200, path)
        for path, target in (("/models", "/plugins"), ("/models.html", "/plugins"),
                             ("/appearance", "/config"), ("/appearance.html", "/config"),
                             ("/device-config", "/plugins"), ("/device-config.html", "/plugins")):
            status, headers, _ = request(self.port, "GET", path)
            self.assertEqual((status, headers.get("Location")), (302, target), path)
        self.assertEqual(request(self.port, "HEAD", "/")[0], 200)
        self.assertEqual(request(self.port, "OPTIONS", "/api/runs")[0], 204)
        self.assertEqual(request(self.port, "POST", "/api/theme", {"theme": "dark"})[0], 200)

    def test_without_a_session_pages_redirect_to_login_and_apis_are_401(self):
        for path in ("/", "/config", "/device-config", "/plugins", "/run-status", "/users",
                     "/build", "/stream.html", "/test.html"):
            status, headers, _ = request(self.port, "GET", path, auth=False)
            self.assertEqual(status, 302, path)
            self.assertTrue(headers.get("Location", "").startswith("/login"), path)
        status, headers, _ = request(self.port, "GET", "/config?x=1", auth=False)
        self.assertEqual(headers.get("Location"), "/login?next=%2Fconfig%3Fx%3D1")
        for path in ("/api/status", "/api/runs", "/api/config", "/api/plugins", "/devices",
                     "/static/run_1/shot.png", "/api/runs/run_1/result", "/api/config/ci-token",
                     "/api/users"):
            self.assertEqual(request(self.port, "GET", path, auth=False)[0], 401, path)
        for path in ("/api/config", "/api/runs/delete", "/api/config/controller", "/api/config/plugins", "/api/builds",
                     "/api/press-event", "/api/users", "/api/config/ci-token"):
            self.assertEqual(request(self.port, "POST", path, {}, auth=False)[0], 401, path)
        self.assertEqual(request(self.port, "HEAD", "/", auth=False)[0], 302)

    def test_public_routes_stay_open(self):
        for path in ("/health", "/login", "/api/auth/status", "/api/theme", "/ci/submit.py",
                     "/fonts/anton.woff2"):
            self.assertEqual(request(self.port, "GET", path, auth=False)[0], 200, path)
        self.assertEqual(request(self.port, "POST", "/api/theme", {"theme": "dark"},
                                 auth=False)[0], 200)

    def test_a_wrong_bearer_or_cookie_opens_nothing(self):
        with patch.dict(os.environ, {"MT_APP_TOKEN": "s3cret"}):
            for headers in ({"Authorization": "Bearer s3cret"}, {"Cookie": "mt_app=s3cret"},
                            {"Cookie": "mt_auth=wrong"}):
                self.assertEqual(request(self.port, "GET", "/config", headers=headers,
                                         auth=False)[0], 302, headers)

    def test_auth_status_names_the_user(self):
        status = self.get_json("/api/auth/status")
        self.assertEqual({k: status[k] for k in ("signed_in", "username", "role", "default_password")},
                         {"signed_in": True, "username": TEST_USER, "role": "admin",
                          "default_password": False})
        _, _, anon = request(self.port, "GET", "/api/auth/status", auth=False)
        self.assertEqual((anon["signed_in"], anon["username"], anon["role"]), (False, "", ""))

    def test_runner_press_events_send_the_runner_token(self):
        import importlib
        import sys as _sys
        _sys.path.insert(0, str(Path(app.__file__).parent / "mt-tests/scripts"))
        self.addCleanup(_sys.path.pop, 0)
        press_events = importlib.import_module("press_events")
        sent = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b""

        def fake_urlopen(req, timeout):
            sent.update(req.headers)
            return Resp()
        with patch.object(press_events.urllib.request, "urlopen", fake_urlopen), \
                patch.dict(os.environ, {"MT_RUNNER_TOKEN": "rt-123"}):
            press_events._post("http://127.0.0.1:1/api/press-event", {}, 1)
        self.assertNotIn("Cookie", sent)
        self.assertEqual(sent.get("X-mt-runner-token"), "rt-123")


class ThemeFirstPaintTests(ServerCase):
    """The saved theme is rendered into the HTML itself (html tag carries
    data-theme), so the first paint is already right and switching pages
    never flashes dark — independent of this browser's localStorage."""

    def test_light_theme_is_on_the_html_tag(self):
        (self.runs / "config.json").write_text(json.dumps({"theme": "light"}))
        for path in ("/", "/logs.html"):
            status, body = raw_get(self.port, path)
            self.assertEqual(status, 200, path)
            self.assertIn(b'<html lang="en" data-theme="light">', body, path)

    def test_dark_theme_keeps_the_plain_tag(self):
        (self.runs / "config.json").write_text(json.dumps({"theme": "dark"}))
        status, body = raw_get(self.port, "/")
        self.assertNotIn(b'<html lang="en" data-theme="light"', body)

    def test_shared_js_does_not_reapply_a_browser_copy_of_the_theme(self):
        # A per-browser localStorage copy that is missing or says "dark"
        # used to strip the server-rendered light theme until /api/config
        # answered: a dark flash on every page switch in light mode.
        self.assertNotIn("mt-theme", app.SHARED_JS)


class ThemeFontsTests(ServerCase):
    """The shared theme self-hosts its web fonts under /fonts/, so pages make
    no external requests. Only bare .woff2 names that exist in fonts/ are
    served; anything else 404s."""

    def test_every_font_the_css_names_is_served(self):
        names = re.findall(r"url\(/fonts/([^)]+)\)", app.SHARED_CSS)
        self.assertEqual(sorted(names),
                         ["anton.woff2", "jetbrains-mono.woff2", "space-grotesk.woff2"])
        for name in names:
            status, body = raw_get(self.port, f"/fonts/{name}")
            self.assertEqual(status, 200, name)
            self.assertEqual(body[:4], b"wOF2", name)

    def test_font_content_type(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/fonts/anton.woff2")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.getheader("Content-Type"), "font/woff2")

    def test_only_woff2_names_in_the_font_dir(self):
        # `..` paths are refused earlier by the global traversal guard (400).
        for path in ("/fonts/../app.py", "/fonts/..%2Fapp.py", "/fonts/missing.woff2",
                     "/fonts/anton.ttf", "/fonts/", "/fonts/sub/anton.woff2"):
            status, _ = raw_get(self.port, path)
            self.assertIn(status, (400, 404), path)

    def test_pages_carry_no_retired_palette(self):
        """The GitHub-dark hexes the old theme used must not linger in the
        shared CSS: they would ignore dark/light mode."""
        for old in ("#0d1117", "#58a6ff", "#1f6feb", "#f85149", "#d29922", "#1a1a2e"):
            self.assertFalse(old in app.SHARED_CSS, old)


class SettingsPagesTests(ServerCase):
    """The settings sections are one page each (login required: these tests
    run signed in as an admin). The Models page is retired: the picker lives inside
    the Device Plugins panel, so /models redirects there. The Appearance page
    is retired the same way: its Light Mode toggle lives on /config. The Device
    Config page is retired too: its options live in the Device Config section
    on /plugins."""
    PAGES = {
        "/config": [b'id="buildSection"', b'id="light_mode"'],
        "/plugins": [b'id="pluginsTable"', b'id="buildPluginsTable"', b'id="controllerTable"',
                     b'id="controllerConfigure"'],
        "/run-status": [b'id="runStatusPanel"'],
    }

    def test_all_pages_open_when_signed_in(self):
        for path in self.PAGES:
            status, body = raw_get(self.port, path)
            self.assertEqual(status, 200, path)
            for marker in self.PAGES[path]:
                self.assertIn(marker, body, path)

    def test_there_is_no_signup(self):
        # No self sign-up: accounts are created by an admin (Settings → Users).
        for auth_ in (True, False):
            for path in ("/api/signup", "/api/config/auth"):
                self.assertIn(request(self.port, "POST", path, {}, auth=auth_)[0], (401, 404), path)
            for path in ("/signup", "/signup.html"):
                try:
                    status = request(self.port, "GET", path, auth=auth_)[0]
                except ConnectionError:
                    status = None
                self.assertNotEqual(status, 200, path)
        self.assertFalse((Path(app.__file__).parent / "signup.html").exists())

    def test_models_page_redirects_to_plugins(self):
        # The standalone Models page is retired: the picker lives in the
        # Device Plugins panel, so old bookmarks must not 404. The Copilot
        # page went the same way: Copilot is set up on the plugin.
        for path in ("/models", "/models.html", "/copilot", "/copilot.html"):
            status, headers, _ = request(self.port, "GET", path)
            self.assertEqual((status, headers.get("Location")), (302, "/plugins"), path)

    def test_each_page_holds_only_its_own_section(self):
        bodies = {path: raw_get(self.port, path)[1] for path in self.PAGES}
        for path, body in bodies.items():
            for other, markers in self.PAGES.items():
                if other != path:
                    for marker in markers:
                        self.assertNotIn(marker, body, f"{path} contains the {other} section")

    def test_logout_button_is_on_the_configuration_page_only(self):
        for path in self.PAGES:
            body = raw_get(self.port, path)[1]
            if path == "/config":
                self.assertIn(b'id="logoutBtn"', body)
            else:
                self.assertNotIn(b'id="logoutBtn"', body, path)

    def test_old_urls_redirect(self):
        for path, target in (("/settings", "/config"), ("/settings.html", "/config"),
                             ("/configuration", "/plugins"),
                             ("/configuration.html", "/plugins"),
                             ("/device-config", "/plugins"),
                             ("/device-config.html", "/plugins"),
                             ("/appearance", "/config"), ("/appearance.html", "/config")):
            status, headers, _ = request(self.port, "GET", path)
            self.assertEqual((status, headers.get("Location")), (302, target), path)


COPILOT_MODELS = [{"id": "gpt-4o", "name": "GPT-4o", "vendor": "OpenAI"},
                  {"id": "claude-sonnet-4", "name": "Claude Sonnet 4", "vendor": "Anthropic"}]


def ai_state():
    return {"selected": "qwen-vl", "installed": ["qwen-vl", "llava"],
            "token": "secret-tok-9876", "rotations": 0, "provider": "local",
            "copilot_user": "", "copilot_token": "", "copilot_model": "gpt-4o",
            "polls": 0, "require_token": False, "plugin_name": "monkeytester-ai",
            "remote_url": "", "remote_model": "", "remote_resize": True, "remote_key": ""}


REMOTE_MODELS = ["video-model", "qwen2.5-vl-7b"]


class AiPlugin(BaseHTTPRequestHandler):
    """Fake monkeytester-ai plugin (the config-owning plugin the Device Plugins
    panel proxies), including its GitHub Copilot provider. State lives on the
    class so tests can read what it got."""
    state = ai_state()
    posts = []
    auth = []  # X-Plugin-Token of every request

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _copilot(self):
        st = AiPlugin.state
        ct = st["copilot_token"]
        return {"configured": bool(ct), "user": st["copilot_user"],
                "last4": ct[-4:] if ct else "", "saved_at": "2026-11-11T10:00:00Z" if ct else "",
                "model": st["copilot_model"], "models": COPILOT_MODELS if ct else [],
                "error": None}

    def _remote(self):
        st = AiPlugin.state
        key = st["remote_key"]
        return {"url": st["remote_url"], "model": st["remote_model"],
                "models": [{"id": m} for m in REMOTE_MODELS] if st["remote_url"] else [],
                "resize": st["remote_resize"],
                "key": {"configured": bool(key), "last4": key[-4:] if key else ""},
                "error": None}

    def _config_body(self):
        tok = AiPlugin.state["token"]
        return {"model": {"selected": AiPlugin.state["selected"],
                          "installed": list(AiPlugin.state["installed"])},
                "token": {"configured": bool(tok), "last4": tok[-4:] if tok else ""},
                "port": 8091, "ollama_port": 8093, "max_pixels": 100352,
                "api_version": 3, "service": "monkeytester-ai",
                "provider": AiPlugin.state["provider"], "copilot": self._copilot(),
                "remote": self._remote()}

    def _authorized(self):
        """With require_token, everything but /health needs the pairing token
        as X-Plugin-Token or Bearer (the real plugin's rule)."""
        if not AiPlugin.state["require_token"] or self.path == "/health":
            return True
        want = AiPlugin.state["token"]
        bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if want and want in (self.headers.get("X-Plugin-Token"), bearer):
            return True
        self._send(401, {"error": {"code": "unauthorized", "message": "pairing token required"}})
        return False

    def _bad(self, message, code="invalid_request"):
        self._send(400, {"error": {"code": code, "message": message}})

    def do_GET(self):
        AiPlugin.auth.append(self.headers.get("X-Plugin-Token"))
        if not self._authorized():
            return
        if self.path == "/health":
            self._send(200, {"ok": True, "contract": "monkeytester-plugin",
                             "contract_version": 1,
                             "plugin": {"name": AiPlugin.state["plugin_name"], "version": "0.3.0"},
                             "devices": 1, "service": "monkeytester-ai",
                             "api_version": 3, "port": 8091, "error": None,
                             "provider": AiPlugin.state["provider"],
                             "model": (AiPlugin.state["copilot_model"]
                                       if AiPlugin.state["provider"] == "copilot"
                                       else AiPlugin.state["selected"])})
        elif self.path == "/devices":
            self._send(200, {"devices": [{"id": "gateway", "kind": "visual-model",
                                          "state": "ready"}], "error": None})
        elif self.path == "/actions":
            self._send(200, {"actions": [{"name": "set-model",
                                          "description": "Select an installed model.",
                                          "params": {"model": {"type": "string",
                                                               "required": True}}}]})
        elif self.path == "/models":
            self._send(200, {"object": "list", "data": [{"id": m} for m in
                             AiPlugin.state["installed"]],
                             "selected": AiPlugin.state["selected"],
                             "installed": list(AiPlugin.state["installed"])})
        elif self.path == "/config":
            self._send(200, self._config_body())
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        AiPlugin.posts.append((self.path, body))
        AiPlugin.auth.append(self.headers.get("X-Plugin-Token"))
        if not self._authorized():
            return
        st = AiPlugin.state
        if self.path == "/config" and any(k.startswith("remote_") for k in body):
            # Validate everything before changing anything (the plugin spec).
            url = st["remote_url"]
            if "remote_url" in body:
                url = str(body["remote_url"] or "").rstrip("/")
                if url and "unreachable" in url:
                    self._bad(f"could not reach {url}/v1/models: connection refused")
                    return
            if "remote_model" in body and body["remote_model"] not in REMOTE_MODELS:
                self._bad(f"model {body['remote_model']!r} is not on {url}")
                return
            st["remote_url"] = url
            if not url and st["provider"] == "remote":
                st["provider"] = "local"
            if "remote_model" in body:
                st["remote_model"] = body["remote_model"]
            if "remote_resize" in body:
                st["remote_resize"] = bool(body["remote_resize"])
            if "remote_api_key" in body:
                st["remote_key"] = body["remote_api_key"] or ""
            self._send(200, {"ok": True, "config": self._config_body()})
        elif self.path == "/config" and body.get("provider") == "remote" and not (
                st["remote_url"] and st["remote_model"]):
            self._bad("set remote_url and remote_model before selecting remote")
        elif self.path == "/copilot/device/start":
            self._send(200, {"user_code": "ABCD-1234", "device_code": "dev-secret-code",
                             "verification_uri": "https://github.com/login/device",
                             "interval": 5, "expires_in": 900})
        elif self.path == "/copilot/device/poll":
            st["polls"] += 1
            if st["polls"] == 1:
                self._send(200, {"state": "pending", "interval": 5})
            else:
                st["copilot_user"], st["copilot_token"] = "octo", "gho_secretgithubtoken42"
                self._send(200, {"state": "done", **self._copilot()})
        elif self.path == "/copilot/remove":
            st["copilot_user"], st["copilot_token"] = "", ""
            if st["provider"] == "copilot":
                st["provider"] = "local"
            self._send(200, self._copilot())
        elif self.path == "/config" and "provider" in body:
            if body["provider"] == "copilot" and not st["copilot_token"]:
                self._bad("sign in to GitHub Copilot before selecting it")
                return
            st["provider"] = body["provider"]
            self._send(200, {"ok": True, "config": self._config_body()})
        elif self.path == "/config" and "copilot_model" in body:
            if body["copilot_model"] not in [m["id"] for m in COPILOT_MODELS]:
                self._bad(f"unknown Copilot model {body['copilot_model']!r}")
                return
            st["copilot_model"] = body["copilot_model"]
            self._send(200, {"ok": True, "config": self._config_body()})
        elif self.path == "/config":
            if "generate" in body:
                # The real plugin has no top-level "generate" key.
                self._send(400, {"error": {"code": "invalid_request",
                                           "message": "unknown setting: generate"}})
                return
            if body.get("token") == {"generate": True}:
                AiPlugin.state["rotations"] += 1
                new = f"rotated-{AiPlugin.state['rotations']:04}"
                AiPlugin.state["token"] = new
                # Real reply shape: {ok, config, new_token}; the value is shown once.
                self._send(200, {"ok": True, "config": self._config_body(), "new_token": new})
            elif "model" in body:
                model = str(body["model"] or "").strip()
                if model not in AiPlugin.state["installed"]:
                    self._send(400, {"error": f"model {model!r} is not installed",
                                     "code": "bad_request"})
                    return
                AiPlugin.state["selected"] = model
                self._send(200, self._config_body())
            else:
                self._send(200, self._config_body())
        elif self.path == "/v1/chat/completions":
            self._send(200, {"id": "c1", "object": "chat.completion",
                             "model": AiPlugin.state["selected"],
                             "choices": [{"index": 0,
                                          "message": {"role": "assistant",
                                                      "content": "hi there"},
                                          "finish_reason": "stop"}]})
        else:
            self.send_error(404)


class VisualModelPluginTests(ServerCase):
    """The Device Plugins panel's Configure view is a thin proxy over the
    monkeytester-ai plugin: the plugin owns endpoint, installed models,
    selection and pairing token (the standalone Models page is gone)."""

    def serve_plugin(self):
        plugin_server = ThreadingHTTPServer(("127.0.0.1", 0), AiPlugin)
        threading.Thread(target=plugin_server.serve_forever, daemon=True).start()
        self.addCleanup(plugin_server.shutdown)
        self.addCleanup(plugin_server.server_close)
        return f"http://127.0.0.1:{plugin_server.server_address[1]}"

    def setUp(self):
        super().setUp()
        AiPlugin.state = ai_state()
        AiPlugin.posts = []
        AiPlugin.auth = []
        app._PLUGIN_SELECTED_CACHE.clear()  # no cross-test staleness

    def plugin_env(self, url):
        """The monkeytester-ai address lives in config.json (set in Settings);
        a MT_PLUGINS entry for it is ignored. Merges visual_plugin_url into
        whatever config.json the test already wrote."""
        path = self.runs / "config.json"
        cfg = json.loads(path.read_text()) if path.exists() else {}
        cfg["visual_plugin_url"] = url
        path.write_text(json.dumps(cfg))
        return patch.dict(os.environ, {"MT_PLUGINS": ""})

    def config(self):
        return json.loads((self.runs / "config.json").read_text())

    def test_plugin_info_is_proxied_with_token_read_only(self):
        url = self.serve_plugin()
        with self.plugin_env(url):
            info = self.get_json("/api/visual-model/plugin")
        self.assertTrue(info["ok"])
        self.assertEqual((info["plugin"], info["selected"]), ("monkeytester-ai", "qwen-vl"))
        self.assertEqual(info["installed"], ["qwen-vl", "llava"])
        self.assertEqual(info["token"], {"configured": True, "last4": "9876"})
        self.assertNotIn("secret-tok-9876", json.dumps(info))

    def test_plugins_endpoint_routes_the_ai_plugin_the_ai_way(self):
        """Settings → Plugins Add row: a set op whose address's /health names
        plugin monkeytester-ai is stored as visual_plugin_url + visual_plugin
        (token -> visual_api_key), never in the `plugins` map; {"remove":
        "monkeytester-ai"} clears it like plugin_url: ""."""
        url = self.serve_plugin()
        AiPlugin.state["require_token"] = True
        with patch.dict(os.environ, {"MT_PLUGINS": ""}):
            # No name in the op: the /health plugin name decides.
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": url + "/",
                                               "token": "secret-tok-9876"}})
            self.assertEqual(status, 200, body)
            self.assertTrue(body["ai"])
            self.assertEqual(body["plugin"]["name"], "monkeytester-ai")
            self.assertEqual(body["plugin"]["url"], url)
            self.assertEqual(body["plugin"]["token"]["last4"], "9876")
            cfg = self.config()
            self.assertEqual((cfg["visual_plugin_url"], cfg["visual_plugin"], cfg["visual_api_key"]),
                             (url, "monkeytester-ai", "secret-tok-9876"))
            self.assertNotIn("vision", cfg.get("plugins") or {})
            self.assertNotIn("monkeytester-ai", cfg.get("plugins") or {})
            self.assertIn("secret-tok-9876", AiPlugin.auth)  # /config validated with it
            out = self.get_json("/api/plugins")
            self.assertEqual(out["ai_plugin"]["url"], url)
            self.assertTrue(out["ai_plugin"]["configured"])
            # Save address on the AI row: same set op with row
            # monkeytester-ai, no token -> the stored pairing token is used.
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": url}, "row": "monkeytester-ai"})
            self.assertEqual(status, 200, body)
            self.assertEqual(self.config()["visual_api_key"], "secret-tok-9876")
            # Remove clears the AI keys; the legacy endpoint keeps working too.
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"remove": "monkeytester-ai"})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["removed"], "monkeytester-ai")
            cfg = self.config()
            self.assertNotIn("visual_plugin_url", cfg)
            self.assertNotIn("visual_plugin", cfg)
            self.assertFalse(self.get_json("/api/plugins")["ai_plugin"]["configured"])
            # Removing again: nothing to remove.
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"remove": "monkeytester-ai"})
            self.assertEqual(status, 400)
            self.assertEqual(body["code"], "nothing_to_remove")
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"plugin_url": url})
            self.assertEqual(status, 200, body)
            self.assertEqual(self.config()["visual_plugin_url"], url)

    def test_plugins_endpoint_ai_row_edits_stay_ai_to_ai(self):
        """The vision-model plugin is added/removed through the Add row: an
        address edit never turns the AI row into another plugin or another
        row into the AI plugin; a second AI address on the Add row is
        name_taken."""
        url = self.serve_plugin()
        Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": {"name": "esp32-sensor"}}
        with patch.dict(os.environ, {"MT_PLUGINS": ""}):
            # Row monkeytester-ai not configured yet: nothing to edit.
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": url}, "row": "monkeytester-ai"})
            self.assertEqual((status, body["code"]), (400, "nothing_to_edit"))
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": url}})
            self.assertEqual(status, 200, body)
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": self.peer_url}})
            self.assertEqual(status, 200, body)
            before = self.config()
            # The AI row onto a device plugin's address.
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": self.peer_url}, "row": "monkeytester-ai"})
            self.assertEqual((status, body["code"]), (400, "bad_request"))
            self.assertIn("Add row", body["error"])
            # A device row onto the AI plugin's address.
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": url}, "row": "esp32-sensor"})
            self.assertEqual((status, body["code"]), (400, "bad_request"))
            self.assertIn("Add row", body["error"])
            # Another AI address on the Add row while one is configured.
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": url.replace("127.0.0.1", "localhost")}})
            self.assertEqual((status, body["code"]), (400, "name_taken"))
            self.assertEqual(self.config(), before)
            # The AI row onto (another form of) an AI address keeps working.
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": url + "/"}, "row": "monkeytester-ai"})
            self.assertEqual(status, 200, body)
            self.assertTrue(body["ai"])

    def test_plugins_endpoint_ai_set_stores_nothing_when_the_token_is_rejected(self):
        url = self.serve_plugin()
        AiPlugin.state["require_token"] = True
        with patch.dict(os.environ, {"MT_PLUGINS": ""}):
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": url, "token": "wrong"}})
        self.assertNotEqual(status, 200, body)
        self.assertEqual(body["code"], "unauthorized")
        path = self.runs / "config.json"
        cfg = json.loads(path.read_text()) if path.exists() else {}
        self.assertNotIn("visual_plugin_url", cfg)
        self.assertNotIn("vision", cfg.get("plugins") or {})

    def test_plugins_overview_flags_the_visual_plugin(self):
        # The Device Plugins panel gates its Configure button on this: GET
        # /config reachable with model info, token read-only.
        url = self.serve_plugin()
        with self.plugin_env(url):
            out = self.get_json("/api/plugins")
        entry = out["plugins"][0]
        self.assertEqual(entry["name"], "monkeytester-ai")
        self.assertTrue(entry["config"])
        self.assertEqual((entry["config"]["selected"], entry["config"]["installed"]),
                         ("qwen-vl", ["qwen-vl", "llava"]))
        self.assertEqual(entry["config"]["token"], {"configured": True, "last4": "9876"})
        self.assertNotIn("secret-tok-9876", json.dumps(out))

    def test_save_selects_the_model_on_the_plugin_not_in_config(self):
        url = self.serve_plugin()
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "local", "model": "llava"})
        self.assertEqual(status, 200)
        self.assertTrue(body["saved"])
        self.assertEqual((body["plugin"], body["plugin_url"]), ("monkeytester-ai", url))
        cfg = json.loads((self.runs / "config.json").read_text())
        self.assertEqual(cfg["visual_plugin"], "monkeytester-ai")
        self.assertNotIn("visual_endpoint", cfg)
        self.assertNotIn("visual_model", cfg)
        self.assertIn(("/config", {"model": "llava"}), AiPlugin.posts)
        self.assertEqual(AiPlugin.state["selected"], "llava")

    def test_single_save_sends_provider_only_as_one_provider_post(self):
        """The panel's staged Save: provider alone => exactly one POST."""
        url = self.serve_plugin()
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "remote", "remote_url": "http://r:8080",
                                       "remote_model": "video-model"})
            self.assertEqual(status, 200, body)
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "local"})
            self.assertEqual(status, 200, body)
        self.assertEqual(AiPlugin.posts[-1], ("/config", {"provider": "local"}))

    def test_single_save_orders_remote_keys_before_provider(self):
        """One staged Save click => remote keys in ONE plugin POST, provider last."""
        url = self.serve_plugin()
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "remote", "remote_url": "http://r:8080",
                                       "remote_model": "video-model", "remote_resize": False,
                                       "remote_api_key": "k-1234"})
            self.assertEqual(status, 200, body)
        self.assertEqual(AiPlugin.posts, [("/config", {"remote_url": "http://r:8080",
                                                      "remote_model": "video-model",
                                                      "remote_resize": False,
                                                      "remote_api_key": "k-1234"}),
                                         ("/config", {"provider": "remote"})])
        st = AiPlugin.state
        self.assertEqual((st["provider"], st["remote_url"], st["remote_model"],
                          st["remote_resize"], st["remote_key"]),
                         ("remote", "http://r:8080", "video-model", False, "k-1234"))

    def test_single_save_address_then_provider_keeps_saved_state(self):
        """A bad staged address saves nothing and later steps keep old state."""
        url = self.serve_plugin()
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"plugin_url": "http://127.0.0.1:9"})
            self.assertNotEqual(status, 200, body)
        cfg = json.loads((self.runs / "config.json").read_text())
        self.assertEqual(cfg["visual_plugin_url"], url)  # bad address never stored

    def test_save_rejects_a_model_the_plugin_does_not_have(self):
        url = self.serve_plugin()
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "local", "model": "nope"})
        # The plugin's 400 and its own message reach the page.
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "bad_request")
        self.assertIn("is not installed", body["error"])
        self.assertEqual(AiPlugin.state["selected"], "qwen-vl")

    def test_save_without_a_configured_plugin_fails(self):
        with patch.dict(os.environ, {"MT_PLUGINS": ""}):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "local", "model": "llava"})
        self.assertEqual((status, body["code"]), (400, "not_configured"))
        self.assertIn("Settings → Device Plugins", body["error"])
        self.assertFalse((self.runs / "config.json").exists())

    def test_generate_token_rotates_on_the_plugin(self):
        url = self.serve_plugin()
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"generate_token": True})
        self.assertEqual(status, 200)
        new_token = f"rotated-{AiPlugin.state['rotations']:04}"
        cfg = json.loads((self.runs / "config.json").read_text())
        self.assertEqual(cfg["visual_api_key"], new_token)
        self.assertEqual(body["token"], {"configured": True, "last4": new_token[-4:]})
        self.assertNotIn(new_token, json.dumps(body))  # the viewer never echoes it

    def test_clear_token_drops_only_the_viewers_copy(self):
        url = self.serve_plugin()
        (self.runs / "config.json").write_text(json.dumps(
            {"visual_provider": "local", "visual_plugin": "monkeytester-ai",
             "visual_api_key": "secret-tok-9876"}))
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"clear_token": True})
        self.assertEqual(status, 200)
        self.assertFalse(body["token"]["configured"])
        cfg = json.loads((self.runs / "config.json").read_text())
        self.assertNotIn("visual_api_key", cfg)
        self.assertEqual(cfg["visual_plugin"], "monkeytester-ai")
        self.assertNotIn(("/config", {"generate": True}), AiPlugin.posts)

    def test_vision_settings_resolves_the_plugin_endpoint(self):
        url = self.serve_plugin()
        (self.runs / "config.json").write_text(json.dumps(
            {"visual_provider": "local", "visual_plugin": "monkeytester-ai"}))
        with self.plugin_env(url):
            endpoint, _, model = app.vision_settings()
        self.assertEqual(endpoint, url + "/v1/chat/completions")
        self.assertEqual(model, "qwen-vl")

    def test_status_tolerates_the_richer_ai_health(self):
        url = self.serve_plugin()
        (self.runs / "config.json").write_text(json.dumps(
            {"visual_provider": "local", "visual_plugin": "monkeytester-ai"}))
        with self.plugin_env(url):
            status = self.get_json("/api/status")
        self.assertEqual(status["plugins"]["monkeytester-ai"],
                         {"ok": True, "url": url, "contract_version": 1,
                          "plugin": {"name": "monkeytester-ai", "version": "0.3.0"}})
        self.assertTrue(status["vision"]["ok"])
        self.assertEqual(status["vision"]["plugin"], "monkeytester-ai")
        self.assertFalse(status["controller"]["ok"])

    def test_chat_test_goes_to_the_plugin(self):
        url = self.serve_plugin()
        (self.runs / "config.json").write_text(json.dumps(
            {"visual_provider": "local", "visual_plugin": "monkeytester-ai"}))
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/visual-model/test",
                                      {"provider": "local", "messages":
                                       [{"role": "user", "content": "Say hi"}]})
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(body["response"]["choices"][0]["message"]["content"], "hi there")

    # ── GitHub Copilot lives on the plugin; the viewer only relays ──

    def test_plugin_info_carries_provider_and_public_copilot(self):
        url = self.serve_plugin()
        AiPlugin.state.update(copilot_user="octo", copilot_token="gho_secretgithubtoken42",
                              provider="copilot", copilot_model="claude-sonnet-4")
        with self.plugin_env(url):
            info = self.get_json("/api/visual-model/plugin")
        self.assertEqual(info["provider"], "copilot")
        self.assertEqual((info["copilot"]["configured"], info["copilot"]["user"],
                          info["copilot"]["last4"], info["copilot"]["model"]),
                         (True, "octo", "en42", "claude-sonnet-4"))
        self.assertEqual([m["id"] for m in info["copilot"]["models"]], ["gpt-4o", "claude-sonnet-4"])
        # The LOCAL list comes from /config, not from the active provider's /models.
        self.assertEqual(info["installed"], ["qwen-vl", "llava"])
        self.assertNotIn("gho_secretgithubtoken42", json.dumps(info))
        self.assertFalse(info["legacy_copilot"])

    def test_plugin_calls_send_the_pairing_token(self):
        url = self.serve_plugin()
        (self.runs / "config.json").write_text(json.dumps(
            {"visual_provider": "local", "visual_plugin": "monkeytester-ai",
             "visual_api_key": "pair-tok-1111"}))
        with self.plugin_env(url):
            self.get_json("/api/visual-model/plugin")
            request(self.port, "POST", "/api/visual-model/plugin/copilot/device/start", {})
        self.assertTrue(AiPlugin.auth)
        self.assertEqual(set(AiPlugin.auth), {"pair-tok-1111"})

    def test_provider_save_forwards_to_the_plugin(self):
        url = self.serve_plugin()
        AiPlugin.state.update(copilot_user="octo", copilot_token="gho_secretgithubtoken42")
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "copilot", "copilot_model": "claude-sonnet-4"})
        self.assertEqual(status, 200, body)
        # Model first, provider last, one change per POST.
        self.assertEqual(AiPlugin.posts, [("/config", {"copilot_model": "claude-sonnet-4"}),
                                          ("/config", {"provider": "copilot"})])
        self.assertEqual((AiPlugin.state["provider"], AiPlugin.state["copilot_model"]),
                         ("copilot", "claude-sonnet-4"))
        self.assertEqual(body["config"]["provider"], "copilot")
        self.assertNotIn("gho_secretgithubtoken42", json.dumps(body))
        cfg = json.loads((self.runs / "config.json").read_text())
        # The viewer still calls the plugin; it keeps no Copilot state itself.
        self.assertEqual(cfg["visual_provider"], "local")
        self.assertNotIn("copilot_model", cfg)
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "local", "model": "llava"})
        self.assertEqual((status, AiPlugin.state["provider"], AiPlugin.state["selected"]),
                         (200, "local", "llava"))

    def test_copilot_while_signed_out_surfaces_the_plugins_refusal(self):
        url = self.serve_plugin()
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "copilot"})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "invalid_request")
        self.assertIn("sign in to GitHub Copilot", body["error"])
        self.assertEqual(AiPlugin.state["provider"], "local")
        self.assertEqual(self.config(), {"visual_plugin_url": url})  # nothing else written

    def test_bad_provider_is_rejected_before_the_plugin(self):
        url = self.serve_plugin()
        with self.plugin_env(url):
            status, _, _ = request(self.port, "POST", "/api/config/visual-model",
                                   {"provider": "openai"})
        self.assertEqual(status, 400)
        self.assertEqual(AiPlugin.posts, [])

    def test_copilot_device_flow_is_proxied(self):
        url = self.serve_plugin()
        with self.plugin_env(url):
            status, _, start = request(self.port, "POST",
                                       "/api/visual-model/plugin/copilot/device/start", {})
            self.assertEqual(status, 200)
            self.assertEqual((start["user_code"], start["verification_uri"], start["interval"]),
                             ("ABCD-1234", "https://github.com/login/device", 5))
            self.assertNotIn("device_code", start)  # stays inside the plugin
            _, _, first = request(self.port, "POST", "/api/visual-model/plugin/copilot/device/poll", {})
            self.assertEqual(first["state"], "pending")
            _, _, done = request(self.port, "POST", "/api/visual-model/plugin/copilot/device/poll", {})
            self.assertEqual((done["state"], done["copilot"]["user"], done["copilot"]["configured"]),
                             ("done", "octo", True))
            self.assertNotIn("gho_secretgithubtoken42", json.dumps(done))
            AiPlugin.state["provider"] = "copilot"
            status, _, removed = request(self.port, "POST", "/api/visual-model/plugin/copilot/remove", {})
        self.assertEqual(status, 200)
        self.assertFalse(removed["copilot"]["configured"])
        self.assertEqual(AiPlugin.state["provider"], "local")
        self.assertEqual([p for p, _ in AiPlugin.posts],
                         ["/copilot/device/start", "/copilot/device/poll",
                          "/copilot/device/poll", "/copilot/remove"])

    def test_copilot_routes_are_open_to_anyone(self):
        url = self.serve_plugin()
        with self.plugin_env(url):
            for path in ("/api/visual-model/plugin/copilot/device/start",
                         "/api/visual-model/plugin/copilot/device/poll",
                         "/api/visual-model/plugin/copilot/remove"):
                self.assertEqual(request(self.port, "POST", path, {})[0], 200, path)
        self.assertTrue(AiPlugin.posts)  # reached the plugin, no gate in front

    def test_copilot_routes_without_a_plugin_say_so(self):
        with patch.dict(os.environ, {"MT_PLUGINS": ""}):
            status, _, body = request(self.port, "POST",
                                      "/api/visual-model/plugin/copilot/device/start", {})
        self.assertEqual((status, body["code"]), (400, "not_configured"))
        self.assertIn("Settings → Device Plugins", body["error"])

    def test_old_copilot_api_is_gone(self):
        for path in ("/api/copilot/device/start", "/api/copilot/device/poll", "/api/copilot/remove"):
            self.assertEqual(request(self.port, "POST", path, {})[0], 404, path)
        for path in ("/api/copilot", "/api/copilot/models"):
            # Unknown GET routes get no answer at all from do_GET (pre-existing).
            try:
                status = request(self.port, "GET", path)[0]
            except ConnectionError:
                status = None
            self.assertNotEqual(status, 200, path)

    def test_status_shows_the_plugins_provider(self):
        url = self.serve_plugin()
        AiPlugin.state.update(copilot_user="octo", copilot_token="gho_secretgithubtoken42",
                              provider="copilot", copilot_model="claude-sonnet-4")
        (self.runs / "config.json").write_text(json.dumps(
            {"visual_provider": "local", "visual_plugin": "monkeytester-ai"}))
        with self.plugin_env(url):
            vision = self.get_json("/api/status")["vision"]
        self.assertEqual((vision["ok"], vision["provider"], vision["model"]),
                         (True, "copilot", "claude-sonnet-4"))

    def test_legacy_copilot_config_is_deprecated_and_hinted(self):
        url = self.serve_plugin()
        (self.runs / "config.json").write_text(json.dumps(
            {"visual_provider": "copilot", "copilot_model": "gpt-4o"}))
        app._DEPRECATION_LOGGED.clear()
        with self.plugin_env(url), patch.object(app, "log_deprecation",
                                                wraps=app.log_deprecation) as dep:
            config = self.get_json("/api/config/visual-model")
            info = self.get_json("/api/visual-model/plugin")
            vision = self.get_json("/api/status")["vision"]
        self.assertTrue(config["legacy_copilot"])
        self.assertIn("monkeytester-ai → Configure → GitHub Copilot", config["legacy_hint"])
        self.assertTrue(info["legacy_copilot"])
        self.assertEqual(info["legacy_hint"], app.COPILOT_MOVED_MESSAGE)
        self.assertFalse(vision["ok"])
        self.assertEqual(vision["error"], app.COPILOT_MOVED_MESSAGE)
        self.assertTrue(any("copilot" in str(c.args[0]) for c in dep.call_args_list))
        # A save from the panel replaces the old setting.
        with self.plugin_env(url):
            status, _, _ = request(self.port, "POST", "/api/config/visual-model",
                                   {"provider": "local", "model": "llava"})
        self.assertEqual(status, 200)
        cfg = json.loads((self.runs / "config.json").read_text())
        self.assertEqual((cfg["visual_provider"], cfg["visual_plugin"]), ("local", "monkeytester-ai"))
        self.assertNotIn("copilot_model", cfg)

    def test_plugins_page_has_the_provider_choice_and_no_copilot_link(self):
        _, body = raw_get(self.port, "/plugins")
        self.assertIn(b'id="pluginProviderCopilot"', body)
        self.assertIn(b"/api/visual-model/plugin/copilot/device/start", body)
        self.assertNotIn(b'href="/copilot"', body)
        self.assertNotIn(b"onclick=", body.split(b"// Device plugins")[1])

    # ── The monkeytester-ai address is set in Settings (config.json) ──

    def test_address_save_validates_health_name_and_token(self):
        url = self.serve_plugin()
        AiPlugin.state["require_token"] = True
        (self.runs / "config.json").write_text(json.dumps({"theme": "dark", "video_size": "75%"}))
        with patch.dict(os.environ, {"MT_PLUGINS": ""}):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"plugin_url": url + "/", "api_key": "secret-tok-9876"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["plugin"], body["plugin_url"]), ("monkeytester-ai", url))
        self.assertEqual(body["token"], {"configured": True, "last4": "9876"})
        self.assertNotIn("secret-tok-9876", json.dumps(body))
        cfg = self.config()
        self.assertEqual((cfg["visual_plugin_url"], cfg["visual_plugin"], cfg["visual_api_key"]),
                         (url, "monkeytester-ai", "secret-tok-9876"))
        self.assertEqual((cfg["theme"], cfg["video_size"]), ("dark", "75%"))  # unrelated keys kept
        # /config was read with the token (validation), /health without needing it.
        self.assertIn("secret-tok-9876", AiPlugin.auth)

    def test_address_save_uses_the_stored_token_when_none_is_given(self):
        url = self.serve_plugin()
        AiPlugin.state["require_token"] = True
        (self.runs / "config.json").write_text(json.dumps({"visual_api_key": "secret-tok-9876"}))
        status, _, body = request(self.port, "POST", "/api/config/visual-model", {"plugin_url": url})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.config()["visual_plugin_url"], url)

    def test_set_token_pairs_the_viewer_before_the_address(self):
        """The panel's first-time "Set pairing token": stores only the viewer's
        copy (the service already owns it), no plugin call — so a plugin that
        answers 401 unauthenticated can be paired before its address is saved."""
        url = self.serve_plugin()
        AiPlugin.state["require_token"] = True
        status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                  {"set_token": "secret-tok-9876"})
        self.assertEqual(status, 200, body)
        cfg = self.config()
        self.assertEqual(cfg["visual_api_key"], "secret-tok-9876")
        self.assertNotIn("visual_plugin_url", cfg)  # address still unset
        self.assertEqual(AiPlugin.posts, [])        # no plugin call for a Set
        self.assertEqual(body["token"], {"configured": True, "last4": "9876"})
        self.assertNotIn("secret-tok-9876", json.dumps(body))  # never echoed
        # With the copy stored, the panel's address save validates with it.
        status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                  {"plugin_url": url})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.config()["visual_plugin_url"], url)

    def test_set_token_needs_a_value(self):
        status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                  {"set_token": "  "})
        self.assertEqual((status, body["code"]), (400, "invalid_request"))
        self.assertFalse((self.runs / "config.json").exists())

    def test_address_save_stores_nothing_on_failure(self):
        url = self.serve_plugin()
        AiPlugin.state["require_token"] = True
        before = {"visual_plugin_url": "http://old:8091", "visual_plugin": "monkeytester-ai",
                  "visual_api_key": "old-tok-0000", "theme": "light"}
        (self.runs / "config.json").write_text(json.dumps(before))
        cases = [
            ({"plugin_url": url, "api_key": "wrong-token"}, 502, "unauthorized"),
            ({"plugin_url": "not a url"}, 400, "invalid_request"),
            ({"plugin_url": "http://127.0.0.1:9"}, 502, "unreachable"),
        ]
        for payload, want_status, want_code in cases:
            status, _, body = request(self.port, "POST", "/api/config/visual-model", payload)
            self.assertEqual((status, body["code"]), (want_status, want_code), payload)
            self.assertTrue(body["error"])
            self.assertEqual(self.config(), before, payload)
        # A plugin-contract service that is not monkeytester-ai is refused too.
        AiPlugin.state.update(require_token=False, plugin_name="esp32-sensor")
        status, _, body = request(self.port, "POST", "/api/config/visual-model", {"plugin_url": url})
        self.assertEqual((status, body["code"]), (400, "wrong_plugin"))
        self.assertIn("esp32-sensor", body["error"])
        self.assertEqual(self.config(), before)
        # Something that is not a plugin at all (the peer answers {} to /health).
        status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                  {"plugin_url": self.peer_url})
        self.assertEqual((status, body["code"]), (400, "contract"))
        self.assertEqual(self.config(), before)

    def test_empty_address_removes_the_plugin(self):
        (self.runs / "config.json").write_text(json.dumps(
            {"visual_plugin_url": "http://x:8091", "visual_plugin": "monkeytester-ai",
             "visual_api_key": "tok-1234", "theme": "dark"}))
        status, _, body = request(self.port, "POST", "/api/config/visual-model", {"plugin_url": ""})
        self.assertEqual((status, body["plugin_url"]), (200, ""))
        self.assertEqual(self.config(), {"visual_api_key": "tok-1234", "theme": "dark"})
        with patch.dict(os.environ, {"MT_PLUGINS": "monkeytester-ai=http://ignored:8091"}):
            out = self.get_json("/api/plugins")
        # The row is still listed ("not configured"), so the panel stays reachable;
        # the env entry does not come back.
        self.assertEqual([p["name"] for p in out["plugins"]], ["monkeytester-ai"])
        self.assertFalse(out["plugins"][0]["ok"])
        self.assertFalse(out["ai_plugin"]["configured"])

    def test_address_routes_are_open(self):
        # No gate in front of config writes: the validation itself answers.
        for payload in ({"plugin_url": "http://x:8091"}, {"plugin_url": ""},
                        {"remote_url": "http://x:8080"}):
            self.assertNotEqual(request(self.port, "POST", "/api/config/visual-model",
                                        payload)[0], 401, payload)

    def test_plugin_list_is_env_minus_ai_plus_config_address(self):
        url = self.serve_plugin()
        Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": {"name": "esp32-sensor"}}
        env = f"esp32-sensor={self.peer_url},monkeytester-ai=http://127.0.0.1:9"
        with self.plugin_env(url), patch.dict(os.environ, {"MT_PLUGINS": env}):
            out = self.get_json("/api/plugins")
            status = self.get_json("/api/status")
        by_name = {p["name"]: p for p in out["plugins"]}
        self.assertEqual(sorted(by_name), ["esp32-sensor", "monkeytester-ai"])
        self.assertEqual(by_name["monkeytester-ai"]["url"], url)  # not the env's :9
        self.assertEqual(by_name["monkeytester-ai"]["source"], "settings")
        self.assertEqual(by_name["esp32-sensor"]["source"], "MT_PLUGINS")
        self.assertTrue(by_name["monkeytester-ai"]["ok"])
        self.assertEqual(out["ai_plugin"]["url"], url)
        self.assertEqual(status["plugins"]["monkeytester-ai"]["url"], url)
        self.assertEqual(status["plugins"]["esp32-sensor"]["url"], self.peer_url)

    # ── Remote server provider: settings live on the plugin ──

    def test_remote_save_forwards_to_the_plugin(self):
        url = self.serve_plugin()
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"remote_url": "http://192.168.1.10:8080",
                                       "remote_api_key": "sk-remote-secret-5678"})
            self.assertEqual(status, 200, body)
            self.assertEqual([m["id"] for m in body["config"]["remote"]["models"]], REMOTE_MODELS)
            self.assertEqual(body["config"]["remote"]["key"], {"configured": True, "last4": "5678"})
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "remote", "remote_model": "video-model",
                                       "remote_resize": False})
            self.assertEqual(status, 200, body)
            info = self.get_json("/api/visual-model/plugin")
            overview = self.get_json("/api/plugins")
        # The remote keys go in one POST (url + key together), provider last.
        self.assertEqual(AiPlugin.posts, [
            ("/config", {"remote_url": "http://192.168.1.10:8080",
                         "remote_api_key": "sk-remote-secret-5678"}),
            ("/config", {"remote_model": "video-model", "remote_resize": False}),
            ("/config", {"provider": "remote"})])
        self.assertEqual((AiPlugin.state["provider"], AiPlugin.state["remote_model"],
                          AiPlugin.state["remote_resize"]), ("remote", "video-model", False))
        self.assertEqual(body["config"]["provider"], "remote")
        self.assertEqual(info["provider"], "remote")
        self.assertEqual((info["remote"]["url"], info["remote"]["model"], info["remote"]["resize"]),
                         ("http://192.168.1.10:8080", "video-model", False))
        # The remote key never comes back from the viewer.
        for reply in (body, info, overview, self.get_json("/api/config/visual-model")):
            self.assertNotIn("sk-remote-secret-5678", json.dumps(reply))
        self.assertNotIn("sk-remote-secret-5678", (self.runs / "config.json").read_text())

    def test_remote_key_remove_and_errors_surface(self):
        url = self.serve_plugin()
        AiPlugin.state.update(remote_url="http://r:8080", remote_key="sk-old-1111")
        with self.plugin_env(url):
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"remote_api_key": None})
            self.assertEqual(status, 200, body)
            self.assertEqual(AiPlugin.posts[-1], ("/config", {"remote_api_key": None}))
            self.assertFalse(body["config"]["remote"]["key"]["configured"])
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"remote_url": "http://unreachable:8080"})
            self.assertEqual((status, body["code"]), (400, "invalid_request"))
            self.assertIn("could not reach http://unreachable:8080/v1/models", body["error"])
            self.assertEqual(AiPlugin.state["remote_url"], "http://r:8080")
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "remote", "remote_model": "nope"})
            self.assertEqual(status, 400)
            self.assertIn("'nope'", body["error"])
            self.assertEqual(AiPlugin.state["provider"], "local")  # provider never sent
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"remote_resize": "yes"})
            self.assertEqual(status, 400)

    def test_plugins_page_has_the_panel_address_and_no_row_controls(self):
        _, body = raw_get(self.port, "/plugins")
        for marker in (b'id="pluginProviderRemote"', b'id="pluginRemoteUrl"',
                       b'id="pluginRemoteResize"',
                       b'id="pluginSaveAll"', b'id="pluginTokenChoice"',
                       b'id="pluginCopilotChoice"',
                       b"pluginTokenSetInput", b"pluginRemoteKeyInput",
                       b"planSavePayload", b"plugin_url"):
            self.assertIn(marker, body)
        # Panel section order: the 'Plugin' heading precedes the
        # 'Configuration' heading, and the single Save button markup
        # comes AFTER the Configuration heading (Save is last in
        # Configuration; the address/token fields precede it).
        i_plugin = body.index(b'class="panel-section-head">Plugin</div>')
        i_config = body.index(b'class="panel-section-head">Configuration</div>')
        i_save = body.index(b'id="pluginSaveAll"')
        self.assertLess(i_plugin, i_config)
        self.assertLess(i_config, i_save)
        # The plugin address field and the keep/remove-plugin select moved
        # out of the panel: the table row owns address and removal now.
        self.assertNotIn(b'id="pluginAddressInput"', body)
        self.assertNotIn(b'id="pluginAddressChoice"', body)
        self.assertLess(body.index(b'id="pluginTokenChoice"'), i_config)
        # One Save button for the panel; every old per-section save control AND
        # the old standalone action buttons (Replace token, Remove plugin,
        # Copilot sign in/out) are gone: they are staged selects now.
        for absent in (b'id="aiAddForm"', b"aiAddrSave", b"aiAddrUrl", b"ai-edit-btn",
                       b"Edit address", b"Add vision model plugin",
                       b"pluginAddressSave", b"pluginTokenSetBtn", b"pluginSaveBtn",
                       b"pluginTokenRemoveBtn", b"pluginRemoteKeySave",
                       b"pluginRemoteKeyRemove", b"Test &amp; save",
                       b"Remove pairing token", b"pluginTokenReplaceBtn",
                       b"pluginAddressRemove", b"pluginCopilotSignIn",
                       b"pluginCopilotSignOut", b">Sign out</button>",
                       b">Sign in with GitHub<", b">Replace token</button>",
                       b">Remove plugin</button>"):
            self.assertNotIn(absent, body)
        self.assertNotIn(b"onclick=", body.split(b"// Device plugins")[1])

    def test_legacy_endpoint_shape_still_saves(self):
        """Old setups (endpoint + model_name in the body) work as before."""
        status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                  {"provider": "local", "endpoint": "http://old/v1",
                                   "model_name": "m1"})
        self.assertEqual(status, 200)
        self.assertEqual((body["endpoint"], body["model_name"]), ("http://old/v1", "m1"))
        cfg = json.loads((self.runs / "config.json").read_text())
        self.assertEqual((cfg["visual_endpoint"], cfg["visual_model"]),
                         ("http://old/v1", "m1"))
        self.assertNotIn("visual_plugin", cfg)

    def test_legacy_config_still_reads_back(self):
        (self.runs / "config.json").write_text(json.dumps(
            {"visual_endpoint": "http://old/v1", "visual_model": "m1"}))
        config = self.get_json("/api/config/visual-model")
        self.assertEqual((config["endpoint"], config["model_name"], config["plugin"]),
                         ("http://old/v1", "m1", ""))
        self.assertEqual(config["token"], {"configured": False, "last4": ""})
        self.assertEqual(app.vision_settings()[0], "http://old/v1")


class DisableWebRunsTests(ServerCase):
    """MT_DISABLE_WEB_RUNS hides the run-starting links in the nav. UI only:
    the pages still open by URL."""

    def test_flag_is_reported_to_the_nav(self):
        for value, expected in (("", False), ("0", False), ("off", False), ("1", True), ("true", True)):
            with patch.dict(os.environ, {"MT_DISABLE_WEB_RUNS": value}):
                self.assertIs(self.get_json("/api/auth/status")["web_runs_disabled"], expected, value)

    def test_pages_still_open_by_url(self):
        with patch.dict(os.environ, {"MT_DISABLE_WEB_RUNS": "1"}):
            # The manual upload page is gone: the viewer no longer answers it.
            try:
                status = request(self.port, "GET", "/new-test.html")[0]
            except http.client.RemoteDisconnected:
                status = None  # unknown routes drop the connection
            self.assertNotEqual(status, 200)
            self.assertEqual(request(self.port, "GET", "/build.html")[0], 200)

    def test_nav_hides_both_run_links_when_flagged(self):
        status, _, _ = request(self.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("!(l.startsRun && authConfig.web_runs_disabled)", app.SHARED_JS)
        for href in ("/build.html",):
            line = next(ln for ln in app.SHARED_JS.splitlines() if f"href: '{href}'" in ln)
            self.assertIn("startsRun: true", line, href)


FAKE_RUNNER = """
import json, os, sys, time
argv = sys.argv[1:]
out = argv[argv.index("--output-dir") + 1]
n = 1 + max([int(d[4:]) for d in os.listdir(out) if d.startswith("run_") and d[4:].isdigit()] or [0])
run = os.path.join(out, f"run_{n}")
os.makedirs(run)
after = int(argv[argv.index("--after-pid") + 1]) if "--after-pid" in argv else None
prev = argv[argv.index("--after-run") + 1] if "--after-run" in argv else None
def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
if after:
    while alive(after) and not (prev and os.path.exists(os.path.join(out, prev, "exit_code"))):
        time.sleep(0.05)
start = time.time()
time.sleep(0.8)
json.dump({"argv": argv, "pid": os.getpid(), "start": start, "end": time.time()},
          open(os.path.join(run, "fake.json"), "w"))
"""


class RunListCountsTests(ServerCase):
    """The dashboard run list shows "passed/total passed" summed over every device."""

    def write_meta(self, name, meta):
        (self.runs / name).mkdir(exist_ok=True)
        (self.runs / name / "run_metadata.json").write_text(json.dumps(meta))

    def runs_by_name(self):
        return {r["name"]: r for r in self.get_json("/api/runs")}

    def test_multi_device_run_reports_the_root_aggregate(self):
        self.write_meta("run_2", {
            "status": "completed_with_failures", "passed": 3, "failed": 1, "total": 4,
            "devices": [{"id": "device_1", "passed": 2, "failed": 0, "total": 2},
                        {"id": "ios_device_1", "passed": 1, "failed": 1, "total": 2}]})
        run = self.runs_by_name()["run_2"]
        self.assertEqual((run["passed"], run["failed"], run["total"]), (3, 1, 4))
        self.assertEqual(run["status"], "Fail")

    def test_runs_without_counts_report_zero(self):
        self.write_meta("run_3", {"status": "failed"})
        runs = self.runs_by_name()
        for name in ("run_1", "run_3"):  # run_1 has no metadata at all
            self.assertEqual((runs[name]["passed"], runs[name]["failed"], runs[name]["total"]), (0, 0, 0))

    def test_total_is_never_below_passed_plus_failed(self):
        self.write_meta("run_4", {"status": "completed", "passed": 2, "failed": 1, "total": 0})
        self.assertEqual(self.runs_by_name()["run_4"]["total"], 3)


class SnapshotMismatchProgressTests(ServerCase):
    """/api/pending adds every mismatched SNAPSHOT (with its percentage) to the
    progress object, so the Run status panel can show it."""

    def write_device(self, device, tests, results):
        d = self.runs / "run_1" / "devices" / device
        (d / "results").mkdir(parents=True)
        (d / "pending_tests.json").write_text(json.dumps({
            "tests": [{"name": t} for t in tests],
            "progress": {"phase": "completed_with_failures", "message": "Run finished"}}))
        for test, snaps in results.items():
            steps = [{"step": {"command": "SNAPSHOT"}, "status": "FAIL" if s["status"] == "mismatch" else "PASS",
                      "snapshot": s} for s in snaps]
            steps.insert(0, {"step": {"command": "CLICK"}, "status": "PASS"})
            (d / "results" / f"{test}_result.json").write_text(
                json.dumps({"test": test, "label": test.replace("__", " / "), "steps": steps}))

    def test_mismatches_carry_the_percentage_in_test_order(self):
        self.write_device("device_1", ["case__b", "case__a"], {
            "case__a": [{"name": "home", "status": "mismatch", "diff_percent": 2.8701,
                         "tolerance": 0.1, "screen_class": "ios/375x667@2x", "message": "m"}],
            "case__b": [{"name": "eula", "status": "match", "diff_percent": 0.0},
                        {"name": "settings", "status": "mismatch", "diff_percent": None,
                         "message": "baseline is 1x1, capture is 2x2 at 1x"}],
        })
        got = self.get_json("/api/pending/run_1?device=device_1")["progress"]["snapshot_mismatches"]
        self.assertEqual([(m["test"], m["name"], m["diff_percent"]) for m in got],
                         [("case__b", "settings", None), ("case__a", "home", 2.8701)])
        self.assertEqual(got[1]["label"], "case / a")
        self.assertEqual(got[1]["tolerance"], 0.1)

    def test_no_mismatch_gives_an_empty_list(self):
        self.write_device("device_1", ["case__a"], {
            "case__a": [{"name": "home", "status": "match", "diff_percent": 0.01}]})
        got = self.get_json("/api/pending/run_1?device=device_1")["progress"]
        self.assertEqual(got["snapshot_mismatches"], [])


class RunEventsTests(ServerCase):
    """/api/run-events/<run> pushes the run's state to test.html as
    server-sent events: once on connect, then only when the run changes."""

    def setUp(self):
        super().setUp()
        for name, value in (("RUN_EVENTS_CHECK_SECS", 0.05), ("RUN_EVENTS_KEEPALIVE_SECS", 0.3)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dev = self.runs / "run_1" / "devices" / "device_1"
        (self.dev / "results").mkdir(parents=True)
        (self.dev / "outputs").mkdir()
        self.write_pending("running")

    def write_pending(self, status):
        (self.dev / "pending_tests.json").write_text(json.dumps(
            {"tests": [{"name": "case__a", "status": status}]}))

    def open_stream(self, path):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", path, headers={"Cookie": TEST_COOKIE})
        return conn.getresponse()

    def next_message(self, resp):
        """The next SSE message: ("run", payload) or ("keepalive", None)."""
        lines = []
        while True:
            line = resp.fp.readline().decode()
            if not line:
                raise AssertionError("stream closed")
            if line == "\n":
                break
            lines.append(line.rstrip("\n"))
        if lines[0].startswith(":"):
            return "keepalive", None
        event = lines[0][len("event: "):]
        return event, json.loads(lines[1][len("data: "):])

    def test_unknown_or_invalid_run_is_404(self):
        for path in ("/api/run-events/run_9", "/api/run-events/foo"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)
            self.assertEqual(ctx.exception.code, 404)
            ctx.exception.close()

    def test_sends_state_on_connect_then_only_on_change(self):
        resp = self.open_stream("/api/run-events/run_1?device=device_1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        event, data = self.next_message(resp)
        self.assertEqual(event, "run")
        self.assertEqual(data["pending"]["tests"][0]["status"], "running")

        # Nothing changed: only a keepalive.
        self.assertEqual(self.next_message(resp), ("keepalive", None))

        # Files the page doesn't show don't wake it up.
        (self.runs / "run_1" / "console.log").write_text("line")
        (self.dev / "outputs" / "shot.png").write_bytes(b"x")
        self.assertEqual(self.next_message(resp), ("keepalive", None))

        # A pending-state change is pushed, with the device's new state.
        time.sleep(0.01)
        self.write_pending("completed")
        event, data = self.next_message(resp)
        self.assertEqual(event, "run")
        self.assertEqual(data["pending"]["tests"][0]["status"], "completed")

        # So are a new result file and a finished recording.
        (self.dev / "results" / "case__a_result.json").write_text("{}")
        self.assertEqual(self.next_message(resp)[0], "run")
        (self.dev / "outputs" / "case__a.mp4").write_bytes(b"x")
        self.assertEqual(self.next_message(resp)[0], "run")

    def test_another_devices_change_is_pushed_too(self):
        other = self.runs / "run_1" / "devices" / "ios_device_1"
        other.mkdir()
        resp = self.open_stream("/api/run-events/run_1?device=device_1")
        self.assertEqual(self.next_message(resp)[0], "run")
        (other / "pending_tests.json").write_text('{"tests": []}')
        event, data = self.next_message(resp)
        self.assertEqual(event, "run")
        # The payload is still the selected device's.
        self.assertEqual(data["pending"]["tests"][0]["name"], "case__a")

    def test_payload_matches_api_pending(self):
        resp = self.open_stream("/api/run-events/run_1?device=device_1")
        _, data = self.next_message(resp)
        self.assertEqual(data["pending"], self.get_json("/api/pending/run_1?device=device_1"))


class ConfiguredDeviceNameTests(ServerCase):
    """A name set in the Device Config section on /plugins replaces the runner-recorded name on the run page."""

    def setUp(self):
        super().setUp()
        names = self.root / "device_names.json"
        names.write_text(json.dumps({"ZA2235GDS8": "Motorola 2025",
                                     "00008030-000C50D42EE3802E": "iPhone SE 2nd"}))
        patcher = patch.object(app, "CONFIG_DEVICES_FILE", names)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_meta(self, name, meta):
        (self.runs / name).mkdir(exist_ok=True)
        (self.runs / name / "run_metadata.json").write_text(json.dumps(meta))

    def test_multi_device_run_uses_configured_names(self):
        self.write_meta("run_2", {"devices": [
            {"id": "device_1", "serial": "ZA2235GDS8", "name": "moto g power - 2025"},
            {"id": "ios_device_1", "serial": "ios:00008030-000C50D42EE3802E", "name": "iPhone"},
            {"id": "device_2", "serial": "192.168.1.9:5555", "name": "SM-A155F"}]})
        names = [d["name"] for d in self.get_json("/api/test-devices/run_2/t")["devices"]]
        self.assertEqual(names, ["Motorola 2025", "iPhone SE 2nd", "SM-A155F"])
        summary = [d["name"] for d in self.get_json("/api/device-summary/run_2")["devices"]]
        self.assertEqual(summary, ["Motorola 2025", "iPhone SE 2nd", "SM-A155F"])

    def test_single_device_run_uses_configured_name(self):
        self.write_meta("run_3", {"device": {"serial": "ZA2235GDS8", "name": "moto g power - 2025"}})
        self.assertEqual(self.get_json("/api/test-devices/run_3/t")["devices"][0]["name"], "Motorola 2025")
        self.assertEqual(self.get_json("/api/device-summary/run_3")["devices"][0]["name"], "Motorola 2025")
        self.assertEqual(self.get_json("/api/metadata/run_3")["device"]["display_name"], "Motorola 2025")

    def test_unconfigured_device_keeps_recorded_name(self):
        self.write_meta("run_4", {"device": {"serial": "OTHER1", "name": "Pixel 8"}})
        self.assertEqual(self.get_json("/api/metadata/run_4")["device"]["display_name"], "Pixel 8")


class RunOrderTests(ServerCase):
    """Runs execute one after another, in submission order: Android and iOS
    runs share the one ESP32 board."""

    def submit(self):
        add_build()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "submit", Path(app.__file__).parent / "ci" / "mt-submit" / "submit.py")
        submit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(submit)
        ctype, body = submit.build_multipart(
            {"device_mode": "specific", "device": "10.0.0.1:5555", "build_request": BUILD_ID},
            [("apk", "app.apk", b"PK-not-really"),
             ("tests", "INSTRUCTION.txt", b"MODE: once\\n"),
             ("tests", "a.txt", b"VERIFY: x\\n")])
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["run_id"]

    def test_each_run_waits_for_the_one_submitted_before_it(self):
        script = self.root / "fake_runner.py"
        script.write_text(FAKE_RUNNER)
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)
        runs = [self.submit() for _ in range(3)]  # e.g. Android, iOS, Android
        self.assertEqual(runs, ["run_2", "run_3", "run_4"])
        deadline = time.time() + 20
        while time.time() < deadline and not all(
                (self.runs / r / "exit_code").exists() for r in runs):
            time.sleep(0.1)
        info = [json.loads((self.runs / r / "fake.json").read_text()) for r in runs]
        self.assertNotIn("--after-pid", info[0]["argv"])
        for before, after, prev_id in zip(info, info[1:], runs):
            argv = after["argv"]
            self.assertEqual(argv[argv.index("--after-pid") + 1], str(before["pid"]))
            self.assertEqual(argv[argv.index("--after-run") + 1], prev_id)
            self.assertGreaterEqual(after["start"], before["end"])

    def test_no_wait_when_the_previous_run_is_finished(self):
        script = self.root / "fake_runner.py"
        script.write_text(FAKE_RUNNER)
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)
        first = self.submit()
        deadline = time.time() + 10
        while time.time() < deadline and not (self.runs / first / "exit_code").exists():
            time.sleep(0.1)
        second = self.submit()
        deadline = time.time() + 10
        while time.time() < deadline and not (self.runs / second / "fake.json").exists():
            time.sleep(0.1)
        argv = json.loads((self.runs / second / "fake.json").read_text())["argv"]
        self.assertNotIn("--after-pid", argv)



class CombinedRunTests(ServerCase):
    """One run can carry an Android .apk and an iOS .ipa; each platform's
    devices get their own app."""

    def setUp(self):
        super().setUp()
        script = self.root / "fake_runner.py"
        script.write_text(FAKE_RUNNER)
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, files, targets, mode="all"):
        add_build()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "submit", Path(app.__file__).parent / "ci" / "mt-submit" / "submit.py")
        submit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(submit)
        ctype, body = submit.build_multipart(
            {"device_mode": mode, "device": "x", "build_request": BUILD_ID},
            files + [("tests", "INSTRUCTION.txt", b"MODE: once\n"), ("tests", "a.txt", b"VERIFY: x\n")])
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        result = (targets, None) if targets else (None, "Mobile Controller unreachable")
        with patch.object(app, "resolve_run_targets", return_value=result):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

    def argv_of(self, run_id):
        fake = self.runs / run_id / "fake.json"
        deadline = time.time() + 10
        while time.time() < deadline and not fake.exists():
            time.sleep(0.1)
        return json.loads(fake.read_text())["argv"]

    def test_both_apps_start_one_run_that_waits_for_both_platforms(self):
        status, body = self.post(
            [("apk", "app.apk", b"PK"), ("ios_app", "app.ipa", b"IPA")],
            {"android": ["ZA2235GDS8"], "ios": ["00008030-000C50D42EE3802E"], "ios_placeholder": False})
        self.assertEqual(status, 200, body)
        argv = self.argv_of(body["run_id"])
        self.assertTrue(argv[argv.index("--apk") + 1].endswith("app.apk"))
        self.assertTrue(argv[argv.index("--ios-app") + 1].endswith("app.ipa"))
        # The run picks the devices when its turn comes, not the upload.
        self.assertNotIn("--device", argv)
        waits = [argv[i + 1] for i, a in enumerate(argv) if a == "--wait-for-platform"]
        self.assertEqual(waits, ["android", "ios"])

    def test_upload_is_queued_even_with_no_devices_connected(self):
        status, body = self.post(
            [("apk", "app.apk", b"PK"), ("ios_app", "app.ipa", b"IPA")],
            None)  # resolve_run_targets would fail: it must not be consulted
        self.assertEqual(status, 200, body)
        self.assertIn("--wait-for-platform", self.argv_of(body["run_id"]))

    def test_two_apps_of_one_platform_are_rejected(self):
        status, body = self.post(
            [("apk", "a.apk", b"PK"), ("ios_app", "b.apk", b"PK")],
            {"android": ["ZA2235GDS8"], "ios": [], "ios_placeholder": False})
        self.assertEqual(status, 400)
        self.assertIn("Only one Android", body["error"])

    def test_ios_only_upload_waits_for_an_iphone(self):
        status, body = self.post(
            [("apk", "app.ipa", b"IPA")],
            {"android": [], "ios": ["00008030-000C50D42EE3802E"], "ios_placeholder": False},
            mode="ios")
        self.assertEqual(status, 200, body)
        argv = self.argv_of(body["run_id"])
        self.assertNotIn("--apk", argv)
        self.assertTrue(argv[argv.index("--ios-app") + 1].endswith("app.ipa"))
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--wait-for-platform"], ["ios"])

    def test_specific_devices_still_need_matching_apps(self):
        status, body = self.post(
            [("apk", "app.apk", b"PK"), ("ios_app", "app.ipa", b"IPA")],
            {"android": ["ZA2235GDS8"], "ios": [], "ios_placeholder": False},
            mode="specific")
        self.assertEqual(status, 400)
        self.assertIn("pick an Android device and an iPhone", body["error"])


class SettingsSubmenuDismissTests(ServerCase):
    """Clicking a Settings submenu item collapses the flyout (PR rule): the
    CSS override and the localStorage-backed flag ship on every page."""

    def test_dismiss_code_ships_on_every_page(self):
        for path in ("/", "/logs.html"):
            body = raw_get(self.port, path)[1]
            for marker in (b':root[data-submenu-dismissed="true"] .site-nav-settings .site-nav-submenu',
                           b"mt-submenu-dismissed",
                           b"addEventListener('mouseenter'",
                           b"addEventListener('focusin'"):
                self.assertIn(marker, body, f"{path} missing {marker!r}")


class InViewBackButtonTests(ServerCase):
    """Native-back windows (MT_NATIVE_BACK) get a floating back arrow inside
    the page content, never a Back entry in the sidebar nav. The home page
    ships the arrow greyed out (disabled) instead of hiding it."""

    def test_back_button_code_ships_on_every_page(self):
        for path in ("/", "/logs.html"):
            body = raw_get(self.port, path)[1]
            for marker in (b"mt-back-btn", b"function renderBackButton()",
                           b".mt-back-btn[disabled]",
                           b"body:has(#mt-back-btn)"):
                self.assertIn(marker, body, f"{path} missing {marker!r}")

    def test_nav_no_longer_carries_a_back_link(self):
        body = raw_get(self.port, "/")[1]
        self.assertNotIn(b'onclick="history.back()"', body)


LIVE_CONFIG = {"theme": "dark", "video_size": "100%",
               "visual_endpoint": "http://192.168.1.20:8091/v1/chat/completions",
               "visual_model": "qwen2.5vl:7b", "visual_api_key": "live-pair-tok-4242"}
LIVE_MT_PLUGINS = ("esp32-sensor=http://127.0.0.1:8091,esp32-server=http://127.0.0.1:8093,"
                   "monkeytester-ai=http://192.168.1.20:8091")


class AiAddressImportTests(unittest.TestCase):
    """At viewer start a monkeytester-ai MT_PLUGINS entry is moved once into
    config.json (visual_plugin_url); after that the entry is ignored."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg_file = Path(tmp.name) / "Run" / "config.json"
        self.cfg_file.parent.mkdir()
        p = patch.object(app, "CONFIG_FILE", self.cfg_file)
        p.start()
        self.addCleanup(p.stop)

    def run_import(self, env):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            url = app.import_ai_plugin_address(env)
        return url, err.getvalue()

    def test_live_config_and_env_import_once(self):
        """Exactly the live Run/config.json (legacy visual_endpoint, no
        visual_plugin) and the live MT_PLUGINS."""
        self.cfg_file.write_text(json.dumps(LIVE_CONFIG))
        env = {"MT_PLUGINS": LIVE_MT_PLUGINS}
        url, log = self.run_import(env)
        self.assertEqual(url, "http://192.168.1.20:8091")
        self.assertIn("imported monkeytester-ai address from MT_PLUGINS; the MT_PLUGINS "
                      "entry is ignored from now on", log)
        cfg = json.loads(self.cfg_file.read_text())
        self.assertEqual(cfg, {**LIVE_CONFIG, "visual_plugin_url": "http://192.168.1.20:8091",
                               "visual_plugin": "monkeytester-ai"})
        # Idempotent: a second start imports nothing, writes nothing, says the
        # env entry is ignored.
        mtime = self.cfg_file.stat().st_mtime_ns
        url, log = self.run_import(env)
        self.assertEqual(url, "")
        self.assertIn("is ignored", log)
        self.assertEqual(self.cfg_file.stat().st_mtime_ns, mtime)
        # A changed env entry is ignored from now on; the other plugins still
        # come from MT_PLUGINS.
        env = {"MT_PLUGINS": LIVE_MT_PLUGINS.replace("192.168.1.20", "10.9.9.9")}
        self.assertEqual(self.run_import(env)[0], "")
        self.assertEqual(app.ai_plugin_url(), "http://192.168.1.20:8091")
        pc = app._plugin_client()
        plugins = pc.viewer_plugins(env, app.ai_plugin_url(), "live-pair-tok-4242")
        self.assertEqual({n: c.url for n, c in plugins.items()},
                         {"esp32-sensor": "http://127.0.0.1:8091",
                          "esp32-server": "http://127.0.0.1:8093",
                          "monkeytester-ai": "http://192.168.1.20:8091"})
        self.assertEqual(plugins["monkeytester-ai"].token, "live-pair-tok-4242")

    def test_import_without_env_entry_or_config_does_nothing_harmful(self):
        self.assertEqual(self.run_import({"MT_PLUGINS": "esp32-sensor=http://127.0.0.1:8091"})[0], "")
        self.assertFalse(self.cfg_file.exists())
        self.assertEqual(self.run_import({"MT_PLUGINS": "broken"})[0], "")
        self.assertFalse(self.cfg_file.exists())
        # No config.json at all yet: the import creates one with just the address.
        url, _ = self.run_import({"MT_PLUGINS": "monkeytester-ai=http://m:8091/"})
        self.assertEqual(url, "http://m:8091")
        self.assertEqual(json.loads(self.cfg_file.read_text()),
                         {"visual_plugin_url": "http://m:8091", "visual_plugin": "monkeytester-ai"})

    def test_config_write_is_atomic_and_private(self):
        # config.json holds the pairing and build-plugin tokens: every write
        # makes it 0600, even when an older version left it group-readable.
        self.cfg_file.write_text(json.dumps(LIVE_CONFIG))
        os.chmod(self.cfg_file, 0o640)
        self.run_import({"MT_PLUGINS": LIVE_MT_PLUGINS})
        self.assertEqual(self.cfg_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.cfg_file.parent.iterdir()], ["config.json"])
        with patch("json.dump", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                app.save_config({"x": 1})
        # The old file is intact and no temp file is left behind.
        self.assertEqual(json.loads(self.cfg_file.read_text())["visual_plugin"], "monkeytester-ai")
        self.assertEqual([p.name for p in self.cfg_file.parent.iterdir()], ["config.json"])


class ManagedAiPluginTests(ServerCase):
    """Managed mode: a launcher app passes MT_MANAGED_AI_PLUGIN_URL
    and MT_MANAGED_AI_PLUGIN_TOKEN (both required). They win over config.json
    for every plugin call, are never written to config.json, the MT_PLUGINS
    import is skipped, and address/token/removal edits are refused (409)."""

    STALE = {"visual_plugin_url": "http://127.0.0.1:9", "visual_api_key": "stale-tok-0000",
             "theme": "dark"}

    def setUp(self):
        super().setUp()
        AiPlugin.state = ai_state()
        AiPlugin.state["require_token"] = True  # the plugin enforces pairing
        AiPlugin.posts = []
        AiPlugin.auth = []
        app._PLUGIN_SELECTED_CACHE.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), AiPlugin)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        self.cfg_path = self.runs / "config.json"

    def managed(self, url=None, token="secret-tok-9876"):
        return patch.dict(os.environ, {"MT_MANAGED_AI_PLUGIN_URL": self.url + "/" if url is None else url,
                                       "MT_MANAGED_AI_PLUGIN_TOKEN": token,
                                       "MT_PLUGINS": ""})

    def write_stale(self):
        self.cfg_path.write_text(json.dumps(self.STALE))
        return self.cfg_path.read_bytes()

    def test_plugins_endpoint_refuses_ai_set_and_remove_when_managed(self):
        before = self.write_stale()
        with self.managed():
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": self.url}, "row": "monkeytester-ai"})
            self.assertEqual(status, 409, body)
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"remove": "monkeytester-ai"})
            self.assertEqual(status, 409, body)
        self.assertEqual(self.cfg_path.read_bytes(), before)

    def test_managed_url_and_token_win_over_config(self):
        before = self.write_stale()
        with self.managed():
            self.assertEqual(app.ai_plugin_url(), self.url)
            self.assertEqual(app.ai_pairing_token(), "secret-tok-9876")
            self.assertEqual(app.visual_plugin_name(), "monkeytester-ai")
            endpoint, key, _ = app.vision_settings()
            info = self.get_json("/api/visual-model/plugin")
            out = self.get_json("/api/plugins")
            vm = self.get_json("/api/config/visual-model")
        self.assertEqual((endpoint, key), (self.url + "/v1/chat/completions", "secret-tok-9876"))
        # The token-enforcing plugin answered: the env token was sent, not config.json's.
        self.assertTrue(info["ok"], info)
        self.assertTrue(info["managed"])
        self.assertEqual((info["plugin"], info["url"]), ("monkeytester-ai", self.url))
        self.assertIn("secret-tok-9876", AiPlugin.auth)
        self.assertNotIn("stale-tok-0000", AiPlugin.auth)
        row = {p["name"]: p for p in out["plugins"]}["monkeytester-ai"]
        self.assertEqual((row["url"], row["ok"], row["managed"]), (self.url, True, True))
        self.assertTrue(row["config"])
        self.assertEqual(out["ai_plugin"]["url"], self.url)
        self.assertTrue(out["ai_plugin"]["managed"])
        self.assertEqual((vm["plugin"], vm["plugin_url"], vm["managed"]),
                         ("monkeytester-ai", self.url, True))
        self.assertNotIn("secret-tok-9876", json.dumps([info, out, vm]))
        self.assertEqual(self.cfg_path.read_bytes(), before)  # config.json untouched

    def test_selected_without_visual_plugin_in_config(self):
        with self.managed():  # no config.json at all
            out = self.get_json("/api/plugins")
            status = self.get_json("/api/status")
        self.assertTrue({p["name"]: p for p in out["plugins"]}["monkeytester-ai"]["ok"])
        self.assertEqual((status["vision"]["plugin"], status["vision"]["endpoint"]),
                         ("monkeytester-ai", self.url))
        self.assertTrue(status["vision"]["ok"])
        self.assertFalse(self.cfg_path.exists())

    def test_configuration_save_still_works_and_never_writes_url_or_token(self):
        self.write_stale()
        with self.managed():
            status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                      {"provider": "local", "model": "llava"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["plugin_url"], self.url)
        self.assertEqual(AiPlugin.state["selected"], "llava")
        cfg = json.loads(self.cfg_path.read_text())
        self.assertEqual((cfg["visual_plugin_url"], cfg["visual_api_key"]),
                         ("http://127.0.0.1:9", "stale-tok-0000"))
        self.assertNotIn(self.url, json.dumps(cfg))
        self.assertNotIn("secret-tok-9876", json.dumps(cfg))

    def test_address_token_and_remove_edits_are_refused(self):
        before = self.write_stale()
        bodies = [{"plugin_url": "http://127.0.0.1:1234"}, {"plugin_url": ""},
                  {"set_token": "x-1234"}, {"generate_token": True}, {"clear_token": True},
                  {"api_key": "k"}, {"clear_api_key": True},
                  {"provider": "local", "generate_token": True}]
        with self.managed():
            for payload in bodies:
                status, _, body = request(self.port, "POST", "/api/config/visual-model", payload)
                self.assertEqual((status, body), (409, {"error": "managed by the MonkeyTester app"}),
                                 payload)
        self.assertEqual(self.cfg_path.read_bytes(), before)
        self.assertEqual(AiPlugin.posts, [])  # nothing reached the plugin (no rotation)
        self.assertEqual(AiPlugin.state["token"], "secret-tok-9876")

    def test_import_is_skipped(self):
        import contextlib
        import io
        err = io.StringIO()
        with self.managed(), contextlib.redirect_stderr(err):
            url = app.import_ai_plugin_address({"MT_PLUGINS": "monkeytester-ai=http://m:8091",
                                                "MT_MANAGED_AI_PLUGIN_URL": self.url,
                                                "MT_MANAGED_AI_PLUGIN_TOKEN": "t"})
        self.assertEqual(url, "")
        self.assertEqual(err.getvalue(), "")
        self.assertFalse(self.cfg_path.exists())

    def test_feature_off_when_only_one_variable_is_set(self):
        before = self.write_stale()
        for url, token in ((self.url, ""), ("", "secret-tok-9876")):
            with self.subTest(url=url, token=token), self.managed(url=url, token=token):
                self.assertIsNone(app.managed_ai_plugin())
                self.assertEqual(app.ai_plugin_url(), "http://127.0.0.1:9")
                self.assertEqual(app.ai_pairing_token(), "stale-tok-0000")
                out = self.get_json("/api/plugins")
                row = {p["name"]: p for p in out["plugins"]}["monkeytester-ai"]
                self.assertEqual((row["url"], row["managed"]), ("http://127.0.0.1:9", False))
                self.assertFalse(out["ai_plugin"]["managed"])
                status, _, body = request(self.port, "POST", "/api/config/visual-model",
                                          {"clear_token": True})
                self.assertNotEqual(status, 409, body)
                self.cfg_path.write_bytes(before)
        with patch.dict(os.environ, {"MT_PLUGINS": "monkeytester-ai=http://m:8091"}):
            self.cfg_path.unlink()
            self.assertEqual(app.import_ai_plugin_address({"MT_PLUGINS": "monkeytester-ai=http://m:8091",
                                                           "MT_MANAGED_AI_PLUGIN_URL": self.url}),
                             "http://m:8091")

    def test_runner_applies_the_same_rule(self):
        """run_on_device.py resolves the vision endpoint/key at import: the
        managed env wins over config.json, and is off with one var missing."""
        import subprocess
        import sys
        scripts = Path(app.__file__).resolve().parent / "mt-tests" / "scripts"
        probe = ("import os, sys; sys.argv=['x']\n"
                 "try:\n    import run_on_device\nexcept SystemExit:\n    pass\n"
                 "print(os.environ.get('VISION_MODEL_API_URL', ''), os.environ.get('VISUAL_MODEL_API_KEY', ''))")
        # run_on_device reads <repo>/Run/config.json, which a test can't
        # redirect; managed mode must win whatever that file holds.

        def resolve(**extra):
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("MT_MANAGED_", "VISION_MODEL", "VISUAL_MODEL"))}
            env.update(extra)
            res = subprocess.run([sys.executable, "-c", probe], cwd=scripts, env=env,
                                 capture_output=True, text=True, timeout=60)
            return res.stdout.strip().splitlines()[-1] if res.stdout.strip() else res.stderr[-500:]

        self.assertEqual(resolve(MT_MANAGED_AI_PLUGIN_URL=self.url + "/",
                                 MT_MANAGED_AI_PLUGIN_TOKEN="secret-tok-9876"),
                         f"{self.url}/v1/chat/completions secret-tok-9876")
        self.assertNotIn(self.url, resolve(MT_MANAGED_AI_PLUGIN_URL=self.url))


class ControllerPluginManageTests(ServerCase):
    """Settings → Plugins → Controller Plugin registers the Mobile Controller
    itself (config.json controller_url). The key's presence is the ownership
    marker, like the plugins map's: while it is absent the CONTROLLER_URL
    environment applies, the first write makes Settings own the slot forever
    (an empty value = removed, the env does not come back), and the runner
    always gets the RESOLVED value re-exported into its environment."""

    def test_reports_the_environment_until_the_first_write(self):
        with patch.dict(os.environ, {"CONTROLLER_URL": self.peer_url}):
            data = self.get_json("/api/config/controller")
        self.assertTrue(data["configured"])
        self.assertEqual((data["source"], data["url"]), ("env", self.peer_url))
        self.assertFalse((self.runs / "config.json").exists())

    def test_set_requires_the_controller_to_answer_devices(self):
        status, _, out = request(self.port, "POST", "/api/config/controller",
                                 {"set": {"url": self.peer_url + "/"}})
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["set"], self.peer_url)
        cfg = json.loads((self.runs / "config.json").read_text())
        self.assertEqual(cfg["controller_url"], self.peer_url)
        with patch.dict(os.environ, {"CONTROLLER_URL": "http://env-only:8090"}):
            data = self.get_json("/api/config/controller")
        self.assertEqual((data["source"], data["url"]), ("settings", self.peer_url))

    def test_dead_address_is_refused(self):
        status, _, body = request(self.port, "POST", "/api/config/controller",
                                  {"set": {"url": "http://127.0.0.1:9"}})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "unreachable")
        self.assertFalse((self.runs / "config.json").exists())

    def test_token_walled_controller_needs_the_service_token_first(self):
        Peer.status = 401
        status, _, body = request(self.port, "POST", "/api/config/controller",
                                  {"set": {"url": self.peer_url}})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "token_required")
        self.assertFalse((self.runs / "config.json").exists())

    def test_remove_owns_the_slot_again(self):
        with patch.dict(os.environ, {"CONTROLLER_URL": self.peer_url}):
            status, _, out = request(self.port, "POST", "/api/config/controller",
                                     {"remove": True})
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"], "mobile-controller")
        cfg = json.loads((self.runs / "config.json").read_text())
        self.assertEqual(cfg["controller_url"], "")
        with patch.dict(os.environ, {"CONTROLLER_URL": self.peer_url}):
            data = self.get_json("/api/config/controller")
        self.assertFalse(data["configured"])
        self.assertEqual(data["source"], "settings")


class CaseSummaryTests(ServerCase):
    """GET /api/cases/<run>: per-test-suite summaries for the run page."""

    def test_unknown_run(self):
        self.assertEqual(self.get_json("/api/cases/run_missing"),
                         {"cases": [], "single": True})

    def test_legacy_flat_run_stays_single(self):
        run = self.runs / "run_9001"
        (run / "results").mkdir(parents=True)
        (run / "results" / "login_initial_result.json").write_text(json.dumps(
            {"test": "login_initial", "status": "PASS"}))
        data = self.get_json("/api/cases/run_9001")
        self.assertTrue(data["single"])
        self.assertEqual([c["case"] for c in data["cases"]], [""])

    def test_single_named_case_stays_single(self):
        run = self.runs / "run_9002"
        (run / "results").mkdir(parents=True)
        for name, status in (("a", "PASS"), ("b", "FAIL")):
            (run / "results" / f"onboarding__{name}_result.json").write_text(json.dumps(
                {"test": f"onboarding__{name}", "case": "onboarding", "status": status}))
        data = self.get_json("/api/cases/run_9002")
        self.assertTrue(data["single"])
        one = data["cases"][0]
        self.assertEqual((one["case"], one["status"], one["passed"],
                          one["failed"], one["total"]), ("onboarding", "fail", 1, 1, 2))

    def test_multi_case_multi_device_aggregate(self):
        # Two suites on two devices: results win over progress state in the
        # same device dir; queued suites still get a row (from metadata).
        run = self.runs / "run_9003"
        run.mkdir()
        (run / "run_metadata.json").write_text(json.dumps(
            {"status": "running", "cases": ["onboarding", "eula"]}))
        for dev in ("device_1", "device_2"):
            (run / "devices" / dev / "results").mkdir(parents=True)
        d1 = run / "devices" / "device_1"
        (d1 / "results" / "onboarding__01_result.json").write_text(json.dumps(
            {"test": "onboarding__01", "case": "onboarding", "status": "PASS"}))
        (d1 / "results" / "onboarding__02_result.json").write_text(json.dumps(
            {"test": "onboarding__02", "case": "onboarding", "status": "FAIL"}))
        (run / "devices" / "device_2" / "pending_tests.json").write_text(json.dumps(
            {"tests": [{"name": "onboarding__01", "case": "onboarding", "status": "running"},
                       {"name": "eula__01", "case": "eula", "status": "pending"}]}))
        data = self.get_json("/api/cases/run_9003")
        self.assertFalse(data["single"])
        self.assertEqual([c["case"] for c in data["cases"]], ["onboarding", "eula"])
        byc = {c["case"]: c for c in data["cases"]}
        ob = byc["onboarding"]
        self.assertEqual((ob["status"], ob["passed"], ob["failed"],
                          ob["running"], ob["pending"], ob["total"]),
                         ("running", 1, 1, 1, 0, 3))
        eula = byc["eula"]
        self.assertEqual((eula["status"], eula["pending"], eula["total"]),
                         ("pending", 1, 1))

    def test_cancelled_and_unavailable_count_as_failed(self):
        run = self.runs / "run_9004"
        (run / "results").mkdir(parents=True)
        (run / "results" / "alpha__one_result.json").write_text(json.dumps(
            {"test": "alpha__one", "case": "alpha", "status": "UNAVAILABLE"}))
        (run / "pending_tests.json").write_text(json.dumps(
            {"tests": [{"name": "alpha__two", "case": "alpha", "status": "cancelled"},
                       {"name": "beta__one", "case": "beta", "status": "pending"}]}))
        data = self.get_json("/api/cases/run_9004")
        self.assertFalse(data["single"])
        byc = {c["case"]: c for c in data["cases"]}
        self.assertEqual(byc["alpha"]["status"], "fail")
        self.assertEqual(byc["alpha"]["unavailable"] + byc["alpha"]["cancelled"], 2)
        self.assertEqual(byc["beta"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
