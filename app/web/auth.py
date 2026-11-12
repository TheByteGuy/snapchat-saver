"""Single-password authentication with a signed session cookie, login rate
limiting and CSRF protection for state-changing requests."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.web.deps import settings
from app.web.templating import templates

COOKIE_NAME = "snapsaver_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days
DEFAULT_PASSWORD = "changeme"

MAX_FAILURES = 5
FAILURE_WINDOW = 300  # seconds

router = APIRouter()


class NotAuthenticated(Exception):
    """Raised by require_auth; turned into 401 JSON or a redirect to /login."""


# --------------------------------------------------------------------------- session


def _serializer() -> URLSafeTimedSerializer:
    key = settings().SECRET_KEY
    if isinstance(key, bytes):
        key = key.decode("utf-8", "replace")
    return URLSafeTimedSerializer(str(key), salt="snapchat-saver-session")


def _password_fingerprint() -> str:
    """Binds sessions to the current password: changing it logs everyone out."""
    pw = str(settings().DASHBOARD_PASSWORD or "")
    return hashlib.sha256(("snapsaver:" + pw).encode()).hexdigest()[:16]


def new_session_cookie() -> str:
    data = {"auth": True, "csrf": secrets.token_urlsafe(24), "pw": _password_fingerprint()}
    return _serializer().dumps(data)


def read_session(cookie: str | None) -> dict[str, Any] | None:
    if not cookie:
        return None
    try:
        data = _serializer().loads(cookie, max_age=SESSION_MAX_AGE)
    except (BadSignature, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("auth") is not True:
        return None
    if not hmac.compare_digest(str(data.get("pw", "")), _password_fingerprint()):
        return None
    return data


def session_from_scope(scope: dict) -> dict[str, Any] | None:
    """Read the session from a raw ASGI scope (used by the noVNC mount)."""
    from http.cookies import SimpleCookie

    for name, value in scope.get("headers", []):
        if name == b"cookie":
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:
                continue
            if COOKIE_NAME in jar:
                return read_session(jar[COOKIE_NAME].value)
    return None


def set_session_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        COOKIE_NAME,
        new_session_cookie(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


# --------------------------------------------------------------------------- dependencies


def require_auth(request: Request) -> dict[str, Any]:
    sess = read_session(request.cookies.get(COOKIE_NAME))
    if sess is None:
        raise NotAuthenticated()
    return sess


def origin_ok(headers: Any) -> bool:
    """Reject cross-site requests: if an Origin header is present its host must
    match the Host (or X-Forwarded-Host) header."""
    origin = headers.get("origin")
    if not origin or origin == "null":
        return origin is None or origin == ""
    o_host = urlsplit(origin).netloc.lower()
    hosts = {headers.get("host", "").lower()}
    fwd = headers.get("x-forwarded-host")
    if fwd:
        hosts.add(fwd.split(",")[0].strip().lower())
    return o_host in hosts


def require_csrf(request: Request) -> dict[str, Any]:
    """Auth + CSRF check for state-changing API calls: the X-CSRF-Token header
    must carry the token stored in the signed session."""
    sess = require_auth(request)
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return sess
    token = request.headers.get("x-csrf-token", "")
    if not token or not hmac.compare_digest(token, str(sess.get("csrf", ""))):
        raise HTTPException(status_code=403, detail="CSRF check failed. Reload the page and try again.")
    if not origin_ok(request.headers):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected.")
    return sess


# --------------------------------------------------------------------------- rate limiting


class LoginRateLimiter:
    def __init__(self, max_failures: int = MAX_FAILURES, window: float = FAILURE_WINDOW):
        self.max_failures = max_failures
        self.window = window
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._fails.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def retry_after(self, key: str) -> int:
        """Seconds until another attempt is allowed (0 = allowed now)."""
        now = time.monotonic()
        with self._lock:
            q = self._prune(key, now)
            if len(q) < self.max_failures:
                return 0
            return max(1, int(self.window - (now - q[0])) + 1)

    def failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now).append(now)
            if len(self._fails) > 10000:  # bound memory
                for k in [k for k, v in self._fails.items() if not v][:5000]:
                    self._fails.pop(k, None)

    def success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()


limiter = LoginRateLimiter()


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _safe_next(next_url: str | None) -> str:
    if not next_url or not next_url.startswith("/") or next_url.startswith("//") or "\\" in next_url:
        return "/"
    return next_url


def is_default_password() -> bool:
    return str(settings().DASHBOARD_PASSWORD or "") == DEFAULT_PASSWORD


# --------------------------------------------------------------------------- routes


def _login_page(request: Request, error: str | None = None, status_code: int = 200, next_url: str = "/"):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "next": _safe_next(next_url), "default_password": is_default_password()},
        status_code=status_code,
    )


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    if read_session(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse(_safe_next(next), status_code=303)
    return _login_page(request, next_url=next)


@router.post("/login")
def login_submit(request: Request, password: str = Form(""), next: str = Form("/")):
    if not origin_ok(request.headers):
        return _login_page(request, "Cross-origin login rejected.", 403, next)
    key = _client_key(request)
    wait = limiter.retry_after(key)
    if wait:
        resp = _login_page(
            request, f"Too many failed attempts. Try again in {wait} seconds.", 429, next
        )
        resp.headers["Retry-After"] = str(wait)
        return resp
    expected = str(settings().DASHBOARD_PASSWORD or "")
    ok = bool(password) and bool(expected) and hmac.compare_digest(
        password.encode("utf-8"), expected.encode("utf-8")
    )
    if not ok:
        limiter.failure(key)
        return _login_page(request, "Wrong password.", 401, next)
    limiter.success(key)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    set_session_cookie(resp, request)
    return resp


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    sess = read_session(request.cookies.get(COOKIE_NAME))
    token = csrf or request.headers.get("x-csrf-token", "")
    if sess and not hmac.compare_digest(token, str(sess.get("csrf", ""))):
        raise HTTPException(status_code=403, detail="CSRF check failed.")
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax")
    return resp
