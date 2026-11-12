"""Shared fakes for the dashboard tests (no tests in this module).

``install_fakes(monkeypatch, tmp_path)`` replaces app.config, app.db,
app.scheduler, app.pipeline, app.notify and app.snapchat in sys.modules with
in-memory fakes, so the dashboard can be tested without the other agents'
modules, a browser or the network.
"""
from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

PASSWORD = "s3cret-pass"


@dataclass
class FakeSendResult:
    ok: bool
    error: str | None = None


class FakeDB(types.ModuleType):
    def __init__(self, default_tz: str = "UTC"):
        super().__init__("app.db")
        self.default_tz = default_tz
        self.reset()

    def reset(self):
        self.settings: dict[str, str] = {}
        self.recipients: list[dict] = []
        self.history: list[dict] = []
        self._next_id = 1
        self.init_calls = 0

    def init_db(self):
        self.init_calls += 1

    def get_setting(self, key, default=None):
        defaults = {"send_time": "09:00", "timezone": self.default_tz, "enabled": "1",
                    "discord_webhook_url": "", "notify_on_success": "1"}
        return self.settings.get(key, defaults.get(key, default))

    def set_setting(self, key, value):
        assert isinstance(value, str)
        self.settings[key] = value

    def list_recipients(self):
        return [dict(r) for r in self.recipients]

    def add_recipient(self, name):
        for r in self.recipients:
            if r["name"] == name:
                return dict(r)
        r = {"id": self._next_id, "name": name, "enabled": True}
        self._next_id += 1
        self.recipients.append(r)
        return dict(r)

    def remove_recipient(self, rid):
        self.recipients = [r for r in self.recipients if r["id"] != rid]

    def set_recipient_enabled(self, rid, enabled):
        for r in self.recipients:
            if r["id"] == rid:
                r["enabled"] = bool(enabled)

    def add_history(self, date, fact, image_path, status, results, test=False):
        hid = len(self.history) + 1
        self.history.insert(0, {"id": hid, "date": date, "created_at": datetime.now(timezone.utc).isoformat(),
                                "fact": fact, "image_path": str(image_path), "status": status,
                                "results": results, "test": test})
        return hid

    def list_history(self, limit=30):
        return self.history[:limit]

    def sent_today(self, date):
        return any(h["date"] == date and not h["test"] and h["status"] in ("success", "partial")
                   for h in self.history)


class FakeClient:
    def __init__(self):
        self.logged_in = False
        self.friends = ["Alice", "Bob", "Carol"]
        self.calls: list[str] = []

    async def status(self):
        return {"running": False, "logged_in": None, "url": None, "last_error": None}

    async def is_logged_in(self):
        self.calls.append("is_logged_in")
        return self.logged_in

    async def open_login(self):
        self.calls.append("open_login")

    async def list_friends(self):
        self.calls.append("list_friends")
        return list(self.friends)

    async def screenshot(self):
        self.calls.append("screenshot")
        return b"\x89PNG\r\n\x1a\nfake"

    async def stop(self):
        self.calls.append("stop")


def install_fakes(monkeypatch, tmp_path: Path, **overrides):
    images = tmp_path / "images"
    images.mkdir(parents=True, exist_ok=True)
    novnc = tmp_path / "novnc"
    cfg = SimpleNamespace(
        DATA_DIR=tmp_path, DASHBOARD_PASSWORD=PASSWORD, PORT=8080, SECRET_KEY="test-secret-key",
        DISPLAY=":99", VNC_HOST="127.0.0.1", VNC_PORT=5900, NOVNC_DIR=novnc, HEADLESS=True,
        DEFAULT_TZ="UTC", IMAGES_DIR=images, PROFILE_DIR=tmp_path / "profile",
        CAMERA_DIR=tmp_path / "camera", DEBUG_DIR=tmp_path / "debug", DB_PATH=tmp_path / "app.db",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)

    config = types.ModuleType("app.config")
    config.get_settings = lambda: cfg
    config.reload_settings = lambda: cfg

    db = FakeDB()

    scheduler = types.ModuleType("app.scheduler")
    scheduler.calls = []
    scheduler.start = lambda: scheduler.calls.append("start")
    scheduler.shutdown = lambda: scheduler.calls.append("shutdown")
    scheduler.reschedule = lambda: scheduler.calls.append("reschedule")
    scheduler.next_run = lambda: datetime(2030, 1, 2, 9, 0, tzinfo=timezone.utc)

    client = FakeClient()
    snapchat = types.ModuleType("app.snapchat")
    snapchat.get_client = lambda: client
    snapchat.SendResult = FakeSendResult
    snapchat.NotLoggedInError = type("NotLoggedInError", (Exception,), {})

    pipeline = types.ModuleType("app.pipeline")
    pipeline.gate = None  # optional asyncio.Event-like gate set by tests
    pipeline.calls = []

    async def generate_preview():
        p = images / "preview.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\npreview")
        return {"fact": "Honey never spoils.", "image_path": p}

    async def run_send(test=False, recipients=None):
        pipeline.calls.append({"test": test, "recipients": recipients})
        if pipeline.gate is not None:
            while not pipeline.gate.is_set():
                await asyncio.sleep(0.01)
        if recipients == ["boom"]:
            raise RuntimeError("pipeline exploded")
        names = recipients or [r["name"] for r in db.list_recipients() if r["enabled"]]
        p = images / "2030-01-02.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\nsend")
        results = {n: FakeSendResult(ok=(n != "Bob"), error=None if n != "Bob" else "not found") for n in names}
        status = "success" if all(r.ok for r in results.values()) else "partial"
        db.add_history("2030-01-02", "A fact", str(p), status,
                       {k: {"ok": v.ok, "error": v.error} for k, v in results.items()}, test=test)
        return {"status": status, "fact": "A fact", "image_path": p, "results": results}

    pipeline.generate_preview = generate_preview
    pipeline.run_send = run_send

    notify = types.ModuleType("app.notify")
    notify.calls = []

    async def notify_discord(title, message, ok, image_path=None):
        notify.calls.append((title, message, ok, image_path))
        return bool(db.get_setting("discord_webhook_url"))

    notify.notify_discord = notify_discord

    mods = {"app.config": config, "app.db": db, "app.scheduler": scheduler, "app.snapchat": snapchat,
            "app.pipeline": pipeline, "app.notify": notify}
    for name, m in mods.items():
        monkeypatch.setitem(sys.modules, name, m)
    return SimpleNamespace(cfg=cfg, db=db, scheduler=scheduler, client=client, pipeline=pipeline,
                           notify=notify, images=images, novnc=novnc)


def login(client, password: str = PASSWORD):
    r = client.post("/login", data={"password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def csrf_headers(client) -> dict:
    """Read the CSRF token from the rendered dashboard page."""
    import re

    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "csrf meta tag missing"
    return {"X-CSRF-Token": m.group(1)}
