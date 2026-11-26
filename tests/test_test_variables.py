"""Test variables: the runner seeds `${KEY}` values from config.json and
MT_TEST_VARS (Settings -> Test Variables), with the env map overriding and
bad entries skipped instead of breaking a run."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mt-tests" / "scripts"))
with patch.dict(os.environ, {"VISION_TESTS_DIR": tempfile.mkdtemp()}):
    import test_runner_core as core


class ConfiguredVariablesTests(unittest.TestCase):
    def test_config_json_map_seeded_with_bad_entries_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({
                "video_size": "100%",
                "test_variables": {"hub_path": "/api", "bad key": "x", "not_a_string": 5},
            }))
            got = core.configured_variables(config_files=[cfg], env_text="")
        self.assertEqual(got, {"HUB_PATH": "/api"})

    def test_env_map_overrides_config_json(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"test_variables": {"HUB_PATH": "/api", "KEEP": "1"}}))
            got = core.configured_variables(config_files=[cfg],
                                            env_text=json.dumps({"HUB_PATH": "/v2", "EXTRA": ""}))
        self.assertEqual(got, {"HUB_PATH": "/v2", "KEEP": "1", "EXTRA": ""})

    def test_missing_files_and_unparsable_env_are_never_fatal(self):
        got = core.configured_variables(config_files=[Path("/nonexistent/config.json")],
                                        env_text="{not json")
        self.assertEqual(got, {})

    def test_empty_value_is_a_value(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text(json.dumps({"test_variables": {"HUB_PATH": ""}}))
            got = core.configured_variables(config_files=[cfg], env_text="")
        self.assertEqual(got, {"HUB_PATH": ""})

    def test_viewer_settings_override_repo_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            repo_default = Path(d) / "repo.json"
            repo_default.write_text(json.dumps({"test_variables": {"HUB_PATH": "", "EXTRA": "1"}}))
            settings = Path(d) / "settings.json"
            settings.write_text(json.dumps({"test_variables": {"HUB_PATH": "/api"}}))
            got = core.configured_variables(config_files=[repo_default, settings], env_text="")
        self.assertEqual(got, {"HUB_PATH": "/api", "EXTRA": "1"})


if __name__ == "__main__":
    unittest.main()
