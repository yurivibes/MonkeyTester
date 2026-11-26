"""The runner's HTTP client against a stub of the Mobile Controller API."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "mt-tests" / "scripts"))
import controller_client as client

PNG = b"\x89PNG" + b"0" * (client.MIN_RENDERED_SCREENSHOT_BYTES + 10)


class Stub(BaseHTTPRequestHandler):
    calls = []
    health = {"ok": True, "api_version": client.EXPECTED_API_VERSION}
    fail_actions = set()
    screen_fails = False
    devices = {}

    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        Stub.calls.append(("GET", self.path, self.headers.get("X-Controller-Token")))
        if self.path == "/health":
            return self.reply(200, Stub.health)
        if self.path == "/devices":
            return self.reply(200, Stub.devices)
        if self.path.endswith("/frame"):
            return self.reply(200, PNG, "image/png")
        if self.path.endswith("/screen"):
            if Stub.screen_fails:
                return self.reply(502, {"error": {"code": "device_error", "message": "offline"}})
            return self.reply(200, {"width": 100, "height": 200})
        return self.reply(404, {"error": {"code": "not_found", "message": "not found"}})

    def do_DELETE(self):
        Stub.calls.append(("DELETE", self.path, None))
        return self.reply(200, {"ok": True})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.endswith("/apps"):
            Stub.calls.append(("APP", self.path, raw))
            return self.reply(200, {"ok": True})
        body = json.loads(raw or b"{}")
        Stub.calls.append(("POST", self.path, body))
        if body.get("action") in Stub.fail_actions:
            return self.reply(502, {"error": {"code": "device_error", "message": "boom"}})
        if self.path.endswith("/recordings"):
            return self.reply(200, {"ok": True, "token": "0123456789ab"})
        return self.reply(200, {"ok": True})


class ControllerClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Stub.calls = []
        Stub.fail_actions = set()
        Stub.screen_fails = False
        client._screen_sizes.clear()
        Stub.devices = {"devices": [
            {"id": "1.1.1.1:5", "platform": "android", "model": "Pixel"},
            {"id": "AB-1", "platform": "ios", "model": None}], "errors": {}}
        Stub.health = {"ok": True, "api_version": client.EXPECTED_API_VERSION}
        env = {"CONTROLLER_URL": f"http://127.0.0.1:{self.server.server_address[1]}",
               "DEVICE_ID": "1.1.1.1:5", "CONTROLLER_TOKEN": ""}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = patch.object(client.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def actions(self):
        return [c[2]["action"] for c in Stub.calls if c[1].endswith("/actions")]

    def test_lists_devices_and_model(self):
        devices, error = client.list_devices()
        self.assertIsNone(error)
        self.assertEqual([d["id"] for d in devices], ["1.1.1.1:5", "AB-1"])
        self.assertEqual(client.get_device_model(), "Pixel")

    def test_unset_or_unreachable_controller_is_an_error_not_a_crash(self):
        with patch.dict(os.environ, {"CONTROLLER_URL": ""}):
            self.assertIn("CONTROLLER_URL", client.list_devices()[1])
            self.assertIn("CONTROLLER_URL", client.check_api_version())
        with patch.dict(os.environ, {"CONTROLLER_URL": "http://127.0.0.1:9"}):
            self.assertIn("unreachable", client.list_devices()[1])

    def test_api_version_mismatch_is_reported(self):
        self.assertIsNone(client.check_api_version())
        Stub.health = {"ok": True, "api_version": 99}
        self.assertIn("99", client.check_api_version())

    def test_token_is_sent_when_configured(self):
        with patch.dict(os.environ, {"CONTROLLER_TOKEN": "s3cret"}):
            client.list_devices()
        self.assertEqual(Stub.calls[-1][2], "s3cret")

    def test_screenshot_writes_where_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "shot.png"
            self.assertEqual(client.screenshot(path), str(path))
            self.assertEqual(path.read_bytes(), PNG)

    def test_restart_stops_clears_then_launches(self):
        self.assertTrue(client.restart_app("com.example"))
        self.assertEqual(self.actions(), ["terminate", "clear-data", "unlock", "launch"])

    def test_restart_can_keep_app_data(self):
        self.assertTrue(client.restart_app("com.example", clear_data=False))
        self.assertEqual(self.actions(), ["terminate", "unlock", "launch"])

    def test_restart_fails_when_launch_fails(self):
        Stub.fail_actions = {"launch"}
        self.assertFalse(client.restart_app("com.example"))

    def test_apk_is_sent_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "app.apk"
            apk.write_bytes(b"PK\x03\x04apk")
            self.assertTrue(client.install_apk(str(apk)))
        self.assertEqual(Stub.calls[-1], ("APP", "/devices/1.1.1.1:5/apps", b"PK\x03\x04apk"))

    def test_delete_uses_the_delete_action(self):
        client.delete_selected(count=2)
        self.assertEqual(Stub.calls[-1][1:], ("/devices/1.1.1.1:5/actions", {"action": "delete", "count": 2}))

    def test_uninstall_uses_delete_with_the_bundle_in_the_path(self):
        self.assertTrue(client.uninstall_app("com.example"))
        self.assertEqual(Stub.calls[-1][:2], ("DELETE", "/devices/1.1.1.1:5/apps/com.example"))

    def test_recording_stops_and_downloads_by_token(self):
        self.assertTrue(client.start_recording())
        client.stop_recording()
        paths = [c[1] for c in Stub.calls]
        self.assertIn("/devices/1.1.1.1:5/recordings", paths)
        self.assertIn("/recordings/0123456789ab/stop", paths)
        self.assertIn("/recordings/0123456789ab/video", paths)

    def test_recording_has_no_time_limit_by_default(self):
        self.assertTrue(client.start_recording())
        client.stop_recording()
        start = [c for c in Stub.calls if c[1] == "/devices/1.1.1.1:5/recordings"][-1]
        self.assertEqual(start[2], {})

    def test_recording_sends_an_explicit_time_limit(self):
        self.assertTrue(client.start_recording(time_limit=30))
        client.stop_recording()
        start = [c for c in Stub.calls if c[1] == "/devices/1.1.1.1:5/recordings"][-1]
        self.assertEqual(start[2], {"time_limit": 30})

    def test_one_platform_failing_leaves_the_other_usable(self):
        Stub.devices = {"devices": [{"id": "1.1.1.1:5", "platform": "android", "model": None}],
                        "errors": {"ios": "no Xcode"}}
        devices, error = client.list_devices()
        self.assertEqual(([d["id"] for d in devices], error), (["1.1.1.1:5"], None))

    def test_no_devices_and_a_failure_is_an_error(self):
        Stub.devices = {"devices": [], "errors": {"ios": "no Xcode", "android": "no adb"}}
        self.assertEqual(client.list_devices(), ([], "android: no adb; ios: no Xcode"))

    def test_error_text_includes_message_and_code(self):
        body = {"error": {"code": "device_locked", "message": "needs a passcode"}}
        self.assertEqual(client._error_text(body), "needs a passcode (device_locked)")

    def test_guessed_screen_size_is_not_remembered(self):
        Stub.screen_fails = True
        self.assertEqual(client._cached_screen_size(), (750, 1334))
        Stub.screen_fails = False
        self.assertEqual(client._cached_screen_size(), (100, 200))


class DevicePlatformSplitTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
            import run_on_device
        self.split = run_on_device.split_devices_by_platform

    def test_splits_by_what_the_controller_reports(self):
        devices = [{"id": "a:1", "platform": "android"}, {"id": "B", "platform": "ios"}]
        self.assertEqual(self.split(["B", "a:1"], devices), (["a:1"], ["B"], None))

    def test_device_the_controller_does_not_serve_is_an_error(self):
        android, ios, error = self.split(["gone:1"], [])
        self.assertIn("gone:1", error)
        self.assertEqual((android, ios), ([], []))


if __name__ == "__main__":
    unittest.main()
