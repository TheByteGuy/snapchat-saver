"""Environment-driven configuration.

Call ``get_settings()`` at use time (never at import time). ``reload_settings()``
re-reads the environment, which tests use after changing ``DATA_DIR`` etc.
"""
from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    DATA_DIR: Path
    DASHBOARD_PASSWORD: str
    PORT: int
    DISPLAY: str
    VNC_HOST: str
    VNC_PORT: int
    NOVNC_DIR: Path
    HEADLESS: bool
    DEFAULT_TZ: str
    _secret_key: str | None = field(default=None, repr=False)
    _secret_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- derived directories (created on demand) ---
    def _dir(self, name: str) -> Path:
        p = self.DATA_DIR / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def IMAGES_DIR(self) -> Path:
        return self._dir("images")

    @property
    def PROFILE_DIR(self) -> Path:
        return self._dir("chromium-profile")

    @property
    def CAMERA_DIR(self) -> Path:
        return self._dir("camera")

    @property
    def DEBUG_DIR(self) -> Path:
        return self._dir("debug")

    @property
    def DB_PATH(self) -> Path:
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        return self.DATA_DIR / "app.db"

    @property
    def SECRET_KEY(self) -> str:
        """SECRET_KEY env var, else a random key persisted in DATA_DIR/secret_key."""
        if self._secret_key:
            return self._secret_key
        with self._secret_lock:
            if self._secret_key:
                return self._secret_key
            env_key = os.environ.get("SECRET_KEY", "").strip()
            if env_key:
                self._secret_key = env_key
                return env_key
            self.DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = self.DATA_DIR / "secret_key"
            key = ""
            if path.exists():
                key = path.read_text(encoding="utf-8").strip()
            if not key:
                key = secrets.token_urlsafe(48)
                path.write_text(key, encoding="utf-8")
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
            self._secret_key = key
            return key


def _load() -> Settings:
    return Settings(
        DATA_DIR=Path(os.environ.get("DATA_DIR") or "/data"),
        DASHBOARD_PASSWORD=os.environ.get("DASHBOARD_PASSWORD") or "changeme",
        PORT=_env_int("PORT", 8080),
        DISPLAY=os.environ.get("DISPLAY") or ":99",
        VNC_HOST=os.environ.get("VNC_HOST") or "127.0.0.1",
        VNC_PORT=_env_int("VNC_PORT", 5900),
        NOVNC_DIR=Path(os.environ.get("NOVNC_DIR") or "/usr/share/novnc"),
        HEADLESS=_env_bool("SNAP_HEADLESS", False),
        DEFAULT_TZ=os.environ.get("TZ") or "UTC",
    )


_settings: Settings | None = None
_lock = threading.Lock()


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        with _lock:
            if _settings is None:
                _settings = _load()
    return _settings


def reload_settings() -> Settings:
    global _settings
    with _lock:
        _settings = _load()
    return _settings
