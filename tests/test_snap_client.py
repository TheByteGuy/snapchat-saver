"""Runs the real SnapchatClient (headless Chromium, fake webcam) against a local fake "Snapchat Web".

The fixture page uses the same roles / aria-labels / texts that the primary
selectors in app/snapchat/selectors.py target, and records what gets sent.
Skipped only when no Chromium binary is available.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("playwright")

from app.snapchat import NotLoggedInError, SnapchatClient  # noqa: E402
from app.snapchat.client import _find_bundled_chromium  # noqa: E402

FRIENDS = ["Alice Smith", "Alice Smithson", "Bob Jones", "Carol O'Neil", "My AI"] + [f"Friend {i:02d}" for i in range(1, 25)]

LOGIN_HTML = """<!doctype html><html><body><h1>Log in to Snapchat</h1>
<form><input name="accountIdentifier" placeholder="Username or email"><button type="button">Next</button></form>
</body></html>"""

APP_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Snapchat</title><style>
body{margin:0;font-family:sans-serif;display:flex;height:100vh}
nav{width:260px;border-right:1px solid #ccc;display:flex;flex-direction:column}
#chats{overflow-y:auto;flex:1}
#chats div[role=listitem]{padding:10px;border-bottom:1px solid #eee}
main{flex:1;position:relative;display:flex;align-items:center;justify-content:center;background:#111}
#pane{width:270px;height:480px;overflow:hidden;display:flex;align-items:center;justify-content:center}
#pane video{width:270px;height:480px;flex:none;object-fit:cover}
canvas{width:270px;height:480px;object-fit:cover}
#promo{position:fixed;left:0;top:250px;width:520px;height:560px;pointer-events:none;opacity:.9}
.hidden{display:none!important}
#cookie{position:fixed;left:0;right:0;bottom:0;top:0;background:rgba(0,0,0,.5);z-index:10}
#picker{position:absolute;inset:20px;background:#fff;padding:10px;overflow:auto}
#picker li.sel{background:#ffc}
</style></head><body>
<nav><button aria-label="New Chat">+</button><div id="chats" role="list"></div></nav>
<main>
  <div id="opencam" role="button" tabindex="0" aria-label="Click the Camera to send Snaps" style="color:#fff">Click the Camera to send Snaps</div>
  <div id="pane"><video id="v" class="hidden" autoplay playsinline muted></video></div>
  <canvas id="c" class="hidden"></canvas>
  <button id="shutter" class="hidden" aria-label="Capture"></button>
  <button id="sendto" class="hidden">Send To</button>
  <form id="picker" class="hidden" onsubmit="return false">
    <input id="q" placeholder="Search">
    <ul id="plist"></ul>
    <button id="send" type="submit">Send</button>
  </form>
  <div id="done" class="hidden" style="color:#fff">Sent!</div>
</main>
<!-- A playing non-camera video (like Spotlight), bigger than the camera pane. -->
<video id="promo" autoplay muted playsinline></video>
<div id="cookie"><div style="background:#fff;padding:20px">We use cookies <button>Accept All</button></div></div>
<script>
const FRIENDS = __FRIENDS__;
const chats = document.getElementById('chats');
for (const f of FRIENDS) { const d=document.createElement('div'); d.setAttribute('role','listitem');
  d.innerHTML='<span></span><br><small>Received · 2h</small>'; d.firstChild.textContent=f; chats.appendChild(d); }
document.querySelector('#cookie button').onclick=()=>document.getElementById('cookie').remove();
const $=id=>document.getElementById(id); let stream=null; const selected=new Set();
{ const pc=document.createElement('canvas'); pc.width=320; pc.height=320; const g=pc.getContext('2d'); let n=0;
  setInterval(()=>{ g.fillStyle=`hsl(${(n+=7)%360},80%,50%)`; g.fillRect(0,0,320,320); }, 50);
  const rec=new MediaRecorder(pc.captureStream(20), {mimeType:'video/webm'}); const parts=[];
  rec.ondataavailable=e=>parts.push(e.data);
  rec.onstop=()=>{ const p=$('promo'); p.loop=true; p.src=URL.createObjectURL(new Blob(parts,{type:'video/webm'})); p.play().catch(()=>{}); };
  rec.start(); setTimeout(()=>rec.stop(), 600); }
$('opencam').onclick=async()=>{ stream=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:1280},height:{ideal:720}}});
  // Like Snapchat Web: the webcam goes through a hidden 9:16 "lens" canvas at the top-left,
  // and the preview <video id="local-video"> shows that canvas' stream.
  const raw=document.createElement('video'); raw.muted=true; raw.srcObject=stream; await raw.play();
  const lens=document.createElement('canvas'); lens.width=406; lens.height=720;
  lens.style.cssText='position:fixed;left:0;top:0;z-index:-1'; document.body.appendChild(lens);
  const lg=lens.getContext('2d');
  (function draw(){ const vw=raw.videoWidth, vh=raw.videoHeight; if (vw) { const cw=vh*406/720;
    lg.drawImage(raw,(vw-cw)/2,0,cw,vh,0,0,406,720); } requestAnimationFrame(draw); })();
  $('v').id='local-video'; window.$v=$('local-video'); window.$v.srcObject=lens.captureStream(30); $v.classList.remove('hidden'); $('opencam').classList.add('hidden');
  await $v.play(); $('shutter').classList.remove('hidden'); };
$('shutter').onclick=()=>{ $('pane').classList.add('hidden'); const v=$v, c=$('c'); c.width=v.videoWidth; c.height=v.videoHeight;
  c.getContext('2d').drawImage(v,0,0); stream.getTracks().forEach(t=>t.stop());
  v.classList.add('hidden'); c.classList.remove('hidden'); $('shutter').classList.add('hidden'); $('sendto').classList.remove('hidden'); };
function renderPicker(){ const q=$('q').value.toLowerCase(); const ul=$('plist'); ul.innerHTML='';
  for (const f of FRIENDS) { if (q && !f.toLowerCase().includes(q)) continue;
    const li=document.createElement('li'); li.setAttribute('role','listitem'); li.setAttribute('aria-selected', selected.has(f));
    if (selected.has(f)) li.className='sel'; const s=document.createElement('span'); s.textContent=f; li.appendChild(s);
    li.onclick=()=>{ selected.has(f)?selected.delete(f):selected.add(f); renderPicker(); }; ul.appendChild(li); } }
$('q').oninput=renderPicker;
$('sendto').onclick=()=>{ $('picker').classList.remove('hidden'); renderPicker(); };
$('send').onclick=async()=>{ if(!selected.size) return; const c=$('c'); const ctx=c.getContext('2d');
  const px=(x,y)=>Array.from(ctx.getImageData(Math.floor(x),Math.floor(y),1,1).data.slice(0,3));
  await fetch('/api/sent',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    recipients:[...selected], width:c.width, height:c.height, center:px(c.width/2,c.height/2), edge:px(5,c.height/2)})});
  if (window.__STALL_SEND__) return;
  $('picker').classList.add('hidden'); $('sendto').classList.add('hidden'); $('done').classList.remove('hidden'); };
</script></body></html>""".replace("__FRIENDS__", json.dumps(FRIENDS))


class FakeSnapchat:
    def __init__(self):
        self.logged_in = True
        self.sent: list[dict] = []
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/web":
                    if state.logged_in:
                        self._send(200, APP_HTML.encode())
                    else:
                        self._send(302, headers={"Location": "/accounts/login"})
                elif path == "/accounts/login":
                    self._send(200, LOGIN_HTML.encode())
                else:
                    self._send(404, b"not found")

            def do_POST(self):
                if self.path == "/api/sent":
                    n = int(self.headers.get("Content-Length", "0"))
                    state.sent.append(json.loads(self.rfile.read(n)))
                    self._send(200, b"{}", "application/json")
                else:
                    self._send(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://localhost:{self.server.server_address[1]}/web"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@functools.lru_cache(maxsize=1)
def _chromium() -> str | None:
    if os.environ.get("SNAP_CHROMIUM_PATH"):
        return os.environ["SNAP_CHROMIUM_PATH"]
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and os.path.exists(path):
                return path
    except Exception:
        pass
    return _find_bundled_chromium()


@pytest.fixture(scope="module")
def fake_snapchat():
    srv = FakeSnapchat()
    yield srv
    srv.close()


@pytest.fixture
def env(tmp_path, monkeypatch, fake_snapchat):
    exe = _chromium()
    if not exe:
        pytest.skip("no Chromium binary available")
    monkeypatch.setenv("SNAP_CHROMIUM_PATH", exe)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SNAP_HEADLESS", "1")
    monkeypatch.setenv("SNAP_WEB_URL", fake_snapchat.url)
    monkeypatch.setenv("SNAP_HUMAN_DELAY", "0")
    monkeypatch.setenv("SNAP_STEP_TIMEOUT", "6")
    monkeypatch.delenv("SNAP_MIRROR_FEED", raising=False)
    monkeypatch.delenv("SNAP_FEED_LAYOUT", raising=False)
    from app.config import reload_settings

    reload_settings()
    fake_snapchat.logged_in = True
    fake_snapchat.sent.clear()
    yield fake_snapchat
    reload_settings()


def _image(tmp_path, color) -> Path:
    path = tmp_path / f"img_{color[0]}_{color[2]}.png"
    Image.new("RGB", (1080, 1920), color).save(path)
    return path


def _run(coro_fn):
    async def wrapper():
        client = SnapchatClient()
        try:
            return await coro_fn(client)
        finally:
            await client.stop()

    return asyncio.run(wrapper())


def _debug_files(tmp_path):
    d = tmp_path / "data" / "debug"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_logged_out_detection(env, tmp_path):
    env.logged_in = False

    async def go(client):
        assert (await client.status())["running"] is False  # status() never launches
        assert await client.is_logged_in() is False
        with pytest.raises(NotLoggedInError):
            await client.send_snap(_image(tmp_path, (200, 30, 30)), ["Alice Smith"])
        st = await client.status()
        assert st["running"] and st["logged_in"] is False and "logged out" in st["last_error"]

    _run(go)
    files = _debug_files(tmp_path)
    assert any(f.endswith(".png") for f in files) and any(f.endswith(".html") for f in files)
    assert env.sent == []


def test_logged_in_and_list_friends(env):
    async def go(client):
        assert await client.is_logged_in() is True
        friends = await client.list_friends()
        st = await client.status()
        assert st["logged_in"] is True and st["url"] == env.url
        png = await client.screenshot()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        return friends

    friends = _run(go)
    assert "My AI" not in friends
    assert friends == [f for f in FRIENDS if f != "My AI"]  # all of them (scrolled), de-duplicated, ordered


def test_send_to_two_recipients_uses_fake_camera(env, tmp_path):
    async def go(client):
        first = await client.send_snap(_image(tmp_path, (210, 40, 40)), ["Alice Smith", "Bob Jones", "Alice Smith"])
        # a second send must pick up the new image (camera re-opened from the replaced feed file)
        second = await client.send_snap(_image(tmp_path, (40, 40, 210)), ["Carol O'Neil"])
        return first, second, await client.status()

    first, second, st = _run(go)
    assert list(first) == ["Alice Smith", "Bob Jones"]
    assert all(r.ok and r.error is None for r in first.values())
    assert second["Carol O'Neil"].ok
    assert st["last_error"] is None
    assert len(env.sent) == 2
    one, two = env.sent
    assert sorted(one["recipients"]) == ["Alice Smith", "Bob Jones"]  # exact match: not "Alice Smithson"
    # The snap is the 9:16 lens output (like Snapchat Web), and after calibration the image fills it.
    assert (one["width"], one["height"]) == (406, 720)
    assert one["edge"][0] > 150 and one["edge"][1] < 90, one
    r, g, b = one["center"]
    assert r > 170 and g < 90 and b < 90
    assert two["recipients"] == ["Carol O'Neil"]
    r, g, b = two["center"]
    assert b > 170 and r < 90


def test_unknown_recipient_fails_others_still_send(env, tmp_path):
    async def go(client):
        return await client.send_snap(_image(tmp_path, (200, 30, 30)), ["Nobody Here", "Bob Jones"])

    res = _run(go)
    assert res["Bob Jones"].ok
    assert not res["Nobody Here"].ok and "Nobody Here" in res["Nobody Here"].error
    assert env.sent[0]["recipients"] == ["Bob Jones"]
    assert any("missing_recipient" in f for f in _debug_files(tmp_path))


def test_all_unknown_recipients_saves_debug(env, tmp_path):
    async def go(client):
        res = await client.send_snap(_image(tmp_path, (200, 30, 30)), ["Ghost"])
        return res, await client.status()

    res, st = _run(go)
    assert not res["Ghost"].ok
    assert st["last_error"] and "Ghost" in st["last_error"]
    files = _debug_files(tmp_path)
    assert any(f.endswith("send_no_recipients.png") for f in files)
    assert any(f.endswith("send_no_recipients.html") for f in files)
    assert env.sent == []


def test_broken_selector_reports_step_and_override_fixes_it(env, tmp_path):
    sel = tmp_path / "data" / "selectors.json"
    sel.parent.mkdir(parents=True, exist_ok=True)
    sel.write_text(json.dumps({"shutter": ["#does-not-exist"]}))

    async def go(client):
        bad = await client.send_snap(_image(tmp_path, (200, 30, 30)), ["Bob Jones"])
        sel.write_text(json.dumps({"shutter": ["#nope", "#shutter"]}))
        os.utime(sel, (1, 1))
        good = await client.send_snap(_image(tmp_path, (200, 30, 30)), ["Bob Jones"])
        return bad, good

    bad, good = _run(go)
    # With no shutter selector the client clicks the pane by position, which misses in this
    # fixture, so the failure surfaces at the next step.
    assert not bad["Bob Jones"].ok and "send_to" in bad["Bob Jones"].error
    assert any("send_prepare" in f for f in _debug_files(tmp_path))
    assert good["Bob Jones"].ok


def test_restarts_after_browser_closed(env):
    async def go(client):
        assert await client.is_logged_in()
        await client._context.close()  # simulate a crash / user closing the window
        assert (await client.status())["running"] is False
        return await client.is_logged_in()

    assert _run(go) is True


def test_calls_from_another_event_loop_are_dispatched_to_owner(env):
    """The scheduler/dashboard may call from a different loop/thread; the browser stays on its own loop."""
    client = SnapchatClient()
    owner = asyncio.new_event_loop()
    t = threading.Thread(target=owner.run_forever, daemon=True)
    t.start()
    try:
        assert asyncio.run_coroutine_threadsafe(client.start(), owner).result(60) is None
        # called from a brand-new loop in this thread
        assert asyncio.run(client.is_logged_in()) is True
        assert asyncio.run(client.status())["running"] is True
    finally:
        asyncio.run_coroutine_threadsafe(client.stop(), owner).result(60)
        owner.call_soon_threadsafe(owner.stop)
        t.join(10)
        owner.close()


def test_first_send_calibrates_camera_and_fills_pane(env, tmp_path):
    import json as _json

    async def go(client):
        return await client.send_snap(_image(tmp_path, (20, 200, 60)), ["Bob Jones"])

    res = _run(go)
    assert res["Bob Jones"].ok
    cal = _json.loads((tmp_path / "data" / "camera" / "calibration.json").read_text())
    x, y, w, h = cal["region"]
    assert w > 80 and h > 80 and 0 <= x < 1920 and 0 <= y < 1080
    sent = env.sent[-1]
    # The fixture's pane is a centred portrait crop of the frame, so that is what must be measured,
    # and the image must land in the middle of the frame.
    assert abs((x + w / 2) - 960) < 20 and abs(w / h - 9 / 16) < 0.05, cal
    assert sent["center"][1] > 150 and sent["center"][0] < 90


def test_camera_box_is_the_clipped_webcam_pane_not_other_videos(env):
    """A playing non-camera video (Spotlight) must not count as the camera, and the box is the
    visible pane, not the oversized <video> element inside it."""
    async def go(client):
        page = await client._get_page()
        await client._goto_web(page)
        await client._dismiss_popups(page)
        await page.wait_for_function(
            "() => { const p = document.getElementById('promo'); return p.readyState >= 2 && !p.paused; }", timeout=10000)
        before = await client._video_playing(page)
        await client._open_camera(page)
        box = await client._camera_box(page)
        pane = await page.evaluate(
            "() => { const r = document.getElementById('pane').getBoundingClientRect();"
            " return {x: r.x, y: r.y, width: r.width, height: r.height}; }")
        return before, box, pane

    before, box, pane = _run(go)
    assert before is False  # the promo video alone is not a live camera
    assert box is not None
    for k in ("x", "y", "width", "height"):
        assert abs(box[k] - pane[k]) <= 1, (box, pane)
