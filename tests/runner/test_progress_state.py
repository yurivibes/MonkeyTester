import json
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mt-tests" / "scripts"))

import test_runner_core as core


class RunProgressStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_project_root = core.PROJECT_ROOT
        core.PROJECT_ROOT = Path(self.temp_dir.name) / "run_test"
        core.PROJECT_ROOT.mkdir(parents=True)
        self.pending_file = core.PROJECT_ROOT / "pending_tests.json"

    def tearDown(self):
        core.PROJECT_ROOT = self.original_project_root
        self.temp_dir.cleanup()

    def test_progress_preserves_pending_tests_and_marks_active_step(self):
        core.initialize_run_progress("initializing", "Runner started")
        data = core._load_pending_data(self.pending_file)
        data["tests"] = [{
            "name": "login",
            "status": "running",
            "steps": [{"command": "LAUNCH", "args": "", "line": 1}],
            "step_statuses": ["pending"],
        }]
        core._write_json_atomic(self.pending_file, data)

        step = {"command": "LAUNCH", "args": "", "line": 1}
        core.update_run_progress(
            self.pending_file,
            "running_step",
            "Running step 1 of 1",
            test_name="login",
            test_index=1,
            test_total=1,
            step=step,
            step_index=1,
            step_total=1,
            step_status="running",
        )

        saved = json.loads(self.pending_file.read_text())
        self.assertEqual(saved["tests"][0]["status"], "running")
        self.assertEqual(saved["tests"][0]["step_statuses"], ["running"])
        self.assertEqual(saved["progress"]["test_name"], "login")
        self.assertEqual(saved["progress"]["step"], step)

    def test_test_status_update_preserves_current_progress(self):
        core.initialize_run_progress("discovering_tests", "Discovering tests")
        core._update_pending_status(self.pending_file, "unknown", "running")

        saved = json.loads(self.pending_file.read_text())
        self.assertEqual(saved["progress"]["phase"], "discovering_tests")
        self.assertEqual(saved["tests"], [])

    def test_run_metadata_records_adb_device(self):
        original_outputs = core.OUTPUTS_DIR
        try:
            core.OUTPUTS_DIR = core.PROJECT_ROOT / "outputs"
            core.OUTPUTS_DIR.mkdir()
            core.save_run_metadata(device_host="192.168.1.5:42521")
            metadata = json.loads((core.PROJECT_ROOT / "run_metadata.json").read_text())
            self.assertEqual(metadata["device"]["serial"], "192.168.1.5:42521")
        finally:
            core.OUTPUTS_DIR = original_outputs

    def test_multi_device_metadata_keeps_device_results_separate(self):
        original_state = (
            core.RUN_ROOT,
            core.PROJECT_ROOT,
            core.REFERENCES_DIR,
            core.OUTPUTS_DIR,
            core.RESULTS_DIR,
            core.LOGS_DIR,
        )
        try:
            core.RUN_ROOT = Path(self.temp_dir.name) / "multi_run"
            core._set_project_root(core.RUN_ROOT)
            core.initialize_multi_device_run(["192.168.1.5:42521", "192.168.1.6:42521"])

            core.activate_device_output("device_1")
            core.save_run_metadata(
                device_host="192.168.1.5:42521",
                device_id="device_1",
            )
            core.update_run_metadata(
                status="completed",
                passed=2,
                failed=0,
                total=2,
                device_id="device_1",
            )
            core.mark_multi_device_failed("device_2", "APK installation failed")
            core.finish_multi_device_run(2, 1, False)

            aggregate = json.loads((core.RUN_ROOT / "run_metadata.json").read_text())
            self.assertEqual(aggregate["active_device_id"], None)
            self.assertEqual(aggregate["total"], 3)
            self.assertEqual(aggregate["devices"][0]["status"], "completed")
            self.assertEqual(aggregate["devices"][1]["status"], "failed")
            self.assertTrue(
                (core.RUN_ROOT / "devices" / "device_1" / "run_metadata.json").exists()
            )
        finally:
            (
                core.RUN_ROOT,
                core.PROJECT_ROOT,
                core.REFERENCES_DIR,
                core.OUTPUTS_DIR,
                core.RESULTS_DIR,
                core.LOGS_DIR,
            ) = original_state


if __name__ == "__main__":
    unittest.main()
