"""build_plugin_client.py against a fake build plugin (BUILD_PLUGIN_API.md)."""
import unittest

import build_plugin_client as bpc
from fake_build_plugin import FORGE_TOKEN, REPO, TEMPLATES, FakeBuildPlugin, start


class ClientCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.url = start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeBuildPlugin.reset()
        self.client = bpc.BuildPluginClient("build-local", self.url)


class HealthTests(ClientCase):
    def test_health_checks_contract_name_and_version(self):
        body = self.client.health()
        self.assertEqual(body["recipes"]["ios"], ".monkeytester/build-ios.sh")
        FakeBuildPlugin.contract = "monkeytester-plugin"
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.health()
        self.assertEqual(cm.exception.code, "contract")
        FakeBuildPlugin.contract, FakeBuildPlugin.contract_version = "monkeytester-build-plugin", 2
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.health()
        self.assertEqual(cm.exception.code, "contract")
        self.assertIn("contract_version is 2", cm.exception.message)

    def test_health_entry_reports_forge_and_errors(self):
        entry = bpc.health_entry(self.client)
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["forge"]["label"], "Gitea")
        self.assertEqual(entry["forge"]["user"], "monkeytester-bot")
        self.assertEqual(entry["platforms"], ["android", "ios"])
        self.assertEqual(entry["recipes"], {"android": ".monkeytester/build-android.sh",
                                            "ios": ".monkeytester/build-ios.sh"})
        self.assertEqual(entry["capabilities"], ["logs", "cancel"])
        self.assertEqual(entry["viewer_url"], "http://viewer.example:8089")
        # A Linux host offers Android only.
        FakeBuildPlugin.platforms = ["android"]
        entry = bpc.health_entry(self.client)
        self.assertEqual((entry["platforms"], list(entry["recipes"])), (["android"], ["android"]))
        dead = bpc.health_entry(bpc.BuildPluginClient("x", "http://127.0.0.1:9"), timeout=1)
        self.assertFalse(dead["ok"])
        self.assertIn("unreachable", dead["error"])

    def test_unreachable_plugin(self):
        with self.assertRaises(bpc.BuildPluginError) as cm:
            bpc.BuildPluginClient("x", "http://127.0.0.1:9", timeout=1).health()
        self.assertEqual(cm.exception.code, "unreachable")


class AuthTests(ClientCase):
    def test_pairing_token_is_sent_as_x_plugin_token(self):
        FakeBuildPlugin.pairing = "pair-1234"
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.config()
        self.assertEqual((cm.exception.status, cm.exception.code), (401, "unauthorized"))
        client = bpc.BuildPluginClient("build-local", self.url, token="pair-1234")
        self.assertTrue(client.config()["token"]["configured"])
        self.assertEqual(FakeBuildPlugin.calls[-1][3].get("X-Plugin-Token"), "pair-1234")

    def test_validate_checks_health_and_the_pairing_token(self):
        FakeBuildPlugin.pairing = "pair-1234"
        url, body = bpc.validate("build-local", self.url + "/", token="pair-1234")
        self.assertEqual(url, self.url)
        self.assertEqual(body["contract"], bpc.CONTRACT)
        with self.assertRaises(bpc.BuildPluginError) as cm:
            bpc.validate("build-local", self.url, token="wrong")
        self.assertEqual(cm.exception.code, "unauthorized")
        for name, url in (("bad name!", self.url), ("ok", "ftp://x"), ("ok", "")):
            with self.assertRaises(bpc.BuildPluginError):
                bpc.validate(name, url)


class ConfigTests(ClientCase):
    def test_config_never_carries_the_forge_token(self):
        cfg = self.client.config()
        self.assertEqual(cfg["token"], {"configured": True, "last4": "abcd",
                                        "saved_at": "2026-11-19T10:00:00"})
        self.assertEqual((cfg["forge_kind"], cfg["viewer_url"]), ("gitea", "http://viewer.example:8089"))
        self.assertNotIn(FORGE_TOKEN, repr(cfg))
        # Even a plugin that sends the token back is reduced to the public shape.
        self.assertNotIn("s3cret", repr(bpc.public_config({"token": "s3cret", "forge_url": "u"})))

    def test_save_config_relays_a_forge_refusal(self):
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.save_config({"token": "wrong"})
        self.assertEqual((cm.exception.status, cm.exception.code, cm.exception.forge_status),
                         (400, "forge_refused", 401))
        cfg = self.client.save_config({"forge_kind": "github", "forge_url": "https://github.com",
                                       "token": FORGE_TOKEN, "viewer_url": "http://v:8089"})
        self.assertEqual((cfg["forge_kind"], cfg["forge_url"], cfg["viewer_url"]),
                         ("github", "https://github.com", "http://v:8089"))
        self.assertNotIn(FORGE_TOKEN, repr(cfg))
        cfg = self.client.save_config({"token": None})
        self.assertFalse(cfg["token"]["configured"])
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.save_config({"secret": "x"})
        self.assertEqual(cm.exception.code, "bad_request")


class RepoTests(ClientCase):
    def test_check_branches_and_cases(self):
        FakeBuildPlugin.repos[REPO]["recipes"]["develop"] = {"ios": False}
        check = self.client.check_repo(REPO, "develop")
        self.assertEqual((check["ok"], check["branch"]), (True, "develop"))
        self.assertEqual(check["recipes"], {"android": True, "ios": False})
        branches = self.client.branches(REPO)
        self.assertEqual(branches["default"], "master")
        self.assertEqual(branches["branches"][0], "master")
        cases = self.client.cases(REPO, "feat/x")
        self.assertEqual(cases["cases"], ["eula", "ios-eula"])
        self.assertEqual(cases["recipes"], {"android": True, "ios": True})
        # owner and repo are quoted segments; the ref is a query parameter.
        path, query = FakeBuildPlugin.calls[-1][1], FakeBuildPlugin.calls[-1][2]
        self.assertEqual(path, f"/repos/{REPO}/cases")
        self.assertEqual(query["ref"], ["feat/x"])

    def test_unknown_branch_is_404_not_found(self):
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.cases(REPO, "gone")
        self.assertEqual((cm.exception.status, cm.exception.code), (404, "not_found"))

    def test_no_forge_token_is_not_configured(self):
        FakeBuildPlugin.forge["token"] = ""
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.branches(REPO)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "not_configured"))


class BuildTests(ClientCase):
    def test_start_and_poll_a_build(self):
        started = self.client.start_build(REPO, "feat/x", ["android", "ios"], ["eula"], "build_0123456789ab")
        self.assertTrue(started["build_ref"].startswith("br_"))
        self.assertEqual(FakeBuildPlugin.started[-1], {
            "repo": REPO, "branch": "feat/x", "platforms": ["android", "ios"], "cases": ["eula"],
            "request_id": "build_0123456789ab"})
        self.assertEqual(self.client.build(started["build_ref"])["state"], "queued")
        FakeBuildPlugin.builds[started["build_ref"]] = {
            "state": "completed", "conclusion": "failure", "url": "http://forge/run/1",
            "jobs": [{"name": "Build and test", "status": "completed", "conclusion": "failure",
                      "url": "http://forge/job/1"}, "junk"]}
        status = self.client.build(started["build_ref"])
        self.assertEqual((status["state"], status["conclusion"]), ("completed", "failure"))
        self.assertEqual(status["jobs"], [{"name": "Build and test", "status": "completed",
                                           "conclusion": "failure", "url": "http://forge/job/1"}])

    def test_values_outside_the_contract_are_dropped(self):
        FakeBuildPlugin.builds["br_x"] = {"state": "weird", "conclusion": "success", "jobs": []}
        status = self.client.build("br_x")
        self.assertEqual((status["state"], status["conclusion"]), ("pending", None))

    def test_refused_start_missing_script_and_platform(self):
        FakeBuildPlugin.start_status = (409, "not_configured", "no viewer URL")
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.start_build(REPO, "feat/x", ["android"], ["eula"], "build_0123456789ab")
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "not_configured"))
        FakeBuildPlugin.start_status = None
        FakeBuildPlugin.repos[REPO]["recipes"]["feat/x"] = {"ios": False}
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.start_build(REPO, "feat/x", ["ios"], ["eula"], "build_0123456789ab")
        self.assertEqual(cm.exception.code, "bad_request")
        self.assertIn(".monkeytester/build-ios.sh", cm.exception.message)
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.start_build(REPO, "gone", ["android"], ["eula"], "build_0123456789ab")
        self.assertEqual(cm.exception.code, "not_found")

    def test_log_is_tailed_with_offset_size_and_complete(self):
        ref = self.client.start_build(REPO, "feat/x", ["android"], ["eula"], "build_0123456789ab")["build_ref"]
        FakeBuildPlugin.logs[ref] = b"line one\nline two\n"
        log = self.client.log(ref)
        self.assertEqual((log["text"], log["size"], log["complete"]), (b"line one\nline two\n", 18, False))
        log = self.client.log(ref, offset=9)
        self.assertEqual((log["text"], log["size"]), (b"line two\n", 18))
        self.assertEqual(FakeBuildPlugin.calls[-1][2]["offset"], ["9"])
        FakeBuildPlugin.builds[ref]["state"] = "completed"
        self.assertTrue(self.client.log(ref, offset=18)["complete"])
        with self.assertRaises(bpc.BuildPluginError) as cm:
            self.client.log("br_nope")
        self.assertEqual(cm.exception.status, 404)

    def test_cancel_returns_the_build_state(self):
        ref = self.client.start_build(REPO, "feat/x", ["android"], ["eula"], "build_0123456789ab")["build_ref"]
        status = self.client.cancel(ref)
        self.assertEqual((status["state"], status["conclusion"]), ("completed", "cancelled"))
        self.assertEqual(FakeBuildPlugin.cancelled, [ref])

    def test_template_is_text_per_platform(self):
        self.assertEqual(self.client.template("ios"), TEMPLATES["ios"])
        self.assertEqual(FakeBuildPlugin.calls[-1][2]["platform"], ["ios"])
        with self.assertRaises(bpc.BuildPluginError):
            self.client.template("windows")


class ParseTests(unittest.TestCase):
    def test_parse_build_plugins(self):
        self.assertEqual(bpc.parse_build_plugins(" build-local=http://127.0.0.1:8094/ ,mac2=http://h:8095"),
                         [("build-local", "http://127.0.0.1:8094"), ("mac2", "http://h:8095")])
        self.assertEqual(bpc.parse_build_plugins(""), [])
        for bad in ("x", "a=ftp://h", "a=http://h,a=http://g", "bad name=http://h"):
            with self.assertRaises(bpc.BuildPluginError, msg=bad):
                bpc.parse_build_plugins(bad)

    def test_repo_path_quotes_each_part(self):
        self.assertEqual(bpc.repo_path("my org/my.repo"), "/repos/my%20org/my.repo")


if __name__ == "__main__":
    unittest.main()
