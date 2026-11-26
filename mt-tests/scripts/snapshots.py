"""SNAPSHOT step: compare the screen against a baseline kept in the test case.

Baselines live next to the test files:

    <case>/snapshots/<platform>/<W>x<H>@<scale>x/<name>.png

W x H is the screen in points (iOS) or dp (Android), so phone models with the
same screen share a baseline. Images are stored and compared at 1x: the
capture is box-averaged down from device pixels, which keeps files small and
evens out anti-aliasing noise.

A missing baseline fails the step, like a mismatch. Both keep the capture as
a candidate (`<name>.actual.png`) that the viewer offers for download. With
record mode on, every SNAPSHOT saves a candidate and passes without comparing.

Standard library only: PNG is decoded and encoded here with zlib.
"""
import os
import re
import shutil
import struct
import zlib
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
CHANNEL_THRESHOLD = 24     # a pixel differs when any channel moves more than this
DEFAULT_TOLERANCE = 0.1    # % of unmasked pixels that may differ

# System bars, in pt/dp. Over-masking a few rows costs little; a clock left in
# the comparison fails every run.
STATUS_BAR = {"ios_tall": 59, "ios_short": 20, "android": 52}
NAV_BAR = {"ios_tall": 34, "ios_short": 0, "android": 48}


class SnapshotError(Exception):
    pass


# ── PNG ──────────────────────────────────────────────────────────────────────

def decode_png(data):
    """PNG bytes -> (width, height, rgb bytes). 8-bit RGB/RGBA/gray, no interlace."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SnapshotError("not a PNG file")
    pos, idat, ihdr = 8, [], None
    while pos < len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat.append(chunk)
        elif ctype == b"IEND":
            break
    if ihdr is None:
        raise SnapshotError("PNG has no IHDR")
    width, height, depth, color, _, _, interlace = ihdr
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if depth != 8 or channels is None or interlace:
        raise SnapshotError(f"unsupported PNG (bit depth {depth}, color type {color}, "
                            f"interlace {interlace})")
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    out = bytearray(height * stride)
    prev = bytearray(stride)
    src = 0
    for y in range(height):
        ftype = raw[src]
        line = bytearray(raw[src + 1:src + 1 + stride])
        src += 1 + stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            raise SnapshotError(f"bad PNG filter {ftype}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    if channels == 3:
        return width, height, bytes(out)
    rgb = bytearray(width * height * 3)
    if channels == 4:
        for c in range(3):
            rgb[c::3] = out[c::4]
    elif channels == 2:
        for c in range(3):
            rgb[c::3] = out[0::2]
    else:
        for c in range(3):
            rgb[c::3] = out
    return width, height, bytes(rgb)


def encode_png(width, height, rgb):
    def chunk(ctype, body):
        return (struct.pack(">I", len(body)) + ctype + body
                + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def read_png(path):
    return decode_png(Path(path).read_bytes())


def write_png(path, width, height, rgb):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(encode_png(width, height, rgb))


# ── Geometry ─────────────────────────────────────────────────────────────────

def format_scale(scale):
    return f"{round(scale, 3):g}"


def logical_size(px_w, px_h, scale):
    return max(1, round(px_w / scale)), max(1, round(px_h / scale))


def screen_class(platform, px_w, px_h, scale):
    w, h = logical_size(px_w, px_h, scale)
    return f"{platform}/{w}x{h}@{format_scale(scale)}x"


def downscale(width, height, rgb, out_w, out_h):
    """Box-average an RGB image to out_w x out_h."""
    if (width, height) == (out_w, out_h):
        return rgb
    xs = [(x * width // out_w, max(x * width // out_w + 1, (x + 1) * width // out_w))
          for x in range(out_w)]
    stride = width * 3
    acc = [0] * (out_w * 3)
    out = bytearray(out_w * out_h * 3)
    for ty in range(out_h):
        y0 = ty * height // out_h
        y1 = max(y0 + 1, (ty + 1) * height // out_h)
        acc = [0] * (out_w * 3)
        for y in range(y0, y1):
            row = rgb[y * stride:(y + 1) * stride]
            chans = (row[0::3], row[1::3], row[2::3])
            for c in range(3):
                ch = chans[c]
                for tx, (x0, x1) in enumerate(xs):
                    acc[tx * 3 + c] += sum(ch[x0:x1])
        rows = y1 - y0
        base = ty * out_w * 3
        for tx, (x0, x1) in enumerate(xs):
            n = rows * (x1 - x0)
            for c in range(3):
                out[base + tx * 3 + c] = (acc[tx * 3 + c] + n // 2) // n
    return bytes(out)


# ── Step arguments ───────────────────────────────────────────────────────────

def parse_args(args):
    """`<name> [mask=a,b] [tolerance=0.1]` -> (name, masks, tolerance)."""
    tokens = args.split()
    if not tokens:
        raise SnapshotError("SNAPSHOT needs a name, e.g. SNAPSHOT: home")
    name, masks, tolerance = tokens[0], [], DEFAULT_TOLERANCE
    if not NAME_RE.match(name):
        raise SnapshotError(f"snapshot name {name!r} may only use letters, digits, _ . -")
    for token in tokens[1:]:
        key, sep, value = token.partition("=")
        if not sep:
            raise SnapshotError(f"SNAPSHOT argument {token!r} is not key=value")
        if key == "mask":
            masks += parse_masks(value)
        elif key == "tolerance":
            try:
                tolerance = float(value.rstrip("%"))
            except ValueError:
                raise SnapshotError(f"tolerance={value!r} is not a number")
            if not 0 <= tolerance <= 100:
                raise SnapshotError("tolerance is a percentage between 0 and 100")
        else:
            raise SnapshotError(f"unknown SNAPSHOT argument {key!r} (use mask= or tolerance=)")
    return name, masks, tolerance


def parse_masks(value):
    """Split `status_bar,tag:clock,rect:0,0,100,20` into mask specs."""
    masks, parts, i = [], value.split(","), 0
    while i < len(parts):
        part = parts[i].strip()
        if part in ("status_bar", "nav_bar"):
            masks.append((part,))
        elif part.startswith("tag:") and len(part) > 4:
            masks.append(("tag", part[4:]))
        elif part.startswith("rect:"):
            nums = [part[5:]] + parts[i + 1:i + 4]
            if len(nums) != 4:
                raise SnapshotError("rect mask needs rect:x,y,w,h (pt/dp)")
            try:
                masks.append(("rect", *[float(n) for n in nums]))
            except ValueError:
                raise SnapshotError(f"rect mask {','.join(nums)!r} is not 4 numbers")
            i += 3
        else:
            raise SnapshotError(f"unknown mask {part!r} (status_bar, nav_bar, tag:<id>, "
                                f"rect:x,y,w,h)")
        i += 1
    return masks


def mask_rects(masks, platform, w, h, scale, elements=None):
    """Mask specs -> rectangles (x0, y0, x1, y1) at 1x."""
    tall = h / max(w, 1) > 2
    key = "android" if platform == "android" else ("ios_tall" if tall else "ios_short")
    rects = []
    for m in masks:
        if m[0] == "status_bar":
            rects.append((0, 0, w, STATUS_BAR[key]))
        elif m[0] == "nav_bar" and NAV_BAR[key]:
            rects.append((0, h - NAV_BAR[key], w, h))
        elif m[0] == "rect":
            x, y, rw, rh = m[1:]
            rects.append((x, y, x + rw, y + rh))
        elif m[0] == "tag":
            matches = [e for e in (elements or []) if e.get("id") == m[1]]
            if not matches:
                raise SnapshotError(f"mask tag:{m[1]} is not on screen")
            for e in matches:
                rects.append((e["x"] / scale, e["y"] / scale,
                              (e["x"] + e["w"]) / scale, (e["y"] + e["h"]) / scale))
    out = []
    for x0, y0, x1, y1 in rects:
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(w, int(-(-x1 // 1))), min(h, int(-(-y1 // 1)))
        if x1 > x0 and y1 > y0:
            out.append((x0, y0, x1, y1))
    return out


def mask_bitmap(w, h, rects):
    m = bytearray(w * h)
    for x0, y0, x1, y1 in rects:
        for y in range(y0, y1):
            m[y * w + x0:y * w + x1] = b"\x01" * (x1 - x0)
    return m


# ── Comparison ───────────────────────────────────────────────────────────────

def compare(w, h, actual, baseline, mask, threshold=CHANNEL_THRESHOLD):
    """-> (differing pixels, compared pixels, diff image rgb)."""
    diff = bytearray(w * h * 3)
    bad = considered = 0
    for p in range(w * h):
        i = p * 3
        a0, a1, a2 = actual[i], actual[i + 1], actual[i + 2]
        if mask[p]:
            # masked: blue tint
            diff[i], diff[i + 1], diff[i + 2] = a0 // 4, a1 // 4, 96 + a2 // 3
            continue
        considered += 1
        if (abs(a0 - baseline[i]) > threshold or abs(a1 - baseline[i + 1]) > threshold
                or abs(a2 - baseline[i + 2]) > threshold):
            bad += 1
            diff[i], diff[i + 1], diff[i + 2] = 255, 0, 0
        else:
            g = (a0 + a1 + a2) // 9 + 40  # dimmed gray
            diff[i] = diff[i + 1] = diff[i + 2] = g
    return bad, considered, bytes(diff)


def existing_classes(snapshot_root, name):
    """Screen classes (platform/class) holding a baseline called name."""
    root = Path(snapshot_root)
    if not root.is_dir():
        return []
    return sorted(str(p.parent.relative_to(root)) for p in root.glob(f"*/*/{name}.png"))


def take(name, *, capture_png, platform, scale, snapshot_root, out_dir,
         masks=(), tolerance=DEFAULT_TOLERANCE, elements=None, record=False):
    """Run one SNAPSHOT. capture_png is the device screenshot (bytes, device px).

    Returns a result dict; status is match, mismatch, missing, recorded or error.
    Paths in it are absolute; the caller makes them relative for result.json.
    """
    px_w, px_h, px = decode_png(capture_png)
    w, h = logical_size(px_w, px_h, scale)
    cls = screen_class(platform, px_w, px_h, scale)
    actual = downscale(px_w, px_h, px, w, h)
    out_dir = Path(out_dir)
    actual_path = out_dir / f"{name}.actual.png"
    write_png(actual_path, w, h, actual)
    baseline_path = Path(snapshot_root) / cls / f"{name}.png"
    result = {"name": name, "screen_class": cls, "baseline_path": f"{cls}/{name}.png",
              "tolerance": tolerance, "diff_percent": None,
              "actual": str(actual_path), "baseline": None, "diff": None}
    if record:
        return {**result, "status": "recorded",
                "message": f"recorded {cls}/{name}.png (record mode, not compared)"}
    if not baseline_path.is_file():
        have = existing_classes(snapshot_root, name)
        return {**result, "status": "missing",
                "message": (f"baseline missing: snapshots/{cls}/{name}.png"
                            + (f" (baselines exist for: {', '.join(have)})" if have
                               else " (no baselines for this snapshot yet)"))}
    bw, bh, base = read_png(baseline_path)
    baseline_copy = out_dir / f"{name}.baseline.png"
    shutil.copyfile(baseline_path, baseline_copy)
    result["baseline"] = str(baseline_copy)
    if (bw, bh) != (w, h):
        return {**result, "status": "mismatch",
                "message": f"baseline is {bw}x{bh}, capture is {w}x{h} at 1x"}
    rects = mask_rects(masks, platform, w, h, scale, elements)
    bad, considered, diff = compare(w, h, actual, base, mask_bitmap(w, h, rects))
    diff_path = out_dir / f"{name}.diff.png"
    write_png(diff_path, w, h, diff)
    percent = 100.0 * bad / considered if considered else 0.0
    status = "match" if percent <= tolerance else "mismatch"
    return {**result, "status": status, "diff": str(diff_path),
            "diff_percent": round(percent, 4),
            "message": (f"{percent:.3f}% of pixels differ (tolerance {tolerance:g}%) "
                        f"against {cls}/{name}.png")}


def record_mode():
    return (os.environ.get("MT_RECORD_SNAPSHOTS") or "").strip().lower() not in (
        "", "0", "false", "off", "no")
