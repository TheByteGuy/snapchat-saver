"""Fact -> image -> snap -> history -> Discord."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import db, facts, imagegen, notify
from app.config import get_settings

log = logging.getLogger(__name__)

_lock = asyncio.Lock()
_LOGIN_RE = re.compile(r"log(ged)?[ -]?(in|out)|session expired", re.IGNORECASE)

LOGIN_HINT = ("Snapchat Web appears to be logged out. Open the dashboard and log in again "
              "through the embedded browser view.")


def get_timezone() -> ZoneInfo:
    name = db.get_setting("timezone") or get_settings().DEFAULT_TZ or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Unknown timezone %r, falling back to UTC", name)
        return ZoneInfo("UTC")


def now_local() -> datetime:
    return datetime.now(get_timezone())


def today_str() -> str:
    return now_local().date().isoformat()


def is_busy() -> bool:
    """True while a send is in progress."""
    return _lock.locked()


async def _render(fact: str, out_path: Path) -> Path:
    date_text = imagegen.format_date(now_local().date())
    return await asyncio.to_thread(imagegen.render_fact_image, fact, out_path, None, date_text)


async def generate_preview() -> dict:
    """Fetch a fact and render IMAGES_DIR/preview.png without sending."""
    fact = await facts.get_fact()
    path = await _render(fact, get_settings().IMAGES_DIR / "preview.png")
    return {"fact": fact, "image_path": str(path)}


def _is_not_logged_in(exc: BaseException) -> bool:
    try:
        from app.snapchat import NotLoggedInError  # noqa: WPS433 (lazy import on purpose)
        if isinstance(exc, NotLoggedInError):
            return True
    except Exception:
        pass
    return type(exc).__name__ == "NotLoggedInError"


def _convert_result(r) -> dict:
    if r is None:
        return {"ok": False, "error": "No result returned for this recipient"}
    if isinstance(r, dict):
        ok = bool(r.get("ok"))
        err = r.get("error")
    else:
        ok = bool(getattr(r, "ok", False))
        err = getattr(r, "error", None)
    if not ok and not err:
        err = "Unknown error"
    return {"ok": ok, "error": None if ok else (str(err) if err is not None else None)}


def _status(results: dict) -> str:
    oks = sum(1 for r in results.values() if r.get("ok"))
    if results and oks == len(results):
        return "success"
    if oks:
        return "partial"
    return "failed"


async def run_send(test: bool = False, recipients: list[str] | None = None) -> dict:
    """Generate today's image and send it as a snap.

    Returns dict(status, fact, image_path, results, error). ``error`` is None unless something
    failed as a whole (busy, no recipients, not logged in, exception).
    """
    if _lock.locked():
        return {"status": "failed", "fact": "", "image_path": "", "results": {},
                "error": "Another send is already in progress"}
    async with _lock:
        return await _run_send_locked(test, recipients)


async def _run_send_locked(test: bool, recipients: list[str] | None) -> dict:
    settings = get_settings()
    date = today_str()
    fact = ""
    image_path = ""
    results: dict[str, dict] = {}
    error: str | None = None
    login_error = False

    try:
        if recipients is None:
            names = [r["name"] for r in db.list_recipients() if r["enabled"]]
        else:
            names = [n.strip() for n in recipients if n and n.strip()]
        names = list(dict.fromkeys(names))  # de-duplicate, keep order

        fact = await facts.get_fact()
        if test:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            out = settings.IMAGES_DIR / f"test-{stamp}.png"
        else:
            out = settings.IMAGES_DIR / f"{date}.png"
        image_path = str(await _render(fact, out))

        if not names:
            error = "No recipients configured. Add and enable recipients in the dashboard."
        else:
            from app.snapchat import get_client  # lazy so tests can mock it

            client = get_client()
            raw = await client.send_snap(Path(image_path), names)
            raw = raw or {}
            results = {name: _convert_result(raw.get(name)) for name in names}
            # keep any extra names the client reported
            for name, r in raw.items():
                results.setdefault(name, _convert_result(r))
    except Exception as exc:  # noqa: BLE001 - everything becomes a "failed" run
        if _is_not_logged_in(exc):
            login_error = True
            error = f"Not logged in to Snapchat. {LOGIN_HINT}"
        else:
            error = f"{type(exc).__name__}: {exc}"
        log.exception("Send run failed")

    status = "failed" if error else _status(results)
    if status == "failed" and not error:
        errs = {r["error"] for r in results.values() if r.get("error")}
        error = "; ".join(sorted(errs)) or "All recipients failed"

    try:
        db.add_history(date, fact, image_path, status, results, test=test)
    except Exception:
        log.exception("Could not write history")

    if not test and status in ("success", "partial") and fact:
        try:
            db.mark_fact_used(fact)
        except Exception:
            log.exception("Could not mark fact used")

    await _notify(status, fact, image_path, results, error, test, login_error)
    return {"status": status, "fact": fact, "image_path": image_path, "results": results, "error": error}


async def _notify(status: str, fact: str, image_path: str, results: dict, error: str | None,
                  test: bool, login_error: bool) -> None:
    prefix = "[TEST] " if test else ""
    img = Path(image_path) if image_path else None
    try:
        if status == "success":
            if (db.get_setting("notify_on_success") or "1") != "1":
                return
            names = ", ".join(results) or "-"
            await notify.notify_discord(
                f"{prefix}Snap sent", f"Sent to {len(results)} recipient(s): {names}\n\n> {fact}", True, img)
            return

        lines = []
        if status == "partial":
            title = f"{prefix}Snap partially sent"
        else:
            title = f"{prefix}Snap failed"
        if error:
            lines.append(f"**Error:** {error}")
        for name, r in results.items():
            lines.append(f"{'OK' if r['ok'] else 'FAILED'} - {name}" + ("" if r["ok"] else f": {r['error']}"))
        all_errors = " ".join([error or ""] + [r.get("error") or "" for r in results.values()])
        if login_error or _LOGIN_RE.search(all_errors):
            if LOGIN_HINT not in "\n".join(lines):
                lines.append(LOGIN_HINT)
        if fact:
            lines.append(f"\n> {fact}")
        await notify.notify_discord(title, "\n".join(lines), False, img if status == "partial" else None)
    except Exception:
        log.exception("Notification failed")
