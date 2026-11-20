#!/usr/bin/env python3
"""
CLI entrypoint: run visual regression tests on devices served by the Mobile
Controller (CONTROLLER_URL). Reads test step files, performs actions through the
controller, and uses a vision model to verify elements and compare screenshots.
Automatically records video. Delegates step execution to test_runner_core.py.

Devices are ids the controller reports (`--list-devices`): an adb serial or an
iPhone UDID. The controller says which platform each one is.

Usage:
    python run_on_device.py --device 192.168.1.100:5555 tests/login_flow.txt
    python run_on_device.py --device 00008030-ABC --ios-bundle-id com.example.app
    python run_on_device.py --device 192.168.1.100:5555 --test-dir tests/ --debug
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

# ── Early config: load visual model settings from app's config.json ──
# This must happen BEFORE importing test_runner_core (which imports
# vision_analyzer, which reads the env vars at module load time).
_VISUAL_CONFIG = None
_cfg_path = None
_CONFIG_SOURCES = [
    Path(__file__).resolve().parent.parent.parent / "Run" / "config.json",  # MonkeyTester/Run/config.json
    Path(__file__).resolve().parent / "config.json",                        # scripts/config.json (fallback)
]
for _cfg_path in _CONFIG_SOURCES:
    if _cfg_path.exists():
        try:
            _VISUAL_CONFIG = json.loads(_cfg_path.read_text())
            break
        except Exception:
            pass

COPILOT_MOVED_MESSAGE = (
    "GitHub Copilot is now configured on the MonkeyTester-AI plugin: Settings → "
    "Device Plugins → monkeytester-ai → Configure → GitHub Copilot")

if _VISUAL_CONFIG and (_VISUAL_CONFIG.get("visual_provider") or "").strip() == "copilot":
    # The viewer no longer talks to Copilot: the AI plugin owns the sign-in and
    # forwards vision calls to Copilot when it is selected there. A config.json
    # from before that move fails here, loudly, instead of silently falling
    # back to the local model.
    print(f"ERROR: config.json still selects visual_provider \"copilot\". {COPILOT_MOVED_MESSAGE}",
          file=sys.stderr, flush=True)
    sys.exit(2)
elif _VISUAL_CONFIG:
    # Provider: the monkeytester-ai plugin at the address set in Settings →
    # Device Plugins (config.json visual_plugin_url; the viewer imports a
    # VT_PLUGINS entry into it once). Its URL is the endpoint (the service
    # ignores the model field; the model lives in the plugin, not in
    # config.json), and visual_api_key is the pairing credential the plugin
    # generated. Vision env is still set here, at import time — live reload
    # of the selection remains a documented limitation: a running run keeps
    # the endpoint it started with.
    _plugin = (_VISUAL_CONFIG.get("visual_plugin") or "").strip()
    _plugin_url = (_VISUAL_CONFIG.get("visual_plugin_url") or "").strip().rstrip("/")
    _ep = _plugin_url + "/v1/chat/completions" if _plugin_url else ""
    if not _ep:
        if _plugin:
            print(f"WARNING: visual_plugin {_plugin!r} has no address (visual_plugin_url); "
                  "set it in Settings → Device Plugins", file=sys.stderr)
            print("DEPRECATED: falling back to visual_endpoint/visual_model", file=sys.stderr)
        _ep = (_VISUAL_CONFIG.get("visual_endpoint") or "").strip()
    if _ep:
        os.environ["VISION_MODEL_API_URL"] = _ep
        os.environ["VISUAL_MODEL_ENDPOINT"] = _ep
    mn = (_VISUAL_CONFIG.get("visual_model") or "").strip()
    if mn and not (_plugin or _plugin_url):
        os.environ["VISION_MODEL_NAME"] = mn
        os.environ["VISUAL_MODEL_NAME"] = mn
    ak = (_VISUAL_CONFIG.get("visual_api_key") or "").strip()
    if ak:
        os.environ["VISUAL_MODEL_API_KEY"] = ak

# Managed mode: the MonkeyTester Mac app passes the monkeytester-ai plugin's
# address and pairing token (both required) to the viewer, and the runner
# inherits them. They win over config.json (visual_plugin_url,
# visual_api_key) — the same rule as the viewer's managed_ai_plugin().
_MANAGED_AI_URL = (os.environ.get("VT_MANAGED_AI_PLUGIN_URL") or "").strip().rstrip("/")
_MANAGED_AI_TOKEN = (os.environ.get("VT_MANAGED_AI_PLUGIN_TOKEN") or "").strip()
if _MANAGED_AI_URL and _MANAGED_AI_TOKEN:
    os.environ["VISION_MODEL_API_URL"] = _MANAGED_AI_URL + "/v1/chat/completions"
    os.environ["VISUAL_MODEL_ENDPOINT"] = os.environ["VISION_MODEL_API_URL"]
    os.environ["VISUAL_MODEL_API_KEY"] = _MANAGED_AI_TOKEN
del _MANAGED_AI_URL, _MANAGED_AI_TOKEN

# Device plugins: once config.json has a `plugins` map (Settings → Device
# Plugins owns the list; the viewer imports a VT_PLUGINS entry set into it
# once), the runner must see exactly that list — the environment's VT_PLUGINS
# is stale by then, so re-export it here before test_runner_core imports run.
if _VISUAL_CONFIG and isinstance(_VISUAL_CONFIG.get("plugins"), dict):
    _PLUGINS_SPEC = ",".join(f"{_name}={_url}"
                             for _name, _url in _VISUAL_CONFIG["plugins"].items()
                             if str(_name).strip() and str(_url).startswith("http"))
    if _PLUGINS_SPEC or _VISUAL_CONFIG["plugins"]:
        os.environ["VT_PLUGINS"] = _PLUGINS_SPEC

del _VISUAL_CONFIG
try:
    del _cfg_path
except NameError:
    pass

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

import test_runner_core as core
import controller_client as controller
import device_lock
import ios_app_archive


def _sigterm_finalize(signum, frame):
    """Host shutdown (or any kill) sends us SIGTERM: the run would otherwise
    leave run_metadata.json stuck at "running" and the viewer would show a
    phantom Running run forever. Finalize as interrupted, then exit with the
    shell's conventional 128+15 status."""
    try:
        core.mark_run_interrupted(
            "interrupted: runner received SIGTERM (host shutdown)")
    except Exception:
        pass
    sys.exit(143)


signal.signal(signal.SIGTERM, _sigterm_finalize)

# Capture visual model config fingerprint at startup (before any tests run)
core.capture_visual_config_fingerprint()


def _process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_previous_run(pid, run_id=None, poll_secs=2.0):
    """Block until the previous run (process `pid`) has finished. The viewer
    writes Run/<run_id>/exit_code when it reaps that process, which also covers
    the moment between the process exiting and being reaped."""
    label = run_id or f"process {pid}"
    exit_file = (core.RUN_ROOT.parent / run_id / "exit_code") if run_id else None
    announced = False
    while _process_alive(pid) and not (exit_file and exit_file.exists()):
        if not announced:
            print(f"  [{core._ts()}] Waiting for {label} to complete "
                  f"(runs execute in submission order)...")
            core.initialize_run_progress("queued", f"Waiting for {label} to complete")
            announced = True
        time.sleep(poll_secs)
    if announced:
        print(f"  [{core._ts()}] {label} finished; starting")


DEFAULT_CONNECT_WAIT_SECS = 300


def wait_for_platform_devices(platforms, timeout=None, poll_secs=5.0):
    """Wait until the Mobile Controller serves at least one device of every
    platform in `platforms`, then return ({platform: [ids]}, None). Gives up
    after `timeout` seconds (VT_DEVICE_CONNECT_WAIT_SECS, default 300) and
    returns (None, error). An unreachable controller counts as no devices."""
    if timeout is None:
        timeout = int(os.environ.get("VT_DEVICE_CONNECT_WAIT_SECS") or DEFAULT_CONNECT_WAIT_SECS)
    names = {"android": "an Android phone", "ios": "an iPhone"}
    deadline = time.monotonic() + timeout
    announced = None
    while True:
        devices, error = controller.list_devices()
        found = {p: [d["id"] for d in devices if d.get("platform") == p] for p in platforms}
        missing = [p for p in platforms if not found[p]]
        if not missing:
            if announced:
                print(f"  [{core._ts()}] Devices connected; starting")
            return found, None
        wanted = " and ".join(names[p] for p in missing)
        if time.monotonic() >= deadline:
            reason = f"Gave up after {timeout}s waiting for {wanted} to connect"
            if error:
                reason += f" ({error})"
            return None, reason
        if announced != wanted:
            print(f"  [{core._ts()}] Waiting up to {timeout}s for {wanted} to connect...")
            core.initialize_run_progress("waiting_for_devices", f"Waiting for {wanted} to connect")
            announced = wanted
        time.sleep(poll_secs)


def _read_ios_bundle_id(app_path):
    """Read CFBundleIdentifier from an .app bundle's Info.plist."""
    import plistlib
    for plist in ("Info.plist", "embedded.mobileprovision"):
        p = os.path.join(app_path, plist)
        if plist == "Info.plist" and os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    data = plistlib.load(f)
                bid = data.get("CFBundleIdentifier")
                if bid:
                    print(f"  [iOS] Bundle ID from Info.plist: {bid}")
                    return bid
            except Exception as e:
                print(f"WARNING: could not parse {p}: {e}")
    return None


def _record_ios_setup_failure(idx, udid, message, is_multi, test_files=()):
    """Record why a device never started (its tests become UNAVAILABLE); a
    single-device run ends here, so without this its metadata would stay
    "running"."""
    if is_multi:
        core.record_setup_failure(list(test_files), f"ios_device_{idx}", message)
    else:
        core.initialize_run_progress("failed", f"{udid}: {message}")


def run_ios_devices(args, ios_app_path, ios_bundle_id, test_files,
                    test_files_list, is_multi):
    """Run the test suite on each iOS device. Returns (passed, failed, all_success)."""
    grand_passed = 0
    grand_failed = 0
    all_success = True

    for idx, udid in enumerate(args.ios_device, 1):
        if is_multi or args.device:
            print(f"\n{'#'*60}")
            print(f"  [{core._ts()}] === iOS DEVICE {idx}/{len(args.ios_device)}: {udid} ===")
            print(f"{'#'*60}")

        os.environ["DEVICE_ID"] = udid
        os.environ["DEVICE_PLATFORM"] = "ios"

        # Lockscreen guard: a locked iPhone black-screens every capture.
        if not controller.check_unlocked():
            print(f"ERROR: {udid} is locked — cannot run tests.")
            all_success = False
            _record_ios_setup_failure(idx, udid, "device locked or unavailable", is_multi, test_files)
            continue

        # Install the app under test (.ipa or .app, via the Mobile Controller)
        ios_app_version = None
        if ios_app_path:
            core.initialize_run_progress(
                "installing_apk", f"Installing the uploaded app on {udid}")
            controller.uninstall_app(ios_bundle_id)
            time.sleep(2)
            if not controller.install_apk(ios_app_path):
                print(f"ERROR: Could not install app on {udid}. Skipping.")
                all_success = False
                _record_ios_setup_failure(idx, udid, "app installation failed", is_multi, test_files)
                continue
            ios_app_version = ios_bundle_id
        elif ios_bundle_id:
            ios_app_version = ios_bundle_id  # app already on device

        passed, failed, success = core._run_device_tests(
            test_files, ios_app_version, ios_bundle_id, ios_app_path, args,
            None, test_files_list, f"ios_device_{idx}" if is_multi else None
        )
        grand_passed += passed
        grand_failed += failed
        if not success:
            all_success = False

        # Uninstall the app under test (same cleanup contract as Android)
        if ios_app_version and ios_app_path:
            controller.uninstall_app(ios_bundle_id)

    return grand_passed, grand_failed, all_success


PLATFORM_NAMES = {"android": "Android", "ios": "iOS"}


def parse_build_failed(values):
    """--build-failed PLATFORM=MESSAGE values -> ({platform: message}, error)."""
    failed = {}
    for value in values or []:
        platform, sep, message = (value or "").partition("=")
        platform = platform.strip().lower()
        if not sep or platform not in PLATFORM_NAMES:
            return None, f"--build-failed needs android=MESSAGE or ios=MESSAGE, got {value!r}"
        message = " ".join(message.split())[:2000]
        failed[platform] = (f"{PLATFORM_NAMES[platform]} app didn't build: {message}"
                            if message else f"{PLATFORM_NAMES[platform]} app didn't build")
    return failed, None


def build_failed_device_entries(build_failed, android_count, ios_count):
    """Run-metadata device entries (plus "message") for the connected devices
    of each platform whose app didn't build. With none connected, one entry
    stands in for the platform, so the run still shows it failed."""
    devices, _error = controller.list_devices()
    entries = []
    for platform, message in build_failed.items():
        ids = [d.get("id") for d in devices or [] if d.get("platform") == platform and d.get("id")]
        offset = android_count if platform == "android" else ios_count
        prefix = "device" if platform == "android" else "ios_device"
        for n, dev_id in enumerate(ids or [None], offset + 1):
            serial = dev_id if platform == "android" else (f"ios:{dev_id}" if dev_id else None)
            entry = {"id": f"{prefix}_{n}", "platform": platform, "message": message}
            if serial:
                entry["serial"] = serial
            name = next((d.get("name") for d in devices or [] if d.get("id") == dev_id and d.get("name")), None)
            if name or not dev_id:
                entry["name"] = name or ("iPhone (none connected)" if platform == "ios"
                                         else "Android phone (none connected)")
            entries.append(entry)
    return entries


def split_devices_by_platform(device_ids, devices):
    """Split requested ids into (android, ios) using the controller's device
    list. Returns (android, ios, error); an id it doesn't serve is an error."""
    platforms = {d.get("id"): d.get("platform") for d in devices}
    missing = [d for d in device_ids if d not in platforms]
    if missing:
        return [], [], ("not connected to the Mobile Controller: "
                        + ", ".join(missing))
    android = [d for d in device_ids if platforms[d] == "android"]
    ios = [d for d in device_ids if platforms[d] == "ios"]
    return android, ios, None


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description='Run visual regression tests with APK upload support'
    )
    parser.add_argument('test_file', nargs='?', default=None,
                       help='Path to a test file (optional; if omitted, all tests in the directory are run)')
    parser.add_argument('--apk', help='Path to APK file to install on device')
    parser.add_argument('--test-dir',
                       help='Directory containing test files to copy from')
    parser.add_argument('--test-files',
                       help='Comma-separated list of test files to run (overrides auto-discovery)')
    parser.add_argument('--verbose', action='store_true', default=False,
                       help='Enable folder-level consolidated result (marks other tests as SKIPPED)')
    parser.add_argument('--no-video', action='store_true', help='Disable video recording')
    parser.add_argument('--branch', default=None,
                       help='Git branch of the change under test (recorded in run metadata)')
    parser.add_argument('--device', nargs='+',
                       help='Device id(s) from the Mobile Controller: adb serials '
                            'and iPhone UDIDs, separated by spaces')
    parser.add_argument('--ios-app', default=None,
                       help='Path to an iOS .ipa, a .app bundle, or a .zip containing one '
                            'to install on iOS devices before running tests.')
    parser.add_argument('--ios-bundle-id', default=None,
                       help='Bundle ID of the app under test (used to relaunch it '
                            'before each test). Required for iOS runs unless the '
                            'bundle is derived from an uploaded .app.')
    parser.add_argument('--ios-placeholder', action='store_true',
                       help='Record iOS as not supported without executing tests')
    parser.add_argument('--debug', action='store_true', help='Enable verbose/debug output')
    parser.add_argument('--output-dir',
                       help='Base directory for run outputs (default: ./Run relative to cwd)')
    parser.add_argument('--after-pid', type=int, default=None,
                       help='Wait until the run with this process id has finished before '
                            'starting (runs execute in submission order)')
    parser.add_argument('--after-run', default=None,
                       help='Run id of the --after-pid run, for the progress message')
    parser.add_argument('--wait-for-platform', action='append', default=[],
                       choices=['android', 'ios'],
                       help='Test every device of this platform the controller serves when '
                            'the run starts; wait up to VT_DEVICE_CONNECT_WAIT_SECS (300) '
                            'for one to connect. Repeat for both platforms.')
    parser.add_argument('--record-snapshots', action='store_true',
                       help='Save every SNAPSHOT as a new baseline candidate without '
                            'comparing; the run ends as "recording", never "passed"')
    parser.add_argument('--build-failed', action='append', default=[], metavar='PLATFORM=MESSAGE',
                        help="The app for PLATFORM (android or ios) didn't build: list that "
                             "platform's connected devices in the run with every test FAILED "
                             "and MESSAGE as the reason (CI build/signing errors). Repeatable.")
    parser.add_argument('--build-log', action='append', default=[], metavar='PLATFORM=PATH',
                        help="Build log of a --build-failed platform, kept with its devices "
                             "and linked from their UNAVAILABLE tests.")
    parser.add_argument('--case', default=None,
                       help='Name of the test case folder (visual-tests/<case>/), used to '
                            'lay out downloaded baselines')
    parser.add_argument('--list-devices', '-l', action='store_true',
                       help='List the devices the Mobile Controller serves and exit')
    args = parser.parse_args()
    args.device = args.device or []

    if args.list_devices:
        devices, error = controller.list_devices()
        if error:
            print(f"ERROR: {error}")
            sys.exit(1)
        for d in devices:
            print(f"  {d['id']:40s} {d['platform']:8s} {d.get('model') or ''}")
        print(f"\nTotal: {len(devices)} device(s)\n")
        sys.exit(0)

    if not args.device and not args.ios_placeholder and not args.wait_for_platform:
        print("ERROR: No device(s) specified.")
        print("\nUse --device <id> to target a device, or --list-devices to see them.")
        sys.exit(1)

    # Set output directory if specified
    if args.output_dir:
        output_base = Path(args.output_dir)
        if not output_base.exists():
            print(f"ERROR: Output directory not found: {args.output_dir}")
            sys.exit(1)
        core.configure_output_dir(output_base)

    # Press-event overlay: report every tap/swipe to the viewer so it can
    # mark where the test is pressing on the live stream. Best-effort — if
    # the viewer is unreachable the events are dropped silently.
    try:
        os.environ.setdefault("PRESS_RUN", core.RUN_ROOT.name)
    except Exception:
        pass
    viewer_url = os.environ.get("VIEWER_URL", "").strip()
    if viewer_url:
        os.environ.setdefault("PRESS_EVENTS_URL", viewer_url)

    # Snapshot settings, read by test_runner_core (run metadata, SNAPSHOT steps)
    if args.record_snapshots:
        os.environ["VT_RECORD_SNAPSHOTS"] = "1"
    else:
        os.environ.pop("VT_RECORD_SNAPSHOTS", None)
    if args.case:
        os.environ["VT_CASE"] = args.case.strip()

    # Pass debug flag through to submodules
    if args.debug:
        os.environ["DEBUG"] = "true"

    # Open console log now that PROJECT_ROOT is final
    core.start_console_logging()
    core.initialize_run_progress(
        "initializing", "Runner started; validating uploaded inputs"
    )

    # Runs execute one after another, in the order they were submitted: wait
    # here (before touching any device) until the previous run has finished.
    # Metadata first, so the dashboard lists the run while it waits.
    if args.after_pid or args.wait_for_platform:
        core.save_run_metadata(branch=(args.branch or "").strip() or None)
    if args.after_pid:
        wait_for_previous_run(args.after_pid, args.after_run)

    # Platforms whose app didn't build: their devices are reported failed,
    # never touched, and not waited for.
    build_failed, error = parse_build_failed(args.build_failed)
    if error:
        print(f"ERROR: {error}")
        sys.exit(2)
    args.wait_for_platform = [p for p in args.wait_for_platform if p not in build_failed]

    # Platforms to test on whatever devices are connected now (the viewer
    # leaves this to the run, so a queued run sees the devices of its turn).
    if args.wait_for_platform:
        found, error = wait_for_platform_devices(list(dict.fromkeys(args.wait_for_platform)))
        if error:
            print(f"ERROR: {error}")
            core.save_run_metadata(branch=(args.branch or "").strip() or None)
            core.initialize_run_progress("failed", error)
            sys.exit(1)
        for ids in found.values():
            args.device += [d for d in ids if d not in args.device]

    # Ask the controller what each device is before doing anything to them.
    args.ios_device = []
    if args.device:
        error = controller.check_api_version()
        devices = []
        if not error:
            devices, error = controller.list_devices()
        if not error:
            args.device, args.ios_device, error = split_devices_by_platform(
                args.device, devices)
        if error:
            print(f"ERROR: {error}")
            core.save_run_metadata(branch=(args.branch or "").strip() or None)
            core.initialize_run_progress("failed", error)
            sys.exit(1)
    has_ios = bool(args.ios_device)
    build_failed_entries = build_failed_device_entries(
        build_failed, len(args.device), len(args.ios_device)) if build_failed else []
    first_device = next(iter(args.device + args.ios_device), None)
    if first_device:
        os.environ["DEVICE_ID"] = first_device

    # Create run_metadata.json early so failed runs are visible on the dashboard
    core.save_run_metadata(
        device_host=(f"ios:{first_device}" if has_ios and not args.device
                     else first_device),
        branch=(args.branch or "").strip() or None,
    )

    # Queue behind any run already using these devices. Held until this process
    # exits, so it covers the install as well as the tests.
    def _announce_wait(device):
        print(f"  [{core._ts()}] Device {device} is busy; waiting for it...")
        core.initialize_run_progress(
            "waiting_for_device", f"Waiting for device {device} to finish another run")

    try:
        _device_locks = device_lock.acquire_device_locks(  # noqa: F841 (held until exit)
            core.RUN_ROOT.parent / ".locks",
            list(args.device) + list(args.ios_device),
            on_wait=_announce_wait,
            timeout=int(os.environ.get("VT_DEVICE_WAIT_SECS")
                        or device_lock.DEFAULT_TIMEOUT_SECS),
        )
    except TimeoutError as e:
        print(f"ERROR: {e}")
        core.initialize_run_progress("failed", str(e))
        sys.exit(1)

    # Validate APK path if provided
    apk_version = None
    apk_package = None
    apk_path = None

    if args.apk and args.device:
        core.initialize_run_progress("installing_apk", "Installing the uploaded APK")
        if not os.path.exists(args.apk):
            print(f"ERROR: APK file not found: {args.apk}")
            core.initialize_run_progress("failed", "APK file was not found")
            sys.exit(1)
        apk_version, apk_package, apk_path = core.install_apk_on_device(args.apk)
        if not apk_version:
            print("FATAL: Could not install APK. Aborting.")
            core.initialize_run_progress("failed", "APK installation failed")
            sys.exit(1)

    # ── iOS app preparation ──
    # The iOS app under test is an .ipa or a .zip holding a .app (uploaded, or
    # given via --ios-app), or a .app directory. Archives are shipped to the Mac
    # unmodified so the signature survives. The bundle ID is explicit
    # (--ios-bundle-id) or read from the app's Info.plist.
    ios_app_path = None
    ios_bundle_id = (args.ios_bundle_id or "").strip() or None
    if has_ios:
        if args.ios_app:
            ios_app_path = args.ios_app
            if not os.path.exists(ios_app_path):
                print(f"ERROR: iOS app not found: {ios_app_path}")
                core.initialize_run_progress("failed", "iOS app file was not found")
                sys.exit(1)
        elif args.apk and ios_app_archive.is_ios_archive(args.apk):
            ios_app_path = args.apk  # an uploaded .ipa, or .zip holding the .app
        elif args.apk and not ios_bundle_id:
            print("ERROR: iOS runs need the app: upload an .ipa, or a .zip "
                  "containing the .app (as the 'apk' file), or pass --ios-app, "
                  "or pass --ios-bundle-id for an app already installed.")
            core.initialize_run_progress("failed", "iOS app is required")
            sys.exit(1)
        if ios_app_path and not ios_bundle_id:
            if ios_app_archive.is_ios_archive(ios_app_path):
                try:
                    ios_bundle_id = ios_app_archive.read_bundle_id(ios_app_path)
                    print(f"  [iOS] Bundle ID from archive: {ios_bundle_id}")
                except ValueError as e:
                    print(f"ERROR: {e}")
                    core.initialize_run_progress("failed", str(e))
                    sys.exit(1)
            else:
                ios_bundle_id = _read_ios_bundle_id(ios_app_path)
            if not ios_bundle_id:
                print("ERROR: could not determine the iOS bundle ID — pass "
                      "--ios-bundle-id explicitly.")
                core.initialize_run_progress("failed", "iOS bundle ID unknown")
                sys.exit(1)
        if not ios_bundle_id:
            print("ERROR: --ios-bundle-id is required for iOS runs (or upload the .app).")
            core.initialize_run_progress("failed", "iOS bundle ID unknown")
            sys.exit(1)

    # Resolve test files
    test_files = []
    test_files_list = []
    if args.device or has_ios or build_failed_entries:
        core.initialize_run_progress("discovering_tests", "Discovering test files")
        test_files = core.resolve_test_files(args)

        # Copy test files from --test-dir if specified
        core.copy_test_files_from_dir(args, test_files)

        # Check test files exist
        for tf in test_files:
            if not os.path.exists(tf):
                print(f"ERROR: Test file not found: {tf}")
                core.initialize_run_progress("failed", f"Test file not found: {tf}")
                if apk_version:
                    core.uninstall_apk_from_device(apk_package)
                sys.exit(1)

        # Keep the exact .txt files this run used, for the run page.
        core.save_test_sources(test_files)

        # Save test_files_list for metadata
        test_files_list = core.build_test_files_list(args)

    # Run tests on each device in sequence
    grand_passed = 0
    grand_failed = 0
    grand_all_success = True
    is_multi = ((len(args.device) + len(args.ios_device) > 1) or args.ios_placeholder
                or bool(build_failed_entries))
    if is_multi:
        core.initialize_multi_device_run(
            args.device, args.ios_placeholder,
            ios_devices=args.ios_device,
            branch=(args.branch or "").strip() or None,
            build_failed=[{k: v for k, v in e.items() if k != "message"} for e in build_failed_entries],
        )
    # Devices whose app didn't build fail first: nothing to run on them.
    build_logs = dict(v.partition("=")[::2] for v in args.build_log if "=" in v)
    for entry in build_failed_entries:
        core.record_setup_failure(test_files, entry["id"], entry["message"],
                                  build_logs.get(entry["platform"]))
        grand_all_success = False

    for idx, device in enumerate(args.device, 1):
        if is_multi:
            print(f"\n{'#'*60}")
            print(f"  [{core._ts()}] === DEVICE {idx}/{len(args.device)}: {device} ===")
            print(f"{'#'*60}")

        os.environ["DEVICE_ID"] = device
        os.environ["DEVICE_PLATFORM"] = "android"

        # A locked phone black-screens every capture.
        if not controller.check_unlocked():
            print(f"ERROR: {device} is locked or unavailable. Skipping.")
            grand_all_success = False
            if is_multi:
                core.record_setup_failure(test_files, f"device_{idx}", "device locked or unavailable")
            else:
                core.initialize_run_progress("failed", f"{device}: device locked or unavailable")
            continue

        # Install APK if specified
        if args.apk and idx > 1:
            if not apk_version:
                apk_version, apk_package, apk_path = core.install_apk_on_device(args.apk)
                if not apk_version:
                    print(f"ERROR: Could not install APK on {device}. Skipping.")
                    grand_all_success = False
                    core.record_setup_failure(
                        test_files, f"device_{idx}", "APK installation failed")
                    continue
            else:
                apk_version, apk_package, apk_path = core.install_apk_on_device(args.apk)
                if not apk_version:
                    print(f"ERROR: Could not install APK on {device}. Skipping.")
                    grand_all_success = False
                    core.record_setup_failure(
                        test_files, f"device_{idx}", "APK installation failed")
                    continue

        # Run tests for this device
        passed, failed, success = core._run_device_tests(
            test_files, apk_version, apk_package, apk_path, args,
            None, test_files_list, f"device_{idx}" if is_multi else None
        )
        grand_passed += passed
        grand_failed += failed
        if not success:
            grand_all_success = False

    # ── iOS devices ──
    if has_ios:
        ios_passed, ios_failed, ios_success = run_ios_devices(
            args, ios_app_path, ios_bundle_id, test_files, test_files_list, is_multi
        )
        grand_passed += ios_passed
        grand_failed += ios_failed
        if not ios_success:
            grand_all_success = False

    if is_multi:
        core.finish_multi_device_run(grand_passed, grand_failed, grand_all_success)
        # Sync the root-level pending_tests.json for multi-device runs: the
        # per-test progress writes above went to devices/<id>/pending_tests.json
        # (PROJECT_ROOT is switched per device), so the root file is still stuck
        # on a pre-device phase and the dashboard shows the run as running.
        core.update_run_progress(
            core.RUN_ROOT / "pending_tests.json",
            "completed" if grand_all_success else "completed_with_failures",
            f"Run finished: {grand_passed} passed, {grand_failed} failed",
        )

    # Final summary
    print(f"\n{'='*60}")
    print(f"  [{core._ts()}] RUN SUMMARY")
    print(f"{'='*60}")
    print(f"  Android devices: {len(args.device)}")
    if args.ios_device:
        print(f"  iOS devices: {', '.join(args.ios_device)}")
    if args.ios_placeholder:
        print("  iOS (other): Not supported yet")
    if args.apk:
        print(f"  APK Version: {apk_version or 'N/A'}")
    print(f"  Tests: {grand_passed} passed, {grand_failed} failed")
    for entry in build_failed_entries:
        print(f"  {entry['id']}: UNAVAILABLE ({entry['message']})")
    print(f"{'='*60}\n")

    sys.exit(0 if grand_all_success else 1)


if __name__ == "__main__":
    main()
