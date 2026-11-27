"""`DIFF: SNAPSHOT` / `DIFF: ASSERT_LAYOUT` through a plugin: runner-supplied
params (`source`), the result detail and files the viewer reads, the parse
errors of the bare forms and the "not configured" message. The plugin is a
stdlib fake speaking the contract (/health, /actions, /uploads, /jobs)."""
import base64
import io
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import zipfile
import zlib
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mt-tests" / "scripts"))
import plugin_client as pc  # noqa: E402
import test_runner_core as core  # noqa: E402


def png(w, h, rgb=(200, 200, 200)):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body)))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


FRAME = png(6, 4)           # the "device screenshot" (device px)
ACTUAL = png(3, 2)          # what the plugin returns as the 1x capture
DIFF = png(3, 2, (255, 0, 0))
SCREEN = {"width": 750, "height": 1334, "scale": 2.0}
ELEMENTS = [{"id": "clock", "x": 10, "y": 10, "w": 20, "h": 20}]
CLASS = "ios/375x667@2x"

ARGS = {"type": "string", "source": "args", "required": True}
ACTIONS = [
    {"name": "SNAPSHOT", "default_timeout_ms": 30000, "needs_device": False, "params": {
        "args": ARGS,
        "screenshot": {"type": "path", "source": "screenshot", "required": True},
        "screen": {"type": "json", "source": "screen", "required": True},
        "elements": {"type": "json", "source": "elements", "when": "tag_mask"},
        "platform": {"type": "string", "source": "platform", "required": True},
        "baselines": {"type": "path", "source": "baselines", "required": True},
        "record": {"type": "bool", "source": "record"},
    }},
    {"name": "ASSERT_LAYOUT", "default_timeout_ms": 30000, "needs_device": False, "params": {
        "args": ARGS,
        "screen": {"type": "json", "source": "screen", "required": True},
        "elements": {"type": "json", "source": "elements", "required": True},
    }},
    {"name": "ECHO", "needs_device": False, "params": {
        "value": {"type": "string"},
        "platform": {"type": "string", "source": "platform"},
    }},
    {"name": "WEIRD", "needs_device": False, "params": {
        "x": {"type": "string", "source": "telepathy", "required": True},
    }},
]


class FakeDiff(BaseHTTPRequestHandler):
    """A DIFF plugin: compares nothing, answers from what the runner sent."""
    uploads = {}
    jobs = {}
    started = []
    layout_result = {"passed": True, "message": "title below logo (gap 10)"}

    def log_message(self, format, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ok": True, "contract": "monkeytester-plugin",
                                    "contract_version": 1, "capabilities": ["uploads"],
                                    "plugin": {"name": "DIFF", "default_name": "DIFF"}})
        if self.path == "/actions":
            return self.reply(200, {"actions": ACTIONS})
        if self.path.startswith("/jobs/"):
            job_id = self.path[len("/jobs/"):].split("?")[0]
            return self.reply(200, self.jobs[job_id])
        self.reply(404, {"error": "not found", "code": "not_found"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/uploads":
            ref = f"upload:u{len(self.uploads) + 1}"
            ctype = self.headers.get("Content-Type")
            if body.startswith(b"\x89PNG"):
                assert ctype == "image/png", ctype
                self.uploads[ref] = body
            else:
                assert ctype == "application/zip", ctype
                self.uploads[ref] = zipfile.ZipFile(io.BytesIO(body))
            return self.reply(201, {"upload": ref, "path": f"/tmp/{ref}"})
        if self.path == "/jobs":
            req = json.loads(body)
            self.started.append(req)
            state, result = self.run_action(req["action"], req["params"])
            job = {"id": f"j{len(self.jobs) + 1}", "action": req["action"], "state": state,
                   "result": result, "error": None, "log": ["compared"]}
            self.jobs[job["id"]] = job
            return self.reply(202, {"job": job})
        self.reply(404, {"error": "not found", "code": "not_found"})

    def run_action(self, action, params):
        if action == "ASSERT_LAYOUT":
            r = dict(self.layout_result)
            return ("passed" if r["passed"] else "failed"), r
        if action != "SNAPSHOT":
            return "passed", {"echo": params}
        name = params["args"].split()[0]
        assert self.uploads[params["screenshot"]] == FRAME
        baselines = self.uploads[params["baselines"]].namelist()
        result = {"name": name, "screen_class": CLASS, "tolerance": 0.1, "diff_percent": None,
                  "baseline_path": f"{CLASS}/{name}.png", "available_classes": [],
                  "actual_png_b64": base64.b64encode(ACTUAL).decode(), "diff_png_b64": None}
        if params.get("record"):
            return "passed", {**result, "status": "recorded", "message": "recorded"}
        if f"snapshots/{CLASS}/{name}.png" not in baselines:
            return "failed", {**result, "status": "missing",
                              "message": f"baseline missing: snapshots/{CLASS}/{name}.png"}
        return "passed", {**result, "status": "match", "diff_percent": 0.0,
                          "diff_png_b64": base64.b64encode(DIFF).decode(),
                          "message": "0.000% of pixels differ"}


class DiffStepCase(unittest.TestCase):
    def setUp(self):
        FakeDiff.uploads, FakeDiff.jobs, FakeDiff.started = {}, {}, []
        FakeDiff.layout_result = {"passed": True, "message": "title below logo (gap 10)"}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDiff)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.case = self.tmp / "case"
        self.case.mkdir()
        core._set_project_root(self.tmp / "run")
        core._current_test_dir = self.case
        core._step_detail = {}
        core._plugins = None
        self.addCleanup(setattr, core, "_plugins", None)
        url = f"http://127.0.0.1:{self.server.server_address[1]}"

        def screenshot(path):
            Path(path).write_bytes(FRAME)
            return str(path)
        self.get_elements = patch.object(core.controller, "get_elements", return_value=ELEMENTS)
        for patcher in (patch.object(core.controller, "screenshot", side_effect=screenshot),
                        patch.object(core.controller, "get_screen_info", return_value=SCREEN),
                        self.get_elements,
                        patch.dict(os.environ, {"DEVICE_PLATFORM": "ios", "DEVICE_ID": "u",
                                                "MT_PLUGINS": f"DIFF={url}",
                                                "MT_AI_PLUGIN_URL": "", "VISION_MODEL_API_URL": "",
                                                "MT_RECORD_SNAPSHOTS": ""})):
            started = patcher.start()
            if patcher is self.get_elements:
                self.elements_mock = started
            self.addCleanup(patcher.stop)

    def step(self, raw):
        step = core.parse_steps([raw])[0]
        out = io.StringIO()
        core._step_detail = {}
        with redirect_stdout(out):
            ok = core.execute_step(step)
        self.log = out.getvalue()
        return ok, core._step_detail

    def baseline(self, name="eula", cls=CLASS):
        path = self.case / "snapshots" / cls / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png(3, 2, (1, 2, 3)))
        return path


class ParseTests(unittest.TestCase):
    def test_bare_snapshot_and_layout_are_parse_errors_naming_the_replacement(self):
        steps = core.parse_steps(["SNAPSHOT: eula mask=status_bar",
                                  "IF IOS: ASSERT_LAYOUT: title below logo gap>=8",
                                  "snapshot:"])
        self.assertEqual(steps[0]["error"], 'SNAPSHOT: steps were removed; write '
                                            '"DIFF: SNAPSHOT eula mask=status_bar" (line 1)')
        self.assertEqual(steps[1]["error"], 'ASSERT_LAYOUT: steps were removed; write '
                                            '"DIFF: ASSERT_LAYOUT title below logo gap>=8" (line 2)')
        self.assertEqual(steps[1]["platform"], "ios")
        self.assertIn('"DIFF: SNAPSHOT <args>" (line 3)', steps[2]["error"])
        with redirect_stdout(io.StringIO()) as out:
            self.assertFalse(core.execute_step(steps[0]))
        self.assertIn("PARSE ERROR: SNAPSHOT: steps were removed", out.getvalue())

    def test_diff_steps_parse_as_plugin_steps(self):
        step = core.parse_steps(["IF ANDROID: DIFF: SNAPSHOT eula mask=status_bar,nav_bar"])[0]
        self.assertEqual((step["command"], step["plugin"], step.get("error")), ("DIFF", "DIFF", None))
        self.assertEqual(step["args"], "SNAPSHOT eula mask=status_bar,nav_bar")

    def test_step_names_stay_reserved(self):
        for name in ("SNAPSHOT", "ASSERT_LAYOUT", "MOTION"):
            self.assertIn(name, pc.RESERVED_STEP_NAMES)
            self.assertIsNone(core._plugin_step_name(name + ":"))


class NotConfiguredTests(DiffStepCase):
    def test_missing_diff_plugin_fails_with_the_setup_message(self):
        with patch.dict(os.environ, {"MT_PLUGINS": ""}):
            core._plugins = None
            ok, detail = self.step("DIFF: SNAPSHOT eula mask=status_bar")
        self.assertFalse(ok)
        self.assertIn("DIFF plugin is not configured: add MonkeyTester-Plugin-Diff on "
                      "Settings → Plugins", self.log)
        self.assertEqual(FakeDiff.started, [])

    def test_other_unknown_plugins_keep_the_old_message(self):
        ok, _ = self.step("NOPE: thing")
        self.assertFalse(ok)
        self.assertIn("Unknown plugin 'NOPE'. Configured plugins: DIFF", self.log)


class SourceParamTests(DiffStepCase):
    def test_snapshot_gets_every_runner_supplied_param(self):
        self.baseline()
        ok, _ = self.step("DIFF: SNAPSHOT eula mask=status_bar,tag:clock tolerance=0.5")
        self.assertTrue(ok, self.log)
        req = FakeDiff.started[-1]
        params = req["params"]
        self.assertEqual(req["action"], "SNAPSHOT")
        self.assertEqual(params["args"], "eula mask=status_bar,tag:clock tolerance=0.5")
        self.assertEqual((params["screen"], params["elements"]), (SCREEN, ELEMENTS))
        self.assertEqual((params["platform"], params["record"]), ("ios", False))
        self.assertEqual(FakeDiff.uploads[params["screenshot"]], FRAME)  # raw PNG, image/png
        self.assertEqual(FakeDiff.uploads[params["baselines"]].namelist(),
                         [f"snapshots/{CLASS}/eula.png"])
        self.assertNotIn("timeout_ms", req)

    def test_baselines_zip_holds_only_baseline_paths(self):
        self.baseline()
        self.baseline("home", "android/443x980@2.438x")
        (self.case / "snapshots" / ".DS_Store").write_bytes(b"x")
        (self.case / "snapshots" / "ios" / "notes.png").write_bytes(png(1, 1))
        os.symlink(self.case / "snapshots" / CLASS / "eula.png",
                   self.case / "snapshots" / CLASS / "link.png")
        self.step("DIFF: SNAPSHOT eula")
        names = FakeDiff.uploads[FakeDiff.started[-1]["params"]["baselines"]].namelist()
        self.assertEqual(sorted(names), ["snapshots/android/443x980@2.438x/home.png",
                                         f"snapshots/{CLASS}/eula.png"])

    def test_elements_only_fetched_when_the_args_match(self):
        ok, _ = self.step("DIFF: SNAPSHOT eula mask=status_bar")
        self.assertFalse(ok)  # no baseline
        self.assertNotIn("elements", FakeDiff.started[-1]["params"])
        self.elements_mock.assert_not_called()
        # The empty snapshots/ dir is still uploaded (the plugin lists classes).
        self.assertEqual(FakeDiff.uploads[FakeDiff.started[-1]["params"]["baselines"]].namelist(), [])

    def test_record_flag_and_lowercase_action(self):
        with patch.dict(os.environ, {"MT_RECORD_SNAPSHOTS": "1"}):
            ok, detail = self.step("DIFF: snapshot eula")
        self.assertTrue(ok, self.log)
        self.assertEqual(FakeDiff.started[-1]["action"], "SNAPSHOT")
        self.assertIs(FakeDiff.started[-1]["params"]["record"], True)
        self.assertEqual(detail["snapshot"]["status"], "recorded")

    def test_author_may_not_set_a_source_param(self):
        ok, _ = self.step("DIFF: ECHO value=1 platform=android")
        self.assertFalse(ok)
        self.assertIn("platform= is filled by MonkeyTester (source: platform)", self.log)
        self.assertEqual(FakeDiff.started, [])
        ok, _ = self.step("DIFF: ECHO value=1")
        self.assertTrue(ok, self.log)
        self.assertEqual(FakeDiff.started[-1]["params"], {"value": "1", "platform": "ios"})

    def test_unknown_source_fails_before_the_job(self):
        ok, _ = self.step("DIFF: WEIRD")
        self.assertFalse(ok)
        self.assertIn("unknown param source 'telepathy'", self.log)
        self.assertEqual(FakeDiff.started, [])

    def test_required_source_that_cannot_be_filled_fails_the_step(self):
        with patch.object(core.controller, "screenshot", return_value=None):
            ok, _ = self.step("DIFF: SNAPSHOT eula")
        self.assertFalse(ok)
        self.assertIn("could not fill screenshot (source: screenshot): screenshot capture failed",
                      self.log)
        self.assertEqual(FakeDiff.started, [])


class ResultDetailTests(DiffStepCase):
    def test_missing_writes_actual_and_snapshot_detail(self):
        ok, detail = self.step("DIFF: SNAPSHOT eula mask=status_bar")
        self.assertFalse(ok)
        snap = detail["snapshot"]
        self.assertEqual((snap["status"], snap["screen_class"]), ("missing", CLASS))
        self.assertEqual(snap["actual"], "outputs/snapshots/eula.actual.png")
        self.assertEqual((snap["baseline"], snap["diff"]), (None, None))
        self.assertEqual(snap["baseline_path"], f"{CLASS}/eula.png")
        self.assertEqual(set(snap), {"name", "status", "screen_class", "baseline_path",
                                     "diff_percent", "tolerance", "message", "actual",
                                     "baseline", "diff"})
        self.assertEqual((core.PROJECT_ROOT / snap["actual"]).read_bytes(), ACTUAL)
        self.assertNotIn(base64.b64encode(ACTUAL).decode(), self.log)  # base64 elided
        self.assertIn("SNAPSHOT eula [ios/375x667@2x]: MISSING", self.log)

    def test_match_writes_baseline_copy_and_diff(self):
        source = self.baseline()
        ok, detail = self.step("DIFF: SNAPSHOT eula")
        self.assertTrue(ok, self.log)
        snap = detail["snapshot"]
        self.assertEqual((snap["status"], snap["diff_percent"]), ("match", 0.0))
        self.assertEqual(snap["baseline"], "outputs/snapshots/eula.baseline.png")
        self.assertEqual(snap["diff"], "outputs/snapshots/eula.diff.png")
        self.assertEqual((core.PROJECT_ROOT / snap["baseline"]).read_bytes(), source.read_bytes())
        self.assertEqual((core.PROJECT_ROOT / snap["diff"]).read_bytes(), DIFF)

    def test_multi_case_outputs_go_under_the_case(self):
        case = self.tmp / "tests" / "eula"
        case.mkdir(parents=True)
        (case / "INSTRUCTION.txt").write_text("MODE: once\n")
        with patch.object(core, "_multi_case_root", (self.tmp / "tests").resolve()):
            core._current_test_dir = case
            ok, detail = self.step("DIFF: SNAPSHOT home")
        self.assertEqual(detail["snapshot"]["actual"], "outputs/eula/snapshots/home.actual.png")

    def test_layout_result_becomes_step_layout(self):
        ok, detail = self.step("DIFF: ASSERT_LAYOUT title below logo gap>=10")
        self.assertTrue(ok, self.log)
        self.assertEqual(detail, {"layout": {"passed": True, "message": "title below logo (gap 10)"}})
        params = FakeDiff.started[-1]["params"]
        self.assertEqual(params, {"args": "title below logo gap>=10", "screen": SCREEN,
                                  "elements": ELEMENTS})
        FakeDiff.layout_result = {"passed": False, "message": "logo is above title"}
        ok, detail = self.step("DIFF: ASSERT_LAYOUT logo below title")
        self.assertFalse(ok)
        self.assertFalse(detail["layout"]["passed"])

    def test_other_results_leave_no_detail(self):
        ok, detail = self.step("DIFF: ECHO value=x")
        self.assertTrue(ok)
        self.assertEqual(detail, {})

    def test_bad_snapshot_name_from_the_plugin_writes_no_file(self):
        core._record_result_detail({"name": "../evil", "status": "match",
                                    "actual_png_b64": base64.b64encode(ACTUAL).decode()})
        self.assertEqual(core._step_detail["snapshot"]["status"], "error")
        self.assertFalse((core.OUTPUTS_DIR / "snapshots").exists()
                         and any((core.OUTPUTS_DIR / "snapshots").iterdir()))


if __name__ == "__main__":
    unittest.main()
