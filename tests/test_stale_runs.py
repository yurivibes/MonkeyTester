"""Stale-run watchdog: a run whose runner died mid-flight reports Error,
never a phantom Running, and stamps its state as interrupted.

Covers the viewer side (_is_running, _interrupt_stale_run, the run badge,
delete_runs, get_run_result) and the runner side (core.mark_run_interrupted
called by run_on_device.py's SIGTERM handler).
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mt-tests" / "scripts"))
with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core
import app

class CoreInterruptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_root = Path(self.temp.name) / "run_1"
        self.run_root.mkdir()
        (self.run_root / "run_metadata.json").write_text(json.dumps({
            "timestamp": "2026-11-12 23:04:57",
            "status": "running",
            "passed": 0, "failed": 0, "total": 0,
            "devices": [
                {"id": "device_1", "status": "completed_with_failures"},
                {"id": "ios_device_1", "status": "running"},
                {"id": "device_2", "status": "pending"},
            ],
        }))
        dev_pending = self.run_root / "devices" / "ios_device_1" / "pending_tests.json"
        dev_pending.parent.mkdir(parents=True)
        dev_pending.write_text(json.dumps({
            "tests": [], "progress": {"phase": "running_step", "message": "x"},
        }))
        (self.run_root / "pending_tests.json").write_text(json.dumps({
            "tests": [], "progress": {"phase": "discovering_tests"},
        }))

    def tearDown(self):
        core.RUN_ROOT = core._root_metadata_path.__self__ if False else core.RUN_ROOT

    def test_mark_run_interrupted_stamps_root_and_devices(self):
        original = core.RUN_ROOT
        core.RUN_ROOT = self.run_root
        try:
            core.mark_run_interrupted("interrupted: SIGTERM")
            meta = json.loads((self.run_root / "run_metadata.json").read_text())
            self.assertEqual(meta["status"], "interrupted")
            self.assertEqual(meta["error"], "interrupted: SIGTERM")
            self.assertIn("completed", meta)
            statuses = {d["id"]: d["status"] for d in meta["devices"]}
            self.assertEqual(statuses["ios_device_1"], "interrupted")
            self.assertEqual(statuses["device_1"], "completed_with_failures")
            self.assertEqual(statuses["device_2"], "pending")
            root_pending = json.loads(
                (self.run_root / "pending_tests.json").read_text())
            dev_pending = json.loads(
                (self.run_root / "devices" / "ios_device_1" / "pending_tests.json")
                .read_text())
            self.assertEqual(root_pending["progress"]["phase"], "interrupted")
            self.assertEqual(dev_pending["progress"]["phase"], "interrupted")
        finally:
            core.RUN_ROOT = original

    def test_terminal_run_is_not_re_stamped(self):
        original = core.RUN_ROOT
        core.RUN_ROOT = self.run_root
        try:
            (self.run_root / "run_metadata.json").write_text(json.dumps(
                {"status": "completed_with_failures", "devices": []}))
            core.mark_run_interrupted("interrupted: SIGTERM")
            meta = json.loads(
                (self.run_root / "run_metadata.json").read_text())
            self.assertEqual(meta["status"], "completed_with_failures")
            self.assertNotIn("error", meta)
        finally:
            core.RUN_ROOT = original


class ViewerStaleWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runs = Path(self.temp.name)
        self.original_test_dir = app.TEST_DIR
        app.TEST_DIR = self.runs
        self.addCleanup(self._restore_test_dir)
        self.original_stale = app.STALE_RUN_SECS
        app.STALE_RUN_SECS = 60
        self.addCleanup(self._restore_stale)

    def tearDown(self):
        app.TEST_DIR = self.original_test_dir
        app.STALE_RUN_SECS = self.original_stale

    def _restore_test_dir(self):
        app.TEST_DIR = self.original_test_dir

    def _restore_stale(self):
        app.STALE_RUN_SECS = self.original_stale

    def make_run(self, name, status="running", age_secs=None, devices=None):
        run_dir = self.runs / name
        run_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "timestamp": "2026-11-12 23:04:57",
            "status": status,
            "passed": 0, "failed": 0, "total": 0,
        }
        if devices:
            meta["devices"] = devices
        (run_dir / "run_metadata.json").write_text(json.dumps(meta))
        log = run_dir / "console.log"
        log.write_text("step")
        if age_secs is not None:
            old = time.time() - age_secs
            os.utime(log, (old, old))
            os.utime(run_dir / "run_metadata.json", (old, old))
        return run_dir

    def test_fresh_running_run_is_still_running(self):
        self.make_run("run_1", devices=[{"id": "ios_device_1", "status": "running"}])
        self.assertTrue(app._is_running("run_1"))
        listed = [r for r in app.get_runs() if r["name"] == "run_1"][0]
        self.assertTrue(listed["is_running"])
        self.assertEqual(listed["status"], "Running")

    def test_stale_running_run_reports_and_stamps_interrupted(self):
        self.make_run("run_1", devices=[{"id": "ios_device_1", "status": "running"}],
                      age_secs=3600)
        self.assertFalse(app._is_running("run_1"))
        meta = json.loads(((self.runs / "run_1") / "run_metadata.json").read_text())
        self.assertEqual(meta["status"], "interrupted")
        self.assertIn("interrupted", meta["error"])
        self.assertEqual(meta["devices"][0]["status"], "interrupted")
        listed = [r for r in app.get_runs() if r["name"] == "run_1"][0]
        self.assertFalse(listed["is_running"])
        self.assertEqual(listed["status"], "Error")

    def test_interrupted_run_can_be_deleted(self):
        self.make_run("run_1", devices=[{"id": "ios_device_1", "status": "running"}],
                      age_secs=3600)
        self.assertFalse(app._is_running("run_1"))
        outcome = app.delete_runs(["run_1"])
        self.assertEqual(outcome["deleted"], ["run_1"])
        self.assertEqual(outcome["skipped"], [])

    def test_ci_verdict_of_stale_run_is_failed(self):
        self.make_run("run_1", devices=[{"id": "ios_device_1", "status": "running"}],
                      age_secs=3600)
        verdict = app.get_run_result("run_1")
        self.assertEqual(verdict["status"], "failed")
        self.assertIn("interrupted", verdict["reason"])

    def test_metadataless_run_with_stale_pending_is_interrupted(self):
        run_dir = self.runs / "run_2"
        (run_dir / "devices" / "ios_device_1").mkdir(parents=True)
        pf = run_dir / "devices" / "ios_device_1" / "pending_tests.json"
        pf.write_text(json.dumps({"tests": [], "progress": {"phase": "running_step"}}))
        old = time.time() - 3600
        os.utime(pf, (old, old))
        self.assertFalse(app._is_running("run_2"))
        saved = json.loads(pf.read_text())
        self.assertEqual(saved["progress"]["phase"], "interrupted")

    def test_ios_device_progress_is_visible_to_the_running_check(self):
        # Before the glob fix, devices/ios_device_*/progress was invisible.
        run_dir = self.runs / "run_3"
        (run_dir / "devices" / "ios_device_1").mkdir(parents=True)
        pf = run_dir / "devices" / "ios_device_1" / "pending_tests.json"
        pf.write_text(json.dumps({"tests": [], "progress": {"phase": "running_step"}}))
        self.assertTrue(app._is_running("run_3"))


if __name__ == "__main__":
    unittest.main()
