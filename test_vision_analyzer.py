"""vision_analyzer against a stub of the OpenAI-style chat completions API."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "mt-tests" / "scripts"))
import screenshot_diff
import vision_analyzer as vision


class Stub(BaseHTTPRequestHandler):
    replies = []  # (status, body) for each request, in order; then a default success
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.requests.append((self.headers.get("Authorization"), body))
        status, payload = Stub.replies.pop(0) if Stub.replies else (200, Stub.ok('{"found": true}'))
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def ok(text):
        return {"choices": [{"message": {"content": text}}]}


class VisionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Stub.replies, Stub.requests = [], []
        url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"
        env = patch.dict(os.environ, {"VISION_MODEL_API_URL": url, "VISUAL_MODEL_API_KEY": "",
                                      "VISION_MODEL_NAME": "m"})
        env.start()
        self.addCleanup(env.stop)
        sleep = patch.object(vision.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.image = Path(tmp.name) / "s.png"
        self.image.write_bytes(b"png")


class ChatTests(VisionCase):
    def test_every_call_sends_the_api_key(self):
        with patch.dict(os.environ, {"VISUAL_MODEL_API_KEY": "k"}):
            vision.verify_element(self.image, "x")
            vision.check_input_field(self.image, "x")
            vision.describe_screen(self.image)
            screenshot_diff.describe_image(self.image)
            screenshot_diff.compare_images(self.image, self.image)
        self.assertEqual([r[0] for r in Stub.requests], ["Bearer k"] * 5)

    def test_endpoint_is_read_when_called_not_when_imported(self):
        vision.verify_element(self.image, "x")
        self.assertEqual(Stub.requests[0][1]["model"], "m")

    def test_503_is_retried_until_it_passes(self):
        Stub.replies = [(503, {"error": {"code": "model_unavailable", "message": "restarting"}}),
                        (200, Stub.ok('{"found": true}'))]
        self.assertTrue(vision.verify_element(self.image, "x")["found"])
        self.assertEqual(len(Stub.requests), 2)

    def test_lasting_503_reports_the_reason(self):
        Stub.replies = [(503, {"error": {"code": "model_unavailable", "message": "restarting"}})] * 3
        with self.assertRaisesRegex(vision.VisionError, "restarting"):
            vision.verify_element(self.image, "x")
        self.assertEqual(len(Stub.requests), 3)

    def test_old_copilot_provider_env_is_ignored(self):
        # The viewer no longer talks to Copilot: the plugin endpoint is always used.
        with patch.dict(os.environ, {"VISION_PROVIDER": "copilot", "VISUAL_MODEL_API_KEY": "pair"}):
            vision.verify_element(self.image, "x")
        self.assertEqual(Stub.requests[0][0], "Bearer pair")
        self.assertFalse(hasattr(vision, "copilot_client"))

    def test_401_points_at_the_pairing_token(self):
        Stub.replies = [(401, {"error": {"code": "unauthorized", "message": "bad token"}})]
        with self.assertRaisesRegex(vision.VisionError, "pairing token"):
            vision.verify_element(self.image, "x")

    def test_client_errors_are_not_retried(self):
        Stub.replies = [(401, {"error": {"code": "unauthorized", "message": "bad token"}})]
        with self.assertRaisesRegex(vision.VisionError, "bad token"):
            vision.verify_element(self.image, "x")
        self.assertEqual(len(Stub.requests), 1)
        self.sleep.assert_not_called()

    def test_old_string_error_bodies_still_read_well(self):
        Stub.replies = [(400, {"error": "too big"})]
        with self.assertRaisesRegex(vision.VisionError, "too big"):
            vision.verify_element(self.image, "x")

    def test_connection_refused_is_retried_then_reported(self):
        with patch.dict(os.environ, {"VISION_MODEL_API_URL": "http://127.0.0.1:1/v1/chat/completions"}):
            with self.assertRaisesRegex(vision.VisionError, "127.0.0.1:1"):
                vision.verify_element(self.image, "x")
        self.assertEqual(self.sleep.call_count, 2)

    def test_malformed_reply_is_a_vision_error(self):
        Stub.replies = [(200, {"choices": []})]
        with self.assertRaisesRegex(vision.VisionError, "unusable reply"):
            vision.verify_element(self.image, "x")

    def test_describe_prompt_is_valid_json_example(self):
        vision.describe_screen(self.image)
        prompt = Stub.requests[0][1]["messages"][0]["content"][1]["text"]
        self.assertNotIn("{{", prompt)


class RunnerConfigTests(unittest.TestCase):
    """run_on_device.py reads Run/config.json at import time."""

    def run_with_config(self, config):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        scripts = root / "mt-tests" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy(Path(__file__).parent / "mt-tests" / "scripts" / "run_on_device.py", scripts)
        (root / "Run").mkdir()
        (root / "Run" / "config.json").write_text(json.dumps(config))
        return subprocess.run([sys.executable, str(scripts / "run_on_device.py"), "--help"],
                              capture_output=True, text=True, timeout=30)

    def resolved_endpoint(self, config, mt_plugins=""):
        """VISION_MODEL_API_URL/VISUAL_MODEL_API_KEY as run_on_device.py sets
        them at import, read by a stub test_runner_core that exits at once."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        scripts = root / "mt-tests" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy(Path(__file__).parent / "mt-tests" / "scripts" / "run_on_device.py", scripts)
        (scripts / "test_runner_core.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps({k: os.environ.get(k, '') for k in "
            "('VISION_MODEL_API_URL', 'VISUAL_MODEL_API_KEY', 'VISION_MODEL_NAME')}))\n"
            "sys.exit(0)\n")
        (root / "Run").mkdir()
        (root / "Run" / "config.json").write_text(json.dumps(config))
        env = {k: v for k, v in os.environ.items()
               if k not in ("VISION_MODEL_API_URL", "VISUAL_MODEL_ENDPOINT", "VISUAL_MODEL_API_KEY",
                            "VISION_MODEL_NAME", "VISUAL_MODEL_NAME")}
        env["MT_PLUGINS"] = mt_plugins
        result = subprocess.run([sys.executable, str(scripts / "run_on_device.py"), "--help"],
                                capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1]), result.stderr

    def test_endpoint_comes_from_visual_plugin_url(self):
        # The live config after the one-time import: the legacy keys are still
        # there, and MT_PLUGINS still names another monkeytester-ai address.
        out, _ = self.resolved_endpoint(
            {"visual_endpoint": "http://192.168.1.20:8091/v1/chat/completions",
             "visual_model": "qwen2.5vl:7b", "visual_api_key": "pair-tok-4242",
             "visual_plugin": "monkeytester-ai", "visual_plugin_url": "http://10.0.0.5:8091/"},
            mt_plugins="monkeytester-ai=http://192.168.1.20:8091")
        self.assertEqual(out, {"VISION_MODEL_API_URL": "http://10.0.0.5:8091/v1/chat/completions",
                               "VISUAL_MODEL_API_KEY": "pair-tok-4242", "VISION_MODEL_NAME": ""})

    def test_live_legacy_config_before_the_import_still_works(self):
        out, _ = self.resolved_endpoint(
            {"visual_endpoint": "http://192.168.1.20:8091/v1/chat/completions",
             "visual_model": "qwen2.5vl:7b", "visual_api_key": "pair-tok-4242"},
            mt_plugins="monkeytester-ai=http://192.168.1.20:8091")
        self.assertEqual(out["VISION_MODEL_API_URL"], "http://192.168.1.20:8091/v1/chat/completions")
        self.assertEqual(out["VISION_MODEL_NAME"], "qwen2.5vl:7b")

    def test_visual_plugin_without_address_falls_back_to_legacy_endpoint(self):
        out, err = self.resolved_endpoint(
            {"visual_plugin": "monkeytester-ai", "visual_endpoint": "http://old:1/v1/chat/completions"},
            mt_plugins="monkeytester-ai=http://ignored:8091")
        self.assertEqual(out["VISION_MODEL_API_URL"], "http://old:1/v1/chat/completions")
        self.assertIn("Settings → Device Plugins", err)

    def test_legacy_copilot_config_fails_fast_with_the_hint(self):
        result = self.run_with_config({"visual_provider": "copilot", "copilot_model": "gpt-4o"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("GitHub Copilot is now configured on the MonkeyTester-Plugin-AI plugin: Settings → "
                      "Device Plugins → monkeytester-ai → Configure → GitHub Copilot", result.stderr)


class ParseTests(unittest.TestCase):
    def test_code_fence(self):
        self.assertEqual(vision._parse_json_response('```json\n{"found": true}\n```'), {"found": True})

    def test_nested_objects_survive(self):
        text = 'Here you go: {"found": true, "box": {"x": 1}, "coordinates": [1, 2]} done'
        self.assertEqual(vision._parse_json_response(text),
                         {"found": True, "box": {"x": 1}, "coordinates": [1, 2]})

    def test_skips_prose_braces(self):
        self.assertEqual(vision._parse_json_response('use {this} {"found": false}'), {"found": False})

    def test_no_json(self):
        self.assertFalse(vision._parse_json_response("no idea")["found"])


if __name__ == "__main__":
    unittest.main()
