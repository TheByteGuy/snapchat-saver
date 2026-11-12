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

import os
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

FEED_FILENAME = "feed.y4m"
LANDSCAPE_SIZE = (1920, 1080)
PORTRAIT_SIZE = (1080, 1920)

_TRUTHY = {"1", "true", "yes", "on"}


def mirror_enabled() -> bool:
    return os.environ.get("SNAP_MIRROR_FEED", "").strip().lower() in _TRUTHY


def feed_layout() -> str:
    layout = os.environ.get("SNAP_FEED_LAYOUT", "landscape").strip().lower()
    return layout if layout in {"landscape", "portrait"} else "landscape"


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


def compose_frame(image: Image.Image, layout: str | None = None, mirror: bool | None = None) -> Image.Image:
    """Return the RGB frame (even dimensions) that the fake webcam will show."""
    layout = layout or feed_layout()
    mirror = mirror_enabled() if mirror is None else mirror
    img = image.convert("RGB")
    if mirror:
        img = ImageOps.mirror(img)

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
        frame = compose_frame(img)
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
