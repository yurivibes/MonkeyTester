#!/usr/bin/env python3
"""
Client for the vision model, used to find UI elements by description.

Speaks the OpenAI-style chat completions API, which MonkeyTester-Plugin-AI, llama.cpp and
hosted services all offer. Every call goes through chat(), so timeouts, auth,
retries and error text are the same everywhere. Standard library only.

The usual endpoint is the MonkeyTester-Plugin-AI plugin's /v1/chat/completions. The
plugin decides which provider answers (its local Ollama model or GitHub Copilot,
chosen in Settings → Device Plugins → monkeytester-ai → Configure), so this
module never talks to Copilot itself and holds no GitHub token.

Environment (read on each call, because run_on_device.py sets these from config):
  VISION_MODEL_API_URL  — chat completions URL (or VISUAL_MODEL_ENDPOINT)
  VISION_MODEL_NAME     — model name (or VISUAL_MODEL_NAME); MonkeyTester-Plugin-AI ignores it
  VISUAL_MODEL_API_KEY  — bearer token (the plugin's pairing token), when needed
  VISION_TIMEOUT_SECS   — seconds to wait for one reply (default 310)
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_URL = "http://127.0.0.1:8081/v1/chat/completions"
DEFAULT_MODEL = "Qwen_Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"
# Longer than MonkeyTester-Plugin-AI's own 300s limit, so its answer arrives rather than a
# timeout here. A reloading model or a queued parallel run can take minutes.
DEFAULT_TIMEOUT_SECS = 310
RETRY_DELAYS_SECS = (2, 5)  # model restarting, or the service not listening yet
RETRYABLE_STATUS = (502, 503, 504)

DEBUG_MODE = os.environ.get("DEBUG", "false").lower() == "true"


class VisionError(RuntimeError):
    """The vision model could not answer. The message says why."""


def debug_print(msg, indent=""):
    """Print debug-only output with timestamp."""
    if DEBUG_MODE:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"  [{ts}] {indent}{msg}", file=sys.stderr, flush=True)


def endpoint():
    return (os.environ.get("VISION_MODEL_API_URL") or os.environ.get("VISUAL_MODEL_ENDPOINT")
            or DEFAULT_URL)


def model_name():
    return (os.environ.get("VISION_MODEL_NAME") or os.environ.get("VISUAL_MODEL_NAME")
            or DEFAULT_MODEL)


def _timeout():
    try:
        return float(os.environ.get("VISION_TIMEOUT_SECS") or DEFAULT_TIMEOUT_SECS)
    except ValueError:
        return DEFAULT_TIMEOUT_SECS


def _error_text(raw):
    """A short reason from an error reply: {"error": {"message"}}, {"error": "..."} or text."""
    try:
        error = json.loads(raw).get("error")
    except (ValueError, AttributeError):
        return raw.decode(errors="replace")[:300]
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error)


def _target():
    """(url, headers): the endpoint plus the bearer key, when one is set."""
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("VISUAL_MODEL_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return endpoint(), headers


def _post_once(payload):
    url, headers = _target()
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    if req.type not in ("http", "https"):
        raise urllib.error.URLError("only http(s) URLs are allowed")
    with urllib.request.urlopen(req, timeout=_timeout()) as resp:  # nosec B310
        return json.loads(resp.read())


def chat(content, max_tokens=500, temperature=0.0):
    """Send one user message of content parts and return the reply's text.

    Retries only failures that can pass by themselves: a refused connection or a
    503 while the model restarts. A timeout isn't retried, since it already waited.
    """
    payload = {"model": model_name(),
               "messages": [{"role": "user", "content": content}],
               "temperature": temperature,
               "max_tokens": max_tokens}
    debug_print(f"POST {endpoint()}")
    for attempt, delay in enumerate((*RETRY_DELAYS_SECS, None)):
        try:
            reply = _post_once(payload)
            return reply["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            problem = f"HTTP {e.code}: {_error_text(e.read())}"
            if e.code == 401:
                # Not retried: a wrong or missing pairing token stays wrong.
                problem += (" — check the pairing token (Settings → Device Plugins → "
                            "monkeytester-ai → Configure)")
            retryable = e.code in RETRYABLE_STATUS
        except urllib.error.URLError as e:
            problem = str(e.reason)
            retryable = not isinstance(e.reason, TimeoutError)
        except TimeoutError:
            problem, retryable = f"no reply within {_timeout():.0f}s", False
        except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
            problem, retryable = f"unusable reply: {e!r}", False
        debug_print(f"Attempt {attempt + 1} failed: {problem}")
        if not retryable or delay is None:
            raise VisionError(f"vision model at {endpoint()} failed: {problem}")
        time.sleep(delay)


def encode_image(image_path):
    """Base64 encode an image file."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def image_part(image_path):
    return {"type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{encode_image(image_path)}"}}


def text_part(text):
    return {"type": "text", "text": text}


def _parse_json_response(result):
    """The first JSON object in a reply, with or without a code fence or other prose."""
    decoder = json.JSONDecoder()
    start = result.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(result, start)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        start = result.find("{", start + 1)
    return {"found": False, "confidence": 0.0, "error": "Failed to parse response"}


def verify_element(image_path, description):
    """Verify an element exists in the screenshot."""
    prompt = f"""Analyze this UI screenshot and find: "{description}"
Return ONLY JSON:
{{"found": true/false, "coordinates": [x, y], "bbox": [x1, y1, x2, y2], "confidence": 0.0-1.0, "text": "visible text"}}"""
    return _parse_json_response(chat([image_part(image_path), text_part(prompt)]))


def find_element(image_path, description):
    """Find element coordinates for clicking/input."""
    return verify_element(image_path, description)


def check_input_field(image_path, description):
    """Check what's in an input field - has text or is it empty/placeholder?

    The reply asks for a bbox as well as the point. Without a box, Qwen2.5-VL
    gives a point on whatever text best matches the words (on a join screen,
    the "enter its link" sentence above the field), and INPUT taps there. The
    AI plugin replaces `coordinates` with the box centre when a box is present,
    which lands inside the field."""
    prompt = f"""Find input field: "{description}"
Check content. Return ONLY JSON:
{{"found": true/false, "has_text": true/false, "text": "visible text", "is_placeholder": true/false, "coordinates": [x, y], "bbox": [x1, y1, x2, y2]}}"""
    return _parse_json_response(chat([image_part(image_path), text_part(prompt)]))


def describe_screen(image_path):
    """Get a full description of the current screen."""
    prompt = """Describe this UI screen. Return JSON:
{"screen_type": "description", "elements": [{"type": "button/input", "text": "label", "position": [x, y]}]}"""
    return chat([image_part(image_path), text_part(prompt)], max_tokens=2000)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "verify":
        print(json.dumps(verify_element(sys.argv[2], sys.argv[3]), indent=2))
    elif len(sys.argv) >= 4 and sys.argv[1] == "find":
        print(json.dumps(find_element(sys.argv[2], sys.argv[3]), indent=2))
    elif len(sys.argv) >= 4 and sys.argv[1] == "check_input":
        print(json.dumps(check_input_field(sys.argv[2], sys.argv[3]), indent=2))
    elif len(sys.argv) >= 3 and sys.argv[1] == "describe":
        print(describe_screen(sys.argv[2]))
    else:
        print("Usage: python vision_analyzer.py <verify|find|check_input|describe> <image> [description]")
