"""Render a "fact of the day" image (1080x1920 PNG) with Pillow only."""
from __future__ import annotations

import math
import random
from datetime import date as _date
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 1080, 1920
ASSETS = Path(__file__).parent / "assets"
FONT_BOLD = ASSETS / "DejaVuSans-Bold.ttf"
FONT_REGULAR = ASSETS / "DejaVuSans.ttf"

# Snapchat Web crops the camera preview, so everything important lives in the central
# ~70% of the width and ~60% of the height.
SAFE_W = int(WIDTH * 0.70)
SAFE_H = int(HEIGHT * 0.60)
SAFE_X0 = (WIDTH - SAFE_W) // 2
SAFE_Y0 = (HEIGHT - SAFE_H) // 2

# Curated palettes (3-4 stops each), chosen to keep white text readable.
PALETTES: list[list[tuple[int, int, int]]] = [
    [(255, 94, 98), (255, 153, 102), (255, 195, 113)],          # sunset
    [(67, 206, 162), (24, 90, 157), (35, 37, 104)],             # lagoon
    [(142, 45, 226), (74, 0, 224), (20, 20, 90)],               # ultraviolet
    [(252, 70, 107), (63, 94, 251), (32, 38, 120)],             # neon dusk
    [(17, 153, 142), (56, 239, 125), (12, 90, 80)],             # mint jungle
    [(255, 81, 47), (221, 36, 118), (90, 20, 110)],             # flamingo
    [(0, 198, 255), (0, 114, 255), (40, 30, 140)],              # ocean
    [(247, 151, 30), (255, 210, 0), (230, 92, 0)],              # mango
    [(238, 9, 121), (255, 106, 0), (120, 20, 90)],              # tropical
    [(43, 192, 228), (234, 236, 198), (60, 120, 170)],          # seaside
    [(131, 96, 195), (46, 191, 145), (25, 60, 90)],             # aurora
    [(255, 0, 132), (51, 0, 27), (120, 0, 80)],                 # berry
    [(252, 92, 125), (106, 130, 251), (60, 40, 140)],           # cotton candy
    [(29, 151, 108), (147, 249, 185), (16, 80, 70)],            # spring
    [(244, 107, 69), (238, 168, 73), (150, 50, 60)],            # autumn
    [(15, 32, 39), (32, 58, 67), (44, 83, 100), (90, 160, 180)],  # deep sea
]

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]


def format_date(d: _date) -> str:
    return f"{_DAYS[d.weekday()]}, {_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except OSError:
            return ImageFont.load_default(size)


def _lerp(a: tuple[int, ...], b: tuple[int, ...], t: float) -> tuple[int, ...]:
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _sample(stops: list[tuple[int, int, int]], t: float) -> tuple[int, ...]:
    t = min(1.0, max(0.0, t))
    n = len(stops) - 1
    pos = t * n
    i = min(int(pos), n - 1)
    local = pos - i
    local = local * local * (3 - 2 * local)  # smoothstep between stops
    return _lerp(stops[i], stops[i + 1], local)


def _gradient(rng: random.Random, stops: list[tuple[int, int, int]]) -> Image.Image:
    """Diagonal gradient mixed with a radial term, computed small and upscaled smoothly."""
    sw, sh = 108, 192
    angle = rng.uniform(0, 2 * math.pi)
    dx, dy = math.cos(angle), math.sin(angle)
    cx, cy = rng.uniform(0.2, 0.8), rng.uniform(0.2, 0.8)
    radial_w = rng.uniform(0.15, 0.4)
    # project corners to normalise the linear term to 0..1
    corners = [(0, 0), (1, 0), (0, 1), (1, 1)]
    proj = [x * dx + y * dy * (sh / sw) for x, y in corners]
    pmin, pmax = min(proj), max(proj)
    small = Image.new("RGB", (sw, sh))
    px = small.load()
    max_r = math.hypot(max(cx, 1 - cx), max(cy, 1 - cy) * sh / sw)
    for y in range(sh):
        fy = y / (sh - 1)
        for x in range(sw):
            fx = x / (sw - 1)
            lin = ((fx * dx + fy * dy * (sh / sw)) - pmin) / (pmax - pmin)
            rad = math.hypot(fx - cx, (fy - cy) * sh / sw) / max_r
            px[x, y] = _sample(stops, lin * (1 - radial_w) + rad * radial_w)
    return small.resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)


def _blobs(rng: random.Random, stops: list[tuple[int, int, int]]) -> Image.Image:
    """Soft, heavily blurred colour blobs for depth (RGBA layer)."""
    layer = Image.new("RGBA", (WIDTH // 4, HEIGHT // 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(rng.randint(4, 7)):
        r = rng.randint(40, 120)
        x, y = rng.randint(-20, layer.width + 20), rng.randint(-20, layer.height + 20)
        base = rng.choice(stops + [(255, 255, 255)])
        col = _lerp(base, (255, 255, 255), rng.uniform(0.0, 0.35))
        d.ellipse([x - r, y - r, x + r, y + r], fill=(*col, rng.randint(50, 110)))
    layer = layer.filter(ImageFilter.GaussianBlur(28))
    return layer.resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)


def _vignette() -> Image.Image:
    """Darkening mask, strongest at the edges (L mode)."""
    small = Image.new("L", (54, 96))
    px = small.load()
    for y in range(96):
        for x in range(54):
            d = math.hypot((x - 26.5) / 27, (y - 47.5) / 48)
            px[x, y] = int(max(0.0, min(1.0, (d - 0.5) / 0.9)) * 120)
    return small.resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)


def _add_noise(img: Image.Image) -> Image.Image:
    """Subtle film grain (overlay with mid-grey noise) to break up gradient banding."""
    noise = Image.effect_noise((WIDTH, HEIGHT), 20).convert("RGB")  # centred on 128
    return Image.blend(img, ImageChops.overlay(img, noise), 0.35)


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int, draw: ImageDraw.ImageDraw) -> tuple[list[str], bool]:
    """Greedy word wrap. Returns (lines, broke_a_word)."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    broke = False
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        # break words that are wider than the box on their own
        while draw.textlength(w, font=font) > max_w and len(w) > 1:
            broke = True
            cut = len(w)
            while cut > 1 and draw.textlength(w[:cut] + "-", font=font) > max_w:
                cut -= 1
            lines.append(w[:cut] + "-")
            w = w[cut:]
        cur = w
    if cur:
        lines.append(cur)
    return lines, broke


def _fit_text(text: str, box_w: int, box_h: int, draw: ImageDraw.ImageDraw,
              max_size: int = 104, min_size: int = 26) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Largest font size whose wrapped text fits the box. Returns (font, lines, line_height)."""
    def attempt(size: int, allow_break: bool):
        font = _font(FONT_BOLD, size)
        lines, broke = _wrap(text, font, box_w, draw)
        if broke and not allow_break:
            return None
        line_h = int(size * 1.25)
        widest = max((draw.textlength(l, font=font) for l in lines), default=0)
        if line_h * len(lines) <= box_h and widest <= box_w:
            return font, lines, line_h
        return None

    best = None
    # Prefer sizes where no word has to be hyphenated; only allow breaking as a last resort.
    for allow_break in (False, True):
        lo, hi = min_size, max_size
        while lo <= hi:
            size = (lo + hi) // 2
            res = attempt(size, allow_break)
            if res:
                best = res
                lo = size + 1
            else:
                hi = size - 1
        if best:
            break
    if best is None:
        font = _font(FONT_BOLD, min_size)
        lines, _ = _wrap(text, font, box_w, draw)
        line_h = int(min_size * 1.25)
        max_lines = max(1, box_h // line_h)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1].rstrip(".,;:") + "\u2026"
        best = (font, lines, line_h)
    return best


def _draw_shadowed(base: Image.Image, items: list[tuple[tuple[float, float], str, ImageFont.FreeTypeFont, tuple]],
                   shadow_offset: int = 5, blur: int = 9, shadow_alpha: int = 150,
                   anchor: str = "ma") -> None:
    """Draw text items with a soft drop shadow onto an RGBA base image (in place)."""
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    for (x, y), text, font, _ in items:
        sd.text((x, y + shadow_offset), text, font=font, fill=(0, 0, 0, shadow_alpha), anchor=anchor)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(shadow)
    d = ImageDraw.Draw(base)
    for (x, y), text, font, fill in items:
        d.text((x, y), text, font=font, fill=fill, anchor=anchor)


def _spaced(text: str) -> str:
    return " ".join(text)  # thin-space letter spacing for the header


def render_fact_image(fact: str, out_path: Path, seed: int | None = None,
                      date_text: str | None = None) -> Path:
    """Render ``fact`` to a 1080x1920 PNG at ``out_path`` and return the path.

    ``date_text`` is shown under the header; defaults to today's local date.
    """
    rng = random.Random(seed)
    stops = list(rng.choice(PALETTES))
    if rng.random() < 0.5:
        stops.reverse()

    img = _gradient(rng, stops).convert("RGBA")
    img.alpha_composite(_blobs(rng, stops))
    deep = _lerp(min(stops, key=sum), (0, 0, 10), 0.6)
    dark = Image.new("RGBA", (WIDTH, HEIGHT), (*deep, 255))
    img = Image.composite(dark, img, _vignette()).convert("RGB")
    img = _add_noise(img).convert("RGBA")

    # Frosted card behind the text, inside the safe area.
    pad = 36
    card = (SAFE_X0 - pad // 2, SAFE_Y0, SAFE_X0 + SAFE_W + pad // 2, SAFE_Y0 + SAFE_H)
    blurred = img.crop(card).filter(ImageFilter.GaussianBlur(30))
    lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in stops) / len(stops)
    tint_alpha = int(min(170, 105 + max(0.0, lum - 110) * 1.1))  # brighter palettes get a darker card
    tint = Image.new("RGBA", blurred.size, (*deep, tint_alpha))
    blurred.alpha_composite(tint)
    mask = Image.new("L", blurred.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, blurred.width - 1, blurred.height - 1], radius=56, fill=255)
    card_shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(card_shadow).rounded_rectangle(
        [card[0], card[1] + 14, card[2], card[3] + 14], radius=56, fill=(0, 0, 0, 90))
    img.alpha_composite(card_shadow.filter(ImageFilter.GaussianBlur(24)))
    img.paste(blurred, card[:2], mask)
    outline = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(outline).rounded_rectangle(card, radius=56, outline=(255, 255, 255, 70), width=3)
    img.alpha_composite(outline)

    draw = ImageDraw.Draw(img)
    cx = WIDTH // 2
    inner_w = SAFE_W - 2 * 48

    # Header + date
    header_font = _font(FONT_BOLD, 50)
    date_font = _font(FONT_REGULAR, 34)
    header_y = SAFE_Y0 + 70
    accent = _lerp(stops[0], (255, 255, 255), 0.55)
    if date_text is None:
        date_text = format_date(_date.today())
    _draw_shadowed(img, [
        ((cx, header_y), _spaced("FACT OF THE DAY"), header_font, (255, 255, 255, 255)),
        ((cx, header_y + 78), date_text, date_font, (*accent, 255)),
    ], shadow_offset=3, blur=6, shadow_alpha=130)

    # Divider: line - dot - line
    div_y = header_y + 160
    d = ImageDraw.Draw(img)
    d.line([(cx - 150, div_y), (cx - 26, div_y)], fill=(255, 255, 255, 170), width=4)
    d.line([(cx + 26, div_y), (cx + 150, div_y)], fill=(255, 255, 255, 170), width=4)
    d.ellipse([cx - 9, div_y - 9, cx + 9, div_y + 9], fill=(*accent, 255))

    # Footer inside the safe area
    footer_font = _font(FONT_BOLD, 32)
    footer_y = SAFE_Y0 + SAFE_H - 92
    _draw_shadowed(img, [((cx, footer_y), "#didyouknow", footer_font, (255, 255, 255, 200))],
                   shadow_offset=2, blur=5, shadow_alpha=120)

    # Fact text, auto-fitted between divider and footer
    box_top = div_y + 60
    box_bottom = footer_y - 50
    box_h = box_bottom - box_top
    text = " ".join((fact or "").split()) or "No fact today."
    font, lines, line_h = _fit_text(text, inner_w, box_h, draw)
    block_h = line_h * len(lines)
    y = box_top + (box_h - block_h) // 2
    size = font.size
    items = []
    for line in lines:
        # anchor "mm" centers each line on its slot; slot centre = y + line_h/2
        items.append(((cx, y + line_h / 2), line, font, (255, 255, 255, 255)))
        y += line_h
    _draw_shadowed(img, items, shadow_offset=max(3, size // 16), blur=max(5, size // 9),
                   shadow_alpha=170, anchor="mm")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    img.convert("RGB").save(tmp, format="PNG", optimize=True)
    tmp.replace(out_path)
    return out_path
