"""A platform whose app didn't build (CI build or signing error) shows up in
the run with its devices FAILED and the build error as the reason."""
import argparse
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mt-tests" / "scripts"))
with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core
    import run_on_device
import app
from tests.test_app_api import ServerCase
from tests.build_fixture import BUILD_ID, add_build

_spec = importlib.util.spec_from_file_location(
    "submit_bf", Path(__file__).resolve().parents[1] / "ci" / "mt-submit" / "submit.py")
submit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(submit)

DEVICES = [
    {"id": "ZA2235GDS8", "platform": "android", "name": "moto"},
    {"id": "00008030-000C50D42EE3802E", "platform": "ios", "name": "iPhone SE"},
]


class ParseTests(unittest.TestCase):
    def test_parse(self):
        failed, err = run_on_device.parse_build_failed(["ios=No profile\n  includes Hotspot"])
        self.assertIsNone(err)
        self.assertEqual(failed, {"ios": "iOS app didn't build: No profile includes Hotspot"})
        _, err = run_on_device.parse_build_failed(["windows=x"])
        self.assertIn("--build-failed", err)

    def test_entries_use_the_connected_devices(self):
        with patch.object(run_on_device.controller, "list_devices", return_value=(DEVICES, None)):
            entries = run_on_device.build_failed_device_entries({"ios": "iOS app didn't build"}, 1, 0)
        self.assertEqual(entries, [{
            "id": "ios_device_1", "platform": "ios", "message": "iOS app didn't build",
            "serial": "ios:00008030-000C50D42EE3802E", "name": "iPhone SE"}])

    def test_no_device_connected_still_shows_the_platform(self):
        with patch.object(run_on_device.controller, "list_devices", return_value=([], "down")):
            entries = run_on_device.build_failed_device_entries({"ios": "m"}, 1, 0)
        self.assertEqual([(e["id"], e["name"]) for e in entries],
                         [("ios_device_1", "iPhone (none connected)")])


class RecordBuildFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old_root, old_run_root = core.PROJECT_ROOT, core.RUN_ROOT
        self.addCleanup(setattr, core, "RUN_ROOT", old_run_root)
        self.addCleanup(core._set_project_root, old_root)
        self.addCleanup(setattr, core, "_multi_case_root", None)
        core.RUN_ROOT = Path(self.tmp.name) / "run_1"
        core._set_project_root(core.RUN_ROOT)
        case = Path(self.tmp.name) / "tests" / "onboarding"
        case.mkdir(parents=True)
        (case / "INSTRUCTION.txt").write_text("MODE: once\n")
        (case / "01_a.txt").write_text("WAIT: 0\nWAIT: 0\n")
        (case / "02_b.txt").write_text("WAIT: 0\n")
        args = argparse.Namespace(test_files=None, test_file=None, test_dir=str(case.parent))
        with redirect_stdout(io.StringIO()):
            self.files = core.resolve_test_files(args)

    def test_every_test_is_unavailable_with_the_build_error_and_log(self):
        core.initialize_multi_device_run(
            ["ZA2235GDS8"], build_failed=[{"id": "ios_device_1", "platform": "ios", "name": "iPhone SE"}])
        log = Path(self.tmp.name) / "xcodebuild.log"
        log.write_text("error: No profile\n")
        with redirect_stdout(io.StringIO()):
            n = core.record_setup_failure(self.files, "ios_device_1", "iOS app didn't build: no profile", log)
        self.assertEqual(n, 2)
        meta = json.loads((core.RUN_ROOT / "run_metadata.json").read_text())
        ios = [d for d in meta["devices"] if d["id"] == "ios_device_1"][0]
        self.assertEqual((ios["status"], ios["unavailable"], ios["failed"], ios["total"], ios["error"]),
                         ("unavailable", 2, 0, 2, "iOS app didn't build: no profile"))
        self.assertEqual(ios["log"], "devices/ios_device_1/logs/setup_log.txt")
        self.assertEqual((core.RUN_ROOT / ios["log"]).read_text(), "error: No profile\n")
        android = [d for d in meta["devices"] if d["id"] == "device_1"][0]
        self.assertEqual(android["status"], "pending")
        dev = core.RUN_ROOT / "devices" / "ios_device_1"
        result = json.loads((dev / "results" / "onboarding__01_a_result.json").read_text())
        self.assertEqual((result["status"], result["error"], result["log"]),
                         ("UNAVAILABLE", "iOS app didn't build: no profile", ios["log"]))
        self.assertEqual({s["status"] for s in result["steps"]}, {"SKIPPED"})
        pending = json.loads((dev / "pending_tests.json").read_text())
        self.assertEqual([t["status"] for t in pending["tests"]], ["unavailable", "unavailable"])

    def test_without_a_build_log_the_console_log_is_linked(self):
        core.initialize_multi_device_run(["ZA2235GDS8", "ZB"])
        with redirect_stdout(io.StringIO()):
            core.record_setup_failure(self.files, "device_2", "APK installation failed")
        meta = json.loads((core.RUN_ROOT / "run_metadata.json").read_text())
        self.assertEqual(meta["devices"][1]["log"], "console")

    def test_run_result_fails_on_unavailable_tests(self):
        with patch.object(app, "get_run_metadata",
                          return_value={"status": "completed", "passed": 2, "failed": 0,
                                        "unavailable": 2, "total": 4}), \
                patch.object(app, "_read_exit_code", return_value=None):
            result = app.get_run_result("run_x")
        self.assertEqual(result["status"], "failed")
        self.assertIn("unavailable", result["reason"])

    def test_log_urls(self):
        run_dir = core.RUN_ROOT
        (run_dir / "devices" / "d" / "logs").mkdir(parents=True)
        (run_dir / "devices" / "d" / "logs" / "setup_log.txt").write_text("x")
        self.assertEqual(app._setup_log_url("run_1", run_dir, "console"), "/api/logs/run_1")
        self.assertEqual(app._setup_log_url("run_1", run_dir, "devices/d/logs/setup_log.txt"),
                         "/static/run_1/devices/d/logs/setup_log.txt")
        self.assertIsNone(app._setup_log_url("run_1", run_dir, "../../etc/passwd"))


class ViewerTests(ServerCase):
    def setUp(self):
        super().setUp()
        script = self.root / "fake_runner.py"
        script.write_text(
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            "out = argv[argv.index('--output-dir') + 1]\n"
            "n = 1 + max([int(d[4:]) for d in os.listdir(out) if d.startswith('run_') and d[4:].isdigit()] or [0])\n"
            "run = os.path.join(out, f'run_{n}')\n"
            "os.makedirs(run)\n"
            "json.dump(argv, open(os.path.join(run, 'argv.json'), 'w'))\n")
        for p in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                  patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                  patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            p.start()
            self.addCleanup(p.stop)

    def post(self, fields, files):
        add_build()
        ctype, body = submit.build_multipart({"build_request": BUILD_ID, **fields}, files)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    TESTS = [("tests", "c/INSTRUCTION.txt", b"MODE: once\n"), ("tests", "c/t.txt", b"WAIT: 0\n")]

    def argv(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            found = sorted(self.runs.glob("run_*/argv.json"))
            if found:
                return json.loads(found[-1].read_text())
            time.sleep(0.1)
        self.fail("runner never started")

    def test_build_failure_is_passed_to_the_runner(self):
        status, _ = self.post({"device_mode": "all", "build_failed_ios": "No profile"},
                              [("apk", "app.apk", b"PK"), ("build_log_ios", "ios.txt", b"error: x\n"),
                               *self.TESTS])
        self.assertEqual(status, 200)
        argv = self.argv()
        self.assertIn("--build-failed", argv)
        self.assertEqual(argv[argv.index("--build-failed") + 1], "ios=No profile")
        self.assertEqual(argv[argv.index("--wait-for-platform") + 1], "android")
        self.assertEqual(argv.count("--wait-for-platform"), 1)
        platform, _, log = argv[argv.index("--build-log") + 1].partition("=")
        self.assertEqual(platform, "ios")
        self.assertTrue(log.endswith("build_log_ios.txt"))

    def test_every_build_failed_still_makes_a_run(self):
        status, _ = self.post({"device_mode": "all", "build_failed_ios": "a", "build_failed_android": "b"},
                              list(self.TESTS))
        self.assertEqual(status, 200)
        self.assertEqual(self.argv().count("--build-failed"), 2)

    def test_rejected_with_an_app_for_that_platform(self):
        status, payload = self.post({"device_mode": "all", "build_failed_ios": "x"},
                                    [("apk", "app.apk", b"PK"), ("ios_app", "app.ipa", b"PK"), *self.TESTS])
        self.assertEqual(status, 400)
        self.assertIn("build_failed_ios", payload["error"])


class SubmitTests(unittest.TestCase):
    def test_build_error_is_sent_and_mode_covers_both_platforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "tests" / "c"
            case.mkdir(parents=True)
            (case / "INSTRUCTION.txt").write_text("MODE: once\n")
            (case / "t.txt").write_text("WAIT: 0\n")
            apk = Path(tmp) / "app.apk"
            apk.write_bytes(b"PK")
            args = argparse.Namespace(
                url="http://v", android_app=str(apk), ios_app=None, app=None, platform=None,
                tests_dir=str(case.parent), branch="b", cases="", build_request="",
                timeout=5, poll=0, android_build_error="", ios_build_error="No\nprofile",
                ios_build_log=str(apk.parent / "x.log"))
            (apk.parent / "x.log").write_text("error: No profile\n")
            with patch.object(submit, "request_json",
                              side_effect=[(200, {"run_id": "run_1"}), (200, {"status": "failed"})]) as rq, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(submit.run(args), 1)
            body = rq.call_args_list[0].args[1]
            self.assertIn(b'name="build_failed_ios"\r\n\r\nNo profile', body)
            self.assertIn(b'name="device_mode"\r\n\r\nall', body)
            self.assertIn(b'name="build_log_ios"; filename="ios_build_log.txt"', body)


if __name__ == "__main__":
    unittest.main()
