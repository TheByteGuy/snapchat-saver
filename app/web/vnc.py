"""noVNC static files and a WebSocket <-> TCP bridge to the local VNC server.

Both are served on the dashboard port and require the dashboard session, so the
VNC server itself never has to be exposed.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, WebSocket
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect, WebSocketState

from app.web.auth import COOKIE_NAME, origin_ok, read_session, session_from_scope
from app.web.deps import settings

log = logging.getLogger(__name__)

router = APIRouter()

CONNECT_TIMEOUT = 5.0
READ_CHUNK = 64 * 1024


def novnc_dir() -> Path:
    return Path(str(settings().NOVNC_DIR))


def novnc_page() -> str | None:
    """Which noVNC viewer page exists ("vnc.html" preferred), or None."""
    d = novnc_dir()
    for name in ("vnc.html", "vnc_lite.html"):
        if (d / name).is_file():
            return name
    return None


_MISSING_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Browser view unavailable</title>
<style>body{font:15px/1.5 system-ui,sans-serif;background:#111318;color:#e6e8ee;margin:0;display:grid;
place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}div{max-width:460px}
code{background:#22262f;padding:2px 5px;border-radius:4px}</style></head><body><div>
<h1>Browser view unavailable</h1><p>The noVNC files were not found in <code>{dir}</code>.
The Docker image ships them at <code>/usr/share/novnc</code>; outside Docker install the
<code>novnc</code> package or set <code>NOVNC_DIR</code>.</p></div></body></html>"""


class NoVNCStatic:
    """ASGI app mounted at /novnc: auth check, then StaticFiles from NOVNC_DIR."""

    def __init__(self) -> None:
        self._static: StaticFiles | None = None
        self._static_dir: Path | None = None

    def _get_static(self, d: Path) -> StaticFiles:
        if self._static is None or self._static_dir != d:
            self._static = StaticFiles(directory=str(d), html=True)
            self._static_dir = d
        return self._static

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        if session_from_scope(scope) is None:
            resp = RedirectResponse("/login", status_code=303)
            await resp(scope, receive, send)
            return
        d = novnc_dir()
        if not d.is_dir():
            from html import escape

            resp = HTMLResponse(_MISSING_HTML.replace("{dir}", escape(str(d))), status_code=404)
            await resp(scope, receive, send)
            return
        try:
            await self._get_static(d)(scope, receive, send)
        except RuntimeError:
            await PlainTextResponse("Not found", status_code=404)(scope, receive, send)


async def _pump_ws_to_tcp(ws: WebSocket, writer: asyncio.StreamWriter) -> None:
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            return
        data = msg.get("bytes")
        if data is None and msg.get("text") is not None:
            data = msg["text"].encode("utf-8")
        if data:
            writer.write(data)
            await writer.drain()


async def _pump_tcp_to_ws(ws: WebSocket, reader: asyncio.StreamReader) -> None:
    while True:
        data = await reader.read(READ_CHUNK)
        if not data:
            return
        await ws.send_bytes(data)


async def vnc_bridge(websocket: WebSocket) -> None:
    sess = read_session(websocket.cookies.get(COOKIE_NAME))
    if sess is None or not origin_ok(websocket.headers):
        # Closing before accept makes Starlette answer the handshake with 403.
        await websocket.close(code=1008)
        return

    requested = websocket.scope.get("subprotocols") or []
    subprotocol = "binary" if "binary" in requested else None

    cfg = settings()
    host, port = str(cfg.VNC_HOST), int(cfg.VNC_PORT)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
    except Exception as exc:  # VNC server down
        log.warning("VNC connection to %s:%s failed: %s", host, port, exc)
        await websocket.accept(subprotocol=subprotocol)
        await websocket.close(code=1011, reason="VNC server unavailable")
        return

    await websocket.accept(subprotocol=subprotocol)
    tasks = [
        asyncio.create_task(_pump_ws_to_tcp(websocket, writer)),
        asyncio.create_task(_pump_tcp_to_ws(websocket, reader)),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionError)):
                log.debug("VNC bridge ended with %r", exc)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2)
        except Exception:
            pass
        if websocket.application_state == WebSocketState.CONNECTED and \
                websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close(code=1000)
            except Exception:
                pass


# noVNC's vnc.html resolves a relative "websockify" path against its own URL
# (/novnc/websockify) in newer releases and against the site root in older ones,
# so the bridge answers on both.
router.add_api_websocket_route("/websockify", vnc_bridge)
router.add_api_websocket_route("/novnc/websockify", vnc_bridge)
