"""Build & Test through build plugins: settings, projects, builds and migration.

A fake build plugin (fake_build_plugin.py) answers BUILD_PLUGIN_API.md; the
viewer's real HTTP server talks to it.
"""
import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

import app
import builds
from tests.fake_build_plugin import FORGE_TOKEN, REPO, TEMPLATES, FakeBuildPlugin, start
from tests.test_app_api import FAKE_RUNNER, Peer, ServerCase

OTHER = "blackjackfruit/Other-App"


def load_submit():
    spec = importlib.util.spec_from_file_location(
        "submit", Path(app.__file__).parent / "ci" / "mt-submit" / "submit.py")
    submit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(submit)
    return submit


class BuildCase(ServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plugin_server, cls.plugin_url = start()

    @classmethod
    def tearDownClass(cls):
        cls.plugin_server.shutdown()
        cls.plugin_server.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        FakeBuildPlugin.reset()
        self.config_file = self.root / "build_config.json"
        p = patch.object(builds, "REFRESH_INTERVAL", 0)
        p.start()
        self.addCleanup(p.stop)
        builds._legacy_push["at"] = 0.0
        builds.BUILDS_DIR = self.runs / ".builds"

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def register(self, name="build-local", token=None, projects=(REPO,), branch=""):
        """Write settings directly: one plugin in the shared registry at the
        fake, and projects on it."""
        builds.save_config({"projects": [{"repo": r, "plugin": name, "branch": branch}
                                         for r in projects]})
        builds.merge_registry({name: {"url": self.plugin_url, "token": token or ""}})

    def stored(self):
        return json.loads(self.config_file.read_text())

    def registry(self):
        return json.loads((self.runs / "config.json").read_text())["plugins"]


class PluginSettingsTests(BuildCase):
    def add(self, **op):
        return self.call("POST", "/api/config/plugins",
                         {"set": {"url": self.plugin_url, **op}})

    def test_add_checks_health_and_saves_privately(self):
        status, body = self.add()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["plugin"]["forge"]["label"], "Gitea")
        self.assertEqual(stat.S_IMODE((self.runs / "config.json").stat().st_mode), 0o600)
        self.assertEqual(self.registry(), {"build-local": {"url": self.plugin_url, "token": ""}})
        status, body = self.call("GET", "/api/config/build")
        self.assertEqual(status, 200)
        row = body["plugins"][0]
        self.assertTrue(row["ok"])
        self.assertEqual((row["forge"]["label"], row["forge"]["user"]), ("Gitea", "monkeytester-bot"))
        self.assertEqual(row["platforms"], ["android", "ios"])
        self.assertEqual(row["recipes"]["ios"], ".monkeytester/build-ios.sh")
        self.assertEqual(row["capabilities"], ["logs", "cancel"])
        self.assertNotIn(FORGE_TOKEN, json.dumps(body))

    def test_the_health_contract_decides_the_entry_kind(self):
        # A device contract registers the address in the SAME registry as a
        # plain url string (a Test Plugin), not as a build entry; dead and
        # malformed requests save nothing.
        Peer.health = {"ok": True, "contract": "monkeytester-plugin", "contract_version": 1,
                       "plugin": {"name": "X"}}
        status, body = self.call("POST", "/api/config/plugins",
                                 {"set": {"url": self.peer_url}})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.registry()["X"], self.peer_url)  # string, not {url, token}
        status, body = self.call("POST", "/api/config/plugins",
                                 {"set": {"url": "http://127.0.0.1:9"}})
        self.assertEqual((status, body["code"]), (400, "unreachable"))
        for bad in ({"set": {"name": "build-local", "url": self.plugin_url}}, {"set": {}},
                    {"remove": "x", "set": {}}, {"set": {"url": "u", "extra": 1}}):
            self.assertEqual(self.call("POST", "/api/config/plugins", bad)[0], 400, bad)
        self.assertFalse(self.config_file.exists())

    def test_pairing_token_is_checked_kept_and_cleared(self):
        FakeBuildPlugin.pairing = "pair-token-1234"
        status, body = self.add()
        self.assertEqual((status, body["code"]), (400, "unauthorized"))
        status, body = self.add(token="pair-token-1234")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["plugin"]["token"], {"configured": True, "last4": "1234"})
        self.assertNotIn("pair-token-1234", json.dumps(body))
        # A blank token keeps the saved one (edit the address only).
        self.assertEqual(self.add(token="")[0], 200)
        # The token lives in service_tokens.json (0600), never in config.json.
        self.assertEqual(self.registry()["build-local"]["token"], "")
        self.assertEqual(app.auth.plugin_token("build-local"), "pair-token-1234")
        status, body = self.call("GET", "/api/config/build/plugins/build-local/config")
        self.assertEqual(status, 200, body)
        FakeBuildPlugin.pairing = None
        self.assertEqual(self.add(token=None)[0], 200)
        self.assertEqual(self.registry()["build-local"]["token"], "")
        self.assertEqual(app.auth.plugin_token("build-local"), "")

    def test_add_without_a_name_uses_the_health_name(self):
        FakeBuildPlugin.name = "build-mac"
        status, body = self.add()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["plugin"]["name"], "build-mac")
        self.assertEqual(self.registry(), {"build-mac": {"url": self.plugin_url, "token": ""}})

    def test_build_plugin_health_without_a_name_is_refused(self):
        FakeBuildPlugin.name = ""
        status, body = self.add()
        self.assertEqual((status, body["code"]), (400, "no_plugin_name"))
        self.assertFalse((self.runs / "config.json").exists())

    def test_taken_build_name_at_another_address_is_refused(self):
        builds.merge_registry({"build-local": {"url": "http://127.0.0.1:9", "token": ""}})
        status, body = self.add()
        self.assertEqual((status, body["code"]), (400, "name_taken"))
        self.assertEqual(self.registry(), {"build-local": {"url": "http://127.0.0.1:9", "token": ""}})

    def test_address_edit_renames_the_plugin_and_repoints_projects(self):
        FakeBuildPlugin.pairing = "pair-token-1234"
        self.register(projects=(REPO, OTHER))
        builds.save_config({"projects": [{"repo": REPO, "plugin": "build-local", "branch": ""},
                                         {"repo": OTHER, "plugin": "elsewhere", "branch": ""}]})
        app.auth.set_service_token("plugin:build-local", "pair-token-1234")
        FakeBuildPlugin.name = "build-mac"  # the plugin now names itself differently
        status, body = self.call("POST", "/api/config/plugins",
                                 {"set": {"url": self.plugin_url}, "row": "build-local"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["renamed_from"], body["plugin"]["name"]), ("build-local", "build-mac"))
        self.assertEqual(body["plugin"]["token"]["last4"], "1234")
        self.assertEqual(self.registry(), {"build-mac": {"url": self.plugin_url, "token": ""}})
        self.assertEqual({p["repo"]: p["plugin"] for p in self.stored()["projects"]},
                         {REPO: "build-mac", OTHER: "elsewhere"})
        self.assertEqual(app.auth.plugin_token("build-mac"), "pair-token-1234")
        self.assertEqual(app.auth.plugin_token("build-local"), "")

    def test_rename_onto_another_build_row_is_name_taken(self):
        self.register()
        builds.merge_registry({"build-mac": {"url": "http://127.0.0.1:9", "token": ""}})
        FakeBuildPlugin.name = "build-mac"
        status, body = self.call("POST", "/api/config/plugins",
                                 {"set": {"url": self.plugin_url}, "row": "build-local"})
        self.assertEqual((status, body["code"]), (400, "name_taken"))
        self.assertIn("build-local", self.registry())
        self.assertEqual(self.stored()["projects"][0]["plugin"], "build-local")

    def test_remove_refuses_a_plugin_projects_use(self):
        self.register()
        status, body = self.call("POST", "/api/config/plugins", {"remove": "build-local"})
        self.assertEqual((status, body["code"]), (400, "in_use"))
        builds.save_config({**builds.load_config(), "projects": []})
        status, body = self.call("POST", "/api/config/plugins", {"remove": "build-local"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.registry(), {})
        self.assertEqual(self.call("POST", "/api/config/plugins", {"remove": "build-local"})[0], 400)

    def test_configure_panel_is_proxied_and_never_returns_the_forge_token(self):
        self.register()
        status, body = self.call("GET", "/api/config/build/plugins/build-local/config")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["token"]["last4"], "abcd")
        self.assertNotIn(FORGE_TOKEN, json.dumps(body))
        status, body = self.call("POST", "/api/config/build/plugins/build-local/config", {"token": "wrong"})
        self.assertEqual((status, body["code"], body.get("forge_status")), (400, "forge_refused", 401))
        status, body = self.call("POST", "/api/config/build/plugins/build-local/config",
                                 {"forge_url": "http://gitea2:3000", "token": FORGE_TOKEN})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["config"]["forge_url"], "http://gitea2:3000")
        self.assertNotIn(FORGE_TOKEN, json.dumps(body))
        self.assertEqual(FakeBuildPlugin.calls[-1][2]["token"], FORGE_TOKEN)
        # Code host kind (Gitea or GitHub) and the viewer URL pass through too.
        status, body = self.call("POST", "/api/config/build/plugins/build-local/config",
                                 {"forge_kind": "github", "forge_url": "https://github.com", "token": FORGE_TOKEN,
                                  "viewer_url": "http://192.168.1.10:8089"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["config"]["forge_kind"], body["config"]["viewer_url"]),
                         ("github", "http://192.168.1.10:8089"))
        self.assertEqual(self.call("GET", "/api/config/build")[1]["plugins"][0]["forge"]["label"], "GitHub")
        for bad in ({"forge_kind": "svn"}, {"viewer_url": None}, {"forge_url": 5}):
            self.assertEqual(self.call("POST", "/api/config/build/plugins/build-local/config", bad)[0], 400, bad)
        status, body = self.call("POST", "/api/config/build/plugins/build-local/config", {"token": None})
        self.assertFalse(body["config"]["token"]["configured"])
        self.assertEqual(self.call("POST", "/api/config/build/plugins/build-local/config", {"x": 1})[0], 400)
        self.assertEqual(self.call("GET", "/api/config/build/plugins/nope/config")[0], 400)
        # The forge token never lands in the viewer's own settings.
        self.assertNotIn(FORGE_TOKEN, self.config_file.read_text())


class ProjectTests(BuildCase):
    def setUp(self):
        super().setUp()
        self.register(projects=())

    def save(self, projects):
        return self.call("POST", "/api/config/build", {"projects": projects})

    def test_projects_are_checked_and_saved_with_their_plugin_and_branch(self):
        status, body = self.save([{"repo": REPO, "plugin": "build-local", "branch": "develop"}])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["checks"][0]["branch"], "develop")
        self.assertTrue(body["checks"][0]["ok"])
        self.assertEqual(body["checks"][0]["forge_label"], "Gitea")
        self.assertEqual(self.stored()["projects"], [{"repo": REPO, "plugin": "build-local", "branch": "develop"}])
        check_call = [c for c in FakeBuildPlugin.calls if c[1] == f"/repos/{REPO}"][-1]
        self.assertEqual(check_call[2]["branch"], ["develop"])
        status, body = self.call("GET", "/api/config/build")
        self.assertEqual(body["projects"][0]["plugin"], "build-local")
        self.assertTrue(body["checks"][0]["ok"])

    def test_missing_script_is_reported_but_does_not_block_saving(self):
        FakeBuildPlugin.repos[REPO]["recipes"]["master"] = {"ios": False}
        status, body = self.save([{"repo": REPO, "plugin": "build-local"}])
        self.assertEqual(status, 200, body)
        check = body["checks"][0]
        self.assertEqual(check["recipes"], {"android": True, "ios": False})
        self.assertEqual(check["recipe_paths"]["ios"], ".monkeytester/build-ios.sh")
        self.assertEqual(check["platforms"], ["android", "ios"])

    def test_a_failing_check_stores_nothing(self):
        cases = (([{"repo": REPO, "plugin": "build-local", "branch": "gone"}], "branch gone"),
                 ([{"repo": "someone/else", "plugin": "build-local"}], "not found"))
        for projects, msg in cases:
            status, body = self.save(projects)
            self.assertEqual(status, 400, projects)
            self.assertIn(msg, body["error"])
            self.assertFalse(body["checks"][0]["ok"])
        FakeBuildPlugin.repos[REPO]["readable"] = False
        status, body = self.save([{"repo": REPO, "plugin": "build-local"}])
        self.assertEqual(status, 400)
        self.assertIn("can't read", body["error"])
        self.assertEqual(self.stored()["projects"], [])

    def test_invalid_projects_are_refused(self):
        for projects, msg in (([{"repo": REPO, "plugin": "build-local"}, {"repo": REPO, "plugin": "build-local"}],
                               "twice"),
                              ([{"repo": REPO, "plugin": "nope"}], "not registered"),
                              ([{"repo": "../etc", "plugin": "build-local"}], "owner/repo"),
                              ([{"repo": REPO, "plugin": "build-local", "branch": "a..b"}], "branch"),
                              ("x", "list")):
            status, body = self.save(projects)
            self.assertEqual(status, 400, projects)
            self.assertIn(msg, body["error"])

    def test_branch_picker_lists_a_repo_not_saved_yet(self):
        status, body = self.call("GET", f"/api/config/build/branches?repo={urllib.parse.quote(REPO)}&plugin=build-local")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["default"], "master")
        self.assertEqual(len(body["branches"]), 63)
        self.assertEqual(self.call("GET", "/api/config/build/branches?repo=../etc&plugin=build-local")[0], 400)
        self.assertEqual(self.call("GET", f"/api/config/build/branches?repo={REPO}")[0], 400)

    def test_projects_feed_build_and_test_and_the_dashboard(self):
        self.save([{"repo": REPO, "plugin": "build-local"}])
        status, body = self.call("GET", "/api/builds/repos")
        self.assertEqual(body, {"configured": True, "repos": [REPO],
                                "projects": [{"repo": REPO, "plugin": "build-local", "branch": ""}]})
        self.assertEqual(self.get_json("/api/projects")["projects"][0], REPO)


class BranchAndCaseTests(BuildCase):
    def test_branches_with_the_project_branch_first(self):
        self.register(branch="develop")
        status, body = self.call("GET", f"/api/builds/branches?repo={urllib.parse.quote(REPO)}")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["preferred"], body["default"]), ("develop", "master"))
        self.assertEqual(body["branches"][:2], ["develop", "master"])

    def test_cases_recipes_and_platforms(self):
        self.register()
        status, body = self.call("GET", f"/api/builds/cases?repo={REPO}&ref=feat%2Fx")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["cases"], ["eula", "ios-eula"])
        self.assertEqual(body["recipes"], {"android": True, "ios": True})
        self.assertEqual(body["recipe_paths"], {"android": ".monkeytester/build-android.sh",
                                                "ios": ".monkeytester/build-ios.sh"})
        self.assertEqual(body["platforms"], ["android", "ios"])
        self.assertEqual((body["plugin"], body["forge_label"]), ("build-local", "Gitea"))
        FakeBuildPlugin.repos[REPO]["recipes"]["feat/x"] = {"ios": False}
        FakeBuildPlugin.platforms = ["android"]
        body = self.call("GET", f"/api/builds/cases?repo={REPO}&ref=feat%2Fx")[1]
        self.assertEqual((body["recipes"]["ios"], body["platforms"]), (False, ["android"]))

    def test_errors(self):
        self.register()
        self.assertEqual(self.call("GET", f"/api/builds/cases?repo={REPO}&ref=gone")[0], 404)
        self.assertEqual(self.call("GET", f"/api/builds/cases?repo={REPO}&ref=a..b")[0], 400)
        self.assertEqual(self.call("GET", "/api/builds/branches?repo=someone/other")[0], 400)
        FakeBuildPlugin.forge["token"] = ""
        status, body = self.call("GET", f"/api/builds/branches?repo={REPO}")
        self.assertEqual((status, body["code"]), (409, "not_configured"))
        builds.merge_registry({"build-local": {"url": "http://127.0.0.1:9", "token": ""}})
        self.assertEqual(self.call("GET", f"/api/builds/branches?repo={REPO}")[0], 502)


class BuildTests(BuildCase):
    def setUp(self):
        super().setUp()
        self.register()

    def start(self, **over):
        body = {"repo": REPO, "branch": "feat/x", "platforms": ["ios", "android"], "cases": ["eula"]}
        body.update(over)
        return self.call("POST", "/api/builds", body)

    def build(self, build_id):
        status, body = self.call("GET", f"/api/builds/{build_id}")
        self.assertEqual(status, 200, body)
        return body

    def set_ci(self, build, **status):
        ref = builds.load_build(build["id"])["build_ref"]
        FakeBuildPlugin.builds[ref] = {"state": "pending", "conclusion": None, "url": "http://forge/run/1",
                                       "jobs": [], **status}

    def test_start_sends_branch_platforms_cases_and_request_id(self):
        status, body = self.start()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "queued")
        self.assertTrue(body["build_ref"].startswith("br_"))
        self.assertEqual((body["plugin"], body["forge_label"]), ("build-local", "Gitea"))
        self.assertEqual(body["capabilities"], ["logs", "cancel"])
        self.assertEqual(FakeBuildPlugin.started[-1], {
            "repo": REPO, "branch": "feat/x", "platforms": ["android", "ios"], "cases": ["eula"],
            "request_id": body["id"]})

    def test_a_build_left_pending_is_given_up_after_the_timeout(self):
        _, b = self.start()
        self.assertEqual(self.build(b["id"])["state"], "queued")
        record = builds.load_build(b["id"])
        record["created"] -= builds.PENDING_TIMEOUT + 1
        builds.save_build(record)
        # Queued behind another build: no timeout.
        self.assertEqual(self.build(b["id"])["state"], "queued")
        self.set_ci(b, state="pending")
        done = self.build(b["id"])
        self.assertEqual(done["state"], "dispatch_failed")
        self.assertIn("never queued it", done["error"])

    def test_plugin_states_map_to_viewer_states(self):
        _, b = self.start()
        for ci, want in (({"state": "pending"}, "queued"), ({"state": "queued"}, "queued"),
                         ({"state": "in_progress"}, "building"),
                         ({"state": "completed", "conclusion": "failure"}, "build_failed")):
            self.set_ci(b, **ci)
            record = builds.load_build(b["id"])
            record["state"] = "queued"
            builds.save_build(record)
            self.assertEqual(self.build(b["id"])["state"], want, ci)

    def test_a_platform_the_host_cannot_build_is_refused(self):
        FakeBuildPlugin.platforms = ["android"]
        status, body = self.start(platforms=["android", "ios"])
        self.assertEqual((status, body["code"]), (400, "bad_request"))
        self.assertIn("can't build ios", body["error"])
        self.assertEqual(FakeBuildPlugin.started, [])
        self.assertEqual(self.start(platforms=["android"])[1]["state"], "queued")

    def test_refused_start_is_recorded_with_the_reason(self):
        FakeBuildPlugin.start_status = (409, "not_configured", "no viewer URL saved")
        status, body = self.start()
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "dispatch_failed")
        self.assertIn("build-local refused the build: no viewer URL saved", body["error"])
        FakeBuildPlugin.start_status = None
        FakeBuildPlugin.repos[REPO]["recipes"]["feat/x"] = {"ios": False}
        _, body = self.start()
        self.assertEqual(body["state"], "dispatch_failed")
        self.assertIn(".monkeytester/build-ios.sh not found on feat/x", body["error"])

    def test_an_unreachable_plugin_is_a_failed_start(self):
        builds.merge_registry({"build-local": {"url": "http://127.0.0.1:9", "token": ""}})
        status, body = self.start()
        self.assertEqual((status, body["state"]), (200, "dispatch_failed"))
        self.assertIn("unreachable", body["error"])

    def test_bad_requests(self):
        for over, msg in (({"platforms": []}, "platform"), ({"cases": []}, "test case"),
                          ({"branch": "a..b"}, "branch"), ({"repo": "someone/other"}, "not a project"),
                          ({"cases": ["../x"]}, "invalid case")):
            status, body = self.start(**over)
            self.assertEqual(status, 400, over)
            self.assertIn(msg, body["error"])

    def test_lifecycle_building_testing_passed(self):
        _, b = self.start(platforms=["android"])
        self.set_ci(b, state="in_progress", jobs=[{"name": "Android", "status": "in_progress",
                                                   "conclusion": None, "url": "http://forge/job/1"}])
        self.assertEqual(self.build(b["id"])["state"], "building")
        (self.runs / "run_5").mkdir()
        self.assertTrue(builds.attach_run(b["id"], "run_5", "eula", ["android"]))
        self.assertEqual(self.build(b["id"])["state"], "testing")
        self.set_ci(b, state="completed", conclusion="success",
                    jobs=[{"name": "Android", "status": "completed", "conclusion": "success",
                           "url": "http://forge/job/1"}])
        with patch.object(app, "get_run_result", return_value={"status": "passed"}):
            done = self.build(b["id"])
        self.assertEqual(done["state"], "passed")
        self.assertEqual((done["ci_state"], done["ci_conclusion"]), ("completed", "success"))
        self.assertEqual(done["jobs"]["android"]["url"], "http://forge/job/1")
        self.assertNotIn(".builds", [r["name"] for r in app.get_runs()])

    def test_one_platform_failing_still_tests_the_other(self):
        _, b = self.start()
        self.set_ci(b, state="completed", conclusion="success",
                    jobs=[{"name": "Android", "status": "completed", "conclusion": "success"},
                          {"name": "iOS", "status": "completed", "conclusion": "failure"},
                          {"name": "Upload", "status": "completed", "conclusion": "success"}])
        builds.attach_run(b["id"], "run_8", "eula", ["android", "ios"])
        with patch.object(app, "get_run_result", return_value={"status": "running"}):
            self.assertEqual(self.build(b["id"])["state"], "testing")
        with patch.object(app, "get_run_result", return_value={"status": "failed"}):
            done = self.build(b["id"])
        self.assertEqual(done["state"], "failed")
        self.assertIn("build failed for ios", done["error"])
        self.assertEqual(done["jobs"]["upload"]["conclusion"], "success")
        self.assertEqual(done["jobs"]["android"]["name"], "Android")

    def test_every_platform_failing_is_build_failed(self):
        _, b = self.start()
        self.set_ci(b, state="completed", conclusion="failure",
                    jobs=[{"name": "Android", "status": "completed", "conclusion": "failure"},
                          {"name": "iOS", "status": "completed", "conclusion": "failure"},
                          {"name": "Upload", "status": "completed", "conclusion": "success"}])
        # The plugin uploads the errors anyway (devices show Unavailable).
        builds.attach_run(b["id"], "run_9", "eula", ["android", "ios"])
        with patch.object(app, "get_run_result", return_value={"status": "failed"}):
            done = self.build(b["id"])
        self.assertEqual(done["state"], "build_failed")
        self.assertIn("android, ios", done["error"])
        _, c = self.start()
        self.set_ci(c, state="completed", conclusion="failure",
                    jobs=[{"name": "Android", "status": "completed", "conclusion": "failure"}])
        done = self.build(c["id"])
        self.assertEqual(done["state"], "build_failed")
        self.assertIn("see its log", done["error"])

    def test_test_failure_is_failed_and_cancel_is_cancelled(self):
        _, b = self.start(platforms=["ios"])
        self.set_ci(b, state="completed", conclusion="success",
                    jobs=[{"name": "iOS", "status": "completed", "conclusion": "success"}])
        builds.attach_run(b["id"], "run_7", "eula", ["ios"])
        with patch.object(app, "get_run_result", return_value={"status": "failed"}):
            self.assertEqual(self.build(b["id"])["state"], "failed")
        _, c = self.start()
        self.set_ci(c, state="completed", conclusion="cancelled")
        done = self.build(c["id"])
        self.assertEqual(done["state"], "cancelled")
        self.assertIn("cancelled", done["error"])

    def test_platform_jobs_win_over_a_shared_job(self):
        jobs = builds.map_jobs([{"name": "Setup", "status": "completed"},
                                {"name": "Android build", "status": "in_progress"}], ["android", "ios"])
        self.assertEqual(jobs["android"]["name"], "Android build")
        self.assertEqual(jobs["ios"]["name"], "Setup")
        self.assertEqual(builds.map_jobs([], ["android"]), {})
        # Upload never stands in for a platform.
        jobs = builds.map_jobs([{"name": "Upload", "status": "queued"}], ["android"])
        self.assertEqual(list(jobs), ["upload"])

    def test_a_ref_the_plugin_forgot_ends_the_build(self):
        _, b = self.start()
        FakeBuildPlugin.builds.clear()
        done = self.build(b["id"])
        self.assertEqual(done["state"], "dispatch_failed")
        self.assertIn("no longer knows", done["error"])

    def test_list_is_newest_first(self):
        _, first = self.start()
        _, second = self.start(branch="master")
        status, body = self.call("GET", "/api/builds")
        self.assertEqual(status, 200)
        self.assertEqual([b["id"] for b in body["builds"]], [second["id"], first["id"]])

    def log(self, build_id, offset=None):
        url = f"http://127.0.0.1:{self.port}/api/builds/{build_id}/log"
        if offset is not None:
            url += f"?offset={offset}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), e.headers

    def test_log_is_proxied_for_tailing(self):
        _, b = self.start()
        ref = builds.load_build(b["id"])["build_ref"]
        FakeBuildPlugin.logs[ref] = b"--- fetch\n--- android: bash .monkeytester/build-android.sh\n"
        status, text, headers = self.log(b["id"])
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertEqual(text, FakeBuildPlugin.logs[ref])
        self.assertEqual((headers["X-Log-Size"], headers["X-Log-Complete"]), (str(len(text)), "0"))
        status, text, headers = self.log(b["id"], offset=10)
        self.assertEqual(text, FakeBuildPlugin.logs[ref][10:])
        self.assertEqual(FakeBuildPlugin.calls[-1][2]["offset"], ["10"])
        FakeBuildPlugin.builds[ref]["state"] = "completed"
        self.assertEqual(self.log(b["id"], offset=len(FakeBuildPlugin.logs[ref]))[2]["X-Log-Complete"], "1")
        self.assertEqual(self.log(b["id"], offset="x")[0], 400)
        self.assertEqual(self.log("build_000000000000")[0], 404)
        # A build that never reached the plugin has no log.
        FakeBuildPlugin.start_status = (409, "not_configured", "no token")
        _, failed = self.start()
        self.assertEqual(self.log(failed["id"])[0], 409)

    def test_cancel_reaches_the_plugin(self):
        _, b = self.start()
        status, body = self.call("POST", f"/api/builds/{b['id']}/cancel")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "cancelled")
        self.assertEqual(FakeBuildPlugin.cancelled, [builds.load_build(b["id"])["build_ref"]])
        self.assertEqual(builds.load_build(b["id"])["state"], "cancelled")
        # A finished build is left as it is and the plugin isn't asked again.
        self.assertEqual(self.call("POST", f"/api/builds/{b['id']}/cancel")[1]["state"], "cancelled")
        self.assertEqual(len(FakeBuildPlugin.cancelled), 1)
        self.assertEqual(self.call("POST", "/api/builds/build_000000000000/cancel")[0], 404)

    def test_unknown_build_ids(self):
        self.assertEqual(self.call("GET", "/api/builds/build_000000000000")[0], 404)
        self.assertEqual(self.call("GET", "/api/builds/../../etc")[0], 400)
        self.assertFalse(builds.attach_run("nope", "run_1"))


class LegacyRecordTests(BuildCase):
    """Build records from the built-in Gitea client still load and display."""

    def test_old_record_reads_the_gitea_fields(self):
        builds.save_build({
            "id": "build_aaaaaaaaaaaa", "repo": REPO, "branch": "master", "platforms": ["android"],
            "cases": ["eula"], "user": "", "created": 1, "state": "passed", "error": "",
            "gitea_run_id": 77, "gitea_run_url": "http://gitea/run/77", "gitea_status": "completed",
            "gitea_conclusion": "success", "jobs": {}, "runs": [], "checked": 1})
        body = self.call("GET", "/api/builds/build_aaaaaaaaaaaa")[1]
        self.assertEqual(body["state"], "passed")
        self.assertEqual((body["ci_url"], body["ci_state"], body["ci_conclusion"]),
                         ("http://gitea/run/77", "completed", "success"))
        self.assertEqual((body["plugin"], body["forge_label"]), ("", "Gitea"))
        self.assertEqual(body["gitea_run_id"], 77)  # the old keys stay
        listed = self.call("GET", "/api/builds")[1]["builds"]
        self.assertEqual(listed[0]["ci_url"], "http://gitea/run/77")

    def test_old_in_flight_record_follows_its_runs(self):
        builds.save_build({
            "id": "build_bbbbbbbbbbbb", "repo": REPO, "branch": "master", "platforms": ["android"],
            "cases": ["eula"], "created": 1, "state": "queued", "error": "", "gitea_run_id": 5,
            "gitea_run_url": "", "gitea_status": "in_progress", "gitea_conclusion": "",
            "jobs": {}, "runs": [], "checked": 0})
        builds.attach_run("build_bbbbbbbbbbbb", "run_1", "eula", ["android"])
        with patch.object(app, "get_run_result", return_value={"status": "running"}):
            self.assertEqual(self.call("GET", "/api/builds/build_bbbbbbbbbbbb")[1]["state"], "testing")


class MigrationTests(BuildCase):
    def write_legacy(self, token="old-token-1234"):
        legacy = self.root / "gitea_config.json"
        legacy.write_text(json.dumps({"url": "http://gitea:3000", "token": token,
                                      "user": "bot", "repos": [REPO, OTHER, "../bad"],
                                      "branches": {OTHER: "develop"}}))
        return legacy

    def test_gitea_config_becomes_the_build_local_plugin_and_projects(self):
        legacy = self.write_legacy()
        before = legacy.read_text()
        with patch.dict(os.environ, {"MT_BUILD_LOCAL_URL": ""}):
            cfg = builds.load_config()  # first load migrates
        self.assertEqual(cfg["plugins"], {"build-local": {"url": "http://127.0.0.1:8094", "token": ""}})
        self.assertEqual(cfg["projects"], [{"repo": REPO, "plugin": "build-local", "branch": ""},
                                           {"repo": OTHER, "plugin": "build-local", "branch": "develop"}])
        self.assertEqual(stat.S_IMODE(self.config_file.stat().st_mode), 0o600)
        self.assertNotIn("old-token-1234", self.config_file.read_text())
        self.assertEqual(legacy.read_text(), before)  # kept until the plugin has the token
        # Once only: later edits to the old file change nothing.
        legacy.write_text(json.dumps({"repos": ["x/y"]}))
        self.assertIsNone(builds.migrate())
        self.assertEqual(len(builds.load_config()["projects"]), 2)

    def test_mt_build_local_url_sets_the_address(self):
        self.write_legacy()
        cfg = builds.migrate({"MT_BUILD_LOCAL_URL": "http://192.168.1.20:8094/"})
        self.assertEqual(cfg["plugins"]["build-local"]["url"], "http://192.168.1.20:8094")

    def test_token_is_handed_to_the_unconfigured_plugin_once(self):
        legacy = self.write_legacy(token=FORGE_TOKEN)
        FakeBuildPlugin.forge = {"forge_kind": "", "forge_url": "", "token": "", "user": "", "viewer_url": ""}
        with patch.dict(os.environ, {"MT_BUILD_LOCAL_URL": self.plugin_url,
                                     "MT_PUBLIC_URL": "http://192.168.1.10:8089/"}):
            status, body = self.call("GET", "/api/config/build")  # first load migrates and hands over
        self.assertEqual(status, 200, body)
        posts = [c for c in FakeBuildPlugin.calls if c[:2] == ("POST", "/config")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], {"forge_kind": "gitea", "forge_url": "http://gitea:3000",
                                       "token": FORGE_TOKEN, "viewer_url": "http://192.168.1.10:8089"})
        self.assertTrue(body["plugins"][0]["forge"]["configured"])
        self.assertEqual([p["repo"] for p in body["projects"]], [REPO, OTHER])
        # The viewer no longer holds the token.
        self.assertFalse(legacy.exists())
        migrated = self.root / "gitea_config.json.migrated"
        self.assertNotIn("token", json.loads(migrated.read_text()))
        self.assertEqual(stat.S_IMODE(migrated.stat().st_mode), 0o600)
        self.assertNotIn(FORGE_TOKEN, self.config_file.read_text())
        builds._legacy_push["at"] = 0.0
        self.call("GET", "/api/config/build")
        self.assertEqual(len([c for c in FakeBuildPlugin.calls if c[:2] == ("POST", "/config")]), 1)

    def test_viewer_url_defaults_to_this_host(self):
        with patch.object(builds, "VIEWER_PORT", 8123):
            self.assertEqual(builds.viewer_url_for("http://127.0.0.1:8094", env={}), "http://127.0.0.1:8123")
            self.assertEqual(builds.viewer_url_for("http://x", env={"MT_PUBLIC_URL": "http://v:1/"}), "http://v:1")

    def test_a_refused_token_is_kept_and_retried_later(self):
        legacy = self.write_legacy(token="wrong-token-0000")
        FakeBuildPlugin.forge = {"forge_kind": "", "forge_url": "", "token": "", "user": "", "viewer_url": ""}
        with patch.dict(os.environ, {"MT_BUILD_LOCAL_URL": self.plugin_url}):
            self.call("GET", "/api/config/build")
            self.call("GET", "/api/config/build")  # within LEGACY_PUSH_RETRY: not tried again
        self.assertEqual(len([c for c in FakeBuildPlugin.calls if c[:2] == ("POST", "/config")]), 1)
        self.assertTrue(legacy.exists())
        self.assertIn("wrong-token-0000", legacy.read_text())

    def test_a_configured_plugin_keeps_its_token_and_the_file_is_retired(self):
        legacy = self.write_legacy()
        with patch.dict(os.environ, {"MT_BUILD_LOCAL_URL": self.plugin_url}):
            self.call("GET", "/api/config/build")
        self.assertEqual([c for c in FakeBuildPlugin.calls if c[:2] == ("POST", "/config")], [])
        self.assertEqual(FakeBuildPlugin.forge["token"], FORGE_TOKEN)
        self.assertFalse(legacy.exists())
        self.assertTrue((self.root / "gitea_config.json.migrated").exists())

    def test_settings_page_migrates_on_first_load(self):
        self.write_legacy()
        status, body = self.call("GET", "/api/config/build")
        self.assertEqual(status, 200, body)
        self.assertEqual([p["repo"] for p in body["projects"]], [REPO, OTHER])
        self.assertEqual(body["plugins"][0]["name"], "build-local")

    def test_mt_build_plugins_is_imported_once(self):
        env = {"MT_BUILD_PLUGINS": "mac2=http://127.0.0.1:8095/"}
        cfg = builds.migrate(env)
        self.assertEqual(cfg["plugins"], {"mac2": {"url": "http://127.0.0.1:8095", "token": ""}})
        self.assertIsNone(builds.migrate({"MT_BUILD_PLUGINS": "other=http://h:1"}, log_ignored=True))
        self.assertEqual(list(builds.load_config()["plugins"]), ["mac2"])

    def test_nothing_to_import_writes_nothing(self):
        self.assertEqual(builds.load_config(), {"plugins": {}, "projects": []})
        self.assertIsNone(builds.migrate({"MT_BUILD_PLUGINS": "not a list"}))
        self.assertFalse(self.config_file.exists())


class AliasTests(BuildCase):
    """The deprecated /api/config/gitea* routes act on the new settings."""

    def test_get_alias_has_the_old_fields(self):
        self.register(branch="develop")
        status, body = self.call("GET", "/api/config/gitea")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["repos"], [REPO])
        self.assertEqual(body["branches"], {REPO: "develop"})
        self.assertTrue(body["configured"])
        self.assertEqual(body["user"], "monkeytester-bot")
        self.assertEqual(body["checks"][0]["branch"], "develop")
        status, body = self.call("GET", f"/api/config/gitea/branches?repo={urllib.parse.quote(REPO)}")
        self.assertEqual((status, body["default"]), (200, "master"))

    def test_post_alias_forwards_the_token_and_saves_projects(self):
        self.register(projects=())
        FakeBuildPlugin.forge["token"] = ""
        status, body = self.call("POST", "/api/config/gitea", {"url": "http://gitea:3000", "token": FORGE_TOKEN,
                                                               "repos": [REPO], "branches": {REPO: "develop"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(FakeBuildPlugin.forge["token"], FORGE_TOKEN)
        self.assertEqual(self.stored()["projects"], [{"repo": REPO, "plugin": "build-local", "branch": "develop"}])
        status, body = self.call("POST", "/api/config/gitea", {"token": "wrong", "repos": [REPO]})
        self.assertEqual((status, body["code"]), (400, "forge_refused"))
        status, body = self.call("POST", "/api/config/gitea/remove-token")
        self.assertEqual(status, 200, body)
        self.assertEqual(FakeBuildPlugin.forge["token"], "")

    def test_workflow_template_alias_redirects_to_the_plugin_template(self):
        self.register()
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/builds/workflow-template",
                                    timeout=10) as resp:
            self.assertTrue(resp.url.endswith("/api/config/build/template?plugin=build-local&platform=android"))
            self.assertEqual(resp.read().decode(), TEMPLATES["android"])


class TemplateTests(BuildCase):
    def test_template_comes_from_the_plugin_per_platform(self):
        self.register()
        for platform in ("android", "ios"):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/config/build/template"
                                        f"?plugin=build-local&platform={platform}", timeout=10) as resp:
                self.assertIn(f'filename="build-{platform}.sh"', resp.headers["Content-Disposition"])
                self.assertTrue(resp.headers["Content-Type"].startswith("text/plain"))
                self.assertEqual(resp.read().decode(), TEMPLATES[platform])
        self.assertEqual(self.call("GET", "/api/config/build/template?plugin=nope&platform=ios")[0], 400)
        self.assertEqual(self.call("GET", "/api/config/build/template?plugin=build-local")[0], 400)


class SubmitScriptTests(ServerCase):
    def test_submit_py_is_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/ci/submit.py", timeout=10) as resp:
            self.assertTrue(resp.headers["Content-Type"].startswith("text/x-python"))
            body = resp.read()
        self.assertEqual(body, (Path(app.__file__).parent / "ci" / "mt-submit" / "submit.py").read_bytes())

    def test_branch_defaults_from_the_ci(self):
        submit = load_submit()
        self.assertEqual(submit.default_branch({"GITHUB_REF_NAME": "main"}), "main")
        self.assertEqual(submit.default_branch({}), "")
        seen = {}
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": REPO, "GITHUB_REF_NAME": "feat/y"}), \
                patch.object(submit, "run", side_effect=lambda args: seen.setdefault("args", args) and 0):
            submit.main(["--url", "http://x", "--app", "a.apk", "--platform", "android"])
            self.assertEqual((seen["args"].repo, seen["args"].branch), (REPO, "feat/y"))
            seen.clear()
            submit.main(["--url", "http://x", "--app", "a.apk", "--platform", "android", "--branch", "b"])
        self.assertEqual(seen["args"].branch, "b")


class UploadAttachTests(BuildCase):
    """A CI upload carrying build_request attaches its run to the build."""

    def setUp(self):
        super().setUp()
        self.register()
        script = self.root / "fake_runner.py"
        script.write_text(FAKE_RUNNER)
        for patcher in (patch.object(app, "RUN_ON_DEVICE_SCRIPT", script),
                        patch.object(app, "UPLOADS_DIR", self.runs / ".uploads"),
                        patch.dict(app._PREVIOUS_RUN, {"proc": None, "run_id": None})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def upload(self, fields):
        ctype, body = load_submit().build_multipart(
            {"device_mode": "android", "case": "eula", **fields},
            [("apk", "app.apk", b"PK"), ("tests", "INSTRUCTION.txt", b"MODE: once\n"),
             ("tests", "a.txt", b"AI: VERIFY x\n")])
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run-test", data=body,
                                     method="POST", headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["run_id"]

    def new_build(self):
        return self.call("POST", "/api/builds", {"repo": REPO, "branch": "feat/x",
                                                 "platforms": ["android"], "cases": ["eula"]})[1]

    def test_upload_attaches_the_run(self):
        b = self.new_build()
        run_id = self.upload({"build_request": b["id"], "branch": "feat/x"})
        stored = builds.load_build(b["id"])
        self.assertEqual(stored["runs"][0]["run_id"], run_id)
        self.assertEqual(stored["runs"][0]["platforms"], ["android"])
        self.assertEqual(stored["state"], "testing")

    def test_upload_without_a_known_build_is_refused(self):
        for fields in ({}, {"build_request": "build_000000000000"}, {"build_request": "../x"}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.upload(fields)
            self.assertEqual(cm.exception.code, 400, fields)
            cm.exception.close()

    def submit(self, *extra):
        """Run submit.py's main() against the viewer with a one-case tests dir."""
        tests = self.root / "mt-tests" / "eula"
        tests.mkdir(parents=True, exist_ok=True)
        (tests / "INSTRUCTION.txt").write_text("MODE: once\n")
        (tests / "01_a.txt").write_text("AI: VERIFY x\n")
        apk = self.root / "app.apk"
        apk.write_bytes(b"PK")
        out = io.StringIO()
        submit = load_submit()
        with contextlib.redirect_stdout(out), \
                patch.object(submit, "wait_for_result", side_effect=AssertionError("waited")):
            code = submit.main(["--url", f"http://127.0.0.1:{self.port}", "--android-app", str(apk),
                                "--tests-dir", str(self.root / "mt-tests"), "--branch", "feat/x",
                                "--repo", REPO, "--cases", "eula", "--no-wait", *extra])
        return code, out.getvalue()

    def test_submit_no_wait_exits_once_the_upload_is_accepted(self):
        b = self.new_build()
        code, out = self.submit("--build-request", b["id"])
        self.assertEqual(code, 0, out)
        run_id = builds.load_build(b["id"])["runs"][0]["run_id"]
        self.assertIn(f"--- {run_id} started\n", out)
        self.assertNotIn("waiting for result", out)

    def test_submit_no_wait_fails_when_the_upload_is_refused(self):
        code, out = self.submit("--build-request", "build_000000000000")
        self.assertEqual(code, 1)
        self.assertIn("upload rejected", out)

    def test_a_viewer_build_files_its_run_under_the_build_repo(self):
        b = self.new_build()
        captured = {}
        real = app.subprocess.Popen

        def spy(cmd, *a, **kw):
            captured["env"] = kw.get("env") or {}
            return real(cmd, *a, **kw)
        with patch.object(app.subprocess, "Popen", side_effect=spy):
            self.upload({"build_request": b["id"], "repo": "someone/else"})
        self.assertEqual(captured["env"].get("MT_REPO"), REPO)


class SubmitCaseSelectionTests(unittest.TestCase):
    def test_select_cases(self):
        submit = load_submit()
        cases = [Path("mt-tests/eula"), Path("mt-tests/ios-eula")]
        self.assertEqual(submit.select_cases(cases, ""), (cases, None))
        self.assertEqual(submit.select_cases(cases, " ios-eula ,"), ([cases[1]], None))
        self.assertEqual(submit.select_cases(cases, "eula,gone")[1], "case(s) not found: gone")


class BranchNameTests(unittest.TestCase):
    def test_branch_pattern(self):
        for ok in ("master", "feat/x", "ci/monkeytester-combined", "v1.2"):
            self.assertTrue(builds.BRANCH_RE.match(ok), ok)
        for bad in ("", "a..b", "/x", "x/", "a b", "a~1", "x.", "a//b"):
            self.assertFalse(builds.BRANCH_RE.match(bad), bad)

    def test_secret_files_are_tightened(self):
        with tempfile.TemporaryDirectory() as tmp:
            loose = Path(tmp) / "loose.json"
            loose.write_text("{}")
            os.chmod(loose, 0o664)
            builds.tighten_mode(loose)
            self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
