import io
import json
import plistlib
import struct
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import app

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mt-tests" / "scripts"))
sys.path.insert(0, str(ROOT / "ci" / "mt-submit"))
import controller_client
import apk_manifest
import device_lock
import ios_app_archive
import submit
import test_runner_core as core


def make_archive(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def plist(bundle_id):
    return plistlib.dumps({"CFBundleIdentifier": bundle_id}, fmt=plistlib.FMT_BINARY)


class ApkManifestTests(unittest.TestCase):
    @staticmethod
    def manifest(utf8):
        names = ["versionName", "package", "1.2", "com.example.app", "manifest"]
        if utf8:
            encoded = [bytes([len(n), len(n)]) + n.encode() + b"\0" for n in names]
            flags = 0x100
        else:
            encoded = [struct.pack("<H", len(n)) + n.encode("utf-16-le") + b"\0\0" for n in names]
            flags = 0
        offsets, cursor = [], 0
        for item in encoded:
            offsets.append(cursor)
            cursor += len(item)
        blob = b"".join(encoded)
        blob += b"\0" * (-len(blob) % 4)
        start = 28 + 4 * len(names)
        pool = struct.pack("<HHIIIIII", 1, 28, start + len(blob), len(names), 0, flags, start, 0)
        pool += struct.pack(f"<{len(names)}I", *offsets) + blob
        attrs = struct.pack("<IIIHBBI", 0xFFFFFFFF, 0, 2, 8, 0, 3, 2)
        attrs += struct.pack("<IIIHBBI", 0xFFFFFFFF, 1, 3, 8, 0, 3, 3)
        body = struct.pack("<IIHHHHHH", 0xFFFFFFFF, 4, 20, 20, 2, 0, 0, 0) + attrs
        element = struct.pack("<HHIII", 0x0102, 16, 16 + len(body), 1, 0xFFFFFFFF) + body
        return struct.pack("<HHI", 3, 8, 8 + len(pool) + len(element)) + pool + element

    def test_reads_utf8_and_utf16_pools(self):
        for utf8 in (True, False):
            attrs = apk_manifest.parse_manifest_attributes(self.manifest(utf8))
            self.assertEqual(attrs, {"package": "com.example.app", "versionName": "1.2"})

    def test_garbage_is_not_mistaken_for_a_manifest(self):
        self.assertEqual(apk_manifest.parse_manifest_attributes(b"\0" * 64), {})


class IosArchiveTests(unittest.TestCase):
    def test_ipa_uses_app_plist_not_nested_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            ipa = Path(tmp) / "app.ipa"
            make_archive(ipa, {
                "Payload/My.app/Frameworks/F.framework/Info.plist": plist("nested.framework"),
                "Payload/My.app/PlugIns/E.appex/Info.plist": plist("nested.extension"),
                "Payload/My.app/Watch/W.app/Info.plist": plist("nested.watch"),
                "Payload/My.app/Info.plist": plist("com.example.app"),
            })
            self.assertEqual(ios_app_archive.read_bundle_id(ipa), "com.example.app")

    def test_zip_with_bare_app_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "app.zip"
            make_archive(archive, {"My.app/": b"", "My.app/Info.plist": plist("com.example.zip")})
            self.assertEqual(ios_app_archive.read_bundle_id(archive), "com.example.zip")

    def test_archive_without_app_or_not_a_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.ipa"
            make_archive(empty, {"readme.txt": b"x"})
            with self.assertRaises(ValueError):
                ios_app_archive.read_bundle_id(empty)
            junk = Path(tmp) / "junk.ipa"
            junk.write_bytes(b"not a zip")
            with self.assertRaises(ValueError):
                ios_app_archive.read_bundle_id(junk)


class AppUnderTestTests(unittest.TestCase):
    def test_restart_without_app_id_does_nothing(self):
        with patch.object(controller_client, "_http") as http:
            self.assertFalse(controller_client.restart_app(None))
        http.assert_not_called()

    def test_unknown_package_is_treated_as_no_app(self):
        core.set_app_under_test("unknown")
        self.assertIsNone(core._app_under_test)
        core.set_app_under_test("a.b")
        self.assertEqual(core._app_under_test, "a.b")
        core.set_app_under_test(None)


class RunResultTests(unittest.TestCase):
    def result(self, meta=None, exit_code=None):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_1"
            run_dir.mkdir()
            if meta is not None:
                (run_dir / "run_metadata.json").write_text(json.dumps(meta))
            if exit_code is not None:
                (run_dir / app.RUN_EXIT_CODE_FILE).write_text(f"{exit_code}\n")
            with patch.object(app, "TEST_DIR", Path(tmp)), \
                    patch.object(app, "get_run_metadata", return_value=meta):
                return app.get_run_result("run_1")

    def test_in_progress(self):
        self.assertEqual(self.result({"status": "running"})["status"], "running")

    def test_passed_requires_tests_and_clean_exit(self):
        done = {"status": "completed", "passed": 2, "failed": 0, "total": 2}
        self.assertEqual(self.result(done, 0)["status"], "passed")
        self.assertEqual(self.result(done)["status"], "passed")
        self.assertEqual(self.result(done, 1)["status"], "failed")

    def test_empty_run_fails(self):
        result = self.result({"status": "completed", "total": 0}, 0)
        self.assertEqual(result["status"], "failed")
        self.assertIn("no tests", result["reason"])

    def test_test_failure_fails(self):
        meta = {"status": "completed_with_failures", "passed": 1, "failed": 1, "total": 2}
        self.assertEqual(self.result(meta, 1)["status"], "failed")

    def test_crashed_runner_still_marked_running_fails(self):
        result = self.result({"status": "running"}, -9)
        self.assertEqual(result["status"], "failed")
        self.assertIn("-9", result["reason"])

    def test_install_failure_reports_its_error(self):
        result = self.result({"status": "failed", "error": "APK installation failed"}, 1)
        self.assertEqual(result["reason"], "APK installation failed")


class DeviceLockTests(unittest.TestCase):
    def test_second_holder_waits_then_times_out_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            held = device_lock.acquire_device_locks(tmp, ["10.0.0.1:5555"])
            waited = []
            with self.assertRaises(TimeoutError):
                device_lock.acquire_device_locks(
                    tmp, ["other", "10.0.0.1:5555"], on_wait=waited.append,
                    timeout=0, poll_secs=0)
            self.assertEqual(waited, ["10.0.0.1:5555"])
            device_lock.release_device_locks(held)
            again = device_lock.acquire_device_locks(tmp, ["10.0.0.1:5555", "other"], timeout=0)
            device_lock.release_device_locks(again)


class PreviousRunWaitTests(unittest.TestCase):
    def test_waits_for_the_previous_process(self):
        import subprocess
        import time as _time
        import run_on_device
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.0)"])
        try:
            t0 = _time.monotonic()
            with redirect_stdout(io.StringIO()), \
                    patch.object(run_on_device.core, "initialize_run_progress") as progress:
                waiter = threading.Thread(target=run_on_device.wait_for_previous_run,
                                          args=(proc.pid,), kwargs={"poll_secs": 0.05})
                waiter.start()
                proc.wait()  # reap it, as the viewer does
                waiter.join(10)
            self.assertFalse(waiter.is_alive())
            self.assertGreaterEqual(_time.monotonic() - t0, 0.9)
            progress.assert_called_once()
            self.assertEqual(progress.call_args[0][0], "queued")
        finally:
            proc.kill()

    def test_exit_code_file_ends_the_wait(self):
        import subprocess
        import run_on_device
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d) / "run_9"
                (Path(d) / "run_8").mkdir()
                (Path(d) / "run_8" / "exit_code").write_text("0\n")
                with patch.object(run_on_device.core, "RUN_ROOT", root), \
                        patch.object(run_on_device.core, "initialize_run_progress"), \
                        redirect_stdout(io.StringIO()):
                    run_on_device.wait_for_previous_run(proc.pid, "run_8", poll_secs=0.05)
        finally:
            proc.kill()
            proc.wait()


class PlatformWaitTests(unittest.TestCase):
    def setUp(self):
        import run_on_device
        self.rod = run_on_device
        p = patch.object(run_on_device.core, "initialize_run_progress")
        self.progress = p.start()
        self.addCleanup(p.stop)

    def test_waits_until_every_platform_has_a_device(self):
        android = {"id": "ZA2235GDS8", "platform": "android"}
        iphone = {"id": "00008030-000C50D42EE3802E", "platform": "ios"}
        answers = [([], "controller unreachable"), ([android], None), ([android, iphone], None)]
        with patch.object(self.rod.controller, "list_devices", side_effect=answers), \
                redirect_stdout(io.StringIO()):
            found, error = self.rod.wait_for_platform_devices(["android", "ios"], timeout=30, poll_secs=0)
        self.assertIsNone(error)
        self.assertEqual(found, {"android": ["ZA2235GDS8"], "ios": ["00008030-000C50D42EE3802E"]})
        phases = [c.args[0] for c in self.progress.call_args_list]
        self.assertEqual(set(phases), {"waiting_for_devices"})

    def test_gives_up_after_the_limit(self):
        with patch.object(self.rod.controller, "list_devices", return_value=([], None)), \
                redirect_stdout(io.StringIO()):
            found, error = self.rod.wait_for_platform_devices(["ios"], timeout=0, poll_secs=0)
        self.assertIsNone(found)
        self.assertIn("an iPhone", error)

    def test_default_limit_is_five_minutes(self):
        self.assertEqual(self.rod.DEFAULT_CONNECT_WAIT_SECS, 300)


class SubmitTests(unittest.TestCase):
    def args(self, tmp, app_name="app.ipa", platform="ios"):
        return submit.argparse.Namespace(
            url="http://viewer", app=str(Path(tmp) / app_name), platform=platform,
            tests_dir=str(Path(tmp) / "cases"), branch="b", timeout=5, poll=0)

    def setup_case(self, tmp, name="case_a"):
        (Path(tmp) / "app.ipa").write_bytes(b"ipa")
        case = Path(tmp) / "cases" / name
        case.mkdir(parents=True)
        (case / "INSTRUCTION.txt").write_text("MODE: once")
        (case / "t.txt").write_text("CLICK: x")

    def test_wrong_platform_or_no_cases_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app.ipa").write_bytes(b"ipa")
            self.assertEqual(submit.run(self.args(tmp, platform="android")), 1)
            self.assertEqual(submit.run(self.args(tmp)), 1)

    def test_rejected_upload_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_case(tmp)
            with patch.object(submit, "request_json", return_value=(400, {"error": "no devices"})):
                self.assertEqual(submit.run(self.args(tmp)), 1)

    def test_every_case_goes_in_one_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_case(tmp, "a")
            self.setup_case(tmp, "b")
            started = (200, {"run_id": "run_1"})
            passed = (200, {"status": "passed"})
            failed = (200, {"status": "failed", "reason": "1 test(s) failed"})
            with patch.object(submit, "request_json", side_effect=[started, passed]) as rq, \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(submit.run(self.args(tmp)), 0)
            uploads = [call for call in rq.call_args_list if len(call.args) > 1 and call.args[1] is not None]
            self.assertEqual(len(uploads), 1)
            body = uploads[0].args[1]
            for name in (b"a/INSTRUCTION.txt", b"a/t.txt", b"b/INSTRUCTION.txt", b"b/t.txt"):
                self.assertIn(b'filename="' + name + b'"', body)
            self.assertNotIn(b'name="case"', body)
            with patch.object(submit, "request_json", side_effect=[started, failed]), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(submit.run(self.args(tmp)), 1)

    def test_timeout_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            args.timeout = 0
            self.assertEqual(submit.wait_for_result(args, "run_1")["status"], "failed")

    def test_android_and_ios_apps_go_in_one_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.setup_case(tmp)
            (Path(tmp) / "app.apk").write_bytes(b"apk")
            args = self.args(tmp)
            args.app = args.platform = None
            args.android_app = str(Path(tmp) / "app.apk")
            args.ios_app = str(Path(tmp) / "app.ipa")
            sent = []

            def fake(url, data=None, content_type=None, timeout=60):
                if data is None:
                    return 200, {"status": "passed"}
                sent.append((content_type, data))
                return 200, {"run_id": "run_1"}

            with patch.object(submit, "request_json", side_effect=fake), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(submit.run(args), 0)
            self.assertEqual(len(sent), 1)  # one case, one run for both platforms
            handler = MagicMock()
            handler.headers = {"Content-Type": sent[0][0], "Content-Length": str(len(sent[0][1]))}
            handler.rfile = io.BytesIO(sent[0][1])
            fields, files = app.parse_multipart_form(handler)
            self.assertEqual(fields["device_mode"], "all")
            self.assertEqual(files["apk"][0]["filename"], "app.apk")
            self.assertEqual(files["ios_app"][0]["filename"], "app.ipa")

    def test_resolve_apps_checks_extensions_and_mixing(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app.ipa").write_bytes(b"ipa")
            ns = submit.argparse.Namespace
            ipa = str(Path(tmp) / "app.ipa")
            self.assertIsNone(submit.resolve_apps(ns(android_app=ipa, ios_app=None, app=None, platform=None))[0])
            self.assertIsNone(submit.resolve_apps(ns(android_app=None, ios_app=ipa, app=ipa, platform="ios"))[0])
            self.assertIsNone(submit.resolve_apps(ns(android_app=None, ios_app=None, app=None, platform=None))[0])
            apps, error = submit.resolve_apps(ns(android_app=None, ios_app=ipa, app=None, platform=None))
            self.assertEqual((list(apps), error), (["ios"], None))

    def test_multipart_is_parsed_by_the_viewer(self):
        content_type, body = submit.build_multipart(
            {"device_mode": "ios"}, [("apk", "a.ipa", b"\x00\xffbin"), ("tests", "t.txt", b"x")])
        handler = MagicMock()
        handler.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        fields, files = app.parse_multipart_form(handler)
        self.assertEqual(fields["device_mode"], "ios")
        self.assertEqual(files["apk"][0]["filename"], "a.ipa")
        self.assertEqual(files["apk"][0]["content"], b"\x00\xffbin")


if __name__ == "__main__":
    unittest.main()
