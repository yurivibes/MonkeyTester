"""Payload files a case's tests reference with `@file:<rel>` travel with the
upload: ci/visual-test/submit.py collects and sends them as `payloads` parts,
app.plan_test_upload validates and places them next to the case's tests, and
the runner anchors the refs to the staged files."""
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

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "visual-tests" / "scripts"))
sys.path.insert(0, str(ROOT / "ci" / "visual-test"))
with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core
import app
import submit
from test_app_api import ServerCase
from build_fixture import BUILD_ID, add_build

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def part(path, content=b"WAIT: 0\n"):
    """A parsed multipart file part, as app.parse_multipart_form returns it."""
    return {"filename": os.path.basename(path), "path": path, "content": content}


def multi_tests(*cases):
    return [p for c in cases for p in (part(f"{c}/INSTRUCTION.txt"), part(f"{c}/t.txt"))]


class PlanPayloadTests(unittest.TestCase):
    def test_no_payloads_keeps_the_old_two_argument_call(self):
        layout, err = app.plan_test_upload([part("INSTRUCTION.txt"), part("a.txt")], [])
        self.assertIsNone(err)
        self.assertEqual([p for p, _ in layout["files"]], ["INSTRUCTION.txt", "a.txt"])

    def test_single_case_payloads_are_staged_at_their_relative_path(self):
        layout, err = app.plan_test_upload(
            [part("INSTRUCTION.txt"), part("a.txt")], [],
            [part("payloads/a.json", b"{}"), part("b.bin", b"x")])
        self.assertIsNone(err)
        self.assertIn(("payloads/a.json", b"{}"), layout["files"])
        self.assertIn(("b.bin", b"x"), layout["files"])

    def test_multi_case_payloads_go_under_their_case(self):
        layout, err = app.plan_test_upload(
            multi_tests("a", "b"), [], [part("a/payloads/x.json", b"{}"), part("b/x.json", b"[]")])
        self.assertIsNone(err)
        self.assertIn(("a/payloads/x.json", b"{}"), layout["files"])
        self.assertIn(("b/x.json", b"[]"), layout["files"])

    def test_multi_case_payload_needs_an_uploaded_case(self):
        for path in ("c/x.json", "x.json", "/a/x.json"):
            _, err = app.plan_test_upload(multi_tests("a"), [], [part(path)])
            self.assertIn("for an uploaded case", err, path)

    def test_bad_relative_paths_are_rejected(self):
        bad = ["/etc/passwd", "../x.json", "p/../x.json", "./x.json", "p//x.json", "p/",
               "p\\x.json", ".hidden", "p/.x", "sp ace.json", "p/x;y", "a.txt", ""]
        for path in bad:
            _, err = app.plan_test_upload([part("INSTRUCTION.txt"), part("t.txt")], [], [part(path)])
            self.assertIsNotNone(err, path)
            self.assertIn("Payload", err, path)
        for path in ("a/../x.json", "a/./x.json", "a/.x"):
            _, err = app.plan_test_upload(multi_tests("a"), [], [part(path)])
            self.assertIsNotNone(err, path)

    def test_txt_payload_below_the_case_root_is_fine(self):
        _, err = app.plan_test_upload(
            [part("INSTRUCTION.txt"), part("t.txt")], [], [part("payloads/body.txt")])
        self.assertIsNone(err)

    def test_size_limit(self):
        tests = [part("INSTRUCTION.txt"), part("t.txt")]
        _, err = app.plan_test_upload(tests, [], [part("a.json", b"x" * 4096)])
        self.assertIsNone(err)
        _, err = app.plan_test_upload(tests, [], [part("a.json", b"x" * 4097)])
        self.assertIn("larger than 4096 bytes", err)
        _, err = app.plan_test_upload(tests, [], [part("a.json", b"")])
        self.assertIn("is empty", err)

    def test_count_limit_is_per_case(self):
        many = [part(f"a/p{i}.json") for i in range(32)] + [part(f"b/p{i}.json") for i in range(32)]
        _, err = app.plan_test_upload(multi_tests("a", "b"), [], many)
        self.assertIsNone(err)
        _, err = app.plan_test_upload(multi_tests("a", "b"), [], many + [part("a/p32.json")])
        self.assertIn("At most 32 payload files per case (case a)", err)
        _, err = app.plan_test_upload(
            [part("INSTRUCTION.txt"), part("t.txt")], [], [part(f"p{i}.json") for i in range(33)])
        self.assertIn("At most 32", err)

    def test_collisions_are_rejected(self):
        snap = "a/snapshots/ios/375x667@2x/home.png"
        cases = [
            ("a/t.txt", "collides"),             # an uploaded test (also .txt at root)
            (snap, "collides"),                   # an uploaded snapshot
            ("a/snapshots", "collides"),          # a file where a snapshot dir goes
            ("a/t.txt/x.json", "collides"),       # under an uploaded test file
            ("a/x.json", None),
        ]
        for path, expected in cases:
            _, err = app.plan_test_upload(multi_tests("a"), [part(snap, PNG)], [part(path)])
            if expected is None:
                self.assertIsNone(err, path)
            else:
                self.assertIsNotNone(err, path)
        _, err = app.plan_test_upload(multi_tests("a"), [], [part("a/x.json"), part("a/x.json")])
        self.assertIn("collides", err)
        _, err = app.plan_test_upload(multi_tests("a"), [], [part("a/p"), part("a/p/x.json")])
        self.assertIn("collides", err)
        _, err = app.plan_test_upload(multi_tests("a"), [], [part("a/p/x.json"), part("a/p")])
        self.assertIn("collides", err)


class CollectPayloadTests(unittest.TestCase):
    def make_case(self, root, tests, files=None):
        case = Path(root) / "case_a"
        case.mkdir(parents=True)
        (case / "INSTRUCTION.txt").write_text("MODE: once\n")
        for name, text in tests.items():
            (case / name).write_text(text)
        for rel, content in (files or {}).items():
            (case / rel).parent.mkdir(parents=True, exist_ok=True)
            (case / rel).write_bytes(content)
        return case

    def test_collects_each_referenced_file_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = self.make_case(tmp, {
                "01.txt": "PLUGIN: esp32 expect\n  GET /a -> 200 @file:payloads/a.json\n"
                          "  GET /b -> 200 @file:payloads/./a.json\nEND\n",
                "02.txt": 'PLUGIN: esp32 expect script="GET /c -> 200 @file:c.bin"\n'
                          "PLUGIN: esp32 expect\n  GET /x -> 200 @file:02.txt\nEND\n",
            }, {"payloads/a.json": b'{"a":1}', "c.bin": b"plain", "unused.json": b"{}"})
            payloads, err = submit.collect_payloads(case)
        self.assertIsNone(err)
        # 02.txt is a test file, already uploaded as a `tests` part.
        self.assertEqual(payloads, [("c.bin", b"plain"), ("payloads/a.json", b'{"a":1}')])

    def assertCollectError(self, tests, files, *texts, setup=None):
        with tempfile.TemporaryDirectory() as tmp:
            case = self.make_case(Path(tmp) / "cases", tests, files)
            if setup:
                setup(case, Path(tmp))
            payloads, err = submit.collect_payloads(case)
        self.assertIsNone(payloads)
        for text in texts:
            self.assertIn(text, err)

    def test_absolute_ref_fails(self):
        self.assertCollectError({"t.txt": "WAIT: 1\nX @file:/etc/hostname\n"}, {},
                                "case case_a", "t.txt line 2", "@file:/etc/hostname",
                                "must be a relative path")

    def test_escaping_ref_fails(self):
        self.assertCollectError({"t.txt": "X @file:../other/a.json\n"}, {},
                                "t.txt line 1", "resolves outside the case directory")

    def test_symlink_escaping_fails(self):
        def link(case, tmp):
            (tmp / "secret.json").write_text("{}")
            os.symlink(tmp / "secret.json", case / "a.json")
        self.assertCollectError({"t.txt": "X @file:a.json\n"}, {},
                                "resolves outside the case directory", setup=link)

    def test_missing_ref_fails(self):
        self.assertCollectError({"t.txt": "X\nY\nZ @file:payloads/nope.json\n"}, {},
                                "t.txt line 3", "@file:payloads/nope.json", "doesn't exist")

    def test_directory_ref_fails(self):
        self.assertCollectError({"t.txt": "X @file:payloads\n"}, {"payloads/a.json": b"{}"},
                                "is not a regular file")

    def test_empty_file_fails(self):
        self.assertCollectError({"t.txt": "X @file:e.json\n"}, {"e.json": b""}, "is empty")

    def test_too_large_fails(self):
        self.assertCollectError({"t.txt": "X @file:big.json\n"}, {"big.json": b"x" * 4097},
                                "is 4097 bytes (max 4096)")

    def test_unsafe_name_fails(self):
        self.assertCollectError({"t.txt": "X @file:.hidden.json\n"}, {".hidden.json": b"{}"},
                                "path segment '.hidden.json'")

    def test_more_than_32_files_fails(self):
        files = {f"p{i}.json": b"{}" for i in range(33)}
        text = "".join(f"X @file:p{i}.json\n" for i in range(33))
        self.assertCollectError({"t.txt": text}, files, "more than 32 payload files")

    def test_bad_ref_fails_before_uploading(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app.ipa").write_bytes(b"ipa")
            self.make_case(Path(tmp) / "cases", {"t.txt": "X @file:/abs.json\n"})
            args = submit.argparse.Namespace(
                url="http://viewer", app=str(Path(tmp) / "app.ipa"), platform="ios",
                tests_dir=str(Path(tmp) / "cases"), branch="b", timeout=5, poll=0)
            out = io.StringIO()
            with patch.object(submit, "request_json") as rq, redirect_stdout(out):
                self.assertEqual(submit.run(args), 1)
            rq.assert_not_called()
            self.assertIn("::error::bad @file: payload reference: case case_a: t.txt line 1", out.getvalue())

    def test_upload_carries_payload_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app.ipa").write_bytes(b"ipa")
            self.make_case(Path(tmp) / "cases", {"t.txt": "X @file:payloads/a.json\n"},
                           {"payloads/a.json": b'{"k":"v"}'})
            args = submit.argparse.Namespace(
                url="http://viewer", app=str(Path(tmp) / "app.ipa"), platform="ios",
                tests_dir=str(Path(tmp) / "cases"), branch="b", timeout=5, poll=0)
            started, passed = (200, {"run_id": "run_1"}), (200, {"status": "passed"})
            with patch.object(submit, "request_json", side_effect=[started, passed]) as rq, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(submit.run(args), 0)
            body = rq.call_args_list[0].args[1]
        self.assertIn(b'name="payloads"; filename="case_a/payloads/a.json"', body)
        self.assertEqual(body.count(b'filename="case_a/payloads/a.json"'), 1)


class EndToEndTests(unittest.TestCase):
    """submit's collection -> multipart parts -> plan_test_upload -> staged
    files -> parse_test_file: the anchored @file: path exists with the bytes."""

    def staged_steps(self, case_names, single=False):
        payload = b'{"temperature": 21.5}\n'
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            for name in case_names:
                case = src / name
                (case / "payloads").mkdir(parents=True)
                (case / "INSTRUCTION.txt").write_text("MODE: once\n")
                (case / "01_payload.txt").write_text(
                    "PLUGIN: esp32 expect\n  GET /temperature.json -> 200 @file:payloads/a.json\nEND\n")
                (case / "payloads" / "a.json").write_bytes(payload)
            case_dirs = sorted(src.iterdir())
            by_case, err = submit.collect_case_payloads(case_dirs)
            self.assertIsNone(err)
            parts = submit.case_parts(case_dirs, by_case)
            if single:  # the flat single-case form: filenames without the case prefix
                parts = [(f, n.split("/", 1)[1], c) for f, n, c in parts]
            # What app.parse_multipart_form makes of the parts.
            grouped = {}
            for field, filename, content in parts:
                grouped.setdefault(field, []).append(part(filename, content))
            self.assertEqual(len(grouped["payloads"]), len(case_names))
            layout, err = app.plan_test_upload(grouped["tests"], grouped.get("snapshots", []),
                                               grouped["payloads"])
            self.assertIsNone(err)
            tests_dir = Path(tmp) / "Run" / ".uploads" / "x" / "tests"
            for rel, content in layout["files"]:  # as /api/run-test stages them
                dest = tests_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
            results = []
            for name in case_names:
                case_dir = tests_dir if single else tests_dir / name
                with redirect_stdout(io.StringIO()):
                    steps = core.parse_test_file(str(case_dir / "01_payload.txt"))
                self.assertEqual(len(steps), 1)
                self.assertNotIn("error", steps[0])
                ref = steps[0]["block"][0].split("@file:", 1)[1]
                self.assertTrue(os.path.isabs(ref))
                self.assertEqual(ref, os.path.normpath(os.path.abspath(case_dir / "payloads" / "a.json")))
                self.assertTrue(os.path.isfile(ref))
                results.append(Path(ref).read_bytes())
        self.assertEqual(results, [payload] * len(case_names))

    def test_multi_case_upload(self):
        self.staged_steps(["case_a", "case_b"])

    def test_single_case_upload(self):
        self.staged_steps(["case_a"], single=True)


class RunTestEndpointTests(ServerCase):
    """/api/run-test reads the `payloads` parts and stages them with the tests."""

    def setUp(self):
        super().setUp()
        script = self.root / "fake_runner.py"
        # Record the staged tests dir (layout + payload bytes) for the test.
        script.write_text(
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            "out = argv[argv.index('--output-dir') + 1]\n"
            "tests = argv[argv.index('--test-dir') + 1]\n"
            "n = 1 + max([int(d[4:]) for d in os.listdir(out) if d.startswith('run_') and d[4:].isdigit()] or [0])\n"
            "run = os.path.join(out, f'run_{n}')\n"
            "os.makedirs(run)\n"
            "files = {os.path.relpath(os.path.join(r, f), tests): open(os.path.join(r, f), 'rb').read().decode()\n"
            "         for r, _, fs in os.walk(tests) for f in fs}\n"
            "json.dump({'files': files}, open(os.path.join(run, 'fake.json'), 'w'))\n")
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, parts):
        add_build()
        ctype, body = submit.build_multipart({"device_mode": "android", "build_request": BUILD_ID},
                                             [("apk", "app.apk", b"PK")] + parts)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_payloads_are_staged_next_to_the_tests(self):
        status, payload = self.post([
            ("tests", "a/INSTRUCTION.txt", b"MODE: once\n"),
            ("tests", "a/t.txt", b"WAIT: 0\n"),
            ("payloads", "a/payloads/x.json", b'{"x":1}')])
        self.assertEqual(status, 200, payload)
        fake = self.runs / payload["run_id"] / "fake.json"
        deadline = time.time() + 10
        while time.time() < deadline and not fake.exists():
            time.sleep(0.1)
        files = json.loads(fake.read_text())["files"]
        self.assertEqual(files.get("a/payloads/x.json"), '{"x":1}')

    def test_bad_payload_is_a_400(self):
        status, payload = self.post([
            ("tests", "a/INSTRUCTION.txt", b"MODE: once\n"),
            ("tests", "a/t.txt", b"WAIT: 0\n"),
            ("payloads", "a/../x.json", b"{}")])
        self.assertEqual(status, 400)
        self.assertIn("Payload 'a/../x.json'", payload["error"])


if __name__ == "__main__":
    unittest.main()
