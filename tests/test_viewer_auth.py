"""Login, accounts, the CI token, the runner token and the service tokens.

The viewer always requires a login. There is no sign-up: with no users,
admin/admin is seeded; admins manage accounts on Settings -> Users.
"""
import hashlib
import importlib.util
import json
import os
import stat
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import threading

import app
import builds
import viewer_auth as auth
from tests.build_fixture import BUILD_ID, add_build
from tests.test_app_api import (TEST_COOKIE, TEST_PASSWORD, TEST_USER, ServerCase, request)


def mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


def load_submit():
    spec = importlib.util.spec_from_file_location(
        "submit", Path(app.__file__).parent / "ci" / "mt-submit" / "submit.py")
    submit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(submit)
    return submit


ENV_RUNNER = """
import json, os, sys
argv = sys.argv[1:]
out = argv[argv.index("--output-dir") + 1]
n = 1 + max([int(d[4:]) for d in os.listdir(out) if d.startswith("run_") and d[4:].isdigit()] or [0])
run = os.path.join(out, f"run_{n}")
os.makedirs(run)
keys = [k for k in os.environ if "TOKEN" in k]
json.dump({k: os.environ[k] for k in keys}, open(os.path.join(run, "env.json"), "w"))
"""


class Controller(BaseHTTPRequestHandler):
    """A Mobile-Controller that requires X-Controller-Token: good-token."""
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Controller.seen.append(self.headers.get("X-Controller-Token"))
        ok = self.headers.get("X-Controller-Token") == "good-controller-token"
        body = json.dumps({"udids": [], "android": []} if ok else {"error": "unauthorized"}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AuthCase(ServerCase):
    def login(self, username, password, headers=None):
        return request(self.port, "POST", "/api/login", {"username": username, "password": password},
                       headers=headers, auth=False)

    def cookie_of(self, headers):
        value = headers.get("Set-Cookie", "")
        return value.split(";", 1)[0]

    def as_user(self, cookie, method, path, body=None, headers=None):
        h = {"Cookie": cookie, "Origin": f"http://127.0.0.1:{self.port}", **(headers or {})}
        return request(self.port, method, path, body, headers=h, auth=False)

    def auth_file(self):
        return json.loads(auth.AUTH_CONFIG_FILE.read_text())


class SeedAndMigrationTests(AuthCase):
    def test_no_users_seeds_admin_admin_hashed(self):
        auth.AUTH_CONFIG_FILE.unlink()
        with patch("sys.stderr"):
            warnings = auth.startup_check()
        cfg = self.auth_file()
        self.assertEqual(list(cfg["users"]), ["admin"])
        user = cfg["users"]["admin"]
        self.assertEqual(user["role"], "admin")
        self.assertTrue(user["password_hash"].startswith("scrypt$"))
        self.assertNotIn("admin", user["password_hash"].split("$")[-1])
        self.assertEqual(mode(auth.AUTH_CONFIG_FILE), 0o600)
        self.assertTrue(any("default password" in w for w in warnings))
        status, headers, _ = self.login("admin", "admin")
        self.assertEqual(status, 200)
        _, _, st = self.as_user(self.cookie_of(headers), "GET", "/api/auth/status")
        self.assertEqual((st["username"], st["role"], st["default_password"]), ("admin", "admin", True))

    def test_old_single_user_file_is_migrated_and_old_sha256_rehashed(self):
        old_hash = hashlib.sha256(b"old-password").hexdigest()
        session = "old-session"
        auth.AUTH_CONFIG_FILE.write_text(json.dumps({
            "enabled": True, "username": "yuri", "password_hash": old_hash,
            "sessions": {auth._session_key(session): 123}}))
        cfg = auth.load_auth()
        self.assertEqual(cfg["users"]["yuri"]["role"], "admin")
        self.assertNotIn("username", cfg)
        self.assertEqual(cfg["sessions"][auth._session_key(session)]["user"], "yuri")
        self.assertNotIn("admin", cfg["users"])  # not seeded: a user exists
        status, _, _ = self.login("yuri", "old-password")
        self.assertEqual(status, 200)
        new_hash = self.auth_file()["users"]["yuri"]["password_hash"]
        self.assertTrue(new_hash.startswith("scrypt$"))
        self.assertEqual(self.login("yuri", "old-password")[0], 200)

    def test_plaintext_password_is_not_accepted(self):
        auth.AUTH_CONFIG_FILE.write_text(json.dumps({"username": "yuri", "password_hash": "hunter22"}))
        self.assertEqual(self.login("yuri", "hunter22")[0], 401)

    def test_hash_format(self):
        h = auth.hash_password("pw-123456")
        self.assertNotEqual(h, auth.hash_password("pw-123456"))  # per-user salt
        self.assertEqual(auth.verify_password(h, "pw-123456"), (True, False))
        self.assertEqual(auth.verify_password(h, "nope"), (False, False))


class LoginTests(AuthCase):
    def test_login_sets_a_strict_httponly_cookie(self):
        status, headers, _ = self.login(TEST_USER, TEST_PASSWORD)
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.as_user(self.cookie_of(headers), "GET", "/api/runs")[0], 200)

    def test_wrong_password_and_unknown_user_are_401(self):
        self.assertEqual(self.login(TEST_USER, "wrong")[0], 401)
        self.assertEqual(self.login("nobody", "wrong")[0], 401)

    def test_throttle_after_five_failures(self):
        for _ in range(4):
            self.assertEqual(self.login(TEST_USER, "wrong")[0], 401)
        self.assertEqual(self.login(TEST_USER, "wrong")[0], 401)  # 5th starts the wait
        status, headers, body = self.login(TEST_USER, TEST_PASSWORD)
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)
        auth.THROTTLE.state.clear()
        self.assertEqual(self.login(TEST_USER, TEST_PASSWORD)[0], 200)

    def test_throttle_backoff_doubles(self):
        now = [100.0]
        t = auth.Throttle(clock=lambda: now[0])
        for _ in range(5):
            t.failed("1.2.3.4")
        self.assertEqual(t.wait_left("1.2.3.4"), 1)
        t.failed("1.2.3.4")
        self.assertEqual(t.wait_left("1.2.3.4"), 2)
        t.failed("1.2.3.4")
        self.assertEqual(t.wait_left("1.2.3.4"), 4)
        self.assertEqual(t.wait_left("5.6.7.8"), 0)
        t.succeeded("1.2.3.4")
        self.assertEqual(t.wait_left("1.2.3.4"), 0)

    def test_logout_ends_only_that_session(self):
        _, h1, _ = self.login(TEST_USER, TEST_PASSWORD)
        _, h2, _ = self.login(TEST_USER, TEST_PASSWORD)
        c1, c2 = self.cookie_of(h1), self.cookie_of(h2)
        self.assertEqual(self.as_user(c1, "POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.as_user(c1, "GET", "/api/runs")[0], 401)
        self.assertEqual(self.as_user(c2, "GET", "/api/runs")[0], 200)

    def test_login_page_is_public_and_redirects_when_signed_in(self):
        status, _, _ = request(self.port, "GET", "/login", auth=False)
        self.assertEqual(status, 200)
        status, headers, _ = request(self.port, "GET", "/login")
        self.assertEqual((status, headers.get("Location")), (302, "/"))

    def test_cookie_is_refreshed_by_auth_status(self):
        status, headers, _ = request(self.port, "GET", "/api/auth/status")
        self.assertIn("Max-Age=34560000", headers.get("Set-Cookie", ""))


class PerPortCookieTests(AuthCase):
    """Cookies ignore the port, so each viewer instance names its cookie
    after its port: signing in to a PR-test instance on the same host must
    not sign the user out of the live viewer."""

    def raw_set_cookies(self, method, path, headers):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=b"{}" if method == "POST" else None,
                     headers={"Origin": f"http://127.0.0.1:{self.port}",
                              "Content-Type": "application/json", **headers})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status, resp.headers.get_all("Set-Cookie") or []

    def test_login_cookie_is_named_after_the_port(self):
        _, headers, _ = self.login(TEST_USER, TEST_PASSWORD)
        self.assertTrue(self.cookie_of(headers).startswith(f"mt_auth_{self.port}="))

    def test_another_instances_cookie_does_not_hide_this_one(self):
        _, headers, _ = self.login(TEST_USER, TEST_PASSWORD)
        mine = self.cookie_of(headers)
        # The browser also holds a cookie from a viewer on another port
        # and the old shared name (e.g. from a PR-test instance).
        other = f"mt_auth_{self.port + 1}=someone-elses; mt_auth=someone-elses"
        self.assertEqual(self.as_user(f"{other}; {mine}", "GET", "/api/runs")[0], 200)
        self.assertEqual(self.as_user(f"{mine}; {other}", "GET", "/api/runs")[0], 200)

    def test_old_shared_cookie_still_works_and_moves_to_the_port_name(self):
        _, headers, _ = self.login(TEST_USER, TEST_PASSWORD)
        token = self.cookie_of(headers).split("=", 1)[1]
        status, cookies = self.raw_set_cookies("GET", "/api/auth/status",
                                               {"Cookie": f"mt_auth={token}"})
        self.assertEqual(status, 200)
        self.assertEqual([c.split(";", 1)[0] for c in cookies], [f"mt_auth_{self.port}={token}"])
        self.assertIn("Max-Age=34560000", cookies[0])

    def test_logout_clears_both_names(self):
        _, headers, _ = self.login(TEST_USER, TEST_PASSWORD)
        mine = self.cookie_of(headers)
        status, cookies = self.raw_set_cookies("POST", "/api/logout", {"Cookie": mine})
        self.assertEqual(status, 200)
        self.assertEqual(sorted(c.split(";", 1)[0] for c in cookies),
                         sorted([f"mt_auth_{self.port}=", "mt_auth="]))
        self.assertTrue(all("Max-Age=0" in c for c in cookies))
        self.assertEqual(self.as_user(mine, "GET", "/api/runs")[0], 401)


class CsrfTests(AuthCase):
    def test_cookie_write_needs_a_matching_origin(self):
        body = {"theme": "dark"}
        for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                        {"Referer": "http://evil.example/x"}):
            status, _, _ = request(self.port, "POST", "/api/config", body,
                                   headers={"Cookie": TEST_COOKIE, **headers}, auth=False)
            self.assertEqual(status, 403, headers)
        # No Origin and no Referer: refused for a cookie-authenticated write.
        status, _, _ = request(self.port, "POST", "/api/config", body,
                               headers={"Cookie": TEST_COOKIE}, auth=False)
        self.assertEqual(status, 403)
        status, _, _ = request(self.port, "POST", "/api/config", body,
                               headers={"Cookie": TEST_COOKIE,
                                        "Referer": f"http://127.0.0.1:{self.port}/config"},
                               auth=False)
        self.assertEqual(status, 200)

    def test_cross_site_login_is_refused(self):
        status, _, _ = self.login(TEST_USER, TEST_PASSWORD, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_reads_need_no_origin(self):
        status, _, _ = request(self.port, "GET", "/api/runs", headers={"Cookie": TEST_COOKIE},
                               auth=False)
        self.assertEqual(status, 200)


class AccountAndUsersTests(AuthCase):
    def make_user(self, name, role="user", password="user-password-1"):
        status, _, body = request(self.port, "POST", "/api/users",
                                  {"create": {"username": name, "password": password, "role": role}})
        self.assertEqual(status, 200, body)
        _, headers, _ = self.login(name, password)
        return self.cookie_of(headers)

    def test_admin_creates_users_and_list_has_no_hashes(self):
        self.make_user("alice")
        status, _, body = request(self.port, "GET", "/api/users")
        self.assertEqual(status, 200)
        names = {u["username"]: u for u in body["users"]}
        self.assertEqual(names["alice"]["role"], "user")
        self.assertNotIn("password_hash", json.dumps(body))
        self.assertNotIn("scrypt", json.dumps(body))
        status, _, _ = request(self.port, "POST", "/api/users",
                               {"create": {"username": "alice", "password": "x" * 8}})
        self.assertEqual(status, 409)
        status, _, _ = request(self.port, "POST", "/api/users",
                               {"create": {"username": "bob", "password": "short"}})
        self.assertEqual(status, 400)

    def test_admin_routes_are_403_for_users(self):
        cookie = self.make_user("alice")
        for method, path, body in (("GET", "/api/users", None),
                                   ("POST", "/api/users", {"delete": TEST_USER}),
                                   ("GET", "/api/config/ci-token", None),
                                   ("POST", "/api/config/ci-token", {"generate": True}),
                                   ("GET", "/api/config/service-tokens", None),
                                   ("POST", "/api/config/service-tokens", {"remove": "controller"})):
            self.assertEqual(self.as_user(cookie, method, path, body)[0], 403, path)
        self.assertEqual(self.as_user(cookie, "GET", "/users")[0], 403)
        # A user still uses everything else.
        self.assertEqual(self.as_user(cookie, "GET", "/config")[0], 200)
        self.assertEqual(self.as_user(cookie, "GET", "/api/runs")[0], 200)
        self.assertIn(TEST_USER, [u["username"] for u in auth.list_users()])

    def test_users_page_is_in_the_settings_nav_for_admins_only(self):
        self.assertIn("href: '/users'", app.SHARED_JS)
        self.assertIn("adminOnly: true", app.SHARED_JS)
        status, _, _ = request(self.port, "GET", "/users")
        self.assertEqual(status, 200)

    def test_last_admin_and_self_guards(self):
        status, _, body = request(self.port, "POST", "/api/users", {"delete": TEST_USER})
        self.assertEqual((status, "own account" in body["error"]), (400, True))
        status, _, body = request(self.port, "POST", "/api/users",
                                  {"set_role": {"username": TEST_USER, "role": "user"}})
        self.assertEqual((status, "last admin" in body["error"]), (400, True))
        other = self.make_user("root2", role="admin")
        # Another admin may not delete the last remaining... first make TEST_USER the only admin.
        self.assertEqual(request(self.port, "POST", "/api/users",
                                 {"set_role": {"username": "root2", "role": "user"}})[0], 200)
        status, _, body = self.as_user(other, "POST", "/api/users", {"delete": TEST_USER})
        self.assertEqual(status, 403)  # root2 is a user now
        auth.set_role("root2", "admin")
        auth.set_role(TEST_USER, "user")
        with self.assertRaises(auth.AuthError):
            auth.delete_user("root2", TEST_USER)  # the last admin

    def test_delete_and_reset_end_that_users_sessions(self):
        alice = self.make_user("alice")
        self.assertEqual(self.as_user(alice, "GET", "/api/runs")[0], 200)
        self.assertEqual(request(self.port, "POST", "/api/users",
                                 {"reset_password": {"username": "alice",
                                                     "password": "new-password-2"}})[0], 200)
        self.assertEqual(self.as_user(alice, "GET", "/api/runs")[0], 401)
        _, headers, _ = self.login("alice", "new-password-2")
        alice = self.cookie_of(headers)
        self.assertEqual(self.as_user(alice, "GET", "/api/runs")[0], 200)
        self.assertEqual(request(self.port, "POST", "/api/users", {"delete": "alice"})[0], 200)
        self.assertEqual(self.as_user(alice, "GET", "/api/runs")[0], 401)
        self.assertEqual(self.login("alice", "new-password-2")[0], 401)
        # The admin who did it stays signed in.
        self.assertEqual(request(self.port, "GET", "/api/runs")[0], 200)

    def test_role_change(self):
        alice = self.make_user("alice")
        self.assertEqual(self.as_user(alice, "GET", "/api/users")[0], 403)
        self.assertEqual(request(self.port, "POST", "/api/users",
                                 {"set_role": {"username": "alice", "role": "admin"}})[0], 200)
        self.assertEqual(self.as_user(alice, "GET", "/api/users")[0], 200)

    def test_own_password_change_ends_other_sessions(self):
        alice = self.make_user("alice", password="first-password")
        _, h, _ = self.login("alice", "first-password")
        alice2 = self.cookie_of(h)
        status, _, _ = self.as_user(alice, "POST", "/api/account/password",
                                    {"current_password": "wrong", "new_password": "second-password"})
        self.assertEqual(status, 403)
        status, headers, _ = self.as_user(alice, "POST", "/api/account/password",
                                          {"current_password": "first-password",
                                           "new_password": "second-password"})
        self.assertEqual(status, 200)
        fresh = self.cookie_of(headers)
        self.assertEqual(self.as_user(alice2, "GET", "/api/runs")[0], 401)
        self.assertEqual(self.as_user(fresh, "GET", "/api/runs")[0], 200)
        self.assertEqual(self.login("alice", "second-password")[0], 200)

    def test_default_password_banner_clears_after_change(self):
        auth.AUTH_CONFIG_FILE.unlink()
        with patch("sys.stderr"):
            auth.load_auth()
        _, h, _ = self.login("admin", "admin")
        cookie = self.cookie_of(h)
        self.assertTrue(self.as_user(cookie, "GET", "/api/auth/status")[2]["default_password"])
        status, h, _ = self.as_user(cookie, "POST", "/api/account/password",
                                    {"current_password": "admin", "new_password": "admin"})
        self.assertEqual(status, 400)  # not the default again
        status, h, _ = self.as_user(cookie, "POST", "/api/account/password",
                                    {"current_password": "admin", "new_password": "a-real-password"})
        self.assertEqual(status, 200)
        self.assertFalse(self.as_user(self.cookie_of(h), "GET", "/api/auth/status")[2]["default_password"])
        self.assertIn("mt-default-password-banner", app.SHARED_JS)


class CiTokenTests(AuthCase):
    def generate(self):
        status, _, body = request(self.port, "POST", "/api/config/ci-token", {"generate": True})
        self.assertEqual(status, 200)
        return body["token"]

    def test_generated_once_then_only_info(self):
        token = self.generate()
        status, _, info = request(self.port, "GET", "/api/config/ci-token")
        self.assertEqual(status, 200)
        self.assertEqual(set(info), {"configured", "last4", "saved_at"})
        self.assertEqual((info["configured"], info["last4"]), (True, token[-4:]))
        self.assertNotIn(token, json.dumps(info))
        self.assertEqual(mode(auth.SERVICE_TOKENS_FILE), 0o600)

    def test_ci_token_opens_only_the_ci_endpoints(self):
        token = self.generate()
        bearer = {"Authorization": f"Bearer {token}"}
        status, _, body = request(self.port, "GET", "/api/runs/run_1/result", headers=bearer, auth=False)
        self.assertEqual(status, 200, body)
        # The run page link carries the bare run number, not run_<n>.
        self.assertTrue(body["url"].endswith("/test.html?run=1"), body)
        for method, path in (("GET", "/api/runs"), ("GET", "/config"), ("GET", "/api/config"),
                             ("GET", "/api/config/ci-token"), ("POST", "/api/config")):
            status = request(self.port, method, path, {} if method == "POST" else None,
                             headers=bearer, auth=False)[0]
            self.assertIn(status, (302, 401), path)
        wrong = {"Authorization": "Bearer wrong"}
        self.assertEqual(request(self.port, "GET", "/api/runs/run_1/result", headers=wrong,
                                 auth=False)[0], 401)

    def upload(self, headers):
        submit = load_submit()
        ctype, body = submit.build_multipart(
            {"device_mode": "android", "build_request": BUILD_ID},
            [("apk", "app.apk", b"PK-not-really"),
             ("tests", "INSTRUCTION.txt", b"MODE: once\n"), ("tests", "a.txt", b"VERIFY: x\n")])
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype, **headers})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            data = json.loads(e.read() or b"{}")
            e.close()
            return e.code, data

    def runner(self):
        script = self.root / "env_runner.py"
        script.write_text(ENV_RUNNER)
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_upload_with_ci_token_and_without_credentials(self):
        self.runner()
        add_build()
        builds.attach_run(BUILD_ID, "run_1")  # the build already has its run
        # No cookie (the opener adds one unless we set our own).
        anon = {"Cookie": "none=1"}
        status, body = self.upload(anon)
        self.assertEqual(status, 401, body)
        token = self.generate()
        status, body = self.upload({**anon, "Authorization": f"Bearer {token}"})
        self.assertEqual(status, 200, body)
        status, body = self.upload({**anon, "Authorization": "Bearer wrong"})
        self.assertEqual(status, 401)

    def test_a_pending_build_may_upload_once_without_a_token(self):
        # The build-local plugin uploads with --build-request and no CI token.
        self.runner()
        add_build()
        status, body = self.upload({"Cookie": "none=1"})
        self.assertEqual(status, 200, body)
        status, body = self.upload({"Cookie": "none=1"})
        self.assertEqual(status, 401, body)  # its run is attached now

    def test_submit_sends_the_token_as_bearer(self):
        submit = load_submit()
        seen = {}

        def fake_urlopen(req, timeout):
            seen.update(req.headers)
            raise OSError("stop")
        with patch.dict(os.environ, {"MT_CI_TOKEN": "ci-abc"}), \
                patch.object(submit.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(OSError):
                submit.request_json("http://127.0.0.1:1/api/runs/run_1/result")
        self.assertEqual(seen.get("Authorization"), "Bearer ci-abc")

    def test_runner_gets_the_runner_and_service_tokens_not_the_old_env(self):
        self.runner()
        add_build()
        auth.set_service_token("controller", "ctl-from-settings")
        with patch.dict(os.environ, {"PLUGIN_TOKEN": "old-shared", "MT_CI_TOKEN": "old-ci",
                                     "IOS_CONTROLLER_TOKEN": "old-ios"}):
            status, body = self.upload({})
        self.assertEqual(status, 200, body)
        env_file = self.runs / body["run_id"] / "env.json"
        deadline = time.time() + 10
        while not env_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        env = json.loads(env_file.read_text())
        self.assertEqual(env.get("MT_RUNNER_TOKEN"), auth.RUNNER_TOKEN)
        self.assertEqual(env.get("CONTROLLER_TOKEN"), "ctl-from-settings")
        for old in ("PLUGIN_TOKEN", "MT_CI_TOKEN", "IOS_CONTROLLER_TOKEN"):
            self.assertNotIn(old, env)


class RunnerTokenTests(AuthCase):
    def test_press_event_takes_the_runner_token_only_there(self):
        event = {"run": "run_1", "kind": "tap", "x": 1, "y": 2}
        good = {auth.RUNNER_HEADER: auth.RUNNER_TOKEN}
        self.assertEqual(request(self.port, "POST", "/api/press-event", event, headers=good,
                                 auth=False)[0], 200)
        self.assertEqual(request(self.port, "POST", "/api/press-event", event,
                                 headers={auth.RUNNER_HEADER: "wrong"}, auth=False)[0], 401)
        self.assertEqual(request(self.port, "POST", "/api/press-event", event, auth=False)[0], 401)
        self.assertEqual(request(self.port, "POST", "/api/config", {}, headers=good, auth=False)[0], 401)
        self.assertEqual(request(self.port, "GET", "/api/runs", headers=good, auth=False)[0], 401)


class ServiceTokenTests(AuthCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ctl = ThreadingHTTPServer(("127.0.0.1", 0), Controller)
        cls.ctl_url = f"http://127.0.0.1:{cls.ctl.server_address[1]}"
        threading.Thread(target=cls.ctl.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.ctl.shutdown()
        cls.ctl.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        patcher = patch.dict(os.environ, {"CONTROLLER_URL": self.ctl_url})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_controller_token_is_validated_stored_0600_and_never_returned(self):
        status, _, body = request(self.port, "POST", "/api/config/service-tokens",
                                  {"set": {"service": "controller", "token": "bad-token"}})
        self.assertEqual(status, 400, body)
        self.assertFalse(auth.SERVICE_TOKENS_FILE.exists() and auth.controller_token())
        status, _, body = request(self.port, "POST", "/api/config/service-tokens",
                                  {"set": {"service": "controller", "token": "good-controller-token"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["controller"]["last4"], "oken")
        self.assertNotIn("good-controller-token", json.dumps(body))
        self.assertEqual(mode(auth.SERVICE_TOKENS_FILE), 0o600)
        _, _, view = request(self.port, "GET", "/api/config/service-tokens")
        self.assertNotIn("good-controller-token", json.dumps(view))
        self.assertEqual(set(view["controller"]), {"url", "configured", "last4", "saved_at"})
        # The viewer itself now sends it.
        Controller.seen.clear()
        app.get_controller_devices()
        self.assertIn("good-controller-token", Controller.seen)
        status, _, body = request(self.port, "POST", "/api/config/service-tokens", {"remove": "controller"})
        self.assertEqual((status, body["controller"]["configured"]), (200, False))

    def test_unknown_service_is_400(self):
        status, _, _ = request(self.port, "POST", "/api/config/service-tokens",
                               {"set": {"service": "plugin:nope", "token": "x" * 10}})
        self.assertEqual(status, 400)

    def test_secrets_never_in_any_get(self):
        token = request(self.port, "POST", "/api/config/ci-token", {"generate": True})[2]["token"]
        auth.set_service_token("controller", "controller-secret-1")
        auth.set_service_token("plugin:esp32", "plugin-secret-1")
        for path in ("/api/config", "/api/config/ci-token", "/api/config/service-tokens",
                     "/api/plugins", "/api/status", "/api/auth/status", "/api/users"):
            status, _, body = request(self.port, "GET", path)
            text = json.dumps(body)
            for secret in (token, "controller-secret-1", "plugin-secret-1", TEST_PASSWORD):
                self.assertNotIn(secret, text, path)


class EnvImportTests(AuthCase):
    def test_old_env_tokens_are_imported_once_then_ignored(self):
        env = {"MT_CI_TOKEN": "ci-from-env-1", "IOS_CONTROLLER_TOKEN": "ctl-from-env",
               "PLUGIN_TOKEN": "shared-env", "MT_PLUGIN_TOKEN_ESP32_HUB": "hub-env"}
        plugins = {"esp32-hub": "http://127.0.0.1:8093", "esp32": "http://127.0.0.1:8091",
                   "build-local": {"url": "http://mac:8094", "token": "build-saved"}}
        with patch("sys.stderr"):
            imported = auth.import_env_tokens(plugins, env)
        self.assertEqual(sorted(imported), ["ci", "controller", "plugin:build-local",
                                            "plugin:esp32", "plugin:esp32-hub"])
        self.assertTrue(auth.ci_token_valid("Bearer ci-from-env-1"))
        self.assertEqual(auth.controller_token(), "ctl-from-env")
        self.assertEqual(auth.plugin_token("esp32-hub"), "hub-env")
        self.assertEqual(auth.plugin_token("esp32"), "shared-env")
        self.assertEqual(auth.plugin_token("build-local"), "build-saved")
        self.assertEqual(mode(auth.SERVICE_TOKENS_FILE), 0o600)
        # Removed in Settings, then a restart with the env still set: stays removed.
        auth.remove_ci_token()
        with patch("sys.stderr"):
            self.assertEqual(auth.import_env_tokens(plugins, {"MT_CI_TOKEN": "again"}), [])
        self.assertEqual(auth.ci_token_info()["configured"], False)

    def test_environ_mirrors_settings(self):
        auth.set_service_token("controller", "ctl-x")
        auth.set_service_token("plugin:esp32", "p-x")
        environ = {"PLUGIN_TOKEN": "old", "MT_PLUGIN_TOKEN_OTHER": "old", "CONTROLLER_TOKEN": "old",
                   "MT_CI_TOKEN": "old", "PATH": "/bin"}
        auth.apply_to_environ(environ)
        self.assertEqual(environ, {"PATH": "/bin", "CONTROLLER_TOKEN": "ctl-x",
                                   "MT_PLUGIN_TOKEN_ESP32": "p-x"})

    def test_build_plugin_token_moves_out_of_config_json(self):
        app.save_config({"plugins": {"build-local": {"url": "http://mac:8094", "token": "in-config"}}})
        with patch("sys.stderr"):
            app.import_service_tokens({})
        self.assertEqual(app.load_config()["plugins"]["build-local"]["token"], "")
        self.assertEqual(auth.plugin_token("build-local"), "in-config")


if __name__ == "__main__":
    unittest.main()
