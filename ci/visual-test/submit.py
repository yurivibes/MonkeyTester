#!/usr/bin/env python3
"""Send an app build to MonkeyTester and, unless --no-wait, wait for the verdict.

Build plugins (build-local) run this after building a branch: the viewer
serves it at GET /ci/submit.py, so the plugin downloads it and runs it with
python3 from the checkout's root, always with --no-wait and --build-request.

Every visual-tests/<case>/ directory (one INSTRUCTION.txt plus test .txt
files) goes into ONE run: the viewer runs every case, in name order, on every
device. Give --android-app and --ios-app together to cover the Android phones
and the iPhones in that run; --app with --platform submits a single platform.

The run records the commit under test and its message (shown on the run
list): HEAD of the checkout this runs in, unless --commit/--commit-message.

--no-wait: upload, print "--- <run_id> started" and exit 0 once the viewer
has accepted the upload (non-zero when it refused it); the viewer works out
the test results itself. Without it the script waits for the verdict and
exits 0 only if at least one test ran and every test passed: rejected
uploads, install failures, crashes, timeouts and empty runs all fail.

Standard library only. Credential: the viewer's CI token from the VT_CI_TOKEN
environment variable (never the command line), sent as `Authorization: Bearer`
(Settings -> Configuration -> CI token). Without it the viewer accepts only an
upload for a build it started itself that is still waiting for its apps (the
build plugins upload that way, with --build-request); everything else is 401.
"""
import argparse
import json
import os
import re
import subprocess  # nosec B404 - fixed git argv, no shell
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PLATFORM_EXTENSIONS = {"android": (".apk",), "ios": (".ipa",)}
MAX_POLL_ERRORS = 30  # consecutive; tolerates a viewer restart mid-run
# `@file:<path>` refs in a case's tests name payload files the case ships
# (same token rule as the runner's FILE_REF_RE); the viewer's limits:
FILE_REF_RE = re.compile(r"@file:(\S+)")
PAYLOAD_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,99}$")
MAX_PAYLOAD_BYTES = 4096
MAX_PAYLOAD_FILES_PER_CASE = 32


def fail(message):
    print(f"::error::{message}", flush=True)


def build_multipart(fields, files):
    """Return (content_type, body). files is a list of (field, filename, bytes)."""
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode())
    for field, filename, content in files:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def find_cases(tests_dir):
    """Return case directories under tests_dir that hold an INSTRUCTION.txt."""
    root = Path(tests_dir)
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / "INSTRUCTION.txt").is_file())


def select_cases(cases, wanted):
    """Keep only the named cases (comma-separated; empty keeps all).

    Returns (cases, None) or (None, error) when a named case doesn't exist.
    """
    names = [n.strip() for n in (wanted or "").split(",") if n.strip()]
    if not names:
        return cases, None
    by_name = {c.name: c for c in cases}
    missing = [n for n in names if n not in by_name]
    if missing:
        return None, f"case(s) not found: {', '.join(missing)}"
    return [by_name[n] for n in sorted(set(names))], None


def collect_payloads(case_dir):
    """Find the payload files a case's tests reference with `@file:<path>`.

    Every `*.txt` in the case dir is scanned; each path must be relative to
    the case dir, stay inside it (also after following symlinks), and name a
    regular file of at most MAX_PAYLOAD_BYTES. Returns ([(rel, bytes)], None)
    with each file once (rel: normalized posix path inside the case), or
    (None, error) naming the case, test file, line and path. Refs to files
    the upload already carries (the case's own .txt files, snapshot
    baselines) are not repeated. A ref with no path is left for the runner,
    which reports it as a parse error naming the line."""
    case_dir = Path(case_dir)
    case = case_dir.name
    real_case = os.path.realpath(case_dir)
    shipped = {t.name for t in case_dir.glob("*.txt")}
    shipped |= {p.relative_to(case_dir).as_posix() for p in (case_dir / "snapshots").glob("*/*/*.png")}
    payloads = {}
    for txt in sorted(case_dir.glob("*.txt")):
        text = txt.read_bytes().decode("utf-8", errors="replace")
        for line_num, line in enumerate(text.split("\n"), 1):
            for match in FILE_REF_RE.finditer(line):
                # A ref inside a quoted script="..." param ends at the quote.
                ref = match.group(1).rstrip("\"'")
                if not ref:
                    continue  # no path: the runner reports it as a parse error
                where = f"case {case}: {txt.name} line {line_num}: @file:{ref}"
                if os.path.isabs(ref) or "\\" in ref:
                    return None, (f"{where} must be a relative path (relative to the "
                                  f"test file's directory)")
                rel = os.path.normpath(ref)
                full = os.path.join(case_dir, rel)
                real = os.path.realpath(full)
                if rel == ".":
                    return None, f"{where} names the case directory, not a file"
                if (rel == ".." or rel.startswith(".." + os.sep)
                        or os.path.commonpath([real, real_case]) != real_case):
                    return None, f"{where} resolves outside the case directory"
                rel = Path(rel).as_posix()
                if rel in shipped or rel in payloads:
                    continue
                if not os.path.lexists(full):
                    return None, f"{where} names a file that doesn't exist"
                if not os.path.isfile(real):
                    return None, f"{where} is not a regular file"
                bad = [seg for seg in rel.split("/") if not PAYLOAD_SEGMENT_RE.match(seg)]
                if bad:
                    return None, (f"{where}: path segment {bad[0]!r} may only use letters, "
                                  f"digits, _ . - (not starting with '.')")
                size = os.path.getsize(real)
                if size > MAX_PAYLOAD_BYTES:
                    return None, f"{where} is {size} bytes (max {MAX_PAYLOAD_BYTES})"
                if size == 0:
                    return None, f"{where} is empty (boards stage 1-{MAX_PAYLOAD_BYTES} bytes)"
                if len(payloads) >= MAX_PAYLOAD_FILES_PER_CASE:
                    return None, (f"case {case}: more than {MAX_PAYLOAD_FILES_PER_CASE} "
                                  f"payload files (at {txt.name} line {line_num}: @file:{ref})")
                with open(real, "rb") as f:
                    payloads[rel] = f.read()
    return sorted(payloads.items()), None


def collect_case_payloads(case_dirs):
    """collect_payloads for every case: ({case: [(rel, bytes)]}, None) or
    (None, error) for the first case with a bad ref."""
    by_case = {}
    for case_dir in case_dirs:
        payloads, error = collect_payloads(case_dir)
        if error:
            return None, error
        by_case[Path(case_dir).name] = payloads
    return by_case, None


def case_parts(case_dirs, payloads_by_case):
    """Multipart file parts for the cases: (field, filename, bytes) tuples.
    Test files are `tests` parts <case>/<file>.txt, snapshot baselines
    `snapshots` parts <case>/snapshots/..., payload files `payloads` parts
    <case>/<rel>."""
    files = []
    if len(case_dirs) > 1:
        # Run-wide suite order (visual-tests/SETUP.txt): staged at the tests
        # root so the runner can fix the case order. Only for multi-case
        # uploads; a single picked case needs no order.
        setup = case_dirs[0].parent / "SETUP.txt"
        if setup.is_file():
            files.append(("tests", "SETUP.txt", setup.read_bytes()))
    for case_dir in case_dirs:
        for txt in sorted(case_dir.glob("*.txt")):
            files.append(("tests", f"{case_dir.name}/{txt.name}", txt.read_bytes()))
        for png in sorted((case_dir / "snapshots").glob("*/*/*.png")):
            files.append(("snapshots", f"{case_dir.name}/{png.relative_to(case_dir).as_posix()}",
                          png.read_bytes()))
        for rel, content in payloads_by_case.get(case_dir.name, []):
            files.append(("payloads", f"{case_dir.name}/{rel}", content))
    return files


def request_json(url, data=None, content_type=None, timeout=60):
    """Return (http_status, parsed_json). Raises URLError if unreachable."""
    headers = {"Accept": "application/json"}
    token = os.environ.get("VT_CI_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers)
    if req.type not in ("http", "https"):
        raise urllib.error.URLError("only http(s) URLs are allowed")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return status, {"error": raw[:200].decode(errors="replace")}


def submit_cases(args, apps, case_dirs, payloads_by_case=None):
    """Upload every case in ONE run. apps maps platform -> app path.
    Returns (run_id, None), or (None, error).

    Each test file is sent as <case>/<file>.txt, each snapshot baseline as
    <case>/snapshots/<platform>/<W>x<H>@<scale>x/<name>.png and each payload
    file a test references with `@file:<rel>` as <case>/<rel>, so the viewer
    stages every case folder with its own INSTRUCTION.txt, baselines and
    payloads."""
    if payloads_by_case is None:
        payloads_by_case, error = collect_case_payloads(case_dirs)
        if error:
            return None, error
    files = []
    if "android" in apps:
        files.append(("apk", apps["android"].name, apps["android"].read_bytes()))
    if "ios" in apps:
        # Alone, the iOS app goes in the "apk" part, as older viewers expect.
        part = "ios_app" if "android" in apps else "apk"
        files.append((part, apps["ios"].name, apps["ios"].read_bytes()))
    files += case_parts(case_dirs, payloads_by_case)
    failed = build_errors(args)
    platforms = set(apps) | set(failed)
    mode = "all" if len(platforms) > 1 else next(iter(platforms))
    fields = {"device_mode": mode, "branch": args.branch}
    commit = (getattr(args, "commit", "") or "").strip()
    if commit:
        # The commit under test, shown with its message on the run list.
        fields["commit"] = commit
        message = getattr(args, "commit_message", "") or ""
        if message.strip():
            fields["commit_message"] = message
    repo = (getattr(args, "repo", "") or "").strip()
    if repo:
        # The run's project in the viewer (owner/repo).
        fields["repo"] = repo
    for platform, message in failed.items():
        fields[f"build_failed_{platform}"] = message
        log = getattr(args, f"{platform}_build_log", "") or ""
        if log and Path(log).is_file():
            # The end of the log holds the error; the viewer keeps up to 2 MB.
            files.append((f"build_log_{platform}", f"{platform}_build_log.txt",
                          Path(log).read_bytes()[-2 * 1024 * 1024:]))
    if getattr(args, "build_request", ""):
        # Started from the viewer's Build & Test page: attach the run to it.
        fields["build_request"] = args.build_request
    content_type, body = build_multipart(fields, files)
    try:
        status, payload = request_json(
            f"{args.url}/api/run-test", body, content_type, timeout=600)
    except OSError as e:
        return None, f"viewer unreachable: {e}"
    if status != 200 or not payload.get("run_id"):
        return None, payload.get("error") or payload.get("warning") or f"HTTP {status}"
    return payload["run_id"], None


def build_errors(args):
    """{platform: error} for the platforms whose app didn't build
    (--android-build-error / --ios-build-error). The viewer shows those
    platforms' devices with every test UNAVAILABLE and the error."""
    errors = {}
    for platform in ("android", "ios"):
        message = " ".join((getattr(args, f"{platform}_build_error", "") or "").split())
        if message:
            errors[platform] = message[:2000]
    return errors


def resolve_apps(args):
    """Return ({platform: Path}, None) or (None, error) from the arguments."""
    apps = {}
    if getattr(args, "android_app", None):
        apps["android"] = Path(args.android_app)
    if getattr(args, "ios_app", None):
        apps["ios"] = Path(args.ios_app)
    if getattr(args, "app", None):
        if apps:
            return None, "use --app/--platform or --android-app/--ios-app, not both"
        if not args.platform:
            return None, "--app needs --platform"
        apps[args.platform] = Path(args.app)
    if not apps and not build_errors(args):
        return None, "no app given: pass --android-app and/or --ios-app"
    for platform in build_errors(args):
        if platform in apps:
            return None, f"--{platform}-build-error was given with a {platform} app"
    for platform, path in apps.items():
        if not path.is_file():
            return None, f"app file not found: {path}"
        if not path.name.lower().endswith(PLATFORM_EXTENSIONS[platform]):
            return None, (f"{path.name} is not a {platform} build "
                          f"(expected {'/'.join(PLATFORM_EXTENSIONS[platform])})")
    return apps, None


def wait_for_result(args, run_id):
    """Poll until the run finishes. Always returns a result dict."""
    url = f"{args.url}/api/runs/{run_id}/result"
    deadline = time.monotonic() + args.timeout
    errors = 0
    while time.monotonic() < deadline:
        try:
            status, result = request_json(url)
        except OSError as e:
            status, result = 0, {"error": str(e)}
        if status == 200:
            errors = 0
            if result.get("status") != "running":
                return result
        elif status in (401, 403, 404):
            return {"status": "failed", "reason": f"HTTP {status}: {result.get('error')}"}
        else:
            errors += 1
            if errors >= MAX_POLL_ERRORS:
                return {"status": "failed", "reason": f"viewer not responding: {result.get('error')}"}
        time.sleep(args.poll)
    return {"status": "failed", "reason": f"timed out after {args.timeout}s"}


def run(args):
    apps, error = resolve_apps(args)
    if error:
        fail(error)
        return 1
    failed = build_errors(args)
    names = " + ".join([path.name for path in apps.values()]
                       + [f"{p} (build failed)" for p in failed])
    for platform, message in failed.items():
        print(f"--- {platform} app didn't build; its devices will be marked failed: {message}",
              flush=True)
    cases = find_cases(args.tests_dir)
    if not cases:
        fail(f"no test cases found in {args.tests_dir} (each case needs an INSTRUCTION.txt)")
        return 1
    cases, error = select_cases(cases, getattr(args, "cases", ""))
    if error:
        fail(error)
        return 1

    payloads_by_case, error = collect_case_payloads(cases)
    if error:
        fail(f"bad @file: payload reference: {error}")
        return 1

    names_list = ", ".join(c.name for c in cases)
    print(f"--- uploading {names} with cases: {names_list}", flush=True)
    run_id, error = submit_cases(args, apps, cases, payloads_by_case)
    if getattr(args, "no_wait", False):
        if error:
            fail(f"upload rejected: {error}")
            return 1
        print(f"--- {run_id} started", flush=True)
        return 0
    if error:
        result = {"status": "failed", "reason": f"upload rejected: {error}"}
    else:
        print(f"--- {run_id} started, waiting for result", flush=True)
        result = wait_for_result(args, run_id)

    passed = result.get("status") == "passed"
    print(f"\nVisual tests ({' + '.join([*apps, *failed])}; {names_list}):")
    print(f"  {'PASS' if passed else 'FAIL'}  passed={result.get('passed', 0)} "
          f"failed={result.get('failed', 0)} cancelled={result.get('cancelled', 0)} "
          f"unavailable={result.get('unavailable', 0)} "
          f"total={result.get('total', 0)}  {result.get('url', '')}")
    if not passed:
        fail(f"{result.get('reason') or 'failed'} {result.get('url', '')}".strip())
    return 0 if passed else 1


def default_branch(env=None):
    """The branch when --branch isn't given: GITHUB_REF_NAME when a CI set
    it; "" otherwise (build plugins pass --branch)."""
    env = os.environ if env is None else env
    return (env.get("GITHUB_REF_NAME") or "").strip()


def git_output(*argv, cwd=None):
    """stdout of `git <argv>` in cwd, or "" when git or the checkout is missing."""
    try:
        out = subprocess.run(["git", *argv], cwd=cwd, capture_output=True,  # nosec B603,B607
                             text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def default_commit(env=None, cwd=None):
    """(commit id, full message) of the code under test: HEAD of the
    checkout submit.py runs in (build plugins and CI both run it from the
    checkout's root), else GITHUB_SHA with no message. ("", "") when neither
    is known."""
    env = os.environ if env is None else env
    sha = git_output("rev-parse", "HEAD", cwd=cwd).strip()
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        sha = (env.get("GITHUB_SHA") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", sha):
            return "", ""
    message = git_output("log", "-1", "--format=%B", sha, cwd=cwd).strip()
    return sha, message


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="viewer base URL")
    parser.add_argument("--android-app", help="path to the .apk")
    parser.add_argument("--ios-app", help="path to the signed .ipa")
    parser.add_argument("--app", help="single-platform form: path to the .apk or .ipa")
    parser.add_argument("--android-build-error", default="",
                        help="the Android app didn't build: the run's Android devices show "
                             "every test UNAVAILABLE with this error")
    parser.add_argument("--android-build-log", default="",
                        help="Android build log, linked from the unavailable tests")
    parser.add_argument("--ios-build-log", default="",
                        help="iOS build log (xcodebuild output), linked from the unavailable tests")
    parser.add_argument("--ios-build-error", default="",
                        help="the iOS app didn't build (e.g. signing): the run's iPhones show "
                             "every test UNAVAILABLE with this error")
    parser.add_argument("--platform", choices=sorted(PLATFORM_EXTENSIONS),
                        help="platform of --app")
    parser.add_argument("--tests-dir", default="visual-tests")
    parser.add_argument("--branch", default=default_branch(),
                        help="branch label shown in the viewer (default: GITHUB_REF_NAME when set)")
    parser.add_argument("--commit", default=None,
                        help="commit id under test (default: HEAD of the current checkout, "
                             "else GITHUB_SHA)")
    parser.add_argument("--commit-message", default=None,
                        help="commit message shown on the run list (default: the message of --commit "
                             "from the current checkout)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                        help="owner/repo the run belongs to in the viewer (default: GITHUB_REPOSITORY)")
    parser.add_argument("--cases", default="",
                        help="comma-separated case names to run (default: every case)")
    parser.add_argument("--build-request", default="",
                        help="viewer build id, when the build was started from the viewer")
    parser.add_argument("--timeout", type=int, default=3600,
                        help="seconds to wait for the run, including queueing for a device")
    parser.add_argument("--no-wait", action="store_true",
                        help="exit 0 as soon as the viewer accepted the upload (non-zero when it "
                             "refused it) instead of waiting for the verdict")
    parser.add_argument("--poll", type=float, default=10.0)
    args = parser.parse_args(argv)
    args.url = args.url.rstrip("/")
    if args.commit is None or args.commit_message is None:
        sha, message = default_commit()
        if args.commit is None:
            args.commit = sha
        if args.commit_message is None:
            if args.commit == sha:
                args.commit_message = message
            elif re.fullmatch(r"[0-9a-fA-F]{7,64}", args.commit or ""):
                args.commit_message = git_output("log", "-1", "--format=%B", args.commit).strip()
            else:
                args.commit_message = ""
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
