# MonkeyTester Device Plugin API (contract_version 1)

A **device plugin** is a separate service that owns one kind of hardware (ESP32
boards, phones, …) and lets MonkeyTester drive it over HTTP. MonkeyTester never
talks to the hardware itself. This document is the whole coupling: a plugin that
implements it can be used from any test without changes to MonkeyTester.

Implementations:

| Plugin | Repo | Default port |
|---|---|---|
| `esp32` | [MonkeyTester-Plugin-Esp32](https://github.com/yurivibes/MonkeyTester-Plugin-Esp32) | 8091 |
| `monkeytester-ai` | [MonkeyTester-Plugin-AI](https://github.com/yurivibes/MonkeyTester-Plugin-AI) | 8091 (on the Mac, e.g. `http://192.168.125.71:8091`); address set in Settings → Device Plugins, not `VT_PLUGINS` |

The Mobile Controller still uses its own API (`API.md` in that repo); moving it onto
this contract is a later step.

`monkeytester-ai` is a **config-owning plugin**: it does not drive hardware per test
step, it owns the vision provider — its local Ollama model, a remote OpenAI-compatible
server or GitHub Copilot. These
endpoints extend the contract for that purpose (all authenticated with the pairing
token as `Authorization: Bearer` or `X-Plugin-Token`):

- `GET /models` — the active provider's models, OpenAI-list style, plus `provider`,
  `selected` and `installed`.
- `GET /config` — `{model: {selected, installed}, token: {configured, last4},
  provider: "local"|"remote"|"copilot", copilot: {configured, user, last4, saved_at, model,
  models: [{id, name, vendor}], error}, remote: {url, model, models: [{id}], resize,
  key: {configured, last4}, error}, port, ollama_port, max_pixels, api_version: 3,
  service}`.
- `POST /config` — the viewer sends one change per call: `{model}` (local model),
  `{provider}` (`copilot` while signed out, or `remote` without `remote_url` +
  `remote_model` → 400 `invalid_request`), `{copilot_model}`, the remote keys together
  (`{remote_url?, remote_model?, remote_resize?, remote_api_key?}`; the plugin checks
  `remote_url` with `GET {url}/v1/models` before storing anything, `remote_api_key`
  `null`/`""` removes the key, which is never returned), or `{token|generate: true}`
  (rotation returns `new_token` exactly once). Reply `{ok, config[, new_token]}`; errors
  are `{error: {code, message}}`.
- `POST /copilot/device/start` → `{user_code, verification_uri, interval, expires_in}`;
  `POST /copilot/device/poll` → `{state: pending|slow_down|done|expired|denied|error, …}`;
  `POST /copilot/remove` → the public copilot block (falls back to `local`).
- `POST /v1/chat/completions` — routed to the active provider.

Settings → Device Plugins → monkeytester-ai → **Configure** proxies these through
the viewer (`plugin_client.py` is the only caller). The plugin's address is set on the
same page (in the row's **Configure** panel: its **Plugin address** field, checked against
`/health` and an authenticated `/config` before it is saved; the row itself is read-only)
and stored as `visual_plugin_url` in
`Run/config.json`; device plugins are registered in the Settings → Device Plugins
**Manage device plugins** box (saved as the `plugins` map {name: url} in
`Run/config.json`; `VT_PLUGINS` entries are imported into that map once at viewer
start and the variable is ignored after that; a `monkeytester-ai` entry there is
imported once into `visual_plugin_url`, then ignored). The viewer stores only a copy of the
pairing token (`visual_api_key`) so it can call the service over the LAN, never displays
it again, and holds no GitHub token and no remote API key. A `PLUGIN:` step against it is `set-model` only — its
actions are not test-step device drives.

## Model

Everything a test asks of a plugin is an **action** run as a **job**:

1. MonkeyTester starts a job: `POST /jobs {device, action, params, timeout_ms}`.
   The plugin answers at once with the job id.
2. MonkeyTester waits for the response: `GET /jobs/<id>?wait=<seconds>` blocks until
   the job finishes or `wait` runs out, and returns the job either way.
3. The job ends in exactly one terminal state, and the test step passes only on
   `passed`.

A plugin publishes its actions at `GET /actions`, with a parameter schema for each,
so MonkeyTester can list and validate them without knowing anything about the
hardware. The plugin decides what each action does to the device and what counts
as the device "responding correctly".

## Conventions

- JSON in and out, UTF-8. Times are milliseconds unless the name says otherwise.
- If the plugin is started with a token (`PLUGIN_TOKEN`), every request must send it
  as `X-Plugin-Token`; otherwise the plugin answers 401.
- Errors on the HTTP level are non-2xx with `{"error": "<message>", "code": "<code>"}`.
  Codes: `bad_request`, `unauthorized`, `not_found`, `unknown_action`,
  `unknown_device`, `device_busy`, `internal`.
- A job's outcome is never an HTTP error: a job that fails is still `200` with
  `state: "failed"`.
- A plugin must be able to run on Linux and macOS. Hardware paths (serial ports,
  firmware directories) are paths **on the plugin's host**.

## Endpoints

### `GET /health`

Readiness. Never blocks on hardware.

```json
{
  "ok": true,
  "contract": "monkeytester-plugin",
  "contract_version": 1,
  "plugin": {"name": "esp32", "version": "0.1.0"},
  "host": {"os": "linux", "hostname": "server"},
  "devices": 1,
  "capabilities": ["events", "uploads"],
  "error": null
}
```

`capabilities` lists the optional parts of this contract the plugin supports.

MonkeyTester refuses a plugin whose `contract` differs or whose `contract_version`
is not one it supports.

### `GET /devices`

```json
{
  "devices": [
    {
      "id": "7c:df:a1:a4:8f:44",
      "kind": "esp32",
      "model": "ESP32-C3",
      "state": "ready",
      "details": {"port": "/dev/ttyUSB0"}
    }
  ],
  "error": null
}
```

- `id` is stable across reconnects and reboots (for boards: the MAC). Tests and jobs
  use it; a plugin may also accept an alias it documents (for example the port).
- `state`: `ready`, `busy` (a job is running on it), `offline` (known but not
  reachable) or `error` (see `details.error`).
- `details` is plugin-specific and informational only.

### `GET /actions`

```json
{
  "actions": [
    {
      "name": "flash",
      "description": "Write a prebuilt firmware directory to the board.",
      "params": {
        "firmware_dir": {"type": "path", "required": true,
                          "description": "ESP-IDF build dir with flasher_args.json"},
        "erase": {"type": "bool", "default": false}
      },
      "default_timeout_ms": 120000,
      "needs_device": true
    }
  ]
}
```

Parameter `type` is one of `string`, `int`, `float`, `bool`, `enum` (with
`"values": [...]`), `path` (a path on the plugin host, or `upload:<id>`, see
`POST /uploads`), `json` (any JSON value).

`needs_device: false` marks actions that don't target a board (for example a
plugin-wide reset); for those `device` may be omitted.

### `POST /jobs`

Start an action.

```json
{"device": "7c:df:a1:a4:8f:44", "action": "flash",
 "params": {"firmware_dir": "/builds/c3-debug-test/build"},
 "timeout_ms": 120000}
```

- `device` may be omitted when the plugin has exactly one device.
- `timeout_ms` is optional; the action's `default_timeout_ms` applies otherwise.
  The plugin enforces it and ends the job in `timeout` when it runs out.
- `202 {"job": <job>}` on success.
- `400 bad_request` for unknown or invalid params, `404 unknown_action` /
  `unknown_device`, `409 device_busy` when the device already runs a job. A plugin
  runs **one job per device at a time**; MonkeyTester serialises its steps anyway.

### `GET /jobs/<id>?wait=<seconds>`

Return the job. With `wait` (0–60, default 0), block until the job is in a terminal
state or `wait` passes. Callers loop on this until the job is terminal.

```json
{
  "id": "j-5f2c",
  "device": "7c:df:a1:a4:8f:44",
  "action": "expect",
  "params": {"script": "GET /temperature.json -> 500"},
  "state": "passed",
  "started_ms": 1762180000000,
  "finished_ms": 1762180000420,
  "result": {"rules": 1},
  "error": null,
  "log": ["-> @vt p7 clear", "<- @vt p7 ok {\"rules\":0}",
          "-> @vt p8 rule GET /temperature.json -> 500", "<- @vt p8 ok {\"rules\":1}"]
}
```

States:

| State | Terminal | Meaning |
|---|---|---|
| `queued` | no | Accepted, not started |
| `running` | no | In progress |
| `passed` | yes | The action ran and the device responded as expected |
| `failed` | yes | The action ran but the device's response was wrong (a test failure) |
| `timeout` | yes | No response within `timeout_ms` |
| `error` | yes | The plugin couldn't run the action (port gone, bad firmware dir, tool crash) |
| `cancelled` | yes | Cancelled by `POST /jobs/<id>/cancel` |

`error` is `null` unless the state is `failed`, `timeout`, `error` or `cancelled`,
when it is `{"code": "<short-code>", "message": "<human text>"}`. `result` is
action-specific and may be present on any terminal state. `log` is the
human-readable transcript of what the plugin did, for the run's console log; it is
capped by the plugin (keep the last 500 lines).

Finished jobs are kept for at least 10 minutes.

### `POST /jobs/<id>/cancel`

Ask a running job to stop. `200 {"job": <job>}`; the job ends `cancelled` (or in
whatever terminal state it reached first).

### `GET /events?device=<id>&since=<seq>`

Optional (listed in `GET /health` → `capabilities` when supported). Server-sent
events (`text/event-stream`) of what the devices are doing, for live views and run
logs. Each event's `data` is:

```json
{"seq": 42, "ts_ms": 1762180000000, "device": "7c:df:a1:a4:8f:44",
 "type": "serial", "data": "I (1234) temp-sensor: reading 19.00 C"}
```

`type` is `serial`, `state` (device state change), or `job` (a job changed state;
`data` is the job). `since` replays buffered events after that `seq`.

### `POST /uploads`

Optional (capability `uploads`). For when MonkeyTester and the plugin run on
different hosts. The body is a zip archive (`Content-Type: application/zip`); the
plugin unpacks it and returns `201 {"upload": "upload:<id>", "path": "<host path>"}`.
`upload:<id>` can then be passed to any `path` parameter. Uploads are deleted after
an hour.

## Using plugins from tests

MonkeyTester reads the plugins it may call from the `plugins` map {name: url} in
`Run/config.json` — registered on the Settings → Device Plugins **Manage device plugins**
box (add / edit address / remove; an address is stored only when its `GET /health`
speaks this contract). A `VT_PLUGINS=name=url[,name=url…]` environment variable is
imported into that map ONCE at viewer start and ignored after that; the monkeytester-ai
plugin's address is set in Settings → Device Plugins instead. A test step calls an
action with:

```
PLUGIN: <plugin> <action> [key=value …] [device=<id>] [timeout=<seconds>]
```

Values may be quoted (`pattern="WiFi connected"`). The step starts a job, waits for
it, prints the job's `log` into the run's console log, and passes only if the job
ends `passed`.

### Runs execute in order

A plugin's hardware is shared by every phone: one ESP32 board serves the Android
and the iOS runs alike, and a test's device steps assume nothing else changes the
device in between. MonkeyTester therefore runs submitted runs one after another,
in the order they arrive; a run waits until the run before it has completed.

### Inline blocks

A `PLUGIN:` step may carry a block of free text: the lines after it, up to a line
that is exactly `END`. MonkeyTester doesn't interpret the block. It strips each
line's surrounding whitespace, drops blank lines, substitutes `${VAR}`s, and sends
the lines joined with `\n` as the param `script`. An action that takes a block
declares a `script` param of type `string`; a block on any other action fails the
step before a job is started. Inside a block, `#` lines are passed through (the
plugin decides what they mean) and nothing is parsed as a test step.

A `@file:<path>` token in a block line — or in a `script=` param on the step line
(single-line form) — names a payload file. A test may only use files inside its
own directory: `<path>` is relative to the test file's directory (e.g.
`GET /x -> 200 @file:payloads/a.json`) and must stay inside it. MonkeyTester
anchors it at parse time, rewriting it to the absolute path of that file; an
absolute path, a ref with no path, or one resolving outside the directory (also
through a symlink) fails the step naming the line. The plugin then reads the file
itself, on the plugin host; the firmware never sees `@file:`.

The case ships its payload files with the upload. `ci/visual-test/submit.py`
scans each case's `.txt` files for `@file:` refs and uploads every referenced
file once as a `payloads` part named `<case>/<path>`; it fails before uploading
when a ref is absolute, leaves the case directory, is missing, is not a regular
file, or is over 4096 bytes (at most 32 files per case). A
`/api/run-test` upload (CI or a Build & Test build) must include them itself: a `payloads` part per file,
named `<path>` (single-case upload) or `<case>/<path>` (multi-case). The viewer
stages them next to the case's tests. Each segment of `<path>` uses letters,
digits, `_ . -` and doesn't start with `.`; the path is normalized (no `..`), a
payload may not replace an uploaded test or snapshot, and a `.txt` payload must
sit in a subfolder (a `.txt` at the case root is a test).

A block starts when the first non-blank line after the step is indented deeper than
the step (or is `END` itself, for an empty block); later lines may be indented any
way. A block with no `END` before the end of the file, an `END` outside a block, and
a block on a step that also has `script=` all fail the step, naming the line.

This is how a test tells a device how to behave without the plugin or MonkeyTester
knowing the device's language. Example, for an app test against a sensor board
(the rule language is the firmware's, see `docs/vt-control.md` in
the sensor firmware):

```
PLUGIN: esp32 flash firmware_dir=${FIRMWARE_DIR}
PLUGIN: esp32 factory-reset
PLUGIN: esp32 broadcast-ssid seconds=0
PLUGIN: esp32 expect
  GET /temperature.json -> 200 {"device":"{device}","temperature_c":22.5,"ok":true,"err":0,"ip":"{ip}"}
  POST /setup -> 500
END
VERIFY: The temperature card shows 22.5 °C
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

A block states only what changes. What the device keeps from earlier blocks is
the plugin's business, not MonkeyTester's: the esp32 plugin keeps every rule
until it is overridden, undone (`-> normal`) or cleared, so the `POST /setup`
rule above stays in force through the whole example.

## Versioning

Change `contract_version` for any change that breaks an existing client or plugin:
removing or renaming an endpoint, field or state, or changing a field's meaning.
Adding optional fields, actions, capabilities or event types is not breaking.
