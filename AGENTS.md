# MonkeyTester — Agent Guide

## Purpose
The web view and test orchestration for AI-driven MonkeyTester testing. Tests use a vision model (Qwen2.5-VL or equivalent) to find UI elements and verify screens, on real iPhones and Android phones. Devices are owned by a separate service, MonkeyTester-Plugin-Mobile-Controller, which this repo reaches only over HTTP. Build & Test starts builds through a build plugin (`BUILD_PLUGIN_API.md`, see "API — Build & Test"); the plugin fetches the code from Gitea or GitHub and builds it on its own host, and the apps reach the viewer through `ci/mt-submit/submit.py` (see "API — CI upload").

## Setup (do this first)
Install the dependencies with the steps in README.md → **Install** before running or testing anything; that section is the single source for them, so don't install from memory. In short:
- **Nix available** (`command -v nix`): nothing to install. `nix develop` gives a shell with Python 3 + ffmpeg, `nix run .#tests` runs the CI tests, `nix run . -- <port>` starts the viewer. Run them from the repo root (the viewer keeps `Run/` and `Conf/` next to `app.py`).
- **No Nix:** Python 3.10+ and `ffmpeg` from the OS package manager. The viewer and runner use only the Python standard library; there is no `requirements.txt` and no `pip install` step.
- Verify with the unit tests (README.md → Install → Verify). If a step in README.md → Install doesn't work as written, fix README.md in the same PR.
- Device plugins and the Mobile Controller are separate repos, each with its own AGENTS.md and README.md → Install. The viewer runs and its tests pass without any of them.

## Critical Rules
- **Never modify the code in place.** Don't edit the main checkout (`/mnt/raid1/Workspace/MonkeyTester/MonkeyTester`), which serves the live instance on port 8089, and don't test changes there. Make every change on a branch in its own git worktree and raise a PR. To test it, start a new instance on a new port with `/mnt/raid1/Workspace/monkeytester-pr-test/pr-test.sh start <pr-number>`, which gives the PR its own worktree, its own copy of `Run/` and `Conf/`, and a free port in 8100-8199. Use `refresh` after pushing more commits and `stop` when finished.
- **No device code in this repo.** adb, Appium, devicectl and video capture belong in MonkeyTester-Plugin-Mobile-Controller. Everything here goes through its HTTP API (`API.md` in that repo).
- **If any endpoint or functionality changes, update this AGENTS.md immediately.** Document the new/changed endpoint, method, path, request/response format, and purpose.
- **If the architecture changes (new services, new ports, new components), update `ARCHITECTURE.md` and note it in AGENTS.md.**
- **A new dependency (Python package, system tool) goes into README.md → Install and into `flake.nix` in the same PR.** Prefer the standard library: the viewer has no third-party Python dependencies today.

## Architecture Overview

```
MonkeyTester (port 8089) — app.py (ThreadingHTTPServer)
  . Web UI for viewing test results + live device view
  . Proxies live streams and frames from the Mobile Controller
  . Serves run results from Run/run_*/
  . Accepts test-run uploads from Build & Test and CI only, and launches test runs via POST /api/run-test (a manual upload is refused)

Runner — mt-tests/scripts/run_on_device.py
  . Runs test steps, calling the vision model and the Mobile Controller

MonkeyTester-Plugin-Mobile-Controller (Mac, port 8090) — separate repo
  . Owns every iPhone and Android phone: input, install, screenshots, stream, video

Device plugins (Linux or Mac) — separate repos, contract in PLUGIN_API.md
  . MonkeyTester-Plugin-AI (Mac, port 8091): the vision model; address set in Settings → Device Plugins
  . MonkeyTester-Plugin-Esp32 (port 8091 on the Linux host): esp32-sensor, the sensor boards
  . MonkeyTester-Plugin-Server (port 8093, fake server 8092): esp32-server, the hub boards
  . Reached by PLUGIN test steps through mt-tests/scripts/plugin_client.py

Build plugins (Linux or Mac) — separate repos, contract in BUILD_PLUGIN_API.md
  . build-local (MonkeyTester-Plugin-Build-Local, port 8094): fetches the code from Gitea or GitHub
    and builds it on its own host (Android + iOS on a Mac, Android only on Linux)
  . Holds the code host kind/URL + a READ-ONLY token and the viewer URL; reached by builds.py
    through build_plugin_client.py
  . Registered in Settings → Configuration → Build & Test (build_config.json)
```

The device plugin contract is `PLUGIN_API.md` and the build plugin contract is
`BUILD_PLUGIN_API.md`, both at the repo root. The architecture includes device plugins
and build plugins; see `ARCHITECTURE.md`. Nothing in the viewer or its docs may assume
which forge hosts an app repo: forge-specific code lives in the build plugins.

## app.py — Endpoints (port 8089)

Served by Python's ThreadingHTTPServer. Pages use `{{CSS}}`/`{{JS}}` template substitution.

**Login is always required** (see API — Authentication): every page and API needs a session except `/login`, `/fonts/*`, `/health`, `/api/auth/status`, `POST /api/login`, `POST /api/logout`, `GET`/`POST /api/theme` and `GET /ci/submit.py`. Without a session a page 302s to `/login?next=<path>` and an API answers 401. The CI routes also take the CI token, `POST /api/press-event` the runner token.

### Page Routes (GET)
| Route | Serves |
|-------|--------|
| `/` or empty | `index.html` — run list + dashboard |
| `/test.html` | Run viewer: test cases, steps and screenshots, with a device switcher for multi-device runs |
| `/test-b.html` | Same page as `/test.html` with the device summary always shown. `/test.html` shows it (one card per device, click to switch) whenever the run has more than one device; `?device=<id>` opens a given device |
| `/logs.html` | Raw summary/log viewer |
| `/player.html` or `/player` | Video player for recordings |
| `/build.html` or `/build` | Build & Test: pick a project, branch, platforms and cases; the project's build plugin starts the app repo's CI workflow, which builds the app, and the runs attach to the build. Signed-in users; the sidebar link shows unless `MT_DISABLE_WEB_RUNS` hides it |
| `/stream.html` | Live device viewer |
| `/login` or `/login.html` | `login.html`, public: username + password form posting `/api/login`, then to `?next=` (same-site paths only) or `/`; a browser that is already signed in is sent on (302 server-side, or by the page's `/api/auth/status` check after a cross-site link). No sign-up page or route exists |
| `/users` or `/users.html` | Settings → Users (admins only; others get 403): the account table (role select, Reset password, Delete — disabled for yourself) and a Create user form (username, initial password, role). "Users" in the Settings submenu, shown to admins only (`adminOnly` in `NAV_SETTINGS_LINKS`) |
| `/config` or `/config.html` | Configuration: Build & Test Projects, Test Variables, Account (own password, Log Out), and for admins the CI token and Service tokens sections (build plugins themselves are registered on the Plugins page). Signed-in users; "Configuration" in the sidebar's Settings submenu, and the submenu's first entry |
| `/device-config` or `/device-config.html` | 302 to `/plugins` — the standalone Device Config page is retired; its options live in the **Controller Plugin** section on the Plugins page (the Settings submenu has no separate entry for it) |
| `/configuration.html` or `/configuration` | 302 to `/plugins` (old Device Config URL; the section now lives on /plugins) |
| `/appearance` or `/appearance.html` | 302 to `/config` — the Light Mode toggle now lives in the Configuration page's Appearance section (the Settings submenu has no Appearance entry) |
| `/plugins` or `/plugins.html` | Plugins, in three sections: **Test Plugins** (device plugins and monkeytester-ai), **Build Plugin** (its Configure panel holds the code host) and **Controller Plugin** (the one controller plugin; its Configure panel holds friendly names for the devices it serves, moved from the retired Device Config page). Every section: title + "?" generic intro box, an **Add row** (name, address, optional pairing token; the controller's Add row is only an address and shows only while none is set), its status line, then a **table** of the added plugins — Name, Status, Address (editable; Save address greyed until it differs), Details, Actions (Save address, Configure where a panel exists, Remove). Configure opens the panel inline under the row. Signed-in users |
| `/models` or `/models.html` | 302 to `/plugins` — the vision provider (Local model or GitHub Copilot) is chosen in the monkeytester-ai plugin's Configure panel there |
| `/copilot` or `/copilot.html` | 302 to `/plugins` — GitHub Copilot is signed in on the monkeytester-ai plugin |
| `/run-status` or `/run-status.html` | Run Status: active and queued runs/tests. Signed-in users |
| `/settings.html` or `/settings` | 302 to `/config` — the old single Settings page is retired; every settings section is its own page |
| `/ci/submit.py` | The CI upload script `ci/mt-submit/submit.py` (`text/x-python`), public (it is this repo's own source; build plugins fetch it without a credential), so any CI can run `curl -fsS "$MT_URL/ci/submit.py" -o "$RUNNER_TEMP/mt_submit.py" && python3 "$RUNNER_TEMP/mt_submit.py" --url "$MT_URL" …` without a checkout of this repo |

### API — Run Management
| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/runs?sort=newest` | List all runs (newest/oldest/name_asc/name_desc). Returns `[{name, date, is_running, status}]` |
| GET | `/api/run/<run>?device=<device_id>` | List run files; optional device selects a multi-device artifact directory |
| GET | `/api/test/<run>/<test>?device=<device_id>` | Get parsed test data; optional device selects a multi-device result. `test_source` = `{test_file, test, instruction}`: the test's `.txt` and its case's `INSTRUCTION.txt` (or null) from the run's `tests/` copy, shown by test.html under **Show test file**; null for runs made before the runner kept the copy |
| GET | `/api/metadata/<run>?device=<device_id>` | Get root or selected-device `run_metadata.json` content |
| GET | `/api/device-summary/<run>` | Get `{devices, active_device_id}` for the device-summary viewer |
| GET | `/api/summary/<run>` | Get `summary.txt` or `run_metadata.json` as text |
| GET | `/api/stats/<run>?device=<device_id>` | Get aggregate or selected-device `{passed, failed, total}` |
| GET | `/api/pending/<run>?device=<device_id>` | Get root or selected-device `pending_tests.json` for active runs |
| GET | `/api/run-events/<run>?device=<device_id>` | Server-sent events for test.html's live updates. Sends `event: run` with `{pending: <the /api/pending body for that device>}` on connect, then again whenever the run's state changes on ANY device: `run_metadata.json`, `pending_tests.json` or `results/*.json` written, or an `outputs/*.mp4` appearing (`run_state_signature`, checked every `RUN_EVENTS_CHECK_SECS` = 0.5 s; console.log and screenshots don't count). Otherwise a `: keepalive` comment every 15 s. 404 for an unknown or invalid run name. test.html re-renders only on these events and falls back to polling `/api/pending` every 2 s while the stream is down or the browser has no `EventSource`. |
| GET | `/api/logs/<run>` | Get `console.log` text for a run |
| GET | `/api/runs/<run>/snapshots.zip` | Zip of the run's `missing`/`mismatch`/`recorded` SNAPSHOT captures as `mt-tests/<case>/snapshots/<platform>/<class>/<name>.png` (404 if none). `?count=1` returns `{count}` instead. |

### API — Configuration
All of these need a session (writes: plus a same-site `Origin`/`Referer`). `GET`/`POST /api/theme` stay public (the login page uses them).

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/config` | Load config (`{"video_size": "100%"}`) |
| POST | `/api/config` | Update config (merges into existing); a `test_variables` key is validated as a `{NAME: value}` map of strings (400 with the reason otherwise) |
| GET | `/api/config/devices` | Load device names from `Conf/device_names.json` as `{device id: name}` |
| POST | `/api/config/devices` | Save device names. Body: `{device id: name}` |
| GET | `/api/stream-device/<run>?device=<device_id>` | How to show the run's device live: `{status: "mapped", device: {path, name}}` for Android (`/stream/<path>`), `{status: "ios", serial, label}` for iOS (poll `/api/frame/<serial>`), or `{status: "unavailable", reason}` when it isn't connected. |

### API — Dashboard
| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/dashboard?range=1m&project=` | Dashboard chart data: all runs (or one project's, same `project` values as `/api/runs`), bucketed by day (≤6m) or month (1y). UI dropdown offers `6m` / `3m` / `1m` (default) / `1w`; the API also accepts `1y` and legacy `period=days|months|years` (→ 1m/6m/1y). Returns `{all_runs: {...}}` with labels/passed/failed/running arrays. |

### API — Health
| Method | Route | Description |
|--------|-------|-------------|
| GET | `/health` | Readiness check, open: `{ok, service, api_version}` and nothing else |
| GET | `/api/status` | Whether each dependency is usable: `{ok, service, api_version, controller: {ok, url, api_version, appium, adb, error?}, vision: {ok, endpoint, checked, api_version?, model?, model_loaded?, error?}, plugins: {<name>: {ok, url, contract_version, plugin, error?}}}`. `vision.checked` is false for an endpoint that isn't MonkeyTester-Plugin-AI, because only that has `/health`. `plugins` has one entry per `MT_PLUGINS` plugin from its `GET /health` (3 s timeout; `{}` when none are configured; a malformed `MT_PLUGINS` shows as a `MT_PLUGINS` entry with the error). Plugins are optional, so they don't affect the top-level `ok` |
| GET | `/api/plugins` | The Settings page's view of the active plugins — `MT_PLUGINS` minus monkeytester-ai, plus monkeytester-ai at its Settings address: `{plugins: [{name, url, source: "MT_PLUGINS"|"settings", ok, contract_version, plugin, capabilities, devices, config, error?}], ai_plugin: {name, url, configured, token: {configured, last4}, managed}, error?}` (the monkeytester-ai row also carries `managed`: true when `MT_MANAGED_AI_PLUGIN_URL` + `_TOKEN` supply its address and token) (`error` for a malformed `MT_PLUGINS`). Per plugin, all plugins at once: `GET /health` for state/capabilities and `GET /devices` for the connected boards (3 s each); a plugin that does not answer still lists, with its error. Build plugin registrations from the same `plugins` map are appended with `kind: "build"` and their live build-plugin `/health` (`forge`, `platforms`, `token: {configured, last4}`); device rows have no `kind` |

Any GET whose path contains `..`, a backslash, or an encoded slash returns 400 before routing, so URLs can't leave `Run/`.

### API — Devices
All of these come from the Mobile Controller, using its API version 2.

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/devices` | Connected devices: `[{id, platform, model, friendly_name}]` |
| GET | `/api/devices/events` | Server-sent events relayed from the controller's `GET /devices/events`. Sends `event: devices` with `{devices: [{id, platform, model, friendly_name}], error}` on connect and on every change, including when the controller becomes unreachable (`error` set). `stream.html` listens to it through `watchDevices()` in the shared JS. |
| GET | `/stream/{id}` | Live MJPEG for an Android device |
| GET | `/api/frame/{id}` | One fresh PNG screenshot of any device |

### API — Test Launching
| Method | Route | Description |
|--------|-------|-------------|
| POST | `/api/run-test` | Only accepts a `build_request` naming a Build & Test build (CI submits with one); a manual upload with no known build is a 400. Upload an app (`.apk`, or for iOS targets `.ipa` or `.zip` holding a `.app`) + test files, launch `run_on_device.py`. An iOS app alone is rejected if any Android target is selected. To test both platforms in one run, send the `.apk` in `apk` and the iOS app in `ios_app`. With `device_mode` `android`/`ios`/`all` the run picks its devices when it starts and waits up to 5 minutes for a missing platform (see API — CI); the upload itself never fails for a missing device. Multipart form: `apk` (file), `tests` (multiple `.txt` files, must include `INSTRUCTION.txt`), `device_mode` (`specific`, `android`, `ios`, or `all`), and `device` (space-separated device ids for `specific`); optional `debug`/`no_video`/`record_snapshots` (`on` → `--record-snapshots`), `case` (case folder name → `--case`), and `snapshots` file parts whose filename is `snapshots/<android|ios>/<W>x<H>@<scale>x/<name>.png` (PNG, ≤ 8 MB, ≤ 1000 files; staged next to the tests, anything else is a 400), and `payloads` file parts: the files tests reference with `@file:<path>`, named `<path>` (single case) or `<case>/<path>` (multi-case) — relative, normalized, safe names, ≤ 4096 B, ≤ 32 per case, not colliding with a test/snapshot, no `.txt` at the case root; staged at that path next to the tests, anything else is a 400. `android`, `ios` and `all` resolve devices from the Mobile Controller. In `specific`, an `IP:PORT` id is treated as Android and any other id must be connected to the controller, which gives its platform. If no iPhone is connected, `all` records iOS as `not_supported`. Returns 400 when the controller is unreachable. |

### Stale-Run Watchdog
A runner can die mid-run without finalizing anything (host shutdown sends it SIGTERM, a crash or kill -9 leaves metadata at `running`). The viewer treats a run whose directory has no file modified within `MT_STALE_RUN_SECS` (default 3600 s) as no longer running: `_is_running` stamps `run_metadata.json` `status: "interrupted"` plus an `error` reason, marks in-flight device entries `interrupted`, and terminalizes every `pending_tests.json` progress file (`phase: "interrupted"`). The run then badges as **Error**, can be deleted, and the CI verdict reports `failed`. `run_on_device.py` also installs a SIGTERM handler calling `core.mark_run_interrupted`, so a clean shutdown finalizes immediately instead of waiting for the threshold (it cannot help on power loss or SIGKILL — the watchdog covers those).

### API — CI
CI uses `POST /api/run-test` and `GET /api/runs/<run>/result` with the CI token as `Authorization: Bearer <token>` (`submit.py` sends `MT_CI_TOKEN`); these are the only routes the token opens, and a wrong token is 401. A session works too. Without either, `/api/run-test` accepts an upload only when its `build_request` names a viewer build that is not finished and has no run attached yet (`build_upload_allowed`) — the build plugins upload that way, once per build; otherwise 401.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/runs/<run>/result` | CI verdict. Returns `{run, url, status, reason, passed, failed, total}`, where `status` is `running`, `passed`, `failed` or `recording` (a record-mode run that would otherwise pass; CI treats it as a failure). `passed` requires a completed run, runner exit 0 when known, at least one test, and no failures. A run stuck at `running` with no filesystem activity for `MT_STALE_RUN_SECS` (see Stale-Run Watchdog) is stamped `interrupted` and reported `failed`. 404 if the run doesn't exist. |

`ci/mt-submit/action.yml` (composite action) wraps `ci/mt-submit/submit.py`. Optional inputs: `cases` (comma-separated case names; a name that doesn't exist fails the job) and `build-request` (sent as the `build_request` form field; see API — Build & Test). The viewer also serves `submit.py` at `GET /ci/submit.py` for CI that can't use the action (GitHub resolves `uses:` only from github.com); its `--repo` defaults to `GITHUB_REPOSITORY` and `--branch` to `GITHUB_REF_NAME`. It uploads every `mt-tests/<case>/` folder in ONE upload (one run), waits for its verdict, and fails unless every test passed. Multi-case upload layout: test parts are named `<case>/<file>.txt`, snapshot parts `<case>/snapshots/<platform>/<W>x<H>@<scale>x/<name>.png` and payload parts (every file a case's `.txt` references with `@file:<rel>`, collected by `collect_payloads`, which fails the job before uploading on an absolute/escaping/missing/non-regular/>4096 B ref) `<case>/<rel>` (`plan_test_upload` in app.py; each case needs its own `INSTRUCTION.txt` and a test; the flat single-case layout still works; a `SETUP.txt` at the tests root is uploaded as a `tests` part named `SETUP.txt` and staged at the tests root). The viewer stages `tests/<case>/…`; the runner (`resolve_test_files`) runs the case folders in `SETUP.txt` order when the tests root has one (folders it doesn't name run after the listed ones, in name order; names there that match no folder warn and are ignored), name order otherwise, names each test `<case>__<stem>` (result files, videos) with label `<case> / <stem>` (pending entry `label`/`case`, result `label`/`case`), writes snapshot outputs under `outputs/<case>/`, and records `cases` in run metadata. A failed test stops that device: its remaining tests get status `cancelled` (metadata `cancelled` count); other devices still run. A platform whose app didn't build is sent as `build_failed_<platform>=<error>` plus an optional `build_log_<platform>` file part (submit `--ios-build-error`/`--ios-build-log`, action `ios-build-error`/`ios-build-log`; Android likewise; device_mode `all` or that platform's mode, and no app for it). The viewer passes `--build-failed ios=<error>` and `--build-log ios=<path>`; run_on_device doesn't wait for or lock that platform's devices: `build_failed_device_entries` lists its connected devices (or one "none connected" stand-in). Setup failures (build failed, device locked, install failed) go through `core.record_setup_failure`: every test gets result status `UNAVAILABLE` with `error` and `log` (the build log copied to `devices/<id>/logs/setup_log.txt`, or `console` = the run's console log), pending status `unavailable`, device status `unavailable` with `unavailable` count and `log`. `/api/test-data` and `/api/device-summary` expose `log_url`; test.html shows a **View logs** button. Unavailable tests fail the run (`get_run_result`). With `android-app` and `ios-app` together, the run covers both platforms: the upload carries the `.apk` in the `apk` part and the `.ipa` in the `ios_app` part with `device_mode=all`, and the viewer passes them to the runner as `--apk` and `--ios-app`. The upload is never rejected for a missing device: with `device_mode` `android`, `ios` or `all` the viewer doesn't pick devices but passes `--wait-for-platform <p>` for each platform it has an app for. When the run's turn comes (after `--after-pid`), the runner takes every device of those platforms the controller serves; if a platform has none it shows phase `waiting_for_devices` ("Waiting for an iPhone to connect") and polls every 5 s for up to `MT_DEVICE_CONNECT_WAIT_SECS` (default 300), then fails with "Gave up after 300s waiting for … to connect". `device_mode=specific` still checks the named devices at upload. `app` + `platform` is the single-platform form.

Runs execute one after another, in the order they were submitted to `POST /api/run-test`, whatever their platform: an Android run and an iOS run both use the one ESP32 board, so the second waits for the first to complete. The viewer starts each runner straight away (so the run id is returned at once) and passes it the previous run's process with `--after-pid <pid> --after-run <run>`; the runner shows phase `queued` ("Waiting for run_N to complete") until that process has exited or `Run/<run>/exit_code` exists, before it touches any device. Each run waits only for the one before it, which gives a first-come, first-served chain. Runs started from the command line (not through the viewer) aren't part of the chain.

The runner records its exit status in `Run/<run>/exit_code`. It takes a per-device `flock` in `Run/.locks/` before installing, showing phase `waiting_for_device` while queued (`MT_DEVICE_WAIT_SECS`, default 3600, then it fails).

### API — Build & Test (build plugins)
Implemented in `builds.py` (settings, projects, build records, state) and `build_plugin_client.py` (the client of `BUILD_PLUGIN_API.md`, contract `monkeytester-build-plugin` version 1, checked on `/health`). Every route is open, like the rest of the viewer. The viewer never talks to a forge and never sees a forge token: the plugin holds the forge URL and token.

Build & Test projects live in `build_config.json` next to `app.py` (git-ignored, mode 0600, written atomically): `{"projects": [{"repo", "plugin", "branch"}]}`. Build plugin registrations live in the viewer's `config.json` `plugins` map — the same Settings → Plugins registry as the device plugins: a build entry is `{name: {"url", "token": ""}}` (an object marks a build plugin; a device plugin is the plain url string). The pairing token of every plugin (build and device) lives in `service_tokens.json` (`viewer_auth.plugin_token`, read by builds through `builds.TOKEN_LOOKUP`), sent as `X-Plugin-Token`, never returned — only `{configured, last4}`; a token still in config.json is moved there at viewer start. A repo is in at most one project (run history is keyed by repo). `branch` "" = the forge's default branch. Build records are `Run/.builds/build_<12 hex>.json`.

Migration, once (at viewer start, or the first load when `build_config.json` doesn't exist): a `gitea_config.json` from the old built-in Gitea client becomes the plugin `build-local` at `MT_BUILD_LOCAL_URL` (default `http://127.0.0.1:8094`, no pairing token) plus one project per saved repo with its branch pick; the first time that plugin answers `/health` with `forge.configured` false, the viewer POSTs the saved url+token to its `/config` (`forge_kind: gitea`), then rewrites the file without the token as `gitea_config.json.migrated`. `MT_BUILD_PLUGINS=name=url[,name=url]` is imported the same way, once, into the shared `config.json` `plugins` map (later starts log that it is ignored). Nothing is written when there is nothing to import.

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/config/build` | `{plugins: [{name, url, ok, contract_version, plugin, forge: {kind, label, url, configured, user}, workflow_path, capabilities, token: {configured, last4}, error}], projects: [{repo, plugin, branch}], checks: [{repo, plugin, ok, reason, workflow, default_branch, branch, writable, workflow_path, forge_label}]}`. Each plugin's live `/health` (3 s) and each project's live check (the plugin's `GET /repos/<o>/<r>?branch=`), all in parallel. `ok` = the plugin's token can start builds there and the branch exists; `workflow` true/false/null (a missing workflow is ✗ in Settings but doesn't block saving) |
| POST | `/api/config/build` | Body `{projects: [{repo, plugin, branch?}]}` replaces the project list. Validates (owner/repo, unique repo, registered plugin, branch name) and checks every project with its plugin first; any failure is 400 `{error, checks}` and nothing is stored. Returns the GET body with this save's `checks` |
| GET | `/api/config/controller` | Controller Plugin state: `{configured, source: "settings"/"env"/"", url, live: <Mobile-Controller /health state>}` |
| POST | `/api/config/controller` | ONE action per request: `{"set": {"url": u}}` registers the Mobile Controller — the write lands in `Run/config.json` `controller_url` only when `GET <u>/devices` answers 200 (401/403 → `token_required`: save the service token first; unreachable → 400 `unreachable`), and the first write makes Settings own the slot (the `CONTROLLER_URL` env is then ignored, like the plugins map's ownership marker). `{"remove": true}` writes `controller_url: ""` (Settings keeps owning it; the env does not come back) |
| POST | `/api/config/plugins` | ONE action per request in the shared Settings → Plugins registry (both kinds): `{"set": {"name", "url", "token"?}}` — one `GET /health` read decides the kind: contract `monkeytester-plugin` stores `name: url`, contract `monkeytester-build-plugin` stores `name: {"url", "token": ""}`; for both, `token` string sets the pairing token in `service_tokens.json` (a new device-plugin token must open `GET /devices`, a build-plugin token `GET /config`), `""`/absent keeps the saved one, `null` clears it; responses show only `{configured, last4}`. `{"remove": name}` forgets either entry; 400 `in_use` while a Build & Test project uses a build plugin; `monkeytester-ai` is refused (`reserved_name`). Returns `{ok, plugin|removed, plugins}` |
| GET | `/api/config/build/plugins/<name>/config` | The plugin's `GET /config` (the Configure panel): `{forge_url, token: {configured, last4, saved_at}, user}` — reduced to that shape, so a forge token never passes on |
| POST | `/api/config/build/plugins/<name>/config` | Body `{forge_url?, token?}` relayed to the plugin's `POST /config` (blank/absent token keeps it, `null` removes it); the plugin checks the token with the forge before storing. Returns `{ok, config}`; the plugin's 400 (e.g. `forge_refused` + `forge_status`), 404 and 409 keep their status, other failures are 502, with `{error, code, plugin, plugin_status}` |
| GET | `/api/config/build/branches?repo=&plugin=` | `{repo, plugin, default, preferred, picked, branches}` for the Settings project table's branch picker. The repo need not be saved; `plugin` defaults to the project's (400 when neither) |
| GET | `/api/config/build/template?plugin=` | The plugin's `GET /template` (the workflow file a new project commits at the plugin's `workflow_path`), `text/plain`, downloaded as that path's file name |
| GET | `/api/builds/repos` | `{configured, repos, projects}`: `configured` = at least one project |
| GET | `/api/builds/branches?repo=` | `{repo, plugin, default, preferred, picked, branches}` from the project's plugin. `preferred` is the project's branch (the Settings pick if it still exists, else the default); it is listed first and build.html preselects it. The repo must be a project (400) |
| GET | `/api/builds/cases?repo=&ref=` | `{repo, ref, cases, workflow, plugin, workflow_path, forge_label}`: the `mt-tests/<case>/INSTRUCTION.txt` folders on that branch and whether it has the plugin's workflow (build.html disables the button and names `workflow_path` with the template link when it doesn't). Unknown branch → 404 |
| POST | `/api/builds` | Body `{repo, branch, platforms: [android, ios], cases: [...]}`. Records the build, then the plugin's `POST /builds` with `request_id` = the build id; stores the returned `build_ref` and `ci_url`. A refused start (or an unreachable plugin) is returned as state `dispatch_failed` with the reason (a missing workflow names the plugin's `workflow_path`) |
| GET | `/api/builds` | Last 30 builds, newest first, each refreshed from its plugin's `GET /builds/<build_ref>` (at most every 4 s) |
| GET | `/api/builds/<id>` | One build, refreshed now |

Build records: `{id, repo, branch, platforms, cases, user, created, state, error, plugin, forge_label, build_ref, ci_url, ci_state, ci_conclusion, jobs: {platform: {name, status, conclusion, url}}, runs: [{run_id, case, platforms, attached, status}], checked}`. `/api/builds*` return the stored keys plus `plugin`, `ci_url`, `ci_state`, `ci_conclusion` and `forge_label`, filled from `gitea_run_url`/`gitea_status`/`gitea_conclusion` (label "Gitea") for records written by the old built-in Gitea client; those old records still load and display, but are not refreshed from a forge (no `build_ref`), and a finished one keeps its state.

Build `state`: `dispatching`, `queued`, `building` (CI run or a job in progress), `testing` (a run is attached), then `passed` (every run passed and the CI conclusion is `success`), `failed` (tests failed), `build_failed` (a platform's job failed before uploading anything, or nothing was uploaded), `dispatch_failed`, or `cancelled`. Jobs are matched to platforms by name (`Android …`/`iOS …`), else one job covers every platform. A build the plugin still reports as `pending` 180 s (`DISPATCH_FIND_TIMEOUT`) after it was created becomes `dispatch_failed` ("accepted the start but never started a run"); a `build_ref` the plugin answers 404 for becomes `dispatch_failed` too.

Deprecated aliases of the old built-in Gitea client's routes (kept for old pages and scripts; they act on the plugin of the given repo's project, else `build-local` when registered, else the first plugin):

| Method | Route | Maps onto |
|--------|-------|-----------|
| GET | `/api/config/gitea` | `/api/config/build` plus the old fields `{url, configured, user, repos, branches}` (url/configured/user from the plugin's `/health` forge block; `last4`/`saved_at` empty) and `deprecated` |
| POST | `/api/config/gitea` | Body `{url?, token?, repos?, branches?}`: url/token → the plugin's `POST /config` (`forge_url`, `token`); repos + branches → `POST /api/config/build` (a repo keeps its plugin, new repos get the alias plugin; without `branches` the saved picks stay) |
| GET | `/api/config/gitea/branches?repo=` | `/api/config/build/branches` with the alias plugin |
| POST | `/api/config/gitea/remove-token` | The plugin's `POST /config {token: null}` |
| GET | `/api/builds/workflow-template` | 302 to `/api/config/build/template?plugin=` (`?repo=`/`?plugin=` pick the plugin) |

### Projects
A project is one repo (`owner/repo`) on one build plugin, listed in Settings → Configuration → Build & Test; each has its own run history and Test Variables, while devices and plugins are shared. A run records its project as `repo` in `run_metadata.json` (written by the runner's `run_flags()` from the `MT_REPO` env). `/api/run-test` sets `MT_REPO` for the runner from: the build's repo when the upload carries a known `build_request` (it wins over any `repo` field), else the `repo` form field when it matches `owner/repo` (no dot-only parts). CI sends its repository (the action's `repo` input / `submit.py --repo`, default `GITHUB_REPOSITORY`); Build & Test has the Project picker (`?repo=` preselects). The viewer never passes its own inherited `MT_REPO` on, so a run with no repo is **Unassigned**, as are all runs from before projects existed (not backfilled). The dashboard filters by project (`?project=` in its URL) and shows each run's project; `/api/projects` lists the Settings projects first, then repos seen only in runs. A new repo builds from the viewer once it has its plugin's workflow file (`workflow_path` from the plugin's `/health`, e.g. `.gitea/workflows/monkeytester-dispatch.yml` or `.github/workflows/monkeytester-dispatch.yml`): start from the plugin's template (`/api/config/build/template?plugin=`) and replace its build steps.

**Commit under test.** `submit.py` sends `commit` (full id) and `commit_message` (full message) form fields, read from HEAD of the checkout it runs in (`default_commit`: `git rev-parse HEAD` + `git log -1 --format=%B`, else `GITHUB_SHA` with no message; `--commit`/`--commit-message` override). Build plugins and CI both run it from the checkout root, so no plugin change is needed. `/api/run-test` refuses a `commit` that isn't 7-64 hex (400), cleans the message (`clean_commit_message`: control characters except newline/tab dropped, capped at 4000 chars) and passes both to the runner as `MT_COMMIT`/`MT_COMMIT_MESSAGE` env (never inherited, like `MT_REPO`); the runner's `run_flags()` records them as `commit`/`commit_message` in `run_metadata.json`. `/api/runs` returns `commit`/`commit_message` ("" for older runs); the dashboard run list shows the short id + the message's first line under the date (`runCommitHtml`, full message as tooltip) and test.html's metadata bar shows `Commit:`.

The app repo's workflow (started only by the plugin) builds the chosen platforms and uploads them with `cases` and `build_request` (the composite action or `GET /ci/submit.py`); `/api/run-test` attaches the new run to that build (unknown ids are refused). The viewer starts builds only when a user clicks, never in reaction to an upload, so the two directions can't loop.

### API — Visual Model Configuration
| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/config/visual-model` | Load saved config: `plugin`, `plugin_url` (config.json `visual_plugin_url`, or `MT_MANAGED_AI_PLUGIN_URL` in managed mode), `managed`, `token` (`{configured, last4}`), legacy endpoint/model_name, has_api_key, default_endpoint, and `legacy_copilot` + `legacy_hint` when config.json still says `visual_provider: "copilot"` |
| GET | `/api/visual-model/plugin` | Live view of the AI plugin at its Settings address: installed/selected local models, `provider` (`local`/`remote`/`copilot`), public `copilot` block (`{configured, user, last4, saved_at, model, models, error}`), public `remote` block (`{url, model, models, resize, key: {configured, last4}, error}` — never the key), pairing token `{configured, last4}`, `configured`, `managed`, legacy hint |
| POST | `/api/config/visual-model` | Address: `{plugin_url, api_key?}` sets the monkeytester-ai address after checking `GET <url>/health` (contract `monkeytester-plugin`, plugin.name `monkeytester-ai`) and `GET <url>/config` with the pairing token (given or stored) — nothing is stored on failure (400 bad address/wrong plugin/contract, 502 unreachable/token rejected); `"plugin_url": ""` removes it. Otherwise save `{provider?, model?, copilot_model?, remote_url?, remote_model?, remote_resize?, remote_api_key?, plugin?, generate_token?, clear_token?}` — forwarded to the plugin's `POST /config` (model, copilot_model, the remote keys in one call, then provider). The plugin's error `code`/message are relayed (its 400 stays 400, other failures 502). The Configure panel has ONE staged "Save changes" button (no per-section save controls): it POSTs the token copy, then the address, then the config keys in one body; greyed means no pending change; a failing step reports per field and leaves the button dirty. Old `{endpoint, model_name, api_key, clear_api_key}` shape still works. Managed mode (`MT_MANAGED_AI_PLUGIN_URL` + `MT_MANAGED_AI_PLUGIN_TOKEN` both set): `plugin_url`, `set_token`, `generate_token`, `clear_token`, `api_key`, `clear_api_key` → 409 `{"error": "managed by the MonkeyTester app"}`, nothing stored or sent; the configuration keys still work |
| POST | `/api/visual-model/plugin/copilot/device/start` | Relay to the plugin: `{user_code, verification_uri, interval, expires_in}` |
| POST | `/api/visual-model/plugin/copilot/device/poll` | Relay: `{state: pending/slow_down/done/expired/denied/error, interval?, error?, copilot?}` |
| POST | `/api/visual-model/plugin/copilot/remove` | Relay: sign the plugin out of Copilot (`{copilot}`); the plugin falls back to local |
| POST | `/api/visual-model/test` | Proxies a chat request to the plugin endpoint (which routes to its active provider) |
| POST | `/api/visual-model/models` | `{endpoint, api_key?}` → the endpoint's `/v1/models` (`loaded` flag from llama-server's router). The saved key is only sent to the saved endpoint |
| GET | `/api/llama-loaded-model` | Query llama-server for currently loaded model alias |

### API — Authentication

Login is always required (`viewer_auth.py`; gate: `TestViewerHandler._authorize`, run first in
`do_GET`/`do_HEAD`/`do_POST`). No off switch, no localhost exemption, **no sign-up**.

- `auth_config.json` (next to app.py, git-ignored, 0600): `{"users": {name: {password_hash, role:
  "admin"|"user", created_at, default_password?}}, "sessions": {sha256(token): {user, created}}}`.
  No users → `admin`/`admin` (role admin, `default_password: true`) is seeded and a startup warning
  logged; an old single-user `{username, password_hash, sessions}` file is migrated (that user
  becomes admin). Passwords: scrypt (N=2^14, r=8, p=1, per-user salt); an old 64-hex SHA-256 hash is
  accepted once and rehashed on that login; plaintext never. New passwords: 8+ chars, not `admin`.
- Cookie `mt_auth` (256-bit random), `Path=/; Max-Age=400 days; HttpOnly; SameSite=Strict`,
  re-sent by `/api/auth/status`. Sessions never expire server-side.
- CSRF: a cookie-authenticated POST needs `Origin` (else `Referer`) host == `Host`, else 403
  (a missing header is 403 too). Public POSTs (`/api/login`, `/api/logout`, `/api/theme`) are 403 only
  when a mismatching Origin/Referer is present.
- Login throttle per client IP (`viewer_auth.THROTTLE`): after 5 failures each further failure
  doubles the wait (1 s … 900 s), 429 + `Retry-After`; a success clears the IP.
- Runner token: `viewer_auth.RUNNER_TOKEN`, made at each viewer start, passed to runners as
  `MT_RUNNER_TOKEN`; `press_events.py` sends it as `X-MT-Runner-Token`; accepted only on
  `POST /api/press-event` (wrong value 401).
- `service_tokens.json` (next to auth_config.json, git-ignored, 0600, plaintext at rest):
  `{"ci": {token, saved_at}, "controller": {token, saved_at}, "plugins": {name: {token, saved_at}},
  "imported_env": true}`. On the first start `MT_CI_TOKEN`, `CONTROLLER_TOKEN`/`IOS_CONTROLLER_TOKEN`
  and `PLUGIN_TOKEN`/`MT_PLUGIN_TOKEN_<NAME>` (per registered plugin) are imported once; afterwards
  the env is ignored: `apply_to_environ` drops those variables from the viewer's environment and
  sets `CONTROLLER_TOKEN`/`MT_PLUGIN_TOKEN_<NAME>` from the file (at start and on every token save),
  and the runner gets `apply_to_environ(os.environ.copy())`.
- Roles: `admin` = everything; `user` = everything except Users, the CI token and service tokens
  (those routes answer 403 to users).

| Method | Route | Description |
|--------|-------|-------------|
| POST | `/api/login` | Public. `{username, password}` → `{success}` + session cookie; 401 bad credentials; 429 while throttled |
| POST | `/api/logout` | Public. Ends this browser's session only and clears the cookie |
| POST | `/api/account/password` | Any signed-in user, own account: `{current_password, new_password}`; 403 wrong current password, 400 weak/default password; ends ALL that user's sessions and sets a fresh cookie for this browser |
| GET | `/api/users` | Admin. `{users: [{username, role, created_at, default_password}]}` (no hashes) |
| POST | `/api/users` | Admin, ONE action: `{"create": {username, password, role?}}` (409 exists), `{"reset_password": {username, password}}` (ends that user's sessions), `{"delete": username}` (not yourself, not the last admin; ends their sessions), `{"set_role": {username, role}}` (the last admin can't be demoted). Returns `{ok, users}` |
| GET | `/api/config/ci-token` | Admin. `{configured, last4, saved_at}` |
| POST | `/api/config/ci-token` | Admin. `{"generate": true}` → `{token, configured, last4, saved_at}` (the only time the token is shown; replaces the old one); `{"remove": true}` |
| GET | `/api/config/service-tokens` | Admin. `{controller: {url, configured, last4, saved_at}, plugins: [{name, kind: device|build, url, configured, last4, saved_at}]}` |
| POST | `/api/config/service-tokens` | Admin. `{"set": {"service": "controller"|"plugin:<name>", "token"}}` — checked first (controller `GET <CONTROLLER_URL>/devices` with `X-Controller-Token`, device plugin `GET /devices`, build plugin `GET /config`, with `X-Plugin-Token`); rejected → 400, unreachable → 502, nothing stored. `{"remove": service}` |

| Method | Route | Description |
|--------|-------|-------------|
| GET | `/api/auth/status` | Public. `{signed_in, username, role, default_password, native_back, web_runs_disabled}` (re-sends the session cookie when signed in). `default_password` is true only for an admin whose password is still the seeded `admin` — every page then shows the `#mt-default-password-banner` warning; `role` drives the admin-only Users nav entry; `native_back` when `MT_NATIVE_BACK` is set (pages then show a floating back arrow at the top-left of the content itself, `#mt-back-btn`, greyed out and `disabled` on the home page; the sidebar never carries a Back entry), `web_runs_disabled` when `MT_DISABLE_WEB_RUNS` is set; drives nav visibility |
| GET | `/api/theme` | Public. Returns `{theme: "light"|"dark"}` from the saved config (default `dark`). Pages are served with `data-theme="light"` already on their `<html>` tag when the saved theme is light, so the first paint is right and page switches never flash dark. |
| POST | `/api/theme` | Public. Body: `{theme: "light"|"dark"}`. Saves the theme to the config; used by the theme toggle on the pages (and mirrored by the Settings page via `/api/config`) |

### Static Files
| Route | Description |
|-------|-------------|
| GET `/static/<run>/<path>` | Serve screenshots, videos, JSON from a run directory |
| GET `/convert-video/<run>/<video>` | Convert Android MP4 to WebM (VP9+Opus) via ffmpeg for browser compatibility |
| GET `/fonts/<name>.woff2` | The theme's self-hosted web fonts from `fonts/` (Anton, Space Grotesk, JetBrains Mono; SIL OFL). Only bare `*.woff2` names that exist there are served (`font/woff2`, cached a day); anything else 404s |

`/static/` and `/convert-video/` serve only inside a `run_N` directory: the path goes through `security_utils.safe_join` (realpath + commonpath), so `..`, absolute parts, NUL, backslashes and symlinks pointing out of the run are 400/404.

### Request Hardening (`security_utils.py`)
- `has_path_traversal` runs on every GET **and POST** path (400). Run routes (`/api/run/`, `/api/metadata/`, `/api/logs/`, `/api/summary/`, `/api/stats/`, `/api/pending/`, `/api/cases/`, `/api/test/`, `/api/test-devices/`, …) accept only `run_N` names (`get_device_run_dir` returns None otherwise); `/api/test*/<run>/<test>` 404s a test name with `/`, `\`, control chars or a leading `.`. `POST /api/runs/delete` deletes only `run_N` dirs (never `.builds`/`.uploads`/`.locks`).
- POST bodies: `Transfer-Encoding` without `Content-Length` → 411, bad length → 400, JSON bodies over 1 MiB (`MAX_JSON_BODY_BYTES`) and `/api/run-test` bodies over `MT_MAX_UPLOAD_MB` → 413 before reading. Malformed or non-object JSON → 400 on every JSON route. Handlers read the body through `self._body()` (length checked once in `do_POST`).
- `@sec.harden_handler` on `TestViewerHandler`: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin` on every response; `timeout = 60` (per socket op, SSE streams unaffected); `Server: MonkeyTester` (no Python version); a route exception is logged with its traceback and answered `500 {"error": "internal server error"}`; a GET that sends nothing (unknown path) gets `404 {"error": "not found"}`.
- `/api/run-test` argv: `device` ids (`specific` mode), `branch`, `ios_bundle_id`, `case` are allowlisted (400 otherwise, none can start with `-`); `commit` is hex-only and the commit message reaches the runner only through the `MT_COMMIT_MESSAGE` env, never argv; uploaded app names go through `safe_filename`; staged files through `safe_join`. Outgoing `urllib` calls accept only http(s) (`sec.check_http_url` / `req.type`). `Run/config.json` is always written 0600 (`sec.write_private_json`). Details: `SECURITY.md` → Code hardening.

### Theme
The viewer shares the MonkeyTester website's look. It's defined once in `SHARED_CSS`:
- **Palette:** `:root` (dark, the default) and `:root[data-theme="light"]` (bone paper) set the variables, and every page colour goes through them. The accent is orange, not green, so it can't be mistaken for PASS. `--accent` is the text-safe accent: `#ff6a00` on dark, `#9a3d00` on light. `--acid` is the orange fill and `--on-acid` the text on it. `--pop` is the orange hard-shadow colour. `--pass` (green), `--fail` (crimson, kept well away from the orange accent), `--running` (blue) and `--warn` (yellow: `#ffd400` dark, `#7a5e00` light) are the status colours, with `--on-status` as the text on solid badges. Pages and the dashboard canvas (`themeColor()` in `index.html`) read these variables instead of hex values, and a test (`ThemeFontsTests`) keeps the old GitHub-dark hexes out of `SHARED_CSS`.
- **Brutalist layer:** at the end of `SHARED_CSS`. It covers the fonts (`--display`, `--sans`, `--mono`), square corners everywhere (only the spinners and the press-overlay markers stay round), hard offset shadows instead of blurred ones, mono uppercase labels, solid status badges, and orange primary buttons (`.btn-run`, `.settings-save-btn`, `.save-btn`, `.config-save-all button`).
- `player.html` is standalone (no `{{CSS}}`) and keeps its own styling.

## Test File Format

Test files are `.txt` in `mt-tests/tests/`. Each step is `COMMAND: args`.

| Command | Args | Description |
|---------|------|-------------|
| `START_RECORDING` | optional time_limit (s), e.g. `EXTERNAL_CAPTURE,30`; none = record until stopped | Start recording on the Mobile Controller |
| `STOP_RECORDING` | — | Stop recording |
| `VERIFY` | description | Verify element exists via vision model |
| `CLICK` | description | Find element and click it |
| `INPUT` | description "text" | Click field, delete, type text |
| `SWIPE` | up/down/left/right | Swipe direction |
| `WAIT` | seconds | Sleep |
| `SCRIPT` | shell command | Execute a command on the MonkeyTester host (`shell=True`, by design: the test author's command runs with the runner's privileges — see `SECURITY.md`). adb is not available there, so a script that calls it fails. |
| `UNINSTALL` | package_name | Uninstall app |
| `INSTALL` | apk_path | Install APK |
| `DIAGNOSE` | shell command | Diagnostic, never fails test |
| `SNAPSHOT` | `<name> [mask=status_bar,nav_bar,tag:<id>,rect:x,y,w,h] [tolerance=<%>]` | Compare the screen at 1x with `<case>/snapshots/<platform>/<W>x<H>@<scale>x/<name>.png`. A missing baseline fails, like a mismatch; record mode saves and passes. See `SNAPSHOTS.md`. |
| `ASSERT_LAYOUT` | `<tag> <check> [<tag2>] [tol=N] [gap>=N] [gap<=N]` | Check tagged element positions in pt/dp: `visible`, `inside screen/<tag>`, `centered-x/-y`, `below/above/left-of/right-of`, `no-overlap`, `aligned-left/-right/-top/-bottom/-center-x/-center-y`. See `SNAPSHOTS.md`. |
| `PLUGIN` | `<plugin> <action> [key=value ...] [device=<id>] [timeout=<seconds>]` | Run an action on a device plugin (`PLUGIN_API.md`) and pass only if the job ends `passed`. Args are split shell-style, so values may be double-quoted (`pattern="WiFi connected"`); `${VAR}` is resolved first. `device=` and `timeout=` are step options, everything else is a param, converted to the type in the action's schema from `GET /actions` (int, float, bool, json). The job's log is printed to the console log. If the job isn't finished 15 s after its timeout, it is cancelled and the step fails. An unknown plugin or action fails the step with the list of known ones. Optional inline block: when the next non-blank line is indented deeper than the step, the lines up to a line that is exactly `END` are the block (stripped, blank lines dropped, `#` lines kept, `${VAR}` resolved) and are sent joined with `\n` as the param `script`. The action's schema must list `script`, and `script=` on the step line can't be combined with a block; either fails the step before a job is started. A block without `END`, or an `END` outside a block, fails at that step with its line number. |

Example with an inline block (`esp32` actions: `flash`, `reset`, `factory-reset`, `broadcast-ssid`, `expect` (takes the block), `clear`; start a test with `factory-reset`):

```
PLUGIN: esp32 flash firmware_dir=${FIRMWARE_DIR}
PLUGIN: esp32 factory-reset
PLUGIN: esp32 broadcast-ssid seconds=0
PLUGIN: esp32 expect
  GET /temperature.json -> 200 {"device":"{device}","temperature_c":22.5,"ok":true,"err":0,"ip":"{ip}"}
END
VERIFY: The temperature card shows 22.5 °C
PLUGIN: esp32 clear
```

Pending steps in `pending_tests.json` and steps in result JSON carry the block as `block` (list of lines) and any parse error as `error`; `test.html` shows both under the step.

Platform-only steps: prefix any step with `IF IOS:` or `IF ANDROID:` (e.g. `IF IOS: CLICK: Allow button`, `IF IOS: PLUGIN: …` with its block). It runs only on that platform; on the other one it is recorded as `SKIPPED` with reason `Only runs on iOS`/`Only runs on Android` and does not fail or stop the test. The step keeps the full line as `raw` and carries `platform` (`ios`/`android`). Use it for system prompts only one platform shows (e.g. the iOS Local Network prompt).

Several platform-only steps can share one condition with a block: a line that is exactly `IF IOS:` / `IF ANDROID:` opens it and `END IF` (or `END IF IOS` / `END IF ANDROID`, which must match the opener) closes it. Every step in between gets that platform (indentation is optional; `raw` is the step line without a prefix). PLUGIN blocks with their own `END` work inside it. Blocks don't nest. A missing `END IF`, a stray or mismatched `END IF`, or a step inside the block prefixed for the other platform is a parse error that fails the test on every device.

```
IF IOS:
  SNAPSHOT: local_network_prompt mask=status_bar
  CLICK: Allow button
  WAIT: 2
END IF
```

Setup: `INSTRUCTION.txt` with `MODE: once` or `MODE: everytime` before each test. Optional `CLEAR_DATA: first | everytime` header (default `everytime`): the runner restarts the app before every test and clears its data on Android; with `first`, only the folder's first test starts from cleared data and later tests just relaunch the app, so state an earlier test created (an accepted EULA, a provisioned device) carries over. In a multi-case run, a case after the first that sets neither `CLEAR_DATA` nor `REINSTALL_APP: true` keeps the app data the previous case left (no clearing before any of its tests), on Android as on iOS; the first case and single-case runs keep the `everytime` default. Optional `REINSTALL_APP: true | false` header (default `false`): in a multi-case run each case after the first continues with the app the run already installed; `true` starts that case from a reinstalled app (uninstall + install the run's bundle), the clean-state lever on iOS (no clear-data there) and on Android. Tests run in file-name order (`01_…`, `02_…`). Header keys go at the top of the file in any order. In a multi-case run the case order comes from `SETUP.txt` at the `mt-tests/` root: one case folder name per line (`#` comments and blank lines allowed); folders it doesn't name run after the listed ones in name order, and without the file the folders run in name order.

Variables: a bare `KEY=VALUE` step (no space before `=`) stores a value and every later `${KEY}`/`$KEY` in the test — step args and `PLUGIN` block lines — is replaced with it. Values can also come from Settings → Test Variables, persisted in `Run/config.json` as `test_variables` (a `{NAME: value}` JSON map) and seeded into the runner at startup; the repo ships defaults for it in `mt-tests/scripts/config.json` (`HUB_PATH` empty = hub root, and the app tests' hub rules are written `GET ${HUB_PATH}/devices` so they need the key defined somewhere). Settings values override the repo defaults. Settings → Test Variables also has a Project selector: a project's variables are saved under `project_variables[owner/repo]` in `Run/config.json` and apply only to that project's runs (picked by `MT_REPO`), overriding the shared ones. The `MT_TEST_VARS` env (the same map as a JSON string) overrides all of them. Settings values are read when a run starts, so a saved change applies to the next run. A `KEY=value` line inside the test file overrides everything. A `${KEY}` with no value anywhere stays literal and fails where used (e.g. a `PLUGIN` rule path not starting with `/` is rejected by the fake hub).

## Key Files

### Core Services
| File | Purpose |
|------|---------|
| `app.py` | Web viewer (port 8089). Serves HTML/CSS/JS for test results and live stream. |
| `builds.py` | Build & Test: projects (`build_config.json`) and build plugin registrations in the shared `config.json` `plugins` map (migration from `gitea_config.json`/`MT_BUILD_PLUGINS`), branch/case listing and build start through the project's plugin, build records and their state. |
| `build_plugin_client.py` | HTTP client for build plugins (`BUILD_PLUGIN_API.md`): `BuildPluginClient` (health with contract check, config, check_repo, branches, cases, start_build, build, template), `health_entry`, `validate`, `public_config`, `parse_build_plugins`. |
| `ci/mt-submit/submit.py` | CI upload script (served at `GET /ci/submit.py`); sends `MT_CI_TOKEN` as `Authorization: Bearer` when set. (The composite action `ci/mt-submit/action.yml` was removed in #80.) |
| `viewer_auth.py` | Accounts (seed admin/admin, migration, scrypt, sessions, users admin), login throttle, CI token, runner token, `service_tokens.json` (env import once, `apply_to_environ`). |
| `test_viewer_auth.py` | Tests for login, accounts, CSRF, throttle, CI/runner/service tokens. |
| `fake_build_plugin.py` | Test helper: an in-process fake build plugin answering the whole contract (used by `test_builds.py`, `test_build_plugin_client.py`). |
| `build_fixture.py` | Test helper: `add_build()` records a build so tests can POST to `/api/run-test`. |
| `security_utils.py` | Shared hardening helpers: `safe_join`/`UnsafePath`, allowlist validators (device id, run id, bundle id, branch, test name), `safe_filename`, `check_http_url`, body limits (`body_length`, `max_upload_bytes`, `MAX_JSON_BODY_BYTES`), `write_private_json`, and the `harden_handler` class decorator. Tested by `test_security_hardening.py` (in the CI list). |
| `SECURITY.md` | Security notes; "Code hardening" lists the path, limit, header, subprocess and secret-file rules. |
| `BUILD_PLUGIN_API.md` | The build plugin contract. |

### Test Runner Scripts
| File | Purpose |
|------|---------|
| `mt-tests/scripts/run_on_device.py` | CLI: run tests on devices the Mobile Controller serves. Thin shim over `test_runner_core.py`. |
| `mt-tests/scripts/test_runner_core.py` | Shared test engine: reads `.txt` test files, executes steps, runs vision model, writes results. |
| `mt-tests/scripts/controller_client.py` | HTTP client for the Mobile Controller: click, input, swipe, screenshot, recording, install. |
| `mt-tests/scripts/plugin_client.py` | HTTP client for device plugins (`PLUGIN_API.md`): `PluginClient` (health, devices, actions, jobs), `load_plugins_from_env()`, `plugins_status()`, and the MonkeyTester-Plugin-AI config calls (`ai_config`, `ai_save_config`, `ai_copilot_device_start/poll`, `ai_copilot_remove`, reduced to public shapes). |
| `mt-tests/scripts/apk_manifest.py` | Reads package and version from an APK's binary manifest (no aapt needed). |
| `mt-tests/scripts/ios_app_archive.py` | Reads the bundle ID from an `.ipa` or `.zip` without unpacking it. |
| `mt-tests/scripts/device_lock.py` | Per-device run queue (`flock`). |
| `mt-tests/scripts/vision_analyzer.py` | Vision model API calls (verify, find, check_input, describe), always to the configured endpoint — normally the MonkeyTester-Plugin-AI plugin, which picks local or Copilot itself. |
| `mt-tests/scripts/screenshot_diff.py` | Compares two screenshots. |
| `mt-tests/scripts/snapshots.py` | SNAPSHOT step: stdlib PNG decode/encode, 1x box downscale, screen class, masks, compare. |
| `mt-tests/scripts/layout.py` | ASSERT_LAYOUT step: parses and checks element relations in pt/dp. |
| `mt-tests/scripts/generate_placeholders.py` | Placeholder generation. |

### HTML Pages
| File | Purpose |
|------|---------|
| `index.html` | Run list viewer + dashboard charts. Each run row shows only its status badge (Pass / Fail / Error / Running) — no per-run `3/6 passed` text and no green/red ratio bar; passed/failed counts stay available through `/api/stats/<run>` and in the dashboard chart. Run rows are labeled `#<n>` (the raw `run_<n>` dir name stays in links, selection and API payloads) |
| `test.html` | Individual test case viewer (steps, screenshots, video); live state is pushed over `/api/run-events/<run>` (2 s poll of `/api/pending` as fallback) |
| `test-b.html` | Device-aware run viewer with summary cards and filtered detail panels |
| `logs.html` | Raw console log viewer |
| `player.html` | Video player for recordings |
| `stream.html` | Live device viewer (MJPEG on Android, polled frames on iOS); device list follows live device changes; with no device source configured it shows a setup guide (Mobile Controller + CONTROLLER_URL, or Settings → Device Plugins) instead of a bare error |
| `build.html` | Build & Test: project/branch/platform/case picker and the recent-builds list (polls `/api/builds`); each build links its CI run as "`<forge_label>` run" (`ci_url`), and a branch without the workflow shows the plugin's `workflow_path` with a link to `/api/config/build/template?plugin=` |
| `config.html` | Configuration: **Account** (signed-in name and role, current + new password → `/api/account/password`, Log Out), for admins **CI token** (state `…last4`, Generate new CI token shows it once in a read-only field, Remove) and **Service tokens** (one write-only row per service: Mobile-Controller and every registered plugin, Save/Remove, state `…last4`); the Build & Test section — a **Build plugins** box (one row per plugin: name, address + pairing-token last4, live state with ✓ forge label · configured user · forge URL or ✗ error; Configure / Edit / Remove; an add/edit form with name, address and optional write-only pairing token) with a **Configure** panel proxied to the plugin's `/config` (forge URL, write-only forge token, Save greyed until something changes, Remove token), and a Projects table (Repo, Plugin select, Branch picker read through the plugin (`Default branch (<name>)` or a branch), ✓/✗ status with the template link when the workflow is missing, Remove; an Add repo input + plugin select below; **Save Projects**) — the Test Variables section — and the **Appearance** section (the Light Mode checkbox toggle; the standalone Appearance page is retired, `/appearance` 302s here). Each section's Save button is greyed/disabled while its fields match the saved state; a typed write-only token counts as a change until saved |
| `plugins.html` | Plugins, three sections, each laid out the same: title + "?" intro (`.section-help-btn` shows the hidden generic `.config-info` box, aria-expanded/aria-controls, one delegated click listener; no how-to lists), an **Add row** above everything, the section's `settings-status` line, then a `.plugin-table` (inside an `overflow-x:auto` wrapper) with one `<tr class="plugin-row">` per plugin: **Name | Status | Address | Details | Actions**. Address is an input (Save address greyed until it differs from the saved address); Details holds what the old cards showed (`<br>`-separated); Actions are Save address, Configure (build plugins, plugins with `config`, monkeytester-ai) and Remove. Configure inserts `<tr class="plugin-configure-row"><td colspan=5>` under the row holding the panel (one per section; click again closes). The 15 s refresh updates rows in place keyed by plugin name (`syncPluginTable`, per-cell, no table innerHTML), so the configure row, an open panel and typed-but-unsaved row addresses survive it. **Test Plugins**: Add row = name, address, optional pairing token — monkeytester-ai is added/removed here too (POST /api/config/plugins routes an address whose /health names monkeytester-ai to config.json `visual_plugin_url`/`visual_plugin`, token → `visual_api_key`; `{"remove": "monkeytester-ai"}` clears it); its row shows only once configured. The monkeytester-ai Configure panel stages every edit and applies it with the single Save changes button (Plugin section on top — pairing-token select and status only, no address field or remove select — Configuration below, Save last at its bottom). In managed mode (`managed: true`) the AI row's address is read-only text with "Managed by the MonkeyTester app" in Details and no Save address/Remove; Configure stays. **Build Plugin**: Add row = name, address, optional pairing token; Configure opens the code-host panel. **Controller Plugin**: the Add row (address only) shows only while no controller is set (POST /api/config/controller); then a one-row table (Status from the controller's /health, Details incl. device count; Save address, Configure, Remove). Configure opens the **Device names** panel inline under the row (`#controllerConfigure`, nothing below the table): the controller's devices with a Friendly Name input each (`/api/config/devices`); "Save All Changes" is greyed while every shown name matches the saved config (Enter saves only when something differs). The 15 s refresh re-reads `/devices` for the row's device count, redraws the list only while the panel is open, and keeps typed-but-unsaved names; removing the controller closes the panel; the standalone Device Config page is retired and its old URLs 302 to /plugins |
| `models.html` | Models: visual model endpoint, model name, API key, Test/Clear actions and the config-changed warning banner |
| `run-status.html` | Run Status: active/queued runs and tests (polls `/api/run-status` every 5 s) |
| `login.html` | Sign-in form (public; `{{CSS}}` only, no shared JS); theme toggle via `/api/theme`; redirects on to `?next=` |
| `users.html` | Settings → Users (admins): account table with role select / Reset password / Delete, Create user form |

## Configuration

### Environment Variables
| Variable | Default | Purpose |
|----------|---------|---------|
| `CONTROLLER_URL` | (empty, required) | Mobile Controller base URL, e.g. `http://192.168.125.5:8090`. Imported once at viewer start into `config.json` `controller_url` when that key is absent; afterwards Settings → Plugins → Controller Plugin (POST /api/config/controller) owns the address and this variable is ignored. The runner always receives the RESOLVED value (`app.py` re-exports `CONTROLLER_URL` into the run environment) |
| `CONTROLLER_TOKEN` / `IOS_CONTROLLER_TOKEN` | (empty) | Legacy: imported once into Settings → Service tokens, then ignored (the viewer overwrites `CONTROLLER_TOKEN` in its own environment from `service_tokens.json`, and runners read that) |
| `MT_CI_TOKEN` | (empty) | Viewer: legacy, imported once as the CI token, then ignored. `submit.py`: the CI token it sends |
| `MT_RUNNER_TOKEN` | (set by the viewer) | Per-start secret the runner sends on press events; never set it yourself |
| `DEVICE_ID` | (set by the runner) | The device a run step targets |
| `CAPTURE_OUTPUT_DIR` | `outputs/` | Where downloaded recordings save |
| `VISION_MODEL_API_URL` | `http://127.0.0.1:8081/v1/chat/completions` | Vision model endpoint. An endpoint saved in Settings overrides it |
| `VISUAL_MODEL_API_KEY` | none | Bearer token for the vision model |
| `VISION_TIMEOUT_SECS` | `310` | Seconds to wait for one vision reply |
| `MT_VIEWER_BIND` | `0.0.0.0` | Address the viewer listens on (generic option; login is required on any bind) |
| `MT_MAX_UPLOAD_MB` | `600` | Cap in MiB on one `/api/run-test` multipart body (read into memory); over it → 413. Invalid or ≤0 = default. Real APK/IPA zips are ~35 MB |
| `VISION_MODEL_NAME` | `qwen2.5-vl` | Model name for API |
| `DEBUG` | `false` | Enable debug output |
| `MT_DISABLE_WEB_RUNS` | (empty) | Set to `1` to hide "Build & Test" from the nav. UI only: the pages still open by URL and their APIs (and CI) keep working. Empty, `0`, `false` or `off` = normal behaviour |
| `MT_DEVICE_WAIT_SECS` | `3600` | How long a run queues for a busy device before failing |
| `MT_BUILD_PLUGINS` | (empty) | Build plugins `name=url[,name=url]`, e.g. `build-local=http://127.0.0.1:8094`. Imported once into the shared `config.json` `plugins` map, when `build_config.json` doesn't exist yet; afterwards Settings → Plugins owns the list and the variable is ignored |
| `MT_PLUGINS` | (empty) | Device plugins tests may call: `name=url[,name=url]`, e.g. `esp32=http://127.0.0.1:8091`. Not for monkeytester-ai: its address is set in Settings → Device Plugins (`visual_plugin_url`); an entry here is imported once at viewer start, then ignored |
| `MT_MANAGED_AI_PLUGIN_URL` | (empty) | With `MT_MANAGED_AI_PLUGIN_TOKEN`: the monkeytester-ai plugin's address, supplied by a launcher app on every launch ("managed mode"; legacy, inert unless both variables are set). Wins over config.json `visual_plugin_url` in the viewer and the runner; never written to config.json; the `MT_PLUGINS` import is skipped; the Configure panel shows the address read-only and address/token/removal edits get 409. Either variable missing = off |
| `MT_MANAGED_AI_PLUGIN_TOKEN` | (empty) | With `MT_MANAGED_AI_PLUGIN_URL`: the pairing token for every monkeytester-ai call (viewer proxy and the runner's `/v1/chat/completions` key), winning over config.json `visual_api_key` |
| `PLUGIN_TOKEN` | (empty) | Legacy: imported once (for every plugin registered at the first start) into `service_tokens.json`, then dropped from the viewer's environment |
| `MT_PLUGIN_TOKEN_<NAME>` | (empty) | Legacy: imported once for that plugin; afterwards the viewer sets it itself from `service_tokens.json` so runners send the Settings value |
| `MT_TEST_VARS` | (empty) | Test variables as a JSON object (`{"HUB_PATH": "/api"}`); overrides the `test_variables` map saved in Settings → Test Variables. Seeded at runner start, so a change applies to the next run |
| `MT_REPO` | (set by the viewer) | The run's project (`owner/repo`), passed to each runner it starts; the runner records it in `run_metadata.json` and uses that project's Test Variables. The viewer drops any value it inherited, so set it per run, not on the viewer |

### Port Layout
| Port | Service |
|------|---------|
| 8089 | `app.py` — Web viewer + stream proxy (ThreadingHTTPServer) |
| 8090 | Mobile Controller, on the Mac (separate repo) |
| 8091 | MonkeyTester-Plugin-Esp32 (`esp32-sensor`) on the Linux host; MonkeyTester-Plugin-AI on the Mac (separate repos) |
| 8092 | Fake server of MonkeyTester-Plugin-Server (separate repo) |
| 8093 | MonkeyTester-Plugin-Server (`esp32-server`) device plugin (separate repo) |
| 8094 | MonkeyTester-Plugin-Build-Local (`build-local`) build plugin (separate repo) |

### Settings files
| File | Holds |
|------|-------|
| `Run/config.json` | Viewer config: video size, theme, Test Variables, device plugins (`plugins`), the AI plugin address |
| `Conf/device_names.json` | Device id → friendly name |
| `build_config.json` | Build & Test: build plugins and projects (0600, git-ignored; see API — Build & Test) |
| `gitea_config.json` | Old built-in Gitea client settings: read once to migrate, never written; the viewer hands its url+token to the build-local plugin once, then renames it `gitea_config.json.migrated` without the token |

## Result Storage

```
~/Workspace/MonkeyTester/run_<YYYYMMDD_HHMMSS>/
  outputs/        # Screenshots, videos
  results/        # <test_name>_result.json
  logs/           # Model and backend logs
  run_metadata.json
  summary.txt
  console.log
  pending_tests.json   # During active runs
  tests/               # The .txt files the run used: [<case>/]<test>.txt + INSTRUCTION.txt
```

Multi-device runs keep aggregate `run_metadata.json` at the run root and
isolate each device's artifacts and progress under `devices/device_N/` using
the same layout. This lets `test-b.html` switch detail panels without mixing
results from different devices.

## Constraints & Conventions
- Device ids are whatever the Mobile Controller reports: adb serials or iPhone UDIDs. The runner asks it which platform each id is. iOS simulators and Android emulators are not supported.
- The controller's API version is checked at the start of a run (`EXPECTED_API_VERSION` in `controller_client.py`); a mismatch fails the run.
- MonkeyTester tests require a vision model API on port 8081 or 8080.
- `Run/.uploads/` is transient staging for uploaded APKs — auto-deleted when the subprocess exits with status 0; kept on failure (non-zero exit) for inspection.

## Running the System

1. Start MonkeyTester-Plugin-Mobile-Controller on the Mac (see its README).
2. Start the web viewer: `CONTROLLER_URL=http://192.168.125.5:8090 python3 app.py 8089` (with Nix: `CONTROLLER_URL=… nix run . -- 8089`)
3. Run tests: `cd mt-tests/scripts && CONTROLLER_URL=http://192.168.125.5:8090 python3 run_on_device.py --device 192.168.1.100:5555 tests/my_test.txt`
4. View results: `http://127.0.0.1:8089/`
5. Run from web UI: open Build & Test at `http://127.0.0.1:8089/build.html`
6. Unit tests: `nix run .#tests`, or without Nix the `python3 -m unittest …` line from `.github/workflows/test.yml` (keep `flake.nix`'s copy of that list in step).

## Hosting and review workflow
This repo isn't tied to one code host: it can live on GitHub, Gitea/Forgejo, GitLab, Bitbucket or a
mirror. Find the host from the remote you were asked to work against (`git remote -v`, usually
`origin`) and use that host's tools; the docs never assume one.

| Remote host | Review is called | CLI |
|---|---|---|
| github.com or GitHub Enterprise | pull request | `gh pr create` |
| Gitea / Forgejo | pull request | `tea pr create` |
| GitLab | merge request | `glab mr create` |
| Bitbucket | pull request | web UI or REST API |

- `master` changes only through a reviewed PR/MR that a maintainer merges. Never push to `master`
  and never merge your own PR/MR.
- Work on a feature branch in its own git worktree, based on the remote's `master`.
- CI is in `.github/workflows/`, which both GitHub Actions and Gitea Actions run. Its test job runs the same command as `nix run .#tests`; on a host without Actions, run that locally before opening the PR/MR. `ci/docs-drift.sh` fails a PR that changes source files without updating this file.
- In docs, name sibling repos (e.g. MonkeyTester-Plugin-AI) instead of linking a host URL, link
  files in this repo with relative paths, and stick to CommonMark plus GFM tables and code
  fences (no host-only syntax such as `> [!NOTE]` callouts or `#123` autolinks).
