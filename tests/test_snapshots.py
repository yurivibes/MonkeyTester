"""What stays in MonkeyTester of SNAPSHOT / ASSERT_LAYOUT now that the DIFF
plugin compares (MonkeyTester-Plugin-Diff, which also has the image and
layout unit tests): record mode in run metadata, the upload of baselines,
the baseline bundle and the CI verdict of a record run."""
import io
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import app
from tests.build_fixture import BUILD_ID, add_build
from tests.test_app_api import ServerCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mt-tests" / "scripts"))
sys.path.insert(0, str(ROOT / "ci" / "mt-submit"))
import submit
import test_runner_core as core


def png(w, h, rgb=(200, 200, 200)):
    """A solid w x h RGB PNG (stdlib only)."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body)))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class RunFlagsTests(unittest.TestCase):
    def test_run_flags(self):
        with patch.dict(os.environ, {"MT_RECORD_SNAPSHOTS": "1", "MT_CASE": "eula"}):
            self.assertTrue(core.record_mode())
            self.assertEqual(core.run_flags(), {"record_snapshots": True, "case": "eula"})
        with patch.dict(os.environ, {"MT_RECORD_SNAPSHOTS": "0", "MT_CASE": ""}):
            self.assertFalse(core.record_mode())
            self.assertEqual(core.run_flags(), {})


# Records its argv and the staged test files (the stage is removed after the run).
LISTING_RUNNER = """
import json, os, sys
argv = sys.argv[1:]
out = argv[argv.index("--output-dir") + 1]
tests = argv[argv.index("--test-dir") + 1]
n = 1 + max([int(d[4:]) for d in os.listdir(out) if d.startswith("run_") and d[4:].isdigit()] or [0])
run = os.path.join(out, f"run_{n}")
os.makedirs(run)
files = sorted(os.path.relpath(os.path.join(r, f), tests) for r, _, fs in os.walk(tests) for f in fs)
json.dump({"argv": argv, "files": files}, open(os.path.join(run, "fake.json"), "w"))
"""


class UploadTests(ServerCase):
    def setUp(self):
        super().setUp()
        script = self.root / "fake_runner.py"
        script.write_text(LISTING_RUNNER)
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, fields, extra):
        add_build()
        ctype, body = submit.build_multipart(
            {"device_mode": "android", "build_request": BUILD_ID, **fields},
            [("apk", "app.apk", b"PK"), ("tests", "INSTRUCTION.txt", b"MODE: once\n"),
             ("tests", "a.txt", b"DIFF: SNAPSHOT home\n")] + extra)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def fake_of(self, run_id):
        fake = self.runs / run_id / "fake.json"
        deadline = time.time() + 10
        while time.time() < deadline and not fake.exists():
            time.sleep(0.1)
        time.sleep(0.1)
        return json.loads(fake.read_text())

    def test_baselines_are_staged_next_to_the_tests(self):
        status, body = self.post({"case": "eula", "record_snapshots": "on"},
                                 [("snapshots", "snapshots/ios/375x667@2x/home.png", png(2, 2))])
        self.assertEqual(status, 200, body)
        fake = self.fake_of(body["run_id"])
        argv = fake["argv"]
        self.assertEqual(fake["files"], ["INSTRUCTION.txt", "a.txt",
                                         "snapshots/ios/375x667@2x/home.png"])
        self.assertIn("--record-snapshots", argv)
        self.assertEqual(argv[argv.index("--case") + 1], "eula")

    def test_bad_snapshot_paths_are_rejected(self):
        for path in ("snapshots/../../x.png", "snapshots/ios/home.png", "snapshots/mac/1x1@1x/a.png",
                     "home.png"):
            status, body = self.post({}, [("snapshots", path, png(2, 2))])
            self.assertEqual(status, 400, path)
        status, body = self.post({}, [("snapshots", "snapshots/ios/375x667@2x/a.png", b"not png")])
        self.assertEqual(status, 400)
        status, body = self.post({"case": "../x"}, [])
        self.assertEqual(status, 400)


class BundleTests(ServerCase):
    def make_run(self, run="run_5", record=False):
        run_dir = self.runs / run
        dev = run_dir / "devices" / "ios_device_1"
        (dev / "outputs" / "snapshots").mkdir(parents=True)
        (dev / "results").mkdir()
        for name in ("eula", "home", "same"):
            (dev / "outputs" / "snapshots" / f"{name}.actual.png").write_bytes(png(2, 2))
        steps = [{"status": "FAIL", "snapshot": {"name": "eula", "status": "missing",
                                                  "baseline_path": "ios/375x667@2x/eula.png",
                                                  "actual": "outputs/snapshots/eula.actual.png"}},
                 {"status": "FAIL", "snapshot": {"name": "home", "status": "mismatch",
                                                  "baseline_path": "ios/375x667@2x/home.png",
                                                  "actual": "outputs/snapshots/home.actual.png"}},
                 {"status": "PASS", "snapshot": {"name": "same", "status": "match",
                                                  "baseline_path": "ios/375x667@2x/same.png",
                                                  "actual": "outputs/snapshots/same.actual.png"}},
                 {"status": "FAIL", "snapshot": {"name": "evil", "status": "missing",
                                                  "baseline_path": "../../../etc/x.png",
                                                  "actual": "../../../../etc/passwd"}}]
        (dev / "results" / "t_result.json").write_text(json.dumps({"test": "t", "steps": steps}))
        meta = {"status": "completed", "case": "eula", "devices": [], "passed": 1, "total": 1}
        if record:
            meta["record_snapshots"] = True
        (run_dir / "run_metadata.json").write_text(json.dumps(meta))
        return run

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_zip_holds_only_new_or_changed_captures_under_the_case(self):
        run = self.make_run()
        status, body = self.get(f"/api/runs/{run}/snapshots.zip")
        self.assertEqual(status, 200)
        names = sorted(zipfile.ZipFile(io.BytesIO(body)).namelist())
        self.assertEqual(names, ["mt-tests/eula/snapshots/ios/375x667@2x/eula.png",
                                 "mt-tests/eula/snapshots/ios/375x667@2x/home.png"])
        self.assertEqual(json.loads(self.get(f"/api/runs/{run}/snapshots.zip?count=1")[1]),
                         {"count": 2})
        self.assertEqual(self.get("/api/runs/run_1/snapshots.zip")[0], 404)

    def test_step_urls_point_into_the_device_dir(self):
        run = self.make_run()
        data = app.get_test_data(run, "t", "ios_device_1")
        self.assertEqual(data["steps"][0]["snapshot"]["actual_url"],
                         f"/static/{run}/devices/ios_device_1/outputs/snapshots/eula.actual.png")


class RecordingResultTests(unittest.TestCase):
    def test_record_mode_never_passes(self):
        meta = {"status": "completed", "passed": 1, "failed": 0, "total": 1, "record_snapshots": True}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "run_1").mkdir()
            with patch.object(app, "TEST_DIR", Path(tmp)), \
                    patch.object(app, "get_run_metadata", return_value=meta):
                self.assertEqual(app.get_run_result("run_1")["status"], "recording")
                meta["failed"] = 1
                self.assertEqual(app.get_run_result("run_1")["status"], "failed")

    def test_submit_treats_recording_as_failure_and_sends_baselines(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app.ipa").write_bytes(b"ipa")
            case = Path(tmp) / "cases" / "eula"
            (case / "snapshots" / "ios" / "375x667@2x").mkdir(parents=True)
            (case / "INSTRUCTION.txt").write_text("MODE: once")
            (case / "t.txt").write_text("DIFF: SNAPSHOT home")
            (case / "snapshots" / "ios" / "375x667@2x" / "home.png").write_bytes(png(2, 2))
            args = submit.argparse.Namespace(
                url="http://viewer", app=str(Path(tmp) / "app.ipa"), platform="ios",
                tests_dir=str(Path(tmp) / "cases"), branch="b", timeout=5, poll=0)
            sent = []

            def fake(url, data=None, content_type=None, timeout=60):
                if data is None:
                    return 200, {"status": "recording", "reason": "record mode"}
                sent.append(data)
                return 200, {"run_id": "run_1"}
            with patch.object(submit, "request_json", side_effect=fake), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(submit.run(args), 1)
            self.assertIn(b'filename="eula/snapshots/ios/375x667@2x/home.png"', sent[0])
            self.assertIn(b'filename="eula/t.txt"', sent[0])


if __name__ == "__main__":
    unittest.main()
