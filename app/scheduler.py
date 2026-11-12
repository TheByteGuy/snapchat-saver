"""Daily send scheduling (APScheduler 3.x AsyncIOScheduler).

``start()`` must be called from inside a running event loop (FastAPI lifespan).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app import db

log = logging.getLogger(__name__)

DAILY_JOB_ID = "daily_send"
RETRY_JOB_ID = "retry_send"
CATCHUP_JOB_ID = "catchup_send"
MAX_RETRIES = 3
RETRY_DELAY = timedelta(minutes=15)
MISFIRE_GRACE = 3 * 3600  # seconds
CATCHUP_DELAY = timedelta(seconds=90)  # give the browser time to come up after a restart

_scheduler: AsyncIOScheduler | None = None


def _tz():
    from app.pipeline import get_timezone
    return get_timezone()


def _parse_time(value: str | None) -> tuple[int, int]:
    try:
        hh, mm = (value or "09:00").strip().split(":")[:2]
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    log.warning("Invalid send_time %r, using 09:00", value)
    return 9, 0


def _enabled() -> bool:
    return (db.get_setting("enabled") or "1") == "1"


async def _daily_job(attempt: int = 0) -> None:
    """Run the daily send unless it already happened today; schedule retries on failure."""
    try:
        from app import pipeline

        if not _enabled():
            log.info("Daily send skipped: disabled")
            return
        today = pipeline.today_str()
        if db.sent_today(today):
            log.info("Daily send skipped: already sent for %s", today)
            return
        log.info("Daily send starting (attempt %d)", attempt + 1)
        result = await pipeline.run_send(test=False)
        status = result.get("status")
    except Exception:
        log.exception("Daily send crashed")
        status = "failed"

    if status == "failed":
        if attempt < MAX_RETRIES:
            _schedule_retry(attempt + 1)
        else:
            log.error("Daily send failed after %d retries; giving up for today", MAX_RETRIES)


def _schedule_retry(attempt: int) -> None:
    if _scheduler is None:
        return
    run_at = datetime.now(_tz()) + RETRY_DELAY
    log.warning("Scheduling retry %d/%d at %s", attempt, MAX_RETRIES, run_at.isoformat())
    _scheduler.add_job(
        _daily_job, DateTrigger(run_date=run_at), id=RETRY_JOB_ID, replace_existing=True,
        kwargs={"attempt": attempt}, misfire_grace_time=MISFIRE_GRACE, coalesce=True, max_instances=1,
    )


def _remove(job_id: str) -> None:
    if _scheduler is not None and _scheduler.get_job(job_id) is not None:
        _scheduler.remove_job(job_id)


def reschedule() -> None:
    """Re-read send_time/timezone/enabled and (re)install the daily job. Safe any time."""
    if _scheduler is None:
        return
    try:
        if not _enabled():
            for jid in (DAILY_JOB_ID, RETRY_JOB_ID, CATCHUP_JOB_ID):
                _remove(jid)
            log.info("Daily send disabled; jobs removed")
            return
        tz = _tz()
        hour, minute = _parse_time(db.get_setting("send_time"))
        _scheduler.add_job(
            _daily_job, CronTrigger(hour=hour, minute=minute, timezone=tz), id=DAILY_JOB_ID,
            replace_existing=True, misfire_grace_time=MISFIRE_GRACE, coalesce=True, max_instances=1,
        )
        log.info("Daily send scheduled at %02d:%02d %s", hour, minute, tz)
    except Exception:
        log.exception("reschedule failed")


def _maybe_catch_up() -> None:
    """After a (container) restart shortly after today's send time, run the missed send."""
    if _scheduler is None or not _enabled():
        return
    try:
        from app import pipeline

        tz = _tz()
        now = datetime.now(tz)
        hour, minute = _parse_time(db.get_setting("send_time"))
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled <= now <= scheduled + timedelta(seconds=MISFIRE_GRACE) \
                and not db.sent_today(pipeline.today_str()):
            run_at = now + CATCHUP_DELAY
            log.info("Missed today's send at %s; catching up at %s", scheduled, run_at)
            _scheduler.add_job(
                _daily_job, DateTrigger(run_date=run_at), id=CATCHUP_JOB_ID, replace_existing=True,
                misfire_grace_time=MISFIRE_GRACE, coalesce=True, max_instances=1,
            )
    except Exception:
        log.exception("catch-up check failed")


def start() -> None:
    """Start the scheduler. Must be called with a running asyncio event loop."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return
    _scheduler = AsyncIOScheduler(timezone=_tz())
    _scheduler.start()
    reschedule()
    _maybe_catch_up()


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            log.exception("scheduler shutdown failed")
    _scheduler = None


def next_run() -> datetime | None:
    """Earliest pending send (daily, retry or catch-up), or None when nothing is scheduled."""
    if _scheduler is None:
        return None
    times = []
    for jid in (DAILY_JOB_ID, RETRY_JOB_ID, CATCHUP_JOB_ID):
        job = _scheduler.get_job(jid)
        if job is not None and job.next_run_time is not None:
            times.append(job.next_run_time)
    return min(times) if times else None
