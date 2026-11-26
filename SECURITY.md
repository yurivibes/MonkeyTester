# Security

MonkeyTester is a home-lab tool: one person (or a small team) testing their own apps on
their own phones, on a home LAN (here `192.168.125.0/24`). This file covers the viewer in
this repo and gives the overview for the whole MonkeyTester family. It does **not** claim
"zero vulnerabilities"; it lists what is protected, how, and what is not covered.

## Threat model

- **In scope:** another device on the same LAN (a guest's laptop, a compromised IoT device)
  that can reach the services' ports. It must not be able to read results, start runs, drive
  the phones, change settings or steal tokens without a credential.
- **Out of scope:** an attacker with a shell on the hosts (they can read every secret file),
  anyone who can sniff or alter LAN traffic (there is no TLS, see below), and exposure to the
  internet (never port-forward these services).

## The MonkeyTester family

| Service | Host | Port | Bind (default) | Auth |
|---|---|---|---|---|
| Viewer `app.py` (this repo) | Linux .70 | 8089 | `0.0.0.0` (`MT_VIEWER_BIND`) | Login always required (accounts, sessions); CI token for CI uploads; runner token for press events |
| ESP32 sensor plugin | Linux | 8091 | `127.0.0.1` (`PLUGIN_BIND`) | `PLUGIN_TOKEN` (`X-Plugin-Token`); mandatory on a non-loopback bind |
| ESP32 hub plugin | Linux | 8093 | `127.0.0.1` (`PLUGIN_BIND`) | `PLUGIN_TOKEN`; mandatory on a non-loopback bind |
| ESP32 fake hub | Linux | LAN | LAN on purpose: the real sensor board connects to it over Wi-Fi | see that repo's SECURITY.md |
| Mobile-Controller | Mac .71 | 8090 | LAN | `CONTROLLER_TOKEN` (`X-Controller-Token`); mandatory on a non-loopback bind |
| Build-Local plugin | Mac | 8094 | LAN | `PLUGIN_TOKEN` (`X-Plugin-Token`); mandatory on a non-loopback bind |
| MonkeyTester-Plugin-AI plugin | Mac | 8091 | `--lan` | `MTAI_TOKEN` (pairing token) |

Every service compares tokens with `hmac.compare_digest`. `/health` stays unauthenticated on
each service and only says that the service is up (no config, no secrets). The viewer reaches
the ESP32 plugins on `127.0.0.1` and the Mac services over the LAN. The legacy managed-mode
variables in the viewer (`MT_MANAGED_AI_PLUGIN_*`) are inert unless both are set.

## The viewer (this repo)

### Accounts and sessions

- **Login is always required.** There is no switch to turn it off and no localhost exemption.
- **Default credentials: `admin` / `admin`.** When `auth_config.json` has no users, the viewer
  creates this one admin account and logs a startup warning. **Change the password
  immediately** (Settings → Configuration → Account); until then the admin sees a warning
  banner on every page.
- **No sign-up.** An admin creates every other account on Settings → Users (create, reset a
  password, change the role, delete). You can't delete yourself or the last admin. Resetting a
  user's password or deleting the user ends that user's sessions. Every user can change their
  own password; that ends their other sessions.
- Roles: `admin` (everything, including Users, the CI token and the service tokens) and `user`
  (everything else). Admin API routes answer 403 to users.
- Passwords are hashed with `hashlib.scrypt` (N=2^14, r=8, p=1) and a per-user salt. An old bare
  SHA-256 hash (older viewers) is accepted once and rehashed on that login; plaintext is never
  accepted. New passwords need 8+ characters and can't be `admin`.
- Session cookie `mt_auth`: random 256-bit token, `HttpOnly; SameSite=Strict`, stored server-side
  only as its SHA-256. Sessions never expire server-side; Logout ends one browser's session.
  No `Secure` flag, because the viewer serves plain HTTP.
- Login throttle per client IP: after 5 failures, each further failure doubles the wait
  (1 s … 15 min), answered with 429 and `Retry-After`. A success resets it.
- CSRF: every state-changing request authenticated by the cookie must carry an `Origin` (or
  `Referer`) whose host matches `Host`, else 403.
- Open without a session: `/login` and its fonts, `/health`, `/api/auth/status`,
  `POST /api/login`, `POST /api/logout`, `/api/theme`, `GET /ci/submit.py` (the upload script,
  this repo's own source). Everything else: pages 302 to `/login`, APIs 401.

### Machine credentials

- **CI token** (Settings → Configuration → CI token, admins): "Generate new CI token" shows it
  **once**; afterwards only `{configured, last4, saved_at}` is returned. CI sends it as
  `Authorization: Bearer <token>` (`ci/mt-submit/submit.py` reads `MT_CI_TOKEN`). It opens only
  `POST /api/run-test` and `GET /api/runs/<run>/result` — no pages, no settings.
- **Build plugin uploads:** an upload naming a build the viewer started, while that build is
  still waiting for its apps (no run attached), is accepted once without a token. The build id
  (`build_` + 48 random bits) is known only to the viewer and the plugin it sent it to. Other
  uploads need a session or the CI token.
- **Runner token:** the viewer makes a fresh secret at each start and passes it to the test
  runners it launches (`MT_RUNNER_TOKEN`); they send it as `X-MT-Runner-Token`, accepted only on
  `POST /api/press-event`.
- **Service tokens** (Settings → Configuration → Service tokens, admins): the Mobile-Controller
  token and each plugin's token (device and build plugins). Write-only: a password field, then
  only `{configured, last4, saved_at}`; Replace/Remove. Each is checked against the service's
  authenticated endpoint before it is stored (controller `GET /devices`, device plugin
  `GET /devices`, build plugin `GET /config`).
- **Old environment variables** (`MT_CI_TOKEN`, `CONTROLLER_TOKEN` / `IOS_CONTROLLER_TOKEN`,
  `PLUGIN_TOKEN` / `MT_PLUGIN_TOKEN_<NAME>`) are imported into Settings once on the first start
  and then ignored: the viewer's environment, and so every runner it starts, mirrors
  `service_tokens.json`.

### Secrets at rest

`auth_config.json`, `service_tokens.json`, `build_config.json` and `Run/config.json` are written
with mode 0600 and are git-ignored. **They are plaintext** (except password hashes): a key kept
on the same disk would add no protection. Protect the host account and its backups. No secret is
returned by any GET or written to the logs.

### Setting it up

1. Start the viewer, open it, sign in as `admin` / `admin`, change the password.
2. Create accounts for other people on Settings → Users.
3. Settings → Configuration → Service tokens: enter the Mobile-Controller's `CONTROLLER_TOKEN`
   and each plugin's `PLUGIN_TOKEN`.
4. Settings → Configuration → CI token: generate one and store it as the `MT_CI_TOKEN` secret of
   any CI that uploads with `submit.py`.

## Not covered

- **No TLS.** Passwords, cookies and tokens cross the LAN in clear text. For access from outside
  the LAN, use a VPN (WireGuard, Tailscale) — never port forwarding.
- **Plaintext secrets at rest** (see above).
- **First-start window:** until the admin password is changed, `admin`/`admin` opens the viewer.
- Hosts are not sandboxed (no systemd hardening yet) and dependencies are not scanned in CI.

## Hardening tiers

| Tier | What | Effort | State |
|---|---|---|---|
| 1 | Auth everywhere: viewer login, tokens mandatory on non-loopback binds, loopback defaults, tokens in Settings | ≈ 1 day | done (this PR and its sibling repo PRs) |
| 2 | Code hardening: subprocess argv/validation, path traversal, request size caps, security headers, cookie/CSRF/throttle/password hashing, bandit | ≈ 3–5 days | done (this PR for auth items; see Code hardening below) |
| 3 | TLS or VPN-only access, systemd sandboxing (`ProtectSystem`, `NoNewPrivileges`, …) | ≈ 2–3 days | remaining |
| 4 | CI scanning (bandit, pip-audit, secret scanning) on every PR | ongoing | remaining |
| 5 | Swap the hand-written HTTP servers for a framework | optional | remaining |

## Code hardening

What the viewer (`app.py`) does to keep request input from reaching places it should not.
This is defence in depth, not a claim of "no vulnerabilities". It covers the home-LAN,
single-user setup. Authentication (login, sessions, CSRF, CI and service tokens) is described
in its own section.

Shared helpers live in `security_utils.py` and are unit-tested in `test_security_hardening.py`.

### Paths
- Every GET and POST path goes through `has_path_traversal` before routing. It refuses `..`
  segments, NUL, backslashes and encoded slashes with 400.
- Run routes (`/api/run/`, `/api/metadata/`, `/api/logs/`, `/api/summary/`, `/api/test/`, …)
  accept only `run_N` names. Before this, `/api/run//etc` joined an absolute path and listed
  files anywhere on disk. Test names may not contain `/`, `\` or control characters, or start
  with `.`.
- Every route that maps a URL or upload name to a file goes through
  `safe_join(root, *parts)`: `/static/<run>/…`, `/convert-video/<run>/…`, `/fonts/<name>` and the
  staged upload files under `Run/.uploads/`. `safe_join` refuses `..`, absolute parts, NUL and
  backslashes. It then checks the `os.path.realpath` result with `os.path.commonpath`, so a
  symlink pointing out of a run directory is also refused (404).
- Uploaded app file names are reduced to `[A-Za-z0-9._-]` (`safe_filename`).
- `POST /api/runs/delete` deletes only `run_N` directories. Before this, it would also remove
  the viewer's own `.builds`, `.uploads` or `.locks` folders.

### Request limits
- A POST body without a `Content-Length` header (chunked) gets 411. A malformed length gets 400.
- JSON bodies are capped at 1 MiB. `/api/run-test` uploads are capped at `MT_MAX_UPLOAD_MB`
  (default 600 MiB, the old hard-coded cap; real APK/IPA zips are about 35 MB). The body is read
  into memory, so this cap also bounds memory use. Over a cap → 413, before any of the body
  is read.
- Malformed JSON, or JSON that is not an object, gets 400. It used to be a 500 or a dropped
  connection on some routes.
- The request handler has a 60 s socket timeout. The timeout applies to each blocking socket
  operation, so the server-sent-event streams (`/api/run-events/`, `/api/devices/events`) and the
  MJPEG proxy keep working. A client that stops sending or reading is dropped.

### Responses
- Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and
  `Referrer-Policy: same-origin`.
- The `Server` header no longer names the Python version.
- An exception inside a route is logged to the viewer's stderr with its traceback. The client
  gets only `500 {"error": "internal server error"}`. An unknown GET path gets 404 (it used to get
  no response at all).

### Subprocesses and outgoing requests
- Every `subprocess` call uses an argv list with no shell (`ffmpeg` for video conversion, the
  `run_on_device.py` launch, `aapt`). Request values that reach the runner's argv are
  allowlisted, so none can start with `-`:
  - device ids: `[0-9A-Za-z][0-9A-Za-z:._-]`
  - case names
  - git branch names (`check-ref-format` rules, no leading `-`)
  - iOS bundle ids (reverse-DNS)
- **By design:** the `SCRIPT` and `DIAGNOSE` test steps run the shell command the test author
  wrote in the test file (`shell=True`, with the runner's privileges). Anyone who can start a
  run can run commands on the viewer host. Uploading tests is a code-execution capability,
  so protect it with the viewer login and network reach.
- Outgoing `urllib` calls (controller, plugins, build plugins, vision model, press events,
  `submit.py`) accept only `http`/`https` URLs. A configured or typed address can never make them
  open `file://`.

### Secret files
- `Run/config.json` holds the AI pairing token and build-plugin tokens. It is written
  atomically with mode 0600, and an existing looser file is tightened at start.
  `build_config.json` already worked this way. Tokens are never logged; GETs return only
  `{configured, last4}`.
- Secrets are plaintext at rest, readable by the viewer's user.

### Static analysis
`nix run nixpkgs#python3Packages.bandit -- -r . -x ./mt-tests/tests,./test_*.py` reports
no medium or high findings. The suppressed ones carry `# nosec <id>`, with the reason in the
comment next to them: the by-design `SCRIPT`/`DIAGNOSE` shell (B602), `urlopen` right after the
http(s) check (B310), and the fixed-argv `ffmpeg`/runner launches (B603/B607). The default
`0.0.0.0` bind (B104) is intended: the viewer is a LAN service, and `MT_VIEWER_BIND` narrows it.
The low findings that remain are `try/except: pass` blocks, `import subprocess`, and
"hardcoded password" false positives on token field names.

## Reporting a problem

Open an issue in this repository's issue tracker, or tell the
maintainer directly. Don't post working exploit details or real tokens in public places.
