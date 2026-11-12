import pytest

from app import config, db


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("SNAP_HEADLESS", raising=False)
    config.reload_settings()
    db.init_db()
    yield tmp_path
    config.reload_settings()


def test_config_paths_and_secret(data_dir):
    s = config.get_settings()
    assert s.DATA_DIR == data_dir
    assert s.IMAGES_DIR == data_dir / "images" and s.IMAGES_DIR.is_dir()
    assert s.PROFILE_DIR.is_dir() and s.CAMERA_DIR.is_dir() and s.DEBUG_DIR.is_dir()
    assert s.DB_PATH == data_dir / "app.db"
    assert s.PORT == 8080 and s.DISPLAY == ":99" and s.VNC_PORT == 5900
    assert s.HEADLESS is False
    key = s.SECRET_KEY
    assert len(key) > 20
    # persisted: a fresh settings object reads the same key
    assert config.reload_settings().SECRET_KEY == key
    assert (data_dir / "secret_key").read_text().strip() == key


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "abc")
    monkeypatch.setenv("SNAP_HEADLESS", "true")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    s = config.reload_settings()
    assert s.SECRET_KEY == "abc" and s.HEADLESS is True and s.DASHBOARD_PASSWORD == "pw"
    assert config.get_settings() is s


def test_setting_defaults_and_set():
    assert db.get_setting("send_time") == "09:00"
    assert db.get_setting("timezone") == "Europe/Amsterdam"
    assert db.get_setting("enabled") == "1"
    assert db.get_setting("discord_webhook_url") == ""
    assert db.get_setting("notify_on_success") == "1"
    assert db.get_setting("unknown") is None
    assert db.get_setting("unknown", "x") == "x"
    db.set_setting("send_time", "07:30")
    db.set_setting("send_time", "08:15")
    assert db.get_setting("send_time") == "08:15"


def test_recipients():
    a = db.add_recipient("Alice")
    again = db.add_recipient("Alice")
    assert a == again and a["enabled"] is True and isinstance(a["id"], int)
    b = db.add_recipient("  Bob ")
    assert b["name"] == "Bob"
    assert [r["name"] for r in db.list_recipients()] == ["Alice", "Bob"]
    db.set_recipient_enabled(b["id"], False)
    assert {r["name"]: r["enabled"] for r in db.list_recipients()} == {"Alice": True, "Bob": False}
    db.remove_recipient(a["id"])
    assert [r["name"] for r in db.list_recipients()] == ["Bob"]
    with pytest.raises(ValueError):
        db.add_recipient("   ")


def test_history_and_sent_today():
    assert not db.sent_today("2026-11-12")
    db.add_history("2026-11-12", "f1", "/x.png", "success", {"A": {"ok": True, "error": None}}, test=True)
    assert not db.sent_today("2026-11-12")  # test sends don't count
    db.add_history("2026-11-12", "f2", "/y.png", "failed", {}, test=False)
    assert not db.sent_today("2026-11-12")
    hid = db.add_history("2026-11-12", "f3", "/z.png", "partial",
                         {"A": {"ok": True, "error": None}, "B": {"ok": False, "error": "nope"}})
    assert isinstance(hid, int)
    assert db.sent_today("2026-11-12")
    assert not db.sent_today("2026-11-13")
    hist = db.list_history()
    assert [h["fact"] for h in hist] == ["f3", "f2", "f1"]
    assert hist[0]["results"]["B"] == {"ok": False, "error": "nope"}
    assert hist[0]["test"] is False and hist[2]["test"] is True
    assert set(hist[0]) == {"id", "date", "created_at", "fact", "image_path", "status", "results", "test"}
    assert len(db.list_history(limit=1)) == 1


def test_facts_used():
    assert not db.fact_used("Cats purr.")
    db.mark_fact_used("Cats purr.")
    assert db.fact_used("Cats purr.")
    assert db.fact_used("  Cats   purr. ")  # whitespace-normalised
    db.mark_fact_used("Cats purr.")  # idempotent
    assert list(db.used_facts_map()) == ["Cats purr."]
