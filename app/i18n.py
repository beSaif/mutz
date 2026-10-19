"""Interface language: English, German or Italian, picked per browser with the switch in the sidebar (cookie mutz-lang).

Strings are keyed by their English text, gettext style:  _("New lead")  or  _("{n} of {total}", n=3, total=9).
Translations live in app/locales/<lang>/*.json, one file per screen group, merged on load. A missing key shows
the English text, so an untranslated string is never blank. Data (lead titles, quotes, names) is not translated.
The pitch keeps its own language, chosen per lead on the Brief."""
from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path

LANGS = [("en", "English"), ("de", "Deutsch"), ("it", "Italiano")]
CODES = [c for c, _ in LANGS]
COOKIE = "mutz-lang"
DIR = Path(__file__).parent / "locales"

_lang: ContextVar[str] = ContextVar("mutz_lang", default="en")
_cat: dict[str, dict[str, str]] = {}
_mtime: dict[str, float] = {}


def catalog(lang):
    """All strings for one language; reloaded when a locale file changes, so edits show without a restart."""
    if lang == "en":
        return {}
    files = sorted((DIR / lang).glob("*.json"))
    stamp = sum(f.stat().st_mtime for f in files) + len(files)
    if _mtime.get(lang) != stamp:
        merged = {}
        for f in files:
            merged.update(json.loads(f.read_text(encoding="utf-8")))
        _cat[lang], _mtime[lang] = merged, stamp
    return _cat[lang]


def pick(request):
    """The cookie if set, else the browser's first supported language, else English."""
    c = request.cookies.get(COOKIE)
    if c in CODES:
        return c
    for part in request.headers.get("accept-language", "").split(","):
        code = part.split(";")[0].strip()[:2].lower()
        if code in CODES:
            return code
    return "en"


def set_lang(lang):
    _lang.set(lang if lang in CODES else "en")


def lang():
    return _lang.get()


def _(s, **kw):
    if not s:
        return s
    out = catalog(lang()).get(s) or s
    return out.format(**kw) if kw else out


def js():
    """The strings the browser scripts look up with t(); the whole catalog is small enough to send as is."""
    return catalog(lang())


MONTHS = {"de": ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."],
          "it": ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]}


def month(d):
    m = MONTHS.get(lang())
    return f"{d.day}{'.' if lang() == 'de' else ''} {m[d.month - 1]}" if m else d.strftime("%-d %b")
