"""One door to the other agents' contracts. Each call tries the real module first and falls back to
app/stubs.py when the import fails, the call raises, or the data behind it is missing. STATUS records
which contracts the last call served from a stub, so the footer can say so."""
from __future__ import annotations

import importlib
import os
import logging
import time
import traceback

from app import stubs

log = logging.getLogger("mutz.contracts")
STATUS: dict[str, str] = {}      # contract -> "real" | "stub"
ERRORS: dict[str, str] = {}
_cache: dict[str, tuple[float, object]] = {}
RETRY_SECS = 30
# MUTZ_STUB=query,why_not forces those contracts onto the stub (demo safety switch); "all" forces every one
FORCE = {x.strip() for x in os.environ.get("MUTZ_STUB", "").split(",") if x.strip()}


def _real(modname, attr):
    now = time.time()
    hit = _cache.get(modname)
    if hit and (hit[1] is not None or now - hit[0] < RETRY_SECS):
        mod = hit[1]
    else:
        try:
            mod = importlib.import_module(modname)
        except Exception as e:  # not written yet, or a dependency is missing on this host
            ERRORS[modname] = f"import: {e.__class__.__name__}: {e}"
            mod = None
        _cache[modname] = (now, mod)
    return getattr(mod, attr, None) if mod is not None else None


def _call(name, modname, attr, stub_fn, *a, **kw):
    fn = None if (name in FORCE or "all" in FORCE) else _real(modname, attr)
    if fn is not None:
        try:
            out = fn(*a, **kw)
            STATUS[name] = "real"
            return out
        except Exception as e:
            ERRORS[name] = f"{e.__class__.__name__}: {e}"
            log.warning("contract %s failed, using stub: %s\n%s", name, e, traceback.format_exc(limit=3))
    STATUS[name] = "stub"
    return stub_fn(*a, **kw)


QUERY_TIMEOUT = float(os.environ.get("MUTZ_QUERY_TIMEOUT", "90"))
_qpool = None


def _timed(fn, timeout):
    """Run a real contract with a time limit; the stub answers if it takes longer (the real call keeps
    running in the background, so its models stay warm for the next query)."""
    def wrapped(*a, **kw):
        global _qpool
        from concurrent.futures import ThreadPoolExecutor
        if _qpool is None:
            _qpool = ThreadPoolExecutor(max_workers=2)
        return _qpool.submit(fn, *a, **kw).result(timeout=timeout)
    return wrapped


def query(text, k=10, sources=None, expansions=None, need_type=None):
    """expansions (the officer's ticked terms) and need_type are optional extras agent C's query() accepts."""
    real = None if ("query" in FORCE or "all" in FORCE) else _real("search.query", "query")
    if real is not None:
        try:
            extra = {k2: v for k2, v in (("expansions", expansions), ("need_type", need_type)) if v}
            try:
                out = _timed(real, QUERY_TIMEOUT)(text, k=k, sources=sources, **extra)
            except TypeError:
                out = _timed(real, QUERY_TIMEOUT)(" ".join([text] + list(expansions or [])), k=k, sources=sources)
            if isinstance(out, dict) and "results" in out:
                STATUS["query"] = "real"
                return out
        except Exception as e:
            ERRORS["query"] = f"{e.__class__.__name__}: {e}"
            log.warning("query failed or timed out, using stub: %r", e)
    STATUS["query"] = "stub"
    return stubs.query(" ".join([text] + [t for t in (expansions or []) if t.lower() not in text.lower()]), k=k, sources=sources)


def why_not(name, query_text):
    return _call("why_not", "search.query", "why_not", stubs.why_not, name, query_text)


def needs(one_pager_text=None, pdf_path=None, website=None, keywords=None):
    return _call("needs", "needs.needs", "needs", stubs.needs, one_pager_text=one_pager_text, pdf_path=pdf_path,
                 website=website, keywords=keywords)


def route(need):
    return _call("route", "needs.needs", "route", stubs.route, need)


def grants_for(entity_id):
    try:
        return _call("grants_for", "enrich.snsf", "grants_for", stubs.grants_for, entity_id) or []
    except Exception:
        return []


def validate(pitch):
    return _call("validate", "pitch.build", "validate", stubs.validate, pitch)


def build_pdf(pitch, mode, out_path):
    """Raises an exception with .problems (either module's ExportError) when export is refused."""
    fn = None if ("build_pdf" in FORCE or "all" in FORCE) else _real("pitch.build", "build_pdf")
    if fn is not None:
        try:
            out = fn(pitch, mode=mode, out_path=out_path)
            STATUS["build_pdf"] = "real"
            return out
        except Exception as e:
            if getattr(e, "problems", None):
                STATUS["build_pdf"] = "real"
                raise
            ERRORS["build_pdf"] = f"{e.__class__.__name__}: {e}"
            log.warning("build_pdf failed, using stub: %s", e)
    STATUS["build_pdf"] = "stub"
    return stubs.build_pdf(pitch, mode=mode, out_path=out_path)


def curated():
    import json
    from pathlib import Path
    p = Path("enrich/curated.json")
    if p.exists():
        try:
            data = json.loads(p.read_text())
            STATUS["curated"] = "real"
            return data
        except Exception:
            pass
    STATUS["curated"] = "stub"
    return stubs.FALLBACK_CURATED


def stubbed():
    return sorted(k for k, v in STATUS.items() if v == "stub")
