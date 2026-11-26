# MonkeyTester — Architecture

> **If the architecture changes (new services, new ports, new components, major refactor), update this file and note it in AGENTS.md.**

## System Diagram

```
        Linux host (192.168.125.70)                  Mac (192.168.125.5)
┌──────────────────────────────────────┐   ┌───────────────────────────────────┐
│ MonkeyTester (this repo)             │   │ MonkeyTester-Plugin-Mobile-Controller    │
│                                      │   │ (separate repo)                   │
│  app.py :8089                        │   │                                   │
│   ├─ web UI and run results          │   │  device_controller.py :8090       │
│   ├─ device list / frame / stream ───┼──►│   ├─ Appium, devicectl ─► iPhone  │
│   │  proxies                         │   │   └─ adb, ffmpeg ─► Android       │
│   ├─ launches runs                   │   │                                   │
│   └─ builds.py ─ build_plugin_client ┼─┐ └───────────────────────────────────┘
│  run_on_device.py                    │                    ▲
│   └─ test_runner_core.py             │                    │ HTTP
│       ├─ controller_client.py ───────┼────────────────────┘
│       ├─ vision_analyzer.py ─► vision model API
│       └─ plugin_client.py ───────────┼──┐  HTTP, PLUGIN_API.md (config.json plugins / MT_PLUGINS import)
└──────────────────────────────────────┘  │
                                          ▼
                          ┌───────────────────────────────────┐
                          │ Device plugins (separate repos),  │
                          │ on the Linux host or the Mac      │
                          │  MonkeyTester-Plugin-Esp32 :8091  │
                          │   └─ esptool, serial ─► ESP32     │
                          └───────────────────────────────────┘

 builds.py ── HTTP, BUILD_PLUGIN_API.md (build_config.json) ──►┌────────────────────────────────────┐
                                                              │ Build plugin: build-local :8094    │
                                                              │ (separate repo, on the build host) │
                                                              │   ├─ fetches the branch ◄── Gitea  │
                                                              │   │   or GitHub (read-only token)  │
                                                              │   ├─ runs the app repo's per-platform build scripts
                                                              │   ├─ log / cancel for the build    │
                                                              │   └─ uploads with GET /ci/submit.py ─┼─► POST /api/run-test (build_request) ─► app.py
                                                              └────────────────────────────────────┘
```

MonkeyTester holds the web view and decides what to do next by combining the vision
model's answers with the test steps. It has no adb, Appium, devicectl or capture
code. The Mobile Controller's HTTP API (`API.md` in that repo) is the only coupling,
and it knows nothing about tests or vision models.

Other hardware is reached through **device plugins**: separate services, each owning one
kind of hardware, that implement the contract in `PLUGIN_API.md` (health, devices,
actions with param schemas, and jobs). MonkeyTester-Plugin-Esp32 (port 8091, Linux or Mac) is the
first. Tests call them with `PLUGIN:` steps; the plugins to use come from the `plugins`
map in `Run/config.json` (Settings → Device Plugins; a `MT_PLUGINS` environment variable
is imported into that map once at viewer start and ignored after that).

Builds are reached through **build plugins**: separate services that implement
`BUILD_PLUGIN_API.md` (health, code host config, repo check, branches, cases, start a build,
build state, build log, cancel, example build script). `build-local` (port 8094, the
MonkeyTester-Plugin-Build-Local repo) fetches the branch from **Gitea or GitHub** with a read-only
token, runs the app repo's per-platform build scripts (`recipes` in `/health`, e.g.
`.monkeytester/build-android.sh`) on its own host — a Mac builds Android and iOS, a Linux host
Android only — and uploads the apps with the viewer's `GET /ci/submit.py` and `--no-wait`, so
the run attaches to the build. The plugin also answers `GET /builds/<ref>/log` (which the viewer
proxies so build.html can tail it while the build runs) and `POST /builds/<ref>/cancel`. The
plugin holds the code host's URL and token; the viewer keeps only the plugin address, optional
pairing token and the projects (`build_config.json`), and never sees a code host token. Another
build system needs only a plugin that implements the same contract.

## Component: app.py (port 8089)

**Framework:** Python ThreadingHTTPServer + SimpleHTTPRequestHandler

**Responsibility:** Web UI, test result viewer, API endpoints, device proxies.

```
TestViewerHandler (HTTP request handler)
  ├─ GET: pages (index, test, logs, player, stream, config, device-config→plugins redirect, appearance→config redirect, plugins, models, run-status)
  ├─ GET: API endpoints (runs, test data, metadata, dashboard)
  ├─ POST: /api/run-test (build plugin upload of app + tests for a known build, launch run_on_device.py)
  ├─ POST: /api/config, /api/config/devices
  ├─ Build & Test: /api/config/build*, /api/builds* incl. /log and /cancel (builds.py → build plugins), GET /ci/submit.py
  ├─ Controller proxies: /devices, /api/devices/events, /stream/{id}, /api/frame/{id}
  ├─ Health: /health (open), /api/status (each dependency's state)
  └─ Static file serving: /static/<run>/<path>, /convert-video/<run>/<video>
```

### Template System
All HTML pages use `{{CSS}}` and `{{JS}}` placeholders replaced at serve time by `SHARED_CSS` and `SHARED_JS` constants defined in app.py.

### Run Management
- Scans `Run/` for run directories
- Reads `run_metadata.json` for status, pass/fail counts
- Checks `pending_tests.json` for active runs

### Web Video Conversion
- `/convert-video/<run>/<video>` converts saved MP4s to WebM (VP9 + Opus)
- Cache in `Run/.cache/`, keyed by MD5 of video path + mtime + size

### Test Launching (/api/run-test)
1. Hand-parse multipart/form-data (stdlib only)
2. Resolve targets: the chosen device ids, or the devices the Mobile Controller reports for the chosen platform
3. Stage files in `Run/.uploads/<upload_id>/`
4. Launch `run_on_device.py` as a detached subprocess
5. Poll `Run/` for the new run directory (up to 15s) and return `run_id`
6. Background thread waits for the subprocess and cleans up staging

CI uploads through the same route (see `ci/mt-submit/` and "API — CI" in `AGENTS.md`). Runs submitted to the viewer execute one after another in submission order, Android and iOS alike (they share the one ESP32 board): each runner is started with the previous run's process id (`--after-pid`) and waits, in phase `queued`, until that run has finished. Runs also queue per device with a `flock` under `Run/.locks/`.

## Component: Runner

`run_on_device.py` asks the controller for its API version and for the platform of each requested device, then runs the suite on each device through `test_runner_core.py`. If the controller is unreachable, or a device isn't connected, the run fails with that reason.

Every device operation is an HTTP call in `controller_client.py`. Coordinates are device pixels everywhere, and the press overlay gets each tap and swipe from `press_events.py`.

`SCRIPT` and `DIAGNOSE` steps run on this host, not on the Mac.

`PLUGIN` steps go through `plugin_client.py`. On the first use of a plugin in a run it
checks `GET /health` (contract `monkeytester-plugin`, version 1) and caches
`GET /actions`, whose param schemas turn `key=value` strings into int, float, bool or
JSON. The step posts a job, loops on `GET /jobs/<id>?wait=30` until the job is terminal,
prints the job's log into the console log, and passes only on `passed`. If the job isn't
done 15 seconds after its timeout, the runner cancels it and fails the step.
A `PLUGIN` step may carry an inline block (indented lines up to `END`). The parser
(`parse_steps` in `test_runner_core.py`, used for test files and `INSTRUCTION.txt`) keeps
the block on the step as `block`, so its lines are never parsed as steps; at run time the
lines get `${VAR}` substitution and are sent joined with `\n` as the `script` param, and
only to an action whose schema has `script`. A block without `END` or a stray `END` gives
a step with an `error` that fails when it runs.

`SNAPSHOT` and `ASSERT_LAYOUT` steps (see `SNAPSHOTS.md`) read `GET /devices/<id>/screen`
(including `scale`) and `GET /devices/<id>/elements` through `controller_client.py`.
`snapshots.py` decodes the screenshot, scales it to 1x, and compares it with
`<test dir>/snapshots/<platform>/<W>x<H>@<scale>x/<name>.png`. The baselines arrive with the
upload. A missing baseline fails like a mismatch. `--record-snapshots` saves candidates without
comparing and marks the run `record_snapshots` in its metadata, so `/api/runs/<run>/result`
reports `recording`, never `passed`. `--case` stores the case name, which
`/api/runs/<run>/snapshots.zip` uses to lay out new baselines.

## Live View

- **Android:** the page shows `/stream/{id}`, MJPEG proxied from the controller.
- **iOS:** there is no stream, so the page polls `/api/frame/{id}`.

Recordings are made on the controller and downloaded to the run's `outputs/` when they stop.

## Configuration Files

| File | Purpose |
|------|---------|
| `Conf/device_names.json` | Device id → friendly name |
| `Run/config.json` | Viewer config (video_size, etc.) |
| `build_config.json` | Build & Test: build plugins `{name: {url, token}}` and projects `[{repo, plugin, branch}]` (0600, git-ignored) |
| `Run/.builds/` | Build records, one `build_<12 hex>.json` per Build & Test build |
| `Run/.uploads/` | Transient staging for web-uploaded apps and tests |
| `Run/.cache/` | WebM conversion cache |

## Talking to other components

All three services fail the same way, `{"error": {"code", "message"}}` with a meaningful
status, and all report `{ok, service, api_version}` from an open `GET /health`.

| Client | Talks to | Version check |
|--------|----------|---------------|
| `controller_client.py`, `app.py` | Mobile Controller API version 2 | At the start of each run, and in `/api/status` |
| `vision_analyzer.py`, `screenshot_diff.py` | Any OpenAI-style chat completions endpoint, normally MonkeyTester-Plugin-AI | In `/api/status`, for MonkeyTester-Plugin-AI |
| `plugin_client.py`, `app.py` | Device plugins, `PLUGIN_API.md` contract_version 1 | On a run's first step for each plugin, and in `/api/status` |
| `build_plugin_client.py`, `builds.py` | Build plugins, `BUILD_PLUGIN_API.md` contract_version 1 | When a plugin is registered (stored only if `/health` matches), and on every Settings load |

Device plugins report errors as `{"error": "<message>", "code": "<code>"}` (their own
contract); `plugin_client.py` also accepts the `{"error": {"code", "message"}}` shape.

**Vision calls** all go through `vision_analyzer.chat()`, so timeouts, auth and retries match.
It retries a refused connection or a 502, 503 or 504 twice, because a model that's restarting
recovers by itself. It doesn't retry other errors or a timeout, and it raises `VisionError` with
the reason. The 310 second timeout is longer than MonkeyTester-Plugin-AI's own limit of 300, so its
answer arrives first.

**Vision provider** is the MonkeyTester-Plugin-AI plugin's choice, not the viewer's: the runner always
posts to the plugin's `/v1/chat/completions` with the pairing token, and the plugin answers with
its local model, forwards to a remote OpenAI-compatible server (Remote server: address, model,
resize switch and optional API key, all stored by the plugin), or forwards to GitHub Copilot
(Settings → Device Plugins → monkeytester-ai → Configure). The plugin's own address is set on
the same page and stored in `Run/config.json` as `visual_plugin_url` — the one source for the
viewer and the runner — except in managed mode, where a launcher app passes
`MT_MANAGED_AI_PLUGIN_URL` + `MT_MANAGED_AI_PLUGIN_TOKEN` (both required) and those win in the
viewer and the runner (inherited env) without being written to config.json; the panel then shows
the address read-only and the API refuses address/token edits with 409. A `monkeytester-ai` entry in
`MT_PLUGINS` is imported once at viewer start (logged, skipped in managed mode) and then ignored; the other device plugins are registered in the **Manage
device plugins** box on that Settings page (the `plugins` map in `Run/config.json`; a
`MT_PLUGINS` environment variable is imported into that map once at viewer start and
ignored after that). The plugin keeps the GitHub sign-in; the viewer stores no GitHub token and has no
Copilot code. A config.json still saying `visual_provider: "copilot"` stops the runner at start.

**Vision settings** come from the environment unless an endpoint is saved in Settings, which wins.
(Legacy: an old launcher app used this and the managed mode above; both stay
harmless when the variables are unset.)

**Device lists** can be partial. If one platform fails, the other's devices stay usable and the
reason appears as the error next to them.

## Security

- Every GET and POST path is checked by `has_path_traversal` before routing, because run names
  and file paths from URLs are joined onto `Run/`. Behind that, `security_utils.py` holds the
  shared checks: run routes accept only `run_N` names, and every route that maps a URL or upload
  name to a file (`/static/`, `/convert-video/`, `/fonts/`, staged uploads) goes through
  `safe_join(root, *parts)` (realpath + commonpath, so symlinks out of the root 404 too).
- Request limits: POST bodies need a `Content-Length` (chunked → 411); JSON bodies are capped at
  1 MiB and `/api/run-test` uploads at `MT_MAX_UPLOAD_MB` (default 600 MiB, read into memory) —
  413 when over; malformed or non-object JSON is 400. The handler has a 60 s socket timeout
  (per socket operation, so the SSE streams keep working).
- Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and
  `Referrer-Policy: same-origin`; an exception in a route is logged server-side and answered with
  500 `{"error": "internal server error"}`, never a traceback; an unknown GET is 404.
- Request values that reach the runner's argv (device ids, branch, iOS bundle id, case) are
  allowlisted; subprocesses use argv lists, no shell — except the `SCRIPT`/`DIAGNOSE` test steps,
  which run the test author's shell command by design. `Run/config.json` is written 0600.
  See `SECURITY.md` ("Code hardening").
- **Login is always required** (`viewer_auth.py`). Every page and API needs a session except
  `/login`, its fonts, `/health`, `/api/auth/status`, `POST /api/login`, `/api/logout`,
  `/api/theme` and `GET /ci/submit.py`. Pages without a session 302 to `/login?next=…`, APIs
  401. There is no sign-up: with no users, `admin`/`admin` is seeded (startup warning, banner
  for the admin until changed); admins create the other accounts on Settings → Users.
- Accounts: `auth_config.json` (0600) — `users {name: {password_hash, role, created_at}}`
  (scrypt, per-user salt; an old bare SHA-256 hash is accepted once and rehashed; plaintext is
  never accepted) and `sessions {sha256(token): {user, created}}`. Cookie `mt_auth`,
  `HttpOnly; SameSite=Strict` (no `Secure`: the viewer serves plain HTTP). Per-IP login
  throttle: after 5 failures each further failure doubles the wait (429 + `Retry-After`).
- CSRF: a cookie-authenticated POST must carry an `Origin` (else `Referer`) whose host equals
  `Host`, else 403.
- CI token (`Authorization: Bearer`, `hmac.compare_digest`) opens only `POST /api/run-test` and
  `GET /api/runs/<run>/result`. Without it, `/api/run-test` takes an upload only for a viewer
  build that is still waiting for its upload (no run attached yet) — how build-local uploads.
- The runner proves itself on `POST /api/press-event` with `MT_RUNNER_TOKEN`, a secret made at
  each viewer start and passed in the runner's environment.
- Service tokens (Mobile-Controller, every plugin) live in `service_tokens.json` (0600,
  plaintext at rest); the viewer's environment mirrors that file (old token env vars dropped),
  so the runner inherits exactly the Settings values. See `SECURITY.md`.

## Testing

```bash
python3 -m unittest test_ci_delivery test_controller_client test_platform_targeting test_vision_analyzer test_app_api test_plugin_client test_snapshots test_builds test_build_plugin_client test_platform_steps test_mt_file_payload test_mt_payload_upload test_test_variables test_stale_runs test_projects test_viewer_auth test_security_hardening
```

These use stubs for the controller, the vision model and a build plugin (`fake_build_plugin.py`), and run the real `app.py` server. The
same list is in `flake.nix` (`nix run .#tests`) and `.github/workflows/test.yml`.

## External Dependencies
- MonkeyTester-Plugin-Mobile-Controller, on the Mac, for all device access
- `ffmpeg`, for WebM conversion of saved videos
- Vision model API (Qwen2.5-VL or similar), called by `vision_analyzer.py`
- Device plugins such as MonkeyTester-Plugin-Esp32, optional, only for tests with `PLUGIN` steps
- A build plugin (MonkeyTester-Plugin-Build-Local, `build-local`, port 8094), for Build & Test: it fetches the code from Gitea or GitHub and builds it on its own host

## Nix Flake
`flake.nix` provides a dev shell with Python 3 and ffmpeg. The viewer and runner need only the standard library. Use `nix develop -c ...` on a fresh machine.
