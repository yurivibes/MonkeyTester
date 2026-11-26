"""Device plugin client and the PLUGIN test step, against a stub plugin (PLUGIN_API.md)."""
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mt-tests" / "scripts"))
import plugin_client
from plugin_client import PluginClient, PluginError

with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core

ACTIONS = [
    {"name": "scenario", "params": {"name": {"type": "string", "required": True}},
     "default_timeout_ms": 5000, "needs_device": True},
    {"name": "typed", "params": {"count": {"type": "int"}, "ratio": {"type": "float"},
                                 "erase": {"type": "bool"}, "extra": {"type": "json"},
                                 "mode": {"type": "enum", "values": ["a", "b"]},
                                 "firmware_dir": {"type": "path"}}},
    {"name": "fail", "params": {}}, {"name": "error", "params": {}},
    {"name": "timeout", "params": {}}, {"name": "slow", "params": {}},
    {"name": "hang", "params": {}},
    {"name": "expect", "params": {"script": {"type": "string", "required": True}}},
]


class Stub(BaseHTTPRequestHandler):
    calls = []
    token = None
    health = {}
    jobs = {}
    busy = False

    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if Stub.token and self.headers.get("X-Plugin-Token") != Stub.token:
            self.reply(401, {"error": "token required", "code": "unauthorized"})
            return False
        return True

    def do_GET(self):
        url = urlparse(self.path)
        Stub.calls.append(("GET", self.path, None, self.headers.get("X-Plugin-Token")))
        if not self.authorized():
            return
        if url.path == "/health":
            return self.reply(200, Stub.health)
        if url.path == "/devices":
            return self.reply(200, {"devices": [{"id": "aa:bb", "kind": "esp32", "state": "ready",
                                                 "model": "ESP32-C3", "details": {}}], "error": None})
        if url.path == "/actions":
            return self.reply(200, {"actions": ACTIONS})
        if url.path == "/text":
            return self.reply(200, b"not json")
        if url.path.startswith("/jobs/"):
            job = Stub.jobs.get(url.path.split("/")[2])
            if not job:
                return self.reply(404, {"error": "no such job", "code": "not_found"})
            job["polls"] += 1
            if job["action"] == "slow" and job["state"] == "running":
                job["log"].append(f"poll {job['polls']}")
                if job["polls"] >= 3:
                    job.update(state="passed", result={"polls": job["polls"]})
            return self.reply(200, job)
        return self.reply(404, {"error": "not found", "code": "not_found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw or b"{}")
        Stub.calls.append(("POST", self.path, body, self.headers.get("X-Plugin-Token")))
        if not self.authorized():
            return
        if self.path == "/jobs":
            if Stub.busy:
                return self.reply(409, {"error": "device busy", "code": "device_busy"})
            action = body.get("action")
            if action not in {a["name"] for a in ACTIONS}:
                return self.reply(404, {"error": f"unknown action {action}", "code": "unknown_action"})
            job_id = f"j-{len(Stub.jobs) + 1}"
            job = {"id": job_id, "device": body.get("device"), "action": action,
                   "params": body.get("params"), "state": "running", "result": None,
                   "error": None, "log": [f"-> {action}"], "polls": 0}
            outcome = {"scenario": ("passed", None), "typed": ("passed", None),
                       "expect": ("passed", None),
                       "fail": ("failed", {"code": "mismatch", "message": "wrong answer"}),
                       "error": ("error", {"code": "port_gone", "message": "no /dev/ttyUSB0"}),
                       "timeout": ("timeout", {"code": "no_reply", "message": "no reply in 1000 ms"})}
            if action in outcome:
                job["state"], job["error"] = outcome[action]
                job["log"].append(f"<- {job['state']}")
                job["result"] = {"echo": body.get("params")}
            Stub.jobs[job_id] = job
            return self.reply(202, {"job": job})
        if self.path.startswith("/jobs/") and self.path.endswith("/cancel"):
            job = Stub.jobs[self.path.split("/")[2]]
            if job["state"] in ("queued", "running"):
                job.update(state="cancelled", error={"code": "cancelled", "message": "cancelled"})
            return self.reply(200, {"job": job})
        return self.reply(404, {"error": "not found", "code": "not_found"})


class StubCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Stub.calls, Stub.jobs, Stub.token, Stub.busy = [], {}, None, False
        Stub.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": {"name": "esp32", "version": "0.1.0"}, "devices": 1,
                       "capabilities": [], "error": None}
        self.client = PluginClient("esp32", self.url)

    def posted_jobs(self):
        return [c[2] for c in Stub.calls if c[0] == "POST" and c[1] == "/jobs"]


class HealthTests(StubCase):
    def test_compatible_plugin(self):
        self.assertEqual(self.client.health()["plugin"]["name"], "esp32")

    def test_other_contract_is_refused(self):
        Stub.health["contract"] = "something-else"
        with self.assertRaises(PluginError) as caught:
            self.client.health()
        self.assertEqual(caught.exception.code, "contract")

    def test_other_contract_version_is_refused(self):
        Stub.health["contract_version"] = 2
        with self.assertRaises(PluginError) as caught:
            self.client.health()
        self.assertEqual(caught.exception.code, "contract")
        self.assertIn("2", caught.exception.message)

    def test_token_is_sent_and_missing_token_is_unauthorized(self):
        Stub.token = "s3cret"
        with self.assertRaises(PluginError) as caught:
            self.client.health()
        self.assertEqual((caught.exception.code, caught.exception.status), ("unauthorized", 401))
        PluginClient("esp32", self.url, token="s3cret").health()
        self.assertEqual(Stub.calls[-1][3], "s3cret")

    def test_unreachable_and_non_json(self):
        with self.assertRaises(PluginError) as caught:
            PluginClient("x", "http://127.0.0.1:9", timeout=2).health()
        self.assertEqual(caught.exception.code, "unreachable")
        with self.assertRaises(PluginError) as caught:
            self.client._request("GET", "/text")
        self.assertEqual(caught.exception.code, "bad_response")

    def test_devices_and_cached_actions(self):
        self.assertEqual(self.client.devices()[0]["id"], "aa:bb")
        self.client.actions()
        self.client.actions()
        self.assertEqual(sum(1 for c in Stub.calls if c[1] == "/actions"), 1)
        self.assertIn("scenario", self.client.actions())

    def test_status_reports_each_plugin(self):
        env = {"MT_PLUGINS": f"esp32={self.url},gone=http://127.0.0.1:9"}
        status = plugin_client.plugins_status(env, timeout=2)
        self.assertEqual(status["esp32"], {"ok": True, "url": self.url, "contract_version": 1,
                                           "plugin": {"name": "esp32", "version": "0.1.0"}})
        self.assertFalse(status["gone"]["ok"])
        self.assertIn("unreachable", status["gone"]["error"])
        self.assertEqual(plugin_client.plugins_status({}), {})


class JobTests(StubCase):
    def test_passed_job(self):
        lines = []
        job = self.client.run("scenario", {"name": "happy"}, device="aa:bb", timeout_s=5,
                              on_log=lines.append)
        self.assertEqual(job["state"], "passed")
        self.assertEqual(self.posted_jobs(), [{"action": "scenario", "params": {"name": "happy"},
                                               "device": "aa:bb", "timeout_ms": 5000}])
        self.assertEqual(lines, ["-> scenario", "<- passed"])

    def test_device_and_timeout_are_optional(self):
        self.client.run("scenario", {"name": "x"})
        self.assertEqual(self.posted_jobs(), [{"action": "scenario", "params": {"name": "x"}}])

    def test_failed_timeout_and_error_states_are_returned(self):
        for action, code in (("fail", "mismatch"), ("timeout", "no_reply"), ("error", "port_gone")):
            job = self.client.run(action, {})
            self.assertEqual((job["state"], job["error"]["code"]), (action if action != "fail" else "failed", code))

    def test_wait_loops_until_terminal_and_streams_the_log(self):
        lines = []
        job = self.client.run("slow", {}, timeout_s=10, on_log=lines.append)
        self.assertEqual((job["state"], job["result"]), ("passed", {"polls": 3}))
        self.assertEqual(lines, ["-> slow", "poll 1", "poll 2", "poll 3"])
        waits = [parse_qs(urlparse(c[1]).query)["wait"][0] for c in Stub.calls
                 if c[1].startswith("/jobs/j-")]
        self.assertEqual(len(waits), 3)
        self.assertTrue(all(0 < int(w) <= plugin_client.MAX_WAIT_S for w in waits), waits)

    def test_local_timeout_cancels_the_job(self):
        with patch.object(plugin_client, "LOCAL_TIMEOUT_MARGIN_S", 0):
            job = self.client.run("hang", {}, timeout_s=0.3)
        self.assertEqual((job["state"], job["error"]["code"]), ("timeout", "client_timeout"))
        self.assertTrue(any(c[1].endswith("/cancel") for c in Stub.calls))
        self.assertEqual(Stub.jobs[job["id"]]["state"], "cancelled")

    def test_http_errors_carry_the_contract_code(self):
        Stub.busy = True
        with self.assertRaises(PluginError) as caught:
            self.client.start_job("scenario", {"name": "x"})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "device_busy"))
        Stub.busy = False
        with self.assertRaises(PluginError) as caught:
            self.client.start_job("nope")
        self.assertEqual(caught.exception.code, "unknown_action")

    def test_unknown_action_is_refused_before_starting_a_job(self):
        with self.assertRaises(PluginError) as caught:
            self.client.run("nope", {})
        self.assertEqual(caught.exception.code, "unknown_action")
        self.assertEqual(self.posted_jobs(), [])

    def test_incompatible_plugin_is_refused_before_starting_a_job(self):
        Stub.health["contract_version"] = 99
        with self.assertRaises(PluginError):
            self.client.run("scenario", {"name": "x"})
        self.assertEqual(self.posted_jobs(), [])


class CoercionTests(StubCase):
    def test_params_take_the_schema_types(self):
        params = self.client.coerce_params("typed", {
            "count": "3", "ratio": "0.5", "erase": "true", "extra": '{"a": [1]}',
            "mode": "b", "firmware_dir": "/builds/x", "unlisted": "7"})
        self.assertEqual(params, {"count": 3, "ratio": 0.5, "erase": True, "extra": {"a": [1]},
                                  "mode": "b", "firmware_dir": "/builds/x", "unlisted": "7"})
        self.assertIs(self.client.coerce_params("typed", {"erase": "off"})["erase"], False)
        self.assertEqual(self.client.coerce_params("typed", {"count": "0x10"})["count"], 16)

    def test_bad_values_are_bad_requests(self):
        for key, value in (("count", "three"), ("ratio", "x"), ("erase", "maybe"),
                           ("extra", "{"), ("mode", "c")):
            with self.assertRaises(PluginError) as caught:
                self.client.coerce_params("typed", {key: value})
            self.assertEqual(caught.exception.code, "bad_request", key)
            self.assertIn(key, caught.exception.message)

    def test_run_sends_coerced_params(self):
        self.client.run("typed", {"count": "2", "erase": "no"})
        self.assertEqual(self.posted_jobs()[0]["params"], {"count": 2, "erase": False})


class EnvTests(unittest.TestCase):
    def test_parses_names_urls_and_tokens(self):
        plugins = plugin_client.load_plugins_from_env({
            "MT_PLUGINS": " esp32=http://127.0.0.1:8091/ , my-cam=https://cam:9000 ,",
            "PLUGIN_TOKEN": "shared", "MT_PLUGIN_TOKEN_MY_CAM": "own"})
        self.assertEqual(list(plugins), ["esp32", "my-cam"])
        self.assertEqual(plugins["esp32"].url, "http://127.0.0.1:8091")
        self.assertEqual((plugins["esp32"].token, plugins["my-cam"].token), ("shared", "own"))

    def test_empty_means_no_plugins(self):
        self.assertEqual(plugin_client.load_plugins_from_env({}), {})
        self.assertIsNone(plugin_client.load_plugins_from_env(
            {"MT_PLUGINS": "a=http://x"})["a"].token)

    def test_malformed_entries_are_config_errors(self):
        for spec in ("esp32", "=http://x", "esp32=", "esp32=ftp://x", "a=http://x,a=http://y"):
            with self.assertRaises(PluginError) as caught:
                plugin_client.load_plugins_from_env({"MT_PLUGINS": spec})
            self.assertEqual(caught.exception.code, "config", spec)


class PluginStepTests(StubCase):
    def setUp(self):
        super().setUp()
        core._plugins = None
        self.addCleanup(setattr, core, "_plugins", None)
        env = patch.dict(os.environ, {"MT_PLUGINS": f"esp32={self.url}", "PLUGIN_TOKEN": ""})
        env.start()
        self.addCleanup(env.stop)

    def step(self, args):
        out = io.StringIO()
        with redirect_stdout(out):
            ok = core.execute_step({"line": 1, "command": "PLUGIN", "args": args,
                                    "raw": f"PLUGIN: {args}"})
        return ok, out.getvalue()

    def test_parse_args(self):
        self.assertEqual(core.parse_plugin_args(
            'esp32 wait-log pattern="WiFi connected" n=2 device=aa:bb timeout=30'),
            ("esp32", "wait-log", {"pattern": "WiFi connected", "n": "2"}, "aa:bb", 30.0))
        for bad in ("esp32", 'esp32 x "unclosed', "esp32 x loose", "esp32 x timeout=soon",
                    "esp32 x timeout=0"):
            with self.assertRaises(ValueError, msg=bad):
                core.parse_plugin_args(bad)

    def test_passed_job_passes_and_logs(self):
        ok, out = self.step('esp32 scenario name="sensor fail" device=aa:bb timeout=5')
        self.assertTrue(ok)
        self.assertEqual(self.posted_jobs(), [{"action": "scenario", "params": {"name": "sensor fail"},
                                               "device": "aa:bb", "timeout_ms": 5000}])
        self.assertIn("[esp32] -> scenario", out)
        self.assertIn("j-1 passed", out)

    def test_variables_are_resolved(self):
        core.VARIABLES["SCEN"] = "happy"
        self.addCleanup(core.VARIABLES.pop, "SCEN", None)
        self.assertTrue(self.step("esp32 scenario name=${SCEN}")[0])
        self.assertEqual(self.posted_jobs()[0]["params"], {"name": "happy"})

    def test_failed_and_error_jobs_fail_the_step(self):
        ok, out = self.step("esp32 fail")
        self.assertFalse(ok)
        self.assertIn("wrong answer (mismatch)", out)
        self.assertFalse(self.step("esp32 error")[0])
        self.assertFalse(self.step("esp32 timeout")[0])

    def test_unknown_plugin_and_action(self):
        ok, out = self.step("nope scenario")
        self.assertFalse(ok)
        self.assertIn("Unknown plugin 'nope'", out)
        self.assertIn("esp32", out)
        ok, out = self.step("esp32 nope")
        self.assertFalse(ok)
        self.assertIn("unknown action", out)

    def test_bad_args_and_bad_config_fail_cleanly(self):
        self.assertFalse(self.step("esp32")[0])
        self.assertFalse(self.step("esp32 typed count=many")[0])
        core._plugins = None
        with patch.dict(os.environ, {"MT_PLUGINS": "broken"}):
            ok, out = self.step("esp32 scenario")
        self.assertFalse(ok)
        self.assertIn("MT_PLUGINS", out)


class BlockParseTests(unittest.TestCase):
    def parse(self, text):
        return core.parse_steps(text.split("\n"))

    def test_block_lines_are_stripped_blank_dropped_and_hashes_kept(self):
        steps = self.parse(
            "PLUGIN: esp32 flash firmware_dir=/fw\n"
            "PLUGIN: esp32 expect\n"
            "    GET /temperature.json -> 500\n"
            "\n"
            "  # a comment the plugin sees\n"
            "\tVERIFY: not a step here\n"
            "    END\n"
            "VERIFY: The temperature card shows an error\n"
            "# a test-file comment\n"
            "PLUGIN: esp32 clear\n")
        self.assertEqual([(s["line"], s["command"]) for s in steps],
                         [(1, "PLUGIN"), (2, "PLUGIN"), (8, "VERIFY"), (10, "PLUGIN")])
        self.assertNotIn("block", steps[0])
        self.assertEqual(steps[1]["block"], ["GET /temperature.json -> 500",
                                             "# a comment the plugin sees", "VERIFY: not a step here"])
        self.assertEqual(steps[1]["raw"], "PLUGIN: esp32 expect")
        self.assertNotIn("block", steps[3])
        self.assertFalse(any("error" in s for s in steps))

    def test_block_needs_indentation_to_start_but_not_after(self):
        steps = self.parse("PLUGIN: esp32 expect\n  a\nb\n END\nWAIT: 1")
        self.assertEqual(steps[0]["block"], ["a", "b"])
        self.assertEqual([s["command"] for s in steps], ["PLUGIN", "WAIT"])
        steps = self.parse("PLUGIN: esp32 clear\nWAIT: 1")
        self.assertEqual([s["command"] for s in steps], ["PLUGIN", "WAIT"])
        self.assertNotIn("block", steps[0])

    def test_end_right_after_the_step_is_an_empty_block(self):
        steps = self.parse("PLUGIN: esp32 expect\nEND\nWAIT: 1")
        self.assertEqual(steps[0]["block"], [])
        self.assertEqual([s["command"] for s in steps], ["PLUGIN", "WAIT"])

    def test_end_must_be_the_whole_line(self):
        steps = self.parse("PLUGIN: esp32 expect\n  END of story\n  ENDING\nEND")
        self.assertEqual(steps[0]["block"], ["END of story", "ENDING"])
        self.assertNotIn("error", steps[0])

    def test_unterminated_block_is_an_error_with_its_line(self):
        steps = self.parse("WAIT: 1\nPLUGIN: esp32 expect\n  GET / -> 200\nVERIFY: x\n")
        self.assertEqual(len(steps), 2)
        self.assertIn("line 2", steps[1]["error"])
        self.assertIn("no END", steps[1]["error"])
        self.assertEqual(steps[1]["block"], ["GET / -> 200", "VERIFY: x"])

    def test_stray_end_is_an_error(self):
        steps = self.parse("WAIT: 1\nEND\nWAIT: 2")
        self.assertEqual([s["command"] for s in steps], ["WAIT", "END", "WAIT"])
        self.assertIn("line 2", steps[1]["error"])
        self.assertNotIn("error", steps[0])

    def test_parse_errors_fail_the_step_when_run(self):
        out = io.StringIO()
        with redirect_stdout(out):
            ok = core.execute_step(self.parse("END")[0])
        self.assertFalse(ok)
        self.assertIn("PARSE ERROR", out.getvalue())

    def test_parse_test_file_and_instruction_steps_use_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.txt"
            path.write_text("PLUGIN: esp32 expect\n  GET / -> 500\nEND\nVERIFY: error\n")
            with redirect_stdout(io.StringIO()) as out:
                steps = core.parse_test_file(path)
        self.assertEqual([s["command"] for s in steps], ["PLUGIN", "VERIFY"])
        self.assertEqual(steps[0]["block"], ["GET / -> 500"])
        self.assertIn("GET / -> 500", out.getvalue())


class PluginBlockStepTests(StubCase):
    def setUp(self):
        super().setUp()
        core._plugins = None
        self.addCleanup(setattr, core, "_plugins", None)
        env = patch.dict(os.environ, {"MT_PLUGINS": f"esp32={self.url}", "PLUGIN_TOKEN": ""})
        env.start()
        self.addCleanup(env.stop)

    def run_text(self, text):
        steps = core.parse_steps(text.split("\n"))
        out = io.StringIO()
        with redirect_stdout(out):
            ok = core.execute_step(steps[0])
        return ok, out.getvalue()

    def test_block_is_sent_as_script(self):
        ok, out = self.run_text('PLUGIN: esp32 expect device=aa:bb\n'
                                '  GET /temperature.json -> 200 {"temperature_c":22.5}\n'
                                '\n  # keep me\n  GET /status -> 500\nEND')
        self.assertTrue(ok, out)
        self.assertEqual(self.posted_jobs(), [{
            "action": "expect", "device": "aa:bb",
            "params": {"script": 'GET /temperature.json -> 200 {"temperature_c":22.5}\n'
                                 '# keep me\nGET /status -> 500'}}])
        self.assertIn("      GET /status -> 500", out)
        self.assertIn("script (3 lines)", out)

    def test_variables_are_resolved_inside_the_block(self):
        core.VARIABLES["TEMP"] = "19.0"
        self.addCleanup(core.VARIABLES.pop, "TEMP", None)
        ok, _ = self.run_text("PLUGIN: esp32 expect\n  GET /t -> 200 ${TEMP} ${UNSET}\nEND")
        self.assertTrue(ok)
        self.assertEqual(self.posted_jobs()[0]["params"], {"script": "GET /t -> 200 19.0 ${UNSET}"})

    def test_block_on_action_without_script_fails_before_a_job(self):
        ok, out = self.run_text("PLUGIN: esp32 scenario name=x\n  something\nEND")
        self.assertFalse(ok)
        self.assertIn("takes no inline block", out)
        self.assertEqual(self.posted_jobs(), [])

    def test_block_and_script_param_together_fail(self):
        ok, out = self.run_text("PLUGIN: esp32 expect script=x\n  y\nEND")
        self.assertFalse(ok)
        self.assertIn("both an inline block and script=", out)
        self.assertEqual(self.posted_jobs(), [])

    def test_unterminated_block_fails_without_a_job(self):
        ok, out = self.run_text("PLUGIN: esp32 expect\n  GET / -> 200")
        self.assertFalse(ok)
        self.assertIn("line 1", out)
        self.assertEqual(self.posted_jobs(), [])

    def test_script_param_on_the_line_still_works_without_a_block(self):
        ok, _ = self.run_text('PLUGIN: esp32 expect script="GET / -> 500"')
        self.assertTrue(ok)
        self.assertEqual(self.posted_jobs()[0]["params"], {"script": "GET / -> 500"})


class AiConfigShapeTests(unittest.TestCase):
    """The AI plugin's replies are reduced to public shapes: no credential
    (pairing token, GitHub token, device code) passes through."""

    def test_public_ai_config_keeps_only_public_fields(self):
        body = {"model": {"selected": "q", "installed": ["q", "l"]},
                "token": {"configured": True, "last4": "9876", "value": "secret-9876"},
                "provider": "copilot",
                "copilot": {"configured": True, "user": "octo", "last4": "en42",
                            "token": "gho_secret", "saved_at": "t", "model": "gpt-4o",
                            "models": [{"id": "gpt-4o", "name": "GPT-4o", "vendor": "OpenAI"}, "x"]},
                "api_version": 3, "service": "monkeytester-ai"}
        out = plugin_client.public_ai_config(body)
        self.assertEqual(out["provider"], "copilot")
        self.assertEqual(out["token"], {"configured": True, "last4": "9876"})
        self.assertEqual(out["copilot"]["models"],
                         [{"id": "gpt-4o", "name": "GPT-4o", "vendor": "OpenAI"},
                          {"id": "x", "name": "x", "vendor": ""}])
        self.assertNotIn("secret", json.dumps(out))
        self.assertNotIn("gho_", json.dumps(out))

    def test_unknown_provider_reads_as_local(self):
        self.assertEqual(plugin_client.public_ai_config({"provider": "weird"})["provider"], "local")
        self.assertFalse(plugin_client.public_ai_config({})["copilot"]["configured"])

    def test_save_refuses_unknown_keys_and_providers_locally(self):
        client = PluginClient("monkeytester-ai", "http://127.0.0.1:1")
        with self.assertRaises(PluginError) as ctx:
            plugin_client.ai_save_config(client, {"endpoint": "x"})
        self.assertEqual(ctx.exception.code, "bad_request")
        with self.assertRaises(PluginError) as ctx:
            plugin_client.ai_save_config(client, {"provider": "openai"})
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_save_unwraps_config_and_new_token(self):
        client = PluginClient("monkeytester-ai", "http://x")
        with patch.object(client, "_request", return_value={
                "ok": True, "new_token": "fresh-1234",
                "config": {"provider": "local", "token": {"configured": True, "last4": "1234"}}}) as req:
            config, token = plugin_client.ai_save_config(client, {"generate": True})
        self.assertEqual((config["provider"], config["token"]["last4"], token),
                         ("local", "1234", "fresh-1234"))
        # The plugin's spelling of a rotation (a bare "generate" key is refused there).
        self.assertEqual(req.call_args.args[:3], ("POST", "/config", {"token": {"generate": True}}))

    def test_device_flow_drops_the_device_code(self):
        client = PluginClient("monkeytester-ai", "http://x")
        with patch.object(client, "_request", return_value={
                "user_code": "ABCD-1234", "device_code": "secret-dc",
                "verification_uri": "https://github.com/login/device", "interval": 5}):
            out = plugin_client.ai_copilot_device_start(client)
        self.assertEqual(out, {"user_code": "ABCD-1234",
                               "verification_uri": "https://github.com/login/device", "interval": 5})
        with patch.object(client, "_request", return_value={
                "state": "done", "configured": True, "user": "octo", "token": "gho_x"}):
            out = plugin_client.ai_copilot_device_poll(client)
        self.assertEqual((out["state"], out["copilot"]["user"]), ("done", "octo"))
        self.assertNotIn("gho_x", json.dumps(out))
        with patch.object(client, "_request", return_value={
                "state": "error", "error": {"code": "x", "message": "boom"}}):
            self.assertEqual(plugin_client.ai_copilot_device_poll(client),
                             {"state": "error", "error": "boom"})


class RemoteProviderShapeTests(unittest.TestCase):
    """The Remote server provider: its block is public-only and its keys are
    forwarded to the plugin together."""

    def test_public_remote_never_carries_the_key(self):
        body = {"provider": "remote",
                "remote": {"url": "http://192.168.1.10:8080", "model": "video-model",
                           "models": [{"id": "video-model"}, {"id": "qwen2.5-vl-7b"}, "x", {}],
                           "resize": False, "api_key": "sk-secret-5678",
                           "key": {"configured": True, "last4": "5678", "value": "sk-secret-5678"},
                           "error": None}}
        out = plugin_client.public_ai_config(body)
        self.assertEqual(out["provider"], "remote")
        self.assertEqual(out["remote"], {"url": "http://192.168.1.10:8080", "model": "video-model",
                                         "models": [{"id": "video-model"}, {"id": "qwen2.5-vl-7b"},
                                                    {"id": "x"}],
                                         "resize": False,
                                         "key": {"configured": True, "last4": "5678"},
                                         "error": None})
        self.assertNotIn("sk-secret", json.dumps(out))

    def test_remote_defaults_when_the_plugin_has_no_remote_block(self):
        remote = plugin_client.public_ai_config({"provider": "local"})["remote"]
        self.assertEqual(remote, {"url": "", "model": "", "models": [], "resize": True,
                                  "key": {"configured": False, "last4": ""}, "error": None})

    def test_save_accepts_remote_keys_and_provider(self):
        client = PluginClient("monkeytester-ai", "http://x")
        payload = {"remote_url": "http://r:8080", "remote_model": "video-model",
                   "remote_resize": True, "remote_api_key": None}
        with patch.object(client, "_request", return_value={"ok": True, "config": {
                "provider": "remote", "remote": {"url": "http://r:8080"}}}) as req:
            config, token = plugin_client.ai_save_config(client, payload)
            plugin_client.ai_save_config(client, {"provider": "remote"})
        self.assertEqual(req.call_args_list[0].args, ("POST", "/config", payload))
        self.assertEqual((config["provider"], config["remote"]["url"], token),
                         ("remote", "http://r:8080", ""))


class ViewerPluginListTests(StubCase):
    """The viewer's plugin list: MT_PLUGINS minus monkeytester-ai, plus
    monkeytester-ai at its Settings address."""

    def test_env_entry_for_the_ai_plugin_is_replaced_by_the_address(self):
        env = {"MT_PLUGINS": "esp32-sensor=http://127.0.0.1:8091,monkeytester-ai=http://old:8091",
               "PLUGIN_TOKEN": "shared"}
        plugins = plugin_client.viewer_plugins(env, "http://new:8091", "pair-1234")
        self.assertEqual({n: (c.url, c.token) for n, c in plugins.items()},
                         {"esp32-sensor": ("http://127.0.0.1:8091", "shared"),
                          "monkeytester-ai": ("http://new:8091", "pair-1234")})
        self.assertNotIn("monkeytester-ai", plugin_client.viewer_plugins(env, ""))

    def test_malformed_env_still_lists_the_ai_plugin(self):
        out = plugin_client.plugins_overview({"MT_PLUGINS": "broken"}, timeout=1,
                                             ai_url="http://127.0.0.1:9")
        self.assertIn("MT_PLUGINS is malformed", out["error"])
        self.assertEqual([(p["name"], p["source"]) for p in out["plugins"]],
                         [("monkeytester-ai", "settings")])
        status = plugin_client.plugins_status({"MT_PLUGINS": "broken"}, timeout=1,
                                              ai_url="http://127.0.0.1:9")
        self.assertEqual(sorted(status), ["MT_PLUGINS", "monkeytester-ai"])

    def test_validate_address_checks_contract_name_and_config(self):
        # The stub speaks the contract but is not monkeytester-ai.
        with self.assertRaises(PluginError) as ctx:
            plugin_client.ai_validate_address(self.url + "/")
        self.assertEqual(ctx.exception.code, "wrong_plugin")
        for bad in ("", "ftp://x", "http://a b", "192.168.1.2:8091"):
            with self.assertRaises(PluginError) as ctx:
                plugin_client.ai_validate_address(bad)
            self.assertEqual(ctx.exception.code, "invalid_request", bad)
        with self.assertRaises(PluginError) as ctx:
            plugin_client.ai_validate_address("http://127.0.0.1:9", timeout=1)
        self.assertEqual(ctx.exception.code, "unreachable")

    def test_validate_address_maps_401_to_a_token_error(self):
        health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                  "plugin": {"name": "monkeytester-ai"}}

        def fake(self_, method, path, payload=None, timeout=None):
            if path == "/health":
                return health
            raise PluginError("unauthorized", "unauthorized", status=401, plugin="monkeytester-ai")

        with patch.object(PluginClient, "_request", fake):
            with self.assertRaises(PluginError) as ctx:
                plugin_client.ai_validate_address("http://ai:8091", token="wrong")
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("unauthorized", 401))
        self.assertIn("pairing token", ctx.exception.message)
        with patch.object(PluginClient, "_request",
                          lambda s, m, p, payload=None, timeout=None: health if p == "/health" else {}):
            self.assertEqual(plugin_client.ai_validate_address("http://ai:8091/", token="ok"),
                             "http://ai:8091")


class PluginConfigListTests(StubCase):
    """The config.json-owned plugin list: Settings → Device Plugins owns the
    list once `plugins` exists, MT_PLUGINS is the import-before that."""

    def test_parse_config_map_normalises_and_validates(self):
        pc = plugin_client
        self.assertIsNone(pc.parse_config_plugins({"video_size": "100%"}))
        self.assertEqual(pc.parse_config_plugins(
            {"plugins": {"esp32-sensor": self.url + "/"}}),
            {"esp32-sensor": self.url})
        for bad in ({"plugins": []},
                    {"plugins": {"Bad Name": self.url}},
                    {"plugins": {"esp32;sensor": self.url}},
                    {"plugins": {"": self.url}},
                    {"plugins": {"monkeytester-ai": self.url}},
                    {"plugins": {"x": "ftp://a"}}):
            with self.assertRaises(PluginError) as ctx:
                pc.parse_config_plugins(bad)
            self.assertEqual(ctx.exception.code,
                             "invalid_request" if "ftp" in str(bad["plugins"]) else "config")

    def test_load_plugins_config_owns_the_list(self):
        pc = plugin_client
        env = dict(os.environ, MT_PLUGINS="envone=http://127.0.0.1:9")
        plugins = pc.load_plugins({"plugins": {"cfgone": self.url}}, env)
        self.assertEqual({n: c.url for n, c in plugins.items()}, {"cfgone": self.url})
        plugins = pc.load_plugins({"plugins": {"cfgone": self.url}},
                                  dict(env, PLUGIN_TOKEN="tok-1"))
        self.assertEqual(plugins["cfgone"].token, "tok-1")

    def test_load_plugins_without_the_map_still_uses_env(self):
        pc = plugin_client
        env = dict(os.environ, MT_PLUGINS="esp32=http://127.0.0.1:9,monkeytester-ai=http://ai:1")
        plugins = pc.load_plugins({"video_size": "100%"}, env)
        self.assertEqual({n: c.url for n, c in plugins.items()},
                         {"esp32": "http://127.0.0.1:9"})  # the AI entry never joins

    def test_validate_device_plugin_accepts_the_stub(self):
        self.assertEqual(plugin_client.validate_device_plugin("hub", self.url + "/"),
                         (self.url, Stub.health))

    def test_validate_device_plugin_refuses_dead_or_foreign(self):
        Stub.health = {}
        with self.assertRaises(PluginError) as ctx:
            plugin_client.validate_device_plugin("hub", self.url)
        self.assertEqual(ctx.exception.code, "contract")
        Stub.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1}
        with self.assertRaises(PluginError) as ctx:
            plugin_client.validate_device_plugin("hub", "http://127.0.0.1:9", timeout=1)
        self.assertEqual(ctx.exception.code, "unreachable")

    def test_viewer_plugins_prefer_the_saved_map(self):
        pc = plugin_client
        env = dict(os.environ, MT_PLUGINS="stale=http://127.0.0.1:9")
        plugins = pc.viewer_plugins(env, ai_url=self.url,
                                    config={"plugins": {"saved": self.url}})
        self.assertEqual({n: c.url for n, c in plugins.items()},
                         {"saved": self.url, "monkeytester-ai": self.url})


if __name__ == "__main__":
    unittest.main()
