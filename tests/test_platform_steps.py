"""Platform-only steps: `IF IOS: <step>` / `IF ANDROID: <step>` run on that platform
and are SKIPPED (not failed) on the other one."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mt-tests" / "scripts"))
with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core


class ParseTests(unittest.TestCase):
    def test_prefix_sets_platform_and_parses_the_rest(self):
        steps = core.parse_steps(["IF IOS: CLICK: Allow button", "if android: WAIT: 2", "WAIT: 1"])
        self.assertEqual([s["command"] for s in steps], ["CLICK", "WAIT", "WAIT"])
        self.assertEqual(steps[0]["args"], "Allow button")
        self.assertEqual(steps[0]["platform"], "ios")
        self.assertEqual(steps[1]["platform"], "android")
        self.assertNotIn("platform", steps[2])
        self.assertEqual(steps[0]["raw"], "IF IOS: CLICK: Allow button")

    def test_prefixed_plugin_step_keeps_its_block(self):
        steps = core.parse_steps([
            "IF IOS: PLUGIN: esp32 expect",
            "    GET /x -> 200",
            "END",
            "WAIT: 1",
        ])
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["command"], "PLUGIN")
        self.assertEqual(steps[0]["platform"], "ios")
        self.assertEqual(steps[0]["block"], ["GET /x -> 200"])

    def test_without_if_there_is_no_platform(self):
        # `IOS:` alone is not the platform form; it parses as an (unknown) command.
        steps = core.parse_steps(["IOS: CLICK: Allow button"])
        self.assertEqual(steps[0]["command"], "IOS")
        self.assertNotIn("platform", steps[0])


class BlockParseTests(unittest.TestCase):
    def test_block_gives_every_step_inside_its_platform(self):
        steps = core.parse_steps([
            "WAIT: 1",
            "IF IOS:",
            " SNAPSHOT: prompt",
            " # a comment",
            " CLICK: Allow button",
            "END IF IOS",
            "IF ANDROID:",
            "  WAIT: 2",
            "end if",
            "WAIT: 3",
        ])
        self.assertEqual([(s["command"], s.get("platform")) for s in steps], [
            ("WAIT", None), ("SNAPSHOT", "ios"), ("CLICK", "ios"),
            ("WAIT", "android"), ("WAIT", None),
        ])
        self.assertFalse(any("error" in s for s in steps))
        self.assertEqual(steps[2]["raw"], "CLICK: Allow button")
        self.assertEqual(steps[2]["line"], 5)

    def test_plugin_block_inside_if_block(self):
        steps = core.parse_steps([
            "IF ANDROID:",
            "  PLUGIN: esp32 expect",
            "      GET /x -> 200",
            "  END",
            "  WAIT: 1",
            "END IF",
        ])
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["block"], ["GET /x -> 200"])
        self.assertEqual([s["platform"] for s in steps], ["android", "android"])
        self.assertNotIn("error", steps[0])

    def test_same_platform_prefix_inside_block_is_fine(self):
        steps = core.parse_steps(["IF IOS:", "IF IOS: WAIT: 1", "END IF"])
        self.assertEqual(steps[0]["platform"], "ios")
        self.assertNotIn("error", steps[0])

    def assertOneError(self, lines, text):
        errors = [s for s in core.parse_steps(lines) if "error" in s]
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(text, errors[0]["error"])
        self.assertNotIn("platform", errors[0])  # fails on every device

    def test_unclosed_block(self):
        self.assertOneError(["IF IOS:", "WAIT: 1"], "has no END IF")

    def test_stray_end_if(self):
        self.assertOneError(["WAIT: 1", "END IF"], "does not close an IF block")

    def test_mismatched_end_if(self):
        self.assertOneError(["IF IOS:", "WAIT: 1", "END IF ANDROID"], "does not match")

    def test_nested_block(self):
        self.assertOneError(["IF IOS:", "IF ANDROID:", "WAIT: 1", "END IF"], "don't nest")

    def test_other_platform_prefix_inside_block(self):
        self.assertOneError(["IF IOS:", "IF ANDROID: WAIT: 1", "END IF"], "is for ANDROID")


class SkipDecisionTests(unittest.TestCase):
    def test_skip_only_on_the_other_platform(self):
        ios, android, both = core.parse_steps(["IF IOS: WAIT: 1", "IF ANDROID: WAIT: 1", "WAIT: 1"])
        with patch.dict(os.environ, {"DEVICE_PLATFORM": "android"}):
            self.assertEqual(core.step_skipped_on_platform(ios), "Only runs on iOS")
            self.assertIsNone(core.step_skipped_on_platform(android))
            self.assertIsNone(core.step_skipped_on_platform(both))
        with patch.dict(os.environ, {"DEVICE_PLATFORM": "ios"}):
            self.assertIsNone(core.step_skipped_on_platform(ios))
            self.assertEqual(core.step_skipped_on_platform(android), "Only runs on Android")


class RunSingleTestTests(unittest.TestCase):
    def setUp(self):
        core._cleared_test_dirs.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old_root = core.PROJECT_ROOT
        core._set_project_root(Path(self.tmp.name) / "out")
        self.addCleanup(core._set_project_root, old_root)
        for target, value in ((core, "_app_under_test"),):
            p = patch.object(target, value, "com.example")
            p.start()
            self.addCleanup(p.stop)
        case = Path(self.tmp.name) / "case"
        case.mkdir()
        (case / "INSTRUCTION.txt").write_text("MODE: once\n")
        self.test = case / "01_a.txt"
        self.test.write_text("WAIT: 0\nIF IOS: CLICK: Allow button\nIF ANDROID: WAIT: 0\nWAIT: 0\n")

    def run_on(self, platform, step_ok=True):
        executed = []
        with patch.dict(os.environ, {"DEVICE_PLATFORM": platform}), \
             patch.object(core.controller, "restart_app", return_value=True), \
             patch.object(core, "execute_step",
                          side_effect=lambda s: executed.append(s["raw"]) or step_ok):
            result = core.run_single_test(str(self.test), verbose=False, record_video=False)
        return result, executed

    def test_android_skips_ios_steps_and_passes(self):
        (success, results, *_), executed = self.run_on("android")
        self.assertTrue(success)
        self.assertEqual(executed, ["WAIT: 0", "IF ANDROID: WAIT: 0", "WAIT: 0"])
        statuses = [r["status"] for r in results]
        self.assertEqual(statuses, ["PASS", "SKIPPED", "PASS", "PASS"])
        self.assertEqual(results[1]["reason"], "Only runs on iOS")

    def test_ios_runs_ios_steps_and_skips_android_ones(self):
        (success, results, *_), executed = self.run_on("ios")
        self.assertTrue(success)
        self.assertEqual(executed, ["WAIT: 0", "IF IOS: CLICK: Allow button", "WAIT: 0"])
        self.assertEqual([r["status"] for r in results], ["PASS", "PASS", "SKIPPED", "PASS"])

    def test_block_form_runs_only_on_its_platform(self):
        self.test.write_text("WAIT: 0\nIF IOS:\n CLICK: Allow button\n WAIT: 2\nEND IF IOS\n"
                             "IF ANDROID:\n WAIT: 1\nEND IF\nWAIT: 0\n")
        (success, results, *_), executed = self.run_on("android")
        self.assertTrue(success)
        self.assertEqual(executed, ["WAIT: 0", "WAIT: 1", "WAIT: 0"])
        self.assertEqual([r["status"] for r in results], ["PASS", "SKIPPED", "SKIPPED", "PASS", "PASS"])
        (success, results, *_), executed = self.run_on("ios")
        self.assertTrue(success)
        self.assertEqual(executed, ["WAIT: 0", "CLICK: Allow button", "WAIT: 2", "WAIT: 0"])

    def test_unclosed_block_fails_the_test(self):
        self.test.write_text("WAIT: 0\nIF IOS:\n CLICK: Allow button\n")
        # The real execute_step fails a step that carries a parse error.
        with patch.dict(os.environ, {"DEVICE_PLATFORM": "android"}), \
             patch.object(core.controller, "restart_app", return_value=True), \
             patch.object(core, "execute_step",
                          side_effect=lambda s: "error" not in s):
            success, results, *_ = core.run_single_test(str(self.test), verbose=False,
                                                        record_video=False)
        self.assertFalse(success)
        self.assertIn("has no END IF", results[-1]["step"]["error"])

    def test_a_failing_platform_step_still_fails_the_test(self):
        (success, results, *_), _ = self.run_on("ios", step_ok=False)
        self.assertFalse(success)
        self.assertEqual(results[0]["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
