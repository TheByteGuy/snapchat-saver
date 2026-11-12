import httpx
import pytest
from PIL import Image

from app import config, db, facts, imagegen


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.reload_settings()
    db.init_db()
    yield tmp_path
    config.reload_settings()


def _mock_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(facts.httpx, "AsyncClient", factory)


def test_bundled_list_is_large_and_clean():
    items = facts.load_bundled_facts()
    assert len(items) >= 200
    assert len(set(items)) == len(items)
    assert all(facts.MIN_LEN <= len(f) <= facts.MAX_LEN for f in items)


async def test_api_fact_used(monkeypatch):
    _mock_transport(monkeypatch, lambda req: httpx.Response(200, json={"text": "Ducks  have  waterproof feathers"}))
    assert await facts.get_fact() == "Ducks have waterproof feathers."


async def test_api_rejects_long_and_used_then_accepts(monkeypatch):
    db.mark_fact_used("Used fact here, already sent.")
    answers = iter(["x" * 300, "Used fact here, already sent.", "A fresh and short fact."])
    _mock_transport(monkeypatch, lambda req: httpx.Response(200, json={"text": next(answers)}))
    assert await facts.get_fact() == "A fresh and short fact."


async def test_fallback_on_network_error(monkeypatch):
    def boom(req):
        raise httpx.ConnectError("no network")

    _mock_transport(monkeypatch, boom)
    fact = await facts.get_fact()
    assert fact in facts.load_bundled_facts()


async def test_fallback_on_http_error_and_skips_used(monkeypatch):
    _mock_transport(monkeypatch, lambda req: httpx.Response(403, text="forbidden"))
    bundled = facts.load_bundled_facts()
    for f in bundled[:-1]:
        db.mark_fact_used(f)
    assert await facts.get_fact() == bundled[-1]


async def test_fallback_reuses_least_recent_when_all_used(monkeypatch):
    _mock_transport(monkeypatch, lambda req: httpx.Response(500))
    bundled = facts.load_bundled_facts()
    for f in bundled[1:]:
        db.mark_fact_used(f)
    db.mark_fact_used(bundled[0])  # most recent now
    assert await facts.get_fact() == bundled[1]


async def test_get_fact_never_raises(monkeypatch):
    async def broken():
        raise RuntimeError("x")

    monkeypatch.setattr(facts, "_from_api", broken)
    monkeypatch.setattr(facts, "FACTS_FILE", facts.FACTS_FILE.with_name("missing.txt"))
    assert isinstance(await facts.get_fact(), str)


def _distinct_colors(img: Image.Image) -> int:
    return len(img.resize((108, 192)).getcolors(maxcolors=1 << 20) or [])


@pytest.mark.parametrize("fact", [
    "Wow.",
    "Octopuses have three hearts and blue blood.",
    "In 1947, engineers found a real moth stuck in a Harvard computer and taped it into their logbook as the "
    "first actual case of a bug being found, which is why debugging is called debugging today, more or less.",
    "Pneumonoultramicroscopicsilicovolcanoconiosis" * 4,
])
def test_render_sizes(tmp_path, fact):
    out = imagegen.render_fact_image(fact, tmp_path / "sub" / "img.png", seed=3, date_text="Thursday, Nov 12")
    assert out.exists()
    img = Image.open(out)
    assert img.size == (1080, 1920)
    assert img.format == "PNG"
    assert _distinct_colors(img.convert("RGB")) > 500  # gradient + text, not a flat fill


def test_text_stays_in_safe_area(tmp_path, monkeypatch):
    calls = []
    real = imagegen._fit_text

    def spy(text, box_w, box_h, draw, **kw):
        res = real(text, box_w, box_h, draw, **kw)
        calls.append((box_w, box_h, res))
        return res

    monkeypatch.setattr(imagegen, "_fit_text", spy)
    imagegen.render_fact_image("Bananas are berries, but strawberries are not. " * 4, tmp_path / "a.png", seed=1)
    box_w, box_h, (font, lines, line_h) = calls[0]
    assert box_w <= imagegen.SAFE_W and box_h <= imagegen.SAFE_H
    assert line_h * len(lines) <= box_h


def test_short_fact_gets_bigger_font(tmp_path, monkeypatch):
    sizes = []
    real = imagegen._fit_text
    monkeypatch.setattr(imagegen, "_fit_text",
                        lambda *a, **k: (lambda r: (sizes.append(r[0].size), r)[1])(real(*a, **k)))
    imagegen.render_fact_image("Short fact.", tmp_path / "s.png", seed=1)
    imagegen.render_fact_image("A much longer fact " * 10, tmp_path / "l.png", seed=1)
    assert sizes[0] > sizes[1]
