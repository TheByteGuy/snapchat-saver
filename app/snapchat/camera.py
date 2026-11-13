"""Turn the daily image into Chromium's fake-webcam feed.

Chromium is launched with ``--use-file-for-fake-video-capture=<CAMERA_DIR>/feed.y4m``.
It opens that file afresh every time a page starts a camera capture
(getUserMedia), so overwriting the fixed path *before* Snapchat Web opens the
camera changes what the camera "sees". A single-frame Y4M file is looped by
Chromium indefinitely (verified with Chromium 14x headless).

Frame layout (env ``SNAP_FEED_LAYOUT``):

* ``landscape`` (default): 1920x1080 frame, the portrait image centred at
  ``SNAP_FEED_SCALE`` (default 0.96) of the frame height on a blurred, darkened
  copy of itself. Chromium serves the file's native size unless the page asks
  for another aspect ratio, in which case it centre-crops. A 9:16 centre crop of
  a 16:9 frame is exactly as wide as the letterboxed portrait image, so the whole
  image survives whether Snapchat shows the stream landscape or crops it to its
  portrait camera pane.
* ``portrait``: 1080x1920 frame containing just the image. Sharper when the page
  accepts a portrait stream, but if the page requests a landscape resolution
  (e.g. 1280x720) Chromium crops the middle third and most of the image is lost.

Mirroring (env ``SNAP_MIRROR_FEED``, default off): if the snap that friends
receive comes out mirrored (text reversed), set ``SNAP_MIRROR_FEED=1`` to
pre-flip the feed horizontally.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

FEED_FILENAME = "feed.y4m"
LANDSCAPE_SIZE = (1920, 1080)
PORTRAIT_SIZE = (1080, 1920)

_TRUTHY = {"1", "true", "yes", "on"}

log = logging.getLogger("snapchat")

CALIBRATION_FILENAME = "calibration.json"
CALIBRATION_MAX_AGE = 7 * 24 * 3600  # re-measure weekly; Snapchat's layout can change
# Part of a 1920x1080 feed that Snapchat Web's camera pane showed when first measured by
# hand (x, y, width, height). Used until an automatic calibration has succeeded.
MEASURED_REGION = (652, 0, 405, 720)

# --- calibration pattern -------------------------------------------------------------
# The frame is painted with stripes: the red channel carries a non-repeating on/off
# sequence along x, the green channel one along y. From any screenshot that shows at
# least _WINDOW stripes we can tell exactly which part of the frame is visible, and
# whether it is mirrored, without relying on exact colour values.
_UNIT = 20
_WINDOW = 11
_LOW, _HIGH = 40, 215


# 96 stripes; every run of 11 consecutive stripes is unique, read forwards or backwards.
_SEQ = [int(c) for c in "111000110010100010001101010101000111010001110011011011010011101010110010110010100100011011001011"]


def calibration_frame(size: tuple[int, int] = LANDSCAPE_SIZE) -> Image.Image:
    w, h = size
    red = Image.new("L", (w, 1))
    red.putdata([_HIGH if _SEQ[(x // _UNIT) % len(_SEQ)] else _LOW for x in range(w)])
    green = Image.new("L", (1, h))
    green.putdata([_HIGH if _SEQ[(y // _UNIT) % len(_SEQ)] else _LOW for y in range(h)])
    return Image.merge("RGB", (red.resize((w, h)), green.resize((w, h)), Image.new("L", (w, h), 128)))


def _decode_line(values: list[float], frame_len: int) -> tuple[float, float] | None:
    """Map a line of pixel values to frame coordinates: returns (coord at index 0, coord at index len)."""
    n = len(values)
    if n < 40:
        return None
    ordered = sorted(values)
    lo, hi = ordered[int(n * 0.05)], ordered[int(n * 0.95)]
    if hi - lo < 50:
        return None
    mid = (lo + hi) / 2
    bits = [1 if v > mid else 0 for v in values]
    for i in range(1, n - 1):  # remove single-pixel noise
        if bits[i - 1] == bits[i + 1] != bits[i]:
            bits[i] = bits[i - 1]
    runs: list[list[int]] = []  # [bit, start, length]
    for i, b in enumerate(bits):
        if runs and runs[-1][0] == b:
            runs[-1][2] += 1
        else:
            runs.append([b, i, 1])
    runs = runs[1:-1]  # first and last are cut off by the edge
    if len(runs) < 5:
        return None
    shortest = min(r[2] for r in runs)
    if shortest < 3:
        return None
    unit = statistics.mean(r[2] for r in runs if r[2] < shortest * 1.5)
    counts = [max(1, round(r[2] / unit)) for r in runs]
    if any(abs(r[2] / unit - c) > 0.35 for r, c in zip(runs, counts)):
        return None
    unit = sum(r[2] for r in runs) / sum(counts)
    text = "".join(str(r[0]) * c for r, c in zip(runs, counts))
    if len(text) < _WINDOW:
        return None
    usable = (frame_len // _UNIT)
    seq = "".join(map(str, _SEQ[:usable])) if usable <= len(_SEQ) else "".join(map(str, _SEQ))
    hits = [(k, False) for k in range(len(seq)) if seq.startswith(text, k)]
    rseq = seq[::-1]
    hits += [(k, True) for k in range(len(rseq)) if rseq.startswith(text, k)]
    if len(hits) != 1:
        return None
    k, mirrored = hits[0]
    start = runs[0][1]
    scale = _UNIT / unit  # frame pixels per screenshot pixel
    if mirrored:
        at_start = (len(seq) - k) * _UNIT
        return at_start + start * scale, at_start - (n - start) * scale
    at_start = k * _UNIT
    return at_start - start * scale, at_start + (n - start) * scale


def decode_calibration(shot: Image.Image, frame_size: tuple[int, int] = LANDSCAPE_SIZE) -> dict | None:
    """Work out which part of the calibration frame a screenshot of the camera pane shows.

    Returns {"region": [x, y, w, h], "preview_mirrored": bool} in frame pixels, or None.
    """
    shot = shot.convert("RGB")
    sw, sh = shot.size
    if sw < 60 or sh < 60:
        return None
    red, green, _ = shot.split()
    rpx, gpx = red.load(), green.load()
    xs, ys = [], []
    for frac in (0.3, 0.38, 0.46, 0.54, 0.62, 0.7):
        row = min(sh - 2, max(1, int(sh * frac)))
        got = _decode_line([(rpx[x, row - 1] + rpx[x, row] + rpx[x, row + 1]) / 3 for x in range(sw)], frame_size[0])
        if got:
            xs.append(got)
        col = min(sw - 2, max(1, int(sw * (frac - 0.0))))
        got = _decode_line([(gpx[col - 1, y] + gpx[col, y] + gpx[col + 1, y]) / 3 for y in range(sh)], frame_size[1])
        if got:
            ys.append(got)
    if len(xs) < 2 or len(ys) < 2:
        return None
    x_a, x_b = statistics.median(v[0] for v in xs), statistics.median(v[1] for v in xs)
    y_a, y_b = statistics.median(v[0] for v in ys), statistics.median(v[1] for v in ys)
    if y_b <= y_a:
        return None  # upside-down preview: not something we handle
    mirrored = x_a > x_b
    x0, x1 = max(0.0, min(x_a, x_b)), min(float(frame_size[0]), max(x_a, x_b))
    y0, y1 = max(0.0, y_a), min(float(frame_size[1]), y_b)
    if x1 - x0 < 80 or y1 - y0 < 80:
        return None
    return {"region": [round(x0), round(y0), round(x1 - x0), round(y1 - y0)], "preview_mirrored": mirrored}


def _frame_size(layout: str | None = None) -> tuple[int, int]:
    return PORTRAIT_SIZE if (layout or feed_layout()) == "portrait" else LANDSCAPE_SIZE


def _calibration_path(camera_dir: Path | None = None) -> Path:
    return feed_path(camera_dir).with_name(CALIBRATION_FILENAME)


def load_calibration(camera_dir: Path | None = None, max_age: float | None = None) -> dict | None:
    """The saved calibration for the current feed layout, or None if missing, stale or invalid."""
    try:
        data = json.loads(_calibration_path(camera_dir).read_text())
        if tuple(data["frame"]) != _frame_size():
            return None
        if max_age is not None and time.time() - float(data.get("at", 0)) > max_age:
            return None
        x, y, w, h = (int(v) for v in data["region"])
        if w < 80 or h < 80:
            return None
        return data
    except Exception:
        return None


def needs_calibration(camera_dir: Path | None = None) -> bool:
    if os.environ.get("SNAP_FEED_CALIBRATE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return load_calibration(camera_dir, CALIBRATION_MAX_AGE) is None


def save_calibration(result: dict, camera_dir: Path | None = None) -> None:
    data = dict(result, frame=list(_frame_size()), at=time.time())
    path = _calibration_path(camera_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def write_calibration_feed(camera_dir: Path | None = None) -> Path:
    out = feed_path(camera_dir)
    write_y4m(calibration_frame(_frame_size()), out)
    return out


def visible_region(camera_dir: Path | None = None) -> tuple[int, int, int, int] | None:
    """Where in the feed frame the image must go to fill Snapchat's camera pane, if known.

    Order of preference: env SNAP_FEED_REGION="x,y,w,h", a saved calibration, the
    hand-measured default for the 1920x1080 layout.
    """
    raw = os.environ.get("SNAP_FEED_REGION", "").strip()
    if raw:
        try:
            x, y, w, h = (int(float(v)) for v in raw.split(","))
            if w > 0 and h > 0:
                return x, y, w, h
        except ValueError:
            log.warning("ignoring invalid SNAP_FEED_REGION=%r (expected x,y,w,h)", raw)
    if os.environ.get("SNAP_FEED_LAYOUT", "").strip().lower() == "centered":
        return None
    data = load_calibration(camera_dir)
    if data:
        return tuple(int(v) for v in data["region"])  # type: ignore[return-value]
    if _frame_size() == LANDSCAPE_SIZE:
        return MEASURED_REGION
    return None


def mirror_enabled() -> bool:
    return os.environ.get("SNAP_MIRROR_FEED", "").strip().lower() in _TRUTHY


def feed_layout() -> str:
    layout = os.environ.get("SNAP_FEED_LAYOUT", "landscape").strip().lower()
    return "portrait" if layout == "portrait" else "landscape"


def _feed_scale() -> float:
    try:
        value = float(os.environ.get("SNAP_FEED_SCALE", "0.96"))
    except ValueError:
        return 0.96
    return min(max(value, 0.5), 1.0)


def feed_path(camera_dir: Path | None = None) -> Path:
    """The FIXED path Chromium reads the fake camera from."""
    if camera_dir is None:
        from app.config import get_settings

        camera_dir = get_settings().CAMERA_DIR
    return Path(camera_dir) / FEED_FILENAME


def compose_frame(image: Image.Image, layout: str | None = None, mirror: bool | None = None,
                  region: tuple[int, int, int, int] | None = None) -> Image.Image:
    """Return the RGB frame (even dimensions) that the fake webcam will show.

    With ``region`` (x, y, w, h) the image is fitted into exactly that part of the
    frame, which is the part Snapchat's camera pane displays.
    """
    layout = layout or feed_layout()
    mirror = mirror_enabled() if mirror is None else mirror
    img = image.convert("RGB")
    if mirror:
        img = ImageOps.mirror(img)

    if region is not None:
        size = _frame_size(layout)
        frame = ImageOps.fit(img, size, Image.BILINEAR).filter(ImageFilter.GaussianBlur(40))
        frame = ImageEnhance.Brightness(frame).enhance(0.6)
        x, y, w, h = region
        fitted = ImageOps.contain(img, (max(2, w), max(2, h)), Image.LANCZOS)
        frame.paste(fitted, (x + (w - fitted.width) // 2, y + (h - fitted.height) // 2))
        return frame

    if layout == "portrait":
        frame = Image.new("RGB", PORTRAIT_SIZE, (0, 0, 0))
        fitted = ImageOps.contain(img, PORTRAIT_SIZE, Image.LANCZOS)
        frame.paste(fitted, ((PORTRAIT_SIZE[0] - fitted.width) // 2, (PORTRAIT_SIZE[1] - fitted.height) // 2))
        return frame

    w, h = LANDSCAPE_SIZE
    background = ImageOps.fit(img, LANDSCAPE_SIZE, Image.LANCZOS)
    background = background.filter(ImageFilter.GaussianBlur(48))
    background = ImageEnhance.Brightness(background).enhance(0.6)
    target_h = int(h * _feed_scale())
    fg = ImageOps.contain(img, (w, target_h), Image.LANCZOS)
    background.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    return background


def write_y4m(frame: Image.Image, path: Path) -> None:
    """Write a single-frame 4:2:0 YUV4MPEG2 file atomically (temp file + rename).

    The rename means a capture that is already running keeps reading the old
    inode instead of a half-written file.
    """
    frame = frame.convert("RGB")
    w, h = frame.size
    if w % 2 or h % 2:
        frame = frame.crop((0, 0, w - w % 2, h - h % 2))
        w, h = frame.size
    y, cb, cr = frame.convert("YCbCr").split()
    cb = cb.resize((w // 2, h // 2), Image.BILINEAR)
    cr = cr.resize((w // 2, h // 2), Image.BILINEAR)
    header = f"YUV4MPEG2 W{w} H{h} F30:1 Ip A1:1 C420jpeg\n".encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".feed-", suffix=".y4m", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(header)
            fh.write(b"FRAME\n")
            fh.write(y.tobytes())
            fh.write(cb.tobytes())
            fh.write(cr.tobytes())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_y4m_frame(path: Path) -> Image.Image:
    """Decode the first frame of a 4:2:0 Y4M file (used by tests and the dashboard preview)."""
    data = Path(path).read_bytes()
    header_end = data.index(b"\n")
    header = data[:header_end].decode("ascii").split()
    if header[0] != "YUV4MPEG2":
        raise ValueError("not a Y4M file")
    w = int(next(t[1:] for t in header if t.startswith("W")))
    h = int(next(t[1:] for t in header if t.startswith("H")))
    frame_start = data.index(b"FRAME", header_end) + len(b"FRAME")
    frame_start = data.index(b"\n", frame_start) + 1
    ysize, csize = w * h, (w // 2) * (h // 2)
    buf = data[frame_start : frame_start + ysize + 2 * csize]
    y = Image.frombytes("L", (w, h), buf[:ysize])
    cb = Image.frombytes("L", (w // 2, h // 2), buf[ysize : ysize + csize]).resize((w, h))
    cr = Image.frombytes("L", (w // 2, h // 2), buf[ysize + csize :]).resize((w, h))
    return Image.merge("YCbCr", (y, cb, cr)).convert("RGB")


def prepare_camera_feed(image_path: Path, camera_dir: Path | None = None) -> Path:
    """Convert the daily PNG into the fake-webcam feed file and return its (fixed) path."""
    with Image.open(image_path) as img:
        img.load()
        frame = compose_frame(img, region=visible_region(camera_dir))
    out = feed_path(camera_dir)
    write_y4m(frame, out)
    return out


def ensure_placeholder_feed(camera_dir: Path | None = None) -> Path:
    """Make sure a valid feed file exists so Chromium's fake camera can start at all."""
    out = feed_path(camera_dir)
    if out.exists() and out.stat().st_size > 0:
        return out
    frame = Image.new("RGB", LANDSCAPE_SIZE, (24, 24, 32))
    draw = ImageDraw.Draw(frame)
    draw.text((60, 60), "snapchat-saver: no image loaded yet", fill=(200, 200, 200))
    write_y4m(frame, out)
    return out
