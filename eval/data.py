"""Read only loaders for the shared data files (run sheet section 3). No network, no writes."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

DATA = Path("data")
RAW_INDEX = DATA / "raw" / "index.jsonl"
PAGES = DATA / "pages.jsonl"
CHUNKS = DATA / "chunks.jsonl"
KG = DATA / "kg.sqlite"


def norm_name(s: str | None) -> str:
    """Accent free, lower case, alnum words only: 'Brüningk' -> 'bruningk'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def url_path(url: str) -> str:
    return urlparse(url or "").path or "/"


def url_host(url: str) -> str:
    return urlparse(url or "").netloc.lower().split(":")[0]


def _jsonl(path: Path):
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


@lru_cache(maxsize=1)
def host_sources() -> dict[str, str]:
    """host -> crawl source id, from the crawl index (each source has its own hosts)."""
    out: dict[str, str] = {}
    for r in _jsonl(RAW_INDEX):
        for u in (r.get("url"), r.get("final_url")):
            if u:
                out.setdefault(url_host(u), r["source"])
    for p in _jsonl(PAGES):
        out.setdefault(url_host(p["url"]), p["source"])
    # bare domain variants (dcberne.com vs www.dcberne.com)
    for h, s in list(out.items()):
        out.setdefault(h.removeprefix("www."), s)
        out.setdefault("www." + h.removeprefix("www."), s)
    return out


def source_of_url(url: str) -> str | None:
    return host_sources().get(url_host(url))


@lru_cache(maxsize=1)
def pages() -> dict[str, dict]:
    """url (and alt urls) -> page record."""
    out: dict[str, dict] = {}
    for p in _jsonl(PAGES):
        for u in [p["url"], *(p.get("alt_urls") or [])]:
            out[u] = p
    return out


@lru_cache(maxsize=1)
def chunks() -> dict[str, dict]:
    return {c["chunk_id"]: c for c in _jsonl(CHUNKS)}


@lru_cache(maxsize=1)
def crawled_urls() -> set[str]:
    urls = set(pages())
    for r in _jsonl(RAW_INDEX):
        if r.get("status") == 200 and r.get("file"):
            urls.add(r["url"])
            if r.get("final_url"):
                urls.add(r["final_url"])
    return urls


def kg_conn():
    if not KG.exists():
        return None
    try:
        return sqlite3.connect(f"file:{KG}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
