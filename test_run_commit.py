"""The commit under test: submit.py reads it from the checkout, the viewer
passes it to the runner, run_metadata.json records it, the run list shows it."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from test_projects import FAKE_RUNNER, ProjectCase, core, load_submit
from build_fixture import BUILD_ID, add_build

SHA = "0123456789abcdef0123456789abcdef01234567"
MESSAGE = "Fix the login button\n\nIt was off by one pixel."


def git(cwd, *argv):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *argv], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class RunListCommitTests(ProjectCase):
    def test_runs_carry_commit_and_message_and_old_runs_have_none(self):
        self.write_meta("run_2", commit=SHA.upper(), commit_message=MESSAGE)
        self.write_meta("run_3")
        self.write_meta("run_4", commit="not-a-sha; rm -rf", commit_message="ok\x00\x1b[31m")
        runs = {r["name"]: r for r in self.get_json("/api/runs")}
        self.assertEqual((runs["run_2"]["commit"], runs["run_2"]["commit_message"]), (SHA, MESSAGE))
        self.assertEqual((runs["run_3"]["commit"], runs["run_3"]["commit_message"]), ("", ""))
        self.assertEqual(runs["run_4"]["commit"], "")
        self.assertEqual(runs["run_4"]["commit_message"], "ok[31m")


class UploadCommitTests(ProjectCase):
    def setUp(self):
        super().setUp()
        script = self.root / "fake_runner.py"
        script.write_text(FAKE_RUNNER)
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def upload(self, fields, inherited=None):
        add_build()
        ctype, body = load_submit().build_multipart(
            {"device_mode": "android", "build_request": BUILD_ID, **fields},
            [("apk", "app.apk", b"PK"), ("tests", "INSTRUCTION.txt", b"MODE: once\n"),
             ("tests", "a.txt", b"VERIFY: x\n")])
        captured = {}
        real = app.subprocess.Popen

        def spy(cmd, *a, **kw):
            captured["env"] = kw.get("env") or {}
            return real(cmd, *a, **kw)
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        with patch.object(app.subprocess, "Popen", side_effect=spy), \
                patch.dict(os.environ, inherited or {}):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.status, captured.get("env") or {}
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, {}

    def test_commit_fields_become_runner_env(self):
        status, env = self.upload({"commit": SHA, "commit_message": MESSAGE + "\r\n"})
        self.assertEqual(status, 200)
        self.assertEqual(env.get("VT_COMMIT"), SHA)
        self.assertEqual(env.get("VT_COMMIT_MESSAGE"), MESSAGE)

    def test_no_commit_is_not_inherited_from_the_viewer_env(self):
        status, env = self.upload({}, {"VT_COMMIT": SHA, "VT_COMMIT_MESSAGE": "stale"})
        self.assertEqual(status, 200)
        self.assertNotIn("VT_COMMIT", env)
        self.assertNotIn("VT_COMMIT_MESSAGE", env)

    def test_bad_commit_id_is_refused(self):
        status, _ = self.upload({"commit": "--exec=evil"})
        self.assertEqual(status, 400)


class RunnerCommitTests(unittest.TestCase):
    def test_run_metadata_records_the_commit(self):
        with patch.dict(os.environ, {"VT_COMMIT": SHA, "VT_COMMIT_MESSAGE": MESSAGE}):
            flags = core.run_flags()
        self.assertEqual((flags.get("commit"), flags.get("commit_message")), (SHA, MESSAGE))
        with patch.dict(os.environ, {"VT_COMMIT": "zzz", "VT_COMMIT_MESSAGE": " "}):
            flags = core.run_flags()
        self.assertNotIn("commit", flags)
        self.assertNotIn("commit_message", flags)


class SubmitCommitTests(unittest.TestCase):
    def test_default_commit_reads_head_of_the_checkout(self):
        submit = load_submit()
        with tempfile.TemporaryDirectory() as tmp:
            git(tmp, "init", "-q")
            Path(tmp, "f").write_text("x")
            git(tmp, "add", "f")
            git(tmp, "commit", "-q", "-m", "Fix the login button", "-m", "It was off by one pixel.")
            head = git(tmp, "rev-parse", "HEAD")
            self.assertEqual(submit.default_commit({}, cwd=tmp), (head, MESSAGE))

    def test_default_commit_outside_a_checkout_falls_back_to_github_sha(self):
        submit = load_submit()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(submit.default_commit({"GITHUB_SHA": SHA}, cwd=tmp), (SHA, ""))
            self.assertEqual(submit.default_commit({}, cwd=tmp), ("", ""))

    def test_main_uses_the_checkout_unless_flags_are_given(self):
        submit = load_submit()
        seen = {}
        with patch.object(submit, "default_commit", return_value=(SHA, MESSAGE)), \
                patch.object(submit, "run", side_effect=lambda args: seen.setdefault("args", args) and 0):
            submit.main(["--url", "http://x", "--app", "a.apk", "--platform", "android"])
            self.assertEqual((seen["args"].commit, seen["args"].commit_message), (SHA, MESSAGE))
            seen.clear()
            submit.main(["--url", "http://x", "--app", "a.apk", "--platform", "android",
                         "--commit", "abcdef1", "--commit-message", "Other"])
            self.assertEqual((seen["args"].commit, seen["args"].commit_message), ("abcdef1", "Other"))

    def test_upload_sends_the_commit_fields(self):
        submit = load_submit()
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "a.apk"
            apk.write_bytes(b"PK")
            args = submit.argparse.Namespace(branch="feat/x", repo="", url="http://x", build_request="",
                                             commit=SHA, commit_message=MESSAGE,
                                             android_build_error="", ios_build_error="",
                                             android_build_log="", ios_build_log="")
            sent = {}

            def fake_multipart(fields, files):
                sent.update(fields)
                return "multipart/form-data; boundary=x", b""
            with patch.object(submit, "build_multipart", side_effect=fake_multipart), \
                    patch.object(submit, "request_json", return_value=(200, {"run_id": "run_1"})):
                self.assertEqual(submit.submit_cases(args, {"android": apk}, [], {}), ("run_1", None))
        self.assertEqual((sent["commit"], sent["commit_message"]), (SHA, MESSAGE))


if __name__ == "__main__":
    unittest.main()
