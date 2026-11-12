"""Fact source: free online API with a bundled offline fallback list."""
from __future__ import annotations

import logging
import random
import re
from pathlib import Path

import httpx

from app import db

log = logging.getLogger(__name__)

API_URL = "https://uselessfacts.jsph.pl/api/v2/facts/random?language=en"
FACTS_FILE = Path(__file__).parent / "data" / "facts.txt"
MAX_LEN = 220
MIN_LEN = 15
API_ATTEMPTS = 4
API_TIMEOUT = 6.0

# Facts from the API occasionally contain things we don't want on a family-friendly snap.
_BLOCKLIST = re.compile(
    r"\b(sex\w*|porn\w*|nude|naked|penis|vagina|breast\w*|condom\w*|orgasm\w*|kill\w*|murder\w*|suicide|"
    r"rape\w*|drug\w*|cocaine|heroin|nazi\w*|hitler|death|dead|die[sd]?|corpse\w*|blood\w*)\b",
    re.IGNORECASE,
)


def clean_fact(text: str) -> str:
    """Normalise whitespace and odd quote characters."""
    text = (text or "").replace("`", "'").replace(" ", " ")
    text = " ".join(text.split()).strip()
    if text and text[-1] not in ".!?\"')":
        text += "."
    return text


def _acceptable(fact: str) -> bool:
    if not fact or len(fact) < MIN_LEN or len(fact) > MAX_LEN:
        return False
    if "http://" in fact or "https://" in fact:
        return False
    if _BLOCKLIST.search(fact):
        return False
    try:
        return not db.fact_used(fact)
    except Exception:  # db trouble should never block fact selection
        log.exception("fact_used check failed")
        return True


async def _from_api() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT, follow_redirects=True) as client:
            for attempt in range(API_ATTEMPTS):
                try:
                    resp = await client.get(API_URL, headers={"Accept": "application/json"})
                    resp.raise_for_status()
                    data = resp.json()
                    fact = clean_fact(str(data.get("text", "")) if isinstance(data, dict) else "")
                    if _acceptable(fact):
                        return fact
                    log.info("API fact rejected (attempt %d): %r", attempt + 1, fact[:80])
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning("Fact API attempt %d failed: %s", attempt + 1, exc)
                    # A transport error (no network) is unlikely to fix itself within seconds.
                    if isinstance(exc, httpx.TransportError):
                        return None
    except Exception:
        log.exception("Fact API unexpected error")
    return None


def load_bundled_facts() -> list[str]:
    try:
        lines = FACTS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        log.exception("Could not read bundled facts file %s", FACTS_FILE)
        return []
    return [clean_fact(l) for l in lines if l.strip() and not l.lstrip().startswith("#")]


def _from_bundle() -> str:
    facts = load_bundled_facts()
    if not facts:
        return "Honey never spoils: archaeologists have found edible honey in ancient Egyptian tombs."
    try:
        used = db.used_facts_map()
    except Exception:
        log.exception("Could not read used facts")
        used = {}
    unused = [f for f in facts if f not in used]
    if unused:
        return random.choice(unused)
    # Everything has been used: reuse the least recently used one.
    return min(facts, key=lambda f: used.get(f, ""))


async def get_fact() -> str:
    """Return a fresh fact. Never raises."""
    try:
        fact = await _from_api()
        if fact:
            return fact
    except Exception:
        log.exception("get_fact API path failed")
    try:
        return _from_bundle()
    except Exception:
        log.exception("get_fact bundle path failed")
        return "Octopuses have three hearts and blue blood."
