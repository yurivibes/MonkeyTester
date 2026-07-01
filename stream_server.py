#!/usr/bin/env python3
"""
MJPEG streaming server using v4l2-ctl + aiohttp.
Captures MJPEG frames from a V4L2 device using v4l2-ctl and serves them via HTTP.
Only one process can access the device at a time.

Usage:
    python3 stream_server.py [device] [port]
    Defaults: /dev/video0, port 8088

Environment variables:
    STREAM_DEVICE  - V4L2 device path (default: /dev/video0)
    STREAM_PORT    - HTTP port        (default: 8088)
"""
import asyncio
import argparse
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("stream_server")

# --- Global state ---
_frame_bytes = []
_frame_lock = threading.Lock()
_running = True


def _signal_handler(sig, _frame):
    global _running
    log.info("Received signal %s, shutting down...", sig)
    _running = False


def _capture_loop(device: str):
    """Use v4l2-ctl to capture MJPEG frames from the V4L2 device."""
    global _frame_bytes
    V4L2_CTL = "/usr/bin/v4l2-ctl"

    tmp_file = tempfile.NamedTemporaryFile(suffix=".mjpeg", delete=False)
    tmp_path = tmp_file.name
    tmp_file.close()
    log.info("Capture temp file: %s", tmp_path)

    try:
        while _running:
            try:
                # Set MJPEG format, then capture to file
                proc = subprocess.run(
                     [
                         V4L2_CTL,
                         "-d", device,
                         "--set-fmt-video=width=1920,height=1080,pixelformat=MJPG",
                         "--set-parm=30",
                         "--stream-count=300",
                         "--stream-to=" + tmp_path,
                     ],
                     capture_output=True,
                     text=True,
                     timeout=30,
                 )

                if proc.returncode != 0:
                    log.warning("v4l2-ctl exit %d: %s", proc.returncode, proc.stderr[:200])
                    if not _running:
                        break
                    time.sleep(0.5)
                    continue

                # Read capture file and extract JPEG frames
                with open(tmp_path, "rb") as f:
                    data = f.read()

                if len(data) == 0:
                    time.sleep(0.1)
                    continue

                # Split into individual JPEG frames (SOI=FFD8, EOI=FFD9)
                frames = []
                i = 0
                while i < len(data) - 1:
                    if data[i] == 0xFF and data[i + 1] == 0xD8:  # SOI
                        start = i
                        i += 2
                        while i < len(data) - 1:
                            if data[i] == 0xFF and data[i + 1] == 0xD9:  # EOI
                                frames.append(data[start : i + 2])
                                i += 2
                                break
                            elif data[i] == 0xFF and data[i + 1] == 0x00:
                                i += 2
                            else:
                                i += 1
                        else:
                            if start < len(data):
                                frames.append(data[start:])
                    else:
                        i += 1

                if frames:
                    with _frame_lock:
                        _frame_bytes = [max(frames, key=len)]
                    log.debug("Captured %d frames", len(frames))

            except subprocess.TimeoutExpired:
                log.warning("v4l2-ctl timeout")
            except Exception as e:
                log.error("Capture error: %s", e, exc_info=True)

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        log.info("V4L2 capture loop exited")


async def mjpeg_handler(request: web.Request) -> web.StreamResponse:
    """Serve MJPEG stream."""
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=--frame",
            "Cache-Control": "no-cache",
            "Connection": "close",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await resp.prepare(request)

    while _running:
        with _frame_lock:
            jpeg = _frame_bytes[0] if _frame_bytes else None
        if jpeg is None:
            await asyncio.sleep(0.1)
            continue

        await resp.write(b"--frame\r\n")
        await resp.write(b"Content-Type: image/jpeg\r\n")
        await resp.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
        await resp.write(jpeg)
        await resp.write(b"\r\n")
        await asyncio.sleep(0.033)  # ~30fps

    await resp.write(b"--frame--\r\n")
    log.info("MJPEG client disconnected")
    return resp


async def health_handler(request: web.Request) -> web.Response:
    with _frame_lock:
        has_frame = len(_frame_bytes) > 0
    return web.json_response({
        "status": "ok",
        "device": request.app["config"]["device"],
        "has_frame": has_frame,
        "frame_size": len(_frame_bytes[0]) if _frame_bytes else 0,
    })


async def on_startup(app: web.Application):
    device = app["config"]["device"]
    if not os.path.exists(device):
        log.error("Device %s does not exist", device)
        sys.exit(1)

    log.info("Starting capture from %s", device)
    t = threading.Thread(target=_capture_loop, args=(device,), daemon=True)
    t.start()

    log.info("Stream server running at http://0.0.0.0:%d", app["config"]["port"])


def make_app(device: str, port: int) -> web.Application:
    app = web.Application()
    app["config"] = {"device": device, "port": port}

    app.router.add_get("/stream", mjpeg_handler)
    app.router.add_get("/health", health_handler)

    app.on_startup.append(on_startup)

    return app


if __name__ == "__main__":
    import signal

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    parser = argparse.ArgumentParser(description="MJPEG streaming server")
    parser.add_argument("device", nargs="?", default=os.environ.get("STREAM_DEVICE", "/dev/video0"))
    parser.add_argument("port", nargs="?", type=int, default=int(os.environ.get("STREAM_PORT", "8088")))
    args = parser.parse_args()

    # Auto-detect device if specified one doesn't exist
    if not os.path.exists(args.device):
        for i in range(10):
            p = f"/dev/video{i}"
            if os.path.exists(p):
                log.info("Specified device %s not found, using %s", args.device, p)
                args.device = p
                break
        else:
            log.error("No V4L2 devices found")
            sys.exit(1)

    app = make_app(args.device, args.port)
    web.run_app(app, host="0.0.0.0", port=args.port)
