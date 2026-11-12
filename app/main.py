"""FastAPI application: ``uvicorn app.main:app``."""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.web import auth, routes, vnc
from app.web.deps import maybe_await, mod
from app.web.templating import STATIC_DIR

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mod("db").init_db()
    try:
        await maybe_await(mod("scheduler").start())
    except Exception:
        log.exception("scheduler failed to start; the dashboard keeps running")
    try:
        yield
    finally:
        try:
            await maybe_await(mod("scheduler").shutdown())
        except Exception:
            log.exception("scheduler shutdown failed")
        # Only stop the browser if something actually used it during this run.
        snap = sys.modules.get("app.snapchat")
        if snap is not None and hasattr(snap, "get_client"):
            try:
                await asyncio.wait_for(maybe_await(snap.get_client().stop()), 20)
            except Exception:
                log.exception("snapchat client stop failed")


def _wants_json(request: Request) -> bool:
    path = request.url.path
    return path.startswith("/api/") or path.startswith("/images/") or \
        "application/json" in request.headers.get("accept", "")


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "same-origin",
}
PAGE_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; "
    "base-uri 'self'; form-action 'self'"
)


def create_app() -> FastAPI:
    app = FastAPI(title="Snapchat Saver", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.exception_handler(auth.NotAuthenticated)
    async def _not_auth(request: Request, exc: auth.NotAuthenticated):
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": "Not logged in."}, status_code=401)
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse({"ok": False, "error": detail, "detail": detail},
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        msgs = []
        for e in exc.errors():
            loc = ".".join(str(p) for p in e.get("loc", []) if p != "body")
            msgs.append(f"{loc}: {e.get('msg')}" if loc else str(e.get("msg")))
        text = "; ".join(msgs) or "Invalid request."
        return JSONResponse({"ok": False, "error": text, "detail": text}, status_code=422)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        path = request.url.path
        if not path.startswith("/novnc"):
            response.headers.setdefault("Content-Security-Policy", PAGE_CSP)
        if path.startswith("/api/") or path in ("/", "/login"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    app.include_router(auth.router)
    app.include_router(vnc.router)  # websocket routes before the /novnc mount
    app.include_router(routes.pages)
    app.include_router(routes.api)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.mount("/novnc", vnc.NoVNCStatic(), name="novnc")
    return app


app = create_app()
