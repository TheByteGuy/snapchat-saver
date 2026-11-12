"""Snapchat Web automation (Playwright + fake webcam).

    from app.snapchat import get_client, SnapchatClient, SendResult, NotLoggedInError
"""
from .camera import prepare_camera_feed
from .client import SnapchatClient, get_client
from .errors import NotLoggedInError, SendResult, SnapchatError, StepError

__all__ = [
    "get_client",
    "SnapchatClient",
    "SendResult",
    "NotLoggedInError",
    "SnapchatError",
    "StepError",
    "prepare_camera_feed",
]
