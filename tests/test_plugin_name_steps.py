"""Plugin-name steps (`ESP32-SERVER: expect`), uppercase plugin names, the
reserved built-in step names, the AI's vision verbs under its name
(`AI: CLICK Login`) and the viewer's one-time uppercase migration."""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mt-tests" / "scripts"))
with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core
import plugin_client as pc

import app
import viewer_auth as auth
from tests.test_app_api import Peer, ServerCase, request

SCRIPTS = Path(__file__).resolve().parents[1] / "mt-tests" / "scripts"


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, core, "_ai_name", core._ai_name)
        core._ai_name = None

    def test_plugin_step_keeps_name_args_block_and_platform(self):
        steps = core.parse_steps([
            "IF IOS: ESP32-SERVER: expect timeout=5",
            "    GET /x -> 200",
            "END",
            "esp32-sensor: ble-advertise seconds=0",
        ])
        self.assertEqual(len(steps), 2)
        first, second = steps
        self.assertEqual((first["command"], first["plugin"], first["args"], first["platform"]),
                         ("ESP32-SERVER", "ESP32-SERVER", "expect timeout=5", "ios"))
        self.assertEqual(first["block"], ["GET /x -> 200"])
        # Lowercase in the test file is uppercased, like built-in commands.
        self.assertEqual((second["command"], second["plugin"]), ("ESP32-SENSOR", "ESP32-SENSOR"))
        self.assertFalse(any("error" in s for s in steps))

    def test_file_refs_resolve_on_the_new_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps = core.parse_steps(['ESP32-SERVER: expect script="GET / -> 200 @file:a.json"',
                                      "ESP32-SERVER: expect",
                                      "  POST / @file:b.json",
                                      "END"], test_dir=tmp)
        self.assertIn("@file:" + tmp + "/a.json", steps[0]["args"])
        self.assertEqual(steps[1]["block"], ["POST / @file:" + tmp + "/b.json"])

    def test_colon_is_required(self):
        steps = core.parse_steps(["ESP32-SERVER expect", "KEY=value"])
        self.assertNotIn("plugin", steps[0])
        self.assertNotIn("plugin", steps[1])

    def test_old_plugin_step_is_an_error_naming_the_replacement(self):
        steps = core.parse_steps(["WAIT: 1", "PLUGIN: esp32-server expect", "  GET / -> 200",
                                  "END", "WAIT: 2"])
        self.assertEqual([s["command"] for s in steps], ["WAIT", "PLUGIN", "WAIT"])
        self.assertEqual(steps[1]["error"],
                         'PLUGIN: steps were removed; write "ESP32-SERVER: expect" (line 2)')

    def test_bare_vision_verbs_are_errors_naming_the_replacement(self):
        steps = core.parse_steps(["CLICK: Login", "VERIFY: Home is shown",
                                  'IF ANDROID: INPUT: Email "a@b.c"'])
        self.assertEqual([s["error"] for s in steps], [
            'CLICK: steps were removed; write "AI: CLICK Login" (line 1)',
            'VERIFY: steps were removed; write "AI: VERIFY Home is shown" (line 2)',
            'INPUT: steps were removed; write "AI: INPUT Email "a@b.c"" (line 3)',
        ])

    def test_bare_verb_error_uses_the_ai_current_name(self):
        core._ai_name = "VISION"
        step = core.parse_steps(["CLICK: Login"])[0]
        self.assertIn('write "VISION: CLICK Login"', step["error"])

    def test_ai_steps_parse_as_plugin_steps(self):
        step = core.parse_steps(["AI: CLICK Login button"])[0]
        self.assertEqual((step["command"], step["plugin"], step["args"]),
                         ("AI", "AI", "CLICK Login button"))
        self.assertNotIn("error", step)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, core, "_ai_name", core._ai_name)
        core._ai_name = None
        for patcher in (patch.object(core.controller, "screenshot", return_value=True),
                        patch.object(core.controller, "click"),
                        patch.object(core, "check_visual_config"),
                        patch.object(core.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_line(self, line):
        step = core.parse_steps([line])[0]
        with contextlib.redirect_stdout(io.StringIO()):
            return core.execute_step(step)

    def test_ai_click_runs_find_element(self):
        with patch.object(core.vision_analyzer, "find_element",
                          return_value={"found": True, "coordinates": [10, 20]}) as find:
            self.assertTrue(self.run_line("AI: CLICK Login button"))
        self.assertEqual(find.call_args[0][1], "Login button")
        core.controller.click.assert_called_once_with(10, 20)

    def test_ai_verify_runs_verify_element(self):
        with patch.object(core.vision_analyzer, "verify_element",
                          return_value={"found": True, "confidence": 0.9}) as verify:
            self.assertTrue(self.run_line("ai: verify Home screen"))
        self.assertEqual(verify.call_args[0][1], "Home screen")

    def test_ai_input_runs_check_input_field(self):
        with patch.object(core.vision_analyzer, "check_input_field",
                          return_value={"found": True, "coordinates": [1, 2]}) as check, \
                patch.object(core.controller, "input_text") as typed:
            self.assertTrue(self.run_line('AI: INPUT Email field "a@b.c"'))
        self.assertEqual(check.call_args[0][1], "Email field")
        typed.assert_called_once_with("a@b.c")

    def test_renamed_ai_name_runs_the_vision_verbs(self):
        core._ai_name = "VISION"
        with patch.object(core.vision_analyzer, "find_element",
                          return_value={"found": True, "coordinates": [1, 1]}) as find, \
                patch.object(core, "_run_plugin_step", return_value=False) as relay:
            self.assertTrue(self.run_line("VISION: CLICK OK"))
            find.assert_called_once()
            # The old name is no longer the AI: it goes the plugin way.
            self.assertFalse(self.run_line("AI: CLICK OK"))
            relay.assert_called_once()

    def test_other_ai_actions_and_device_plugins_are_relayed(self):
        with patch.object(core, "_run_plugin_step", return_value=True) as relay, \
                patch.object(core.vision_analyzer, "find_element") as find:
            self.assertTrue(self.run_line("AI: set-model model=qwen"))
            self.assertTrue(self.run_line("ESP32-SERVER: CLICK x"))
        self.assertEqual([c[0][1] for c in relay.call_args_list],
                         ["set-model model=qwen", "CLICK x"])
        find.assert_not_called()

    def test_parse_errors_fail_without_running(self):
        with patch.object(core.vision_analyzer, "find_element") as find:
            self.assertFalse(self.run_line("CLICK: Login"))
        find.assert_not_called()


class AiNameResolutionTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, core, "_ai_name", core._ai_name)
        self.addCleanup(setattr, core, "_plugins", None)
        core._plugins = None

    def resolve(self, env, health=None, error=None):
        def fake_health(client, timeout=None):
            if error:
                raise pc.PluginError(error, "unreachable")
            return health
        out = io.StringIO()
        with patch.dict(os.environ, env, clear=False), \
                patch.object(pc.PluginClient, "health", fake_health), \
                contextlib.redirect_stdout(out):
            name = core.resolve_ai_plugin_name()
        return name, out.getvalue()

    def test_current_name_comes_from_health(self):
        name, log = self.resolve({"MT_AI_PLUGIN_URL": "http://ai:8091", "VISION_MODEL_API_URL": ""},
                                 {"plugin": {"name": "VISION", "default_name": "AI"}})
        self.assertEqual((name, core.ai_plugin_name()), ("VISION", "VISION"))
        self.assertIn("named VISION", log)

    def test_vision_endpoint_gives_the_address(self):
        with patch.dict(os.environ, {"MT_AI_PLUGIN_URL": "",
                                     "VISION_MODEL_API_URL": "http://ai:8091/v1/chat/completions"}):
            self.assertEqual(core._ai_plugin_url(), "http://ai:8091")

    def test_unreachable_falls_back_to_ai_and_says_so(self):
        name, log = self.resolve({"MT_AI_PLUGIN_URL": "http://ai:8091"}, error="refused")
        self.assertEqual(name, "AI")
        self.assertIn("falling back to AI", log)

    def test_no_address_falls_back_to_ai(self):
        name, log = self.resolve({"MT_AI_PLUGIN_URL": "", "VISION_MODEL_API_URL": ""})
        self.assertEqual(name, "AI")
        self.assertIn("no address configured", log)

    def test_ai_joins_the_plugin_list_under_its_current_name(self):
        core._ai_name = "VISION"
        with patch.dict(os.environ, {"MT_AI_PLUGIN_URL": "http://ai:8091", "MT_PLUGINS": "",
                                     "VISUAL_MODEL_API_KEY": "pair"}):
            plugins = core._get_plugins()
        self.assertEqual((plugins["VISION"].url, plugins["VISION"].token), ("http://ai:8091", "pair"))


class NameRuleTests(unittest.TestCase):
    def test_runner_dispatch_commands_are_reserved(self):
        src = (SCRIPTS / "test_runner_core.py").read_text()
        body = src[src.index("def execute_step("):src.index("\ndef run_single_test(")]
        dispatched = set(re.findall(r'cmd == "([A-Z_]+)"', body))
        self.assertTrue({"VERIFY", "CLICK", "INPUT", "WAIT", "LAUNCH"} <= dispatched)
        self.assertLessEqual(dispatched, pc.RESERVED_STEP_NAMES)
        self.assertLessEqual(set(pc.AI_VISION_VERBS), pc.RESERVED_STEP_NAMES)
        self.assertLessEqual({"PLUGIN", "IF", "END", "MODE", "CLEAR_DATA", "REINSTALL_APP"},
                             pc.RESERVED_STEP_NAMES)
        self.assertNotIn(pc.AI_PLUGIN_NAME, pc.RESERVED_STEP_NAMES)

    def test_reserved_names_never_parse_as_plugin_steps(self):
        for name in pc.RESERVED_STEP_NAMES:
            self.assertIsNone(core._plugin_step_name(name + ":"), name)

    def test_name_problems(self):
        self.assertIsNone(pc.plugin_name_problem("ESP32-SERVER"))
        self.assertEqual(pc.plugin_name_problem("esp32-server")[1], "bad_request")
        self.assertIn("uppercase", pc.plugin_name_problem("esp32-server")[0])
        self.assertEqual(pc.plugin_name_problem("bad name")[1], "bad_request")
        self.assertEqual(pc.plugin_name_problem("CLICK")[1], "reserved_name")
        self.assertEqual(pc.plugin_name_problem("END")[1], "reserved_name")

    def test_ai_identity_accepts_ai_and_the_legacy_name(self):
        self.assertEqual(pc.AI_PLUGIN_NAME, "AI")
        for ident in ("AI", "ai", "monkeytester-ai", "MONKEYTESTER-AI"):
            self.assertEqual(pc.plugin_identity({"name": "X", "default_name": ident}), "AI", ident)
        self.assertEqual(pc.plugin_identity({"name": "ESP32"}), "ESP32")


class AddRefusesBadNamesTests(ServerCase):
    def add(self, name):
        Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": {"name": name, "version": "0.1"}}
        return request(self.port, "POST", "/api/config/plugins", {"set": {"url": self.peer_url}})

    def test_lowercase_name_is_bad_request(self):
        status, _, body = self.add("esp32-server")
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        self.assertIn("uppercase", body["error"])
        self.assertFalse((self.runs / "config.json").exists())

    def test_reserved_name_is_refused(self):
        status, _, body = self.add("CLICK")
        self.assertEqual((status, body["code"]), (400, "reserved_name"))

    def test_ai_name_is_taken(self):
        status, _, body = self.add("AI-LIKE")
        self.assertEqual(status, 200, body)
        Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": {"name": "AI"}}
        # /health names AI but the plugin isn't the AI (no /models): as a test
        # plugin it may not take the AI's name.
        with patch.object(app, "_ai_reported_name", return_value=None), \
                patch.object(app._plugin_client(), "plugin_identity", return_value="X"):
            status, _, body = request(self.port, "POST", "/api/config/plugins",
                                      {"set": {"url": self.peer_url}, "row": "AI-LIKE"})
        self.assertEqual((status, body["code"]), (400, "name_taken"))

    def test_uppercase_name_registers(self):
        status, _, body = self.add("ESP32-SERVER")
        self.assertEqual(status, 200, body)
        self.assertIn("ESP32-SERVER", json.loads((self.runs / "config.json").read_text())["plugins"])


class StartupMigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cfg_file = self.root / "config.json"
        for patcher in (patch.object(app, "CONFIG_FILE", self.cfg_file),
                        patch.object(auth, "SERVICE_TOKENS_FILE", self.root / "service_tokens.json")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def migrate(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return app.migrate_plugin_names()

    def test_keys_visual_plugin_and_tokens_are_uppercased_once(self):
        self.cfg_file.write_text(json.dumps({
            "plugins": {"esp32-sensor": "http://s:8091", "ESP32-SERVER": "http://h:8093",
                        "build-local": {"url": "http://b:1", "token": ""}},
            "visual_plugin": "monkeytester-ai", "visual_plugin_url": "http://ai:8091"}))
        auth.set_service_token("plugin:esp32-sensor", "tok-1234")
        self.assertEqual(self.migrate(), {"esp32-sensor": "ESP32-SENSOR"})
        cfg = json.loads(self.cfg_file.read_text())
        self.assertEqual(list(cfg["plugins"]), ["ESP32-SENSOR", "ESP32-SERVER", "build-local"])
        self.assertEqual(cfg["visual_plugin"], "AI")
        self.assertEqual(cfg["visual_plugin_url"], "http://ai:8091")
        self.assertEqual(auth.plugin_token("ESP32-SENSOR"), "tok-1234")
        self.assertFalse(auth.plugin_token("esp32-sensor"))
        # Idempotent.
        self.assertEqual(self.migrate(), {})
        self.assertEqual(json.loads(self.cfg_file.read_text()), cfg)

    def test_collision_is_left_alone(self):
        self.cfg_file.write_text(json.dumps({"plugins": {"hub": "http://a:1", "HUB": "http://b:1"}}))
        self.assertEqual(self.migrate(), {})
        self.assertEqual(json.loads(self.cfg_file.read_text())["plugins"],
                         {"hub": "http://a:1", "HUB": "http://b:1"})

    def test_mt_plugins_import_is_uppercased(self):
        self.cfg_file.write_text("{}")
        env = {"MT_PLUGINS": "esp32-sensor=http://s:8091,monkeytester-ai=http://ai:8091"}
        with contextlib.redirect_stderr(io.StringIO()):
            imported = app.import_device_plugins(env)
        self.assertEqual(imported, {"ESP32-SENSOR": "http://s:8091"})

    def test_no_config_does_nothing(self):
        self.assertEqual(self.migrate(), {})
        self.assertFalse(self.cfg_file.exists())


if __name__ == "__main__":
    unittest.main()
