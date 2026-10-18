#!/usr/bin/env python3
"""Synthetic reverse queries (playbook "Test cases", layer 2).

Samples research units from the public KG (stratified by source and by size, so small one page groups are
in), shows Apertus 70B a unit's public page text and asks for two lay company needs in two languages that
avoid the unit's own name, people's names and technical terms. A query that still copies rare words from
the page is dropped (too easy). The expected answer is the unit's URL key plus its KG id.

    python -m eval.synthetic                 # writes eval/fixtures/synthetic.json (gateway calls cached)
    python -m eval.synthetic --units 30 --dry-run

Small on purpose: 25 units, one call each (batches of 5 mixed up which query belonged to which unit), at
most 4 in flight (the gateway is shared; only agent B runs bulk jobs). Calls are cached, so reruns are free.
The language label is what py3langid detects, not what was asked for (the model does not always comply). Deviation from the playbook: it asks for an HF model from another family to
write these; the run sheet assigns Apertus 70B. docs/eval.md says so next to the numbers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval import data  # noqa: E402
from eval.data import url_path  # noqa: E402

OUT = Path("eval/fixtures/synthetic.json")
BATCH = 1          # one unit per call: batching made the model mix up which query belongs to which unit
LANGS = [("English", "German"), ("German", "French"), ("French", "English"), ("English", "French"), ("German", "English")]
STOP = set("""the and for with that this from into over their they them have has are was were will would could
should about which what when where while your our you can how who why not but all any some such than then very
der die das und für mit von ein eine einer eines den dem des ist sind wir ihr sie auf aus bei nach oder wie
les des une pour avec dans sur par aux est sont nous vous qui que pas plus leur leurs""".split())
CONTACT = re.compile(r"\S+@\S+|\+?\d[\d ()/-]{7,}\d")
TOK = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def toks(s: str) -> list[str]:
    return [t.lower() for t in TOK.findall(s or "") if t.lower() not in STOP]


def unit_dir(url: str) -> str:
    p = url_path(url)
    return p if p.endswith("/") else p.rsplit("/", 1)[0] + "/"


def h(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


def broad(u: dict) -> bool:
    segs = [x for x in u["dir"].split("/") if x]
    return not segs or (len(segs) == 1 and len(segs[0]) == 2)


def load_units() -> list[dict]:
    """Units with their pages. KG units first; without a KG, group/unit pages from pages.jsonl."""
    pages = {p["page_id"]: p for p in data.pages().values()}
    units = []
    conn = data.kg_conn()
    if conn is not None:
        try:
            rows = conn.execute("SELECT id, name, source, url FROM unit WHERE url IS NOT NULL").fetchall()
        except Exception:  # noqa: BLE001
            rows = []
        conn.close()
        per_src = Counter(r[2] for r in rows)
        for uid, name, source, url in rows:
            if uid.endswith(":root") and per_src[source] > 2:
                continue  # whole-site roots are too broad to be a fair target
            units.append({"id": uid, "name": name, "source": source, "dir": unit_dir(url), "url": url})
    if not units:
        seen = set()
        for p in pages.values():
            if p.get("page_type") in ("group", "unit") and (p["source"], unit_dir(p["url"])) not in seen:
                seen.add((p["source"], unit_dir(p["url"])))
                units.append({"id": None, "name": p.get("h1") or p.get("title"), "source": p["source"],
                              "dir": unit_dir(p["url"]), "url": p["url"]})
    by_dir = defaultdict(list)
    for p in pages.values():
        by_dir[(p["source"], unit_dir(p["url"]))].append(p)
    for u in units:
        under = [p for (s, d), ps in by_dir.items() if s == u["source"] and d.startswith(u["dir"]) for p in ps]
        u["n_pages"] = len(under)
        main = [p for p in under if p.get("page_type") != "person"] or under
        main.sort(key=lambda p: (len(url_path(p["url"])), p.get("lang") != "en"))
        text = "\n\n".join((p.get("title") or "") + "\n" + p.get("text", "")[:1200] for p in main[:3])
        u["text"] = CONTACT.sub("", text)[:3000]
    units = [u for u in units if len(u["text"].split()) >= 60]
    # a whole-site or language-root "unit" makes any result from that source a hit: keep it only when the
    # source has nothing finer (single platform sites)
    finer = {u["source"] for u in units if not broad(u)}
    return [u for u in units if not broad(u) or u["source"] not in finer]


def stratified(units: list[dict], n: int) -> list[dict]:
    """Round robin over sources; inside a source alternate small and large units (gap: fairness)."""
    by_src = defaultdict(list)
    for u in units:
        by_src[u["source"]].append(u)
    queues = []
    for src, us in sorted(by_src.items()):
        us.sort(key=lambda u: h(u["dir"]))
        small = [u for u in us if u["n_pages"] <= 3]
        large = [u for u in us if u["n_pages"] > 3]
        mixed = []
        while small or large:
            for bucket in (small, large):
                if bucket:
                    mixed.append(bucket.pop(0))
        queues.append(mixed)
    out = []
    while len(out) < n and any(queues):
        for q in queues:
            if q and len(out) < n:
                out.append(q.pop(0))
    return out


def prompt(batch: list[tuple[int, dict, tuple[str, str]]]) -> list[dict]:
    parts = []
    for i, u, (la, lb) in batch:
        parts.append(f"### Unit 1\nVariant A language: {la}. Variant B language: {lb}.\n{u['text'][:2500]}")
    system = ("You write test queries for a search engine that finds research groups and facilities at the "
              "University of Bern for companies. For each unit, write two different needs that a company CEO "
              "might type into a search box, one per requested language, 6 to 20 words each, in plain lay words. "
              "Describe what the company wants to achieve, not what the lab is called. Never use the unit's name, "
              "any abbreviation, any person's name, or the distinctive technical terms of the text. The two "
              "variants must use different wording. Call record_queries once, with unit number 1.")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]


def call(batch):
    from common.llm import LARGE, chat, force, tool
    t = tool("record_queries", "Save the two test queries for every unit", {
        "type": "object", "properties": {"items": {"type": "array", "items": {
            "type": "object", "properties": {"unit": {"type": "integer"}, "a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["unit", "a", "b"]}}}, "required": ["items"]})
    r = chat(LARGE, prompt(batch), tools=[t], tool_choice=force("record_queries"), tag="eval_synthetic",
             temperature=0.7, max_tokens=1500)
    calls = r.get("tool_calls") or []
    args = calls[0]["arguments"] if calls else {}
    return args.get("items", []) if isinstance(args, dict) else []


def build(n_units: int, dry_run: bool = False) -> dict:
    units = load_units()
    df = Counter()
    all_pages = {p["page_id"]: p for p in data.pages().values()}
    for p in all_pages.values():
        df.update(set(toks(p.get("text", ""))))
    n_pages = max(len(all_pages), 1)
    sample = stratified(units, n_units)
    print(f"[synthetic] {len(units)} candidate units, sampled {len(sample)} from "
          f"{len(set(u['source'] for u in sample))} sources, {sum(u['n_pages'] <= 3 for u in sample)} small")
    jobs = [(i + 1, u, LANGS[i % len(LANGS)]) for i, u in enumerate(sample)]
    batches = [jobs[i:i + BATCH] for i in range(0, len(jobs), BATCH)]
    if dry_run:
        print(json.dumps(prompt(batches[0]), ensure_ascii=False)[:3000])
        return {}
    import py3langid as langid
    print(f"[synthetic] {len(batches)} gateway calls (cached ones are free), at most 4 in flight")
    with ThreadPoolExecutor(max_workers=4) as ex:   # never more than 4 in flight
        answers = list(ex.map(call, batches))
    cases, dropped = [], Counter()
    lookup = {i: (u, langs) for i, u, langs in jobs}
    for (job,), items in zip(batches, answers):
        for it in items[:1]:
            it = dict(it, _job=job[0]) if isinstance(it, dict) else it
            if not isinstance(it, dict):
                dropped["bad_item"] += 1
                continue
            u, langs = lookup[it["_job"]]
            utoks = set(toks(u["text"]))
            for var, lang in (("a", langs[0]), ("b", langs[1])):
                q = re.sub(r"^(english|german|french|deutsch|français|variant [ab])\s*:\s*", "", (it.get(var) or "").strip(), flags=re.I)
                qt = toks(q)
                if not q or not qt:
                    dropped["empty"] += 1
                    continue
                if CONTACT.search(q):
                    dropped["contact_like"] += 1
                    continue
                detected = langid.classify(q)[0]
                if detected not in ("en", "de", "fr"):
                    dropped["language"] += 1
                    continue
                rare = [t for t in qt if t in utoks and df[t] / n_pages < 0.01]
                if len(rare) / len(qt) > 0.3:     # copied the page's rare words: too easy
                    dropped["rare_term_overlap"] += 1
                    continue
                key = {"source": u["source"], "path": u["dir"]}
                cases.append({"id": f"syn{it['_job']:02d}{var}", "origin": "synthetic",
                              "cat": "synthetic_site" if broad(u) else "synthetic_unit",
                              "lang": detected, "asked_lang": lang, "query": q, "expect": u["name"] or u["dir"], "split": "synthetic",
                              "check": "recall", "pair": u["id"] or f"{u['source']}:{u['dir']}",
                              "size": "small" if u["n_pages"] <= 3 else "large",
                              "targets": [{"label": u["name"] or u["dir"], "keys": [key], "kg_id": u["id"]}]})
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": "apertus-v1-5-70b",
           "n_units": len(sample), "n": len(cases), "dropped": dict(dropped),
           "note": "Generated from public page text; queries hold no names or contact data. Report only, never tune on it.",
           "cases": cases}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"[synthetic] wrote {len(cases)} queries to {OUT}; dropped {dict(dropped)}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--units", type=int, default=25)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    build(a.units, a.dry_run)


if __name__ == "__main__":
    main()
