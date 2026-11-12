"""WebSocket <-> TCP VNC bridge tests against a local asyncio echo server."""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, os.path.dirname(__file__))
from test_dash_fakes import install_fakes, login  # noqa: E402


class EchoServer:
    """asyncio TCP server in its own thread; sends a greeting, then echoes."""

    def __init__(self, greeting: bytes = b"RFB 003.008\n"):
        self.greeting = greeting
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.closed_conns = 0
        self.port = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    async def _handle(self, reader, writer):
        writer.write(self.greeting)
        await writer.drain()
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                if data == b"QUIT":
                    break
                writer.write(data)
                await writer.drain()
        finally:
            self.closed_conns += 1
            writer.close()

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def main():
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            self.ready.set()

        self.loop.run_until_complete(main())
        self.loop.run_forever()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5)
        return self

    def __exit__(self, *exc):
        def stop():
            self.server.close()
            self.loop.stop()

        self.loop.call_soon_threadsafe(stop)
        self.thread.join(5)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def echo():
    with EchoServer() as srv:
        yield srv


def _client(monkeypatch, tmp_path, port):
    install_fakes(monkeypatch, tmp_path, VNC_PORT=port)
    from app.main import app
    from app.web import auth

    auth.limiter.reset()
    return TestClient(app)


@pytest.mark.parametrize("path", ["/websockify", "/novnc/websockify"])
def test_bridge_roundtrip(monkeypatch, tmp_path, echo, path):
    with _client(monkeypatch, tmp_path, echo.port) as c:
        login(c)
        with c.websocket_connect(path, subprotocols=["binary"]) as ws:
            assert ws.accepted_subprotocol == "binary"
            assert ws.receive_bytes() == b"RFB 003.008\n"
            ws.send_bytes(b"\x00\x01hello")
            assert ws.receive_bytes() == b"\x00\x01hello"
            big = bytes(range(256)) * 1000
            ws.send_bytes(big)
            got = b""
            while len(got) < len(big):
                got += ws.receive_bytes()
            assert got == big


def test_bridge_closes_when_tcp_closes(monkeypatch, tmp_path, echo):
    with _client(monkeypatch, tmp_path, echo.port) as c:
        login(c)
        with c.websocket_connect("/websockify") as ws:
            assert ws.receive_bytes().startswith(b"RFB")
            ws.send_bytes(b"QUIT")
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
            assert exc.value.code == 1000


def test_bridge_closes_tcp_when_ws_closes(monkeypatch, tmp_path, echo):
    with _client(monkeypatch, tmp_path, echo.port) as c:
        login(c)
        with c.websocket_connect("/websockify") as ws:
            ws.receive_bytes()
        deadline = __import__("time").time() + 5
        while echo.closed_conns < 1 and __import__("time").time() < deadline:
            __import__("time").sleep(0.02)
        assert echo.closed_conns == 1


def test_bridge_rejects_without_auth(monkeypatch, tmp_path, echo):
    with _client(monkeypatch, tmp_path, echo.port) as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect("/websockify", subprotocols=["binary"]):
                pass
        assert exc.value.code == 1008


def test_bridge_rejects_forged_cookie(monkeypatch, tmp_path, echo):
    with _client(monkeypatch, tmp_path, echo.port) as c:
        c.cookies.set("snapsaver_session", "forged.value.here")
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/websockify"):
                pass


def test_bridge_rejects_cross_origin(monkeypatch, tmp_path, echo):
    with _client(monkeypatch, tmp_path, echo.port) as c:
        login(c)
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/websockify", headers={"Origin": "https://evil.example"}):
                pass


def test_bridge_vnc_down(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path, _free_port()) as c:
        login(c)
        with c.websocket_connect("/websockify") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
            assert exc.value.code == 1011
