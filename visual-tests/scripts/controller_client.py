#!/usr/bin/env python3
"""
Device executor for test runs: an HTTP client for MonkeyTester-Plugin-Mobile-Controller.

The Mobile Controller owns every device, iOS and Android, and is its own repo and
service. This module is what the runner calls for every device operation, so
nothing here touches adb, Appium or devicectl. The contract is its API.md.

    runner ──HTTP──► device_controller.py (Mac) ──► iPhone / Android phone

Coordinates are DEVICE PIXELS everywhere, the same space the vision model works
in. The controller converts them where a device needs something else.

Environment:
  CONTROLLER_URL   — controller base URL (e.g. http://192.168.125.5:8090). REQUIRED.
  CONTROLLER_TOKEN — shared token, only sent when the controller requires it.
  DEVICE_ID        — target device (set by run_on_device.py per device).
"""
import io
import json
import os
import tempfile
import time
import urllib.request
import urllib.error
import zipfile
from urllib.parse import quote

DEBUG_MODE = os.environ.get("DEBUG", "false").lower() == "true"

# The controller API this client was written for (see API.md there).
EXPECTED_API_VERSION = 2

# Below this a screenshot is a blank or lock-screen frame, not a rendered app.
MIN_RENDERED_SCREENSHOT_BYTES = 30 * 1024


def _ts():
    return time.strftime("%H:%M:%S")


def _debug(msg):
    if DEBUG_MODE:
        print(f"  [{_ts()}] [device] {msg}")


def _controller_url():
    url = (os.environ.get("CONTROLLER_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "CONTROLLER_URL is not set — point MonkeyTester at "
            "MonkeyTester-Plugin-Mobile-Controller (e.g. http://192.168.125.5:8090).")
    return url.rstrip("/")


def _device_id():
    device_id = (os.environ.get("DEVICE_ID") or "").strip()
    if not device_id:
        raise RuntimeError("DEVICE_ID is not set (no target device)")
    return device_id


# ── HTTP helpers ──

def _http(method, url, payload=None, raw_body=None, timeout=90, headers=None):
    """HTTP to the device controller. Returns (ok, parsed_json_or_bytes).

    Never raises. JSON bodies are parsed; non-JSON responses (screenshots,
    video) are returned as raw bytes.
    """
    data = raw_body
    hdrs = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
    token = (os.environ.get("CONTROLLER_TOKEN") or "").strip()
    if token:
        hdrs["X-Controller-Token"] = token
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        if req.type not in ("http", "https"):
            raise urllib.error.URLError("only http(s) URLs are allowed")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            body = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                return True, json.loads(body.decode() or "{}")
            return True, body
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode() or "{}")
        except Exception:
            err = {"error": f"HTTP {e.code}"}
        return False, err
    except Exception as e:
        return False, {"error": str(e)}


def _error_text(body):
    """The readable part of a controller error body, for logs and run results."""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"{error.get('message')} ({error.get('code')})"
    return str(error or body)[:300]


def _device_path(suffix):
    return f"/devices/{_device_id()}/{suffix}"


def _get(path, timeout=90):
    return _http("GET", f"{_controller_url()}{path}", timeout=timeout)


def _post(path, payload=None, raw_body=None, headers=None, timeout=120):
    return _http("POST", f"{_controller_url()}{path}", payload=payload,
                 raw_body=raw_body, headers=headers, timeout=timeout)


def _delete(path, timeout=300):
    return _http("DELETE", f"{_controller_url()}{path}", timeout=timeout)


# ── Devices ──

def list_devices():
    """Devices the controller serves now. Returns (devices, error), where each
    device is {"id", "platform", "model"}. One platform failing isn't an error
    while the other still has devices, so the run can use those."""
    try:
        ok, body = _get("/devices", timeout=30)
    except RuntimeError as e:
        return [], str(e)
    if not ok or not isinstance(body, dict):
        return [], f"controller unreachable: {_error_text(body)}"
    devices = body.get("devices") or []
    errors = body.get("errors") or {}
    if errors and not devices:
        return [], "; ".join(f"{name}: {text}" for name, text in sorted(errors.items()))
    return devices, None


def check_api_version():
    """Return an error message when the controller speaks another API version."""
    try:
        ok, body = _get("/health", timeout=30)
    except RuntimeError as e:
        return str(e)
    if not ok or not isinstance(body, dict):
        return f"controller unreachable: {_error_text(body)}"
    version = body.get("api_version")
    if version != EXPECTED_API_VERSION:
        return (f"controller API version is {version}, but this MonkeyTester "
                f"expects {EXPECTED_API_VERSION}; update whichever is older")
    return None


def get_device_model():
    """The device's model name, or "" when the controller doesn't report one."""
    devices, _ = list_devices()
    for device in devices:
        if device.get("id") == os.environ.get("DEVICE_ID"):
            return device.get("model") or ""
    return ""


# ── Screenshots & size ──

def screenshot(path):
    """Save the current screen as a PNG at `path`. Returns the path, or None."""
    print(f"  [{_ts()}] Capturing screen: {path}")
    try:
        ok, body = _get(_device_path("frame"), timeout=60)
    except RuntimeError as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        return None
    if not ok or not isinstance(body, (bytes, bytearray)):
        print(f"  [{_ts()}] -> ERROR: screenshot failed: {_error_text(body)}")
        return None
    try:
        os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(bytes(body))
    except OSError as e:
        print(f"  [{_ts()}] -> ERROR: screenshot save failed: {e}")
        return None
    _debug(f"screenshot {len(body):,} bytes -> {path}")
    return str(path)


def _query_screen_size():
    """Device screen size in pixels from the controller, or None if it can't say."""
    try:
        ok, body = _get(_device_path("screen"), timeout=30)
    except RuntimeError as e:
        print(f"  [{_ts()}] WARNING: {e}")
        return None
    if ok and isinstance(body, dict):
        w = int(body.get("width") or 0)
        h = int(body.get("height") or 0)
        if w and h:
            return w, h
    print(f"  [{_ts()}] WARNING: screen size unavailable: {_error_text(body)}")
    return None


def get_screen_size():
    """Device screen size in PIXELS (matching screenshots), from the controller."""
    size = _query_screen_size()
    if size is None:
        print(f"  [{_ts()}] WARNING: assuming a 750x1334 screen")
        return 750, 1334
    return size


def get_screen_info():
    """{width, height, scale} for the device: pixels plus the px-per-pt/dp
    scale. Raises RuntimeError when the controller can't say (an older
    controller reports no scale)."""
    ok, body = _get(_device_path("screen"), timeout=30)
    if not ok or not isinstance(body, dict):
        raise RuntimeError(f"screen info unavailable: {_error_text(body)}")
    try:
        width, height = int(body["width"]), int(body["height"])
        scale = float(body["scale"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("the controller reported no screen scale; update "
                           "MonkeyTester-Plugin-Mobile-Controller (GET /devices/<id>/screen "
                           "must return width, height and scale)")
    if width <= 0 or height <= 0 or scale <= 0:
        raise RuntimeError(f"bad screen info from the controller: {body}")
    return {"width": width, "height": height, "scale": scale}


def get_elements():
    """Visible tagged elements: [{id, x, y, w, h, label?}] in device pixels.
    Raises RuntimeError on failure."""
    ok, body = _get(_device_path("elements"), timeout=90)
    if not ok or not isinstance(body, dict) or not isinstance(body.get("elements"), list):
        raise RuntimeError(f"element list unavailable: {_error_text(body)}")
    return body["elements"]


_screen_sizes = {}  # device id -> (width, height)


def _cached_screen_size():
    """For press events, which fire on every tap. One query per device."""
    device_id = _device_id()
    if device_id not in _screen_sizes:
        size = _query_screen_size()
        if size is None:  # a guess isn't worth remembering; ask again next time
            return 750, 1334
        _screen_sizes[device_id] = size
    return _screen_sizes[device_id]


# ── Input ──

def _action(name, **params):
    timeout = params.pop("timeout", 60)
    try:
        ok, body = _post(_device_path("actions"), {"action": name, **params},
                         timeout=timeout)
    except RuntimeError as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        return False
    if not ok:
        print(f"  [{_ts()}] -> {name} error: {_error_text(body)}")
    return ok


def click(x, y):
    """Tap at device-pixel coordinates."""
    print(f"  [{_ts()}] CLICK: ({x}, {y})")
    ok = _action("tap", x=int(x), y=int(y))
    try:
        import press_events
        w, h = _cached_screen_size()
        press_events.emit("tap", x=x, y=y, screen_w=w, screen_h=h)
    except Exception:
        pass
    return ok


def delete_selected(count=1):
    """Send backspace key `count` times."""
    print(f"  [{_ts()}] DELETE: {count} backspace(s)")
    return _action("delete", count=int(count))


def input_text(text):
    """Type text into the focused field, then dismiss the keyboard."""
    print(f"  [{_ts()}] INPUT: '{text}'")
    ok = _action("text", text=text, timeout=90)
    if not ok:
        return False
    _action("dismiss-keyboard")
    return True


def swipe(start_x, start_y, end_x, end_y, duration_ms=300):
    """Swipe from one point to another (device pixels)."""
    print(f"  [{_ts()}] SWIPE: ({start_x},{start_y}) -> ({end_x},{end_y})")
    ok = _action("swipe", x1=int(start_x), y1=int(start_y),
                 x2=int(end_x), y2=int(end_y), duration_ms=int(duration_ms),
                 timeout=90)
    try:
        import press_events
        w, h = _cached_screen_size()
        press_events.emit("swipe", x=start_x, y=start_y, x2=end_x, y2=end_y,
                          screen_w=w, screen_h=h)
    except Exception:
        pass
    return ok


def swipe_up(amount=0.3):
    w, h = get_screen_size()
    return swipe(w // 2, int(h * (1 - amount)), w // 2, int(h * amount))


def swipe_down(amount=0.3):
    w, h = get_screen_size()
    return swipe(w // 2, int(h * amount), w // 2, int(h * (1 - amount)))


def swipe_left(amount=0.3):
    w, h = get_screen_size()
    return swipe(int(w * (1 - amount)), h // 2, int(w * amount), h // 2)


def swipe_right(amount=0.3):
    w, h = get_screen_size()
    return swipe(int(w * amount), h // 2, int(w * (1 - amount)), h // 2)


def keyevent(code):
    """Press an Android key code; the controller maps common ones on iOS."""
    print(f"  [{_ts()}] KEY: {code}")
    return _action("keyevent", code=str(code))

# ── App lifecycle ──

def restart_app(package_name, clear_data=True):
    """Stop the app, clear its data where the platform can, relaunch it, and
    check that it rendered.

    With ``clear_data=False`` the app is only stopped and relaunched, so its
    saved state (e.g. an accepted EULA) carries over.

    iOS can't clear app data, so it survives restarts there. If a suite needs a
    clean state on iOS, add an UNINSTALL step in INSTRUCTION.txt.
    """
    if not package_name:
        print(f"  [{_ts()}] -> ERROR: no package or bundle ID for the app under test")
        return False
    print(f"  [{_ts()}] RESTART: {package_name}"
          f"{'' if clear_data else ' (keeping app data)'}")
    _action("terminate", bundle_id=package_name)
    if clear_data:
        _action("clear-data", bundle_id=package_name)
    time.sleep(2)
    _action("unlock", timeout=30)
    tmp = os.path.join(tempfile.gettempdir(), f"vt_render_{os.getpid()}.png")
    for attempt in range(3):
        if not _action("launch", bundle_id=package_name, timeout=180):
            print(f"  [{_ts()}] -> LAUNCH FAILED")
            return False
        print(f"  [{_ts()}] Launched app (attempt {attempt + 1})...")
        time.sleep(8)
        if screenshot(tmp):
            size = os.path.getsize(tmp)
            os.remove(tmp)
            if size >= MIN_RENDERED_SCREENSHOT_BYTES:
                print(f"  [{_ts()}] App appears active ({size:,}B screenshot)")
                return True
            print(f"  [{_ts()}] Screenshot too small ({size}B) — retrying...")
        else:
            print(f"  [{_ts()}] Screenshot failed — retrying launch...")
    print(f"  [{_ts()}] -> WARNING: could not confirm app rendered")
    return False


def install_apk(app_path):
    """Install an app: send an .apk/.ipa/.zip byte for byte (or zip a .app
    directory) to the controller, which installs it on the device."""
    print(f"  [{_ts()}] APP: {app_path}")
    if not app_path or not os.path.exists(app_path):
        print(f"  [{_ts()}] -> ERROR: app bundle not found: {app_path}")
        return False
    if app_path.lower().endswith((".apk", ".ipa", ".zip")):
        with open(app_path, "rb") as f:
            payload = f.read()
        name = os.path.basename(app_path)
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            base = os.path.basename(app_path)
            for root, dirs, files in os.walk(app_path):
                for fn in files:
                    full = os.path.join(root, fn)
                    zf.write(full, os.path.join(base, os.path.relpath(full, app_path)))
        payload = buf.getvalue()
        name = os.path.basename(app_path) + ".zip"
    try:
        print(f"  [{_ts()}] Shipping {len(payload) // 1024}KB app bundle "
              f"to controller...")
        ok, body = _post(_device_path("apps"), raw_body=payload,
                         headers={"Content-Type": "application/zip"},
                         timeout=900)
    except RuntimeError as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        return False
    if ok:
        print(f"  [{_ts()}] -> Installed: {name}")
        return True
    print(f"  [{_ts()}] -> INSTALL FAILED: {_error_text(body)}")
    return False


def uninstall_app(package_name):
    """Uninstall an app by package or bundle ID."""
    print(f"  [{_ts()}] UNINSTALL: {package_name}")
    if not package_name or package_name == "unknown":
        print(f"  [{_ts()}] -> SKIP: no package name")
        return True
    try:
        ok, body = _delete(_device_path(f"apps/{quote(package_name, safe='')}"), timeout=300)
    except RuntimeError as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        return False
    if ok:
        print(f"  [{_ts()}] -> Uninstalled: {package_name}")
        return True
    print(f"  [{_ts()}] -> UNINSTALL FAILED: {_error_text(body)}")
    return False


def launch_app(bundle_id):
    return _action("launch", bundle_id=bundle_id, timeout=180)


def terminate_app(bundle_id):
    return _action("terminate", bundle_id=bundle_id)


# ── Video recording ──

_recording = None  # {"udid":..., "token":...}


def start_recording(remote_path=None, time_limit=None, local_path=None):
    """Start recording the device screen on the controller.

    time_limit=None sends no limit, so an Android recording runs until
    stop_recording (the controller's screenrecord has no length cap then).
    A number caps it at that many seconds. iOS ignores it."""
    global _recording
    if _recording is not None:
        print(f"  [{_ts()}] START_RECORDING: already recording, stopping first...")
        stop_recording()
    try:
        req = {} if time_limit is None else {"time_limit": int(time_limit)}
        ok, body = _post(_device_path("recordings"), req, timeout=30)
    except RuntimeError as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        return False
    if not ok or not isinstance(body, dict) or not body.get("ok"):
        print(f"  [{_ts()}] -> RECORDING START FAILED: {_error_text(body)}")
        return False
    _recording = {"udid": _device_id(), "token": body.get("token", "")}
    print(f"  [{_ts()}] Recording started on controller "
          f"(token={_recording['token']})")
    return True


def stop_recording(remote_path=None, local_path=None):
    """Stop the active recording and download the MP4 to the outputs dir.
    Returns the local path or None."""
    global _recording
    if _recording is None:
        print(f"  [{_ts()}] STOP_RECORDING: No capture process running")
        return None
    rec, _recording = _recording, None
    try:
        ok, body = _post(f"/recordings/{rec['token']}/stop", {}, timeout=300)
        if not ok or not isinstance(body, dict) or not body.get("ok"):
            print(f"  [{_ts()}] -> RECORDING STOP FAILED: {_error_text(body)}")
            return None
        ok2, video = _get(f"/recordings/{rec['token']}/video", timeout=300)
    except RuntimeError as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        return None
    if not ok2 or not isinstance(video, (bytes, bytearray)):
        print(f"  [{_ts()}] -> VIDEO DOWNLOAD FAILED: {_error_text(video)}")
        return None
    out_dir = os.environ.get("CAPTURE_OUTPUT_DIR") or os.environ.get("OUTPUTS_DIR") \
        or os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(
        out_dir, f"capture_{time.strftime('%Y%m%d_%H%M%S')}.mp4")
    try:
        with open(out_path, "wb") as f:
            f.write(bytes(video))
    except Exception as e:
        print(f"  [{_ts()}] -> VIDEO SAVE FAILED: {e}")
        return None
    print(f"  [{_ts()}] -> Video saved: {out_path} ({len(video):,} bytes)")
    return out_path


def check_unlocked():
    """Ask the controller to wake/unlock the device (lock-screen guard)."""
    try:
        ok, body = _post(_device_path("actions"), {"action": "unlock"}, timeout=30)
    except RuntimeError as e:
        print(f"  [{_ts()}] -> ERROR: {e}")
        return False
    if not ok:
        print(f"  [{_ts()}] -> device not usable: {_error_text(body)}")
        return False
    return True
