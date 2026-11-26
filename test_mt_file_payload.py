"""`@file:` refs in PLUGIN steps: the runner anchors them to the test file's
directory at parse time (relative -> absolute); the plugin reads the files on
the plugin host, so the firmware never sees `@file:`."""
import os
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).parent / "mt-tests" / "scripts"))
with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core


class BlockFileRefTests(unittest.TestCase):
    def test_relative_block_ref_rewritten_to_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps = core.parse_steps(
                ["PLUGIN: esp32 expect", "  GET /x -> 200 @file:payloads/a.json", "END"],
                test_dir=tmp)
        self.assertEqual(steps[0]["block"], ["GET /x -> 200 @file:" + tmp + "/payloads/a.json"])
        self.assertNotIn("error", steps[0])

    def test_relative_ref_with_inner_dots_stays_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps = core.parse_steps(
                ["PLUGIN: esp32 expect", "  GET /x -> 200 @file:sub/../a.json", "END"],
                test_dir=tmp)
        self.assertEqual(steps[0]["block"], ["GET /x -> 200 @file:" + tmp + "/a.json"])
        self.assertNotIn("error", steps[0])

    def test_lines_without_refs_stay_byte_identical(self):
        block_in = ["GET /temperature.json -> 200 {\"ok\":true}", "# note", "POST /setup -> 500"]
        with tempfile.TemporaryDirectory() as tmp:
            steps = core.parse_steps(["PLUGIN: esp32 expect"] + ["  " + b for b in block_in] + ["END"],
                                     test_dir=tmp)
        self.assertEqual(steps[0]["block"], block_in)
        self.assertNotIn("error", steps[0])

    def test_only_ref_tokens_are_rewritten_in_a_mixed_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps = core.parse_steps(
                ["PLUGIN: esp32 expect",
                 "  GET /a -> 200 @file:a.json",
                 "  GET /b -> 200",
                 "  POST /c @file:sub/c.json @ref:d1",
                 "END"],
                test_dir=tmp)
        self.assertEqual(steps[0]["block"], [
            "GET /a -> 200 @file:" + tmp + "/a.json",
            "GET /b -> 200",
            "POST /c @file:" + tmp + "/sub/c.json @ref:d1",
        ])
        self.assertNotIn("error", steps[0])

    def test_without_test_dir_refs_stay_verbatim(self):
        steps = core.parse_steps(["PLUGIN: esp32 expect", "  GET /x -> 200 @file:a.json", "END"])
        self.assertEqual(steps[0]["block"], ["GET /x -> 200 @file:a.json"])


class ScriptParamFileRefTests(unittest.TestCase):
    def test_script_param_ref_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps = core.parse_steps(
                ['PLUGIN: esp32 expect pattern="x" script="GET /x -> 200 @file:a.json"'],
                test_dir=tmp)
        plugin, action, params, device, timeout = core.parse_plugin_args(steps[0]["args"])
        self.assertEqual((plugin, action), ("esp32", "expect"))
        self.assertEqual(params["script"], "GET /x -> 200 @file:" + tmp + "/a.json")
        self.assertEqual(params["pattern"], "x")
        self.assertNotIn("error", steps[0])

    def test_ref_outside_script_param_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps = core.parse_steps(["PLUGIN: esp32 flash firmware_dir=@file:rel"], test_dir=tmp)
        self.assertEqual(steps[0]["args"], "esp32 flash firmware_dir=@file:rel")
        self.assertNotIn("error", steps[0])

    def test_single_line_without_refs_stays_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps = core.parse_steps(['PLUGIN: esp32 expect script="GET /x -> 200"'], test_dir=tmp)
        self.assertEqual(steps[0]["args"], 'esp32 expect script="GET /x -> 200"')


class FileRefParseErrorTests(unittest.TestCase):
    def assertOneFileRefError(self, lines, tmp, text, line_num):
        errors = [s for s in core.parse_steps(lines, test_dir=tmp) if "error" in s]
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(text, errors[0]["error"])
        self.assertIn(f"at line {line_num}", errors[0]["error"])

    def test_empty_ref_is_a_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertOneFileRefError(
                ["WAIT: 1", "PLUGIN: esp32 expect", "  GET /x -> 200 @file:", "END"],
                tmp, "has no path after it", 3)

    def test_ref_escaping_the_test_dir_is_a_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertOneFileRefError(
                ["PLUGIN: esp32 expect", "  GET /x -> 200 @file:../../secret.json", "END"],
                tmp, "resolves outside the test file's directory", 2)

    def test_absolute_ref_is_a_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertOneFileRefError(
                ["WAIT: 1", "PLUGIN: esp32 expect", "  GET /x -> 200 @file:/elsewhere/a.json", "END"],
                tmp, "must be relative to the test file's directory", 3)
            # Even an absolute path that points inside the test dir.
            self.assertOneFileRefError(
                ["PLUGIN: esp32 expect", f"  GET /x -> 200 @file:{tmp}/a.json", "END"],
                tmp, "is an absolute path", 2)

    def test_absolute_script_ref_is_a_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertOneFileRefError(
                ['PLUGIN: esp32 expect script="GET /x -> 200 @file:/etc/passwd"'],
                tmp, "is an absolute path", 1)

    def test_absolute_ref_leaves_the_block_line_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            steps = core.parse_steps(
                ["PLUGIN: esp32 expect", "  GET /a -> 200 @file:a.json @file:/x.json", "END"],
                test_dir=tmp)
        self.assertEqual(steps[0]["block"], ["GET /a -> 200 @file:a.json @file:/x.json"])
        self.assertIn("error", steps[0])

    def test_symlink_escaping_the_test_dir_is_a_parse_error(self):
        with tempfile.TemporaryDirectory() as outside, tempfile.TemporaryDirectory() as tmp:
            Path(outside, "secret.json").write_text("{}")
            os.symlink(outside, os.path.join(tmp, "payloads"))
            os.symlink(os.path.join(outside, "secret.json"), os.path.join(tmp, "a.json"))
            self.assertOneFileRefError(
                ["PLUGIN: esp32 expect", "  GET /x -> 200 @file:payloads/secret.json", "END"],
                tmp, "resolves outside the test file's directory", 2)
            self.assertOneFileRefError(
                ["PLUGIN: esp32 expect", "  GET /x -> 200 @file:a.json", "END"],
                tmp, "resolves outside the test file's directory", 2)

    def test_symlink_inside_the_test_dir_is_allowed_and_path_stays_normpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "real.json").write_text("{}")
            os.symlink(os.path.join(tmp, "real.json"), os.path.join(tmp, "a.json"))
            steps = core.parse_steps(
                ["PLUGIN: esp32 expect", "  GET /x -> 200 @file:a.json", "END"], test_dir=tmp)
        self.assertEqual(steps[0]["block"], ["GET /x -> 200 @file:" + tmp + "/a.json"])
        self.assertNotIn("error", steps[0])

    def test_script_param_empty_ref_is_a_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertOneFileRefError(
                ['PLUGIN: esp32 expect script="GET /x -> 200 @file:"'],
                tmp, "has no path after it", 1)

    def test_escaping_script_ref_is_a_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertOneFileRefError(
                ["WAIT: 1", 'PLUGIN: esp32 expect script="@file:../x.json"'],
                tmp, "resolves outside the test file's directory", 2)


class ParseTestFileEndToEndTests(unittest.TestCase):
    def test_parse_test_file_resolves_against_its_own_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "case"
            case.mkdir()
            test = case / "01_payload.txt"
            test.write_text("PLUGIN: esp32 expect\n"
                            "  GET /x -> 200 @file:payloads/a.json\n"
                            "END\n")
            with redirect_stdout(io.StringIO()):
                steps = core.parse_test_file(str(test))
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["block"], ["GET /x -> 200 @file:" + str(case) + "/payloads/a.json"])
        self.assertNotIn("error", steps[0])


if __name__ == "__main__":
    unittest.main()
