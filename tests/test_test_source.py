"""Show test file: the runner keeps each test's .txt (plus its case's
INSTRUCTION.txt) in the run, and /api/test returns it as ``test_source``.
Runs from before this have no copy and get no test_source."""
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mt-tests" / "scripts"))
with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core
import app
from tests.test_app_api import ServerCase


def write_tests(root, files):
    for rel, text in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class RunnerSavesSourcesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src = Path(tmp.name) / "src"
        self.run_dir = Path(tmp.name) / "run_1"
        old = core.RUN_ROOT
        core.RUN_ROOT = self.run_dir
        self.addCleanup(setattr, core, "RUN_ROOT", old)
        self.addCleanup(setattr, core, "_multi_case_root", None)

    def resolve(self, test_dir):
        args = argparse.Namespace(test_files=None, test_file=None, test_dir=str(test_dir))
        with redirect_stdout(io.StringIO()):
            return core.resolve_test_files(args)

    def test_multi_case_copies_into_case_folders(self):
        write_tests(self.src, {
            "eula/INSTRUCTION.txt": "MODE: once\n",
            "eula/01_a.txt": "# comment\n\nVERIFY: EULA shown\n",
            "home/INSTRUCTION.txt": "MODE: everytime\n",
            "home/01_a.txt": "CLICK: Home\n",
        })
        core.save_test_sources(self.resolve(self.src))
        t = self.run_dir / "tests"
        self.assertEqual((t / "eula" / "01_a.txt").read_text(), "# comment\n\nVERIFY: EULA shown\n")
        self.assertEqual((t / "eula" / "INSTRUCTION.txt").read_text(), "MODE: once\n")
        self.assertEqual((t / "home" / "01_a.txt").read_text(), "CLICK: Home\n")
        self.assertEqual((t / "home" / "INSTRUCTION.txt").read_text(), "MODE: everytime\n")

    def test_single_case_copies_flat(self):
        write_tests(self.src, {"INSTRUCTION.txt": "MODE: once\n", "a.txt": "WAIT: 1\n"})
        core.save_test_sources(self.resolve(self.src))
        self.assertEqual(sorted(p.name for p in (self.run_dir / "tests").iterdir()),
                         ["INSTRUCTION.txt", "a.txt"])

    def test_copy_failure_does_not_raise(self):
        with redirect_stdout(io.StringIO()) as out:
            core.save_test_sources([str(self.src / "missing.txt")])
        self.assertIn("could not save test source", out.getvalue())


class ApiTestSourceTests(ServerCase):
    def result(self, rel, **fields):
        p = self.runs / "run_1" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"status": "PASS", "steps": [], **fields}))

    def test_multi_case_device_result_returns_its_source(self):
        self.result("devices/device_1/results/eula__01_a_result.json", case="eula", test="eula__01_a")
        write_tests(self.runs / "run_1", {
            "tests/eula/INSTRUCTION.txt": "MODE: once\n",
            "tests/eula/01_a.txt": "VERIFY: <b>EULA</b>\n",
        })
        data = self.get_json("/api/test/run_1/eula__01_a?device=device_1")
        self.assertEqual(data["test_source"], {
            "test_file": "01_a.txt", "test": "VERIFY: <b>EULA</b>\n", "instruction": "MODE: once\n"})
        self.assertNotIn("full_json", data)

    def test_single_case_without_instruction(self):
        self.result("results/a_result.json", test="a")
        write_tests(self.runs / "run_1", {"tests/a.txt": "WAIT: 1\n"})
        src = self.get_json("/api/test/run_1/a")["test_source"]
        self.assertEqual(src, {"test_file": "a.txt", "test": "WAIT: 1\n", "instruction": None})

    def test_old_run_without_copy_has_no_source(self):
        self.result("results/a_result.json", test="a")
        data = self.get_json("/api/test/run_1/a")
        self.assertIsNone(data["test_source"])
        self.assertNotIn("full_json", data)

    def test_bad_case_cannot_escape_tests_dir(self):
        write_tests(self.runs / "run_1", {"secret.txt": "x"})
        self.assertIsNone(app.get_test_source(self.runs / "run_1", "..__secret", ".."))
        self.assertIsNone(app.get_test_source(self.runs / "run_1", "INSTRUCTION"))


if __name__ == "__main__":
    unittest.main()
