"""Per-device run queue: one run at a time on a physical device.

flock is released by the OS when the process exits, so a crashed runner can't
leave a device locked. Locks are taken in sorted order so that two multi-device
runs can't deadlock.
"""
import fcntl
import os
import re
import time
from pathlib import Path

DEFAULT_TIMEOUT_SECS = 3600


def _lock_path(lock_dir, device):
    return Path(lock_dir) / (re.sub(r"[^A-Za-z0-9._-]", "_", device) + ".lock")


def acquire_device_locks(lock_dir, devices, on_wait=None,
                         timeout=DEFAULT_TIMEOUT_SECS, poll_secs=2.0):
    """Block until every device is free; return the fds holding the locks.

    Keep the returned list referenced for as long as the run lasts.
    Raises TimeoutError, after releasing anything held, if a device stays busy.
    """
    Path(lock_dir).mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    held = []
    try:
        for device in sorted(set(devices)):
            fd = os.open(_lock_path(lock_dir, device), os.O_CREAT | os.O_RDWR, 0o644)
            held.append(fd)
            announced = False
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not announced and on_wait:
                        on_wait(device)
                    announced = True
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"device {device} stayed busy for {timeout}s")
                    time.sleep(poll_secs)
    except BaseException:
        release_device_locks(held)
        raise
    return held


def release_device_locks(fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass
