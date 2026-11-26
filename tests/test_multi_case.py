"""One run per CI build: every case folder in one upload / one run.

Covers the upload layout check (app.plan_test_upload), staging through
/api/run-test, the runner's multi-case discovery, names, labels and per-case
outputs, and the "a failure cancels the rest of that device" rule.
"""
import argparse
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mt-tests" / "scripts"))
with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core
import app
from tests.build_fixture import BUILD_ID, add_build
from tests.test_app_api import ServerCase

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def part(path, content=b"WAIT: 0\n"):
    return {"filename": os.path.basename(path), "path": path, "content": content}


class PlanUploadTests(unittest.TestCase):
    def test_single_case_layout_is_unchanged(self):
        layout, err = app.plan_test_upload(
            [part("INSTRUCTION.txt"), part("a.txt")],
            [part("snapshots/ios/375x667@2x/home.png", PNG)])
        self.assertIsNone(err)
        self.assertEqual(layout["cases"], [])
        self.assertEqual([p for p, _ in layout["files"]],
                         ["INSTRUCTION.txt", "a.txt", "snapshots/ios/375x667@2x/home.png"])

    def test_multi_case_layout(self):
        layout, err = app.plan_test_upload(
            [part("eula/INSTRUCTION.txt"), part("eula/t.txt"),
             part("provisioning/INSTRUCTION.txt"), part("provisioning/01_a.txt"),
             part("provisioning/02_b.txt")],
            [part("provisioning/snapshots/android/443x980@2.438x/sheet.png", PNG)])
        self.assertIsNone(err)
        self.assertEqual(layout["cases"], ["eula", "provisioning"])
        self.assertIn("provisioning/snapshots/android/443x980@2.438x/sheet.png",
                      [p for p, _ in layout["files"]])

    def test_setup_order_file_stages_at_the_root(self):
        layout, err = app.plan_test_upload(
            [part("SETUP.txt", b"prov\neula\n"),
             part("eula/INSTRUCTION.txt"), part("eula/t.txt"),
             part("prov/INSTRUCTION.txt"), part("prov/01_a.txt")],
            [])
        self.assertIsNone(err)
        self.assertEqual(layout["cases"], ["eula", "prov"])
        self.assertEqual(layout["files"][0], ("SETUP.txt", b"prov\neula\n"))

    def test_each_case_needs_its_own_instruction_and_a_test(self):
        _, err = app.plan_test_upload([part("a/INSTRUCTION.txt"), part("a/t.txt"), part("b/t.txt")], [])
        self.assertIn("Case b", err)
        _, err = app.plan_test_upload([part("a/INSTRUCTION.txt")], [])
        self.assertIn("Case a", err)

    def test_bad_paths_are_rejected(self):
        for bad in ("../x/t.txt", "a/b/t.txt", ".hidden/t.txt"):
            _, err = app.plan_test_upload([part("a/INSTRUCTION.txt"), part(bad)], [])
            self.assertIsNotNone(err, bad)
        _, err = app.plan_test_upload(
            [part("a/INSTRUCTION.txt"), part("a/t.txt")],
            [part("other/snapshots/ios/375x667@2x/home.png", PNG)])
        self.assertIn("uploaded case", err)


class MultiCaseUploadTests(ServerCase):
    """/api/run-test stages every case folder and starts ONE run."""

    def setUp(self):
        super().setUp()
        script = self.root / "fake_runner.py"
        # Record the staged tests dir layout before the viewer cleans it up.
        script.write_text(
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            "out = argv[argv.index('--output-dir') + 1]\n"
            "tests = argv[argv.index('--test-dir') + 1]\n"
            "n = 1 + max([int(d[4:]) for d in os.listdir(out) if d.startswith('run_') and d[4:].isdigit()] or [0])\n"
            "run = os.path.join(out, f'run_{n}')\n"
            "os.makedirs(run)\n"
            "files = sorted(os.path.relpath(os.path.join(r, f), tests) for r, _, fs in os.walk(tests) for f in fs)\n"
            "json.dump({'argv': argv, 'files': files}, open(os.path.join(run, 'fake.json'), 'w'))\n")
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_cases_are_staged_in_their_folders(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "submit", Path(app.__file__).parent / "ci" / "mt-submit" / "submit.py")
        submit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(submit)
        add_build()
        ctype, body = submit.build_multipart(
            {"device_mode": "android", "build_request": BUILD_ID},
            [("apk", "app.apk", b"PK"),
             ("tests", "eula/INSTRUCTION.txt", b"MODE: once\n"),
             ("tests", "eula/t.txt", b"WAIT: 0\n"),
             ("tests", "prov/INSTRUCTION.txt", b"CLEAR_DATA: first\n"),
             ("tests", "prov/01_a.txt", b"WAIT: 0\n"),
             ("snapshots", "prov/snapshots/android/443x980@2.438x/s.png", PNG)])
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=30) as resp:
            run_id = json.loads(resp.read())["run_id"]
        fake = self.runs / run_id / "fake.json"
        deadline = time.time() + 10
        while time.time() < deadline and not fake.exists():
            time.sleep(0.1)
        info = json.loads(fake.read_text())
        self.assertEqual(info["files"], [
            "eula/INSTRUCTION.txt", "eula/t.txt",
            "prov/01_a.txt", "prov/INSTRUCTION.txt",
            "prov/snapshots/android/443x980@2.438x/s.png"])
        self.assertNotIn("--case", info["argv"])


def make_cases(root, cases):
    for case, tests in cases.items():
        d = Path(root) / case
        d.mkdir(parents=True)
        (d / "INSTRUCTION.txt").write_text("MODE: once\n")
        for t in tests:
            (d / t).write_text("WAIT: 0\n")


class RunnerDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, core, "_multi_case_root", None)
        self.addCleanup(setattr, core, "_current_test_dir", None)

    def resolve(self, test_dir):
        args = argparse.Namespace(test_files=None, test_file=None, test_dir=test_dir)
        with redirect_stdout(io.StringIO()):
            return core.resolve_test_files(args)

    def test_case_folders_run_in_name_order_with_unique_names(self):
        make_cases(self.tmp.name, {"provisioning": ["02_b.txt", "01_a.txt"], "eula": ["eula.txt"]})
        files = self.resolve(self.tmp.name)
        self.assertEqual([core.test_name_of(f) for f in files],
                         ["eula__eula", "provisioning__01_a", "provisioning__02_b"])
        self.assertEqual(core.test_label(files[1]), "provisioning / 01_a")
        self.assertEqual(core.test_case_of(files[0]), "eula")

    def test_setup_txt_fixes_the_case_order(self):
        make_cases(self.tmp.name, {"provisioning": ["01_a.txt"], "eula": ["e.txt"]})
        Path(self.tmp.name, "SETUP.txt").write_text("# suites, run order\nprovisioning\n\neula\n")
        files = self.resolve(self.tmp.name)
        self.assertEqual([core.test_name_of(f) for f in files],
                         ["provisioning__01_a", "eula__e"])

    def test_setup_txt_unlisted_cases_run_last_in_name_order(self):
        make_cases(self.tmp.name, {"a": ["t.txt"], "b": ["t.txt"], "c": ["t.txt"]})
        Path(self.tmp.name, "SETUP.txt").write_text("c\nghost\n")
        files = self.resolve(self.tmp.name)
        self.assertEqual([core.test_case_of(f) for f in files], ["c", "a", "b"])

    def test_single_case_dir_keeps_plain_names(self):
        make_cases(self.tmp.name, {"only": ["a.txt"]})
        files = self.resolve(str(Path(self.tmp.name) / "only"))
        self.assertEqual([core.test_name_of(f) for f in files], ["a"])
        self.assertIsNone(core.test_case_of(files[0]))

    def test_outputs_are_kept_apart_per_case(self):
        make_cases(self.tmp.name, {"a": ["t.txt"], "b": ["t.txt"]})
        files = self.resolve(self.tmp.name)
        old_root = core.PROJECT_ROOT
        core._set_project_root(Path(self.tmp.name) / "out")
        self.addCleanup(core._set_project_root, old_root)
        dirs = []
        for f in files:
            core._current_test_dir = Path(f).parent
            dirs.append(core._test_outputs_dir())
        self.assertEqual([d.name for d in dirs], ["a", "b"])


class CaseReinstallTests(unittest.TestCase):
    """Cases after the first keep the app the run installed; a case starts
    from a reinstalled app only when its INSTRUCTION.txt opts in with
    REINSTALL_APP: true — on both platforms."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, core, "_multi_case_root", None)
        self.cases = Path(self.tmp.name) / "cases"
        make_cases(self.cases, {"a": ["1.txt", "2.txt"], "b": ["1.txt"]})
        args = argparse.Namespace(test_files=None, test_file=None, test_dir=str(self.cases))
        with redirect_stdout(io.StringIO()):
            self.files = core.resolve_test_files(args)
        ipa = Path(self.tmp.name) / "app.ipa"
        ipa.write_bytes(b"ipa")
        for p in (patch.object(core, "_app_under_test", "com.example"),
                  patch.object(core, "_app_path_under_test", str(ipa)),
                  patch.object(core.time, "sleep"),
                  patch.dict(core._last_case_by_output, clear=True)):
            p.start()
            self.addCleanup(p.stop)

    def run_all(self, platform):
        with patch.object(core, "_device_platform", return_value=platform), \
                patch.object(core.controller, "uninstall_app") as uninstall, \
                patch.object(core.controller, "install_apk", return_value=True) as install, \
                redirect_stdout(io.StringIO()):
            results = [core.reinstall_for_new_case(f) for f in self.files]
        return results, uninstall.call_count, install.call_count

    def test_cases_keep_the_installed_app_by_default(self):
        self.assertEqual(self.run_all("ios"), ([True, True, True], 0, 0))
        self.assertEqual(self.run_all("android"), ([True, True, True], 0, 0))

    def test_reinstall_app_case_reinstalls_on_both_platforms(self):
        (self.cases / "b" / "INSTRUCTION.txt").write_text("MODE: once\nREINSTALL_APP: true\n")
        self.assertEqual(self.run_all("ios"), ([True, True, True], 1, 1))
        self.assertEqual(self.run_all("android"), ([True, True, True], 1, 1))


class CaseAppDataTests(unittest.TestCase):
    """Android data clearing at case boundaries: a case after the first keeps
    the app data the previous case left (like iOS) unless its INSTRUCTION.txt
    sets CLEAR_DATA or REINSTALL_APP: true. The first case keeps the old
    default (clear before every test)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, core, "_multi_case_root", None)
        self.cases = Path(self.tmp.name) / "cases"
        make_cases(self.cases, {"a": ["1.txt", "2.txt"], "b": ["1.txt", "2.txt"]})
        for p in (patch.dict(core._first_case_by_output, clear=True),
                  patch.object(core, "_cleared_test_dirs", set())):
            p.start()
            self.addCleanup(p.stop)

    def decisions(self, out="device_1"):
        args = argparse.Namespace(test_files=None, test_file=None, test_dir=str(self.cases))
        with redirect_stdout(io.StringIO()):
            files = core.resolve_test_files(args)
        with patch.object(core, "PROJECT_ROOT", Path(self.tmp.name) / out):
            return [core.should_clear_app_data(f) for f in files]

    def test_later_case_keeps_data_by_default(self):
        self.assertEqual(self.decisions(), [True, True, False, False])

    def test_each_device_tracks_its_own_first_case(self):
        self.assertEqual(self.decisions("device_1"), [True, True, False, False])
        self.assertEqual(self.decisions("device_2"), [True, True, False, False])

    def test_clear_data_header_on_later_case_still_clears(self):
        (self.cases / "b" / "INSTRUCTION.txt").write_text("CLEAR_DATA: everytime\n")
        self.assertEqual(self.decisions(), [True, True, True, True])
        core._first_case_by_output.clear()
        (self.cases / "b" / "INSTRUCTION.txt").write_text("CLEAR_DATA: first\n")
        self.assertEqual(self.decisions("device_3"), [True, True, True, False])

    def test_reinstall_app_case_does_not_keep_data(self):
        (self.cases / "b" / "INSTRUCTION.txt").write_text("REINSTALL_APP: true\n")
        self.assertEqual(self.decisions(), [True, True, True, True])

    def test_single_case_run_is_unchanged(self):
        case = Path(self.tmp.name) / "single"
        case.mkdir()
        (case / "INSTRUCTION.txt").write_text("MODE: once\n")
        for n in ("1.txt", "2.txt"):
            (case / n).write_text("WAIT: 0\n")
        core._multi_case_root = None
        tests = [str(case / "1.txt"), str(case / "2.txt")]
        self.assertEqual([core.should_clear_app_data(t) for t in tests], [True, True])


class CancelRestOfDeviceTests(unittest.TestCase):
    """A failure stops that device: the rest of its tests, in every case, are
    cancelled (not failed)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, core, "_multi_case_root", None)
        old_root = core.PROJECT_ROOT
        core._set_project_root(Path(self.tmp.name) / "out")
        self.addCleanup(core._set_project_root, old_root)

    def test_failure_cancels_the_remaining_tests(self):
        make_cases(Path(self.tmp.name) / "cases", {"a": ["1.txt", "2.txt"], "b": ["1.txt"]})
        args = argparse.Namespace(test_files=None, test_file=None, test_dir=str(Path(self.tmp.name) / "cases"),
                                  verbose=False, no_video=True, branch=None)
        with redirect_stdout(io.StringIO()):
            files = core.resolve_test_files(args)
            pending = core.PROJECT_ROOT / "pending_tests.json"
            with patch.object(core, "run_test", side_effect=[False]) as run_test, \
                    patch.object(core, "check_visual_config"), \
                    patch.object(core, "uninstall_apk_from_device"):
                passed, failed, ok = core._run_device_tests(
                    files, None, "com.example", None, args, pending, [], None)
        self.assertEqual(run_test.call_count, 1)
        self.assertEqual((passed, failed, ok), (0, 1, False))
        statuses = {t["name"]: t["status"] for t in json.loads(pending.read_text())["tests"]}
        self.assertEqual(statuses, {"a__1": "failed", "a__2": "cancelled", "b__1": "cancelled"})
        meta = json.loads((core.PROJECT_ROOT / "run_metadata.json").read_text())
        self.assertEqual((meta["failed"], meta["cancelled"]), (1, 2))
        labels = [t["label"] for t in json.loads(pending.read_text())["tests"]]
        self.assertEqual(labels, ["a / 1", "a / 2", "b / 1"])


if __name__ == "__main__":
    unittest.main()
