# MonkeyTester Build Plugin API (contract_version 1)

A **build plugin** is a separate service that turns a branch of an app repo into the
apps Build & Test installs on the test devices. Build & Test asks the plugin to list the
repo's branches and test cases and to start a build; the plugin gets the code, builds the
chosen platforms and sends the apps to the viewer (`POST /api/run-test` with
`build_request` = the build's request id, through the viewer's `GET /ci/submit.py`), so
the run attaches to the build. This document is the whole coupling: a plugin that
implements it works with Build & Test without changes to MonkeyTester.

Implementations:

| Plugin | Repo | Default port | What it does |
|---|---|---|---|
| `build-local` | MonkeyTester-Plugin-Build-Local | 8094 | Fetches the code from Gitea or GitHub and builds it on its own host: Android and iOS on a Mac, Android only on Linux |

## Who owns what

| Viewer | Build plugin |
|---|---|
| The project list (one project = one repo on one build plugin), each project's branch pick, Test Variables and run history | Where the code comes from (code host kind, URL, read-only access token — never sent to the viewer) and the viewer URL it uploads to |
| Build records (`Run/.builds/`), attaching uploaded runs, the overall build state | Fetching the code, running the build, the build log, uploading the apps |
| The Build & Test and Settings pages | Example build scripts (`GET /template`) |

## Conventions

Same as [PLUGIN_API.md](PLUGIN_API.md):

- JSON in and out, UTF-8. Times are seconds since the epoch unless the name says otherwise.
- A plugin started with a pairing token (`PLUGIN_TOKEN`) requires it on every request
  except `GET /health`, as `X-Plugin-Token` (or `Authorization: Bearer`); otherwise 401.
- HTTP errors are non-2xx with `{"error": "<message>", "code": "<code>"}`. Codes:
  `bad_request`, `unauthorized`, `not_configured` (no code host token saved, 409),
  `not_found`, `forge_unreachable` (the code host can't be reached, 502), `forge_refused`
  (the code host answered with an error; `forge_status` carries its HTTP status),
  `internal`.
- A build that fails is not an HTTP error: `GET /builds/<ref>` is `200` with a failed
  conclusion.
- Repos are `owner/repo`. Branch names follow git's ref rules. Platforms are `android`
  and `ios`.
- Runs on Linux and macOS, Python standard library only.

## Endpoints

### `GET /health`

```json
{
  "ok": true,
  "contract": "monkeytester-build-plugin",
  "contract_version": 1,
  "plugin": {"name": "build-local", "version": "0.1.0"},
  "forge": {"kind": "gitea", "label": "Gitea", "url": "https://gitea.example.com",
            "configured": true, "user": "ci-bot"},
  "platforms": ["android", "ios"],
  "recipes": {"android": ".monkeytester/build-android.sh", "ios": ".monkeytester/build-ios.sh"},
  "viewer_url": "http://192.168.1.10:8089",
  "queue": {"running": "br_0123456789abcdef", "waiting": 0},
  "capabilities": ["logs", "cancel"],
  "error": null
}
```

`viewer_url` above (`http://192.168.1.10:8089`) is an example address; the real value is your viewer's own address.

- `forge` is the code host the plugin reads from (`kind`: `gitea` or `github`).
- `platforms`: what this host can build (detected at start: Xcode → `ios`, an Android SDK
  → `android`). Build & Test offers only these.
- `recipes`: the file in the app repo that builds each platform.
- `capabilities`: optional parts of this contract the plugin supports (`logs`, `cancel`).

MonkeyTester refuses a plugin whose `contract` differs or whose `contract_version` it
does not support.

### `GET /config` / `POST /config`

`GET` → `{forge_kind, forge_url, token: {configured, last4, saved_at}, user, viewer_url}`.

`POST {forge_kind?, forge_url?, token?, viewer_url?}` checks the token against the code
host (who it belongs to) before storing anything; a blank or missing `token` keeps the
saved one, `token: null` removes it. Changing `forge_kind` or `forge_url` needs the token
again. `viewer_url` must answer `GET /health` as a MonkeyTester viewer. Reply
`{ok, config}`; a rejected token is `400` with `code: "forge_refused"` and nothing is
stored. The token is plaintext at rest in the plugin's config file (mode 0600) and never
returned. A read-only token is enough (it reads the repo and fetches it with git).

### `GET /repos/<owner>/<repo>?branch=<b>`

The Settings ✓/✗ check for one project.

```json
{"repo": "o/r", "ok": true, "reason": "", "default_branch": "master",
 "branch": "master", "recipes": {"android": true, "ios": false}}
```

`branch` is the requested branch, else the default. `ok` means the token can read the
repo and the branch exists. `recipes` says which platform's build script that branch has
(`null` per platform when it could not be checked); a missing script does not make `ok`
false (the repo is added first, the scripts committed after).

### `GET /repos/<owner>/<repo>/branches`

`{"repo": "o/r", "default": "master", "branches": ["master", "feat/x", …]}` — every
branch, default first.

### `GET /repos/<owner>/<repo>/cases?ref=<branch>`

`{"repo": "o/r", "ref": "feat/x", "cases": ["device-setup", "onboarding"], "recipes": {"android": true, "ios": true}}`
— the `mt-tests/<case>/INSTRUCTION.txt` folders on that branch, sorted, and which
build scripts the branch has. Unknown branch → 404.

### `POST /builds`

```json
{"repo": "o/r", "branch": "feat/x", "platforms": ["android", "ios"],
 "cases": ["onboarding"], "request_id": "build_0123456789ab"}
```

Queues a build of that branch. `201` → `{"build_ref": "<opaque>", "url": ""}`. The viewer
stores `build_ref` and passes it back unchanged. Refused: a platform this host can't build
or the branch has no script for → `400 bad_request`; unknown branch → `404 not_found`;
no `viewer_url` or token configured → `409 not_configured`.

### `GET /builds/<build_ref>`

```json
{
  "state": "in_progress",
  "conclusion": null,
  "url": "",
  "jobs": [
    {"name": "Android", "status": "completed", "conclusion": "success", "url": ""},
    {"name": "iOS", "status": "in_progress", "conclusion": null, "url": ""},
    {"name": "Upload", "status": "queued", "conclusion": null, "url": ""}
  ]
}
```

- `state`: `pending` (accepted, not queued yet), `queued` (waiting for an earlier build),
  `in_progress`, `completed`.
- `conclusion` (only when `completed`): `success` (the apps were uploaded), `failure`
  (nothing could be uploaded: every platform failed to build, the fetch failed, or the
  upload failed), `cancelled`.
- A platform that failed to build while another one built still ends the build with
  `success`: that platform is uploaded as a build error (`--<platform>-build-error` with
  the log), so its devices show every test **Unavailable** with **View logs**.
- `jobs`: one per chosen platform, named `Android` / `iOS`, plus `Upload`. Same values.

### `GET /builds/<build_ref>/log?offset=<bytes>` (capability `logs`)

`text/plain`: the build's log from byte `offset` (default 0) — the fetch, each platform's
script output and the upload. Header `X-Log-Size` is the log's total size and
`X-Log-Complete: 1` is set once the build has finished, so a client can tail it.

### `POST /builds/<build_ref>/cancel` (capability `cancel`)

Stops a queued or running build (the running script's process group is terminated).
`200` → the build's `GET /builds/<ref>` body. A finished build is left as it is.

### `GET /template?platform=<android|ios>`

`text/plain`: an example build script for that platform, the file to commit at
`recipes[platform]`.

## The app repo side

Commit one script per platform at the paths in `recipes`. The plugin runs it with `bash`
from the root of a clean checkout of the branch, with:

| Variable | Value |
|---|---|
| `MT_PLATFORM` | `android` or `ios` |
| `MT_OUTPUT` | where the script must write the app: a `.apk` (Android) or a signed `.ipa` (iOS) |
| `MT_BRANCH` | the branch being built |
| `MT_CASES` | comma-separated case names |
| `MT_REQUEST_ID` | the viewer's build id |

Exit status `0` with a file at `MT_OUTPUT` is a successful build; anything else is a build
error, reported with the script's last `error:` lines and its log.

## Versioning

Change `contract_version` for any change that breaks an existing client or plugin.
Adding optional fields, endpoints or capabilities is not breaking.
