"""Pure metric functions. No I/O except reading page/chunk text through eval.data."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from itertools import combinations

from eval import data

ADJACENT_TAGS = {"adjacent", "edge", "adjacent_opportunity"}


def is_adjacent(result: dict) -> bool:
    """Gap 11: adjacent opportunities are by definition not in the expected list, so they never count for
    or against recall. Accepts the tag in any of the shapes a result might carry it."""
    if result.get("adjacent") is True:
        return True
    for k in ("tag", "kind", "lane", "match_type"):
        if str(result.get(k, "")).lower() in ADJACENT_TAGS:
            return True
    return bool(ADJACENT_TAGS & {str(t).lower() for t in result.get("tags") or []})


def core_results(results: list[dict], k: int = 10) -> list[dict]:
    """The first k results that are not tagged adjacent."""
    return [r for r in results if not is_adjacent(r)][:k]


# ---------- citation validity ----------

_PUNCT = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "«": '"', "»": '"',
                        "–": "-", "—": "-", " ": " ", "­": ""})


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").translate(_PUNCT).lower()
    s = re.sub(r"[#*_`>|]+", " ", s)          # markdown left in chunk text
    s = re.sub(r"(^|\s)-\s", " ", s)          # list bullets
    return re.sub(r"\s+", " ", s).strip()


def quote_in_text(quote: str, text: str) -> bool:
    """True when the quote appears verbatim (after whitespace, case and markdown normalisation) in text.
    An ellipsis in the quote splits it into parts that must all appear, in order."""
    q, t = norm_text(quote), norm_text(text)
    if not q or not t:
        return False
    parts = [p.strip(" .") for p in re.split(r"\s*(?:\.\.\.|…|\[\.\.\.\])\s*", q) if p.strip(" .")]
    pos = 0
    for p in parts:
        i = t.find(p, pos)
        if i < 0:
            return False
        pos = i + len(p)
    return bool(parts)


def check_reason(reason: dict) -> tuple[bool, str]:
    """A reason is valid when its URL is a crawled public page and its quote is in the cited chunk or page."""
    quote, url, cid = reason.get("quote") or "", reason.get("url") or "", reason.get("chunk_id")
    if not url:
        return False, "no_url"
    if not quote.strip():
        return False, "no_quote"
    chunk = data.chunks().get(cid) if cid else None
    if chunk and quote_in_text(quote, chunk.get("text", "")):
        return True, "chunk"
    page = data.pages().get(url) or (data.pages().get(chunk["url"]) if chunk else None)
    if page and quote_in_text(quote, (page.get("title") or "") + "\n" + page.get("text", "")):
        return True, "page"
    if not page and url not in data.crawled_urls():
        return False, "url_not_in_crawl"
    return False, "quote_not_found"


# ---------- overlap, concentration ----------

def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    return 1.0 if not a and not b else len(a & b) / len(a | b)


def overlap(a, b) -> float:
    """|A and B| / min(|A|, |B|): how much of the shorter list the other one also finds."""
    a, b = set(a), set(b)
    return 1.0 if not a and not b else (len(a & b) / min(len(a), len(b)) if a and b else 0.0)


def pairwise(groups: dict[str, list[list[str]]]) -> dict:
    """groups: group id -> list of top-k entity id lists. Mean pairwise Jaccard and overlap per group."""
    per, js, os_ = {}, [], []
    for gid, lists in groups.items():
        pj = [jaccard(a, b) for a, b in combinations(lists, 2)]
        po = [overlap(a, b) for a, b in combinations(lists, 2)]
        if pj:
            per[gid] = {"jaccard": round(sum(pj) / len(pj), 3), "overlap": round(sum(po) / len(po), 3)}
            js += pj
            os_ += po
    return {"jaccard": round(sum(js) / len(js), 3) if js else None,
            "overlap": round(sum(os_) / len(os_), 3) if os_ else None, "pairs": len(js), "groups": per}


def gini(values) -> float:
    v = sorted(x for x in values if x >= 0)
    n, s = len(v), sum(v)
    if n == 0 or s == 0:
        return 0.0
    return round(sum((2 * i - n - 1) * x for i, x in enumerate(v, 1)) / (n * s), 3)


def exposure(unit_hits: list[tuple[str, int]], index_units: set[str] | None = None) -> dict:
    """unit_hits: (unit key, rank starting at 1) for every core result shown. Position weighted exposure
    1/log2(rank+1), as in fair ranking work. Reports how concentrated attention is across units."""
    w = Counter()
    raw = Counter()
    for unit, rank in unit_hits:
        w[unit] += 1 / math.log2(rank + 1)
        raw[unit] += 1
    total = sum(w.values())
    if not total:
        return {"units_exposed": 0, "gini": 0.0, "top1_share": 0.0, "top5_share": 0.0, "entropy_norm": 0.0, "top_units": []}
    shares = sorted((x / total for x in w.values()), reverse=True)
    ent = -sum(p * math.log(p) for p in shares if p > 0)
    out = {
        "units_exposed": len(w),
        "gini": gini(list(w.values())),
        "top1_share": round(shares[0], 3),
        "top5_share": round(sum(shares[:5]), 3),
        "entropy_norm": round(ent / math.log(len(shares)), 3) if len(shares) > 1 else 0.0,
        "top_units": [{"unit": u, "exposure_share": round(x / total, 3), "appearances": raw[u]} for u, x in w.most_common(8)],
    }
    if index_units:  # fairness: how many of the indexed units ever get shown at all
        out["units_in_index"] = len(index_units)
        out["share_of_index_units_exposed"] = round(len(set(w) & index_units) / len(index_units), 3)
    return out
