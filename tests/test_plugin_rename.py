"""Renamable plugins (PLUGIN_API.md: /health plugin.default_name + renamable,
POST /config {"name"}): the viewer identifies AI by its fixed
default_name, shows the reported name in the /plugins Name column, renames
through the Configure proxy routes (AI: /api/config/visual-model, proxied,
no re-key; device plugin: /api/config/plugins/<row>/config, re-keyed like an
address-edit rename), refuses a name another row uses (name_taken) and a
non-renamable plugin (not_renamable)."""
import json
import os
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
import viewer_auth as auth
from tests.test_app_api import AiPlugin, Peer, ServerCase, ai_state, request
from tests.test_panel_staging import run_staging

ROOT = Path(__file__).resolve().parents[1]
NAME_RE = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"


def plugin_client():
    return app._plugin_client()


class DevicePlugin(BaseHTTPRequestHandler):
    """A fake renamable device plugin that ENFORCES its pairing token on
    every route but /health and persists the rename in its state."""
    state = {}
    posts = []

    @staticmethod
    def reset(**kw):
        DevicePlugin.state = {"name": "LAB-SENSOR", "default_name": "ESP32-SENSOR",
                              "renamable": True, "token": "dev-token-4321"}
        DevicePlugin.state.update(kw)
        DevicePlugin.posts = []

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        if self.path == "/health":
            return True
        want = DevicePlugin.state["token"]
        if not want or self.headers.get("X-Plugin-Token") == want:
            return True
        self._send(401, {"error": "pairing token required", "code": "unauthorized"})
        return False

    def do_GET(self):
        if not self._authorized():
            return
        st = DevicePlugin.state
        if self.path == "/health":
            plugin = {"name": st["name"], "version": "1.0"}
            if st["default_name"] is not None:
                plugin["default_name"] = st["default_name"]
            if st["renamable"] is not None:
                plugin["renamable"] = st["renamable"]
            self._send(200, {"ok": True, "contract": "monkeytester-plugin",
                             "contract_version": 1, "plugin": plugin})
        elif self.path == "/devices":
            self._send(200, {"devices": []})
        elif self.path == "/config":
            self._send(200, {"name": st["name"], "default_name": st["default_name"]})
        else:
            self._send(404, {"error": "not found", "code": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        DevicePlugin.posts.append((self.path, body, self.headers.get("X-Plugin-Token")))
        if not self._authorized():
            return
        st = DevicePlugin.state
        if self.path != "/config":
            self._send(404, {"error": "not found", "code": "not_found"})
            return
        new = body.get("name")
        if not st["renamable"] or not (isinstance(new, str) and re.fullmatch(NAME_RE, new)) \
                or new == "REFUSED-NAME":
            self._send(400, {"error": f"bad name {new!r}", "code": "bad_request"})
            return
        st["name"] = new
        self._send(200, {"ok": True, "config": {"name": new, "default_name": st["default_name"]}})


class RenameCase(ServerCase):
    def setUp(self):
        super().setUp()
        AiPlugin.state = ai_state()
        AiPlugin.posts = []
        AiPlugin.auth = []
        DevicePlugin.reset()
        app._PLUGIN_SELECTED_CACHE.clear()
        env = patch.dict(os.environ, {"MT_PLUGINS": ""})
        env.start()
        self.addCleanup(env.stop)

    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def renamed_ai(self, name="VISION"):
        """A renamable AI build whose user-chosen name is `name`."""
        AiPlugin.state.update(plugin_name=name, default_name="AI", renamable=True)
        return self.serve(AiPlugin)

    def config(self):
        path = self.runs / "config.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def write_config(self, cfg):
        (self.runs / "config.json").write_text(json.dumps(cfg))

    def post(self, path, body, **kw):
        return request(self.port, "POST", path, body, **kw)

    def rows(self):
        return {p["name"]: p for p in self.get_json("/api/plugins")["plugins"]}

    def name_posts(self):
        return [b for p, b in AiPlugin.posts if p == "/config" and "name" in b]

    def as_plain_user(self):
        """Headers of a signed-in ordinary (non-admin) user."""
        data = auth.load_auth()
        data["users"]["plain"] = {"password_hash": data["users"]["tester"]["password_hash"],
                                  "role": "user", "created_at": 1}
        data["sessions"][auth._session_key("plain-session")] = {"user": "plain", "created": 1}
        auth.save_auth(data)
        return {"Cookie": f"{auth.LEGACY_COOKIE_NAME}=plain-session"}


class AiIdentifiedByDefaultNameTests(RenameCase):
    """Decision 4: the AI is recognised by default_name, never by its
    current name; older builds without default_name still work."""

    def test_add_row_routes_a_renamed_ai_to_visual_plugin_url(self):
        url = self.renamed_ai("VISION")
        status, _, body = self.post("/api/config/plugins", {"set": {"url": url}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ai"])
        self.assertEqual((body["plugin"]["name"], body["plugin"]["display_name"]),
                         ("AI", "VISION"))
        cfg = self.config()
        self.assertEqual((cfg["visual_plugin_url"], cfg["visual_plugin"]), (url, "AI"))
        self.assertNotIn("VISION", cfg.get("plugins") or {})

    def test_validate_accepts_a_renamed_ai_and_refuses_a_plugin_only_named_like_it(self):
        url = self.renamed_ai("VISION")
        status, _, body = self.post("/api/config/visual-model", {"plugin_url": url})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.config()["visual_plugin_url"], url)
        # A device plugin renamed to "AI" is still not the AI.
        Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": {"name": "AI", "default_name": "ESP32-SENSOR"}}
        status, _, body = self.post("/api/config/visual-model", {"plugin_url": self.peer_url})
        self.assertEqual((status, body["code"]), (400, "wrong_plugin"))

    def test_edit_guards_use_the_default_name(self):
        url = self.renamed_ai("VISION")
        Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": {"name": "ESP32-SENSOR"}}
        self.assertEqual(self.post("/api/config/plugins", {"set": {"url": url}})[0], 200)
        self.assertEqual(self.post("/api/config/plugins", {"set": {"url": self.peer_url}})[0], 200)
        # The AI row edited onto (another form of) its own address: still AI.
        status, _, body = self.post("/api/config/plugins",
                                    {"set": {"url": url + "/"}, "row": "AI"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ai"])
        # A device row onto the renamed AI's address: refused.
        status, _, body = self.post("/api/config/plugins",
                                    {"set": {"url": url}, "row": "ESP32-SENSOR"})
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        self.assertIn("Add row", body["error"])
        # Another address of the renamed AI on the Add row: name_taken.
        status, _, body = self.post("/api/config/plugins",
                                    {"set": {"url": url.replace("127.0.0.1", "localhost")}})
        self.assertEqual((status, body["code"]), (400, "name_taken"))
        self.assertEqual(self.config()["plugins"], {"ESP32-SENSOR": self.peer_url})

    def test_older_ai_without_default_name_still_works(self):
        url = self.serve(AiPlugin)  # plugin.name AI, no default_name
        status, _, body = self.post("/api/config/plugins", {"set": {"url": url}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ai"])
        row = self.rows()["AI"]
        self.assertEqual((row["display_name"], row["default_name"], row["renamable"]),
                         ("AI", "AI", False))


class NameColumnTests(RenameCase):
    def test_name_column_shows_the_reported_name(self):
        url = self.renamed_ai("VISION")
        self.write_config({"visual_plugin_url": url, "visual_plugin": "AI"})
        row = self.rows()["AI"]  # the row key stays the AI slot
        self.assertEqual((row["display_name"], row["default_name"], row["renamable"]),
                         ("VISION", "AI", True))
        info = self.get_json("/api/visual-model/plugin")
        self.assertEqual((info["name"], info["default_name"], info["renamable"]),
                         ("VISION", "AI", True))

    def test_unreachable_ai_shows_monkeytester_ai(self):
        self.write_config({"visual_plugin_url": "http://127.0.0.1:9",
                           "visual_plugin": "AI"})
        row = self.rows()["AI"]
        self.assertEqual((row["display_name"], row["renamable"]), ("AI", False))

    def test_build_rows_are_never_renamable(self):
        row = {"name": "build-local", "kind": "build", "ok": True,
               "plugin": {"name": "build-local", "renamable": True}}
        app._add_row_names(plugin_client(), row)
        self.assertFalse(row["renamable"])


class AiRenameTests(RenameCase):
    """The AI renames through its Configure route: proxied to the plugin's
    POST /config, the viewer keeps no alias and re-keys nothing."""

    def setUp(self):
        super().setUp()
        self.url = self.renamed_ai("AI")
        self.write_config({"visual_plugin_url": self.url, "visual_plugin": "AI"})

    def test_rename_is_proxied_and_reset_restores_the_default(self):
        status, _, body = self.post("/api/config/visual-model", {"name": "VISION"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.name_posts(), [{"name": "VISION"}])
        self.assertEqual((body["config"]["name"], body["config"]["default_name"]),
                         ("VISION", "AI"))
        cfg = self.config()
        self.assertEqual(cfg["visual_plugin"], "AI")  # no viewer alias
        self.assertNotIn("VISION", json.dumps(cfg))
        self.assertEqual(self.rows()["AI"]["display_name"], "VISION")
        # Reset to default = posting the default_name.
        status, _, body = self.post("/api/config/visual-model", {"name": "AI"})
        self.assertEqual(status, 200, body)
        self.assertEqual(AiPlugin.state["plugin_name"], "AI")
        self.assertEqual(self.rows()["AI"]["display_name"], "AI")

    def test_name_goes_first_among_the_staged_config_keys(self):
        status, _, body = self.post("/api/config/visual-model",
                                    {"provider": "local", "model": "llava", "name": "VISION"})
        self.assertEqual(status, 200, body)
        config_posts = [b for p, b in AiPlugin.posts if p == "/config"]
        self.assertEqual(config_posts[0], {"name": "VISION"})
        self.assertEqual(AiPlugin.state["selected"], "llava")

    def test_current_name_is_a_noop(self):
        status, _, body = self.post("/api/config/visual-model", {"name": "AI"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.name_posts(), [])

    def test_name_used_by_another_row_is_name_taken(self):
        cfg = self.config()
        cfg["plugins"] = {"ESP32": "http://127.0.0.1:9", "BUILD-LOCAL": {"url": "http://b:1",
                                                                         "token": ""}}
        self.write_config(cfg)
        for taken in ("ESP32", "BUILD-LOCAL"):
            status, _, body = self.post("/api/config/visual-model", {"name": taken})
            self.assertEqual((status, body["code"]), (400, "name_taken"), taken)
        self.assertEqual(self.name_posts(), [])
        self.assertEqual(AiPlugin.state["plugin_name"], "AI")

    def test_bad_name_is_refused_before_the_plugin(self):
        for bad in ("", "has space", "-lead", "x" * 65, 5, None):
            status, _, body = self.post("/api/config/visual-model", {"name": bad})
            self.assertEqual((status, body["code"]), (400, "bad_request"), bad)
        self.assertEqual(self.name_posts(), [])

    def test_lowercase_and_reserved_names_are_refused_before_the_plugin(self):
        for bad, code in (("vision", "bad_request"), ("Vision", "bad_request"),
                          ("CLICK", "reserved_name"), ("WAIT", "reserved_name")):
            status, _, body = self.post("/api/config/visual-model", {"name": bad})
            self.assertEqual((status, body["code"]), (400, code), bad)
        self.assertIn("uppercase", self.post("/api/config/visual-model", {"name": "vision"})[2]["error"])
        self.assertEqual(self.name_posts(), [])

    def test_older_ai_is_not_renamable(self):
        AiPlugin.state.update(renamable=False, default_name=None)
        status, _, body = self.post("/api/config/visual-model", {"name": "VISION"})
        self.assertEqual((status, body["code"]), (400, "not_renamable"))
        self.assertEqual(self.name_posts(), [])

    def test_rename_is_admin_only(self):
        status, _, body = self.post("/api/config/visual-model", {"name": "VISION"},
                                    headers=self.as_plain_user())
        self.assertEqual(status, 403, body)
        self.assertEqual(self.name_posts(), [])


class DevicePluginRenameTests(RenameCase):
    """A renamable device plugin: POST /api/config/plugins/<row>/config
    {name} relays the rename and re-keys the row (key moved in place, token
    plugin:<old> -> plugin:<new>, renamed_from)."""

    def setUp(self):
        super().setUp()
        self.url = self.serve(DevicePlugin)
        self.write_config({"plugins": {"A": "http://127.0.0.1:9", "LAB-SENSOR": self.url,
                                       "z": "http://127.0.0.1:9"}})
        auth.set_service_token("plugin:LAB-SENSOR", "dev-token-4321")

    def rename(self, name, row="LAB-SENSOR", **kw):
        return self.post(f"/api/config/plugins/{row}/config", {"name": name}, **kw)

    def config_posts(self):
        return [p for p in DevicePlugin.posts if p[0] == "/config"]

    def test_rename_rekeys_the_row_and_moves_the_token(self):
        status, _, body = self.rename("BENCH-SENSOR")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["renamed_from"], "LAB-SENSOR")
        self.assertEqual(body["plugin"]["name"], "BENCH-SENSOR")
        self.assertEqual(body["config"], {"name": "BENCH-SENSOR", "default_name": "ESP32-SENSOR"})
        # The plugin got the call with its pairing token, and now reports it.
        self.assertEqual(self.config_posts(), [("/config", {"name": "BENCH-SENSOR"},
                                                "dev-token-4321")])
        self.assertEqual(DevicePlugin.state["name"], "BENCH-SENSOR")
        self.assertEqual(list(self.config()["plugins"]),
                         ["A", "BENCH-SENSOR", "z"])  # same place, new key
        self.assertEqual(auth.plugin_token("BENCH-SENSOR"), "dev-token-4321")
        self.assertEqual(auth.plugin_token("LAB-SENSOR"), "")
        row = self.rows()["BENCH-SENSOR"]
        self.assertEqual((row["display_name"], row["default_name"], row["renamable"]),
                         ("BENCH-SENSOR", "ESP32-SENSOR", True))
        # Reset to default: post default_name; re-keyed back.
        status, _, body = self.rename("ESP32-SENSOR", row="BENCH-SENSOR")
        self.assertEqual((status, body["renamed_from"]), (200, "BENCH-SENSOR"))
        self.assertEqual(list(self.config()["plugins"]), ["A", "ESP32-SENSOR", "z"])
        self.assertEqual(auth.plugin_token("ESP32-SENSOR"), "dev-token-4321")

    def test_current_name_is_a_noop(self):
        status, _, body = self.rename("LAB-SENSOR")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["unchanged"])
        self.assertNotIn("renamed_from", body)
        self.assertEqual(self.config_posts(), [])

    def test_name_taken_by_another_row_is_refused_before_the_plugin(self):
        cfg = self.config()
        cfg["plugins"]["BUILD-LOCAL"] = {"url": "http://127.0.0.1:9", "token": ""}
        self.write_config(cfg)
        for taken in ("A", "BUILD-LOCAL", "AI"):
            status, _, body = self.rename(taken)
            self.assertEqual((status, body["code"]), (400, "name_taken"), taken)
        self.assertEqual(self.config_posts(), [])
        self.assertIn("LAB-SENSOR", self.config()["plugins"])

    def test_name_taken_by_a_renamed_ai(self):
        ai_url = self.renamed_ai("VISION")
        cfg = self.config()
        cfg["visual_plugin_url"] = ai_url
        self.write_config(cfg)
        status, _, body = self.rename("VISION")
        self.assertEqual((status, body["code"]), (400, "name_taken"))
        self.assertEqual(self.config_posts(), [])

    def test_non_renamable_plugin_gets_400(self):
        for renamable in (None, False):
            DevicePlugin.reset(renamable=renamable)
            status, _, body = self.rename("BENCH-SENSOR")
            self.assertEqual((status, body["code"]), (400, "not_renamable"), renamable)
        self.assertEqual(self.config_posts(), [])
        self.assertIn("LAB-SENSOR", self.config()["plugins"])

    def test_lowercase_and_reserved_names_are_refused_before_the_plugin(self):
        for bad, code in (("bench-sensor", "bad_request"), ("VERIFY", "reserved_name"),
                          ("PLUGIN", "reserved_name"), ("END", "reserved_name")):
            status, _, body = self.rename(bad)
            self.assertEqual((status, body["code"]), (400, code), bad)
        self.assertEqual(self.config_posts(), [])
        self.assertIn("LAB-SENSOR", self.config()["plugins"])

    def test_plugin_refusal_is_relayed_and_nothing_moves(self):
        status, _, body = self.rename("REFUSED-NAME")
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        self.assertIn("LAB-SENSOR", self.config()["plugins"])
        self.assertEqual(auth.plugin_token("LAB-SENSOR"), "dev-token-4321")

    def test_bad_requests(self):
        cases = [
            ("/api/config/plugins/ghost/config", {"name": "X"}, 404),
            ("/api/config/plugins/LAB-SENSOR/config", {"name": "bad name"}, 400),
            ("/api/config/plugins/LAB-SENSOR/config", {"name": "x", "extra": 1}, 400),
            ("/api/config/plugins/LAB-SENSOR/config", {}, 400),
            ("/api/config/plugins/AI/config", {"name": "x"}, 400),
        ]
        for path, payload, want in cases:
            status, _, body = self.post(path, payload)
            self.assertEqual(status, want, (path, payload, body))
        cfg = self.config()
        cfg["plugins"]["build-local"] = {"url": "http://127.0.0.1:9", "token": ""}
        self.write_config(cfg)
        status, _, body = self.rename("x", row="build-local")
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        self.assertEqual(self.config_posts(), [])

    def test_rename_is_admin_only(self):
        status, _, _ = self.rename("BENCH-SENSOR", headers=self.as_plain_user())
        self.assertEqual(status, 403)
        self.assertEqual(self.config_posts(), [])
        self.assertIn("LAB-SENSOR", self.config()["plugins"])

    def test_renamable_device_plugin_gets_a_configure_button(self):
        # /api/plugins flags it; plugins.html shows Configure for renamable rows.
        self.assertTrue(self.rows()["LAB-SENSOR"]["renamable"])
        src = (ROOT / "plugins.html").read_text()
        self.assertIn("p.config || p.__ai || p.renamable", src)


class PluginNamesHelperTests(unittest.TestCase):
    def test_plugin_names(self):
        pc = plugin_client()
        self.assertEqual(pc.plugin_names({"name": "VISION", "default_name": "AI",
                                          "renamable": True}),
                         ("VISION", "AI", True))
        self.assertEqual(pc.plugin_names({"name": "esp32"}), ("esp32", "esp32", False))
        self.assertEqual(pc.plugin_names("hub"), ("hub", "hub", False))
        self.assertEqual(pc.plugin_names({"name": "a", "default_name": "bad name"}), ("a", "a", False))
        self.assertEqual(pc.plugin_names({"name": "a", "renamable": "yes"}), ("a", "a", False))
        self.assertEqual(pc.plugin_names({"name": "Bad Name", "renamable": True}), (None, None, False))
        self.assertEqual(pc.plugin_names(None), (None, None, False))
        self.assertEqual(pc.plugin_identity({"name": "VISION", "default_name": "AI"}),
                         "AI")

    def test_public_ai_config_carries_the_names(self):
        pc = plugin_client()
        out = pc.public_ai_config({"name": "VISION", "default_name": "AI"})
        self.assertEqual((out["name"], out["default_name"]), ("VISION", "AI"))
        self.assertEqual(pc.public_ai_config({})["name"], "")


class NameSectionStagingTests(unittest.TestCase):
    """The Name field is a staged field of the AI panel's single Save."""

    SAVED = {"configured": True, "url": "http://a:1", "tokenConfigured": True,
             "provider": "local", "selected": "qwen", "copilotModel": "", "copilotSignedIn": True,
             "remote": {"url": "", "model": "", "resize": True, "keyConfigured": False},
             "renamable": True, "name": "AI"}
    CURRENT = {"address": "http://a:1", "token": "", "provider": "local", "localModel": "qwen",
               "copilotModel": "", "remoteUrl": "", "remoteModel": "", "remoteResize": True,
               "remoteKey": "", "tokenChoice": "keep", "pluginChoice": "keep",
               "copilotChoice": "keep", "name": "AI"}

    def stage(self, saved=None, **current):
        return run_staging({**self.SAVED, **(saved or {})}, {**self.CURRENT, **current})

    def test_unchanged_name_stages_nothing(self):
        self.assertEqual(self.stage()["staged"], {})

    def test_new_name_stages_first_in_the_config_body(self):
        out = self.stage(name=" VISION ", provider="remote")
        self.assertEqual(out["staged"], {"name": "VISION", "provider": "remote"})
        self.assertEqual(list(out["steps"][0]["body"]), ["name", "provider"])

    def test_invalid_or_empty_name_never_stages(self):
        for bad in ("", "bad name", "-x", "X" * 65, "vision"):
            self.assertEqual(self.stage(name=bad)["staged"], {}, bad)

    def test_not_renamable_never_stages_a_name(self):
        self.assertEqual(self.stage(saved={"renamable": False}, name="VISION")["staged"], {})

    def test_panel_markup_and_listeners(self):
        src = (ROOT / "plugins.html").read_text()
        for present in ('id="pluginNameInput"', ">Reset to default</button>", '"Default: "',
                        'inp.addEventListener("input", onEdit)',
                        'inp.addEventListener("change", onEdit)',
                        "reset.dataset.default = defaultName",
                        "name: p.display_name || p.name",
                        "/api/config/plugins/\" + encodeURIComponent(key) + \"/config"):
            self.assertIn(present, src)
        # The Name section sits between the Plugin and Configuration sections.
        i_name = src.index("(renamable ? nameSectionHtml(false)")
        self.assertLess(src.index('class="panel-section-head">Plugin</div>'), i_name)
        self.assertLess(i_name, src.index('class="panel-section-head">Configuration</div>'))


if __name__ == "__main__":
    unittest.main()
