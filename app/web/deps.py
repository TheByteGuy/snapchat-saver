"""Small helpers shared by the web modules.

Other app modules (config, db, pipeline, scheduler, notify, snapchat) are always
resolved at call time through ``mod()`` so that the dashboard never captures
them at import time (tests replace them in ``sys.modules``).
"""
from __future__ import annotations

import dataclasses
import importlib
import inspect
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any


def mod(name: str) -> ModuleType:
    """Return ``app.<name>`` from sys.modules (importing it on first use)."""
    return importlib.import_module(f"app.{name}")


def settings() -> Any:
    return mod("config").get_settings()


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def jsonable(obj: Any) -> Any:
    """Convert dataclasses, Paths, datetimes etc. into JSON-friendly values."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, "ok"):
        return {"ok": bool(getattr(obj, "ok")), "error": getattr(obj, "error", None)}
    return str(obj)


def image_url(image_path: Any) -> str | None:
    """Public URL (/images/<name>) for a file inside IMAGES_DIR, else None."""
    if not image_path:
        return None
    try:
        base = Path(settings().IMAGES_DIR).resolve()
        p = Path(str(image_path))
        if not p.is_absolute():
            p = base / p
        p = p.resolve()
        if p.parent != base:
            return None
        version = ""
        try:
            version = f"?v={int(p.stat().st_mtime)}"
        except OSError:
            pass
        return f"/images/{p.name}{version}"
    except Exception:
        return None
