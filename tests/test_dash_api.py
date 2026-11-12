"""Dashboard auth + JSON API tests (all other app modules are faked)."""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))
from test_dash_fakes import PASSWORD, csrf_headers, install_fakes, login  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    fakes = install_fakes(monkeypatch, tmp_path)
    from app.web import auth, routes

    auth.limiter.reset()
    routes.send_job.reset()
    return fakes


@pytest.fixture
def client(env):
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed(client):
    login(client)
    client.headers.update(csrf_headers(client))
    return client


# ------------------------------------------------------------------ auth


def test_healthz_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_lifespan_calls_db_and_scheduler(env, client):
    assert env.db.init_calls == 1
    assert "start" in env.scheduler.calls


def test_login_page_public(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert 'name="password"' in r.text


PROTECTED_GET = ["/", "/api/status", "/api/recipients", "/api/friends", "/api/settings",
                 "/api/timezones", "/api/history", "/api/send-test/status", "/api/snapchat/screenshot",
                 "/images/preview.png", "/novnc/vnc.html"]
PROTECTED_POST = ["/api/recipients", "/api/snapchat/open-login", "/api/snapchat/check-login",
                  "/api/preview", "/api/send-test", "/api/notify-test"]


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_get_requires_auth(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code in (401, 303), (path, r.status_code)
    if r.status_code == 303:
        assert r.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", PROTECTED_POST)
def test_post_requires_auth(env, client, path):
    r = client.post(path, json={}, follow_redirects=False)
    assert r.status_code == 401
    assert env.client.calls == []
    assert env.pipeline.calls == []


def test_mutations_without_auth(client):
    assert client.put("/api/settings", json={"send_time": "10:00"}).status_code == 401
    assert client.delete("/api/recipients/1").status_code == 401
    assert client.patch("/api/recipients/1", json={"enabled": False}).status_code == 401


def test_index_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


def test_login_success_sets_cookie(client):
    r = login(client)
    cookie = r.headers["set-cookie"]
    assert "snapsaver_session=" in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "Max-Age=2592000" in cookie
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 200


def test_login_wrong_password(client):
    r = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 401
    assert "Wrong password" in r.text
    assert "snapsaver_session" not in r.headers.get("set-cookie", "")
    assert client.get("/api/status").status_code == 401


def test_login_empty_password(client):
    r = client.post("/login", data={"password": ""}, follow_redirects=False)
    assert r.status_code == 401


def test_login_rate_limited(client):
    for _ in range(5):
        assert client.post("/login", data={"password": "bad"}).status_code == 401
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 429
    assert "Too many" in r.text
    assert int(r.headers["retry-after"]) > 0


def test_login_next_param_is_safe(client):
    r = client.post("/login", data={"password": PASSWORD, "next": "//evil.example/x"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    client.cookies.clear()
    r = client.post("/login", data={"password": PASSWORD, "next": "/api/status"}, follow_redirects=False)
    assert r.headers["location"] == "/api/status"


def test_tampered_cookie_rejected(client):
    login(client)
    value = client.cookies.get("snapsaver_session")
    client.cookies.set("snapsaver_session", value[:-2] + ("aa" if not value.endswith("aa") else "bb"))
    assert client.get("/api/status").status_code == 401


def test_password_change_invalidates_session(env, client):
    login(client)
    assert client.get("/api/status").status_code == 200
    env.cfg.DASHBOARD_PASSWORD = "another-password"
    assert client.get("/api/status").status_code == 401


def test_logout(authed):
    token = authed.headers["X-CSRF-Token"]
    r = authed.post("/logout", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert authed.get("/api/status").status_code == 401


def test_logout_requires_csrf(client):
    login(client)
    r = client.post("/logout", data={"csrf": "wrong"}, follow_redirects=False)
    assert r.status_code == 403
    assert client.get("/api/status").status_code == 200


def test_default_password_banner(monkeypatch, tmp_path):
    install_fakes(monkeypatch, tmp_path, DASHBOARD_PASSWORD="changeme")
    from app.main import app
    from app.web import auth

    auth.limiter.reset()
    with TestClient(app) as c:
        assert "still the default" in c.get("/login").text
        login(c, "changeme")
        page = c.get("/").text
        assert "Security warning" in page
        assert c.get("/api/status").json()["default_password"] is True


def test_no_banner_with_custom_password(authed):
    assert "Security warning" not in authed.get("/").text


# ------------------------------------------------------------------ CSRF


def test_csrf_required_on_api_post(client):
    login(client)
    r = client.post("/api/recipients", json={"name": "Alice"})
    assert r.status_code == 403
    r = client.post("/api/recipients", json={"name": "Alice"}, headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403


def test_cross_origin_rejected(authed):
    r = authed.post("/api/recipients", json={"name": "Alice"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = authed.post("/api/recipients", json={"name": "Alice"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_cross_origin_login_rejected(client):
    r = client.post("/login", data={"password": PASSWORD}, headers={"Origin": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 403


# ------------------------------------------------------------------ status


def test_status(env, authed):
    s = authed.get("/api/status").json()
    assert s["snapchat"] == {"running": False, "logged_in": None, "url": None, "last_error": None}
    assert s["next_run"].startswith("2030-01-02T09:00")
    assert s["sent_today"] is False
    assert s["settings"]["send_time"] == "09:00"
    assert s["settings"]["enabled"] is True
    assert s["send_test"]["state"] == "idle"


def test_status_survives_snapchat_error(env, authed):
    async def broken():
        raise RuntimeError("no browser")

    env.client.status = broken
    s = authed.get("/api/status").json()
    assert s["snapchat"]["logged_in"] is None
    assert "no browser" in s["snapchat"]["last_error"]


def test_sent_today_uses_configured_timezone(env, authed):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    env.db.settings["timezone"] = "Pacific/Kiritimati"
    today = datetime.now(ZoneInfo("Pacific/Kiritimati")).date().isoformat()
    env.db.add_history(today, "f", "x.png", "success", {}, test=False)
    s = authed.get("/api/status").json()
    assert s["today"] == today and s["sent_today"] is True


# ------------------------------------------------------------------ recipients


def test_recipients_crud(env, authed):
    assert authed.get("/api/recipients").json() == {"recipients": []}
    r = authed.post("/api/recipients", json={"name": "  Alice   Smith "})
    assert r.status_code == 200
    alice = r.json()["added"][0]
    assert alice["name"] == "Alice Smith" and alice["enabled"] is True
    r = authed.post("/api/recipients", json={"names": ["Bob", "Carol"]})
    assert [x["name"] for x in r.json()["recipients"]] == ["Alice Smith", "Bob", "Carol"]

    r = authed.patch(f"/api/recipients/{alice['id']}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["recipient"]["enabled"] is False

    r = authed.delete(f"/api/recipients/{alice['id']}")
    assert r.status_code == 200
    assert [x["name"] for x in authed.get("/api/recipients").json()["recipients"]] == ["Bob", "Carol"]


def test_recipients_validation(authed):
    assert authed.post("/api/recipients", json={"name": "   "}).status_code == 422
    assert authed.post("/api/recipients", json={"name": "x" * 101}).status_code == 422
    assert authed.post("/api/recipients", json={"name": 5}).status_code == 422
    assert authed.post("/api/recipients", json={"names": []}).status_code == 422
    assert authed.delete("/api/recipients/999").status_code == 404
    r = authed.post("/api/recipients", json={"name": "Al"})
    rid = r.json()["added"][0]["id"]
    r = authed.patch(f"/api/recipients/{rid}", json={"enabled": "yes"})
    assert r.status_code == 422
    assert "error" in r.json()


def test_friends(env, authed):
    authed.post("/api/recipients", json={"name": "Bob"})
    r = authed.get("/api/friends")
    assert r.json()["friends"] == [{"name": "Alice", "added": False}, {"name": "Bob", "added": True},
                                   {"name": "Carol", "added": False}]


def test_friends_error(env, authed):
    async def broken():
        raise RuntimeError("not logged in")

    env.client.list_friends = broken
    r = authed.get("/api/friends")
    assert r.status_code == 502 and "not logged in" in r.json()["error"]


# ------------------------------------------------------------------ snapchat actions


def test_snapchat_actions(env, authed):
    assert authed.post("/api/snapchat/open-login").json()["ok"] is True
    r = authed.post("/api/snapchat/check-login").json()
    assert r["logged_in"] is False
    env.client.logged_in = True
    assert authed.post("/api/snapchat/check-login").json()["logged_in"] is True
    r = authed.get("/api/snapchat/screenshot")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    assert env.client.calls == ["open_login", "is_logged_in", "is_logged_in", "screenshot"]


# ------------------------------------------------------------------ settings


def test_settings_get_put(env, authed):
    assert authed.get("/api/settings").json()["settings"] == {
        "send_time": "09:00", "timezone": "UTC", "enabled": True,
        "discord_webhook_url": "", "notify_on_success": True}
    hook = "https://discord.com/api/webhooks/123/abc"
    r = authed.put("/api/settings", json={"send_time": "18:45", "timezone": "Europe/Berlin", "enabled": False,
                                          "discord_webhook_url": hook, "notify_on_success": False})
    assert r.status_code == 200, r.text
    assert r.json()["settings"] == {"send_time": "18:45", "timezone": "Europe/Berlin", "enabled": False,
                                    "discord_webhook_url": hook, "notify_on_success": False}
    assert env.db.settings["enabled"] == "0" and env.db.settings["send_time"] == "18:45"
    assert env.scheduler.calls.count("reschedule") == 1


@pytest.mark.parametrize("payload", [
    {"send_time": "24:00"}, {"send_time": "9:00"}, {"send_time": "09:60"}, {"send_time": 900},
    {"timezone": "Mars/Olympus"}, {"timezone": ""}, {"timezone": "../../etc/passwd"},
    {"enabled": "yes"}, {"notify_on_success": 1},
    {"discord_webhook_url": "http://discord.com/api/webhooks/1/a"},
    {"discord_webhook_url": "https://evil.com/api/webhooks/1/a"},
    {"discord_webhook_url": "https://discord.com.evil.com/api/webhooks/1/a"},
    {"discord_webhook_url": "https://discord.com/other/1"},
    {"discord_webhook_url": "https://user@discord.com/api/webhooks/1/a"},
    {"bogus": 1},
])
def test_settings_validation(env, authed, payload):
    r = authed.put("/api/settings", json=payload)
    assert r.status_code == 422, payload
    assert r.json()["ok"] is False and r.json()["errors"]
    assert env.db.settings == {}
    assert "reschedule" not in env.scheduler.calls


def test_settings_accepts_valid_webhooks(env, authed):
    for url in ["", "https://discordapp.com/api/webhooks/1/x", "https://discord.com/api/webhooks/1/x"]:
        r = authed.put("/api/settings", json={"discord_webhook_url": url})
        assert r.status_code == 200, url
    assert "reschedule" not in env.scheduler.calls  # webhook-only changes need no reschedule


def test_timezones(authed):
    zones = authed.get("/api/timezones").json()["timezones"]
    assert "Europe/Berlin" in zones and "UTC" in zones and zones == sorted(zones)


# ------------------------------------------------------------------ preview / notify


def test_preview(authed):
    r = authed.post("/api/preview").json()
    assert r["fact"] == "Honey never spoils."
    assert r["image_url"].startswith("/images/preview.png")
    img = authed.get(r["image_url"])
    assert img.status_code == 200 and img.content.endswith(b"preview")


def test_notify_test(env, authed):
    r = authed.post("/api/notify-test")
    assert r.status_code == 400 and "webhook" in r.json()["error"].lower()
    authed.put("/api/settings", json={"discord_webhook_url": "https://discord.com/api/webhooks/1/x"})
    r = authed.post("/api/notify-test")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert env.notify.calls and env.notify.calls[0][2] is True


# ------------------------------------------------------------------ send test


def _wait_done(c, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = c.get("/api/send-test/status").json()
        if not st["running"]:
            return st
        time.sleep(0.02)
    raise AssertionError("send test did not finish")


def test_send_test_background_flow(env, authed):
    env.pipeline.gate = threading.Event()
    authed.post("/api/recipients", json={"names": ["Alice", "Bob"]})
    r = authed.post("/api/send-test", json={"recipients": ["Alice", "Bob"]})
    assert r.status_code == 202
    assert r.json()["send_test"]["running"] is True
    # still running while the pipeline is blocked; the request did not hang
    st = authed.get("/api/send-test/status").json()
    assert st["state"] == "running" and st["recipients"] == ["Alice", "Bob"]
    # a second start is refused while running
    assert authed.post("/api/send-test", json={}).status_code == 409
    env.pipeline.gate.set()
    st = _wait_done(authed)
    assert st["state"] == "done"
    assert st["result"]["status"] == "partial"
    assert st["result"]["results"] == {"Alice": {"ok": True, "error": None}, "Bob": {"ok": False, "error": "not found"}}
    assert st["result"]["image_url"].startswith("/images/2030-01-02.png")
    assert env.pipeline.calls == [{"test": True, "recipients": ["Alice", "Bob"]}]

    hist = authed.get("/api/history").json()["history"]
    assert hist[0]["test"] is True and hist[0]["status"] == "partial"
    assert hist[0]["image_url"].startswith("/images/2030-01-02.png")


def test_send_test_all_enabled(env, authed):
    r = authed.post("/api/send-test", json={})
    assert r.status_code == 400  # no recipients yet
    authed.post("/api/recipients", json={"name": "Alice"})
    r = authed.post("/api/send-test")
    assert r.status_code == 202
    st = _wait_done(authed)
    assert st["state"] == "done"
    assert env.pipeline.calls == [{"test": True, "recipients": None}]


def test_send_test_error(env, authed):
    r = authed.post("/api/send-test", json={"recipients": ["boom"]})
    assert r.status_code == 202
    st = _wait_done(authed)
    assert st["state"] == "error" and "pipeline exploded" in st["error"]


def test_send_test_validation(authed):
    assert authed.post("/api/send-test", json={"recipients": "Alice"}).status_code == 422
    assert authed.post("/api/send-test", json={"recipients": [""]}).status_code == 422


# ------------------------------------------------------------------ images


def test_image_serving_and_traversal(env, authed):
    (env.images / "ok.png").write_bytes(b"img")
    secret = env.cfg.DATA_DIR / "secret_key"
    secret.write_text("TOPSECRET")
    assert authed.get("/images/ok.png").content == b"img"
    for path in ["/images/../secret_key", "/images/..%2Fsecret_key", "/images/%2e%2e%2fsecret_key",
                 "/images/..%5Csecret_key", "/images/.hidden", "/images/missing.png",
                 "/images/%2Fetc%2Fpasswd", "/images/%2e%2e"]:
        r = authed.get(path)
        assert r.status_code == 404, path
        assert b"TOPSECRET" not in r.content


def test_image_symlink_outside_rejected(env, authed):
    outside = env.cfg.DATA_DIR / "outside.png"
    outside.write_bytes(b"outside")
    (env.images / "link.png").symlink_to(outside)
    assert authed.get("/images/link.png").status_code == 404


def test_image_requires_auth(env, client):
    (env.images / "ok.png").write_bytes(b"img")
    assert client.get("/images/ok.png").status_code == 401


# ------------------------------------------------------------------ noVNC static


def test_novnc_missing_dir_shows_message(env, authed):
    r = authed.get("/novnc/vnc.html")
    assert r.status_code == 404 and "noVNC files were not found" in r.text
    page = authed.get("/").text
    assert '"novnc_page": null' in page


def test_novnc_static_served(env, authed):
    env.novnc.mkdir()
    (env.novnc / "vnc.html").write_text("<html>novnc</html>")
    r = authed.get("/novnc/vnc.html")
    assert r.status_code == 200 and "novnc" in r.text
    assert '"novnc_page": "vnc.html"' in authed.get("/").text
    assert authed.get("/novnc/../app.db").status_code == 404


def test_security_headers(authed):
    r = authed.get("/")
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    assert "script-src 'self'" in r.headers["content-security-policy"]
