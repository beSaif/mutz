"""Stand in for search.query.query (agent C), used only until the real one imports.

Follows the run sheet section 3 contract exactly, so the harness and the UI can be built against it.
Plain BM25 over data/chunks.jsonl, chunks aggregated to a unit keyed by the URL directory. No expansion,
no embeddings, no reranker: lay and multilingual queries are expected to do badly here. Numbers from the
stub are a floor for the harness, never a claim about the product.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from functools import lru_cache

from eval import data
from eval.data import url_path

_TOK = re.compile(r"[^\W\d_]{2,}|\d{2,}", re.UNICODE)


def tokens(s: str) -> list[str]:
    return [t.lower() for t in _TOK.findall(s or "")]


def unit_key(source: str, url: str) -> str:
    segs = [s for s in url_path(url).split("/") if s and not s.startswith("index_")]
    depth = 3 if source.startswith("unibe") else 2
    return f"stub:{source}:/" + "/".join(segs[:depth])


@lru_cache(maxsize=1)
def _index():
    docs, df = [], Counter()
    for c in data.chunks().values():
        toks = tokens((c.get("heading") or "") + " " + c.get("text", ""))
        tf = Counter(toks)
        docs.append((c, tf, len(toks)))
        df.update(tf.keys())
    avg = sum(n for _, _, n in docs) / max(len(docs), 1)
    return docs, df, avg


def _quote(text: str) -> str:
    """A verbatim sentence from the chunk, so citation validity can be checked end to end."""
    for line in text.splitlines():
        line = line.strip().lstrip("#-* ").strip()
        if len(line.split()) >= 8:
            return line[:300]
    return text.strip()[:200]


def query(text, k=10, sources=None):
    docs, df, avg = _index()
    n = len(docs)
    q = set(tokens(text))
    scores = []
    for c, tf, ln in docs:
        if sources and c["source"] not in sources:
            continue
        s = 0.0
        for t in q:
            f = tf.get(t)
            if f:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * ln / avg))
        if s > 0:
            scores.append((s, c))
    scores.sort(key=lambda x: -x[0])
    units: dict[str, dict] = {}
    agg = defaultdict(float)
    for s, c in scores[:400]:
        u = unit_key(c["source"], c["url"])
        agg[u] = max(agg[u], s)
        if u not in units:
            page = data.pages().get(c["url"], {})
            units[u] = {"source": c["source"], "name": (page.get("h1") or page.get("title") or u)[:120], "reasons": []}
        if len(units[u]["reasons"]) < 2:
            units[u]["reasons"].append({"chunk_id": c["chunk_id"], "url": c["url"], "quote": _quote(c["text"]),
                                        "deep_link": c["url"]})
    ranked = sorted(agg.items(), key=lambda x: -x[1])[:k]
    top = ranked[0][1] if ranked else 0
    results = [{
        "entity_id": u, "type": "unit", "name": units[u]["name"], "unit": units[u]["name"], "function": None,
        "score": round(s, 3), "coverage": "strong" if s >= 12 and s >= 0.6 * top else ("thin" if s >= 6 else "none"),
        "recency": None, "reasons": units[u]["reasons"]} for u, s in ranked]
    searched = sorted({c["source"] for c in data.chunks().values()} if not sources else set(sources))
    return {"expansions": [], "searched_sources": searched, "results": results, "stub": True}
