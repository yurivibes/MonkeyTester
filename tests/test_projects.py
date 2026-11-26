"""Projects: runs filed under a repo, the dashboard filter, per-project variables."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime
import urllib.request
from pathlib import Path
from unittest.mock import patch

import app
import builds
from tests.test_app_api import FAKE_RUNNER, ServerCase
from tests.build_fixture import BUILD_ID, add_build

sys.path.insert(0, str(Path(app.__file__).parent / "mt-tests" / "scripts"))
import test_runner_core as core  # noqa: E402

REPO = "blackjackfruit/TemperatureSensor-App"
OTHER = "blackjackfruit/Other-App"


def load_submit():
    spec = importlib.util.spec_from_file_location(
        "submit", Path(app.__file__).parent / "ci" / "mt-submit" / "submit.py")
    submit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(submit)
    return submit


class ProjectCase(ServerCase):
    # ServerCase keeps build_config.json (builds.CONFIG_FILE) per test.

    def write_meta(self, name, **meta):
        (self.runs / name).mkdir(exist_ok=True)
        meta.setdefault("timestamp", "2099-01-01 10:00:00")
        meta.setdefault("status", "completed")
        (self.runs / name / "run_metadata.json").write_text(json.dumps(meta))

    def post_json(self, path, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")


class RunListTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.write_meta("run_2", repo=REPO, passed=1, total=1)
        self.write_meta("run_3", repo=OTHER, failed=2, total=2)
        self.write_meta("run_4", passed=3, total=3)  # from before projects: Unassigned
        self.write_meta("run_5", repo="not a repo", passed=1, total=1)

    def names(self, query=""):
        return sorted(r["name"] for r in self.get_json("/api/runs" + query))

    def test_runs_carry_their_repo_and_legacy_runs_have_none(self):
        repos = {r["name"]: r["repo"] for r in self.get_json("/api/runs")}
        self.assertEqual(repos["run_2"], REPO)
        self.assertEqual(repos["run_3"], OTHER)
        self.assertEqual(repos["run_4"], "")
        self.assertEqual(repos["run_5"], "")  # junk in metadata is not a project

    def test_project_filter(self):
        self.assertEqual(self.names(), ["run_1", "run_2", "run_3", "run_4", "run_5"])
        self.assertEqual(self.names("?project=" + REPO), ["run_2"])
        self.assertEqual(self.names("?project=" + OTHER), ["run_3"])
        # run_1 (no metadata) and the legacy runs are Unassigned.
        self.assertEqual(self.names("?project=_unassigned"), ["run_1", "run_4", "run_5"])
        self.assertEqual(self.names("?project=nobody/none"), [])

    def test_dashboard_counts_only_the_project(self):
        def totals(query):
            data = self.get_json("/api/dashboard?range=1w" + query)["all_runs"]
            return sum(data["passed"]), sum(data["failed"])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for name in ("run_2", "run_3", "run_4", "run_5"):
            meta = json.loads((self.runs / name / "run_metadata.json").read_text())
            meta["timestamp"] = now
            (self.runs / name / "run_metadata.json").write_text(json.dumps(meta))
        self.assertEqual(totals(""), (5, 2))
        self.assertEqual(totals("&project=" + REPO), (1, 0))
        self.assertEqual(totals("&project=" + OTHER), (0, 2))
        self.assertEqual(totals("&project=_unassigned"), (4, 0))

    def test_projects_list_settings_repos_first_then_repos_seen_in_runs(self):
        builds.save_config({"plugins": {"build-local": {"url": "http://127.0.0.1:9", "token": ""}},
                            "projects": [{"repo": "blackjackfruit/Configured", "plugin": "build-local"},
                                         {"repo": REPO, "plugin": "build-local"}]})
        self.assertEqual(self.get_json("/api/projects"), {
            "projects": ["blackjackfruit/Configured", REPO, OTHER], "unassigned": True})


class ProjectVariablesConfigTests(ProjectCase):
    def config(self):
        return json.loads((self.runs / "config.json").read_text())

    def test_saving_one_project_leaves_the_others_and_the_shared_ones(self):
        self.post_json("/api/config", {"test_variables": {"hub_path": "/api"}})
        status, _ = self.post_json("/api/config", {"project_variables": {REPO: {"host": "a"}}})
        self.assertEqual(status, 200)
        self.post_json("/api/config", {"project_variables": {OTHER: {"HOST": "b"}}})
        cfg = self.config()
        self.assertEqual(cfg["test_variables"], {"HUB_PATH": "/api"})
        self.assertEqual(cfg["project_variables"], {REPO: {"HOST": "a"}, OTHER: {"HOST": "b"}})

    def test_an_empty_map_removes_the_project(self):
        self.post_json("/api/config", {"project_variables": {REPO: {"HOST": "a"}, OTHER: {"HOST": "b"}}})
        self.post_json("/api/config", {"project_variables": {REPO: {}}})
        self.assertEqual(self.config()["project_variables"], {OTHER: {"HOST": "b"}})

    def test_bad_input_is_rejected_and_nothing_saved(self):
        self.post_json("/api/config", {"project_variables": {REPO: {"HOST": "a"}}})
        for body, msg in (({"project_variables": []}, "JSON object"),
                          ({"project_variables": {"no-slash": {"A": "1"}}}, "owner/repo"),
                          ({"project_variables": {REPO: {"1BAD": "x"}}}, "bad variable name"),
                          ({"project_variables": {REPO: {"A": 1}}}, "must be a string"),
                          ({"project_variables": {REPO: "A=1"}}, "JSON object")):
            status, data = self.post_json("/api/config", body)
            self.assertEqual(status, 400, body)
            self.assertIn(msg, data["error"])
        self.assertEqual(self.config()["project_variables"], {REPO: {"HOST": "a"}})


class UploadRepoTests(ProjectCase):
    """The run-test upload passes the run's project to the runner as MT_REPO."""

    def setUp(self):
        super().setUp()
        script = self.root / "fake_runner.py"
        script.write_text(FAKE_RUNNER)
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def runner_env(self, fields, inherited=None):
        add_build()  # a build without a repo: the run takes the repo field
        ctype, body = load_submit().build_multipart(
            {"device_mode": "android", "build_request": BUILD_ID, **fields},
            [("apk", "app.apk", b"PK"), ("tests", "INSTRUCTION.txt", b"MODE: once\n"),
             ("tests", "a.txt", b"AI: VERIFY x\n")])
        captured = {}
        real = app.subprocess.Popen

        def spy(cmd, *a, **kw):
            captured["env"] = kw.get("env") or {}
            return real(cmd, *a, **kw)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        with patch.object(app.subprocess, "Popen", side_effect=spy), \
                patch.dict(os.environ, inherited or {}):
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.assertTrue(json.loads(resp.read())["run_id"])
        return captured["env"]

    def test_repo_field_becomes_mt_repo(self):
        self.assertEqual(self.runner_env({"repo": REPO}).get("MT_REPO"), REPO)

    def test_no_or_bad_repo_is_unassigned_even_if_the_viewer_env_has_one(self):
        self.assertNotIn("MT_REPO", self.runner_env({}, {"MT_REPO": OTHER}))
        self.assertNotIn("MT_REPO", self.runner_env({"repo": "../etc"}))


class RunnerTests(unittest.TestCase):
    def test_project_variables_override_shared_and_env_overrides_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(json.dumps({
                "test_variables": {"HOST": "shared", "PATH_A": "/a"},
                "project_variables": {REPO: {"HOST": "project"}, OTHER: {"HOST": "other"}}}))
            got = core.configured_variables(config_files=[cfg], env_text="", repo=REPO)
            self.assertEqual(got, {"HOST": "project", "PATH_A": "/a"})
            got = core.configured_variables(config_files=[cfg], env_text="", repo="")
            self.assertEqual(got, {"HOST": "shared", "PATH_A": "/a"})
            got = core.configured_variables(config_files=[cfg], env_text='{"HOST": "env"}', repo=REPO)
            self.assertEqual(got["HOST"], "env")
            with patch.dict(os.environ, {"MT_REPO": OTHER}):
                self.assertEqual(core.configured_variables(config_files=[cfg], env_text="")["HOST"], "other")

    def test_run_metadata_records_the_repo(self):
        with patch.dict(os.environ, {"MT_REPO": REPO}):
            self.assertEqual(core.run_flags().get("repo"), REPO)
        with patch.dict(os.environ, {"MT_REPO": "bad repo"}):
            self.assertNotIn("repo", core.run_flags())
        env = dict(os.environ)
        env.pop("MT_REPO", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertNotIn("repo", core.run_flags())


class SubmitRepoTests(unittest.TestCase):
    def test_repo_defaults_to_the_workflow_repository(self):
        submit = load_submit()
        seen = {}
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO}), \
                patch.object(submit, "run", side_effect=lambda args: seen.setdefault("args", args) and 0):
            submit.main(["--url", "http://x", "--app", "a.apk", "--platform", "android"])
            self.assertEqual(seen["args"].repo, REPO)
            seen.clear()
            submit.main(["--url", "http://x", "--app", "a.apk", "--platform", "android", "--repo", OTHER])
            self.assertEqual(seen["args"].repo, OTHER)

    def test_upload_sends_the_repo_field(self):
        submit = load_submit()
        with tempfile.TemporaryDirectory() as tmp:
            apk = Path(tmp) / "a.apk"
            apk.write_bytes(b"PK")
            args = submit.argparse.Namespace(branch="feat/x", repo=REPO, url="http://x", build_request="",
                                             android_build_error="", ios_build_error="",
                                             android_build_log="", ios_build_log="")
            sent = {}

            def fake_multipart(fields, files):
                sent.update(fields)
                return "multipart/form-data; boundary=x", b""
            with patch.object(submit, "build_multipart", side_effect=fake_multipart), \
                    patch.object(submit, "request_json", return_value=(200, {"run_id": "run_1"})):
                self.assertEqual(submit.submit_cases(args, {"android": apk}, [], {}), ("run_1", None))
        self.assertEqual(sent["repo"], REPO)


if __name__ == "__main__":
    unittest.main()
