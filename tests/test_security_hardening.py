"""Code hardening: validators, safe_join, request limits, headers, errors.

Server tests send the signed-in test session (test_app_api.TEST_COOKIE) and a
same-origin Origin, because login is always required (PR #84).
"""
import io
import json
import os
import socket
import stat
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import app
import security_utils as sec
from tests.build_fixture import BUILD_ID, add_build
from tests.test_app_api import FAKE_RUNNER, TEST_COOKIE, ServerCase, raw_get


def raw_request(port, head, body=b"", auth=True):
    """Send a raw request (head without the final blank line); return
    (status, headers dict lower-cased, body). auth=True adds the test session
    cookie, a Host (when missing) and a same-origin Origin."""
    if auth:
        if "\r\nhost:" not in head.lower():
            head += "\r\nHost: x"
        head += f"\r\nCookie: {TEST_COOKIE}\r\nOrigin: http://x"
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head.encode() + b"\r\n\r\n" + body)
        data = b""
        try:
            while chunk := sock.recv(65536):
                data += chunk
        except ConnectionResetError:
            pass
    head_blob, _, rest = data.partition(b"\r\n\r\n")
    lines = head_blob.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return int(lines[0].split()[1]), headers, rest


class ValidatorTests(unittest.TestCase):
    def test_device_ids(self):
        for ok in ("ZA2235GDS8", "10.0.0.9:5555", "00008030-000C50D42EE3802E", "emulator-5554"):
            self.assertTrue(sec.valid_device_id(ok), ok)
        for bad in ("", "x", "-rf", "--apk", "a b c d", "abc;rm", "../../x", "a/b/c/d", None, 5):
            self.assertFalse(sec.valid_device_id(bad), bad)

    def test_run_ids(self):
        self.assertTrue(sec.valid_run_id("run_12"))
        for bad in ("run_", "run_1/..", "/etc", ".builds", "run_1a", "config.json", "", None):
            self.assertFalse(sec.valid_run_id(bad), bad)

    def test_bundle_ids(self):
        for ok in ("com.example.app", "org.yurivibes.TemperatureSensor", "App", "a-b.c_d"):
            self.assertTrue(sec.valid_bundle_id(ok), ok)
        for bad in ("", "-x", ".com", "com..x", "com.x.", "a b", "a;b", "x" * 300):
            self.assertFalse(sec.valid_bundle_id(bad), bad)

    def test_branches(self):
        for ok in ("master", "feat/x", "security/viewer-hardening", "v1.2.3", "fix_1"):
            self.assertTrue(sec.valid_branch(ok), ok)
        for bad in ("", "-x", "--upload-pack=evil", "/x", "a..b", "a//b", "a b", "x.lock",
                    "x/", "x.", "a~1", "a^", "a:b", "a\\b", "a@{1}", "a\x00b"):
            self.assertFalse(sec.valid_branch(bad), bad)

    def test_test_names(self):
        for ok in ("login", "onboarding__first run", "my test..txt"):
            self.assertTrue(sec.valid_test_name(ok), ok)
        for bad in ("", ".hidden", "a/b", "a\\b", "a\x00", "x" * 300):
            self.assertFalse(sec.valid_test_name(bad), bad)

    def test_safe_filename(self):
        self.assertEqual(sec.safe_filename("TemperatureSensor.app.zip"), "TemperatureSensor.app.zip")
        self.assertEqual(sec.safe_filename("../../evil.apk"), "evil.apk")
        self.assertEqual(sec.safe_filename("..\\..\\evil.apk"), "evil.apk")
        self.assertEqual(sec.safe_filename(".hidden.apk"), "hidden.apk")
        self.assertEqual(sec.safe_filename("a b;c.apk"), "a_b_c.apk")
        self.assertEqual(sec.safe_filename(".."), "upload")
        self.assertNotIn("\x00", sec.safe_filename("a\x00.apk"))

    def test_http_only_urls(self):
        self.assertEqual(sec.check_http_url("http://h:1/x"), "http://h:1/x")
        self.assertEqual(sec.check_http_url("HTTPS://h/x"), "HTTPS://h/x")
        for bad in ("file:///etc/passwd", "ftp://h/x", "/etc/passwd", "", None, "gopher://x"):
            with self.assertRaises(ValueError, msg=bad):
                sec.check_http_url(bad)


class SafeJoinTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "root"
        (self.root / "run_1" / "outputs").mkdir(parents=True)
        (self.root / "run_1" / "outputs" / "a.png").write_bytes(b"x")
        (self.base / "secret.txt").write_text("secret")

    def test_inside_paths_resolve(self):
        p = sec.safe_join(self.root, "run_1", "outputs/a.png")
        self.assertEqual(p, (self.root / "run_1" / "outputs" / "a.png").resolve())
        self.assertEqual(sec.safe_join(self.root), self.root.resolve())

    def test_rejects_escapes(self):
        for parts in (("..",), ("run_1", "../../secret.txt"), ("/etc/passwd",), ("run_1", "/etc"),
                      ("a\x00b",), ("a\\..\\b",), ("",), ("run_1/../..",), (None,)):
            with self.assertRaises(sec.UnsafePath, msg=parts):
                sec.safe_join(self.root, *parts)

    def test_rejects_symlink_out_of_root(self):
        os.symlink(self.base / "secret.txt", self.root / "run_1" / "link")
        with self.assertRaises(sec.UnsafePath):
            sec.safe_join(self.root, "run_1", "link")

    def test_rejects_sibling_with_common_prefix(self):
        (self.base / "root2").mkdir()
        os.symlink(self.base / "root2", self.root / "sib")
        with self.assertRaises(sec.UnsafePath):
            sec.safe_join(self.root, "sib")

    def test_unsafe_path_is_a_value_error(self):
        self.assertTrue(issubclass(sec.UnsafePath, ValueError))


class LimitTests(unittest.TestCase):
    def test_body_length(self):
        self.assertEqual(sec.body_length({}, 10), 0)
        self.assertEqual(sec.body_length({"Content-Length": "10"}, 10), 10)
        for headers, status in (({"Content-Length": "11"}, 413),
                                ({"Content-Length": "abc"}, 400),
                                ({"Content-Length": "-1"}, 400),
                                ({"Transfer-Encoding": "chunked"}, 411)):
            with self.assertRaises(sec.BodyError) as cm:
                sec.body_length(headers, 10)
            self.assertEqual(cm.exception.status, status, headers)

    def test_upload_cap_from_env(self):
        mib = 1024 * 1024
        self.assertEqual(sec.max_upload_bytes({}), sec.DEFAULT_MAX_UPLOAD_MB * mib)
        self.assertEqual(sec.max_upload_bytes({"MT_MAX_UPLOAD_MB": "2048"}), 2048 * mib)
        self.assertEqual(sec.max_upload_bytes({"MT_MAX_UPLOAD_MB": "junk"}),
                         sec.DEFAULT_MAX_UPLOAD_MB * mib)
        self.assertEqual(sec.max_upload_bytes({"MT_MAX_UPLOAD_MB": "0"}),
                         sec.DEFAULT_MAX_UPLOAD_MB * mib)


class PrivateFileTests(unittest.TestCase):
    def test_write_private_json_is_0600_and_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x_config.json"
            path.write_text("{}")
            os.chmod(path, 0o664)
            sec.write_private_json(path, {"token": "t"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), {"token": "t"})
            self.assertEqual([p.name for p in Path(d).iterdir()], ["x_config.json"])

    def test_save_config_is_0600(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            with patch.object(app, "CONFIG_FILE", cfg):
                app.save_config({"visual_api_key": "k"})
            self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)


class TraversalRouteTests(ServerCase):
    def setUp(self):
        super().setUp()
        (self.root / "secret.txt").write_text("TOPSECRET")
        (self.runs / "config.json").write_text(json.dumps({"visual_api_key": "TOPSECRET"}))

    def test_absolute_run_names_do_not_reach_the_filesystem(self):
        # '//etc' used to join as an absolute path and list or read files
        # anywhere on disk through the run routes.
        for path in ("/api/run//etc", "/api/metadata//etc", "/api/summary//etc",
                     "/api/logs//etc", "/api/pending//etc", "/api/stats//etc",
                     "/api/device-summary//etc", "/api/cases//etc",
                     f"/api/run/{self.root}", f"/api/summary/{self.root}"):
            status, body = raw_get(self.port, path)
            self.assertIn(status, (200, 400, 404), path)
            self.assertNotIn(b"passwd", body, path)
            self.assertNotIn(b"TOPSECRET", body, path)
            self.assertNotIn(b"secret.txt", body, path)

    def test_run_routes_refuse_non_run_names(self):
        self.assertEqual(self.get_json("/api/run/.builds"), [])
        self.assertEqual(self.get_json("/api/cases/.uploads"), {"cases": [], "single": True})
        status, _ = raw_get(self.port, "/api/test//etc/passwd")
        self.assertEqual(status, 404)
        status, _ = raw_get(self.port, "/api/test-devices/run_1/a%2Fb")
        self.assertEqual(status, 400)  # encoded slash is refused up front

    def test_static_serves_only_inside_runs(self):
        for path in ("/static/config.json", "/static/.uploads/x", "/static/run_1",
                     "/static//etc/passwd", "/convert-video/config.json"):
            status, body = raw_get(self.port, path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"TOPSECRET", body, path)
        for path in ("/static/run_1/../config.json", "/static/run_1/..%5cconfig.json",
                     "/static/run_1/%00x"):
            status, _ = raw_get(self.port, path)
            self.assertEqual(status, 400, path)

    def test_symlink_out_of_a_run_is_not_served(self):
        os.symlink(self.root / "secret.txt", self.runs / "run_1" / "link.txt")
        status, body = raw_get(self.port, "/static/run_1/link.txt")
        self.assertEqual(status, 404)
        self.assertNotIn(b"TOPSECRET", body)

    def test_fonts_stay_in_fonts(self):
        for path in ("/fonts/..%2fapp.py", "/fonts/x.woff2", "/fonts/a.b.woff2"):
            status, _ = raw_get(self.port, path)
            self.assertIn(status, (400, 404), path)

    def test_delete_refuses_the_viewers_own_folders(self):
        (self.runs / ".builds").mkdir()
        (self.runs / ".uploads").mkdir()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/runs/delete", method="POST",
            data=json.dumps({"runs": [".builds", ".uploads", "..", "/tmp"]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        self.assertEqual(result["deleted"], [])
        self.assertTrue((self.runs / ".builds").is_dir())
        self.assertTrue((self.runs / ".uploads").is_dir())

    def test_post_paths_are_checked_too(self):
        status, _, _ = raw_request(self.port, "POST /api/config/build/plugins/..%2f/config HTTP/1.0\r\n"
                                              "Content-Length: 2", b"{}")
        self.assertEqual(status, 400)


class RequestLimitTests(ServerCase):
    def post_raw(self, path, headers, body=b""):
        head = f"POST {path} HTTP/1.0\r\nHost: x\r\n" + "\r\n".join(f"{k}: {v}" for k, v in headers.items())
        return raw_request(self.port, head, body)

    def test_oversize_json_is_413_without_reading_it(self):
        status, _, body = self.post_raw("/api/config", {"Content-Type": "application/json",
                                                        "Content-Length": str(2 * 1024 * 1024)})
        self.assertEqual(status, 413)
        self.assertIn(b"too large", body)

    def test_oversize_upload_is_413(self):
        with patch.dict(os.environ, {"MT_MAX_UPLOAD_MB": "1"}):
            status, _, _ = self.post_raw("/api/run-test", {
                "Content-Type": "multipart/form-data; boundary=x",
                "Content-Length": str(1024 * 1024 + 1)})
        self.assertEqual(status, 413)

    def test_upload_cap_does_not_apply_to_json_routes_and_vice_versa(self):
        # 2 MiB is fine for an upload (default 600 MiB) but not for JSON.
        status, _, _ = self.post_raw("/api/theme", {"Content-Length": str(2 * 1024 * 1024)})
        self.assertEqual(status, 413)

    def test_chunked_body_is_411(self):
        status, _, _ = self.post_raw("/api/config", {"Transfer-Encoding": "chunked"},
                                     b"2\r\n{}\r\n0\r\n\r\n")
        self.assertEqual(status, 411)

    def test_bad_content_length_is_400(self):
        status, _, _ = self.post_raw("/api/config", {"Content-Length": "lots"})
        self.assertEqual(status, 400)

    def test_malformed_json_is_400_everywhere(self):
        routes = ("/api/config", "/api/theme", "/api/config/devices", "/api/config/plugins",
                  "/api/runs/delete", "/api/press-event", "/api/visual-model/test",
                  "/api/config/build", "/api/builds", "/api/config/visual-model")
        for body in (b"{not json", b"[1, 2]", b"5", b'"str"'):
            for route in routes:
                status, _, resp = self.post_raw(route, {"Content-Type": "application/json",
                                                        "Content-Length": str(len(body))}, body)
                self.assertEqual(status, 400, (route, body, resp[:200]))
                self.assertNotIn(b"Traceback", resp)


class HeaderAndErrorTests(ServerCase):
    def head_of(self, path):
        status, headers, body = raw_request(self.port, f"GET {path} HTTP/1.0\r\nHost: x")
        return status, headers, body

    def test_security_headers_on_pages_api_and_errors(self):
        for path in ("/", "/test.html", "/config", "/api/runs", "/health", "/static/nope",
                     "/no-such-page"):
            status, headers, _ = self.head_of(path)
            self.assertEqual(headers.get("x-content-type-options"), "nosniff", path)
            self.assertEqual(headers.get("x-frame-options"), "DENY", path)
            self.assertEqual(headers.get("referrer-policy"), "same-origin", path)

    def test_server_header_hides_the_python_version(self):
        _, headers, _ = self.head_of("/health")
        self.assertNotIn("Python", headers.get("server", ""))

    def test_unknown_get_is_404_json(self):
        status, headers, body = self.head_of("/no-such-page")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})

    def test_route_exception_is_500_without_traceback(self):
        with patch.object(app, "get_runs", side_effect=RuntimeError("internal-detail-xyz")), \
                patch("sys.stderr", io.StringIO()) as log:
            status, _, body = self.head_of("/api/runs")
        self.assertIn("internal-detail-xyz", log.getvalue())  # logged server-side only
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "internal server error"})
        self.assertNotIn(b"internal-detail-xyz", body)
        self.assertNotIn(b"Traceback", body)

    def test_post_exception_is_500_without_traceback(self):
        with patch.object(app, "delete_runs", side_effect=RuntimeError("internal-detail-xyz")), \
                patch("sys.stderr", io.StringIO()):
            body = json.dumps({"runs": ["run_1"]}).encode()
            status, _, resp = raw_request(
                self.port, f"POST /api/runs/delete HTTP/1.0\r\nContent-Length: {len(body)}", body)
        self.assertEqual(status, 500)
        self.assertNotIn(b"internal-detail-xyz", resp)

    def test_handler_has_a_socket_timeout(self):
        self.assertEqual(app.TestViewerHandler.timeout, sec.HANDLER_TIMEOUT_SECS)

    def test_idle_connection_is_dropped_after_the_timeout(self):
        with patch.object(app.TestViewerHandler, "timeout", 0.5):
            with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
                sock.sendall(b"GET /health HTTP/1.1\r\n")  # never finishes the request
                start = time.monotonic()
                data = sock.recv(1024)
                self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(data, b"")

    def test_run_events_stream_still_works(self):
        # The SSE endpoint keeps the socket open; the timeout must not break it.
        (self.runs / "run_1" / "pending_tests.json").write_text(json.dumps({"tests": []}))
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(f"GET /api/run-events/run_1 HTTP/1.0\r\nCookie: {TEST_COOKIE}\r\n\r\n".encode())
            data = b""
            while b"event: run" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        self.assertIn(b"event: run", data)


class RunArgvValidationTests(ServerCase):
    def setUp(self):
        super().setUp()
        script = self.root / "fake_runner.py"
        script.write_text(FAKE_RUNNER)
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, fields, apk_name="app.apk"):
        add_build()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "submit", Path(app.__file__).parent / "ci" / "mt-submit" / "submit.py")
        submit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(submit)
        ctype, body = submit.build_multipart(
            {"device_mode": "specific", "device": "10.0.0.1:5555", "build_request": BUILD_ID,
             **fields},
            [("apk", apk_name, b"PK"), ("tests", "INSTRUCTION.txt", b"MODE: once\n"),
             ("tests", "a.txt", b"VERIFY: x\n")])
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_option_like_or_bad_values_are_refused(self):
        for fields in ({"branch": "--upload-pack=x"}, {"branch": "a b"},
                       {"ios_bundle_id": "-x"}, {"ios_bundle_id": "a;b"},
                       {"device": "--apk /etc/passwd"}, {"device": "-x"}):
            status, body = self.post(fields)
            self.assertEqual(status, 400, (fields, body))
        self.assertFalse(any(self.runs.glob("run_*/fake.json")))

    def test_valid_values_reach_the_runner_and_the_app_name_is_sanitized(self):
        status, body = self.post({"branch": "feat/x", "ios_bundle_id": "com.example.app"},
                                 apk_name="my app;rm.apk")
        self.assertEqual(status, 200, body)
        fake = self.runs / body["run_id"] / "fake.json"
        deadline = time.time() + 10
        while time.time() < deadline and not fake.exists():
            time.sleep(0.1)
        argv = json.loads(fake.read_text())["argv"]
        self.assertEqual(argv[argv.index("--branch") + 1], "feat/x")
        apk = argv[argv.index("--apk") + 1]
        self.assertEqual(Path(apk).name, "my_app_rm.apk")
        self.assertTrue(Path(apk).resolve().is_relative_to((self.runs / ".uploads").resolve()))


if __name__ == "__main__":
    unittest.main()
