from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import config, db, pipeline, scheduler


@pytest.fixture
async def sched(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.reload_settings()
    db.init_db()
    calls = []

    async def fake_run_send(test=False, recipients=None):
        calls.append((test, recipients))
        return {"status": fake_run_send.status, "fact": "f", "image_path": "", "results": {}}

    fake_run_send.status = "success"
    monkeypatch.setattr(pipeline, "run_send", fake_run_send)
    yield fake_run_send, calls
    scheduler.shutdown()
    config.reload_settings()


async def test_next_run_none_before_start():
    scheduler.shutdown()
    assert scheduler.next_run() is None
    scheduler.reschedule()  # safe before start


async def test_start_and_next_run(sched):
    db.set_setting("timezone", "America/New_York")
    db.set_setting("send_time", "07:45")
    scheduler.start()
    scheduler.start()  # idempotent
    nr = scheduler.next_run()
    assert nr is not None
    local = nr.astimezone(ZoneInfo("America/New_York"))
    assert (local.hour, local.minute) == (7, 45)
    assert nr > datetime.now(ZoneInfo("UTC"))
    assert nr - datetime.now(ZoneInfo("UTC")) <= timedelta(days=1, hours=1)


async def test_reschedule_changes_time_and_disable(sched):
    db.set_setting("timezone", "UTC")
    db.set_setting("send_time", "09:00")
    scheduler.start()
    db.set_setting("send_time", "21:30")
    db.set_setting("timezone", "Asia/Tokyo")
    scheduler.reschedule()
    local = scheduler.next_run().astimezone(ZoneInfo("Asia/Tokyo"))
    assert (local.hour, local.minute) == (21, 30)
    db.set_setting("enabled", "0")
    scheduler.reschedule()
    assert scheduler.next_run() is None
    db.set_setting("enabled", "1")
    scheduler.reschedule()
    assert scheduler.next_run() is not None


async def test_invalid_time_falls_back(sched):
    db.set_setting("timezone", "UTC")
    db.set_setting("send_time", "garbage")
    scheduler.start()
    nr = scheduler.next_run()
    assert (nr.hour, nr.minute) == (9, 0)


async def test_daily_job_skips_when_sent(sched):
    fake, calls = sched
    scheduler.start()
    db.add_history(pipeline.today_str(), "f", "", "success", {})
    await scheduler._daily_job()
    assert calls == []


async def test_daily_job_skips_when_disabled(sched):
    fake, calls = sched
    db.set_setting("enabled", "0")
    scheduler.start()
    await scheduler._daily_job()
    assert calls == []


async def test_daily_job_retries_on_failure(sched):
    fake, calls = sched
    db.set_setting("timezone", "UTC")
    db.set_setting("send_time", "03:00")
    scheduler.start()
    fake.status = "failed"
    before = datetime.now(ZoneInfo("UTC"))
    await scheduler._daily_job()
    assert calls == [(False, None)]
    job = scheduler._scheduler.get_job(scheduler.RETRY_JOB_ID)
    assert job is not None and job.kwargs == {"attempt": 1}
    delta = job.next_run_time - before
    assert timedelta(minutes=14) < delta < timedelta(minutes=16)
    assert scheduler.next_run() <= job.next_run_time

    # last allowed retry does not schedule another one
    scheduler._remove(scheduler.RETRY_JOB_ID)
    await scheduler._daily_job(attempt=scheduler.MAX_RETRIES)
    assert scheduler._scheduler.get_job(scheduler.RETRY_JOB_ID) is None


async def test_daily_job_no_retry_on_success(sched):
    fake, calls = sched
    scheduler.start()
    await scheduler._daily_job()
    assert calls and scheduler._scheduler.get_job(scheduler.RETRY_JOB_ID) is None


async def test_catch_up_after_restart(sched):
    db.set_setting("timezone", "UTC")
    past = datetime.now(ZoneInfo("UTC")) - timedelta(minutes=30)
    if past.date() != datetime.now(ZoneInfo("UTC")).date():
        pytest.skip("too close to midnight")
    db.set_setting("send_time", past.strftime("%H:%M"))
    scheduler.start()
    job = scheduler._scheduler.get_job(scheduler.CATCHUP_JOB_ID)
    assert job is not None
    assert scheduler.next_run() == job.next_run_time


async def test_no_catch_up_if_already_sent(sched):
    db.set_setting("timezone", "UTC")
    past = datetime.now(ZoneInfo("UTC")) - timedelta(minutes=30)
    if past.date() != datetime.now(ZoneInfo("UTC")).date():
        pytest.skip("too close to midnight")
    db.set_setting("send_time", past.strftime("%H:%M"))
    db.add_history(pipeline.today_str(), "f", "", "success", {})
    scheduler.start()
    assert scheduler._scheduler.get_job(scheduler.CATCHUP_JOB_ID) is None
