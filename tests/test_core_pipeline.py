import sys
import types
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app import config, db, facts, imagegen, notify, pipeline


@dataclass
class FakeSendResult:
    ok: bool
    error: str | None = None


class FakeNotLoggedInError(Exception):
    pass


class FakeClient:
    def __init__(self):
        self.behaviour = "ok"  # ok | partial | fail | logout | crash
        self.calls = []

    async def send_snap(self, image_path, recipients):
        self.calls.append((Path(image_path), list(recipients)))
        if self.behaviour == "logout":
            raise FakeNotLoggedInError("login page shown")
        if self.behaviour == "crash":
            raise RuntimeError("browser exploded")
        out = {}
        for i, name in enumerate(recipients):
            if self.behaviour == "ok" or (self.behaviour == "partial" and i == 0):
                out[name] = FakeSendResult(True)
            else:
                out[name] = FakeSendResult(False, "recipient not found")
        return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.reload_settings()
    db.init_db()

    client = FakeClient()
    mod = types.ModuleType("app.snapchat")
    mod.get_client = lambda: client
    mod.NotLoggedInError = FakeNotLoggedInError
    mod.SendResult = FakeSendResult
    monkeypatch.setitem(sys.modules, "app.snapchat", mod)

    fact_counter = iter(range(1000))

    async def fake_fact():
        return f"Fact number {next(fact_counter)} is true."

    monkeypatch.setattr(facts, "get_fact", fake_fact)

    def fake_render(fact, out_path, seed=None, date_text=None):
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x89PNG fake")
        return out_path

    monkeypatch.setattr(imagegen, "render_fact_image", fake_render)

    sent = []

    async def fake_notify(title, message, ok, image_path=None):
        sent.append({"title": title, "message": message, "ok": ok, "image_path": image_path})
        return True

    monkeypatch.setattr(notify, "notify_discord", fake_notify)
    yield types.SimpleNamespace(client=client, notes=sent, tmp=tmp_path)
    config.reload_settings()


async def test_success(env):
    db.add_recipient("Alice")
    db.add_recipient("Bob")
    off = db.add_recipient("Carol")
    db.set_recipient_enabled(off["id"], False)
    res = await pipeline.run_send()
    assert res["status"] == "success"
    assert res["results"] == {"Alice": {"ok": True, "error": None}, "Bob": {"ok": True, "error": None}}
    assert env.client.calls[0][1] == ["Alice", "Bob"]
    today = pipeline.today_str()
    assert Path(res["image_path"]).name == f"{today}.png"
    assert Path(res["image_path"]).parent == config.get_settings().IMAGES_DIR
    assert db.fact_used(res["fact"])
    assert db.sent_today(today)
    h = db.list_history()[0]
    assert h["status"] == "success" and h["test"] is False and h["fact"] == res["fact"]
    assert len(env.notes) == 1 and env.notes[0]["ok"] is True and env.notes[0]["image_path"]


async def test_success_notification_can_be_disabled(env):
    db.set_setting("notify_on_success", "0")
    db.add_recipient("Alice")
    res = await pipeline.run_send()
    assert res["status"] == "success"
    assert env.notes == []


async def test_partial(env):
    db.add_recipient("Alice")
    db.add_recipient("Bob")
    env.client.behaviour = "partial"
    res = await pipeline.run_send()
    assert res["status"] == "partial"
    assert res["results"]["Bob"] == {"ok": False, "error": "recipient not found"}
    assert db.fact_used(res["fact"])
    assert db.sent_today(pipeline.today_str())
    assert env.notes[0]["ok"] is False and "Bob" in env.notes[0]["message"]


async def test_all_fail(env):
    db.add_recipient("Alice")
    env.client.behaviour = "fail"
    res = await pipeline.run_send()
    assert res["status"] == "failed"
    assert not db.fact_used(res["fact"])
    assert not db.sent_today(pipeline.today_str())
    assert db.list_history()[0]["status"] == "failed"
    assert env.notes[0]["ok"] is False


async def test_not_logged_in(env):
    db.add_recipient("Alice")
    env.client.behaviour = "logout"
    res = await pipeline.run_send()
    assert res["status"] == "failed"
    assert "log in" in res["error"].lower()
    assert "dashboard" in env.notes[0]["message"].lower()
    assert "log in" in env.notes[0]["message"].lower()


async def test_exception(env):
    db.add_recipient("Alice")
    env.client.behaviour = "crash"
    res = await pipeline.run_send()
    assert res["status"] == "failed" and "browser exploded" in res["error"]
    assert db.list_history()[0]["status"] == "failed"


async def test_no_recipients(env):
    res = await pipeline.run_send()
    assert res["status"] == "failed"
    assert "recipient" in res["error"].lower()
    assert env.client.calls == []
    assert env.notes and env.notes[0]["ok"] is False


async def test_test_send_explicit_recipients(env):
    db.add_recipient("Alice")
    res = await pipeline.run_send(test=True, recipients=["Zed"])
    assert res["status"] == "success"
    assert env.client.calls[0][1] == ["Zed"]
    assert Path(res["image_path"]).name.startswith("test-")
    assert not db.fact_used(res["fact"])  # test sends don't consume facts
    assert not db.sent_today(pipeline.today_str())
    assert db.list_history()[0]["test"] is True
    assert env.notes[0]["title"].startswith("[TEST]")


async def test_dict_results_and_missing_names(env, monkeypatch):
    async def send(image_path, recipients):
        return {"Alice": {"ok": True, "error": None}}

    monkeypatch.setattr(env.client, "send_snap", send)
    res = await pipeline.run_send(recipients=["Alice", "Bob"])
    assert res["status"] == "partial"
    assert res["results"]["Bob"]["ok"] is False and res["results"]["Bob"]["error"]


async def test_concurrent_run_rejected(env):
    import asyncio

    db.add_recipient("Alice")
    gate = asyncio.Event()

    async def slow(image_path, recipients):
        await gate.wait()
        return {n: FakeSendResult(True) for n in recipients}

    env.client.send_snap = slow
    first = asyncio.create_task(pipeline.run_send())
    await asyncio.sleep(0.05)
    assert pipeline.is_busy()
    second = await pipeline.run_send()
    assert second["status"] == "failed" and "in progress" in second["error"]
    gate.set()
    assert (await first)["status"] == "success"
    assert len(db.list_history()) == 1


async def test_generate_preview(env):
    res = await pipeline.generate_preview()
    assert Path(res["image_path"]).name == "preview.png"
    assert Path(res["image_path"]).exists()
    assert res["fact"].startswith("Fact number")
    assert db.list_history() == []


async def test_timezone_setting_used(env):
    db.set_setting("timezone", "Pacific/Kiritimati")  # UTC+14
    assert str(pipeline.get_timezone()) == "Pacific/Kiritimati"
    db.set_setting("timezone", "Not/AZone")
    assert str(pipeline.get_timezone()) == "UTC"


# ---------------------------------------------------------------- notify (real implementation)
@pytest.fixture
def plain_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.reload_settings()
    db.init_db()
    yield tmp_path
    config.reload_settings()


def _mock_httpx(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(notify.httpx, "AsyncClient", factory)


async def test_notify_noop_without_webhook(plain_env, monkeypatch):
    called = []
    _mock_httpx(monkeypatch, lambda req: called.append(req) or httpx.Response(204))
    assert await notify.notify_discord("t", "m", True) is False
    assert called == []


async def test_notify_json_and_multipart(plain_env, monkeypatch):
    db.set_setting("discord_webhook_url", "https://discord.example/api/webhooks/1/abc")
    reqs = []

    def handler(req):
        reqs.append(req)
        return httpx.Response(204)

    _mock_httpx(monkeypatch, handler)
    assert await notify.notify_discord("Hello", "World", False) is True
    import json
    body = json.loads(reqs[0].content)
    assert body["embeds"][0]["color"] == notify.COLOR_FAIL
    assert body["embeds"][0]["title"] == "Hello"

    img = plain_env / "x.png"
    img.write_bytes(b"\x89PNGdata")
    assert await notify.notify_discord("Yay", "ok", True, img) is True
    assert reqs[1].headers["content-type"].startswith("multipart/form-data")
    assert b"payload_json" in reqs[1].content and b"\x89PNGdata" in reqs[1].content
    assert str(notify.COLOR_OK).encode() in reqs[1].content


async def test_notify_never_raises(plain_env, monkeypatch):
    db.set_setting("discord_webhook_url", "https://discord.example/api/webhooks/1/abc")

    def boom(req):
        raise httpx.ConnectError("down")

    _mock_httpx(monkeypatch, boom)
    assert await notify.notify_discord("t", "m", True) is False
    _mock_httpx(monkeypatch, lambda req: httpx.Response(400, text="bad"))
    assert await notify.notify_discord("t", "m", True) is False
