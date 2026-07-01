#!/usr/bin/env python3
"""Simple web viewer for VisualTest results."""
import json
import os
import re
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TEST_DIR = Path.home() / "Workspace" / "VisualTest"
CONFIG_FILE = TEST_DIR / "config.json"
PAGE_DIR = Path(__file__).parent


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"video_size": "100%"}


def save_config(data):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def detect_file_type(filepath):
    ext = filepath.suffix.lower()
    if ext in (".mp4",):
        return "video"
    elif ext in (".png", ".jpg", ".jpeg", ".webp"):
        return "image"
    elif ext == ".json":
        return "json"
    elif ext == ".txt":
        return "text"
    try:
        with open(filepath, "rb") as f:
            header = f.read(16)
        if header[:8] == b"\x89PNG\r\n\x1a\n":
            return "image"
        elif header[:6] in (b"\xff\xd8\xff",):
            return "image"
        elif header[:4] in (b"\x1a\x45df", b"ftyp", b"\x00\x00\x00\x18ftyp"):
            return "video"
    except Exception:
        pass
    return "other"


def get_runs(sort="newest"):
    """Get list of run directories sorted by timestamp or name."""
    tasks = [d.name for d in TEST_DIR.iterdir() if d.is_dir()]
    ts_pat = re.compile(r"^run_(\d{8})_(\d{6})$")
    ts_items = []
    other_items = []
    for t in tasks:
        m = ts_pat.match(t)
        if m:
            ts_items.append(int(m.group(1) + m.group(2)))
        else:
            other_items.append(t)

    if sort == "newest":
        ts_items.sort(reverse=True)
        return [f"run_{n//1000000:08d}_{n%1000000:06d}" for n in ts_items] + sorted(other_items)
    elif sort == "oldest":
        ts_items.sort()
        return [f"run_{n//1000000:08d}_{n%1000000:06d}" for n in ts_items] + sorted(other_items)
    elif sort == "name_asc":
        return sorted(tasks)
    elif sort == "name_desc":
        return sorted(tasks, reverse=True)
    return sorted(tasks)


def get_run_files(run):
    """Get all files in a run directory."""
    run_dir = TEST_DIR / run
    if not run_dir.is_dir():
        return []
    files = []
    for f in run_dir.rglob("*"):
        if f.is_file():
            ftype = detect_file_type(f)
            rel = f.relative_to(run_dir)
            files.append({
                "name": str(rel),
                "type": ftype,
                "size": f.stat().st_size,
            })
    return files


def get_run_metadata(run):
    """Get run metadata from run_metadata.json."""
    meta_file = TEST_DIR / run / "run_metadata.json"
    if meta_file.exists():
        with open(meta_file) as f:
            return json.load(f)
    return None
def get_run_summary(run):
    """Get run summary text."""
    for name in ("run_metadata.json", "summary.txt"):
        sf = TEST_DIR / run / name
        if sf.exists():
            return sf.read_text()
    return ""


def get_test_data(run, test_name):
    """Get parsed test data: status, steps, screenshots, video."""
    files = get_run_files(run)
    run_dir = TEST_DIR / run

    # Find the result JSON
    result_file = None
    video_file = None
    screenshots = []

    for f in files:
         basename = f['name'].split("/").pop()
         if basename == f"{test_name}_result.json":
             result_file = f
         elif basename == f"{test_name}.mp4":
             video_file = f
         elif f['type'] == "image" and basename.startswith(test_name):
             screenshots.append(f)

    # Also read screenshots from result JSON (primary source, set by runner)
    if result_file:
        result_path = run_dir / result_file["name"]
        try:
            with open(result_path) as f:
                result_json = json.load(f)
            if 'screenshots' in result_json and result_json['screenshots']:
                # Build file entries from the result JSON's screenshot list
                screenshots = []
                for ss_name in result_json['screenshots']:
                    ss_path = run_dir / ss_name
                    if ss_path.exists():
                        ftype = detect_file_type(ss_path)
                        screenshots.append({
                            'name': ss_name,
                            'type': ftype,
                            'size': ss_path.stat().st_size,
                        })
        except Exception:
            pass

    data = {
        "status": "",
        "steps": [],
        "screenshots": screenshots,
        "video": video_file,
        "result_file": result_file,
    }

    if result_file:
        result_path = run_dir / result_file["name"]
        try:
            with open(result_path) as f:
                result_json = json.load(f)
            data["status"] = result_json.get("status", "")
            data["steps"] = result_json.get("steps", [])
            data["full_json"] = result_json
        except Exception:
            pass

    return data


def parse_summary_stats(summary):
    """Extract pass/fail/total counts from summary text."""
    passed = failed = total = 0
    for line in summary.split("\n"):
        line = line.strip()
        if "Passed:" in line:
            try:
                passed = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif "Failed:" in line:
            try:
                failed = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif "Total:" in line:
            try:
                total = int(line.split(":")[-1].strip())
            except ValueError:
                pass
    return passed, failed, total


class TestViewerHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        # Serve HTML pages
        if path == "/" or path == "":
            self.serve_page("index.html")
        elif path == "/test.html":
            self.serve_page("test.html")
        elif path == "/logs.html":
            self.serve_page("logs.html")
        elif path == "/player.html" or path == "/player":
            self.serve_page("player.html")
        elif path == "/test_stream.html" or path == "/test_stream":
            self.serve_page("test_stream.html")
        elif path == "/test_index.html" or path == "/test_index":
            self.serve_page("test_index.html")

        # API endpoints
        elif path == "/api/runs":
            sort = params.get("sort", ["newest"])[0]
            self.send_json(get_runs(sort))
        elif path.startswith("/api/run/"):
            run = path[len("/api/run/"):]
            self.send_json(get_run_files(run))
        elif path.startswith("/api/test/"):
            parts = path[len("/api/test/"):].split("/", 1)
            if len(parts) == 2:
                run, test = parts
                self.send_json(get_test_data(run, test))
        elif path.startswith("/api/metadata/"):
            run = path[len("/api/metadata/"):]
            self.send_json(get_run_metadata(run))
        elif path.startswith("/api/summary/"):
            run = path[len("/api/summary/"):]
            summary = get_run_summary(run)
            if summary:
                self.send_text(summary)
            else:
                self.send_json({})
        elif path.startswith("/api/stats/"):
            run = path[len("/api/stats/"):]
            summary = get_run_summary(run)
            passed, failed, total = parse_summary_stats(summary)
            self.send_json({"passed": passed, "failed": failed, "total": total})

        # Serve static files (run outputs)
        elif path.startswith("/static/"):
            self.serve_run_file(path)

        # Serve converted video (for web compatibility)
        elif path.startswith("/convert-video/"):
            self.serve_converted_video(path)

        # Config endpoint
        elif path == "/api/config":
            self.send_json(load_config())


        # Live stream route — proxy to stream_server.py which owns the V4L2 device
        elif path == "/stream.html":
            self.serve_page("stream.html")
        elif path == "/stream":
            self.proxy_stream(params)

        # Capture devices API
        elif path == "/api/capture-devices":
            self.send_json(self.list_capture_devices())

    def add_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self.add_cors_headers()
        self.end_headers()

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/" or path == "":
            self._head_page("index.html")
        elif path == "/stream.html" or path == "/stream":
            self._head_page("stream.html")
        else:
            self._head_page("index.html")

    def _head_page(self, filename):
        filepath = PAGE_DIR / filename
        if filepath.exists():
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(filepath.stat().st_size))
            self.add_cors_headers()
            self.end_headers()
        else:
            self.send_error(404)

    def serve_page(self, filename):
        filepath = PAGE_DIR / filename
        if filepath.exists():
            content_type = "text/html"
            body = filepath.read_text()
            body = body.replace("{{CSS}}", SHARED_CSS)
            body = body.replace("{{JS}}", SHARED_JS)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.add_cors_headers()
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_error(404)

    def serve_run_file(self, rel_path):
        parts = [p for p in rel_path.split("/") if p]  # remove empty strings
        if not parts or parts[0] != "static":
            self.send_error(404)
            return

        # /static/<run>/<rest_of_path>
        filepath = TEST_DIR / parts[1]
        for p in parts[2:]:
            filepath = filepath / p

        if not filepath.is_file():
            self.send_error(404)
            return

        ext = filepath.suffix.lower()
        content_type = {
            ".mp4": "video/mp4",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".json": "application/json",
            ".txt": "text/plain",
        }.get(ext)

        if not content_type:
            try:
                with open(filepath, "rb") as f:
                    header = f.read(8)
                if header[:8] == b"\x89PNG\r\n\x1a\n":
                    content_type = "image/png"
                elif header[:6] in (b"\xff\xd8\xff",):
                    content_type = "image/jpeg"
            except Exception:
                pass
            if not content_type:
                content_type = "application/octet-stream"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", filepath.stat().st_size)
        self.send_header("Accept-Ranges", "bytes")
        self.add_cors_headers()
        self.end_headers()
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def serve_converted_video(self, rel_path):
        """Convert Android MP4 to WebM for better Firefox compatibility."""
        import subprocess
        import hashlib
        import time
        
        parts = [p for p in rel_path.split("/") if p]
        if not parts or parts[0] != "convert-video":
            self.send_error(404)
            return

        # /convert-video/<run>/<video_filename>
        run = parts[1]
        video_parts = parts[2:]
        if len(video_parts) < 1:
            self.send_error(404)
            return
        video_name = "/".join(video_parts)
        video_path = TEST_DIR / run / video_name

        print(f"DEBUG convert-video: run={run}, video_name={video_name}", file=sys.stderr, flush=True)
        print(f"DEBUG convert-video: video_path={video_path}, exists={video_path.exists()}", file=sys.stderr, flush=True)

        if not video_path.is_file():
            self.send_error(404)
            return

        # Always convert to WebM (VP9 + Opus) for web compatibility
        cache_dir = TEST_DIR / ".cache"
        cache_dir.mkdir(exist_ok=True)
        
        # Create cache key
        stat = video_path.stat()
        cache_key = hashlib.md5(f"{video_path}{stat.st_mtime}{stat.st_size}".encode()).hexdigest()
        cache_file = cache_dir / f"{cache_key}.webm"

        if not cache_file.exists() or os.path.getsize(cache_file) < 1000:
            # Full conversion to WebM
            proc = subprocess.run(
                ['ffmpeg', '-y', '-i', str(video_path),
                 '-c:v', 'libvpx-vp9', '-b:v', '150k', '-minrate', '50k', '-maxrate', '250k',
                 '-pix_fmt', 'yuv420p', '-c:a', 'libopus',
                 '-f', 'webm', str(cache_file)],
                capture_output=True, text=True, timeout=300
            )
            if proc.returncode != 0:
                print(f"CONVERT ERROR: {proc.stderr[:200]}", file=sys.stderr, flush=True)
                self.send_error(500)
                return

        # Serve converted WebM
        self.send_response(200)
        self.send_header("Content-Type", "video/webm")
        self.send_header("Content-Length", cache_file.stat().st_size)
        self.send_header("Accept-Ranges", "bytes")
        self.add_cors_headers()
        self.end_headers()
        with open(cache_file, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)


    def list_capture_devices(self):
        """List available V4L2 capture devices and their capabilities."""
        import subprocess
        devices = []
        V4L2_CTL = '/usr/bin/v4l2-ctl'
        try:
            result = subprocess.run(
                [V4L2_CTL, '--list-devices'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                output = result.stdout
                # Parse: "Device Name (bus):\n	/dev/videoN\n	/dev/videoM\n"
                current_name = None
                for line in output.split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    if line.endswith(':') and not line.startswith('/'):
                        current_name = line[:-1]
                    elif line.startswith('/dev/video') and current_name:
                        # Query this device for format info
                        try:
                            fmt_result = subprocess.run(
                                [V4L2_CTL, '-d', line, '--list-formats-ext'],
                                capture_output=True, text=True, timeout=5
                            )
                            formats = []
                            if fmt_result.returncode == 0:
                                for fmt_line in fmt_result.stdout.split('\n'):
                                    fmt_line = fmt_line.strip()
                                    if 'MJPEG' in fmt_line or 'MJPG' in fmt_line:
                                        # Extract resolution and fps
                                        for sub in fmt_line.split('\n'):
                                            if 'Discrete' in sub:
                                                formats.append(sub.strip())
                                formats_str = ', '.join(formats[:3]) if formats else 'MJPEG'
                            else:
                                formats_str = 'MJPEG'
                        except:
                            formats_str = 'MJPEG'
                        
                        devices.append({
                            'path': line,
                            'name': current_name,
                            'formats': formats_str
                        })
                        current_name = None
        except Exception as e:
            print(f"Error listing devices: {e}", file=sys.stderr, flush=True)
        
        # Fallback: check if /dev/video0 exists
        if not devices:
            for i in range(10):
                path = f'/dev/video{i}'
                if os.path.exists(path):
                    devices.append({
                        'path': path,
                        'name': f'Capture device {i}',
                        'formats': 'MJPEG'
                    })
        
        return devices

    def proxy_stream(self, params):
        """Proxy /stream requests to stream_server.py which owns the V4L2 device exclusively."""
        import urllib.request
        import urllib.error

        # The streaming server runs on localhost:8088 by default
        stream_host = os.environ.get("STREAM_SERVER_URL", "http://127.0.0.1:8088")
        device = params.get("device", ["/dev/video0"])[0]
        stream_url = f"{stream_host}/stream"
        if device:
            stream_url += f"?device={urllib.parse.quote(device)}"

        # Forward the request to the streaming server
        try:
            req = urllib.request.Request(stream_url)
            req.add_header("User-Agent", "VisualTest-Proxy/1.0")
            with urllib.request.urlopen(req, timeout=10) as upstream:
                self.send_response(200)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "multipart/x-mixed-replace"))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                # Stream response body in chunks
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as e:
            print(f"Stream proxy error: {e}", file=sys.stderr, flush=True)
            self.send_error(502, f"Stream proxy failed: {e}")

    def serve_mjpeg_stream(self, device_path):
         """Proxy to the V4L2 MJPEG stream server running on port 8088."""
         import urllib.request
         import urllib.error

         stream_url = "http://localhost:8088/stream"

         try:
             req = urllib.request.Request(stream_url)
             with urllib.request.urlopen(req, timeout=5) as response:
                 self.send_response(200)
                 self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=--frame')
                 self.send_header('Cache-Control', 'no-cache')
                 self.send_header('Connection', 'close')
                 self.end_headers()

                 while True:
                     try:
                         # Read line by line until we get a blank line (end of headers)
                         line = response.readline()
                         if not line:
                             break
                         self.wfile.write(line)
                         if line.strip() == b'--frame' or line.strip().startswith(b'Content-Type') or line.strip().startswith(b'Content-Length') or line.strip() == b'':
                             pass
                     except Exception:
                         break
         except Exception as e:
             self.send_error(500, f"Stream proxy error: {e}")
             return

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            self.update_config()
        elif parsed.path == "/offer":
            self.handle_webrtc_offer()
        else:
            self.send_error(404)

    def handle_webrtc_offer(self):
        """Handle WebRTC SDP offer and return answer."""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            offer_sdp = data.get("sdp", "")
            offer_type = data.get("type", "")

            if not offer_sdp or offer_type != "offer":
                self.send_json({"error": "Invalid offer"})
                return

            # Create a simple SDP answer (minimal setup for now)
            # For real video streaming, we'd need to integrate with aiortc or similar
            answer_sdp = """v=0
o=- 0 0 IN IP4 127.0.0.1
s=VisualTest Capture Card Stream
t=0 0
a=group:BUNDLE 0
a=msid-semantic: WMS
m=video 9 UDP/TLS/RTP/SAVPF 96
c=IN IP4 0.0.0.0
a=rtcp:9 IN IP4 0.0.0.0
a=ice-ufrag:test
a=ice-pwd:testtesttesttesttesttest
a=fingerprint:sha-256 AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89
a=setup:actpass
a=mid:0
a=extmap:1 urn:ietf:params:rtp-hdrext:ssrc-audio-level
a=extmap:2 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time
a=extmap:3 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01
a=extmap:4 urn:ietf:params:rtp-hdrext:sdes:mid
a=extmap:5 urn:ietf:params:rtp-hdrext:sdes:rtp-stream-id
a=extmap:6 urn:ietf:params:rtp-hdrext:sdes:repaired-rtp-stream-id
a=rtcp-fb:96 nack
a=rtcp-fb:96 nack pli
a=rtcp-fb:96 goog-remb
a=rtcp-fb:96 transport-cc
a=fmtp:96 max-fs=122,max-cs=1300
a=rtpmap:96 VP8/90000
a=rid:send recv
a=ssrc:1 cname:test
a=ssrc:1 msid:test test
a=ssrc:1 mslabel:test
a=ssrc:1 label:test
"""

            self.send_json({
                "sdp": answer_sdp,
                "type": "answer"
            })
        except json.JSONDecodeError:
            self.send_json({"error": "Invalid JSON"})
        except Exception as e:
            self.send_json({"error": str(e)})

    def update_config(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            config = load_config()
            config.update(data)
            save_config(config)
            self.send_json(config)
        except Exception:
            self.send_error(400)

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_text(self, text):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def log_message(self, format, *args):
        pass


# ─── Shared CSS & JS template string for all pages ───

SHARED_CSS = r"""
:root {
    --bg: #0d1117;
    --surface: #161b22;
    --card: #1c2333;
    --card-hover: #222d3f;
    --accent: #58a6ff;
    --accent-dim: #1f6feb;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --border: #30363d;
    --pass: #3fb950;
    --pass-bg: rgba(63,185,80,0.15);
    --fail: #f85149;
    --fail-bg: rgba(248,81,73,0.15);
    --skip: #8b949e;
    --skip-bg: rgba(139,148,158,0.15);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); }
.container { max-width: 1600px; margin: 0 auto; padding: 20px 24px; }
header { padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; }
h1 { font-size: 1.3rem; font-weight: 600; background: linear-gradient(135deg, var(--accent), #a371f7); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.breadcrumb { font-size: 0.85rem; margin-top: 4px; }
.breadcrumb span { cursor: pointer; color: var(--accent); }
.breadcrumb span:hover { text-decoration: underline; }
.breadcrumb .sep { color: var(--text-muted); margin: 0 6px; }

/* Run list (index.html) */
.run-list { display: flex; flex-direction: column; gap: 6px; }
.run-item { background: var(--surface); padding: 12px 16px; border-radius: 8px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; border: 1px solid var(--border); transition: border-color 0.15s; }
.run-item:hover { border-color: var(--accent); }
.run-item .run-id { font-weight: 600; font-size: 0.95rem; }
.run-item .run-meta { font-size: 0.8rem; color: var(--text-muted); }

/* Metadata & stats */
.run-meta-bar { background: var(--surface); padding: 10px 16px; border-radius: 8px; margin-bottom: 12px; display: flex; gap: 20px; flex-wrap: wrap; border: 1px solid var(--border); }
.run-meta-item { font-size: 0.8rem; color: var(--text-muted); }
.run-meta-item b { color: var(--accent); }

.stats-bar { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.stat-item { background: var(--surface); padding: 6px 14px; border-radius: 6px; font-size: 0.85rem; border: 1px solid var(--border); }
.stat-item .count { font-weight: 700; }
.stat-item.pass .count { color: var(--pass); }
.stat-item.fail .count { color: var(--fail); }
.stat-item.total .count { color: var(--accent); }

/* Test panel layout */
.tests-panel { min-width: 0; }
.run-summary { grid-column: 1 / -1; }

.tests-main { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; min-height: 60vh; }

/* Video panel (inside tests-main, right column) */
.video-panel { align-self: start; position: sticky; top: 20px; }
.video-section { margin-top: 16px; display: block; }
.video-container { background: #000; border-radius: 10px; overflow: hidden; border: 1px solid var(--border); display: block; width: 100%; }
.video-container video { width: 100%; height: auto; display: block; background: #000; }

.test-body-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.test-content { min-width: 0; }

/* Test cases list */
.tests-list { display: flex; flex-direction: column; gap: 12px; }
.test-case { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: visible; }
.test-case-header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; background: var(--card); border-bottom: 1px solid var(--border); }
.test-case-name { font-weight: 600; font-size: 0.95rem; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 700; letter-spacing: 0.5px; }
.badge.pass { background: var(--pass-bg); color: var(--pass); border: 1px solid rgba(63,185,80,0.3); }
.badge.fail { background: var(--fail-bg); color: var(--fail); border: 1px solid rgba(248,81,73,0.3); }
.badge.skip { background: var(--skip-bg); color: var(--skip); border: 1px solid rgba(139,148,158,0.3); }
.badge.unknown { background: rgba(139,148,158,0.15); color: var(--text-muted); border: 1px solid var(--border); }

.test-case-body { padding: 14px 16px; }

/* Steps */
.steps-section { margin-bottom: 12px; }
.steps-section h3, .screenshots-section h3 { font-size: 0.75rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
.steps-list { display: flex; flex-direction: column; gap: 2px; }
.step-row { display: flex; align-items: flex-start; gap: 10px; padding: 6px 8px; border-radius: 4px; transition: background 0.1s; }
.step-row:hover { background: var(--card); }
.step-status { width: 8px; height: 8px; border-radius: 50%; margin-top: 5px; flex-shrink: 0; }
.step-status.pass { background: var(--pass); }
.step-status.fail { background: var(--fail); }
.step-status.skipped { background: var(--skip); }
.step-content { flex: 1; min-width: 0; }
.step-command { font-weight: 600; color: var(--accent); font-size: 0.8rem; }
.step-args { color: var(--text); font-size: 0.8rem; margin-top: 1px; }
.step-description { color: var(--text); font-size: 0.8rem; margin-top: 2px; }
.step-reason { color: var(--fail); font-style: italic; font-size: 0.75rem; margin-top: 2px; }

/* Screenshots */
.screenshots-section { margin-top: 10px; }
.screenshot-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 6px; }
.screenshot-grid img { width: 100%; border-radius: 6px; border: 1px solid var(--border); cursor: pointer; transition: border-color 0.15s; }
.screenshot-grid img:hover { border-color: var(--accent); }

/* JSON details */
.json-details { margin-top: 12px; }
.json-details summary { cursor: pointer; color: var(--accent); font-size: 0.8rem; font-weight: 500; }
.json-view { background: var(--bg); padding: 12px; border-radius: 6px; font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace; font-size: 0.72rem; max-height: 300px; overflow: auto; margin-top: 8px; color: var(--text-muted); border: 1px solid var(--border); line-height: 1.5; }

/* Loading */
.loading { text-align: center; padding: 30px; color: var(--text-muted); font-size: 0.9rem; }

/* DL button */
.dl-btn { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 5px 12px; border-radius: 6px; cursor: pointer; font-size: 0.75rem; margin-top: 8px; display: inline-flex; align-items: center; gap: 4px; text-decoration: none; transition: all 0.15s; }
.dl-btn:hover { border-color: var(--accent); color: var(--accent); }

/* Summary (logs.html) */
.summary { background: var(--surface); padding: 16px; border-radius: 8px; margin-bottom: 20px; white-space: pre-wrap; font-family: monospace; font-size: 0.82rem; word-break: break-word; border: 1px solid var(--border); }

/* Scrollbar */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #484f58; }



# ── Live stream page ──
.nav a { color: var(--text-muted); text-decoration: none; padding: 4px 12px; border-radius: 6px; font-size: 0.85rem; transition: all 0.15s; }
.nav a:hover, .nav a.active { color: var(--accent); background: var(--card); }

.stream-layout { display: grid; grid-template-columns: 280px 1fr; gap: 16px; min-height: 70vh; }
.stream-controls { display: flex; flex-direction: column; gap: 12px; }
.stream-controls label { font-size: 0.8rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
.stream-controls select { background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; font-size: 0.9rem; width: 100%; cursor: pointer; }
.stream-controls select:focus { outline: none; border-color: var(--accent); }
.device-info { font-size: 0.75rem; color: var(--text-muted); padding: 8px 0; border-top: 1px solid var(--border); }
.stream-status { display: flex; align-items: center; gap: 8px; font-size: 0.85rem; padding: 8px 0; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--text-muted); }
.status-dot.connected { background: var(--pass); box-shadow: 0 0 6px rgba(63,185,80,0.4); }
.status-dot.disconnected { background: var(--fail); }

.stream-viewport { display: flex; flex-direction: column; }
.stream-player { position: relative; background: #000; border-radius: 10px; overflow: hidden; border: 1px solid var(--border); flex: 1; min-height: 400px; display: flex; align-items: center; justify-content: center; }
.stream-player video { width: 100%; height: auto; display: block; }
.stream-overlay { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; background: rgba(0,0,0,0.7); color: var(--text-muted); font-size: 0.9rem; }
.overlay-spinner { width: 32px; height: 32px; border: 3px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.stream-meta { font-size: 0.7rem; color: var(--text-muted); margin-top: 6px; font-family: monospace; }
"""


SHARED_JS = r"""
const currentSort = 'newest';
let videoSize = '100%';

// Load saved config
fetchJSON('/api/config').then(cfg => {
    if (cfg.video_size) {
        videoSize = cfg.video_size;
        applyVideoSize(videoSize);
    }
}).catch(() => {});

function applyVideoSize(size) {
    videoSize = size;
    document.querySelectorAll('.video-size-btn').forEach(btn => {
        btn.classList.toggle('active', btn.textContent === size);
    });
    const vp = document.querySelector('.video-panel');
    if (!vp) return;
    const vw = size.replace('%', 'vw');
    vp.style.width = vw;
    vp.style.maxWidth = vw;
    vp.querySelectorAll('.video-container').forEach(container => {
        container.style.width = '100%';
        container.style.maxWidth = '100%';
    });
}

function setVideoSize(size) {
    applyVideoSize(size);
    fetch('/api/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({video_size: size})
    }).catch(() => {});
}

function fetchJSON(url) { return fetch(url).then(r => r.json()); }
function fetchText(url) { return fetch(url).then(r => r.text()); }
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
    return (bytes/1048576).toFixed(1) + ' MB';
}
"""


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8089
    print(f"Serving VisualTest viewer on http://localhost:{port}")
    print(f"Reading from: {TEST_DIR}")
    server = HTTPServer(("0.0.0.0", port), TestViewerHandler)
    server.serve_forever()

