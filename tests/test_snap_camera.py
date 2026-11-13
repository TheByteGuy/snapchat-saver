"""Fake-webcam feed conversion and selector override tests (no browser needed)."""
from __future__ import annotations

import json
import os

from PIL import Image

from app.snapchat import camera
from app.snapchat.selectors import DEFAULT_SELECTORS, SelectorStore, escape_name


def _portrait(tmp_path, color=(200, 30, 30)):
    img = Image.new("RGB", (1080, 1920), color)
    # a white block in the top-left corner to check orientation/mirroring
    img.paste((255, 255, 255), (0, 0, 200, 200))
    path = tmp_path / "fact.png"
    img.save(path)
    return path


def test_landscape_feed_letterboxes_whole_image(tmp_path, monkeypatch):
    monkeypatch.delenv("SNAP_MIRROR_FEED", raising=False)
    monkeypatch.delenv("SNAP_FEED_LAYOUT", raising=False)
    out = camera.prepare_camera_feed(_portrait(tmp_path), tmp_path / "cam")
    assert out == tmp_path / "cam" / "feed.y4m"
    head = out.read_bytes()[:60]
    assert head.startswith(b"YUV4MPEG2 W1920 H1080 ")
    frame = camera.read_y4m_frame(out)
    assert frame.size == (1920, 1080)
    r, g, b = frame.getpixel((960, 540))
    assert r > 170 and g < 70 and b < 70  # centre is the image colour
    # The fitted image (96% of 1080 high => ~583 px wide) sits inside the central 9:16 crop (607 px).
    fg_h = int(1080 * 0.96)
    fg_w = round(1080 * fg_h / 1920)
    left = (1920 - fg_w) // 2
    top = (1080 - fg_h) // 2
    assert left >= (1920 - 1080 * 9 // 16) // 2
    # white marker in the image's top-left corner survives, unmirrored
    assert min(frame.getpixel((left + 20, top + 20))) > 200
    assert min(frame.getpixel((left + fg_w - 20, top + 20))) < 120
    # the side fill is the darkened blur, not the bright image
    assert frame.getpixel((20, 540))[0] < 170


def test_mirror_and_portrait_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAP_MIRROR_FEED", "1")
    monkeypatch.setenv("SNAP_FEED_LAYOUT", "portrait")
    out = camera.prepare_camera_feed(_portrait(tmp_path), tmp_path)
    frame = camera.read_y4m_frame(out)
    assert frame.size == (1080, 1920)
    assert min(frame.getpixel((1080 - 30, 30))) > 200  # marker moved to the right
    assert min(frame.getpixel((30, 30))) < 120


def test_feed_is_replaced_atomically(tmp_path, monkeypatch):
    monkeypatch.delenv("SNAP_FEED_LAYOUT", raising=False)
    monkeypatch.delenv("SNAP_MIRROR_FEED", raising=False)
    out = camera.prepare_camera_feed(_portrait(tmp_path, (200, 30, 30)), tmp_path)
    camera.prepare_camera_feed(_portrait(tmp_path, (30, 30, 200)), tmp_path)
    assert camera.read_y4m_frame(out).getpixel((960, 540))[2] > 170
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".feed-")] == []


def test_placeholder_feed(tmp_path):
    out = camera.ensure_placeholder_feed(tmp_path)
    assert out.stat().st_size > 1920 * 1080
    mtime = os.stat(out).st_mtime_ns
    camera.ensure_placeholder_feed(tmp_path)
    assert os.stat(out).st_mtime_ns == mtime  # not overwritten


def test_selector_overrides(tmp_path):
    path = tmp_path / "selectors.json"
    store = SelectorStore(path)
    assert store.get("shutter") == DEFAULT_SELECTORS["shutter"]
    path.write_text(
        json.dumps(
            {
                "shutter": ["#my-shutter"],
                "send_to": {"prepend": ["#first"], "append": ["#last"]},
                "bogus": ["x"],
                "send_button": "not a list",
            }
        )
    )
    os.utime(path, (1, 1))  # force a distinct mtime
    assert store.get("shutter") == ["#my-shutter"]
    st = store.get("send_to")
    assert st[0] == "#first" and st[-1] == "#last" and st[1:-1] == DEFAULT_SELECTORS["send_to"]
    assert store.get("send_button") == DEFAULT_SELECTORS["send_button"]
    path.write_text("{broken")
    os.utime(path, (2, 2))
    assert store.get("shutter") == DEFAULT_SELECTORS["shutter"]


def test_recipient_name_templating():
    store = SelectorStore(None)
    cands = store.get("picker_recipient", name='Bob "B" Jones')
    assert all("{name}" not in c for c in cands)
    assert any(escape_name('Bob "B" Jones') in c for c in cands)


def test_calibration_pattern_decodes_crops_and_mirroring():
    from PIL import Image, ImageOps

    from app.snapchat import camera

    frame = camera.calibration_frame()
    for x, y, w, h, mirrored, shot_w in [(652, 0, 405, 720, True, 203), (757, 0, 607, 1080, False, 300),
                                         (100, 300, 500, 700, True, 250)]:
        crop = frame.crop((x, y, x + w, y + h))
        if mirrored:
            crop = ImageOps.mirror(crop)
        got = camera.decode_calibration(crop.resize((shot_w, round(shot_w * h / w)), Image.BILINEAR))
        assert got and got["preview_mirrored"] is mirrored
        assert all(abs(a - b) <= 4 for a, b in zip(got["region"], (x, y, w, h)))
    assert camera.decode_calibration(Image.new("RGB", (200, 300), (90, 120, 40))) is None


def test_compose_frame_places_image_in_region():
    from PIL import Image

    from app.snapchat import camera

    frame = camera.compose_frame(Image.new("RGB", (1080, 1920), (250, 10, 10)), region=(652, 0, 405, 720))
    assert frame.size == (1920, 1080)
    r, g, b = frame.getpixel((652 + 200, 360))
    assert r > 230 and g < 40
    assert frame.getpixel((100, 900))[0] < 200  # outside the region: darkened blur, not the image


def test_chat_row_name_skips_bitmoji_status_emoji():
    from app.snapchat.client import SnapchatClient, _search_term

    row = "\U0001F60A\n\U0001F483Q-Tip\U0001F57A\nDelivered \u00b7 2h \u00b7 1351 \U0001F525"
    assert SnapchatClient._name_from_row(row) == "\U0001F483Q-Tip\U0001F57A"
    assert SnapchatClient._name_from_row("\U0001F60A\nJamison Cartwright\nOpened \u00b7 2h") == "Jamison Cartwright"
    assert _search_term("\U0001F483Q-Tip\U0001F57A") == "Q-Tip"
    assert _search_term("Carol O'Neil") == "Carol O'Neil"
    assert _search_term("\U0001F483") == "\U0001F483"
