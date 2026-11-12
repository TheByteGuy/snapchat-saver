"""Discord webhook notifications. Never raises."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app import db

log = logging.getLogger(__name__)

COLOR_OK = 0x2ECC71
COLOR_FAIL = 0xE74C3C
TIMEOUT = 20.0


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def notify_discord(title: str, message: str, ok: bool, image_path: Path | None = None) -> bool:
    """Post an embed to the configured Discord webhook.

    Returns True when Discord accepted the message, False when no webhook is configured
    or anything went wrong.
    """
    try:
        url = (db.get_setting("discord_webhook_url") or "").strip()
    except Exception:
        log.exception("Could not read discord_webhook_url setting")
        return False
    if not url:
        return False

    embed: dict = {
        "title": _truncate(title, 256),
        "description": _truncate(message, 4000),
        "color": COLOR_OK if ok else COLOR_FAIL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "snapchat-saver"},
    }
    payload = {"username": "Snapchat Saver", "embeds": [embed]}

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            img = Path(image_path) if image_path else None
            if img is not None and img.is_file():
                filename = "snap.png"
                embed["image"] = {"url": f"attachment://{filename}"}
                files = {"files[0]": (filename, img.read_bytes(), "image/png")}
                resp = await client.post(url, data={"payload_json": json.dumps(payload)}, files=files)
            else:
                resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            log.warning("Discord webhook returned %s: %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as exc:
        log.warning("Discord webhook failed: %s", exc)
        return False
