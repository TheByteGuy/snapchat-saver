"""Smoke test of the dashboard against the REAL config/db/scheduler/pipeline
modules (Snapchat client and fact fetching are stubbed; no browser, no network)."""
from __future__ import annotations

import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))
from test_dash_fakes import csrf_headers, login  # noqa: E402

PASSWORD = "smoke-test-pw"


@pytest.fixture
def real_app(monkeypatch, tmp_path):
    try:
        import app.config as config
        import app.db  # noqa: F401
        import app.pipeline  # noqa: F401
        import app.scheduler  # noqa: F401
        import app.notify  # noqa: F401
        import app.snapchat as snapchat
        import app.facts as facts
    except ImportError as exc:  # other agents' modules not present
        pytest.skip(f"real modules unavailable: {exc}")
    for name in ("app.config", "app.db", "app.pipeline", "app.scheduler", "app.notify", "app.snapchat"):
        assert not type(sys.modules[name]).__name__.startswith("Fake")

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("SNAP_HEADLESS", "1")
    monkeypatch.setenv("NOVNC_DIR", str(tmp_path / "no-novnc"))
    monkeypatch.delenv("SECRET_KEY", raising=False)
    config.reload_settings()

    async def fake_fact():
        return "Octopuses have three hearts."

    monkeypatch.setattr(facts, "get_fact", fake_fact)

    sent = []

    class StubClient:
        async def status(self):
            return {"running": False, "logged_in": None, "url": None, "last_error": None}

        async def send_snap(self, image_path, recipients):
            sent.append((image_path, list(recipients)))
            return {n: snapchat.SendResult(ok=True, error=None) for n in recipients}

        async def stop(self):
            pass

    stub = StubClient()
    monkeypatch.setattr(snapchat, "get_client", lambda: stub)

    from app.main import app
    from app.web import auth, routes

    auth.limiter.reset()
    routes.send_job.reset()
    yield app, sent
    config.reload_settings()


def test_real_modules_end_to_end(real_app):
    app, sent = real_app
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        assert c.get("/api/status").status_code == 401
        login(c, PASSWORD)
        c.headers.update(csrf_headers(c))

        st = c.get("/api/status").json()
        assert st["sent_today"] is False
        assert st["next_run"]  # real scheduler scheduled the daily job

        r = c.put("/api/settings", json={"send_time": "07:15", "timezone": "Europe/Berlin", "enabled": True})
        assert r.status_code == 200, r.text
        assert r.json()["settings"]["timezone"] == "Europe/Berlin"
        assert "T07:15:00" in r.json()["next_run"]

        r = c.post("/api/recipients", json={"names": ["Alice", "Bob"]})
        assert [x["name"] for x in r.json()["recipients"]] == ["Alice", "Bob"]
        bob = r.json()["recipients"][1]
        assert c.patch(f"/api/recipients/{bob['id']}", json={"enabled": False}).status_code == 200

        pv = c.post("/api/preview").json()
        assert pv["fact"] == "Octopuses have three hearts."
        img = c.get(pv["image_url"])
        assert img.status_code == 200 and img.content.startswith(b"\x89PNG")

        assert c.post("/api/send-test", json={}).status_code == 202
        deadline = time.time() + 30
        while time.time() < deadline:
            job = c.get("/api/send-test/status").json()
            if not job["running"]:
                break
            time.sleep(0.05)
        assert job["state"] == "done", job
        assert job["result"]["status"] == "success"
        assert sent and sent[0][1] == ["Alice"]

        hist = c.get("/api/history").json()["history"]
        assert hist[0]["test"] is True and hist[0]["status"] == "success"
        assert c.get(hist[0]["image_url"]).status_code == 200

        assert "Security warning" not in c.get("/").text
