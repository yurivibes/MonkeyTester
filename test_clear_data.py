"""INSTRUCTION.txt `CLEAR_DATA: first`: only a folder's first test starts from
cleared app data; later tests just relaunch the app."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "visual-tests" / "scripts"))
with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core


def make_case(root, instruction, names=("01_a.txt", "02_b.txt", "03_c.txt")):
    case = Path(root) / "case"
    case.mkdir()
    (case / "INSTRUCTION.txt").write_text(instruction)
    for n in names:
        (case / n).write_text("WAIT: 0\n")
    return case, [str(case / n) for n in names]


class HeaderParsingTests(unittest.TestCase):
    def test_defaults(self):
        opts, body = core.parse_instruction_options("WAIT: 1\n")
        self.assertEqual(opts, {"mode": "everytime", "clear_data": "everytime",
                                "reinstall_app": False})
        self.assertIn("WAIT: 1", body)

    def test_both_keys_any_order_with_comments(self):
        opts, body = core.parse_instruction_options(
            "# setup\nCLEAR_DATA: first\n\nMODE: once\nWAIT: 1\n")
        self.assertEqual(opts, {"mode": "once", "clear_data": "first",
                                "reinstall_app": False})
        self.assertNotIn("CLEAR_DATA", body)
        self.assertNotIn("MODE", body)
        self.assertEqual(len(core.parse_steps(body.strip().split("\n"))), 1)

    def test_header_ends_at_first_step(self):
        opts, body = core.parse_instruction_options("MODE: once\nWAIT: 1\nCLEAR_DATA: first\n")
        self.assertEqual(opts["clear_data"], "everytime")
        self.assertIn("CLEAR_DATA: first", body)

    def test_unknown_value_falls_back_to_everytime(self):
        opts, _ = core.parse_instruction_options("CLEAR_DATA: sometimes\n")
        self.assertEqual(opts["clear_data"], "everytime")

    def test_reinstall_app_header(self):
        opts, body = core.parse_instruction_options("REINSTALL_APP: true\nWAIT: 1\n")
        self.assertTrue(opts["reinstall_app"])
        self.assertNotIn("REINSTALL_APP", body)
        self.assertEqual(len(core.parse_steps(body.strip().split("\n"))), 1)
        opts, _ = core.parse_instruction_options("REINSTALL_APP: yes\n")
        self.assertTrue(opts["reinstall_app"])
        opts, _ = core.parse_instruction_options("REINSTALL_APP: false\n")
        self.assertFalse(opts["reinstall_app"])

    def test_parse_instruction_header_still_returns_mode(self):
        mode, body = core.parse_instruction_header("MODE: once\nCLEAR_DATA: first\n")
        self.assertEqual(mode, "once")
        self.assertEqual(body.strip(), "")


class ShouldClearTests(unittest.TestCase):
    def setUp(self):
        core._cleared_test_dirs.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_first_clears_only_the_first_test(self):
        _, tests = make_case(self.tmp.name, "MODE: once\nCLEAR_DATA: first\n")
        self.assertEqual([core.should_clear_app_data(t) for t in tests], [True, False, False])

    def test_everytime_and_missing_key_always_clear(self):
        _, tests = make_case(self.tmp.name, "MODE: once\n")
        self.assertEqual([core.should_clear_app_data(t) for t in tests], [True, True, True])

    def test_each_device_output_gets_its_own_first_clear(self):
        _, tests = make_case(self.tmp.name, "CLEAR_DATA: first\n")
        with patch.object(core, "PROJECT_ROOT", Path(self.tmp.name) / "devices" / "device_1"):
            self.assertEqual([core.should_clear_app_data(t) for t in tests], [True, False, False])
        with patch.object(core, "PROJECT_ROOT", Path(self.tmp.name) / "devices" / "device_2"):
            self.assertEqual([core.should_clear_app_data(t) for t in tests], [True, False, False])


class RunSingleTestRestartTests(unittest.TestCase):
    """run_single_test passes the decision on to the controller."""

    def setUp(self):
        core._cleared_test_dirs.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old_root = core.PROJECT_ROOT
        core._set_project_root(Path(self.tmp.name) / "out")
        self.addCleanup(core._set_project_root, old_root)
        p = patch.object(core, "_app_under_test", "com.example")
        p.start()
        self.addCleanup(p.stop)

    def test_restart_keeps_data_after_first_test(self):
        _, tests = make_case(self.tmp.name, "MODE: once\nCLEAR_DATA: first\n")
        calls = []
        with patch.object(core.controller, "restart_app",
                          side_effect=lambda pkg, clear_data=True: calls.append(clear_data) or True), \
             patch.object(core, "execute_step", return_value=True):
            for t in tests:
                core.run_single_test(t, verbose=False, record_video=False)
        self.assertEqual(calls, [True, False, False])


if __name__ == "__main__":
    unittest.main()
