"""Playwright driver for Snapchat Web (https://www.snapchat.com/web).

There is no official API for sending snaps, so this drives the real web client in
a persistent Chromium profile (the user logs in by hand through the VNC view;
cookies persist in PROFILE_DIR). The camera is Chromium's fake capture device
fed from CAMERA_DIR/feed.y4m (see camera.py).

Environment knobs (all optional):
  SNAP_WEB_URL         base URL (default https://www.snapchat.com/web; tests point it at a local fixture)
  SNAP_CHROMIUM_PATH   Chromium/Chrome executable (default: Playwright's bundled browser)
  SNAP_STEP_TIMEOUT    seconds to wait for each UI step (default 20)
  SNAP_HUMAN_DELAY     multiplier for small random pauses between actions (default 1, 0 disables)
  SNAP_LOCALE          browser locale (default en-US); timezone comes from TZ
  SNAP_USER_AGENT      override the user agent (default: Chromium's own)
  SNAP_MIRROR_FEED / SNAP_FEED_LAYOUT / SNAP_FEED_SCALE   see camera.py
"""
from __future__ import annotations

import asyncio
import functools
import io
import glob
import logging
import os
import random
import shlex
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import camera
from .camera import ensure_placeholder_feed, feed_path, prepare_camera_feed
from .errors import NotLoggedInError, SendResult, StepError
from .selectors import SelectorStore

log = logging.getLogger("snapchat")

DEFAULT_WEB_URL = "https://www.snapchat.com/web"
SNAPCHAT_DOMAIN = "snapchat.com"
DEBUG_KEEP = 60  # debug artefact pairs kept in DEBUG_DIR
IGNORED_CHAT_NAMES = {"my ai", "team snapchat"}
_CHAT_NOISE = {
    "new chat", "chat", "chats", "stories", "spotlight", "camera", "search", "new snap", "received",
    "delivered", "opened", "sent", "typing...", "tap to chat", "new",
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def web_url() -> str:
    return os.environ.get("SNAP_WEB_URL", "").strip() or DEFAULT_WEB_URL


def _find_bundled_chromium() -> str | None:
    """Locate a Playwright-managed Chromium when the exact revision Playwright expects is absent."""
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), os.path.expanduser("~/.cache/ms-playwright")]
    found: list[str] = []
    for root in filter(None, roots):
        for pattern in ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-linux/chrome"):
            found.extend(glob.glob(os.path.join(root, pattern)))
    found = sorted(set(p for p in found if os.access(p, os.X_OK)))
    if found:
        return found[-1]
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _serialized(fn):
    """Run the method on the loop that owns the browser, one call at a time."""

    @functools.wraps(fn)
    async def wrapper(self: "SnapchatClient", *args, **kwargs):
        loop = asyncio.get_running_loop()
        owner = self._loop
        if owner is not None and owner is not loop:
            if owner.is_running() and not owner.is_closed():
                fut = asyncio.run_coroutine_threadsafe(wrapper(self, *args, **kwargs), owner)
                return await asyncio.wrap_future(fut)
            log.warning("event loop that owned the browser is gone; forgetting the old browser")
            self._forget()
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            return await fn(self, *args, **kwargs)

    return wrapper


class SnapchatClient:
    def __init__(self) -> None:
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._logged_in: bool | None = None
        self.last_error: str | None = None
        self._selectors: SelectorStore | None = None

    # ------------------------------------------------------------------ settings helpers
    @staticmethod
    def _settings():
        from app.config import get_settings

        return get_settings()

    @property
    def selectors(self) -> SelectorStore:
        path = Path(self._settings().DATA_DIR) / "selectors.json"
        if self._selectors is None or self._selectors.override_path != path:
            self._selectors = SelectorStore(path)
        return self._selectors

    @staticmethod
    def _step_timeout() -> float:
        return max(1.0, _env_float("SNAP_STEP_TIMEOUT", 20.0))

    async def _human_pause(self, low: float = 0.3, high: float = 0.9) -> None:
        factor = max(0.0, _env_float("SNAP_HUMAN_DELAY", 1.0))
        if factor:
            await asyncio.sleep(random.uniform(low, high) * factor)

    async def _human_click(self, page, loc, timeout: float | None = None) -> None:
        """Click the way a person does: glide the pointer to a spot inside the element
        (not its exact centre), settle briefly, then press and release with a short hold."""
        kwargs: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        factor = max(0.0, _env_float("SNAP_HUMAN_DELAY", 1.0))
        if factor <= 0:
            await loc.click(**kwargs)
            return
        try:
            await loc.scroll_into_view_if_needed(timeout=3000)
            box = await loc.bounding_box()
            if not box or box["width"] < 4 or box["height"] < 4:
                raise ValueError("no usable box")
            dx = box["width"] * random.uniform(0.3, 0.7)
            dy = box["height"] * random.uniform(0.3, 0.7)
            await page.mouse.move(box["x"] + dx, box["y"] + dy, steps=random.randint(9, 22))
            await asyncio.sleep(random.uniform(0.08, 0.3) * factor)
            await loc.click(position={"x": dx, "y": dy}, delay=random.uniform(45, 130), **kwargs)
        except Exception as exc:
            if self._is_target_closed(exc):
                raise
            log.debug("human click fell back to a plain click: %s", exc)
            await loc.click(**kwargs)

    async def _human_type(self, loc, text: str) -> None:
        """Type with uneven gaps between keys instead of a fixed machine rhythm."""
        factor = max(0.0, _env_float("SNAP_HUMAN_DELAY", 1.0))
        if factor <= 0:
            await loc.press_sequentially(text)
            return
        for ch in text:
            await loc.press_sequentially(ch)
            gap = random.uniform(0.05, 0.17)
            if ch == " " or random.random() < 0.06:
                gap += random.uniform(0.08, 0.3)
            await asyncio.sleep(gap * factor)

    # ------------------------------------------------------------------ lifecycle
    def _alive(self) -> bool:
        if self._context is None:
            return False
        try:
            browser = self._context.browser
            if browser is not None and not browser.is_connected():
                return False
        except Exception:
            return False
        return True

    def _forget(self) -> None:
        self._pw = self._context = self._page = None
        self._loop = None
        self._lock = None

    def _on_context_close(self, *_: Any) -> None:
        if self._context is None:  # we closed it ourselves in _shutdown()
            return
        log.warning("browser closed unexpectedly; it will be restarted on the next call")
        self._context = None
        self._page = None

    def _on_page_crash(self, *_: Any) -> None:
        log.error("browser page crashed; it will be replaced on the next call")
        self.last_error = "Browser page crashed"
        self._page = None

    @_serialized
    async def start(self) -> None:
        await self._ensure_started()

    async def _ensure_started(self) -> None:
        if self._alive():
            return
        if self._context is not None or self._pw is not None:
            await self._shutdown()
        from playwright.async_api import async_playwright

        s = self._settings()
        profile = Path(s.PROFILE_DIR)
        camera_dir = Path(s.CAMERA_DIR)
        ensure_placeholder_feed(camera_dir)
        # Stale singleton locks from a crashed Chromium would block the launch; we are the only user.
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (profile / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.debug("could not remove %s: %s", name, exc)

        headless = bool(s.HEADLESS)
        args = [
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-video-capture={feed_path(camera_dir)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--disable-dev-shm-usage",
            # Behave like a normally started browser: no "controlled by automated test
            # software" banner and no navigator.webdriver marker.
            "--disable-blink-features=AutomationControlled",
        ]
        kwargs: dict[str, Any] = {
            "headless": headless,
            "args": args,
            "locale": os.environ.get("SNAP_LOCALE", "en-US"),
            "accept_downloads": False,
            "ignore_default_args": ["--enable-automation"],
        }
        tz = getattr(s, "DEFAULT_TZ", None) or os.environ.get("TZ")
        if tz and self._valid_tz(tz):
            kwargs["timezone_id"] = tz
        if os.environ.get("SNAP_USER_AGENT"):
            kwargs["user_agent"] = os.environ["SNAP_USER_AGENT"]
        if headless:
            kwargs["viewport"] = {"width": 1280, "height": 800}
        else:
            # Headed inside Xvfb: let the page follow the real window size the user sees over VNC.
            args.extend(["--window-size=1280,860", "--window-position=0,0"])
            kwargs["no_viewport"] = True
            env = dict(os.environ)
            env["DISPLAY"] = s.DISPLAY
            kwargs["env"] = env
        extra = os.environ.get("SNAP_CHROMIUM_ARGS", "").strip()
        if extra:
            args.extend(shlex.split(extra))
        exe = os.environ.get("SNAP_CHROMIUM_PATH", "").strip()
        if exe:
            kwargs["executable_path"] = exe

        self._pw = await async_playwright().start()
        try:
            try:
                ctx = await self._pw.chromium.launch_persistent_context(str(profile), **kwargs)
            except Exception as exc:
                fallback = None if exe else _find_bundled_chromium()
                if not fallback or "Executable doesn't exist" not in str(exc):
                    raise
                log.warning("Playwright's expected browser is missing; using %s", fallback)
                kwargs["executable_path"] = fallback
                ctx = await self._pw.chromium.launch_persistent_context(str(profile), **kwargs)
        except Exception as exc:
            self.last_error = f"Could not launch Chromium: {exc}"
            await self._pw.stop()
            self._pw = None
            raise
        self._context = ctx
        self._loop = asyncio.get_running_loop()
        ctx.on("close", self._on_context_close)
        try:
            origin = "{0.scheme}://{0.netloc}".format(urlparse(web_url()))
            await ctx.grant_permissions(["camera", "microphone"], origin=origin)
            if urlparse(web_url()).hostname != "www.snapchat.com":
                await ctx.grant_permissions(["camera", "microphone"], origin="https://www.snapchat.com")
        except Exception as exc:  # fake-ui flag auto-accepts anyway
            log.warning("could not pre-grant camera permission: %s", exc)
        log.info("Chromium started (headless=%s, profile=%s)", headless, profile)

    @staticmethod
    def _valid_tz(tz: str) -> bool:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(tz)
            return True
        except Exception:
            return False

    async def _get_page(self):
        await self._ensure_started()
        page = self._page
        if page is not None and not page.is_closed():
            return page
        pages = [p for p in self._context.pages if not p.is_closed()]
        page = pages[0] if pages else await self._context.new_page()
        page.on("crash", self._on_page_crash)
        page.set_default_timeout(self._step_timeout() * 1000)
        self._page = page
        return page

    async def _shutdown(self) -> None:
        ctx, pw = self._context, self._pw
        self._context = self._page = None
        self._pw = None
        if ctx is not None:
            try:
                await ctx.close()
            except Exception as exc:
                log.debug("context close: %s", exc)
        if pw is not None:
            try:
                await pw.stop()
            except Exception as exc:
                log.debug("playwright stop: %s", exc)

    @_serialized
    async def stop(self) -> None:
        await self._shutdown()
        self._loop = None
        log.info("Chromium stopped")

    # ------------------------------------------------------------------ generic helpers
    def _is_snapchat_url(self, url: str | None) -> bool:
        if not url:
            return False
        host = (urlparse(url).hostname or "").lower()
        base = (urlparse(web_url()).hostname or "").lower()
        return host == base or host == SNAPCHAT_DOMAIN or host.endswith("." + SNAPCHAT_DOMAIN)

    async def _first_visible(self, page, step: str, candidates: list[str], root=None):
        root = page if root is None else root
        for idx, cand in enumerate(candidates):
            try:
                loc = root.locator(f"{cand} >> visible=true").first
                if await loc.count():
                    return loc, idx, cand
            except Exception as exc:  # invalid selector from selectors.json, detached frame...
                log.debug("step=%s candidate #%d %r errored: %s", step, idx + 1, cand, exc)
        return None

    async def _find(self, page, step: str, *, name: str | None = None, timeout: float | None = None, root=None):
        candidates = self.selectors.get(step, name)
        deadline = time.monotonic() + (self._step_timeout() if timeout is None else timeout)
        while True:
            hit = await self._first_visible(page, step, candidates, root)
            if hit:
                loc, idx, cand = hit
                log.info("step=%s matched candidate #%d: %s", step, idx + 1, cand)
                return loc
            if time.monotonic() >= deadline:
                raise StepError(step, candidates, f"waited {timeout or self._step_timeout():.0f}s")
            await asyncio.sleep(0.25)

    async def _click(self, page, step: str, *, name: str | None = None, timeout: float | None = None):
        loc = await self._find(page, step, name=name, timeout=timeout)
        await self._human_pause()
        await self._human_click(page, loc)
        return loc

    async def _dismiss_popups(self, page) -> int:
        dismissed = 0
        for _ in range(3):
            hit = await self._first_visible(page, "popup_dismiss", self.selectors.get("popup_dismiss"))
            if not hit:
                break
            loc, idx, cand = hit
            try:
                label = (await loc.inner_text(timeout=1000)).strip()
                await self._human_click(page, loc, timeout=3000)
                dismissed += 1
                log.info("step=popup_dismiss clicked %r via candidate #%d: %s", label, idx + 1, cand)
                await asyncio.sleep(0.4)
            except Exception as exc:
                log.debug("popup dismiss failed: %s", exc)
                break
        return dismissed

    async def _goto_web(self, page) -> None:
        url = web_url()
        log.info("navigating to %s", url)
        await page.goto(url, wait_until="domcontentloaded", timeout=max(30.0, self._step_timeout()) * 1000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # Snapchat keeps long-polling connections open; domcontentloaded is enough

    async def _detect_login(self, page, timeout: float) -> bool | None:
        """True/False when a marker is seen, None if neither appears within timeout."""
        deadline = time.monotonic() + timeout
        while True:
            host = (urlparse(page.url).hostname or "").lower()
            if host.startswith("accounts."):
                log.info("login state: logged out (on %s)", host)
                return False
            out = await self._first_visible(page, "logged_out", self.selectors.get("logged_out"))
            if out:
                log.info("login state: logged out (matched logged_out #%d: %s)", out[1] + 1, out[2])
                return False
            inn = await self._first_visible(page, "logged_in", self.selectors.get("logged_in"))
            if inn:
                log.info("login state: logged in (matched logged_in #%d: %s)", inn[1] + 1, inn[2])
                return True
            if time.monotonic() >= deadline:
                log.warning("login state: unknown (no logged_in/logged_out marker at %s)", page.url)
                return None
            await asyncio.sleep(0.5)

    async def save_debug(self, label: str, page=None) -> Path | None:
        """Save a screenshot + HTML of the current page into DEBUG_DIR. Never raises."""
        try:
            debug_dir = Path(self._settings().DEBUG_DIR)
            debug_dir.mkdir(parents=True, exist_ok=True)
            page = page or self._page
            if page is None or page.is_closed():
                return None
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
            stem = debug_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe}"
            try:
                await page.screenshot(path=str(stem.with_suffix(".png")), full_page=False, timeout=10000)
            except Exception as exc:
                log.warning("debug screenshot failed: %s", exc)
            try:
                html = await page.content()
                stem.with_suffix(".html").write_text(f"<!-- url: {page.url} -->\n{html}", encoding="utf-8")
            except Exception as exc:
                log.warning("debug HTML dump failed: %s", exc)
            try:
                stem.with_suffix(".txt").write_text(await self._page_report(page), encoding="utf-8")
            except Exception as exc:
                log.warning("debug report failed: %s", exc)
            log.info("saved debug artefacts %s.{png,html,txt}", stem)
            self._prune_debug(debug_dir)
            return stem
        except Exception as exc:
            log.warning("could not save debug artefacts: %s", exc)
            return None

    async def _page_report(self, page) -> str:
        """A compact text list of what is clickable on the page, for fixing selectors remotely.
        Contains labels and layout only, no message contents beyond short button texts."""
        items = await page.evaluate(
            """() => {
              const out = [];
              const sel = 'button, [role=button], a[href], input, textarea, video, canvas, [aria-label], [title], [role=dialog], [role=listitem], [role=option]';
              for (const el of document.querySelectorAll(sel)) {
                const r = el.getBoundingClientRect();
                if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.top > innerHeight) continue;
                const cs = getComputedStyle(el);
                if (cs.visibility === 'hidden' || cs.display === 'none') continue;
                const role = el.getAttribute('role') || '';
                const chat = role === 'listitem' || role === 'option';
                out.push([el.tagName.toLowerCase(), role, el.getAttribute('aria-label') || '',
                  el.getAttribute('title') || '', el.getAttribute('placeholder') || '', el.getAttribute('type') || '',
                  chat ? '(row)' : (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 40),
                  (typeof el.className === 'string' ? el.className : '').slice(0, 70),
                  el.id || '', Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height),
                  el.tagName === 'VIDEO' ? `video ${el.videoWidth}x${el.videoHeight} fit=${cs.objectFit} transform=${cs.transform}` : '']);
                if (out.length >= 400) break;
              }
              return {url: location.href, w: innerWidth, h: innerHeight, items: out};
            }"""
        )
        lines = [f"url: {items['url']}", f"viewport: {items['w']}x{items['h']}",
                 "tag | role | aria-label | title | placeholder | type | text | class | id | x,y,w,h | extra"]
        for it in items["items"]:
            lines.append(" | ".join(str(v) for v in it[:9]) + f" | {it[9]},{it[10]},{it[11]},{it[12]} | {it[13]}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _prune_debug(debug_dir: Path) -> None:
        files = sorted(debug_dir.glob("*.png")) + sorted(debug_dir.glob("*.html"))
        stems = sorted({f.with_suffix("") for f in files})
        for stem in stems[:-DEBUG_KEEP]:
            for ext in (".png", ".html", ".txt"):
                try:
                    stem.with_suffix(ext).unlink()
                except OSError:
                    pass

    def _fail(self, message: str) -> None:
        self.last_error = message
        log.error(message)

    # ------------------------------------------------------------------ public API
    async def status(self) -> dict:
        """Cheap status; never launches the browser."""
        url = None
        page = self._page
        if self._alive() and page is not None:
            try:
                url = None if page.is_closed() else page.url
            except Exception:
                url = None
        return {
            "running": self._alive(),
            "logged_in": self._logged_in,
            "url": url,
            "last_error": self.last_error,
        }

    @_serialized
    async def is_logged_in(self) -> bool:
        for attempt in (1, 2):
            try:
                page = await self._get_page()
                if not self._is_snapchat_url(page.url):
                    await self._goto_web(page)
                else:
                    log.info("inspecting current page %s without navigating", page.url)
                state = await self._detect_login(page, timeout=self._step_timeout())
                self._logged_in = bool(state)
                return self._logged_in
            except Exception as exc:
                if attempt == 1 and self._is_target_closed(exc):
                    log.warning("browser went away (%s); restarting", exc)
                    await self._shutdown()
                    continue
                self._fail(f"is_logged_in failed: {exc}")
                await self.save_debug("is_logged_in")
                return False
        return False

    @_serialized
    async def open_login(self) -> None:
        page = await self._get_page()
        await self._goto_web(page)
        try:
            await page.bring_to_front()
        except Exception:
            pass
        await self._dismiss_popups(page)

    @_serialized
    async def screenshot(self) -> bytes:
        page = await self._get_page()
        return await page.screenshot(type="png")

    @_serialized
    async def list_friends(self) -> list[str]:
        for attempt in (1, 2):
            try:
                return await self._list_friends()
            except Exception as exc:
                if attempt == 1 and self._is_target_closed(exc):
                    await self._shutdown()
                    continue
                self._fail(f"list_friends failed: {exc}")
                await self.save_debug("list_friends")
                return []
        return []

    async def _list_friends(self) -> list[str]:
        page = await self._get_page()
        if not self._is_snapchat_url(page.url) or (urlparse(page.url).hostname or "").startswith("accounts."):
            await self._goto_web(page)
        state = await self._detect_login(page, timeout=self._step_timeout())
        self._logged_in = bool(state)
        if not state:
            log.info("list_friends: not logged in")
            return []
        await self._dismiss_popups(page)
        try:
            await self._find(page, "chat_list_item", timeout=self._step_timeout())
        except StepError as exc:
            self._fail(str(exc))
            await self.save_debug("list_friends")
            return []
        names: list[str] = []
        seen: set[str] = set()
        for round_ in range(6):
            before = len(names)
            for name in await self._scrape_chat_names(page):
                key = name.casefold()
                if key not in seen:
                    seen.add(key)
                    names.append(name)
            if round_ and len(names) == before:
                break
            await self._scroll_chat_list(page)
            await asyncio.sleep(0.6)
        log.info("list_friends: %d names", len(names))
        return names

    async def _scrape_chat_names(self, page) -> list[str]:
        hit = await self._first_visible(page, "chat_list_item", self.selectors.get("chat_list_item"))
        if not hit:
            return []
        _, _, cand = hit
        texts: list[str] = await page.locator(f"{cand} >> visible=true").evaluate_all(
            "els => els.map(e => e.innerText || '')"
        )
        out = []
        for text in texts:
            name = self._name_from_row(text)
            if name:
                out.append(name)
        return out

    @staticmethod
    def _name_from_row(text: str) -> str | None:
        for line in (ln.strip() for ln in text.splitlines()):
            if not line or line.casefold() in _CHAT_NOISE or len(line) > 64:
                continue
            if line.casefold() in IGNORED_CHAT_NAMES:
                return None
            return line
        return None

    async def _scroll_chat_list(self, page) -> None:
        hit = await self._first_visible(page, "chat_list_scroll", self.selectors.get("chat_list_scroll"))
        if not hit:
            return
        try:
            await hit[0].evaluate(
                """el => { let n = el; while (n && n !== document.body) {
                       if (n.scrollHeight > n.clientHeight + 4) { n.scrollTop += n.clientHeight * 0.8; return; }
                       n = n.parentElement; } window.scrollBy(0, 600); }"""
            )
        except Exception as exc:
            log.debug("chat list scroll failed: %s", exc)

    @staticmethod
    def _is_target_closed(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(s in msg for s in ("target closed", "has been closed", "browser closed", "crashed", "connection closed"))

    # ------------------------------------------------------------------ sending
    @_serialized
    async def send_snap(self, image_path: Path, recipients: list[str]) -> dict[str, SendResult]:
        # De-duplicate while keeping order.
        wanted: list[str] = []
        for r in recipients:
            r = (r or "").strip()
            if r and r not in wanted:
                wanted.append(r)
        if not wanted:
            return {}
        image_path = Path(image_path)
        if not image_path.is_file():
            msg = f"Image not found: {image_path}"
            self._fail(msg)
            return {r: SendResult(False, msg) for r in wanted}

        camera_dir = Path(self._settings().CAMERA_DIR)
        calibrate = camera.needs_calibration(camera_dir)
        try:
            if calibrate:
                camera.write_calibration_feed(camera_dir)
                log.info("camera feed set to the calibration pattern")
            else:
                prepare_camera_feed(image_path, camera_dir)
                log.info("camera feed prepared from %s", image_path)
        except Exception as exc:
            msg = f"Could not prepare camera feed: {exc}"
            self._fail(msg)
            return {r: SendResult(False, msg) for r in wanted}

        page = None
        try:
            page = await self._get_page()
            # Always (re)load so the camera is opened fresh and reads the new feed file.
            await self._goto_web(page)
            await self._dismiss_popups(page)
            state = await self._detect_login(page, timeout=self._step_timeout())
            if state is False:
                self._logged_in = False
                raise NotLoggedInError("Snapchat Web is logged out; log in again from the dashboard")
            if state is None:
                raise StepError("logged_in", self.selectors.get("logged_in"), "could not tell whether logged in")
            self._logged_in = True
            await self._open_camera(page)
            if calibrate:
                await self._calibrate_camera(page, camera_dir)
                prepare_camera_feed(image_path, camera_dir)
                log.info("camera feed prepared from %s", image_path)
                await self._goto_web(page)  # reopen the camera so it reads the real image
                await self._dismiss_popups(page)
                await self._open_camera(page)
            await self._human_pause(1.0, 2.0)  # let a few frames render
            await self._press_shutter(page)
            await self._dismiss_popups(page)
            await self._click(page, "send_to")
            await self._human_pause()
        except NotLoggedInError as exc:
            self._fail(str(exc))
            await self.save_debug("send_not_logged_in", page)
            raise
        except Exception as exc:
            msg = f"Send failed before choosing recipients: {exc}"
            self._fail(msg)
            await self.save_debug("send_prepare", page)
            if self._is_target_closed(exc):
                await self._shutdown()
            return {r: SendResult(False, msg) for r in wanted}

        results: dict[str, SendResult] = {}
        selected: list[str] = []
        for name in wanted:
            try:
                await self._select_recipient(page, name)
                selected.append(name)
            except StepError as exc:
                log.warning("recipient %r not found in picker: %s", name, exc)
                results[name] = SendResult(False, f"Recipient '{name}' was not found in Snapchat's send list")
            except Exception as exc:
                results[name] = SendResult(False, f"Could not select recipient '{name}': {exc}")
                if self._is_target_closed(exc):
                    break
        for name in wanted:
            if name not in results and name not in selected:
                results[name] = SendResult(False, "Browser closed while choosing recipients")

        if not selected:
            self._fail("No recipient could be selected: " + "; ".join(r.error or "" for r in results.values()))
            await self.save_debug("send_no_recipients", page)
            return {r: results[r] for r in wanted}
        if len(selected) < len(wanted):
            await self.save_debug("send_missing_recipient", page)

        try:
            send_btn = await self._click(page, "send_button")
            confirmed = await self._confirm_sent(page, send_btn)
        except Exception as exc:
            msg = f"Could not press Send: {exc}"
            self._fail(msg)
            await self.save_debug("send_button", page)
            for name in selected:
                results[name] = SendResult(False, msg)
            return {r: results[r] for r in wanted}

        if confirmed:
            for name in selected:
                results[name] = SendResult(True, None)
            log.info("snap sent to %s", ", ".join(selected))
            if len(selected) == len(wanted):
                self.last_error = None
            else:
                self.last_error = "Some recipients were not found: " + ", ".join(
                    n for n in wanted if not results[n].ok
                )
        else:
            msg = "Send was clicked but Snapchat did not confirm (picker stayed open); the snap may not have been sent"
            self._fail(msg)
            await self.save_debug("send_unconfirmed", page)
            for name in selected:
                results[name] = SendResult(False, msg)
        return {r: results[r] for r in wanted}

    async def _open_camera(self, page) -> None:
        if await self._video_playing(page):
            log.info("step=open_camera camera already live")
            return
        await self._click(page, "open_camera")
        deadline = time.monotonic() + self._step_timeout()
        while time.monotonic() < deadline:
            if await self._video_playing(page):
                log.info("step=open_camera camera live")
                return
            await self._dismiss_popups(page)
            await asyncio.sleep(0.4)
        raise StepError("camera_video", self.selectors.get("camera_video"), "camera preview never started")

    async def _camera_box(self, page) -> dict | None:
        """Bounding box of the camera pane: the largest visible <video> or <canvas>."""
        try:
            return await page.evaluate(
                """() => {
                  let best = null;
                  for (const el of document.querySelectorAll('video, canvas')) {
                    const r = el.getBoundingClientRect();
                    const x0 = Math.max(0, r.left), y0 = Math.max(0, r.top);
                    const x1 = Math.min(innerWidth, r.right), y1 = Math.min(innerHeight, r.bottom);
                    if (x1 - x0 < 60 || y1 - y0 < 60) continue;
                    const cs = getComputedStyle(el);
                    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
                    const area = (x1 - x0) * (y1 - y0);
                    if (!best || area > best.area) best = {x: x0, y: y0, width: x1 - x0, height: y1 - y0, area};
                  }
                  return best;
                }"""
            )
        except Exception as exc:
            log.debug("camera box lookup failed: %s", exc)
            return None

    async def _calibrate_camera(self, page, camera_dir: Path) -> None:
        """Measure which part of the camera frame Snapchat's pane shows. Never raises."""
        try:
            await asyncio.sleep(1.5)  # let the pattern reach the screen
            box = await self._camera_box(page)
            if not box:
                raise RuntimeError("camera pane not found")
            clip = {k: box[k] for k in ("x", "y", "width", "height")}
            from PIL import Image

            result = None
            for _ in range(3):
                shot = Image.open(io.BytesIO(await page.screenshot(clip=clip, type="png")))
                result = camera.decode_calibration(shot, camera._frame_size())
                if result:
                    break
                await asyncio.sleep(1.0)
            if not result:
                raise RuntimeError("calibration pattern not recognised in the camera pane")
            result["pane"] = [round(box["width"]), round(box["height"])]
            camera.save_calibration(result, camera_dir)
            log.info("camera calibrated: pane shows frame region %s (preview mirrored: %s)",
                     result["region"], result["preview_mirrored"])
        except Exception as exc:
            log.warning("camera calibration failed (%s); using the default placement", exc)
            await self.save_debug("calibration", page)

    async def _press_shutter(self, page) -> None:
        """Press the capture button; if no selector matches, click where it sits in the camera pane."""
        try:
            await self._click(page, "shutter", timeout=min(self._step_timeout(), 6.0))
            return
        except StepError as exc:
            box = await self._camera_box(page)
            if not box:
                raise
            fx, fy = 0.5, 0.914  # centre of the capture ring, measured from Snapchat Web
            raw = os.environ.get("SNAP_SHUTTER_POS", "").strip()
            if raw:
                try:
                    fx, fy = (float(v) for v in raw.split(","))
                except ValueError:
                    log.warning("ignoring invalid SNAP_SHUTTER_POS=%r (expected x,y fractions)", raw)
            x, y = box["x"] + box["width"] * fx, box["y"] + box["height"] * fy
            log.warning("step=shutter no selector matched (%s); clicking the camera pane at %.0f,%.0f instead",
                        exc.step, x, y)
            await page.mouse.move(x, y, steps=random.randint(9, 22))
            await self._human_pause(0.1, 0.3)
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.05, 0.12))
            await page.mouse.up()

    async def _video_playing(self, page) -> bool:
        for cand in self.selectors.get("camera_video"):
            try:
                ok = await page.locator(cand).evaluate_all(
                    "els => els.some(v => v.readyState >= 2 && v.videoWidth > 0 && !v.paused)"
                )
                if ok:
                    return True
            except Exception:
                continue
        return False

    async def _picker_root(self, page):
        hit = await self._first_visible(page, "picker_container", self.selectors.get("picker_container"))
        if hit:
            log.info("step=picker_container matched candidate #%d: %s", hit[1] + 1, hit[2])
            return hit[0]
        log.warning("step=picker_container: no candidate matched; searching recipients in the whole page")
        return None

    async def _select_recipient(self, page, name: str) -> None:
        root = await self._picker_root(page)
        search = None
        try:
            search = await self._find(page, "picker_search", timeout=min(5.0, self._step_timeout()), root=root)
        except StepError:
            log.info("no picker search box found; looking for %r in the visible list", name)
        if search is not None:
            await self._human_click(page, search)
            await search.fill("")
            await self._human_type(search, name)
            await asyncio.sleep(0.8)
        item = await self._find(
            page, "picker_recipient", name=name, timeout=min(10.0, self._step_timeout()), root=root
        )
        await self._human_pause()
        await self._human_click(page, item)
        log.info("selected recipient %r", name)
        if search is not None:
            await self._human_pause(0.2, 0.5)
            try:
                await search.fill("")
            except Exception:
                pass

    async def _confirm_sent(self, page, send_btn) -> bool:
        """The picker closes (Send button disappears) once Snapchat accepts the snap."""
        try:
            await send_btn.wait_for(state="hidden", timeout=self._step_timeout() * 1000)
            log.info("step=confirm picker closed after Send")
            return True
        except Exception:
            pass
        hit = await self._first_visible(page, "send_button", self.selectors.get("send_button"))
        return hit is None


_client: SnapchatClient | None = None


def get_client() -> SnapchatClient:
    global _client
    if _client is None:
        _client = SnapchatClient()
    return _client
