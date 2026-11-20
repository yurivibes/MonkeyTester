#!/usr/bin/env python3
"""
Shared test-execution engine for visual regression testing.

Reads test step files, performs actions through the Mobile Controller, and uses
a vision model to verify elements and compare screenshots. Automatically records
video.

Assumes DEVICE_ID is set by the caller before any function here is called. No
argparse/CLI code lives in this module; see run_on_device.py.
"""
import os
import sys
import json
import time
import glob
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

import vision_analyzer
import controller_client as controller
import apk_manifest
import plugin_client
import snapshots
import layout


# Package/bundle ID of the app under test, set per device by _run_device_tests.
_app_under_test = None

# Folder of the test file being run; SNAPSHOT baselines are under its snapshots/.
_current_test_dir = None
# Extra data the last step leaves for its result entry (e.g. a SNAPSHOT outcome).
_step_detail = {}
# App file of the device being tested (APK or IPA), for reinstalling it.
_app_path_under_test = None
# Last case started per output dir (device), to spot case boundaries.
_last_case_by_output = {}


def _device_platform():
    """'ios' or 'android' for DEVICE_ID. run_on_device sets DEVICE_PLATFORM;
    otherwise a UDID (hex with a dash) is an iPhone, anything else Android."""
    platform = (os.environ.get("DEVICE_PLATFORM") or "").strip().lower()
    if platform in ("ios", "android"):
        return platform
    device = os.environ.get("DEVICE_ID", "")
    return "ios" if re.fullmatch(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f-]{36,40}", device) else "android"


def run_flags():
    """Run-level settings recorded in run_metadata.json."""
    flags = {}
    if snapshots.record_mode():
        flags['record_snapshots'] = True
    case = (os.environ.get("VT_CASE") or "").strip()
    if case:
        flags['case'] = case
    repo = run_repo()
    if repo:
        flags['repo'] = repo
    if _multi_case_root is not None:
        flags['cases'] = [c.name for c in find_case_folders(_multi_case_root)]
    return flags


def run_repo():
    """The run's project (owner/repo) from VT_REPO, set by the viewer; "" if none."""
    repo = (os.environ.get("VT_REPO") or "").strip().strip("/")
    return repo if re.fullmatch(r"(?!\.+/)[A-Za-z0-9_.-]+/(?!\.+$)[A-Za-z0-9_.-]+", repo) else ""


def set_app_under_test(app_id):
    global _app_under_test
    _app_under_test = None if app_id in (None, "", "unknown") else app_id


# Test configuration - output base is Run/ relative to the current working directory
DEFAULT_OUTPUT_BASE = Path(".") / "Run"
_OUTPUT_BASE = Path(os.environ.get("VISION_TESTS_DIR", str(DEFAULT_OUTPUT_BASE)))


def _next_run_index(output_base: Path) -> int:
    """Return the next run index by scanning output_base for run_N directories.
    Starts at 0; if runs are deleted manually, continues from the highest found."""
    pat = re.compile(r"^run_(\d+)$")
    highest = -1
    if output_base.is_dir():
        for d in output_base.iterdir():
            m = pat.match(d.name)
            if m and d.is_dir():
                highest = max(highest, int(m.group(1)))
    return highest + 1


PROJECT_ROOT = _OUTPUT_BASE / f"run_{_next_run_index(_OUTPUT_BASE)}"
RUN_ROOT = PROJECT_ROOT
REFERENCES_DIR = PROJECT_ROOT / "references"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
RESULTS_DIR = PROJECT_ROOT / "results"
LOGS_DIR = PROJECT_ROOT / "logs"

# Ensure all output directories exist
for d in (PROJECT_ROOT, OUTPUTS_DIR, RESULTS_DIR, REFERENCES_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Where controller_client.py saves downloaded video recordings
os.environ["CAPTURE_OUTPUT_DIR"] = str(OUTPUTS_DIR)

# Console log file — opened lazily after PROJECT_ROOT is final (via start_console_logging())
_CONSOLE_LOG_PATH = None
_console_log = None

def _open_console_log(path):
    global _console_log
    _console_log = open(path, "a", buffering=1)

def _setup_tee():
    """Replace stdout/stderr with a tee writer that also writes to the console log."""
    class TeeWriter:
        def __init__(self, original, log_file):
            self.original = original
            self.log_file = log_file
        def write(self, data):
            self.original.write(data)
            self.log_file.write(data)
            try:
                self.original.flush()
            except Exception:
                pass
            try:
                self.log_file.flush()
            except Exception:
                pass
        def flush(self):
            try:
                self.original.flush()
            except Exception:
                pass
            try:
                self.log_file.flush()
            except Exception:
                pass
        def isatty(self):
            return getattr(self.original, 'isatty', lambda: False)()
    sys.stdout = TeeWriter(sys.stdout, _console_log)
    sys.stderr = TeeWriter(sys.stderr, _console_log)


def _set_project_root(project_root: Path) -> None:
    """Point the active test-artifact paths at one run or device directory."""
    global PROJECT_ROOT, REFERENCES_DIR, OUTPUTS_DIR, RESULTS_DIR, LOGS_DIR
    PROJECT_ROOT = project_root
    REFERENCES_DIR = PROJECT_ROOT / "references"
    OUTPUTS_DIR = PROJECT_ROOT / "outputs"
    RESULTS_DIR = PROJECT_ROOT / "results"
    LOGS_DIR = PROJECT_ROOT / "logs"
    for d in (PROJECT_ROOT, OUTPUTS_DIR, RESULTS_DIR, REFERENCES_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    os.environ["CAPTURE_OUTPUT_DIR"] = str(OUTPUTS_DIR)


def configure_output_dir(output_base: Path) -> None:
    """Reassign PROJECT_ROOT/OUTPUTS_DIR/etc. under output_base/run_N (incrementing
    index), create them, and point CAPTURE_OUTPUT_DIR at the new outputs dir. Callers
    should invoke this (if using a custom output base) before start_console_logging()."""
    global RUN_ROOT
    RUN_ROOT = output_base / f"run_{_next_run_index(output_base)}"
    _set_project_root(RUN_ROOT)
    print(f"  [RUN] Output base: {output_base}")
    print(f"  [RUN] Run dir: {RUN_ROOT}")


def _root_metadata_path() -> Path:
    return RUN_ROOT / "run_metadata.json"


def _update_root_device(device_id, **changes):
    """Update one device entry in the aggregate multi-device run metadata."""
    meta_path = _root_metadata_path()
    if not meta_path.exists():
        return
    with open(meta_path) as f:
        metadata = json.load(f)
    for device in metadata.get("devices", []):
        if device.get("id") == device_id:
            device.update(changes)
            break
    metadata["active_device_id"] = (
        device_id if changes.get("status") == "running" else metadata.get("active_device_id")
    )
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)


def initialize_multi_device_run(devices, ios_placeholder=False, branch=None,
                                ios_devices=None, build_failed=None):
    """Create the aggregate metadata read by the device-aware run viewer."""
    # The Android device name is filled in by save_run_metadata() once the
    # device is connected (reported by the Mobile Controller) — it is not known
    # at run-init time.
    entries = [
        {
            "id": f"device_{index}",
            "platform": "android",
            "serial": serial,
            "status": "pending",
            "passed": 0,
            "failed": 0,
            "total": 0,
        }
        for index, serial in enumerate(devices, 1)
    ]
    for index, udid in enumerate(ios_devices or [], 1):
        entries.append({
            "id": f"ios_device_{index}",
            "platform": "ios",
            "name": f"iOS {udid[:8]}",
            "serial": f"ios:{udid}",
            "status": "pending",
            "passed": 0,
            "failed": 0,
            "total": 0,
        })
    # Devices of a platform whose app didn't build: listed so the run shows
    # them with every test unavailable (record_setup_failure fills them in).
    for entry in build_failed or []:
        entries.append({
            "status": "pending",
            "passed": 0,
            "failed": 0,
            "total": 0,
            **entry,
        })
    if ios_placeholder:
        entries.append({
            "id": "ios_placeholder",
            "platform": "ios",
            "name": "iOS devices and simulators",
            "status": "not_supported",
            "passed": 0,
            "failed": 0,
            "total": 0,
            "completed": _ts(),
            "error": "iOS test execution is not supported yet.",
        })
    metadata = {
        "timestamp": _ts(),
        "status": "running",
        "passed": 0,
        "failed": 0,
        "total": 0,
        "active_device_id": None,
        "devices": entries,
    }
    if branch:
        metadata["branch"] = branch
    metadata.update(run_flags())
    with open(_root_metadata_path(), "w") as f:
        json.dump(metadata, f, indent=2)


def finish_multi_device_run(passed, failed, success):
    """Finalize aggregate metadata after all selected devices have run."""
    meta_path = _root_metadata_path()
    if not meta_path.exists():
        return
    with open(meta_path) as f:
        metadata = json.load(f)
    cancelled = sum(int(d.get("cancelled") or 0) for d in metadata.get("devices") or []
                    if isinstance(d, dict))
    unavailable = sum(int(d.get("unavailable") or 0) for d in metadata.get("devices") or []
                      if isinstance(d, dict))
    metadata.update({
        "status": "completed" if success else "completed_with_failures",
        "passed": passed,
        "failed": failed,
        "cancelled": cancelled,
        "unavailable": unavailable,
        "total": passed + failed + unavailable,
        "completed": _ts(),
        "active_device_id": None,
    })
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)


def mark_multi_device_failed(device_id, message):
    """Record a setup failure for a selected device without aborting other devices."""
    _update_root_device(
        device_id,
        status="failed",
        failed=1,
        total=1,
        completed=_ts(),
        error=message,
    )


CONSOLE_LOG = "console"  # result "log" value: the run's console log


def record_setup_failure(test_files, device_id, message, log_file=None):
    """Nothing could run on this device because setup failed: its app didn't
    build (e.g. signing), it is locked, or the app didn't install. Record every
    test as UNAVAILABLE with the reason and a log to look at, and mark the
    device unavailable. `log_file` is a build log to keep with the device; by
    default the run's console log is the log. Returns the number of tests."""
    activate_device_output(device_id)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    log = CONSOLE_LOG
    if log_file and os.path.isfile(str(log_file)):
        dest = LOGS_DIR / "setup_log.txt"
        shutil.copyfile(str(log_file), dest)
        log = os.path.relpath(dest, RUN_ROOT)
    pending_tests = []
    for i, tf in enumerate(test_files):
        steps = parse_test_file(tf)
        name = test_name_of(tf)
        results = [{"step": st, "status": "SKIPPED", "reason": "Not run: setup failed"} for st in steps]
        with open(RESULTS_DIR / f"{name}_result.json", "w") as f:
            json.dump({
                "test": name,
                "label": test_label(tf),
                "case": test_case_of(tf),
                "status": "UNAVAILABLE",
                "error": message,
                "log": log,
                "steps": results,
                "screenshots": [],
                "video": None,
                "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
            }, f, indent=2)
        pending_tests.append({
            "name": name,
            "label": test_label(tf),
            "case": test_case_of(tf),
            "order": i,
            "steps": [{k: st[k] for k in ('line', 'command', 'args', 'raw', 'block', 'error') if k in st}
                      for st in steps],
            "step_statuses": ["skipped"] * len(steps),
            "status": "unavailable",
            "reason": message,
        })
    with open(PROJECT_ROOT / "pending_tests.json", "w") as f:
        json.dump({"phase": "unavailable", "message": message, "tests": pending_tests}, f, indent=2)
    total = len(test_files)
    _update_root_device(device_id, status="unavailable", passed=0, failed=0, cancelled=0,
                        unavailable=total, total=total, completed=_ts(), error=message, log=log)
    print(f"  [{_ts()}] {device_id}: {total} test(s) UNAVAILABLE: {message}")
    return total


def activate_device_output(device_id):
    """Switch artifact output to a multi-device run's selected device."""
    _set_project_root(RUN_ROOT / "devices" / device_id)


def start_console_logging() -> None:
    """Open PROJECT_ROOT/console.log and tee stdout/stderr into it. Call after
    configure_output_dir() if a custom output base was given."""
    _open_console_log(PROJECT_ROOT / "console.log")
    _setup_tee()


def resolve_test_files(args) -> list:
    """Resolve the list of test files to run from args.test_files / args.test_file
    / args.test_dir. Exits the process on unresolvable input, matching the
    original inline behavior in run_on_device.py's main()."""
    test_files = []
    if args.test_files:
        raw = [f.strip() for f in args.test_files.split(',')]
        for p in raw:
            if os.path.isdir(p):
                test_files.extend(discover_tests_in_folder(p))
            elif os.path.isfile(p):
                test_files.append(p)
            else:
                test_dir = args.test_dir or '.'
                candidate = os.path.join(test_dir, p)
                if os.path.isdir(candidate):
                    test_files.extend(discover_tests_in_folder(candidate))
                elif os.path.isfile(candidate):
                    test_files.append(candidate)
                else:
                    print(f"ERROR: --test-files path not found: {p}")
                    sys.exit(1)
        if not test_files:
            print("ERROR: --test-files did not resolve to any test files")
            sys.exit(1)
    elif args.test_file:
        test_files = [args.test_file]
    else:
        global _multi_case_root
        test_dir = args.test_dir or '.'
        if not os.path.isdir(test_dir):
            print(f"ERROR: No test file specified and no valid --test-dir found")
            sys.exit(1)
        cases = find_case_folders(test_dir)
        if cases:
            # Multi-case run: every case folder, SETUP.txt order at the tests
            # root (name order for folders it doesn't name, or no SETUP.txt),
            # each with its own INSTRUCTION.txt (MODE, CLEAR_DATA) and
            # snapshots.
            _multi_case_root = Path(test_dir).resolve()
            all_txt = [tf for case in cases for tf in discover_tests_in_folder(str(case))]
            print(f"\n  [{_ts()}] Multi-case run: {', '.join(c.name for c in cases)}")
        else:
            all_txt = discover_tests_in_folder(test_dir)
        if not all_txt:
            print(f"ERROR: No test files found in {test_dir}")
            sys.exit(1)
        test_files = all_txt
        print(f"\n  [{_ts()}] Auto-discovered {len(test_files)} test file(s) in {test_dir}")

    print(f"  [{_ts()}] Resolved test files: {len(test_files)}")
    for tf in test_files:
        print(f"  [{_ts()}]   - {tf}")
    print()

    return test_files


def copy_test_files_from_dir(args, test_files) -> None:
    """Copy files from --test-dir into the local test dir if not already present."""
    if args.test_dir and test_files:
        local_test_dir = str(Path(test_files[0]).parent)
        print(f"  [{_ts()}] Copying test files from --test-dir: {args.test_dir}")
        for tf in test_files:
            src = os.path.join(args.test_dir, os.path.basename(tf))
            dst = os.path.join(local_test_dir, os.path.basename(tf))
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
                print(f"  [{_ts()}] Copied {os.path.basename(tf)} from --test-dir")
            elif os.path.exists(dst):
                print(f"  [{_ts()}] {os.path.basename(tf)} already exists locally")


TEST_SOURCES_DIR = "tests"  # <run>/tests/[<case>/]: the .txt files the run used


def save_test_sources(test_files) -> None:
    """Copy each test's .txt and its case's INSTRUCTION.txt into
    <run>/tests/ (<run>/tests/<case>/ in a multi-case run), so the run page
    shows the files exactly as this run used them. Best effort: a failed
    copy is logged and never stops the run."""
    root = RUN_ROOT / TEST_SOURCES_DIR
    for tf in test_files:
        src = Path(tf)
        case = test_case_of(tf)
        dest = root / case if case else root
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / src.name)
            instruction = src.parent / "INSTRUCTION.txt"
            if instruction.is_file() and not (dest / "INSTRUCTION.txt").exists():
                shutil.copyfile(instruction, dest / "INSTRUCTION.txt")
        except OSError as e:
            print(f"  [{_ts()}] WARNING: could not save test source {src}: {e}")


def build_test_files_list(args):
    """Build the instruction_files list for run metadata from --test-dir."""
    test_files_list = None
    if args.test_dir and os.path.isdir(args.test_dir):
        test_files_list = [
            f.name for f in sorted(Path(args.test_dir).glob('*.txt'))
            if not f.name.startswith('INSTRUCTION')
        ]
    return test_files_list


DEBUG_MODE = os.environ.get("DEBUG", "false").lower() == "true"
VARIABLES: dict = {}


def configured_variables(config_files=None, env_text=None, repo=None):
    """-> test variables from viewer Settings and the environment.

    Sources, in order of increasing priority: `test_variables` (a {NAME:
    value} JSON object) in `scripts/config.json` next to the runner (repo-
    shipped defaults, e.g. HUB_PATH empty), then the same key in the
    viewer's `Run/config.json` — Settings -> Test Variables saves the
    shared ones there and overrides the defaults — then the run's project
    entry in `project_variables` ({owner/repo: {NAME: value}}, picked by
    `repo`, default VT_REPO), then the `VT_TEST_VARS` env as the same map
    as a JSON string, which overrides all.
    Keys are upper-cased; a bad entry is skipped with a warning so a
    mistyped setting never breaks a run. A test file's own `KEY=VALUE`
    line still wins over both, since the step assigns it later.
    """
    if config_files is None:
        config_files = [
            Path(__file__).resolve().parent / "config.json",
            Path(__file__).resolve().parents[2] / "Run" / "config.json",
        ]
    if env_text is None:
        env_text = os.environ.get("VT_TEST_VARS", "")
    if repo is None:
        repo = run_repo()
    loaded = {}
    project = {}
    for cfg in config_files:
        try:
            with open(cfg) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        from_cfg = data.get("test_variables")
        if isinstance(from_cfg, dict):
            loaded.update(from_cfg)
        per_repo = data.get("project_variables")
        if repo and isinstance(per_repo, dict) and isinstance(per_repo.get(repo), dict):
            project.update(per_repo[repo])
    loaded.update(project)
    env_text = env_text.strip()
    if env_text:
        try:
            from_env = json.loads(env_text)
        except ValueError:
            shown = env_text if len(env_text) <= 120 else env_text[:120] + "..."
            print(f"WARNING: ignoring bad VT_TEST_VARS (not JSON): {shown}", file=sys.stderr)
            from_env = None
        if isinstance(from_env, dict):
            loaded.update(from_env)
    out = {}
    for key, value in loaded.items():
        key = str(key).strip().upper()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            print(f"WARNING: ignoring test variable {key!r}: bad name", file=sys.stderr)
            continue
        if not isinstance(value, str):
            print(f"WARNING: ignoring test variable {key!r}: value is not a string", file=sys.stderr)
            continue
        out[key] = value
    return out


VARIABLES.update(configured_variables())

# ── Visual model config fingerprinting ────────────────────────────────────
# Captured at startup so we can abort tests when settings change on disk.
_CONFIG_FINGERPRINT = None


def _visual_config_fingerprint():
    """Return a tuple of (endpoint, model_name, provider, plugin, plugin_url) from the config.
    
    Returns None if config is empty or unreadable.
    """
    _config_path = _OUTPUT_BASE / "config.json"
    if not _config_path.exists():
        return None
    try:
        data = json.loads(_config_path.read_text())
        return (
            (data.get("visual_endpoint") or "").strip(),
            (data.get("visual_model") or "").strip(),
            (data.get("visual_provider") or "local").strip(),
            (data.get("visual_plugin") or "").strip(),
            (data.get("visual_plugin_url") or "").strip().rstrip("/"),
        )
    except Exception:
        return None


def capture_visual_config_fingerprint():
    """Capture the current visual model config as a fingerprint.
    
    Call this once at test runner startup (after reading config.json).
    """
    global _CONFIG_FINGERPRINT
    _CONFIG_FINGERPRINT = _visual_config_fingerprint()


def check_visual_config():
    """Check if the visual model config on disk differs from the captured fingerprint.
    
    Raises ConfigChangedError when a mismatch is detected.
    """
    global _CONFIG_FINGERPRINT
    current = _visual_config_fingerprint()
    if current != _CONFIG_FINGERPRINT:
        old_ep = _CONFIG_FINGERPRINT[0] if _CONFIG_FINGERPRINT else None
        new_ep = current[0] if current else None
        raise ConfigChangedError(
            f"Visual model config changed: old endpoint={old_ep!r}, new endpoint={new_ep!r}"
        )


class ConfigChangedError(Exception):
    """Raised when the visual model config on disk differs from startup."""
    pass


def _write_json_atomic(path, data):
    """Write JSON as a complete replacement so polling clients never see partial data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp.write("\n")
        temp_name = tmp.name
    Path(temp_name).replace(path)


def _load_pending_data(pending_file):
    if not pending_file.exists():
        return {"tests": [], "run_start": _ts()}
    with open(pending_file) as f:
        return json.load(f)


def initialize_run_progress(phase, message):
    """Create early run state before inputs are resolved or tests are known."""
    pending_file = PROJECT_ROOT / "pending_tests.json"
    data = _load_pending_data(pending_file)
    data.setdefault("tests", [])
    data.setdefault("run_start", _ts())
    data["progress"] = {
        "phase": phase,
        "message": message,
        "updated_at": _ts(),
    }
    _write_json_atomic(pending_file, data)
    
    # Also update run_metadata.json so failed runs are visible on the dashboard
    if phase == "failed":
        meta_path = OUTPUTS_DIR.parent / "run_metadata.json"
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                meta["status"] = "failed"
                meta["error"] = message
                meta["completed"] = _ts()
                OUTPUTS_DIR.parent.mkdir(parents=True, exist_ok=True)
                with open(meta_path, "w") as f:
                    json.dump(meta, f, indent=2)
            except Exception:
                pass


def update_run_progress(
    pending_file,
    phase,
    message,
    *,
    test_name=None,
    test_index=None,
    test_total=None,
    step=None,
    step_index=None,
    step_total=None,
    step_status=None,
):
    """Persist the runner's current activity and optional per-step status."""
    data = _load_pending_data(pending_file)
    previous = data.get("progress", {})
    progress = {
        "phase": phase,
        "message": message,
        "updated_at": _ts(),
    }
    if test_name is not None:
        progress["test_name"] = test_name
    if test_index is not None:
        progress["test_index"] = test_index
    elif previous.get("test_name") == test_name and previous.get("test_index") is not None:
        progress["test_index"] = previous["test_index"]
    if test_total is not None:
        progress["test_total"] = test_total
    elif previous.get("test_name") == test_name and previous.get("test_total") is not None:
        progress["test_total"] = previous["test_total"]
    if step is not None:
        progress["step"] = step
    if step_index is not None:
        progress["step_index"] = step_index
    if step_total is not None:
        progress["step_total"] = step_total
    data["progress"] = progress

    if test_name is not None:
        for test in data.get("tests", []):
            if test.get("name") != test_name:
                continue
            if step_index is not None and step_status is not None:
                statuses = test.setdefault(
                    "step_statuses", ["pending"] * len(test.get("steps", []))
                )
                if 0 <= step_index - 1 < len(statuses):
                    statuses[step_index - 1] = step_status
            break
    _write_json_atomic(pending_file, data)


def _update_pending_status(pending_file, test_name, new_status):
    """Update a test status in pending_tests.json without losing progress."""
    data = _load_pending_data(pending_file)
    for test in data.get("tests", []):
        if test.get("name") == test_name:
            test["status"] = new_status
            break
    _write_json_atomic(pending_file, data)


def debug_print(msg):
    """Print debug-only output to stderr with timestamp."""
    if DEBUG_MODE:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"  [{ts}] [DEBUG] {msg}", file=sys.stderr, flush=True)


def resolve_variables(text):
    """Replace ${VAR} and $VAR references with stored values."""
    def replacer(match):
        var_name = match.group(1) or match.group(2)
        value = VARIABLES.get(var_name)
        if value is not None:
            debug_print(f"Substituting ${{{var_name}}} -> {value}")
            return value
        debug_print(f"WARNING: Variable not defined: ${match.group(0)}")
        return match.group(0)

    # Log variable resolution if any variables are present
    has_vars = '$' in text
    if has_vars:
        debug_print(f"Resolving variables in: {text}")

    result = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)', replacer, text)

    if has_vars and result != text:
        debug_print(f"Variable resolution result: {result}")

    return result


def _ts():
    """Return current timestamp string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _once_state_path():
    """Path to the temp file tracking once-mode instructions."""
    return str(OUTPUTS_DIR / ".once_instructions_run")


def _load_once_state():
    """Load set of already-run once-mode instruction paths."""
    path = _once_state_path()
    if os.path.exists(path):
        with open(path) as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def _save_once_state(state):
    """Persist set of already-run once-mode instruction paths."""
    path = _once_state_path()
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    with open(path, "w") as f:
        for p in sorted(state):
            f.write(p + "\n")


def load_run_metadata():
    """Load run metadata from run_metadata.json in the output directory.

    Returns dict with APK, device/emulator, vision model, and options info.
    Returns empty dict if metadata file not found.
    """
    meta_path = OUTPUTS_DIR.parent / "run_metadata.json"
    if not meta_path.exists():
        return {}
    try:
        with open(meta_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def discover_tests_in_folder(folder_path):
    """Discover all .txt test files in a folder, excluding setup files.

    INSTRUCTION.txt is a case's setup; SETUP.txt is the run-wide suite order
    at the tests root (never a test either)."""
    pattern = os.path.join(folder_path, "*.txt")
    all_txt = sorted(glob.glob(pattern))
    # Exclude INSTRUCTION.txt and SETUP.txt (setup, not a test case)
    test_files = [f for f in all_txt
                  if not os.path.basename(f).startswith("INSTRUCTION")
                  and os.path.basename(f) != "SETUP.txt"]
    return test_files


INSTRUCTION_HEADER_KEYS = ('mode', 'clear_data', 'reinstall_app')


def parse_instruction_options(content):
    """Parse the header of an INSTRUCTION.txt.

    The header is the run of ``KEY: value`` lines at the top of the file (any
    order, comments and blank lines allowed) before the first setup step:

    - ``MODE: once | everytime`` (default ``everytime``): run the setup steps
      only for the first test of the folder, or before every test.
    - ``CLEAR_DATA: first | everytime``: before each test the runner
      restarts the app; ``everytime`` clears app data before every test of
      the folder, ``first`` only before its first test (later tests just
      relaunch the app so what an earlier test did, e.g. accepting the EULA,
      carries over). Without the key: ``everytime`` for a single-case run and
      the first case of a multi-case run; a later case keeps the app data the
      previous case left (like iOS), unless it sets REINSTALL_APP: true. iOS
      can't clear app data, so this changes nothing there.
    - ``REINSTALL_APP: true | false`` (default ``false``): in a multi-case run
      each case after the first continues with the app the run already
      installed. ``true`` starts that case from a reinstalled app (uninstall +
      install the run's bundle), the way to get a clean state (accepted EULA,
      fresh onboarding) on iOS; works on Android too.

    Returns ``({'mode': ..., 'clear_data': ..., 'reinstall_app': ...},
    steps_content)``.
    """
    options, body, _ = _parse_instruction_header_keys(content)
    return options, body


def _parse_instruction_header_keys(content):
    """parse_instruction_options plus the set of header keys the file sets."""
    options = {'mode': 'everytime', 'clear_data': 'everytime',
               'reinstall_app': False}
    keys = set()
    body = []
    in_header = True
    for line in content.strip().split('\n'):
        stripped = line.strip()
        if in_header:
            if not stripped or stripped.startswith('#'):
                body.append(line)
                continue
            key, sep, value = stripped.partition(':')
            key = key.strip().lower()
            if sep and key in INSTRUCTION_HEADER_KEYS:
                keys.add(key)
                value = value.strip().lower()
                if key == 'mode':
                    options['mode'] = 'once' if 'once' in value else 'everytime'
                elif key == 'clear_data':
                    options['clear_data'] = 'first' if value == 'first' else 'everytime'
                else:
                    options['reinstall_app'] = value in ('true', 'yes')
                continue
            in_header = False
        body.append(line)
    return options, '\n'.join(body), keys


# Root folder of a multi-case run (tests_dir/<case>/INSTRUCTION.txt + tests),
# set by resolve_test_files. None for a single-case run (tests directly in the
# test dir), where a test's name is just its file stem.
_multi_case_root = None


def read_setup_order(test_dir):
    """The case order named by SETUP.txt at the tests root, or None.

    One case folder name per line; blank lines and # comments allowed, a
    name listed twice counts once (first position). No SETUP.txt -> None,
    and the folders keep their plain name order."""
    path = Path(test_dir) / "SETUP.txt"
    if not path.is_file():
        return None
    names, seen = [], set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and stripped not in seen:
            seen.add(stripped)
            names.append(stripped)
    return names


def find_case_folders(test_dir):
    """Case folders directly under test_dir (each holds an INSTRUCTION.txt).

    Run order: SETUP.txt at test_dir's root lists case names, one per line,
    and fixes the order; folders it doesn't name run after the listed ones,
    in name order. Without SETUP.txt the folders run in name order. Empty
    when test_dir is itself a single case."""
    root = Path(test_dir)
    if not root.is_dir() or discover_tests_in_folder(str(root)):
        return []
    by_name = {d.name: d for d in root.iterdir()
               if d.is_dir() and (d / "INSTRUCTION.txt").is_file()}
    listed = read_setup_order(test_dir)
    if listed:
        unknown = [n for n in listed if n not in by_name]
        if unknown:
            print(f"\n  [{_ts()}] WARNING: SETUP.txt names cases not on disk: {', '.join(unknown)}")
        ordered = [by_name[n] for n in listed if n in by_name]
        ordered += [by_name[n] for n in sorted(by_name) if n not in set(listed)]
        return ordered
    return [by_name[n] for n in sorted(by_name)]


def test_case_of(test_path):
    """Case folder name of a test in a multi-case run, else None."""
    p = Path(test_path)
    if _multi_case_root is not None and p.parent.parent.resolve() == _multi_case_root:
        return p.parent.name
    return None


def test_name_of(test_path):
    """Unique name of a test inside a run, used for result files, videos and
    the run page: ``<case>__<file stem>`` in a multi-case run, else the stem."""
    case = test_case_of(test_path)
    stem = Path(test_path).stem
    return f"{case}__{stem}" if case else stem


def _test_outputs_dir():
    """Where the current test's named outputs (snapshot captures)
    go: outputs/<case>/ in a multi-case run, so equal names in different cases
    don't overwrite each other; plain outputs/ otherwise."""
    if _current_test_dir is not None:
        case = test_case_of(Path(_current_test_dir) / "x.txt")
        if case:
            d = OUTPUTS_DIR / case
            d.mkdir(parents=True, exist_ok=True)
            return d
    return OUTPUTS_DIR


def test_label(test_path):
    """Display label: ``<case> / <file stem>`` in a multi-case run."""
    case = test_case_of(test_path)
    stem = Path(test_path).stem
    return f"{case} / {stem}" if case else stem


def parse_instruction_header(content):
    """Parse MODE header from INSTRUCTION.txt.

    Returns (mode, steps_content) where mode is 'once' or 'everytime'; other
    header keys (see parse_instruction_options) are dropped from the steps.
    """
    options, steps_content = parse_instruction_options(content)
    return options['mode'], steps_content


def instruction_clear_data(test_dir):
    """The CLEAR_DATA option of a test folder's INSTRUCTION.txt
    ('everytime' when the file or the key is missing)."""
    path = os.path.join(str(test_dir), "INSTRUCTION.txt")
    try:
        with open(path) as f:
            return parse_instruction_options(f.read())[0]['clear_data']
    except OSError:
        return 'everytime'


def instruction_sets_clear_data(test_dir):
    """Whether a test folder's INSTRUCTION.txt has a CLEAR_DATA header key."""
    path = os.path.join(str(test_dir), "INSTRUCTION.txt")
    try:
        with open(path) as f:
            return 'clear_data' in _parse_instruction_header_keys(f.read())[2]
    except OSError:
        return False


def instruction_reinstall_app(test_dir):
    """The REINSTALL_APP option of a test folder's INSTRUCTION.txt
    (False when the file or the key is missing)."""
    path = os.path.join(str(test_dir), "INSTRUCTION.txt")
    try:
        with open(path) as f:
            return parse_instruction_options(f.read())[0]['reinstall_app']
    except OSError:
        return False


# (output dir, test folder) pairs whose first test already started from
# cleared app data. Keyed by output dir so each device of a multi-device run
# gets its own clean first test.
_cleared_test_dirs = set()
# First case started per output dir (device) in a multi-case run.
_first_case_by_output = {}


def reinstall_for_new_case(test_path):
    """In a multi-case run each case after the first continues with the app
    the run already installed, on every platform. Set REINSTALL_APP: true in
    a case's INSTRUCTION.txt to start that case from a reinstalled app
    (uninstall + install the run's bundle) — the clean-state lever on iOS
    (no clear-data there), honoured on Android too.
    Returns False only when the reinstall was requested and failed."""
    case = test_case_of(test_path)
    key = str(PROJECT_ROOT)
    previous = _last_case_by_output.get(key)
    _last_case_by_output[key] = case
    if not case or previous is None or previous == case:
        return True
    if not instruction_reinstall_app(Path(test_path).parent):
        return True
    if not (_app_under_test and _app_path_under_test and os.path.exists(str(_app_path_under_test))):
        print(f"  [{_ts()}] WARNING: can't reinstall the app for case {case}: app file unknown")
        return True
    print(f"\n  [{_ts()}] NEW CASE {case}: REINSTALL_APP requested, reinstalling the app")
    controller.uninstall_app(_app_under_test)
    time.sleep(2)
    if not controller.install_apk(str(_app_path_under_test)):
        print(f"  [{_ts()}] ERROR: reinstall failed")
        return False
    return True


def keeps_previous_case_data(test_path):
    """True when this test belongs to a case after the run's first one whose
    INSTRUCTION.txt sets neither CLEAR_DATA nor REINSTALL_APP: true. Such a
    case continues from the app data the previous case left, on Android as
    on iOS, so a suite like onboarding -> device-setup can chain."""
    case = test_case_of(test_path)
    if not case:
        return False
    first = _first_case_by_output.setdefault(str(PROJECT_ROOT), case)
    if case == first:
        return False
    test_dir = Path(test_path).parent
    return not (instruction_sets_clear_data(test_dir)
                or instruction_reinstall_app(test_dir))


def should_clear_app_data(test_path):
    """Whether the restart before this test clears app data (CLEAR_DATA)."""
    if keeps_previous_case_data(test_path):
        return False
    test_dir = os.path.abspath(str(Path(test_path).parent))
    if instruction_clear_data(test_dir) != 'first':
        return True
    key = (str(PROJECT_ROOT), test_dir)
    if key in _cleared_test_dirs:
        return False
    _cleared_test_dirs.add(key)
    return True


def run_instructions(test_path):
    """Find and execute INSTRUCTION.txt steps before running a test.

    Supports MODE: once | MODE: everytime (default) header.
    Once-mode instructions run only the first time a test folder is encountered.

    Looks for INSTRUCTION.txt in the same directory as the test file.
    If found, executes its setup steps before the main test.
    If missing, aborts the test — INSTRUCTION.txt is required setup.
    INSTRUCTION.txt is NOT a test case; only .txt test files are test cases.

    Returns True if INSTRUCTION.txt was found and executed, False if missing.
    """
    test_dir = str(Path(test_path).parent)
    instruction_path = os.path.join(test_dir, "INSTRUCTION.txt")

    print(f"  [{_ts()}] Checking for INSTRUCTION.txt in: {test_dir}")
    if not os.path.exists(instruction_path):
        print(f"\n  [{_ts()}] FATAL: INSTRUCTION.txt not found in {test_dir}")
        print(f"  [{_ts()}] INSTRUCTION.txt is required test setup. Aborting.")
        return False

    print(f"  [{_ts()}] Found INSTRUCTION.txt: {instruction_path}")

    # Read file to check mode
    with open(instruction_path) as f:
        content = f.read()

    mode, steps_content = parse_instruction_header(content)
    print(f"  [{_ts()}] Instruction mode: {mode}")

    if mode == 'once':
        state = _load_once_state()
        abs_path = os.path.abspath(instruction_path)
        if abs_path in state:
            print(f"\n  [{_ts()}] INSTRUCTION (once) skipped: {instruction_path} (already run)")
            return True
        print(f"  [{_ts()}] First time running once-mode instructions, recording state")
        state.add(abs_path)
        _save_once_state(state)

    print(f"\n  [{_ts()}] FOUND INSTRUCTIONS ({mode}): {instruction_path}")

    # Parse steps from content (skip MODE header)
    steps = parse_steps(steps_content.strip().split('\n'))

    print(f"  [{_ts()}] Executing {len(steps)} setup step(s) for {mode} mode...")
    pending_file = PROJECT_ROOT / "pending_tests.json"
    test_name = test_name_of(test_path)
    for step_index, step in enumerate(steps, 1):
        update_run_progress(
            pending_file,
            "running_setup",
            f"Running setup step {step_index} of {len(steps)}",
            test_name=test_name,
            step=step,
            step_index=step_index,
            step_total=len(steps),
        )
        print(f"  [{_ts()}] [{step['line']}] SETUP STEP: {step['raw']}")
        skip = step_skipped_on_platform(step)
        if skip:
            print(f"  [{_ts()}] -> SKIPPED: {skip}")
            continue
        success = execute_step(step)
        if not success:
            print(f"  [{_ts()}] WARNING: Setup step failed at line {step['line']}, continuing...")
        else:
            print(f"  [{_ts()}] -> Setup step {step['line']} complete")
    print(f"  [{_ts()}] Setup complete")
    return True


BLOCK_END = "END"
IF_BLOCK_OPEN = re.compile(r'IF\s+(IOS|ANDROID):$', re.IGNORECASE)
IF_BLOCK_CLOSE = re.compile(r'END\s+IF(?:\s+(IOS|ANDROID))?:?$', re.IGNORECASE)


def _indent(line):
    return len(line) - len(line.lstrip())


FILE_REF_RE = re.compile(r'@file:(\S*)')


def _resolve_line_file_refs(line, test_dir, line_num):
    """Anchor `@file:<path>` refs in one text line to `test_dir` (absolute).

    A test may only use files inside its own directory: `<path>` must be
    relative to the test file's directory and becomes the absolute normpath of
    test_dir + path. An absolute path, a ref with no path, or one that resolves
    outside test_dir (lexically, or through a symlink once the file exists)
    gives an error message naming line_num. Returns (new_line, error or None);
    the line is returned unchanged when any ref is malformed."""
    if "@file:" not in line:
        return line, None
    error = None
    real_dir = os.path.realpath(test_dir)

    def repl(match):
        nonlocal error
        path = match.group(1)
        if not path:
            if error is None:
                error = f"@file: at line {line_num} has no path after it"
            return match.group(0)
        if os.path.isabs(path):
            if error is None:
                error = (f"@file:{path} at line {line_num} is an absolute path; @file: paths "
                         f"must be relative to the test file's directory ({test_dir})")
            return match.group(0)
        resolved = os.path.normpath(os.path.join(test_dir, path))
        real = os.path.realpath(resolved)
        if (os.path.commonpath([resolved, test_dir]) != test_dir
                or os.path.commonpath([real, real_dir]) != real_dir):
            if error is None:
                error = (f"@file:{path} at line {line_num} resolves outside the test "
                         f"file's directory ({test_dir})")
            return match.group(0)
        return "@file:" + resolved

    new_line = FILE_REF_RE.sub(repl, line)
    return (line, error) if error else (new_line, None)


def _resolve_script_file_refs(args, test_dir, line_num):
    """Rewrite `@file:` refs inside a PLUGIN step's `script=` param value.

    Only the script= value is rewritten — the plugin expands refs there —
    refs in other params pass through for the plugin to see as-is. Returns
    (args, error or None); args is only rebuilt (via shlex) when a ref
    actually changed."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        return args, None  # parse_plugin_args reports the quoting at run time
    changed = False
    for idx, token in enumerate(tokens):
        key, sep, value = token.partition("=")
        if not sep or key != "script":
            continue
        value, error = _resolve_line_file_refs(value, test_dir, line_num)
        if error:
            return args, error
        tokens[idx] = "script=" + value
        changed = True
    return (shlex.join(tokens), None) if changed else (args, None)


def parse_steps(lines, test_dir=None):
    """Turn test-file lines into steps: {line, command, args, raw}.

    Blank and `#` lines are skipped. A PLUGIN step whose next non-blank line is
    indented deeper than the step carries an inline block: the lines up to a
    line that is exactly END. Block lines are stripped, blank ones dropped, and
    `#` lines kept; they are stored in step['block'] and never parsed as steps.
    A block without END, or an END outside a block, gives a step with an
    'error' key, which fails when it is executed (see execute_step).

    A step may start with `IF IOS:` or `IF ANDROID:` (e.g. `IF IOS: CLICK: Allow`):
    it runs only on that platform and is skipped on the other one; the
    platform is stored in step['platform'] (see step_skipped_on_platform).

    Several steps can share one condition: a line that is exactly `IF IOS:` or
    `IF ANDROID:` opens a block and `END IF` (optionally `END IF <platform>` /
    `END IF ANDROID`, which must match) closes it; every step in between gets
    that platform. Blocks don't nest. An unclosed block, a stray `END IF`, a
    mismatched `END IF <platform>` or a step inside the block prefixed for the
    other platform gives a step with an 'error' key (no platform, so it fails
    on every device).

    When `test_dir` (an absolute directory — the test file's directory, see
    parse_test_file) is given, `@file:<path>` refs inside a PLUGIN step's block
    lines and `script=` param are anchored to it at parse time: a relative path
    must be relative and is rewritten to an absolute one inside `test_dir`; an
    absolute path, a ref with no path, or one that resolves outside `test_dir`
    (also through a symlink) gives a step with an 'error' key naming the
    offending line. Without `test_dir`, `@file:`
    text is left verbatim. Nothing else about block lines is interpreted —
    the plugin expands `@file:`; the firmware never sees it."""
    lines = [line.rstrip('\r\n') for line in lines]
    steps = []
    block_platform = None  # platform of the open IF block, if any
    block_line = None
    i = 0
    while i < len(lines):
        line_num, original = i + 1, lines[i]
        line = original.strip()
        i += 1
        if not line or line.startswith('#'):
            continue
        opener = IF_BLOCK_OPEN.match(line)
        if opener:
            if block_platform:
                steps.append({'line': line_num, 'command': 'IF', 'args': '', 'raw': line,
                              'error': (f"{line} at line {line_num} is inside the IF block opened "
                                        f"at line {block_line}; IF blocks don't nest")})
            else:
                block_platform, block_line = opener.group(1).lower(), line_num
            continue
        closer = IF_BLOCK_CLOSE.match(line)
        if closer:
            named = (closer.group(1) or '').lower()
            if not block_platform:
                error = f"{line} at line {line_num} does not close an IF block"
            elif named and named != block_platform:
                error = (f"{line} at line {line_num} does not match the IF "
                         f"{block_platform.upper()}: block opened at line {block_line}")
                block_platform = block_line = None  # closed anyway: report one error
            else:
                block_platform = block_line = None
                continue
            steps.append({'line': line_num, 'command': 'END', 'args': 'IF', 'raw': line,
                          'error': error})
            continue
        if line == BLOCK_END:
            steps.append({'line': line_num, 'command': BLOCK_END, 'args': '', 'raw': line,
                          'error': f"END at line {line_num} does not close a PLUGIN block"})
            continue
        platform = None
        prefix = re.match(r'IF\s+(IOS|ANDROID):\s*(\S.*)$', line, re.IGNORECASE)
        if prefix:
            platform, body = prefix.group(1).lower(), prefix.group(2)
        else:
            body = line
        parts = body.split(None, 1)
        cmd = parts[0].rstrip(':').upper()
        args = parts[1] if len(parts) > 1 else ""
        step = {'line': line_num, 'command': cmd, 'args': args, 'raw': line}
        if block_platform and platform and platform != block_platform:
            step['error'] = (f"Line {line_num} is for {platform.upper()} but sits inside the "
                             f"IF {block_platform.upper()}: block opened at line {block_line}")
        elif platform or block_platform:
            step['platform'] = platform or block_platform
        if cmd == "PLUGIN" and test_dir and "@file:" in args:
            resolved_args, file_ref_error = _resolve_script_file_refs(args, test_dir, line_num)
            if file_ref_error:
                step.setdefault('error', file_ref_error)
            else:
                step['args'] = resolved_args
        steps.append(step)
        if cmd != "PLUGIN":
            continue
        nxt = i
        while nxt < len(lines) and not lines[nxt].strip():
            nxt += 1
        if nxt >= len(lines):
            continue
        if lines[nxt].strip() != BLOCK_END and _indent(lines[nxt]) <= _indent(original):
            continue  # no block: the next line is a step at the same level
        block = []
        while i < len(lines) and lines[i].strip() != BLOCK_END:
            if lines[i].strip():
                block_line = lines[i].strip()
                if test_dir:
                    block_line, file_ref_error = _resolve_line_file_refs(block_line, test_dir, i + 1)
                    if file_ref_error:
                        step.setdefault('error', file_ref_error)
                block.append(block_line)
            i += 1
        step['block'] = block
        if i >= len(lines):
            step['error'] = (f"PLUGIN block starting at line {line_num} has no END line "
                             f"(a line that is exactly END)")
        else:
            i += 1  # the END line
    if block_platform:
        steps.append({'line': block_line, 'command': 'IF', 'args': '',
                      'raw': f"IF {block_platform.upper()}:",
                      'error': (f"IF {block_platform.upper()}: block starting at line {block_line} "
                                f"has no END IF line")})
    return steps


def step_skipped_on_platform(step):
    """The skip reason when `step` is for another platform (`IF IOS:` /
    `IF ANDROID:` prefix), else None."""
    platform = step.get('platform')
    if not platform or platform == _device_platform():
        return None
    return f"Only runs on {'iOS' if platform == 'ios' else 'Android'}"


def parse_test_file(test_path):
    """Parse a test step file and return list of steps (see parse_steps)."""
    print(f"  [{_ts()}] Parsing test file: {test_path}")
    test_dir = os.path.normpath(os.path.dirname(os.path.abspath(str(test_path))))
    with open(test_path) as f:
        steps = parse_steps(f.read().split('\n'), test_dir=test_dir)
    print(f"  [{_ts()}] Parsed {len(steps)} step(s) from test file")
    for s in steps:
        print(f"  [{_ts()}]   [{s['line']}] {s['raw']}")
        for block_line in s.get('block', []):
            print(f"  [{_ts()}]         {block_line}")
        if s.get('error'):
            print(f"  [{_ts()}]   PARSE ERROR: {s['error']}")
    return steps


# Device plugins from VT_PLUGINS, loaded on the first PLUGIN step so each
# plugin's /health and /actions are fetched once per run.
_plugins = None


def _get_plugins():
    global _plugins
    if _plugins is None:
        _plugins = plugin_client.load_plugins_from_env()
    return _plugins


def parse_plugin_args(args):
    """`<plugin> <action> [key=value ...] [device=<id>] [timeout=<seconds>]` ->
    (plugin, action, params, device, timeout_s). Raises ValueError."""
    try:
        tokens = shlex.split(args)
    except ValueError as e:
        raise ValueError(f"cannot parse PLUGIN arguments: {e}")
    if len(tokens) < 2:
        raise ValueError("PLUGIN needs <plugin> <action> [key=value ...]")
    plugin, action = tokens[0], tokens[1]
    params, device, timeout_s = {}, None, None
    for token in tokens[2:]:
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise ValueError(f"PLUGIN argument {token!r} is not key=value")
        if key == "device":
            device = value or None
        elif key == "timeout":
            try:
                timeout_s = float(value)
            except ValueError:
                raise ValueError(f"PLUGIN timeout={value!r} is not a number of seconds")
            if timeout_s <= 0:
                raise ValueError("PLUGIN timeout must be more than 0 seconds")
        else:
            params[key] = value
    return plugin, action, params, device, timeout_s


def _run_plugin_step(step, args):
    """PLUGIN step: run an action on a device plugin; passes only on `passed`."""
    try:
        plugin, action, params, device, timeout_s = parse_plugin_args(args)
    except ValueError as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        return False
    try:
        plugins = _get_plugins()
    except plugin_client.PluginError as e:
        print(f"  [{_ts()}] -> ERROR: {e.message}")
        return False
    client = plugins.get(plugin)
    if client is None:
        known = ", ".join(sorted(plugins)) or "none (set VT_PLUGINS=name=url)"
        print(f"  [{_ts()}] -> ERROR: Unknown plugin '{plugin}'. Configured plugins: {known}")
        return False
    block = step.get('block')
    if block is not None:
        if "script" in params:
            print(f"  [{_ts()}] -> ERROR: PLUGIN step at line {step['line']} has both an inline "
                  f"block and script= on the step line; use one")
            return False
        try:
            client.ensure_compatible()
            schema = client.action(action).get("params") or {}
        except plugin_client.PluginError as e:
            print(f"  [{_ts()}] -> ERROR: {e}")
            return False
        if "script" not in schema:
            print(f"  [{_ts()}] -> ERROR: {plugin} action '{action}' takes no inline block "
                  f"(its params have no 'script'); remove the block or use another action")
            return False
        block = [resolve_variables(line) for line in block]
        params["script"] = "\n".join(block)
    where = f" on {device}" if device else ""
    shown = {k: v for k, v in params.items() if not (block is not None and k == "script")}
    print(f"  [{_ts()}] [{step['line']}] PLUGIN {plugin} {action}{where} "
          f"params={json.dumps(shown)}" + (f" timeout={timeout_s:g}s" if timeout_s else ""))
    if block is not None:
        print(f"  [{_ts()}]   script ({len(block)} line{'s' if len(block) != 1 else ''}):")
        for line in block:
            print(f"  [{_ts()}]       {line}")

    def on_log(line):
        print(f"  [{_ts()}]   [{plugin}] {line}")

    try:
        job = client.run(action, params, device=device, timeout_s=timeout_s, on_log=on_log)
    except plugin_client.PluginError as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        return False
    state = job.get("state")
    print(f"  [{_ts()}] -> PLUGIN job {job.get('id')} {state}")
    if job.get("result") is not None:
        print(f"  [{_ts()}] -> Result: {json.dumps(job['result'])}")
    error = job.get("error")
    if error:
        if isinstance(error, dict):
            print(f"  [{_ts()}] -> Error: {error.get('message')} ({error.get('code')})")
        else:
            print(f"  [{_ts()}] -> Error: {error}")
    return state == "passed"


def _rel_to_project(path):
    return os.path.relpath(path, PROJECT_ROOT) if path else None


def _run_snapshot_step(step, args):
    """SNAPSHOT: compare the screen with the case's baseline (see snapshots.py).
    A missing baseline fails, like a mismatch; record mode saves and passes."""
    global _step_detail
    try:
        name, masks, tolerance = snapshots.parse_args(args)
    except snapshots.SnapshotError as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        return False
    detail = {'name': name, 'status': 'error'}
    _step_detail = {'snapshot': detail}
    try:
        screen = controller.get_screen_info()
        elements = None
        if any(m[0] == 'tag' for m in masks):
            elements = controller.get_elements()
        capture = _test_outputs_dir() / "snapshots" / f"{name}.capture.png"
        capture.parent.mkdir(parents=True, exist_ok=True)
        if not controller.screenshot(capture):
            raise RuntimeError("screenshot capture failed")
        root = Path(_current_test_dir or ".") / "snapshots"
        result = snapshots.take(
            name, capture_png=capture.read_bytes(), platform=_device_platform(),
            scale=screen['scale'], snapshot_root=root, out_dir=capture.parent,
            masks=masks, tolerance=tolerance, elements=elements,
            record=snapshots.record_mode())
        capture.unlink(missing_ok=True)
    except (RuntimeError, snapshots.SnapshotError, OSError) as e:
        detail['message'] = str(e)
        print(f"  [{_ts()}] -> SNAPSHOT ERROR: {e}")
        return False
    for key in ('actual', 'baseline', 'diff'):
        result[key] = _rel_to_project(result[key])
    detail.update(result)
    print(f"  [{_ts()}] -> SNAPSHOT {name} [{result['screen_class']}]: "
          f"{result['status'].upper()} - {result['message']}")
    return result['status'] in ('match', 'recorded')


def _run_layout_step(step, args):
    """ASSERT_LAYOUT: check tagged element positions (see layout.py)."""
    global _step_detail
    try:
        layout.parse(args)
        screen = controller.get_screen_info()
        elements = controller.get_elements()
        passed, message = layout.check(args, screen, elements)
    except (layout.LayoutError, RuntimeError) as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        _step_detail = {'layout': {'passed': False, 'message': str(e)}}
        return False
    _step_detail = {'layout': {'passed': passed, 'message': message}}
    print(f"  [{_ts()}] -> LAYOUT {'OK' if passed else 'FAILED'}: {message}")
    return passed


def execute_step(step):
    """Execute a single test step."""
    cmd = step['command']
    args = step['args']
    raw_cmd = step.get('raw', f"{cmd} {args}")

    # Log the raw line from the test file
    print(f"  [{_ts()}] [{step['line']}] STEP START: {raw_cmd}")

    if step.get('error'):
        print(f"  [{_ts()}] -> PARSE ERROR: {step['error']}")
        return False

    # Substitute variables in command and args
    cmd = resolve_variables(cmd)
    args = resolve_variables(args)

    # Log after variable resolution so you can see what actually gets executed
    if cmd != step['command'] or args != step['args']:
        print(f"  [{_ts()}] [{step['line']}] AFTER VAR RESOLUTION: {cmd}: {args}")
    else:
        print(f"  [{_ts()}] [{step['line']}] EXECUTING: {cmd}: {args}")

    if cmd == "START_RECORDING":
        # Args: optional <remote_path_or_EXTERNAL_CAPTURE>,time_limit (e.g. "EXTERNAL_CAPTURE,30")
        # The first field is accepted but ignored.
        debug_print(f"Starting video recording with timeout from args: {args}")
        parts = args.strip().split(",") if args.strip() else []
        time_limit = int(parts[1]) if len(parts) > 1 and parts[1].strip() else None
        limit_text = f"{time_limit}s" if time_limit else "none"
        debug_print(f"Recording timeout: {limit_text}")
        print(f"  [{_ts()}] [{step['line']}] STARTING VIDEO RECORDING (timeout: {limit_text})")
        success = controller.start_recording(time_limit=time_limit)
        if success:
            print(f"  [{_ts()}] -> Video recording started")
        else:
            print(f"  [{_ts()}] -> ERROR: Failed to start video recording")
        return success

    elif cmd == "STOP_RECORDING":
        # Args: optional <local_filename>, accepted but ignored — output goes to CAPTURE_OUTPUT_DIR.
        debug_print(f"Stopping video recording")
        print(f"  [{_ts()}] [{step['line']}] STOPPING VIDEO RECORDING")
        video_path = controller.stop_recording()
        if video_path:
            print(f"  [{_ts()}] -> Video recording stopped: {video_path}")
            return True
        else:
            print(f"  [{_ts()}] -> ERROR: No active recording to stop")
            return False

    elif cmd == "VERIFY":
        description = args.strip()
        debug_print(f"VERIFY: Looking for '{description}' on screen")
        current_screenshot = OUTPUTS_DIR / "current.png"
        if not controller.screenshot(current_screenshot):
            print(f"  [{_ts()}] -> ERROR: Screenshot capture failed — check the device (DEVICE_ID={os.environ.get('DEVICE_ID', 'not set')})")
            return False
        # Check if visual model config changed before calling API
        try:
            check_visual_config()
        except ConfigChangedError as e:
            print(f"\n  [{_ts()}] *** VISUAL MODEL CONFIG CHANGED - ABORTING ***")
            print(f"  [{_ts()}] {e}")
            raise
        debug_print(f"Calling vision model to verify: '{description}'")
        print(f"  [{_ts()}] [{step['line']}] SENDING TO VISION MODEL: '{description}'")
        result = vision_analyzer.verify_element(str(current_screenshot), description)
        debug_print(f"Vision model result: {result}")
        if result.get('found'):
            conf = result.get('confidence', 0)
            print(f"  [{_ts()}] -> VERIFIED: {description} (confidence: {conf:.2f})")
            return True
        else:
            reason = result.get('reason', 'unknown')
            print(f"  [{_ts()}] -> NOT FOUND: {description}")
            print(f"  [{_ts()}] -> Vision model reason: {reason}")
            return False

    elif cmd == "CLICK":
        description = args.strip()
        debug_print(f"CLICK: Looking for '{description}' on screen")
        current_screenshot = OUTPUTS_DIR / "current.png"
        if not controller.screenshot(current_screenshot):
            print(f"  [{_ts()}] -> ERROR: Screenshot capture failed — check the device")
            return False
        # Check if visual model config changed before calling API
        try:
            check_visual_config()
        except ConfigChangedError as e:
            print(f"\n  [{_ts()}] *** VISUAL MODEL CONFIG CHANGED - ABORTING ***")
            print(f"  [{_ts()}] {e}")
            raise
        debug_print(f"Calling vision model to find: '{description}'")
        result = vision_analyzer.find_element(str(current_screenshot), description)
        debug_print(f"Vision model found: {result}")
        if result.get('found') and result.get('coordinates'):
            coords = result['coordinates']
            debug_print(f"Tapping at coordinates: {coords}")
            controller.click(coords[0], coords[1])
            time.sleep(1)
            print(f"  [{_ts()}] -> CLICKED: {description} at {coords}")
            return True
        else:
            print(f"  [{_ts()}] -> FAILED to find: {description}")
            reason = result.get('reason', 'unknown') if result.get('reason') else 'vision model did not locate element'
            print(f"  [{_ts()}] -> Reason: {reason}")
            return False

    elif cmd == "INPUT":
        import re
        match = re.search(r'"([^"]*)"', args)
        if match:
            text = match.group(1)
            element_desc = args[:match.start()].strip()
        else:
            words = args.split()
            if len(words) < 2:
                print(f"  [{_ts()}] -> ERROR: INPUT requires element description and text")
                print(f"  [{_ts()}] -> Parsed: element='{words}' text='<missing>'")
                return False
            element_desc = ' '.join(words[:-1])
            text = words[-1]

        debug_print(f"INPUT parsing: element='{element_desc}', text='{text}'")
        current_screenshot = OUTPUTS_DIR / "current.png"
        if not controller.screenshot(current_screenshot):
            print(f"  [{_ts()}] -> ERROR: Screenshot capture failed — check the device")
            return False

        # Check if visual model config changed before calling API
        try:
            check_visual_config()
        except ConfigChangedError as e:
            print(f"\n  [{_ts()}] *** VISUAL MODEL CONFIG CHANGED - ABORTING ***")
            print(f"  [{_ts()}] {e}")
            raise

        # Check field content first
        debug_print(f"Checking input field: '{element_desc}'")
        field_result = vision_analyzer.check_input_field(str(current_screenshot), element_desc)
        debug_print(f"Field check result: {field_result}")
        if field_result.get('found') and field_result.get('coordinates'):
            coords = field_result['coordinates']
            debug_print(f"Field found at: {coords}")
            controller.click(coords[0], coords[1])
            time.sleep(0.5)

            if field_result.get('has_text') and not field_result.get('is_placeholder'):
                text_len = len(field_result.get('text', ''))
                if text_len > 0:
                    print(f"  [{_ts()}] -> Field has text: '{field_result.get('text', '')}' - deleting {text_len} chars")
                    controller.delete_selected(count=text_len)
                    time.sleep(0.2)
                else:
                    print(f"  [{_ts()}] -> Field empty or placeholder - inserting text")
            else:
                print(f"  [{_ts()}] -> Field is placeholder or empty - inserting text")

            # Log the exact ADB command being sent
            escaped = text.replace("'", "'\\''")
            adb_cmd = f"shell 'input text {escaped}'"
            debug_print(f"ADB command: {adb_cmd}")
            print(f"  [{_ts()}] [{step['line']}] TYPING '{text}' into '{element_desc}'")
            controller.input_text(text)
            print(f"  [{_ts()}] -> Input '{text}' into {element_desc}")
            time.sleep(2)  # Wait for UI to update
            return True
        else:
            print(f"  [{_ts()}] -> FAILED to find field: '{element_desc}'")
            reason = field_result.get('reason', 'vision model did not locate field') if field_result.get('reason') else 'vision model did not locate field'
            print(f"  [{_ts()}] -> Reason: {reason}")
            return False

    elif cmd == "SWIPE":
        direction = args.strip().lower()
        debug_print(f"SWIPE: direction={direction}")
        print(f"  [{_ts()}] [{step['line']}] SWIPING {direction}")
        if direction == "up": controller.swipe_up()
        elif direction == "down": controller.swipe_down()
        elif direction == "left": controller.swipe_left()
        elif direction == "right": controller.swipe_right()
        time.sleep(1)
        print(f"  [{_ts()}] -> SWIPE {direction} complete")
        return True

    elif cmd == "WAIT":
        seconds = float(args.strip())
        debug_print(f"WAIT: sleeping for {seconds}s")
        print(f"  [{_ts()}] [{step['line']}] WAITING {seconds}s")
        time.sleep(seconds)
        print(f"  [{_ts()}] -> WAIT complete ({seconds}s)")
        return True

    elif cmd == "SCRIPT":
        # Run a shell command on this host. Device tools such as adb are not here;
        # they live on the Mobile Controller.
        # By design: SCRIPT runs a command the test author wrote in the test file,
        # with the runner's privileges (see SECURITY.md "Code hardening"). The
        # shell is the feature, so the command is not split or escaped.
        debug_print(f"Executing SCRIPT: {args}")
        print(f"  [{_ts()}] [{step['line']}] RUNNING SCRIPT: {args}")
        try:
            result = subprocess.run(  # nosec B602
                args.strip(),
                shell=True,
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.stdout:
                print(f"  [{_ts()}] -> OUTPUT: {result.stdout.strip()}")
                debug_print(f"  stdout: {result.stdout.strip()[:500]}")
            if result.stderr and result.returncode != 0:
                print(f"  [{_ts()}] -> STDERR: {result.stderr.strip()}")
                debug_print(f"  stderr: {result.stderr.strip()[:500]}")
            print(f"  [{_ts()}] -> SCRIPT EXIT CODE: {result.returncode}")
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            print(f"  [{_ts()}] -> TIMEOUT after 30s")
            return False
        except Exception as e:
            print(f"  [{_ts()}] -> ERROR: {e}")
            return False

    elif cmd == "UNINSTALL":
        # Uninstall app from device
        package_name = args.strip()
        debug_print(f"UNINSTALL: package={package_name}")
        print(f"  [{_ts()}] [{step['line']}] UNINSTALLING: {package_name}")
        if not package_name:
            print(f"  [{_ts()}] -> ERROR: UNINSTALL requires a package name")
            return False
        result = controller.uninstall_app(package_name)
        if result:
            print(f"  [{_ts()}] -> Uninstall complete")
        else:
            print(f"  [{_ts()}] -> Uninstall failed")
        return result

    elif cmd == "INSTALL":
        # Install an APK on the device
        apk_path = args.strip()
        debug_print(f"INSTALL: apk={apk_path}")
        print(f"  [{_ts()}] [{step['line']}] INSTALLING APK: {apk_path}")
        if not apk_path:
            print(f"  [{_ts()}] -> ERROR: INSTALL requires an APK path")
            return False
        result = controller.install_apk(apk_path)
        if result:
            print(f"  [{_ts()}] -> Install complete")
        else:
            print(f"  [{_ts()}] -> Install failed")
        return result

    elif cmd == "DIAGNOSE":
        # Diagnostic command - never fails the test, just logs output
        # By design, like SCRIPT: a test-author-written shell command.
        debug_print(f"Running DIAGNOSE: {args}")
        print(f"  [{_ts()}] [{step['line']}] DIAGNOSING: {args}")
        try:
            result = subprocess.run(  # nosec B602
                args.strip(),
                shell=True,
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.stdout:
                for line in result.stdout.strip().split('\n'):
                    print(f"     {line}")
            if result.stderr and result.returncode != 0:
                print(f"     STDERR: {result.stderr.strip()}")
            debug_print(f"  stdout: {result.stdout.strip()[:500]}")
            if result.stderr:
                debug_print(f"  stderr: {result.stderr.strip()[:500]}")
            print(f"  [{_ts()}] -> DIAGNOSE COMPLETE (exit code: {result.returncode})")
            return True  # Never fail test
        except Exception as e:
            print(f"     [{_ts()}] ERROR: {e}")
            return True

    elif cmd == "PLUGIN":
        return _run_plugin_step(step, args)

    elif cmd == "SNAPSHOT":
        return _run_snapshot_step(step, args)

    elif cmd == "ASSERT_LAYOUT":
        return _run_layout_step(step, args)

    elif cmd == "LAUNCH":
        # Launch the app using restart (force-stop, clear data, start)
        debug_print(f"Running LAUNCH: force-stop, clear data, start app")
        print(f"  [{_ts()}] [{step['line']}] LAUNCHING APP (force-stop -> clear data -> start)")
        controller.restart_app(_app_under_test)
        print(f"  [{_ts()}] -> App launch complete")
        return True

    # Variable assignment: KEY=VALUE (no space before =)
    if "=" in cmd and " " not in cmd:
        key, value = cmd.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and not key.startswith("#"):
            debug_print(f"Storing variable: {key.upper()} = {value}")
            VARIABLES[key.upper()] = value
            print(f"  [{_ts()}] -> VARIABLE SET: {key.upper()} = {value}")
            return True

    else:
        print(f"  [{_ts()}] -> UNKNOWN COMMAND: '{cmd}'")
        print(f"  [{_ts()}] -> Raw step: {raw_cmd}")
        return False


def run_single_test(test_path, verbose=True, record_video=True):
    """Run a single test file and return (success, results_list, video_path)."""
    global _current_test_dir, _step_detail
    test_name = test_name_of(test_path)
    _current_test_dir = Path(test_path).parent

    # A case that opts in (REINSTALL_APP) starts from a reinstalled app; the
    # rest continue with the app the run installed.
    if not reinstall_for_new_case(test_path):
        return False, [], None, []

    # Run INSTRUCTION.txt first (required)
    if not run_instructions(test_path):
        return False, [], None, []

    pending_file = PROJECT_ROOT / "pending_tests.json"

    # Restart app after instructions; clears app data unless the folder's
    # INSTRUCTION.txt says CLEAR_DATA: first and this isn't its first test,
    # or this is a later case of the run with no CLEAR_DATA/REINSTALL_APP.
    keep_case_data = keeps_previous_case_data(test_path)
    clear_data = should_clear_app_data(test_path)
    if clear_data:
        restart_note = '...'
    elif keep_case_data:
        restart_note = ' (keeping app data from the previous case)...'
    else:
        restart_note = ' (keeping app data, CLEAR_DATA: first)...'
    print(f"\n  [{_ts()}] RESTARTING APP{restart_note}")
    update_run_progress(
        pending_file, "preparing_test", "Restarting the app before test steps",
        test_name=test_name,
    )
    controller.restart_app(_app_under_test, clear_data=clear_data)

    steps = parse_test_file(test_path)
    print(f"  [{_ts()}] Steps: {len(steps)}")

      # Start video recording using external capture (AVerMedia)
    video_path = None
    if record_video:
        try:
            print(f"\n  [{_ts()}] RECORDING: {test_name}.mp4")
            update_run_progress(
                pending_file, "preparing_recording", "Starting test recording",
                test_name=test_name,
            )
            # Record the whole test: no time limit, stop_recording ends it.
            # (A 60 s cap cut Android videos off mid-test.)
            controller.start_recording()
        except Exception as e:
            print(f"  [{_ts()}] -> Video recording start failed: {e}")

    # Execute steps
    results = []
    failed = False

    print(f"\n  [{_ts()}] === EXECUTING {len(steps)} STEPS ===")
    for step_idx, step in enumerate(steps, 1):
        if failed:
            results.append({
                'step': step,
                'status': 'SKIPPED',
                'reason': 'Previous step failed'
            })
            print(f"  [{_ts()}] [{step_idx}/{len(steps)}] SKIPPED: {step['raw']} (previous step failed)")
            update_run_progress(
                pending_file, "test_failed", "Skipping remaining steps after a failure",
                test_name=test_name, step=step, step_index=step_idx,
                step_total=len(steps), step_status="skipped",
            )
            continue

        skip = step_skipped_on_platform(step)
        if skip:
            # Another platform's step: not run, and not a failure.
            results.append({'step': step, 'status': 'SKIPPED', 'reason': skip})
            print(f"  [{_ts()}] [{step_idx}/{len(steps)}] SKIPPED: {step['raw']} ({skip})")
            update_run_progress(
                pending_file, "running_step", f"Skipped step {step_idx} of {len(steps)}",
                test_name=test_name, step=step, step_index=step_idx,
                step_total=len(steps), step_status="skipped",
            )
            continue

        print(f"  [{_ts()}] [{step_idx}/{len(steps)}] STEP {step_idx} of {len(steps)}: {step['raw']}")
        update_run_progress(
            pending_file, "running_step", f"Running step {step_idx} of {len(steps)}",
            test_name=test_name, step=step, step_index=step_idx,
            step_total=len(steps), step_status="running",
        )
        _step_detail = {}
        success = execute_step(step)
        status = 'PASS' if success else 'FAIL'
        results.append({'step': step, 'status': status, **_step_detail})
        update_run_progress(
            pending_file, "running_step",
            f"{'Completed' if success else 'Failed'} step {step_idx} of {len(steps)}",
            test_name=test_name, step=step, step_index=step_idx,
            step_total=len(steps), step_status=status.lower(),
        )

        if not success:
            failed = True
            print(f"  [{_ts()}] [{step_idx}/{len(steps)}] STEP FAILED: {step['raw']}")
        else:
            print(f"  [{_ts()}] [{step_idx}/{len(steps)}] STEP PASSED: {step['command']}")

    # Let recording extend 5s after steps complete so the video doesn't end abruptly
    if record_video:
        print(f"\n  [{_ts()}] Extending recording by 5s...")
        update_run_progress(
            pending_file, "finishing_test", "Finishing the test recording",
            test_name=test_name,
        )
        time.sleep(5)

    if record_video:
        print(f"\n  [{_ts()}] Stopping recording...")
        update_run_progress(
            pending_file, "finishing_test", "Saving the test recording",
            test_name=test_name,
        )
        try:
            video_path = controller.stop_recording()
            if video_path:
                print(f"  [{_ts()}] -> Captured video: {video_path}")
        except Exception as e:
            print(f"  [{_ts()}] -> Failed to stop recording gracefully: {e}")
            video_path = None

    summary_passed = sum(1 for r in results if r['status'] == 'PASS')
    summary_failed = sum(1 for r in results if r['status'] == 'FAIL')
    summary_skipped = sum(1 for r in results if r['status'] == 'SKIPPED')

    print(f"\n{'='*60}")
    print(f"  [{_ts()}] RESULT: {'PASS' if not failed else 'FAIL'}")
    print(f"  Passed: {summary_passed}")
    print(f"  Failed: {summary_failed}")
    print(f"  Skipped: {summary_skipped}")
    if video_path:
        print(f"  Video: {video_path}")
    print(f"{'='*60}")

    # Collect screenshots from outputs dir (files matching test_name prefix or step labels)
    screenshot_files = []
    shots_dir = _test_outputs_dir()
    if shots_dir.exists():
        for f in sorted(shots_dir.iterdir()):
            if f.is_file() and f.name not in ('current.png', '.once_instructions_run'):
                ext = f.suffix.lower()
                if ext in ('.png', '.jpg', '.jpeg', '.webp') or f.stat().st_size > 1000:
                    # Check PNG/JPEG magic bytes for files without extension
                    with open(f, 'rb') as fh:
                        header = fh.read(8)
                    if ext == '' and header[:8] == b"\x89PNG\r\n\x1a\n":
                        screenshot_files.append(str(f.relative_to(PROJECT_ROOT)))
                    elif ext in ('.png', '.jpg', '.jpeg', '.webp'):
                        screenshot_files.append(str(f.relative_to(PROJECT_ROOT)))

    # Save per-test result.json
    RESULTS_DIR.mkdir(exist_ok=True)
    result_file = RESULTS_DIR / f"{test_name}_result.json"
    run_meta = load_run_metadata()
    video_rel = os.path.relpath(video_path, PROJECT_ROOT) if video_path else None
    with open(result_file, 'w') as f:
        json.dump({
            'test': test_name,
            'label': test_label(test_path),
            'case': test_case_of(test_path),
            'status': 'PASS' if not failed else 'FAIL',
            'steps': results,
            'screenshots': screenshot_files,
            'video': video_rel,
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'run_metadata': run_meta
        }, f, indent=2)
    print(f"\n  [{_ts()}] Results saved: {result_file}")

    return not failed, results, video_path, screenshot_files


def run_test(test_path, verbose=False, record_video=True):
    """Run a test file and return success.

    Discovers all test files in the folder. Without --test-files, runs all
    discovered tests and saves a consolidated folder-level result.json.
    With --test-files, runs only the specified files with per-test result.json.
    """
    test_name = test_name_of(test_path)
    print(f"\n{'='*60}")
    print(f"  [{_ts()}] TEST: {test_name}")
    print(f"{'='*60}")

    # Discover all test files in the folder
    test_dir = str(Path(test_path).parent)
    all_tests = discover_tests_in_folder(test_dir)

    if not all_tests:
        print(f"  [{_ts()}] ERROR: No test files found in {test_dir}")
        return False

    print(f"  [{_ts()}] Discovered {len(all_tests)} test(s) in folder: {os.path.basename(test_dir)}")
    for t in all_tests:
        print(f"  [{_ts()}]     {os.path.basename(t)}")
    print()

    # Run the specified test (INSTRUCTION.txt is required before this)
    print(f"  [{_ts()}] Running test: {test_path}")
    success, results, video_path, screenshot_files = run_single_test(test_path, verbose, record_video)
    print(f"  [{_ts()}] Test {test_name} completed with status: {'PASS' if success else 'FAIL'}")

    # Determine result file: folder-level when running all tests, per-test otherwise
    folder_name = os.path.basename(test_dir)

    if verbose:  # --all flag means verbose=True was passed
        # Consolidated folder-level result: all tests listed,
        # specified one gets its results, others are SKIPPED
        consolidated = []
        for t in all_tests:
            if test_name_of(t) == test_name:
                consolidated.extend(results)
            else:
                consolidated.append({
                    'step': {'line': 0, 'command': 'TEST_FILE', 'args': os.path.basename(t), 'raw': os.path.basename(t)},
                    'status': 'SKIPPED',
                    'reason': 'Not specified in test run'
                })

        overall = 'PASS' if success else 'FAIL'
        skipped_count = sum(1 for r in consolidated if r['status'] == 'SKIPPED')

        passed = sum(1 for r in consolidated if r['status'] == 'PASS')
        failed = sum(1 for r in consolidated if r['status'] == 'FAIL')

        print(f"\n{'='*60}")
        print(f"  [{_ts()}] FOLDER RESULT: {overall} ({folder_name})")
        print(f"  Passed: {passed}")
        print(f"  Failed: {failed}")
        print(f"  Skipped: {skipped_count}")
        if video_path:
            print(f"  Video: {video_path}")
        print(f"{'='*60}")

        RESULTS_DIR.mkdir(exist_ok=True)
        result_file = RESULTS_DIR / f"{folder_name}_result.json"
        run_meta = load_run_metadata()
        video_rel = os.path.relpath(video_path, PROJECT_ROOT) if video_path else None
        with open(result_file, 'w') as f:
            json.dump({
                'test': folder_name,
                'status': overall,
                'steps': consolidated,
                'screenshots': screenshot_files,
                'video': video_rel,
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'run_metadata': run_meta,
                'specified_test': test_name,
                'all_tests': [os.path.basename(t) for t in all_tests]
            }, f, indent=2)
        print(f"  [{_ts()}] Consolidated result saved: {result_file}")
    else:
        # Standard per-test result (no --all flag)
        pass

    return success


def extract_apk_info(apk_path):
    """Extract versionName and package name from an APK file.

    Returns dict with 'version', 'package', 'file_size' keys.
    """
    import re
    info = {'version': 'unknown', 'package': 'unknown', 'file_size': 0}

    if not os.path.exists(apk_path):
        print(f"  [{_ts()}] ERROR: APK not found: {apk_path}")
        return info

    info['file_size'] = os.path.getsize(apk_path)

    # Try aapt/aapt2 first
    for aapt_name in ['aapt', 'aapt2']:
        aapt_path = shutil.which(aapt_name)
        if aapt_path:
            try:
                result = subprocess.run(
                    [aapt_path, 'dump', 'badging', apk_path],
                    capture_output=True, text=True, timeout=10
                )
                for line in result.stdout.split('\n'):
                    if 'versionName=' in line:
                        info['version'] = line.split("'")[1]
                    if 'package:' in line:
                        info['package'] = line.split("'")[1]
                return info
            except Exception:
                pass

    # No aapt on this host: read the binary manifest directly.
    try:
        package, version = apk_manifest.read_apk_identity(apk_path)
        info['package'] = package or info['package']
        info['version'] = version or info['version']
    except Exception as e:
        print(f"  [{_ts()}] WARNING: could not read APK manifest: {e}")

    return info


def install_apk_on_device(apk_path):
    """Install APK on the device via ADB and return (version, package_name)."""
    print(f"\n  [{_ts()}] === INSTALLING APK ===")
    info = extract_apk_info(apk_path)
    print(f"  [{_ts()}] APK: {os.path.basename(apk_path)}")
    print(f"  [{_ts()}] Version: {info['version']}")
    print(f"  [{_ts()}] Package: {info['package']}")
    print(f"  [{_ts()}] Size: {info['file_size'] // 1024}KB")

    # Without the package there is no way to launch the app under test.
    if info['package'] == 'unknown':
        print(f"  [{_ts()}] ERROR: could not determine the APK's package name")
        return None, None, None

    # Uninstall existing version first
    print(f"  [{_ts()}] Uninstalling existing {info['package']}...")
    controller.uninstall_app(info['package'])
    time.sleep(2)

    # Install new APK
    print(f"  [{_ts()}] Installing APK...")
    success = controller.install_apk(apk_path)
    if not success:
        print(f"  [{_ts()}] ERROR: APK install failed")
        return None, None, None

    print(f"  [{_ts()}] APK installed successfully")
    return info['version'], info['package'], apk_path


def uninstall_apk_from_device(package_name):
    """Uninstall APK from device (cleanup step)."""
    if not package_name or package_name == 'unknown':
        return
    print(f"\n  [{_ts()}] === CLEANING UP: UNINSTALLING {package_name} ===")
    controller.uninstall_app(package_name)
    time.sleep(1)
    print(f"  [{_ts()}] Cleanup complete")


def save_run_metadata(apk_version=None, apk_package=None, apk_path=None,
                      instruction_files=None, test_files=None,
                      options=None, device_host=None, device_id=None,
                      branch=None):
    """Save run metadata including APK info and test configuration.

    The device's real name comes from the Mobile Controller so the viewer's
    "Ran on" list shows e.g. "SM-A155F" instead of the device id.
    """
    # Resolve the real device name once, while the device is connected.
    # Fall back to the device id if the model can't be read.
    device_name = ""
    if device_host:
        try:
            device_name = controller.get_device_model()
        except Exception:
            device_name = ""
    meta_path = OUTPUTS_DIR.parent / "run_metadata.json"
    meta = {
        'timestamp': _ts(),
        'apk_version': apk_version,
        'apk_package': apk_package,
        'apk_path': apk_path,
        'device': (
            {'serial': device_host, 'name': device_name or device_host}
            if device_host else None
        ),
        'instruction_files': instruction_files,
        'test_files': test_files,
        'options': options or {},
        'branch': branch,
        **run_flags(),
        'status': 'running',
        'passed': 0,
        'failed': 0,
        'total': 0,
    }
    OUTPUTS_DIR.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, 'w') as f:
        json.dump(meta, f, indent=2)
    if device_id:
        _update_root_device(
            device_id,
            status="running",
            started=meta["timestamp"],
            serial=device_host,
            name=device_name or device_host,
        )
    print(f"  [{_ts()}] Run metadata saved: {meta_path}")


def update_run_metadata(status='completed', passed=0, failed=0, total=0, device_id=None, cancelled=0):
    """Update run metadata with final results."""
    meta_path = OUTPUTS_DIR.parent / "run_metadata.json"
    if not meta_path.exists():
        return
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        meta['status'] = status
        meta['passed'] = passed
        meta['failed'] = failed
        meta['cancelled'] = cancelled
        meta['total'] = total
        meta['completed'] = _ts()
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)
        if device_id:
            _update_root_device(
                device_id,
                status=status,
                passed=passed,
                failed=failed,
                cancelled=cancelled,
                total=total,
                completed=meta["completed"],
            )
    except Exception as e:
        print(f"  [{_ts()}] WARNING: Failed to update metadata: {e}")


def mark_run_interrupted(reason):
    """Finalize a run whose process was killed mid-flight (host shutdown,
    crash, kill): the metadata would otherwise stay "running" forever and the
    viewer would show a phantom Running run indefinitely.

    Stamps the aggregate root metadata, every in-flight device entry and the
    per-device pending_tests.json progress files as "interrupted" so the
    viewer (and the stale-run watchdog's _is_running) sees a terminal state.
    """
    meta_path = _root_metadata_path()
    terminal = ("completed", "completed_with_failures", "failed",
                "interrupted", "aborted")
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = None
    if meta is not None and meta.get("status") not in terminal:
        meta["status"] = "interrupted"
        meta["error"] = reason
        meta["completed"] = _ts()
        for dev in meta.get("devices", []):
            if dev.get("status") == "running":
                dev["status"] = "interrupted"
                dev["completed"] = meta["completed"]
        _write_json_atomic(meta_path, meta)
    # Terminalize progress files: run root and every device's own copy.
    pending_files = [RUN_ROOT / "pending_tests.json"]
    devices_dir = RUN_ROOT / "devices"
    if devices_dir.is_dir():
        pending_files += sorted(devices_dir.glob("*/pending_tests.json"))
    for pf in pending_files:
        if not pf.exists():
            continue
        try:
            with open(pf) as f:
                pt = json.load(f)
        except (OSError, ValueError):
            continue
        progress = pt.get("progress")
        if progress and progress.get("phase") not in terminal:
            progress["phase"] = "interrupted"
            progress["updated_at"] = _ts()
            _write_json_atomic(pf, pt)
    print(f"  [{_ts()}] Run marked INTERRUPTED: {reason}")


def _run_device_tests(test_files, apk_version, apk_package, apk_path, args, pending_file, test_files_list, device_label):
    """Run the test suite for a single device. Returns (passed, failed, all_success)."""
    global _app_path_under_test
    set_app_under_test(apk_package)
    _app_path_under_test = apk_path
    if device_label:
        activate_device_output(device_label)

    # Save run metadata before starting tests
    save_run_metadata(
        apk_version=apk_version,
        apk_package=apk_package,
        apk_path=apk_path,
        instruction_files=test_files_list,
        test_files=[os.path.basename(tf) for tf in test_files],
        options={
            'test_files': args.test_files,
            'verbose': args.verbose,
            'no_video': args.no_video,
        },
        device_host=os.environ.get('DEVICE_ID'),
        device_id=device_label,
        branch=getattr(args, 'branch', None),
    )

    # Build and write pending_tests.json before running any tests
    pending_tests = []
    for i, tf in enumerate(test_files):
        steps = parse_test_file(tf)
        pending_tests.append({
            'name': test_name_of(tf),
            'label': test_label(tf),
            'case': test_case_of(tf),
            'order': i,
            'steps': [{k: s[k] for k in ('line', 'command', 'args', 'raw', 'block', 'error') if k in s}
                      for s in steps],
            'step_statuses': ['pending'] * len(steps),
            'status': 'pending'
        })
    pending_file = PROJECT_ROOT / "pending_tests.json"
    pending_data = _load_pending_data(pending_file)
    pending_data['tests'] = pending_tests
    pending_data['run_start'] = pending_data.get('run_start', _ts())
    pending_data['progress'] = {
        'phase': 'ready_to_run',
        'message': f'Ready to run {len(test_files)} test(s)',
        'updated_at': _ts(),
    }
    _write_json_atomic(pending_file, pending_data)
    print(f"  [{_ts()}] Pending tests written: {pending_file}")

    # Run tests
    all_success = True
    total_passed = 0
    total_failed = 0
    total_cancelled = 0

    print(f"\n{'='*60}")
    print(f"  [{_ts()}] === RUNNING {len(test_files)} TEST(S) ===")
    print(f"{'='*60}")

    for i, tf in enumerate(test_files, 1):
        test_name = test_name_of(tf)
        print(f"\n  [{_ts()}] --- TEST {i}/{len(test_files)}: {test_name} ---")
        # Check if visual model config changed on disk
        try:
            check_visual_config()
        except ConfigChangedError as e:
            print(f"\n  [{_ts()}] *** VISUAL MODEL CONFIG CHANGED - ABORTING TEST RUN ***")
            print(f"  [{_ts()}] {e}")
            _update_pending_status(pending_file, test_name, 'aborted')
            update_run_progress(
                pending_file, 'aborted',
                f'Aborted: visual model config changed on disk',
                test_name=test_name, test_index=i, test_total=len(test_files),
            )
            # Mark all remaining tests as aborted
            for remaining_i in range(i, len(test_files)):
                remaining_name = test_name_of(test_files[remaining_i])
                _update_pending_status(pending_file, remaining_name, 'aborted')
            break
        _update_pending_status(pending_file, test_name, 'running')
        update_run_progress(
            pending_file, 'starting_test', f'Starting test {i} of {len(test_files)}',
            test_name=test_name, test_index=i, test_total=len(test_files),
        )
        success = True
        try:
            success = run_test(tf, verbose=args.verbose, record_video=not args.no_video)
        except ConfigChangedError as e:
            print(f"\n  [{_ts()}] *** VISUAL MODEL CONFIG CHANGED - ABORTING TEST RUN ***")
            print(f"  [{_ts()}] {e}")
            success = False
            # Mark this test as aborted and all remaining tests as aborted
            _update_pending_status(pending_file, test_name, 'aborted')
            update_run_progress(
                pending_file, 'aborted',
                f'Aborted: visual model config changed on disk',
                test_name=test_name, test_index=i, test_total=len(test_files),
            )
            for remaining_i in range(i + 1, len(test_files)):
                remaining_name = test_name_of(test_files[remaining_i])
                _update_pending_status(pending_file, remaining_name, 'aborted')
            print(f"  [{_ts()}] All remaining test(s) marked as aborted")
            all_success = False
            break
        except Exception as e:
            print(f"  [{_ts()}] TEST EXCEPTION: {test_name} - {e}")
            success = False

        _update_pending_status(pending_file, test_name, 'completed' if success else 'failed')
        update_run_progress(
            pending_file,
            'test_completed' if success else 'test_failed',
            f"{'Completed' if success else 'Failed'} test {i} of {len(test_files)}",
            test_name=test_name, test_index=i, test_total=len(test_files),
        )
        if not success:
            all_success = False
            total_failed += 1
            print(f"  [{_ts()}] TEST FAILED: {test_name}")
        else:
            total_passed += 1
            print(f"  [{_ts()}] TEST PASSED: {test_name}")

        if not success:
            # Stop this device: its remaining tests (in every case) are
            # cancelled, not failed. Other devices in the run still run.
            remaining = test_files[i:]
            for rem_tf in remaining:
                rem_name = test_name_of(rem_tf)
                _update_pending_status(pending_file, rem_name, 'cancelled')
            total_cancelled = len(remaining)
            print(f"  [{_ts()}] Cancelled {len(remaining)} remaining test(s) on this device")
            break

    print(f"\n{'='*60}")
    print(f"  [{_ts()}] === TEST RUN COMPLETE ===")
    print(f"  Total: {total_passed + total_failed} | Passed: {total_passed} | Failed: {total_failed}")
    print(f"{'='*60}")

    # Update metadata with final results
    total = total_passed + total_failed
    status = 'completed' if all_success else 'completed_with_failures'
    update_run_metadata(
        status=status,
        passed=total_passed,
        failed=total_failed,
        cancelled=total_cancelled,
        total=total,
        device_id=device_label,
    )
    update_run_progress(
        pending_file, status,
        f'Run finished: {total_passed} passed, {total_failed} failed',
        test_total=len(test_files),
    )

    # Cleanup: uninstall APK
    if apk_version:
        uninstall_apk_from_device(apk_package)

    return total_passed, total_failed, all_success
