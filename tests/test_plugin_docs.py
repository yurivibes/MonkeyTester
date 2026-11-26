"""GET /plugins/docs/<name>: the viewer proxies a plugin's GET /docs page
(PLUGIN_API.md "Documentation"), and /api/plugins carries `docs`."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import app
import security_utils as sec
import viewer_auth as auth
from tests.test_app_api import ServerCase, SignedInHandler, raw_get

CONTRACT_HEALTH = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                   "plugin": {"name": "esp32-sensor", "version": "1.0.0"}}
DOCS_PAGE = b"<!DOCTYPE html><html><body><h1>ESP32 sensor</h1><script>alert(1)</script></body></html>"


class DocsPlugin(BaseHTTPRequestHandler):
    """A plugin that serves /health (+ docs field) and GET /docs."""
    health = {}
    docs_status = 200
    docs_type = "text/html; charset=utf-8"
    docs_body = DOCS_PAGE
    docs_location: "str | None" = None
    seen = []  # (path, X-Plugin-Token, X-Controller-Token)

    def log_message(self, *args):
        pass

    def do_GET(self):
        DocsPlugin.seen.append((self.path, self.headers.get("X-Plugin-Token"),
                                self.headers.get("X-Controller-Token")))
        if self.path == "/health":
            body = json.dumps(DocsPlugin.health).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/devices":
            body = b'{"devices": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/docs":
            body = DocsPlugin.docs_body
            self.send_response(DocsPlugin.docs_status)
            self.send_header("Content-Type", DocsPlugin.docs_type)
            if DocsPlugin.docs_location:
                self.send_header("Location", DocsPlugin.docs_location)
        else:
            body = b'{"error": "not found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PluginDocsCase(ServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.docs = ThreadingHTTPServer(("127.0.0.1", 0), DocsPlugin)
        cls.docs_url = f"http://127.0.0.1:{cls.docs.server_address[1]}"
        threading.Thread(target=cls.docs.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.docs.shutdown()
        cls.docs.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        DocsPlugin.health = dict(CONTRACT_HEALTH, docs="/docs")
        DocsPlugin.docs_status, DocsPlugin.docs_type = 200, "text/html; charset=utf-8"
        DocsPlugin.docs_body, DocsPlugin.docs_location, DocsPlugin.seen = DOCS_PAGE, None, []
        (self.runs / "config.json").write_text(json.dumps({"plugins": {"esp32-sensor": self.docs_url}}))
        auth.save_tokens({"plugins": {"esp32-sensor": {"token": "esp-token-1234", "saved_at": 1}}})

    def fetch(self, path, auth_=True):
        """(status, headers, body) without following redirects."""
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        handlers: list = [NoRedirect()]
        if auth_:
            handlers.append(SignedInHandler(self.port))
        else:
            req.add_header("Cookie", "none=1")
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(req, timeout=20) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()


class ProxyTests(PluginDocsCase):
    def test_passes_the_page_through_under_a_sandbox_csp(self):
        status, headers, body = self.fetch("/plugins/docs/esp32-sensor")
        self.assertEqual(status, 200)
        self.assertEqual(body, DOCS_PAGE)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        csp = headers["Content-Security-Policy"]
        self.assertEqual(csp, app.PLUGIN_DOCS_CSP)
        self.assertTrue(csp.endswith("; sandbox"))
        self.assertIn("default-src 'none'", csp)
        self.assertEqual(headers.get_all("Referrer-Policy"), ["no-referrer"])
        self.assertEqual(headers.get_all("X-Content-Type-Options"), ["nosniff"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_sends_the_plugins_saved_token(self):
        self.fetch("/plugins/docs/esp32-sensor")
        self.assertIn(("/docs", "esp-token-1234", None), DocsPlugin.seen)

    def test_controller_gets_its_controller_token(self):
        (self.runs / "config.json").write_text(json.dumps({"controller_url": self.docs_url}))
        auth.save_tokens({"plugins": {}, "controller": {"token": "ctl-token-5678", "saved_at": 1}})
        status, _, body = self.fetch("/plugins/docs/controller")
        self.assertEqual((status, body), (200, DOCS_PAGE))
        self.assertIn(("/docs", None, "ctl-token-5678"), DocsPlugin.seen)

    def test_ai_plugin_gets_its_pairing_token(self):
        (self.runs / "config.json").write_text(json.dumps({
            "plugins": {}, "visual_plugin_url": self.docs_url, "visual_plugin": "monkeytester-ai",
            "visual_api_key": "ai-pairing-9999"}))
        status, _, _ = self.fetch("/plugins/docs/monkeytester-ai")
        self.assertEqual(status, 200)
        self.assertIn(("/docs", "ai-pairing-9999", None), DocsPlugin.seen)

    def test_build_plugin(self):
        (self.runs / "config.json").write_text(json.dumps({
            "plugins": {"build-local": {"url": self.docs_url, "token": ""}}}))
        auth.save_tokens({"plugins": {"build-local": {"token": "build-tok-4321", "saved_at": 1}}})
        status, _, _ = self.fetch("/plugins/docs/build-local")
        self.assertEqual(status, 200)
        self.assertIn(("/docs", "build-tok-4321", None), DocsPlugin.seen)

    def test_unknown_name_is_404_without_contacting_anything(self):
        for name in ("nope", "bad%20name", "a" * 65, "controller"):
            status, headers, body = self.fetch("/plugins/docs/" + name)
            self.assertEqual(status, 404, name)
            self.assertTrue(headers["Content-Type"].startswith("text/html"), name)
        self.assertEqual(DocsPlugin.seen, [])

    def test_invalid_or_missing_docs_path_is_404(self):
        for docs in ("//evil.example/docs", "http://x/docs", "docs", "/a\\b", "/a\r\nb",
                     "/" + "x" * 300, 5, None):
            DocsPlugin.health = dict(CONTRACT_HEALTH, docs=docs)
            if docs is None:
                DocsPlugin.health.pop("docs")
            DocsPlugin.seen = []
            status, _, body = self.fetch("/plugins/docs/esp32-sensor")
            self.assertEqual(status, 404, repr(docs))
            self.assertIn(b"no documentation", body)
            self.assertEqual([p for p, _, _ in DocsPlugin.seen], ["/health"], repr(docs))

    def test_redirect_is_502_and_not_followed(self):
        DocsPlugin.docs_status, DocsPlugin.docs_location = 302, "/health"
        status, _, body = self.fetch("/plugins/docs/esp32-sensor")
        self.assertEqual(status, 502)
        self.assertIn(b"redirect", body)
        self.assertEqual([p for p, _, _ in DocsPlugin.seen], ["/health", "/docs"])

    def test_non_html_is_502(self):
        DocsPlugin.docs_type = "application/json"
        status, _, body = self.fetch("/plugins/docs/esp32-sensor")
        self.assertEqual(status, 502)
        self.assertIn(b"Couldn&#x27;t load the documentation for esp32-sensor", body)
        self.assertIn(b'href="/plugins"', body)

    def test_plugin_error_status_is_502(self):
        DocsPlugin.docs_status = 404
        status, _, _ = self.fetch("/plugins/docs/esp32-sensor")
        self.assertEqual(status, 502)

    def test_oversize_is_502(self):
        with patch.object(app, "PLUGIN_DOCS_MAX_BYTES", 64):
            status, _, body = self.fetch("/plugins/docs/esp32-sensor")
        self.assertEqual(status, 502)
        self.assertIn(b"larger than", body)

    def test_offline_is_502(self):
        (self.runs / "config.json").write_text(json.dumps({"plugins": {"esp32-sensor": "http://127.0.0.1:9"}}))
        status, _, body = self.fetch("/plugins/docs/esp32-sensor")
        self.assertEqual(status, 502)
        self.assertIn(b"unreachable", body)

    def test_error_page_escapes_the_reason(self):
        DocsPlugin.docs_type = "text/<b>evil"
        _, _, body = self.fetch("/plugins/docs/esp32-sensor")
        self.assertNotIn(b"<b>evil", body)

    def test_unauthenticated_redirects_to_login(self):
        status, body = raw_get(self.port, "/plugins/docs/esp32-sensor", auth=False)
        self.assertEqual(status, 302)
        self.assertEqual(DocsPlugin.seen, [])
        status, headers, _ = self.fetch("/plugins/docs/esp32-sensor", auth_=False)
        self.assertEqual(status, 302)
        self.assertTrue(headers["Location"].startswith("/login"))


class StatusDataTests(PluginDocsCase):
    def test_api_plugins_carries_docs(self):
        DocsPlugin.health = dict(CONTRACT_HEALTH, docs="/docs")
        rows = {p["name"]: p for p in self.get_json("/api/plugins")["plugins"]}
        self.assertIs(rows["esp32-sensor"]["docs"], True)

    def test_api_plugins_docs_false_without_or_with_invalid_field(self):
        for health in (dict(CONTRACT_HEALTH), dict(CONTRACT_HEALTH, docs="//evil")):
            DocsPlugin.health = health
            rows = {p["name"]: p for p in self.get_json("/api/plugins")["plugins"]}
            self.assertIs(rows["esp32-sensor"]["docs"], False)

    def test_controller_live_carries_docs(self):
        (self.runs / "config.json").write_text(json.dumps({"controller_url": self.docs_url}))
        DocsPlugin.health = {"ok": True, "api_version": app.CONTROLLER_API_VERSION, "docs": "/docs"}
        self.assertIs(self.get_json("/api/config/controller")["live"]["docs"], True)
        DocsPlugin.health = {"ok": True, "api_version": app.CONTROLLER_API_VERSION}
        self.assertIs(self.get_json("/api/config/controller")["live"]["docs"], False)


class DocsPathTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(sec.plugin_docs_path("/docs"), "/docs")
        self.assertEqual(sec.plugin_docs_path("/api/docs?x=1"), "/api/docs?x=1")
        for bad in ("", "docs", "//x", "/\\x", "http://x", "https://x/docs", "/a\nb", "/a\rb",
                    "/x\x00", "/a://b", "/" + "a" * 256, None, 3, ["/docs"]):
            self.assertIsNone(sec.plugin_docs_path(bad), repr(bad))


if __name__ == "__main__":
    unittest.main()
