import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import app

sys.path.insert(0, str(Path(__file__).parent / "mt-tests" / "scripts"))
import test_runner_core as core


class DeviceEventRelayTests(unittest.TestCase):
    def test_unset_controller_is_reported_as_an_error_event(self):
        with patch.dict("os.environ", {"CONTROLLER_URL": ""}):
            payload = next(app.controller_device_events())

        self.assertEqual(payload, {"devices": [], "error": app._CONTROLLER_UNSET})

    def test_unreachable_controller_is_reported_as_an_error_event(self):
        with patch.dict("os.environ", {"CONTROLLER_URL": "http://127.0.0.1:1"}):
            payload = next(app.controller_device_events())

        self.assertEqual(payload["devices"], [])
        self.assertIn("unreachable", payload["error"])

    def test_events_for_pages_carry_friendly_names_and_the_error(self):
        with patch("app.load_device_names", return_value={"a:1": "Lab phone"}):
            event = app.device_event_for_ui({
                "udids": [],
                "devices": [{"id": "a:1", "platform": "android", "model": "M"}],
                "error": "devicectl down",
            })

        self.assertEqual(event, {
            "devices": [{"id": "a:1", "platform": "android", "model": "M",
                         "friendly_name": "Lab phone"}],
            "error": "devicectl down",
        })


class RunTargetResolutionTests(unittest.TestCase):
    def test_legacy_specific_devices_are_preserved(self):
        targets, error = app.resolve_run_targets({
            "device": "192.168.1.100:5555 192.168.1.101:5555",
        })

        self.assertIsNone(error)
        self.assertEqual(targets, {
            "android": ["192.168.1.100:5555", "192.168.1.101:5555"],
            "ios": [],
            "ios_placeholder": False,
        })

    @patch("app.get_controller_devices", return_value=([], None))
    def test_specific_device_must_be_connected_unless_ip_port(self, _devices):
        targets, error = app.resolve_run_targets({
            "device_mode": "specific",
            "device": "emulator-5554",
        })

        self.assertIsNone(targets)
        self.assertEqual(error, "Device emulator-5554 is not connected to the Mobile Controller")

    def test_specific_mode_requires_a_device(self):
        targets, error = app.resolve_run_targets({"device_mode": "specific", "device": " "})

        self.assertIsNone(targets)
        self.assertEqual(error, "At least one device is required")

    @patch("app.get_controller_devices")
    def test_specific_devices_get_their_platform_from_the_controller(self, devices):
        devices.return_value = ([
            {"id": "R58N123", "platform": "android"},
            {"id": "00008030-ABC", "platform": "ios"},
        ], None)

        targets, error = app.resolve_run_targets({
            "device_mode": "specific",
            "device": "R58N123 00008030-ABC 192.168.1.100:5555",
        })

        self.assertIsNone(error)
        self.assertEqual(targets, {
            "android": ["192.168.1.100:5555", "R58N123"],
            "ios": ["00008030-ABC"],
            "ios_placeholder": False,
        })

    @patch("app.get_controller_devices")
    def test_all_platforms_combines_android_and_ios(self, devices):
        devices.return_value = ([
            {"id": "192.168.1.100:5555", "platform": "android"},
            {"id": "00008030-ABC", "platform": "ios"},
        ], None)

        targets, error = app.resolve_run_targets({"device_mode": "all"})

        self.assertIsNone(error)
        self.assertEqual(targets["android"], ["192.168.1.100:5555"])
        self.assertEqual(targets["ios"], ["00008030-ABC"])
        self.assertFalse(targets["ios_placeholder"])

    @patch("app.get_controller_devices")
    def test_all_platforms_without_an_iphone_records_ios_placeholder(self, devices):
        devices.return_value = ([{"id": "1.1.1.1:5", "platform": "android"}], None)

        targets, error = app.resolve_run_targets({"device_mode": "all"})

        self.assertIsNone(error)
        self.assertTrue(targets["ios_placeholder"])

    @patch("app.get_controller_devices")
    def test_controller_failure_is_reported(self, devices):
        devices.return_value = ([], "Mobile Controller unreachable")

        targets, error = app.resolve_run_targets({"device_mode": "android"})

        self.assertIsNone(targets)
        self.assertIn("unreachable", error)

    @patch("app.get_active_android_devices")
    def test_android_mode_rejects_an_empty_active_device_list(self, active_devices):
        active_devices.return_value = ([], None)

        targets, error = app.resolve_run_targets({"device_mode": "android"})

        self.assertIsNone(targets)
        self.assertEqual(error, "No active Android devices were found")

    @patch("app.get_controller_devices")
    def test_ios_mode_rejects_no_connected_iphone(self, devices):
        devices.return_value = ([{"id": "1.1.1.1:5", "platform": "android"}], None)

        targets, error = app.resolve_run_targets({"device_mode": "ios"})

        self.assertIsNone(targets)
        self.assertIn("No connected iOS devices", error)

    def test_ios_placeholder_is_visible_in_aggregate_metadata(self):
        original_run_root = core.RUN_ROOT
        try:
            with TemporaryDirectory() as directory:
                core.RUN_ROOT = Path(directory)
                core.initialize_multi_device_run(
                    ["192.168.1.100:5555"], ios_placeholder=True
                )
                metadata = json.loads((core.RUN_ROOT / "run_metadata.json").read_text())
        finally:
            core.RUN_ROOT = original_run_root

        self.assertEqual(metadata["devices"][0]["platform"], "android")
        ios = metadata["devices"][1]
        self.assertEqual(ios["platform"], "ios")
        self.assertEqual(ios["status"], "not_supported")
        self.assertIn("not supported", ios["error"])


class RunStreamTests(unittest.TestCase):
    def stream(self, serial, devices):
        with patch("app.get_run_metadata", return_value={"device": {"serial": serial}}), \
                patch("app.cached_controller_devices", return_value=devices), \
                patch("app.load_device_names", return_value={}):
            return app.get_run_stream_device("run_1")

    def test_android_streams_and_ios_polls(self):
        android = self.stream("1.1.1.1:5", [{"id": "1.1.1.1:5", "platform": "android", "model": "M"}])
        self.assertEqual(android["status"], "mapped")
        self.assertEqual(android["device"], {"path": "1.1.1.1:5", "name": "M"})
        ios = self.stream("ios:AB-1", [{"id": "AB-1", "platform": "ios"}])
        self.assertEqual((ios["status"], ios["serial"]), ("ios", "AB-1"))

    def test_disconnected_device_is_unavailable(self):
        self.assertEqual(self.stream("1.1.1.1:5", [])["status"], "unavailable")


class DeviceNamesTests(unittest.TestCase):
    def test_old_capture_card_file_keeps_only_device_names(self):
        for old in ([{"path": "/dev/video0", "name": "Samsung", "adb_host": "1.1.1.1:5"},
                     {"path": "/dev/video1", "name": "unused", "adb_host": ""}],
                    {"/dev/video0": "Samsung"}):
            with TemporaryDirectory() as directory:
                config = Path(directory) / "names.json"
                config.write_text(json.dumps(old))
                with patch.object(app, "CONFIG_DEVICES_FILE", config):
                    expected = {"1.1.1.1:5": "Samsung"} if isinstance(old, list) else {}
                    self.assertEqual(app.load_device_names(), expected)


if __name__ == "__main__":
    unittest.main()