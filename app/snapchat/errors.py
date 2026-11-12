"""Exceptions and result types for the Snapchat automation."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SendResult:
    """Outcome of sending today's snap to one recipient."""

    ok: bool
    error: str | None = None


class SnapchatError(Exception):
    """Base class for Snapchat automation errors."""


class NotLoggedInError(SnapchatError):
    """Snapchat Web is showing the login page; the user must log in via the dashboard's browser view."""


class StepError(SnapchatError):
    """A UI step could not be completed because none of its candidate selectors matched."""

    def __init__(self, step: str, candidates: list[str], detail: str | None = None):
        self.step = step
        self.candidates = list(candidates)
        msg = f"Snapchat Web step '{step}' failed: no candidate selector matched"
        if detail:
            msg += f" ({detail})"
        msg += f". Tried: {self.candidates}. Fix by overriding '{step}' in DATA_DIR/selectors.json."
        super().__init__(msg)
