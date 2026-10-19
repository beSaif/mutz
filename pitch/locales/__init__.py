"""The fixed words of the pitch document in the pitch's own language (pitch["lang"]: en, de, fr, it).

This is the document's language, chosen per lead on the Brief, not the interface language of the app.
Catalogs are flat JSON, {"English text": "translation"}, keyed by the exact English text in the templates
and code; a missing key prints the English. German is Swiss Standard German (ss, never ß) and addresses the
company as "Sie", French as "vous", Italian (as in Ticino) as "Lei"."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

DIR = Path(__file__).resolve().parent
LANGS = ("en", "de", "fr", "it")
_cat: dict[str, dict] = {}
_mtime: dict[str, float] = {}

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def norm(lang) -> str:
    """A supported pitch language, English for anything else."""
    lang = (lang or "en").lower()[:2]
    return lang if lang in LANGS else "en"


def catalog(lang) -> dict:
    """All document strings for one language; reloaded when the file changes."""
    lang = norm(lang)
    if lang == "en":
        return {}
    f = DIR / f"{lang}.json"
    stamp = f.stat().st_mtime if f.is_file() else 0.0
    if _mtime.get(lang) != stamp:
        _cat[lang] = json.loads(f.read_text("utf-8")) if stamp else {}
        _mtime[lang] = stamp
    return _cat[lang]


def tr(lang, s, **kw) -> str:
    """s in the pitch language, with str.format placeholders filled in. A missing key gives the English."""
    if not s:
        return s
    out = catalog(lang).get(s) or s
    return out.format(**kw) if kw else out


def fmt_date(value, lang="en") -> str:
    """19 October 2026 · 19. Oktober 2026 · 19 octobre 2026 · 19 ottobre 2026. Unparsable values come back as they are."""
    if not value:
        return ""
    if isinstance(value, (date, datetime)):
        d = value
    else:
        try:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    month = tr(lang, MONTHS[d.month - 1])
    if d.day == 1:  # 1er octobre, 1º ottobre
        return tr(lang, "1 {month} {year}", month=month, year=d.year)
    return tr(lang, "{day} {month} {year}", day=d.day, month=month, year=d.year)
