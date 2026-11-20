# MonkeyTester

AI-driven visual testing: a vision model drives a device through a test
suite, with a live viewer that shows what the test is doing — including the
exact screen position where it is pressing.

## Architecture

MonkeyTester is the web view and the orchestration between the vision model and the
device. Devices are owned by a separate service,
[MonkeyTester-Mobile-Controller](http://192.168.125.70:3000/yurivibes/MonkeyTester-Mobile-Controller),
which runs on a Mac. The two talk only over HTTP.

```
        Linux host (192.168.125.70)                 Mac (192.168.125.5)
┌─────────────────────────────────────────┐   ┌──────────────────────────────────┐
│  viewer (app.py) :8089                  │   │  MonkeyTester-Mobile-Controller  │
│   ├── serves the run pages              │   │  :8090                           │
│   ├── /stream/<id>, /api/frame/<id> ────┼──►│   ├── Appium, devicectl          │
│   └── press overlay (tap/swipe markers) │   │   └── adb, ffmpeg                │
│                                         │   └──────────────────────────────────┘
│  test runner (run_on_device.py)         │              ▲            │
│   └── controller_client.py ─────────────┼──────────────┘            ▼
└─────────────────────────────────────────┘                   iPhone, Android phone
```

Coordinate space is **device pixels** everywhere (vision model, input, screenshot
dimensions), so press markers land exactly where the test pressed.

Build & Test adds a third service: a **build plugin** (`build-local`, port 8094) on the host that
builds the app — it fetches the branch from Gitea or GitHub, runs the app repo's build scripts
there and uploads the apps back to the viewer (see [Build & Test](#build--test)). The viewer
never talks to a code host and never holds its token.

## Install

The viewer (`app.py`) and the test runner (`visual-tests/scripts/`) use only the Python
standard library, so there is no `pip install` step. They need:

| Dependency | Why | Required |
|---|---|---|
| Python 3.10 or newer | viewer, runner, tests | yes |
| `ffmpeg` with libvpx (VP9) and libopus | converts recordings to WebM for the run page | for videos |
| `git` | clone the repo | yes |
| Pillow | only `visual-tests/scripts/generate_placeholders.py` | no |

Phones are driven by [MonkeyTester-Mobile-Controller](http://192.168.125.70:3000/yurivibes/MonkeyTester-Mobile-Controller)
on a Mac and the vision model by [MonkeyTester-AI](http://192.168.125.70:3000/yurivibes/MonkeyTester-AI);
ESP32 boards by [MonkeyTester-Esp32-Sensor](http://192.168.125.70:3000/yurivibes/MonkeyTester-Esp32-Sensor)
and [MonkeyTester-Esp32-Server](http://192.168.125.70:3000/yurivibes/MonkeyTester-Esp32-Server).
Build & Test builds your app through a build plugin (see [Build & Test](#build--test)).
Each is a separate repo with its own install steps, and none of them is needed to install
the viewer or run its tests.

Clone this repository from wherever you got it (its home is
[yurivibes/MonkeyTester](http://192.168.125.70:3000/yurivibes/MonkeyTester)) and
work from the checkout:

```bash
git clone <MonkeyTester repository URL> MonkeyTester
cd MonkeyTester
```

### With Nix (Linux or macOS)

Needs Nix with flakes enabled (`experimental-features = nix-command flakes` in
`~/.config/nix/nix.conf`). Nothing else to install; run these from the repo root,
because the viewer keeps its state (`Run/`, `Conf/`, `build_config.json`) next to `app.py`:

```bash
nix run .#tests            # the CI unit tests
nix run . -- 8089          # the viewer on 0.0.0.0:8089
nix develop                # a shell with python3 + ffmpeg (+ Pillow), for python3 app.py etc.
```

### Without Nix

**Debian / Ubuntu:**

```bash
sudo apt-get update
sudo apt-get install -y python3 ffmpeg git
```

**Fedora:** `sudo dnf install -y python3 ffmpeg git` (ffmpeg comes from RPM Fusion).

**macOS (Homebrew):**

```bash
brew install python ffmpeg git
```

Optional, only for `generate_placeholders.py`: `python3 -m pip install --user pillow`
(or the OS package, e.g. `python3-pil`).

### Verify

```bash
python3 --version          # 3.10 or newer
ffmpeg -hide_banner -encoders | grep -E 'libvpx-vp9|libopus'   # both listed
python3 -m unittest test_ci_delivery test_controller_client test_platform_targeting \
  test_vision_analyzer test_app_api test_plugin_client test_snapshots test_builds \
  test_build_plugin_client test_platform_steps test_vt_file_payload test_vt_payload_upload test_test_variables \
  test_stale_runs test_projects test_security_hardening   # or: nix run .#tests
```

The test list is the one `.gitea/workflows/test.yml` runs in CI; the tests need no
devices, controller or vision model. Then start the viewer (next section) and check
`curl -s localhost:8089/health` answers `{"ok": true, ...}`.

## Running it

Set up the Mobile Controller on the Mac first (see its README), then:

```bash
CONTROLLER_URL=http://192.168.125.5:8090 python3 app.py 8089
# or, with Nix: CONTROLLER_URL=http://192.168.125.5:8090 nix run . -- 8089
# optional, only if the controller is started with CONTROLLER_TOKEN:
# CONTROLLER_TOKEN=<token>
# optional: cap on one /api/run-test upload in MiB (default 600, read into memory):
# VT_MAX_UPLOAD_MB=600
```

### Signing in

The viewer always requires a login. On the first start (no users in `auth_config.json`) it
creates ONE account, **username `admin`, password `admin`**, and logs a warning. **Sign in and
change that password immediately** (Settings → Configuration → Account): until you do, anyone
on the network can sign in, and a warning banner stays on every page. There is no sign-up:
an admin creates every other account on **Settings → Users** (create, reset password, change
role, delete; you can't delete yourself or the last admin). Admins also manage the CI token
and the service tokens; users can do everything else. Lost the only admin password? Stop the
viewer, delete `auth_config.json` and start it again (admin/admin is seeded again).

Tokens the viewer sends to other services are set in **Settings → Configuration → Service
tokens** (admins): the Mobile-Controller token (`X-Controller-Token`) and each plugin's token
(`X-Plugin-Token`). Each is checked against the service before it is saved, kept in
`service_tokens.json` (mode 0600, plaintext at rest) and never shown again. The old
environment variables `CONTROLLER_TOKEN` / `IOS_CONTROLLER_TOKEN`, `PLUGIN_TOKEN` /
`VT_PLUGIN_TOKEN_<NAME>` and `VT_CI_TOKEN` are imported once on the first start, then ignored.
See `SECURITY.md` for the threat model and what is not covered (no TLS: use a VPN).

Request limits and hardening (path checks, 1 MiB JSON cap, upload cap → 413, security headers)
are listed in `SECURITY.md` under "Code hardening".

This needs a Mobile Controller and, if you use it, a MonkeyTester-AI on API version 2.
Open `/api/status` to check that the viewer can reach both. For the vision model,
add the MonkeyTester-AI service in **Settings → Device Plugins**, in the monkeytester-ai
row's **Configure** panel (the **Plugin address** field and **Pairing token** block at its top):
its address (e.g. `http://192.168.125.71:8091`) and pairing token. The viewer checks both
first (`/health` must be the monkeytester-ai plugin and `/config` must accept the token) and
saves nothing if that fails; the address goes into `Run/config.json` as `visual_plugin_url`
and can be changed later in the same panel. The plugin's row stays read-only (it shows the
current address, or "not configured"). A
`monkeytester-ai=…` entry in `VT_PLUGINS` (the old way) is imported into config.json once
when the viewer starts and ignored after that, so remove it from `VT_PLUGINS`; the other
device plugins now live in Settings too — register them in the **Manage device plugins**
box (saved as the `plugins` map in `Run/config.json`, the variable imported into it once).

**App-managed plugin (legacy, archived MonkeyTester Mac app):** when both `VT_MANAGED_AI_PLUGIN_URL` (e.g.
`http://127.0.0.1:8091`) and `VT_MANAGED_AI_PLUGIN_TOKEN` (the pairing token) are set, the
viewer and the runner use them for the monkeytester-ai plugin, winning over config.json's
`visual_plugin_url`/`visual_api_key`, and monkeytester-ai is the vision plugin even without
`visual_plugin` in config.json. Neither value is ever written to config.json, and the
`VT_PLUGINS` import is skipped. The plugin's row and its Configure panel show the address
read-only with "Managed by the MonkeyTester app" (no address field, Plugin or Token select);
the Configuration section and Save work as usual, and the API refuses address, token and
removal changes with 409 `{"error": "managed by the MonkeyTester app"}`. With only one of the two
set, the feature is off. The Mac launcher sets both on every launch, because it picks the AI
port and a fresh token each time.

Then click **Configure** on that plugin's row. The viewer always sends
vision calls to the plugin's `/v1/chat/completions` with the pairing token; the plugin
decides who answers. The panel has a provider choice:

- **Local model** — one of the plugin's installed (Ollama) models.
- **Remote server** — an OpenAI-compatible server at an address you enter (e.g. the Linux
  llama-server `http://192.168.125.70:8080`): *Load models* saves the address on the plugin
  (which checks `/v1/models` first) and lists the server's models. *Resize screenshots for
  Qwen2.5-VL* (on by default) keeps the plugin's resize + coordinate hint + rescale; off
  forwards requests unchanged except the model. An optional API key is stored by the
  **plugin** and only ever shown as `configured · …last4`.
- **GitHub Copilot** — *Sign in with GitHub* shows a device code to enter on github.com;
  the panel polls until the plugin reports the sign-in, then lists your plan's vision
  models. The **plugin** stores the GitHub sign-in and forwards screenshots to GitHub's
  Copilot API; the viewer stores no GitHub token. The plugin uses the OAuth client of
  GitHub's editor plugins, which is not an official API.

The panel also holds the pairing token (the service owns and generates it; the viewer keeps
a copy so it can call the service over the LAN and only ever reads it back as
`{configured, last4}`). There is no endpoint or model text field — the endpoint is the
plugin's address from Settings. Old configs that saved `visual_endpoint`/`visual_model` still work
and log a deprecation. A `Run/config.json` from before Copilot moved into the plugin
(`visual_provider: "copilot"`) makes the runner stop at start with a pointer to the panel;
saving there replaces it. An old `copilot_config.json` in the repo root is no longer read —
delete it (it holds a GitHub token).
Without saved settings the runner uses `VISION_MODEL_API_URL` and `VISUAL_MODEL_API_KEY`.

To run the tests, see [Install → Verify](#verify).

- **New test:** start it from Build & Test (`/build.html`): choose the platforms and cases, and the
  build's runs attach to it (setup: [Build & Test](#build--test)). The viewer lists devices from the controller, which installs the built app.
- **Live view:** Android runs show a live MJPEG stream. iOS has no stream, so the
  run page polls a fresh screenshot.
- **Press overlay:** every tap and swipe is POSTed to `/api/press-event`
  (fire-and-forget: a dead viewer never breaks a run). The run page draws a fading
  marker at the exact device-pixel position.
- **Runner:** `run_on_device.py --device <id> ...`, where `<id>` is an adb serial or
  an iPhone UDID. `--list-devices` shows what the controller serves.

If the controller is unreachable or a device isn't connected, the run fails and says
so. `SCRIPT` test steps run on the Linux host, so a script that calls `adb` fails.

Other hardware (ESP32 boards, …) is driven through **device plugins**, separate HTTP
services that follow [PLUGIN_API.md](PLUGIN_API.md). Register them in the **Manage device
plugins** box on the Settings → Device Plugins page — a name and an address (e.g.
`esp32-sensor=http://127.0.0.1:8091` and `esp32-server=http://127.0.0.1:8093`
for [MonkeyTester-Esp32-Sensor](http://192.168.125.70:3000/yurivibes/MonkeyTester-Esp32-Sensor) and
[MonkeyTester-Esp32-Server](http://192.168.125.70:3000/yurivibes/MonkeyTester-Esp32-Server));
an address is saved (in the `plugins` map of `Run/config.json`) only once its `GET /health`
answers with the plugin contract, and a `VT_PLUGINS=name=url[,name=url]` environment
variable is imported into that map once at viewer start and ignored after that. Call
them from a test with `PLUGIN: <plugin> <action> [key=value …] [device=<id>] [timeout=<s>]`.
The Settings page lists the active plugins with their live state, capabilities and
connected devices, and lets you add, edit or remove them; the `plugins` map decides which
device plugins are enabled. The monkeytester-ai plugin is the exception: its address is set
only in that row's **Configure** panel (see above), not in the manage box.
A `PLUGIN` step may carry an inline block, indented lines up to a line that is exactly
`END`, which is sent to the action as its `script` param:

```
PLUGIN: esp32 flash firmware_dir=${FIRMWARE_DIR}
PLUGIN: esp32 factory-reset
PLUGIN: esp32 broadcast-ssid seconds=0
PLUGIN: esp32 expect
  GET /temperature.json -> 500
END
VERIFY: The temperature card shows an error
PLUGIN: esp32 expect
  GET /temperature.json -> normal
END
VERIFY: The temperature card shows a live reading
PLUGIN: esp32 clear
```

Fonts, styles and asset placement are checked with `SNAPSHOT` (compare against a baseline kept
in the test case, per screen class) and `ASSERT_LAYOUT` (positions of tagged elements in pt/dp).
See [SNAPSHOTS.md](SNAPSHOTS.md).

## Build & Test

Build & Test (`/build.html`) builds a branch of your app and tests the result on
the devices. The build runs on the **build plugin's** own host — a Mac builds
Android and iOS, a Linux host Android only — never on the viewer. MonkeyTester
never talks to a code host itself: a **build plugin**, a separate service that
implements [BUILD_PLUGIN_API.md](BUILD_PLUGIN_API.md), fetches the branch from
Gitea or GitHub, runs the app repo's build scripts on its own host and uploads
the apps back to the viewer. The plugin holds the code host's address and a
read-only access token; the viewer never sees that token.

| Plugin | Repo | Default port | What it does |
|---|---|---|---|
| `build-local` | [MonkeyTester-Build-Local](http://192.168.125.70:3000/yurivibes/MonkeyTester-Build-Local) | 8094 | Fetches the code from Gitea or GitHub and builds it on its own host: Android and iOS on a Mac, Android only on Linux |

Any other build system can be connected by a plugin that implements the same contract.

Setup:

1. Install and start the plugin where your app gets built (its README; it runs on
   Linux or macOS, with or without Nix). On a Mac it needs Xcode (iOS), the Android
   SDK and a JDK; a Linux host offers Android only (Android SDK + JDK).
2. In **Settings → Configuration → Build & Test**, add it in the **Build plugins** box: a
   name (e.g. `build-local`), its address (e.g. `http://127.0.0.1:8094`) and, only if the
   plugin was started with `PLUGIN_TOKEN`, that pairing token. The address is saved only when
   the plugin's `/health` answers the build plugin contract. The row then shows the plugin's
   live state, its code host, the user that token belongs to and the platforms its host can
   build — Build & Test greys out a platform the plugin can't build.
3. Click **Configure** on the row and fill the panel: the **code host** (Gitea or GitHub), its
   **URL**, a **read-only token** and the **viewer URL** (prefilled with the address you are
   using). The plugin checks the token with the code host before it stores it. Read-only is
   enough, since the plugin only reads the repo and fetches it with git:
   - **Gitea:** an access token with `read:repository` (plus `read:user`), code host URL the
     site's own address (e.g. `http://192.168.125.70:3000`).
   - **GitHub:** a fine-grained token with `Contents: read` + `Metadata: read` (or a classic
     token with `repo` scope for private repos), code host URL `https://github.com`
     (`https://<host>` for GitHub Enterprise Server).
4. Add your repos under **Projects**: `owner/repo`, the plugin that builds it and the
   project's branch (the default branch unless you pick another). A repo belongs to one
   project, on one build plugin. **Save Projects** asks the plugin whether its token can read
   the repo and whether the branch exists.
5. Commit one build script per platform to the app repo at the paths the plugin names
   (`.monkeytester/build-android.sh`, `.monkeytester/build-ios.sh`). The Status column names
   a missing one and links the plugin's **example script**
   (`/api/config/build/template?plugin=<name>&platform=<platform>`, the plugin's `GET /template`);
   adjust the example for your app. The script runs with
   `VT_PLATFORM`, `VT_OUTPUT` (where it must write the `.apk` or the signed `.ipa`),
   `VT_BRANCH`, `VT_CASES`, `VT_REQUEST_ID`, and exits 0 with a file at `VT_OUTPUT` when the
   build worked. Download the scripts from Settings and commit them to the app repo.
6. Nothing else to set: the plugin uploads the built apps through the viewer's
   `GET /ci/submit.py` with the build's request id, so the run attaches to the build
   (see [Upload](#upload)).

Settings are kept in `build_config.json` next to `app.py` (mode 0600, git-ignored). A viewer
upgraded from the built-in Gitea client moves its `gitea_config.json` once: it becomes the
`build-local` plugin at `VT_BUILD_LOCAL_URL` (default `http://127.0.0.1:8094`) plus one project
per saved repo with its branch pick. The first time that plugin answers `/health` with `forge.configured`
false, the viewer POSTs the saved Gitea URL and token to its `/config` once (with `forge_kind:
gitea` and the viewer URL); on success `gitea_config.json` is rewritten without the token and
renamed `gitea_config.json.migrated`, so the viewer no longer holds a code host token. A
`VT_BUILD_PLUGINS=name=url[,name=url]` environment variable is imported the same way, once.

## Upload

Every run starts from Build & Test: the viewer accepts an upload only when its
`build_request` names a build it started (anything else is refused with "Runs start from
Build & Test"). Build & Test asks the plugin to build with three inputs — `platforms`, `cases`
and `request_id` — and the plugin sends the built apps back with `build_request` = `request_id`,
so the run attaches to the build.

The upload is `ci/visual-test/submit.py`, which uses only the Python standard library. The
viewer serves it at `GET /ci/submit.py`, so a build plugin can fetch it without a checkout of
this repo and run it with `python3` from the checkout's root:

```bash
curl -fsS "$VT_URL/ci/submit.py" -o vt_submit.py &&
  python3 vt_submit.py --url "$VT_URL" \
    --android-app app/build/outputs/apk/debug/app-debug.apk \
    --ios-app build/visual-test/app.ipa \
    --tests-dir visual-tests --branch "$BRANCH" --repo "$REPO" \
    --cases "$CASES" --build-request "$REQUEST_ID" --no-wait
```

`--no-wait` is what a build plugin uses: the script uploads, prints `--- <run_id> started` and
exits 0 once the viewer accepted the upload (non-zero when it refused it); the viewer works out
the test results itself. Without the flag the script waits for the verdict and exits 0 only if
at least one test ran and every test passed — rejected uploads, install failures, crashes,
timeouts and empty runs all fail. `--repo` defaults to `GITHUB_REPOSITORY` and `--branch` to
`GITHUB_REF_NAME` when a CI sets them; a build plugin passes both explicitly.

### Where the code comes from

The plugin reads and fetches the branch from the code host you configured — Gitea or GitHub —
so the app repo needs no workflow file, no secrets and no CI settings. Nothing in the viewer
or its docs assumes which code host hosts your app repo: the kind, URL and read-only token live
in the plugin's own config. For GitHub Enterprise Server the code host URL is the web address
(the plugin derives the API base and the clone URL from it).

### Runs, cases and build errors

The run is always accepted and queued. When its turn comes, the run uses the
Android phones and iPhones connected at that moment; if one platform has none,
it waits up to 5 minutes (`VT_DEVICE_CONNECT_WAIT_SECS` on the viewer) for one
to connect, then fails with "Gave up after 300s waiting for an iPhone to
connect". Build both apps in one build (on the Mac, which has the Android SDK and
Xcode) and pass both: the whole build is ONE run, every case on the Android
phone and the iPhone, with one result page and a device switcher per test. For
a single platform, pass `--app path/to/app.ipa --platform ios` (or an `.apk`
with `--platform android`).

Each `visual-tests/<case>/` folder needs an `INSTRUCTION.txt` and at least one
test `.txt`. All cases go into the one run, in folder-name order, each keeping
its own `INSTRUCTION.txt` (`MODE`, `CLEAR_DATA`) and snapshot baselines. Files a
test references with `@file:<path>` (plugin payloads, see PLUGIN_API.md) are
relative to the case folder, must stay inside it, and are uploaded with the case
automatically. Rows on
the run page read `case / test`. Devices run one after another. When a test
fails on a device, the rest of that device's tests (in every case) are marked
**Cancelled** and not run; the other devices still run everything. Every case
after the first continues with the app the run already installed; add
`REINSTALL_APP: true` to a case's `INSTRUCTION.txt` to start it from a
reinstalled app.

If one platform's app doesn't build (for example an iOS signing error), pass
the error instead of the app (`--ios-build-error <message>` plus
`--ios-build-log <path>`, same for Android). The run still happens on the other
platform. That platform's connected devices appear in it with every test
**Unavailable**, showing the error and a **View logs** button for the build
log, and the run (and the check) fails. With no device of that platform
connected, a stand-in entry ("iPhone (none connected)") shows it. Other setup
failures (device locked, app install failed) also mark that device's tests
Unavailable, with View logs opening the run's console log.

Setup:

- **Viewer:** nothing to set for build plugins: an upload naming a build the viewer started
  (still waiting for its apps) is accepted once without credentials. Any other CI that
  uploads with `submit.py` needs the CI token: Settings → Configuration → **Generate new CI
  token** (shown once), stored as the CI's `VT_CI_TOKEN` secret; `submit.py` sends it as
  `Authorization: Bearer`. It only starts runs and reads their results.
- **Build plugin:** registered and configured in Settings (see [Build & Test](#build--test)),
  including the viewer URL it uploads to.
- **App repo:** the two build scripts committed at the plugin's recipe paths; no secrets
  and no workflow file.
- **IPA signing:** the app's build script signs the IPA, and MonkeyTester only installs it.
  The profile must include the iPhone's UDID. Simulator builds can't install on an iPhone.
- **Mac:** the signing identity must be usable by the build plugin's agent (keychain
  unlocked) and by the Mobile Controller's LaunchAgent (keychain unlocked). The iPhone stays
  connected, trusted, unlocked and awake.
- **Android:** a device connected to the Mac, over USB or wireless debugging paired
  with the Mac.

## Files

| Path | Role |
|------|------|
| `app.py` | viewer + API (press events, device proxies, run launch) |
| `viewer_auth.py` | accounts (admin/admin seed, scrypt, sessions, Users admin), login throttle, CI token, runner token, service tokens |
| `login.html`, `users.html` | sign-in page; Settings → Users (admins) |
| `SECURITY.md` | threat model, what is protected and how, family-wide overview |
| `builds.py` | Build & Test: build plugin settings, projects, build records and their state |
| `build_plugin_client.py` | HTTP client for build plugins (`BUILD_PLUGIN_API.md`) |
| `BUILD_PLUGIN_API.md` | the build plugin contract |
| `.monkeytester/build-android.sh` + `.monkeytester/build-ios.sh` (in the **app repo**) | the per-platform build scripts the plugin runs |
| `ci/visual-test/submit.py` | the upload script build plugins run (served at `GET /ci/submit.py`); `--no-wait` exits once the viewer accepted the upload |
| `test.html` | run detail page: live view + press overlay |
| `visual-tests/scripts/run_on_device.py` | runner CLI |
| `visual-tests/scripts/test_runner_core.py` | runner core |
| `visual-tests/scripts/controller_client.py` | HTTP client for the Mobile Controller |
| `visual-tests/scripts/vision_analyzer.py` | HTTP client for the vision model |
| `visual-tests/scripts/plugin_client.py` | HTTP client for device plugins (`PLUGIN_API.md`) |
| `PLUGIN_API.md` | the device plugin contract |
| `visual-tests/scripts/snapshots.py` | `SNAPSHOT`: screen classes, 1x PNG compare, masks (stdlib only) |
| `visual-tests/scripts/layout.py` | `ASSERT_LAYOUT` checks |
| `SNAPSHOTS.md` | snapshot and layout steps, baselines, record mode |
| `visual-tests/scripts/press_events.py` | fire-and-forget press event emitter |

## Notes

- A locked iPhone black-screens every capture; the runner refuses to start
  and the `unlock` action reports it (passcode/Face ID can't be sent by
  WDA).
- iOS has no `pm clear`: app data survives restarts. Use an UNINSTALL step
  in the test instructions, or the `REINSTALL_APP: true` header of a case
  folder, for a clean state.
- On Android the app's data is cleared before every test. To chain tests in
  one case (test 2 starts where test 1 left the app), add
  `CLEAR_DATA: first` to the case's `INSTRUCTION.txt`: only the first test
  (in file-name order) starts from cleared data, later ones just relaunch.
- In a multi-case run, a case after the first whose `INSTRUCTION.txt` sets
  neither `CLEAR_DATA` nor `REINSTALL_APP: true` keeps the app data the
  previous case left on Android too (no clearing before any of its tests),
  the same as iOS, so e.g. `device-setup` starts on Home after `onboarding`.
  Set `CLEAR_DATA` in that case to clear again.
- A step that only one platform needs (e.g. an iOS-only system prompt)
  takes an `IF IOS:` or `IF ANDROID:` prefix: `IF IOS: CLICK: Allow button`. On the
  other platform it shows as skipped and doesn't fail the test. For several
  steps, wrap them in a block: a line `IF IOS:` (or `IF ANDROID:`), the steps,
  then `END IF` (or `END IF IOS`). Blocks don't nest, and a missing or
  mismatched `END IF` fails the test.
- Screen recordings are made on the controller and downloaded to the run's
  outputs dir when recording stops.
