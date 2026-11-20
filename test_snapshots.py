"""SNAPSHOT and ASSERT_LAYOUT steps: image code, screen classes, record mode,
the runner steps, the upload of baselines and the baseline bundle."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import app
from build_fixture import BUILD_ID, add_build
from test_app_api import ServerCase

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "visual-tests" / "scripts"))
sys.path.insert(0, str(ROOT / "ci" / "visual-test"))
import layout
import snapshots
import submit
import test_runner_core as core


def solid(w, h, rgb=(200, 200, 200)):
    return bytes(rgb) * (w * h)


def png(w, h, rgb=(200, 200, 200), pixels=None):
    return snapshots.encode_png(w, h, pixels if pixels is not None else solid(w, h, rgb))


def paint(pixels, w, x0, y0, x1, y1, rgb=(0, 0, 0)):
    out = bytearray(pixels)
    for y in range(y0, y1):
        for x in range(x0, x1):
            out[(y * w + x) * 3:(y * w + x) * 3 + 3] = bytes(rgb)
    return bytes(out)


class PngTests(unittest.TestCase):
    def test_round_trip_and_rgba(self):
        pixels = paint(solid(7, 5), 7, 1, 1, 3, 4, (10, 20, 30))
        self.assertEqual(snapshots.decode_png(snapshots.encode_png(7, 5, pixels)), (7, 5, pixels))
        # An RGBA PNG with every filter type decodes to the same RGB.
        import struct, zlib
        rgba = bytearray()
        for i in range(0, len(pixels), 3):
            rgba += pixels[i:i + 3] + b"\xff"
        stride = 7 * 4
        raw = bytearray()
        prev = bytearray(stride)
        for y in range(5):
            line = rgba[y * stride:(y + 1) * stride]
            ftype = y % 5
            enc = bytearray(stride)
            for i in range(stride):
                a = line[i - 4] if i >= 4 else 0
                b = prev[i]
                c = prev[i - 4] if i >= 4 else 0
                if ftype == 0:
                    pred = 0
                elif ftype == 1:
                    pred = a
                elif ftype == 2:
                    pred = b
                elif ftype == 3:
                    pred = (a + b) >> 1
                else:
                    p = a + b - c
                    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                enc[i] = (line[i] - pred) & 0xFF
            raw += bytes([ftype]) + enc
            prev = line

        def chunk(t, body):
            return struct.pack(">I", len(body)) + t + body + struct.pack(">I", zlib.crc32(t + body))
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 7, 5, 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))
        self.assertEqual(snapshots.decode_png(data), (7, 5, pixels))

    def test_not_a_png(self):
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.decode_png(b"GIF89a")


class GeometryTests(unittest.TestCase):
    def test_screen_class_is_points_or_dp_plus_scale(self):
        self.assertEqual(snapshots.screen_class("ios", 750, 1334, 2.0), "ios/375x667@2x")
        self.assertEqual(snapshots.screen_class("ios", 1179, 2556, 3.0), "ios/393x852@3x")
        self.assertEqual(snapshots.screen_class("android", 1080, 2388, 2.625),
                         "android/411x910@2.625x")

    def test_downscale_averages_boxes(self):
        # 4x2 -> 2x1: left box black/white average, right box solid.
        px = bytes([0, 0, 0, 255, 255, 255, 50, 50, 50, 50, 50, 50,
                    0, 0, 0, 255, 255, 255, 50, 50, 50, 50, 50, 50])
        self.assertEqual(snapshots.downscale(4, 2, px, 2, 1), bytes([128] * 3 + [50] * 3))


class ArgsTests(unittest.TestCase):
    def test_masks_and_tolerance(self):
        name, masks, tol = snapshots.parse_args(
            "home mask=status_bar,tag:clock,rect:0,10,20.5,30 tolerance=0.5")
        self.assertEqual(name, "home")
        self.assertEqual(masks, [("status_bar",), ("tag", "clock"), ("rect", 0, 10, 20.5, 30)])
        self.assertEqual(tol, 0.5)

    def test_bad_arguments(self):
        for args in ("", "../x", "home mask=moon", "home tolerance=abc", "home colour=red",
                     "home mask=rect:1,2"):
            with self.assertRaises(snapshots.SnapshotError, msg=args):
                snapshots.parse_args(args)


class TakeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "snapshots"
        self.out = Path(tmp.name) / "out"
        # A 40x60 px phone at scale 2 -> 20x30 @2x.
        self.capture = png(40, 60)

    def take(self, capture=None, **kw):
        return snapshots.take("home", capture_png=capture or self.capture, platform="ios",
                              scale=2.0, snapshot_root=self.root, out_dir=self.out, **kw)

    def baseline(self, cls="ios/20x30@2x", pixels=None, w=20, h=30):
        path = self.root / cls / "home.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(snapshots.encode_png(w, h, pixels or solid(w, h)))

    def test_missing_baseline_fails_and_lists_what_exists(self):
        r = self.take()
        self.assertEqual(r["status"], "missing")
        self.assertIn("snapshots/ios/20x30@2x/home.png", r["message"])
        self.assertTrue(Path(r["actual"]).is_file())
        self.baseline("ios/375x667@2x")
        self.assertIn("ios/375x667@2x", self.take()["message"])

    def test_record_mode_saves_without_comparing(self):
        self.baseline(pixels=solid(20, 30, (0, 0, 0)))
        r = self.take(record=True)
        self.assertEqual(r["status"], "recorded")
        self.assertEqual(snapshots.read_png(r["actual"])[:2], (20, 30))

    def test_match_mismatch_and_masks(self):
        self.baseline()
        self.assertEqual(self.take()["status"], "match")
        changed = png(40, 60, pixels=paint(solid(40, 60), 40, 0, 0, 40, 4))  # top 2pt rows
        r = self.take(changed)
        self.assertEqual(r["status"], "mismatch")
        self.assertAlmostEqual(r["diff_percent"], 100 * 20 * 2 / 600, places=3)
        self.assertTrue(Path(r["diff"]).is_file())
        self.assertEqual(self.take(changed, masks=[("rect", 0, 0, 20, 2)])["status"], "match")
        self.assertEqual(self.take(changed, tolerance=10)["status"], "match")

    def test_tag_mask_uses_element_pixels(self):
        self.baseline()
        changed = png(40, 60, pixels=paint(solid(40, 60), 40, 10, 10, 20, 20))
        elements = [{"id": "clock", "x": 10, "y": 10, "w": 10, "h": 10}]
        self.assertEqual(self.take(changed, masks=[("tag", "clock")], elements=elements)["status"],
                         "match")
        with self.assertRaises(snapshots.SnapshotError):
            self.take(changed, masks=[("tag", "nope")], elements=elements)

    def test_size_mismatch_fails(self):
        self.baseline(w=20, h=31, pixels=solid(20, 31))
        r = self.take()
        self.assertEqual(r["status"], "mismatch")
        self.assertIn("20x31", r["message"])

    def test_status_bar_mask_depends_on_screen_shape(self):
        self.assertEqual(snapshots.mask_rects([("status_bar",)], "ios", 375, 667, 2), [(0, 0, 375, 20)])
        self.assertEqual(snapshots.mask_rects([("status_bar",), ("nav_bar",)], "ios", 393, 852, 3),
                         [(0, 0, 393, 59), (0, 818, 393, 852)])
        self.assertEqual(snapshots.mask_rects([("nav_bar",)], "ios", 375, 667, 2), [])


SCREEN = {"width": 750, "height": 1334, "scale": 2.0}  # 375x667 pt


def el(tag, x, y, w, h):  # pt -> device px at scale 2
    return {"id": tag, "x": x * 2, "y": y * 2, "w": w * 2, "h": h * 2}


ELEMENTS = [el("logo", 137.5, 100, 100, 50), el("title", 40, 170, 295, 30),
            el("agree", 40, 600, 295, 44), el("terms", 20, 190, 335, 400)]


class LayoutTests(unittest.TestCase):
    def check(self, args):
        return layout.check(args, SCREEN, ELEMENTS)

    def test_passing_checks(self):
        for args in ("logo visible", "logo centered-x", "logo inside screen",
                     "title below logo gap>=10 gap<=30", "logo above title",
                     "title no-overlap agree", "title aligned-left agree",
                     "agree aligned-center-x logo", "title inside terms tol=40",
                     "logo centered-x tol=1%"):
            ok, msg = self.check(args)
            self.assertTrue(ok, f"{args}: {msg}")

    def test_failing_checks(self):
        for args in ("title centered-y", "logo below title", "title below logo gap>=30",
                     "terms no-overlap title", "missing visible", "title aligned-left terms"):
            ok, msg = self.check(args)
            self.assertFalse(ok, f"{args}: {msg}")
        self.assertIn("tags on screen", self.check("missing visible")[1])

    def test_bad_syntax(self):
        for args in ("logo", "logo spins", "logo below", "logo below title gap=3", "logo visible tol>=2"):
            with self.assertRaises(layout.LayoutError, msg=args):
                self.check(args)


class RunnerStepTests(unittest.TestCase):
    """The SNAPSHOT / ASSERT_LAYOUT steps inside the runner, with a fake controller."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.case = self.tmp / "case"
        self.case.mkdir()
        core._set_project_root(self.tmp / "run")
        core._current_test_dir = self.case
        core._step_detail = {}
        frame = png(750, 1334)

        def screenshot(path):
            Path(path).write_bytes(frame)
            return str(path)
        for patcher in (patch.object(core.controller, "screenshot", side_effect=screenshot),
                        patch.object(core.controller, "get_screen_info", return_value=SCREEN),
                        patch.object(core.controller, "get_elements", return_value=ELEMENTS),
                        patch.dict(os.environ, {"DEVICE_PLATFORM": "ios", "DEVICE_ID": "u"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def step(self, raw):
        step = core.parse_steps([raw])[0]
        with redirect_stdout(io.StringIO()):
            ok = core.execute_step(step)
        return ok, core._step_detail

    def test_missing_baseline_fails_record_then_match(self):
        ok, detail = self.step("SNAPSHOT: eula mask=status_bar")
        self.assertFalse(ok)
        snap = detail["snapshot"]
        self.assertEqual((snap["status"], snap["screen_class"]), ("missing", "ios/375x667@2x"))
        self.assertEqual(snap["actual"], "outputs/snapshots/eula.actual.png")
        with patch.dict(os.environ, {"VT_RECORD_SNAPSHOTS": "1"}):
            ok, detail = self.step("SNAPSHOT: eula")
        self.assertTrue(ok)
        self.assertEqual(detail["snapshot"]["status"], "recorded")
        target = self.case / "snapshots" / "ios" / "375x667@2x" / "eula.png"
        target.parent.mkdir(parents=True)
        target.write_bytes((core.PROJECT_ROOT / detail["snapshot"]["actual"]).read_bytes())
        ok, detail = self.step("SNAPSHOT: eula")
        self.assertTrue(ok, detail)
        self.assertEqual(detail["snapshot"]["status"], "match")
        self.assertEqual(detail["snapshot"]["baseline"], "outputs/snapshots/eula.baseline.png")

    def test_old_controller_without_scale_fails_clearly(self):
        with patch.object(core.controller, "get_screen_info",
                          side_effect=RuntimeError("the controller reported no screen scale")):
            ok, detail = self.step("SNAPSHOT: eula")
        self.assertFalse(ok)
        self.assertIn("no screen scale", detail["snapshot"]["message"])

    def test_layout_step(self):
        ok, detail = self.step("ASSERT_LAYOUT: title below logo gap>=10")
        self.assertTrue(ok, detail)
        ok, detail = self.step("ASSERT_LAYOUT: logo below title")
        self.assertFalse(ok)
        self.assertFalse(detail["layout"]["passed"])

    def test_run_flags(self):
        with patch.dict(os.environ, {"VT_RECORD_SNAPSHOTS": "1", "VT_CASE": "eula"}):
            self.assertEqual(core.run_flags(), {"record_snapshots": True, "case": "eula"})
        with patch.dict(os.environ, {"VT_RECORD_SNAPSHOTS": "0", "VT_CASE": ""}):
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
             ("tests", "a.txt", b"SNAPSHOT: home\n")] + extra)
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
        self.assertEqual(names, ["visual-tests/eula/snapshots/ios/375x667@2x/eula.png",
                                 "visual-tests/eula/snapshots/ios/375x667@2x/home.png"])
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
            (case / "t.txt").write_text("SNAPSHOT: home")
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
