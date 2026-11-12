"""All Snapchat Web selectors, as ordered candidate lists per UI step.

Snapchat Web uses obfuscated class names that change between releases, so every
step is located through accessible roles, aria-labels, titles and visible text,
with several fallbacks tried in order. The first candidate that matches a
*visible* element wins, and the client logs which one it was.

Each candidate is a Playwright selector string (CSS, ``role=...``, ``text=...``,
``xpath=...``, ``:text-is()``, ``:has()`` ...). In the ``picker_recipient`` step
the placeholder ``{name}`` is replaced by the recipient's display name (quotes
escaped), e.g. ``:text-is("{name}")``.

Runtime overrides: create ``DATA_DIR/selectors.json`` (re-read whenever it
changes, no restart needed)::

    {
      "shutter": ["button[aria-label='Take a Snap']", "role=button[name=/capture/i]"],
      "send_to": {"prepend": ["button:has-text('Send to')"]},
      "popup_dismiss": {"append": ["role=button[name=/^later$/i]"]}
    }

A JSON list replaces the built-in candidates for that step; an object with
``prepend`` and/or ``append`` lists extends them. Unknown step names are ignored
with a warning.
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

log = logging.getLogger("snapchat")

# Accessible-name regexes shared by several steps.
_SHUTTER_RE = r"/capture|shutter|take (a )?(snap|photo|picture)/i"

DEFAULT_SELECTORS: dict[str, list[str]] = {
    # Present only when logged OUT (login form on accounts.snapchat.com or the /web landing page).
    "logged_out": [
        "input[name='accountIdentifier']",
        "input#ai_input",
        "input[autocomplete='username']",
        "input[type='password']",
        "role=button[name=/^log ?in$/i]",
        "role=link[name=/^log ?in$/i]",
        "text=/log in to snapchat/i",
    ],
    # Present only when logged IN (chat list / camera UI).
    "logged_in": [
        "role=button[name=/click the camera to send snaps/i]",
        "text=/click the camera to send snaps/i",
        "role=button[name=/^new chat$/i]",
        "[title='New Chat' i]",
        "[aria-label*='new chat' i]",
        "[role='list'] [role='listitem']",
        "nav [role='listitem']",
    ],
    # Rows of the chat list (sidebar). The first text line of each row is the display name.
    "chat_list_item": [
        "[role='list'] [role='listitem']",
        "nav [role='listitem']",
        "[role='listitem']",
        "nav li",
    ],
    # Scrollable container of the chat list (used to load more rows).
    "chat_list_scroll": [
        "[role='list']",
        "nav",
    ],
    # Button/area that turns the camera on (Snapchat Web keeps it off until clicked).
    "open_camera": [
        "role=button[name=/click the camera to send snaps|open camera|enable camera|turn on camera|use camera/i]",
        "text=/click the camera to send snaps/i",
        "[aria-label*='camera' i]",
        "[title*='camera' i]",
    ],
    # Live preview <video> showing the (fake) webcam.
    "camera_video": [
        "video",
    ],
    # Shutter / capture button.
    "shutter": [
        f"role=button[name={_SHUTTER_RE}]",
        "[aria-label*='capture' i]",
        "[title*='capture' i]",
        "[aria-label*='take a snap' i]",
        "button[class*='shutter' i]",
        "button[class*='capture' i]",
    ],
    # "Send To" button shown under the captured snap preview.
    "send_to": [
        "role=button[name=/^send ?to$/i]",
        "button:has-text('Send To')",
        "[aria-label*='send to' i]",
        "[title*='send to' i]",
        "text=/^send to$/i",
    ],
    # Search box in the recipient picker.
    "picker_search": [
        "role=textbox[name=/search|send to|^to:?$/i]",
        "role=searchbox",
        "input[placeholder*='search' i]",
        "input[type='search']",
        "form input[type='text']",
    ],
    # Container of the recipient picker; recipient rows are searched inside it so that a chat-list
    # row with the same name (still visible in the sidebar) is never clicked by mistake.
    "picker_container": [
        "[role='dialog']:has(input)",
        "form:has(input):has(button)",
        "[role='dialog']",
        "form",
    ],
    # A recipient row in the picker; {name} = exact display name.
    "picker_recipient": [
        "[role='listitem']:has(:text-is(\"{name}\"))",
        "[role='option']:has(:text-is(\"{name}\"))",
        "[role='checkbox']:has(:text-is(\"{name}\"))",
        "label:has(:text-is(\"{name}\"))",
        "li:has(:text-is(\"{name}\"))",
        ":text-is(\"{name}\")",
    ],
    # Final Send button in the picker.
    "send_button": [
        "role=button[name=/^send$/i]",
        "button[type='submit']:has-text('Send')",
        "[aria-label='Send' i]",
        "button:text-is('Send')",
    ],
    # Harmless popups to dismiss (cookie banner, notification prompts, tips).
    "popup_dismiss": [
        "role=button[name=/^(not now|accept all|accept all cookies|allow all|got it|maybe later|no thanks|dismiss)$/i]",
        "button:text-is('Not now')",
        "button:text-is('Accept All')",
    ],
}


def _load_overrides(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.error("selectors.json at %s is invalid and was ignored: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        log.error("selectors.json at %s must contain a JSON object; ignored", path)
        return {}
    return data


def _merge(base: dict[str, list[str]], overrides: dict) -> dict[str, list[str]]:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if key not in merged:
            log.warning("selectors.json: unknown step '%s' ignored (known: %s)", key, ", ".join(sorted(merged)))
            continue
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            merged[key] = list(value)
        elif isinstance(value, dict):
            pre = [v for v in value.get("prepend", []) if isinstance(v, str)]
            post = [v for v in value.get("append", []) if isinstance(v, str)]
            merged[key] = pre + merged[key] + post
        else:
            log.warning("selectors.json: value for '%s' must be a list of strings or {prepend,append}; ignored", key)
            continue
        log.info("selectors.json: overriding step '%s' -> %s", key, merged[key])
    return merged


def escape_name(name: str) -> str:
    """Escape a display name for use inside a double-quoted selector string."""
    return name.replace("\\", "\\\\").replace('"', '\\"')


class SelectorStore:
    """Built-in selectors merged with DATA_DIR/selectors.json (reloaded when the file changes)."""

    def __init__(self, override_path: Path | None):
        self.override_path = override_path
        self._mtime: float | None = None
        self._merged: dict[str, list[str]] = copy.deepcopy(DEFAULT_SELECTORS)

    def _refresh(self) -> None:
        if self.override_path is None:
            return
        try:
            mtime = self.override_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return
        self._mtime = mtime
        overrides = _load_overrides(self.override_path) if mtime is not None else {}
        self._merged = _merge(DEFAULT_SELECTORS, overrides)

    def get(self, step: str, name: str | None = None) -> list[str]:
        self._refresh()
        candidates = self._merged[step]
        if name is not None:
            esc = escape_name(name)
            candidates = [c.replace("{name}", esc) for c in candidates]
        return list(candidates)

    def all(self) -> dict[str, list[str]]:
        self._refresh()
        return copy.deepcopy(self._merged)
