"""The Configure panel's staged-edit state machine (plugins.html JS), run
against node, plus static asserts that ONE Save button replaced every
action/save button (token ops and "Remove plugin" are staged selects). The
Save payload order — token op first, then the address step (incl. removal),
Copilot sign-in before the config body, remote keys + provider in one config
body, sign-out last — is asserted here; the routing itself is asserted in
test_app_api.py against the fake plugin."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent


def load_staging_js():
    src = (ROOT / "plugins.html").read_text()
    m = re.search(r"(?s)// ── STAGING MACHINE START.*?// ── STAGING MACHINE END", src)
    assert m, "staging machine markers missing from plugins.html"
    return m.group(0)


def run_staging(saved, current):
    harness = load_staging_js() + """
let input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
let staged = stagePanelFields(input.saved, input.current);
console.log(JSON.stringify({staged: staged, steps: planSavePayload(staged)}));
"""
    fd, js = tempfile.mkstemp(suffix=".js", prefix="mt_staging_")
    try:
        with open(fd, "w") as fh:
            fh.write(harness)
        res = subprocess.run(["node", js],
                             input=json.dumps({"saved": saved, "current": current}),
                             capture_output=True, text=True, timeout=30)
    finally:
        os.remove(js)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


class PanelStagingTests(unittest.TestCase):
    """Dirty/clean diffing: staged = fields whose panel value differs from the
    saved snapshot; Save greyed (no steps) while staged is empty. Selects arm
    the token op, plugin removal and Copilot sign in/out."""

    def saved(self, **kw):
        s = {"configured": True, "url": "http://a:1", "tokenConfigured": True,
             "provider": "local", "selected": "qwen", "copilotModel": "",
             "copilotSignedIn": True,
             "remote": {"url": "", "model": "", "resize": True, "keyConfigured": False}}
        s.update(kw)
        return s

    def current(self, **kw):
        c = {"address": "http://a:1", "token": "", "provider": "local",
             "localModel": "qwen", "copilotModel": "", "remoteUrl": "",
             "remoteModel": "", "remoteResize": True, "remoteKey": "",
             "tokenChoice": "keep", "pluginChoice": "keep", "copilotChoice": "keep"}
        c.update(kw)
        return c

    def test_clean_panel_stages_nothing(self):
        out = run_staging(self.saved(), self.current())
        self.assertEqual(out["staged"], {})
        self.assertEqual(out["steps"], [])  # Save greyed: nothing to apply

    def test_provider_flip_stages_only_the_provider(self):
        out = run_staging(self.saved(), self.current(provider="remote"))
        self.assertEqual(out["staged"], {"provider": "remote"})
        self.assertEqual(out["steps"],
                         [{"kind": "config", "body": {"provider": "remote"},
                           "url": "/api/config/visual-model"}])

    def test_trailing_slash_does_not_stage_the_address(self):
        out = run_staging(self.saved(url="http://a:1"), self.current(address="http://a:1/"))
        self.assertEqual(out["staged"], {})

    def test_address_and_provider_stage_both_address_first(self):
        out = run_staging(self.saved(), self.current(address="http://b:2", provider="remote"))
        self.assertEqual(out["staged"], {"plugin_url": "http://b:2", "provider": "remote"})
        self.assertEqual([(s["kind"], s["body"]) for s in out["steps"]],
                         [("address", {"plugin_url": "http://b:2"}),
                          ("config", {"provider": "remote"})])

    def test_token_paste_alone_stages_nothing(self):
        """The Set choice arms the paste — pasting without it changes nothing."""
        out = run_staging(self.saved(tokenConfigured=False), self.current(token="tok-1234"))
        self.assertEqual(out["staged"], {})

    def test_set_choice_stages_token_op_and_saves_first(self):
        out = run_staging(self.saved(tokenConfigured=False),
                          self.current(tokenChoice="set", token="tok-1234"))
        self.assertEqual(out["staged"], {"token_op": "set", "token_value": "tok-1234"})
        self.assertEqual(out["steps"],
                         [{"kind": "token", "body": {"set_token": "tok-1234"},
                           "url": "/api/config/visual-model"}])

    def test_replace_choice_arms_a_rotation(self):
        out = run_staging(self.saved(), self.current(tokenChoice="replace"))
        self.assertEqual(out["staged"], {"token_op": "replace"})
        self.assertEqual(out["steps"],
                         [{"kind": "token", "body": {"generate_token": True},
                           "url": "/api/config/visual-model"}])

    def test_remove_choice_clears_the_viewer_copy_before_other_steps(self):
        out = run_staging(self.saved(), self.current(tokenChoice="remove", provider="remote"))
        self.assertEqual(out["staged"], {"provider": "remote", "token_op": "remove"})
        self.assertEqual([(s["kind"], s["body"]) for s in out["steps"]],
                         [("token", {"clear_token": True}), ("config", {"provider": "remote"})])

    def test_plugin_removal_choice_is_the_address_step(self):
        out = run_staging(self.saved(), self.current(pluginChoice="remove"))
        self.assertEqual(out["staged"], {"plugin_remove": True})
        self.assertEqual(out["steps"],
                         [{"kind": "address", "body": {"plugin_url": ""},
                           "url": "/api/config/visual-model"}])

    def test_copilot_sign_in_arms_before_the_config_body(self):
        out = run_staging(self.saved(copilotSignedIn=False),
                          self.current(copilotChoice="sign_in", provider="copilot"))
        self.assertEqual(out["staged"], {"provider": "copilot", "copilot_op": "sign_in"})
        self.assertEqual([(s["kind"], s["url"]) for s in out["steps"]],
                         [("copilot_in", "/api/visual-model/plugin/copilot/device/start"),
                          ("config", "/api/config/visual-model")])

    def test_copilot_sign_out_runs_last(self):
        out = run_staging(self.saved(), self.current(copilotChoice="sign_out",
                                                     provider="copilot"))
        self.assertEqual([(s["kind"], s["url"]) for s in out["steps"]],
                         [("config", "/api/config/visual-model"),
                          ("copilot_out", "/api/visual-model/plugin/copilot/remove")])

    def test_unchanged_remote_and_resize_stage_nothing(self):
        saved = self.saved(provider="remote",
                           remote={"url": "http://r:8080", "model": "video-model",
                                   "resize": False, "keyConfigured": False})
        current = self.current(provider="remote", remoteUrl="http://r:8080",
                               remoteModel="video-model", remoteResize=False)
        out = run_staging(saved, current)
        self.assertEqual(out["staged"], {})
        self.assertEqual(out["steps"], [])

    def test_remote_edits_land_in_one_config_body(self):
        saved = self.saved(remote={"url": "http://r:8080", "model": "video-model",
                                   "resize": True, "keyConfigured": False})
        current = self.current(provider="remote", remoteUrl="http://r2:9000/",
                               remoteModel="qwen2.5-vl-7b", remoteResize=False,
                               remoteKey="k-1234 ")
        out = run_staging(saved, current)
        self.assertEqual(out["staged"],
                         {"provider": "remote", "remote_url": "http://r2:9000",
                          "remote_model": "qwen2.5-vl-7b", "remote_resize": False,
                          "remote_api_key": "k-1234"})
        self.assertEqual(out["steps"],
                         [{"kind": "config", "body": out["staged"],
                           "url": "/api/config/visual-model"}])

    def test_local_model_swap_stages_the_model(self):
        out = run_staging(self.saved(), self.current(localModel="llava"))
        self.assertEqual(out["staged"], {"model": "llava"})

    def test_one_save_button_and_no_action_buttons(self):
        """Static: the panel ships the single Save, the fetch buttons and the
        staged selects — none of the old action/save buttons anywhere."""
        src = (ROOT / "plugins.html").read_text()
        for present in ('id="pluginSaveAll"', 'id="pluginTokenChoice"',
                        'id="pluginCopilotChoice"', "planSavePayload"):
            self.assertIn(present, src)
        for gone in ('id="pluginAddressSave"', 'id="pluginSaveBtn"',
                     'id="pluginTokenSetBtn"', 'id="pluginTokenRemoveBtn"',
                     'id="pluginRemoteKeySave"', 'id="pluginRemoteKeyRemove"',
                     'id="pluginAddressRemove"', 'id="pluginTokenReplaceBtn"',
                     'id="pluginCopilotSignIn"', 'id="pluginCopilotSignOut"',
                     "Test &amp; save", "Remove pairing token",
                     ">Remove plugin</button>", ">Replace token</button>",
                     ">Sign out</button>", ">Sign in with GitHub<"):
            self.assertNotIn(gone, src)

    def test_panel_sections_plugin_then_configuration(self):
        """Static order of the panel's rendered markup (the innerHTML
        concatenation preserves it): a 'Plugin' section heading comes
        before the 'Configuration' heading, and the single Save button
        markup sits AFTER the Configuration heading — Save is the last
        interactive element at the bottom of Configuration."""
        src = (ROOT / "plugins.html").read_text()
        plugin_head = 'class="panel-section-head">Plugin</div>'
        config_head = 'class="panel-section-head">Configuration</div>'
        self.assertIn(plugin_head, src)
        self.assertIn(config_head, src)
        i_plugin = src.index(plugin_head)
        i_config = src.index(config_head)
        i_save = src.index('id="pluginSaveAll"')
        self.assertLess(i_plugin, i_config)
        self.assertLess(i_config, i_save)
        # The token select lives in the Plugin section, above the
        # Configuration heading. The plugin address field and the
        # keep/remove-plugin select are gone: the table row owns them.
        self.assertLess(src.index('id="pluginTokenChoice"'), i_config)
        self.assertNotIn('id="pluginAddressInput"', src)
        self.assertNotIn('id="pluginAddressChoice"', src)
        # No element comes between the Save row and the end of the
        # Configuration section: the Save inline-row div is what closes it.
        self.assertIn('<span class="hint" id="pluginSaveHint"></span></div>\'\n            + \'</div>\'',
                      src)


if __name__ == "__main__":
    unittest.main()
