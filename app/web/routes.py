"""Dashboard page and JSON API (everything here requires the session)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.web.auth import is_default_password, require_auth, require_csrf
from app.web.deps import image_url, jsonable, maybe_await, mod, settings
from app.web.templating import templates
from app.web.vnc import novnc_page

log = logging.getLogger(__name__)

pages = APIRouter(dependencies=[Depends(require_auth)])
api = APIRouter(prefix="/api", dependencies=[Depends(require_csrf)])

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
WEBHOOK_HOSTS = {"discord.com", "discordapp.com", "www.discord.com", "ptb.discord.com",
                 "canary.discord.com", "www.discordapp.com"}
MAX_NAME_LEN = 100


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _short_exc(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text if len(text) <= 400 else text[:400] + "..."


# --------------------------------------------------------------------------- helpers


def _all_settings() -> dict[str, Any]:
    db = mod("db")
    return {
        "send_time": db.get_setting("send_time") or "09:00",
        "timezone": db.get_setting("timezone") or settings().DEFAULT_TZ,
        "enabled": (db.get_setting("enabled") or "1") == "1",
        "discord_webhook_url": db.get_setting("discord_webhook_url") or "",
        "notify_on_success": (db.get_setting("notify_on_success") or "1") == "1",
    }


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _today(tz_name: str) -> str:
    return datetime.now(_tz(tz_name)).date().isoformat()


def valid_webhook(url: str) -> bool:
    if url == "":
        return True
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() in WEBHOOK_HOSTS
        and parts.port is None
        and parts.path.startswith("/api/webhooks/")
        and len(parts.path) > len("/api/webhooks/")
        and not parts.username
        and not parts.password
    )


def valid_timezone(name: str) -> bool:
    if not name or not isinstance(name, str):
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


async def _reschedule() -> None:
    try:
        await maybe_await(mod("scheduler").reschedule())
    except Exception as exc:  # settings are saved even if the scheduler hiccups
        log.exception("reschedule failed")
        raise HTTPException(status_code=500, detail=f"Settings saved, but rescheduling failed: {_short_exc(exc)}")


def _next_run_iso() -> str | None:
    try:
        nr = mod("scheduler").next_run()
    except Exception:
        log.exception("next_run failed")
        return None
    return nr.isoformat() if nr else None


async def _snap_status() -> dict[str, Any]:
    try:
        st = await maybe_await(mod("snapchat").get_client().status())
        return {
            "running": bool(st.get("running")),
            "logged_in": st.get("logged_in"),
            "url": st.get("url"),
            "last_error": st.get("last_error"),
        }
    except Exception as exc:
        return {"running": False, "logged_in": None, "url": None, "last_error": _short_exc(exc)}


# --------------------------------------------------------------------------- page


@pages.get("/")
def index(request: Request, session: dict = Depends(require_auth)):
    boot = {
        "csrf": session.get("csrf", ""),
        "novnc_page": novnc_page(),
        "novnc_dir": str(settings().NOVNC_DIR),
    }
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "csrf": session.get("csrf", ""),
            "default_password": is_default_password(),
            "boot_json": json.dumps(boot).replace("</", "<\\/"),
        },
    )


@pages.get("/images/{filename}")
def serve_image(filename: str):
    if (not filename or "/" in filename or "\\" in filename or "\x00" in filename
            or filename.startswith(".")):
        raise HTTPException(status_code=404, detail="Not found")
    base = Path(settings().IMAGES_DIR).resolve()
    try:
        path = (base / filename).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Not found")
    if path.parent != base or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=60"})


# --------------------------------------------------------------------------- status


@api.get("/status")
async def api_status():
    s = _all_settings()
    db = mod("db")
    today = _today(s["timezone"])
    try:
        sent = bool(db.sent_today(today))
    except Exception:
        sent = False
    return {
        "snapchat": await _snap_status(),
        "next_run": _next_run_iso(),
        "settings": s,
        "today": today,
        "sent_today": sent,
        "default_password": is_default_password(),
        "send_test": send_job.snapshot(),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- recipients


@api.get("/recipients")
def list_recipients():
    return {"recipients": mod("db").list_recipients()}


def _clean_name(name: Any) -> str:
    if not isinstance(name, str):
        raise HTTPException(status_code=422, detail="Recipient name must be text.")
    name = " ".join(name.split())
    if not name:
        raise HTTPException(status_code=422, detail="Recipient name is empty.")
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(status_code=422, detail=f"Recipient name is longer than {MAX_NAME_LEN} characters.")
    return name


@api.post("/recipients")
def add_recipients(payload: dict = Body(...)):
    """Body: {"name": "..."} or {"names": ["...", ...]}."""
    names: list[Any]
    if "names" in payload:
        if not isinstance(payload["names"], list) or not payload["names"]:
            raise HTTPException(status_code=422, detail="'names' must be a non-empty list.")
        names = payload["names"]
    else:
        names = [payload.get("name")]
    cleaned = [_clean_name(n) for n in names]
    db = mod("db")
    added = [db.add_recipient(n) for n in cleaned]
    return {"ok": True, "added": added, "recipients": db.list_recipients()}


def _find_recipient(rid: int) -> dict:
    for r in mod("db").list_recipients():
        if int(r["id"]) == rid:
            return r
    raise HTTPException(status_code=404, detail="Recipient not found.")


@api.delete("/recipients/{rid}")
def delete_recipient(rid: int):
    _find_recipient(rid)
    db = mod("db")
    db.remove_recipient(rid)
    return {"ok": True, "recipients": db.list_recipients()}


@api.patch("/recipients/{rid}")
def patch_recipient(rid: int, payload: dict = Body(...)):
    _find_recipient(rid)
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="'enabled' must be true or false.")
    db = mod("db")
    db.set_recipient_enabled(rid, enabled)
    return {"ok": True, "recipient": _find_recipient(rid), "recipients": db.list_recipients()}


@api.get("/friends")
async def list_friends():
    try:
        friends = await maybe_await(mod("snapchat").get_client().list_friends())
    except Exception as exc:
        return _err(502, f"Could not load friends: {_short_exc(exc)}")
    existing = {r["name"].casefold() for r in mod("db").list_recipients()}
    seen: set[str] = set()
    out = []
    for f in friends or []:
        name = str(f).strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append({"name": name, "added": name.casefold() in existing})
    return {"friends": out}


# --------------------------------------------------------------------------- snapchat


@api.post("/snapchat/open-login")
async def snapchat_open_login():
    try:
        await maybe_await(mod("snapchat").get_client().open_login())
    except Exception as exc:
        return _err(502, f"Could not open Snapchat: {_short_exc(exc)}")
    return {"ok": True, "snapchat": await _snap_status()}


@api.post("/snapchat/check-login")
async def snapchat_check_login():
    try:
        logged_in = await maybe_await(mod("snapchat").get_client().is_logged_in())
    except Exception as exc:
        return _err(502, f"Login check failed: {_short_exc(exc)}")
    return {"ok": True, "logged_in": bool(logged_in), "snapchat": await _snap_status()}


@api.get("/snapchat/screenshot")
async def snapchat_screenshot():
    try:
        png = await maybe_await(mod("snapchat").get_client().screenshot())
    except Exception as exc:
        return _err(502, f"Screenshot failed: {_short_exc(exc)}")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- settings


@api.get("/settings")
def get_settings_api():
    return {"settings": _all_settings()}


@api.get("/timezones")
def timezones():
    zones = sorted(z for z in available_timezones() if "/" in z or z == "UTC")
    return {"timezones": zones}


@api.put("/settings")
async def put_settings(payload: dict = Body(...)):
    errors: dict[str, str] = {}
    updates: dict[str, str] = {}
    known = {"send_time", "timezone", "enabled", "discord_webhook_url", "notify_on_success"}
    for key in payload:
        if key not in known:
            errors[key] = "Unknown setting."
    if "send_time" in payload:
        v = payload["send_time"]
        if isinstance(v, str) and TIME_RE.match(v.strip()):
            updates["send_time"] = v.strip()
        else:
            errors["send_time"] = "Use 24-hour HH:MM, e.g. 09:00."
    if "timezone" in payload:
        v = payload["timezone"]
        if isinstance(v, str) and valid_timezone(v.strip()):
            updates["timezone"] = v.strip()
        else:
            errors["timezone"] = "Unknown timezone. Pick one from the list, e.g. Europe/Berlin."
    for key in ("enabled", "notify_on_success"):
        if key in payload:
            v = payload[key]
            if isinstance(v, bool):
                updates[key] = "1" if v else "0"
            else:
                errors[key] = "Must be true or false."
    if "discord_webhook_url" in payload:
        v = payload["discord_webhook_url"]
        v = v.strip() if isinstance(v, str) else None
        if v is not None and valid_webhook(v):
            updates["discord_webhook_url"] = v
        else:
            errors["discord_webhook_url"] = (
                "Must be empty or a Discord webhook URL like https://discord.com/api/webhooks/..."
            )
    if errors:
        return JSONResponse(
            {"ok": False, "error": "; ".join(f"{k}: {v}" for k, v in errors.items()), "errors": errors},
            status_code=422,
        )
    db = mod("db")
    for k, v in updates.items():
        db.set_setting(k, v)
    if updates.keys() & {"send_time", "timezone", "enabled"}:
        await _reschedule()
    return {"ok": True, "settings": _all_settings(), "next_run": _next_run_iso()}


# --------------------------------------------------------------------------- preview / notify


@api.post("/preview")
async def preview():
    try:
        res = await maybe_await(mod("pipeline").generate_preview())
    except Exception as exc:
        log.exception("preview failed")
        return _err(500, f"Preview failed: {_short_exc(exc)}")
    return {"ok": True, "fact": res.get("fact"), "image_url": image_url(res.get("image_path"))}


@api.post("/notify-test")
async def notify_test():
    s = _all_settings()
    if not s["discord_webhook_url"]:
        return _err(400, "No Discord webhook URL is saved yet.")
    try:
        ok = await maybe_await(mod("notify").notify_discord(
            "Snapchat Saver test alert",
            "If you can read this, Discord alerts are working.",
            True,
        ))
    except Exception as exc:
        return _err(502, f"Sending the test alert failed: {_short_exc(exc)}")
    if not ok:
        return _err(502, "Discord did not accept the test alert. Check the webhook URL.")
    return {"ok": True}


# --------------------------------------------------------------------------- send test


class SendTestJob:
    """Tracks the single background 'send test snap' run."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.state = "idle"  # idle | running | done | error
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.recipients: list[str] = []
        self.result: dict | None = None
        self.error: str | None = None
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.state == "running"

    def snapshot(self) -> dict[str, Any]:
        def iso(ts):
            return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None

        now = time.time()
        elapsed = None
        if self.started_at:
            elapsed = round((self.finished_at or now) - self.started_at, 1)
        result = None
        if self.result is not None:
            result = jsonable(self.result)
            result["image_url"] = image_url(self.result.get("image_path"))
        return {
            "state": self.state,
            "running": self.running,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "elapsed": elapsed,
            "recipients": self.recipients,
            "result": result,
            "error": self.error,
        }

    async def _run(self, recipients: list[str] | None) -> None:
        try:
            res = await maybe_await(mod("pipeline").run_send(test=True, recipients=recipients))
            self.result = res if isinstance(res, dict) else {"status": str(res)}
            self.state = "done"
        except Exception as exc:
            log.exception("test send failed")
            self.error = _short_exc(exc)
            self.state = "error"
        finally:
            self.finished_at = time.time()

    def start(self, recipients: list[str] | None, display: list[str]) -> None:
        self.reset()
        self.state = "running"
        self.started_at = time.time()
        self.recipients = display
        self.task = asyncio.create_task(self._run(recipients))


send_job = SendTestJob()


@api.post("/send-test")
async def send_test(payload: dict | None = Body(default=None)):
    if send_job.running:
        return _err(409, "A test send is already running.")
    raw = (payload or {}).get("recipients")
    if raw is not None and not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="'recipients' must be a list of names.")
    recipients = [_clean_name(r) for r in raw] if raw else None
    if recipients is None:
        display = [r["name"] for r in mod("db").list_recipients() if r.get("enabled")]
        if not display:
            return _err(400, "No enabled recipients. Add or enable a recipient first.")
    else:
        display = recipients
    send_job.start(recipients, display)
    return JSONResponse({"ok": True, "send_test": send_job.snapshot()}, status_code=202)


@api.get("/send-test/status")
def send_test_status():
    return send_job.snapshot()


# --------------------------------------------------------------------------- history


@api.get("/history")
def history(limit: int = 30):
    limit = max(1, min(limit, 200))
    items = []
    for h in mod("db").list_history(limit=limit):
        item = jsonable(h)
        item["image_url"] = image_url(h.get("image_path"))
        items.append(item)
    return {"history": items}
