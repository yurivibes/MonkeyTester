#!/usr/bin/env python3
"""
Press-event emitter for the MonkeyTester viewer.

Reports every tap/swipe the test runner sends to the device so the viewer can
draw a marker on the live stream at exactly where the test is pressing.

Design:
- Standalone module with no dependencies on the runner — the runner imports it
  (from action_executor / ios_action_executor) and calls `emit()` after each
  tap/swipe.
- Fire-and-forget: posts a tiny JSON payload to the viewer's /api/press-event
  endpoint on a daemon thread with a short timeout. If the viewer is down or
  unreachable, the event is silently dropped — a broken overlay must NEVER
  break a test run.
- Coordinates are absolute device pixels (the same space the vision model
  works in), plus the device screen size (w/h) so the viewer can convert to
  relative positions regardless of how the live frame is scaled.

Environment:
  PRESS_EVENTS_URL  — viewer base URL (e.g. http://127.0.0.1:8089). When unset,
                      events are not sent (standalone CLI runs without a viewer
                      are unaffected).
  PRESS_RUN         — run name (e.g. run_20261022_150000 or run_1). Set by the
                      runner entrypoints after the run dir is configured.
  PRESS_DEVICE      — device label for the event (serial / UDID / ios:<udid>).
  VT_RUNNER_TOKEN   — sent as X-VT-Runner-Token; the viewer makes it at start
                      and passes it to the runners it launches.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from itertools import count

_ENABLED = bool(os.environ.get("PRESS_EVENTS_URL", "").strip())
_seq = count(1)
_lock = threading.Lock()


def _post(url, payload, timeout):
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    # The viewer requires a login; the runner it starts proves itself with
    # the per-start secret the viewer passes in the environment.
    runner_token = os.environ.get("VT_RUNNER_TOKEN", "").strip()
    if runner_token:
        headers["X-VT-Runner-Token"] = runner_token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    if req.type not in ("http", "https"):
        raise urllib.error.URLError("only http(s) URLs are allowed")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        resp.read()


def emit(kind, x=None, y=None, x2=None, y2=None, screen_w=None, screen_h=None):
    """Report one tap/swipe to the viewer. Never raises.

    kind: "tap" (x, y) or "swipe" (x, y, x2, y2)
    """
    if not _ENABLED:
        return
    url = os.environ["PRESS_EVENTS_URL"].rstrip("/") + "/api/press-event"
    run = os.environ.get("PRESS_RUN", "").strip()
    if not run:
        return
    payload = {
        "run": run,
        "device": os.environ.get("PRESS_DEVICE", ""),
        "kind": kind,
        "ts": round(time.time(), 3),
        "id": next(_seq),
    }
    if x is not None:
        payload["x"] = int(x)
    if y is not None:
        payload["y"] = int(y)
    if x2 is not None:
        payload["x2"] = int(x2)
    if y2 is not None:
        payload["y2"] = int(y2)
    if screen_w:
        payload["w"] = int(screen_w)
    if screen_h:
        payload["h"] = int(screen_h)

    def _send():
        try:
            _post(url, payload, 2.0)
        except Exception:
            pass  # overlay is best-effort

    with _lock:
        t = threading.Thread(target=_send, daemon=True)
        t.start()
