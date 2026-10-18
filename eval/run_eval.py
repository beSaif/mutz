#!/usr/bin/env python3
"""Run the mutz eval against search.query.query (or the contract stub until it imports).

    python -m eval.run_eval                     # dev split (tune on this one)
    python -m eval.run_eval --split heldout     # once, after the 19:00 freeze; every run is logged
    python -m eval.run_eval --split synthetic   # eval/fixtures/synthetic.json (python -m eval.synthetic)
    python -m eval.run_eval --backend stub      # force the stub

Prints Recall@10, synonym parity, citation validity, field completeness, per source coverage and exposure.
Results tagged adjacent are excluded from recall (gap 11). Writes data/eval/latest.json (contract in
docs/contracts-extra.md), data/eval/latest_<split>.json, and refreshes the numbers block in docs/eval.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval import data, metrics  # noqa: E402
from eval.data import norm_name  # noqa: E402
from eval.metrics import core_results, is_adjacent  # noqa: E402
from eval.targets import KG, result_matches  # noqa: E402

GOLD = Path("eval/gold.json")
LOCK = Path("eval/heldout.lock")
SYNTH = Path("eval/fixtures/synthetic.json")
OUT_DIR = Path("data/eval")
DOC = Path("docs/eval.md")
SKIP_CHECKS = {"manual", "needs"}       # judged by hand, or by agent E's needs tests


# ---------- gold, lock ----------

def heldout_digest(gold: dict) -> tuple[str, int]:
    held = sorted((c for c in gold["cases"] if c["split"] == "held"), key=lambda c: c["id"])
    groups = sorted((g for g in gold.get("synonym_groups", []) if g["split"] == "held"), key=lambda g: g["id"])
    blob = json.dumps({"cases": held, "groups": groups}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest(), len(held)


def check_lock(gold: dict, split: str, write: bool = False) -> str:
    digest, n = heldout_digest(gold)
    if write:
        if LOCK.exists() and json.loads(LOCK.read_text())["sha256"] != digest:
            sys.exit("heldout.lock exists with a different hash. Changing the held out set needs the human's OK; "
                     "delete eval/heldout.lock by hand first if they agreed.")
        LOCK.write_text(json.dumps({"sha256": digest, "n_cases": n, "locked_at": now()}, indent=1) + "\n")
        print(f"held out set locked: {n} cases, sha256 {digest[:16]}")
        return "locked"
    if not LOCK.exists():
        print("WARNING eval/heldout.lock missing: run with --lock once")
        return "missing"
    want = json.loads(LOCK.read_text())["sha256"]
    if want != digest:
        msg = f"held out set changed since it was locked (sha256 {digest[:16]} != {want[:16]})"
        if split == "heldout":
            sys.exit("REFUSING: " + msg)
        print("WARNING " + msg)
        return "changed"
    return "ok"


def load_cases(gold: dict, split: str) -> tuple[list[dict], list[dict]]:
    if split == "synthetic":
        if not SYNTH.exists():
            sys.exit(f"{SYNTH} missing: run python -m eval.synthetic first")
        return json.loads(SYNTH.read_text())["cases"], []
    want = {"dev": {"dev"}, "heldout": {"held"}, "all": {"dev", "held"}}[split]
    cases = [c for c in gold["cases"] if c["split"] in want]
    groups = [g for g in gold.get("synonym_groups", []) if g["split"] in want]
    return cases, groups


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- backend ----------

def get_backend(name: str):
    if name in ("auto", "real"):
        try:
            from search.query import query  # agent C
            return query, "search.query"
        except Exception as e:  # noqa: BLE001 - any import failure means "not ready yet"
            if name == "real":
                raise
            print(f"[eval] search.query not importable ({type(e).__name__}: {e}); using eval.stub_query")
    from eval.stub_query import query
    return query, "stub"


# ---------- per result helpers ----------

def result_source(r: dict, kg: KG) -> str | None:
    e = kg.ent.get(kg.resolve_id(r.get("entity_id")) or "")
    if e and e["sources"]:
        return sorted(e["sources"])[0]
    for reason in r.get("reasons") or []:
        s = data.source_of_url(reason.get("url"))
        if s:
            return s
    return None


def result_unit(r: dict, kg: KG) -> str:
    eid = kg.resolve_id(r.get("entity_id"))
    e = kg.ent.get(eid or "")
    if e:
        if e["type"] == "unit":
            return eid
        if e["unit_ids"]:
            return sorted(e["unit_ids"])[0]
    if r.get("unit_id"):
        return r["unit_id"]
    if r.get("type") == "unit" and r.get("entity_id"):
        return r["entity_id"]
    return f"{result_source(r, kg)}:{norm_name(r.get('unit')) or '?'}"


def completeness(r: dict, kg: KG) -> tuple[int, int, dict]:
    """filled / available for the case page's required fields. 'Available' comes from the KG when it
    exists (the source has the fact), else every applicable field counts as available."""
    e = kg.ent.get(kg.resolve_id(r.get("entity_id")) or "")
    typ = r.get("type") or (e or {}).get("type")
    fields = {"name": True, "unit": True, "source_link": True}
    if typ == "person":
        fields["function"] = True
    if e:
        fields["unit"] = bool(e["unit_ids"]) or e["type"] == "unit"
        if typ == "person":
            fields["function"] = bool(e["functions"])
            fields["email"] = bool([x for x in e["emails"] if x])
            fields["phone"] = bool([x for x in e["phones"] if x])
    got = {
        "name": bool(r.get("name")), "unit": bool(r.get("unit")), "function": bool(r.get("function")),
        "source_link": any(x.get("url") for x in r.get("reasons") or []),
        "email": bool(r.get("email") or r.get("emails")), "phone": bool(r.get("phone") or r.get("phones")),
    }
    per = {f: got[f] for f, avail in fields.items() if avail}
    return sum(per.values()), len(per), per


# ---------- per case ----------

def eval_case(case: dict, out: dict, kg: KG, k: int) -> dict:
    results = out.get("results") or []
    core = core_results(results, k)
    rec = {"id": case["id"], "cat": case["cat"], "split": case["split"], "check": case["check"],
           "query": case["query"], "origin": case.get("origin"), "n_results": len(results),
           "n_adjacent": sum(is_adjacent(r) for r in results[: k + 10]),
           "top": [{"entity_id": r.get("entity_id"), "name": r.get("name"), "type": r.get("type"),
                    "unit": r.get("unit"), "coverage": r.get("coverage"), "source": result_source(r, kg)} for r in core],
           "searched_sources": out.get("searched_sources") or [], "expansions": out.get("expansions") or []}
    chk = case["check"]
    if chk in ("recall", "fields"):
        hits, how = [], Counter()
        for t in case["targets"]:
            resolved = kg.resolve_target(t) if kg.available else set()
            rank = None
            for i, r in enumerate(core, 1):
                via = result_matches(r, t, resolved, kg)
                if via:
                    rank = i
                    how[via] += 1
                    break
            hits.append({"label": t["label"], "rank": rank, "kg_ids": len(resolved),
                         "source": t["keys"][0]["source"]})
        rec["targets"] = hits
        rec["recall"] = sum(h["rank"] is not None for h in hits) / len(hits) if hits else None
        rec["hit_via"] = dict(how)
        if chk == "fields":
            first = next((core[h["rank"] - 1] for h in hits if h["rank"]), None)
            if first is None:
                rec["pass"], rec["note"] = False, "target not in top k"
            else:
                e = kg.ent.get(kg.resolve_id(first.get("entity_id")) or "")
                shown = {f: bool(first.get(f) or first.get(f + "s")) for f in ("email", "phone")}
                in_kg = {f: bool(e and [x for x in e[f + "s"] if x]) for f in ("email", "phone")}
                rec["pass"] = all(shown.values()) or (not any(shown.values()) and all(in_kg.values()))
                rec["note"] = f"shown in result {shown}; in kg {in_kg}"
    elif chk == "abstain":
        strong = [r for r in core if r.get("coverage") == "strong"]
        rec["pass"] = not strong and bool(out.get("searched_sources"))
        rec["note"] = f"{len(strong)} strong results; searched_sources listed: {bool(out.get('searched_sources'))}"
    elif chk == "entity_resolution":
        spec = case.get("entity_resolution", {})
        pats = [norm_name(p) for p in spec.get("name_patterns", [])]
        people = [r for r in core if r.get("type") == "person" and any(p in norm_name(r.get("name")) for p in pats)]
        ids = {kg.resolve_id(r.get("entity_id")) for r in people}
        ok = len(ids) == spec.get("expect_count", 1)
        note = f"{len(ids)} matching person entities"
        if ok and spec.get("expect_sources"):
            e = kg.ent.get(next(iter(ids)) or "")
            srcs = set(e["sources"]) if e else {result_source(r, kg) for r in people}
            srcs |= {data.source_of_url(x.get("url")) for r in people for x in r.get("reasons") or []}
            ok = set(spec["expect_sources"]) <= srcs
            note += f"; sources {sorted(s for s in srcs if s)}"
        rec["pass"], rec["note"] = ok, note
    elif chk == "recency_active":
        projects = [r for r in core if r.get("type") == "project"]
        rec["pass"] = bool(projects) and all(r.get("recency") == "active" for r in projects)
        rec["note"] = f"{len(projects)} project results, recency {Counter(r.get('recency') for r in projects)}"
    return rec


# ---------- aggregate ----------

def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def run(split: str, k: int, backend: str, limit: int | None = None, write: bool = True, doc: bool = True,
        label: str | None = None) -> dict:
    gold = json.loads(GOLD.read_text())
    lock_state = check_lock(gold, split)
    cases, groups = load_cases(gold, split)
    if limit:
        cases = cases[:limit]
    query, backend_name = get_backend(backend)
    kg = KG()
    print(f"[eval] split={split} cases={len(cases)} backend={backend_name} kg={'yes' if kg.available else 'no'} "
          f"({len(kg.ent)} entities) pages={len(set(p['page_id'] for p in data.pages().values()))} chunks={len(data.chunks())}")

    per_query, outputs, lat = [], {}, []
    for case in cases:
        if case["check"] in SKIP_CHECKS:
            per_query.append({"id": case["id"], "cat": case["cat"], "split": case["split"], "check": case["check"],
                              "query": case["query"], "skipped": True})
            continue
        t0 = time.time()
        try:
            out = query(case["query"], k=k)
        except Exception as e:  # noqa: BLE001 - one broken query must not hide the others
            per_query.append({"id": case["id"], "cat": case["cat"], "split": case["split"], "check": case["check"],
                              "query": case["query"], "error": f"{type(e).__name__}: {e}"})
            traceback.print_exc(limit=1)
            continue
        lat.append(time.time() - t0)
        outputs[case["id"]] = out
        rec = eval_case(case, out, kg, k)
        rec["secs"] = round(lat[-1], 2)
        per_query.append(rec)

    scored = [q for q in per_query if q.get("recall") is not None and q["check"] == "recall"]
    recall = mean(q["recall"] for q in scored)
    hit_rate = mean(1.0 if q["recall"] > 0 else 0.0 for q in scored)

    # synonym parity: top-k entity sets of queries that should find the same experts
    tops = {q["id"]: [t["entity_id"] for t in q.get("top", [])] for q in per_query if "top" in q}
    parity = metrics.pairwise({g["id"]: [tops[c] for c in g["cases"] if c in tops] for g in groups})
    if split == "synthetic":  # paired variants written for the same unit
        by_unit = defaultdict(list)
        for c in cases:
            if c.get("pair") and c["id"] in tops:
                by_unit[c["pair"]].append(tops[c["id"]])
        parity = metrics.pairwise({u: v for u, v in by_unit.items() if len(v) > 1})

    # citation validity over every core result shown
    n_reasons = n_valid = n_res = n_res_ok = 0
    why = Counter()
    filled = avail = 0
    per_field = defaultdict(lambda: [0, 0])
    unit_hits, src_shown = [], Counter()
    for qid, out in outputs.items():
        for rank, r in enumerate(core_results(out.get("results") or [], k), 1):
            n_res += 1
            reasons = r.get("reasons") or []
            oks = []
            for reason in reasons:
                ok, how = metrics.check_reason(reason)
                oks.append(ok)
                why[how] += 1
            n_reasons += len(reasons)
            n_valid += sum(oks)
            n_res_ok += bool(reasons) and all(oks)
            f, a, per = completeness(r, kg)
            filled += f
            avail += a
            for name, ok in per.items():
                per_field[name][0] += ok
                per_field[name][1] += 1
            unit_hits.append((result_unit(r, kg), rank))
            src_shown[result_source(r, kg) or "unknown"] += 1

    # per source coverage
    pages_by_src = Counter(p["source"] for p in {p["page_id"]: p for p in data.pages().values()}.values())
    ent_by_src = Counter()
    for e in kg.ent.values():
        for s in e["sources"]:
            ent_by_src[s] += 1
    tgt_by_src = defaultdict(list)
    for q in scored:
        for t in q["targets"]:
            tgt_by_src[t["source"]].append(1.0 if t["rank"] else 0.0)
    all_src = sorted(set(pages_by_src) | set(ent_by_src) | set(tgt_by_src))
    coverage = {s: {"pages": pages_by_src.get(s, 0), "entities": ent_by_src.get(s, 0),
                    "gold_targets": len(tgt_by_src.get(s, [])),
                    "recall_at_10": mean(tgt_by_src.get(s, [])), "results_shown": src_shown.get(s, 0)} for s in all_src}

    index_units = {eid for eid, e in kg.ent.items() if e["type"] == "unit"}
    expo = metrics.exposure(unit_hits, index_units or None)
    tot = sum(src_shown.values()) or 1
    expo["by_source"] = {s: round(n / tot, 3) for s, n in src_shown.most_common()}

    checks = Counter()
    for q in per_query:
        if "pass" in q:
            checks[(q["check"], q["pass"])] += 1
    by_cat = defaultdict(list)
    for q in scored:
        by_cat[q["cat"]].append(q["recall"])
    by_via = Counter()
    for q in scored:
        by_via.update(q.get("hit_via", {}))

    res = {
        "run_at": now(), "split": split, "n": len(cases),
        "metrics": {
            "recall_at_10": recall,
            "synonym_parity": parity["jaccard"],
            "citation_validity": round(n_valid / n_reasons, 3) if n_reasons else None,
            "field_completeness": round(filled / avail, 3) if avail else None,
            "per_source_coverage": coverage,
            "exposure": expo,
            # extras (contract allows more fields)
            "hit_rate_at_10": hit_rate,
            "synonym_overlap": parity["overlap"],
            "synonym_groups": parity["groups"],
            "results_fully_cited": round(n_res_ok / n_res, 3) if n_res else None,
            "citation_failures": {k_: v for k_, v in why.items() if k_ not in ("chunk", "page")},
            "field_completeness_by_field": {f: round(a / b, 3) for f, (a, b) in per_field.items() if b},
            "recall_by_category": {c: mean(v) for c, v in sorted(by_cat.items())},
            "checks": {f"{c}:{'pass' if p else 'fail'}": n for (c, p), n in sorted(checks.items())},
            "hits_via": dict(by_via),
            "latency_mean_s": mean(lat), "latency_p95_s": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 2) if lat else None,
        },
        "backend": backend_name, "label": label, "kg_available": kg.available, "k": k, "heldout_lock": lock_state,
        "n_scored": len(scored), "n_skipped": sum(1 for q in per_query if q.get("skipped")),
        "n_errors": sum(1 for q in per_query if q.get("error")),
        "per_query": per_query,
    }
    report(res)
    if write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(res, indent=1, ensure_ascii=False)
        (OUT_DIR / "latest.json").write_text(blob)
        (OUT_DIR / f"latest_{split}.json").write_text(blob)
        if split == "heldout":
            with open(OUT_DIR / "heldout_runs.jsonl", "a") as f:
                f.write(json.dumps({"run_at": res["run_at"], "backend": backend_name,
                                    "recall_at_10": recall, "lock": lock_state}) + "\n")
            n_runs = sum(1 for _ in open(OUT_DIR / "heldout_runs.jsonl"))
            print(f"held out set has now been run {n_runs} time(s); the playbook asks for once, after the freeze")
        print(f"wrote {OUT_DIR / 'latest.json'}")
    if doc and write:
        update_doc(res)
    return res


# ---------- output ----------

def pct(x):
    return "n/a" if x is None else f"{100 * x:.0f}%"


def summary_lines(res: dict) -> list[str]:
    m = res["metrics"]
    ex = m["exposure"]
    lines = [
        f"split {res['split']} | backend {res['backend']}{' (' + res['label'] + ')' if res.get('label') else ''} | kg {'yes' if res['kg_available'] else 'no'} | "
        f"{res['n_scored']} recall queries, {res['n_skipped']} skipped (manual/needs), {res['n_errors']} errors",
        f"Recall@10            {pct(m['recall_at_10'])}   (hit rate {pct(m['hit_rate_at_10'])}, target >= 80%; adjacent excluded)",
        f"Synonym parity       Jaccard {m['synonym_parity'] if m['synonym_parity'] is not None else 'n/a'}, "
        f"overlap {m['synonym_overlap'] if m['synonym_overlap'] is not None else 'n/a'} over {len(m['synonym_groups'])} groups",
        f"Citation validity    {pct(m['citation_validity'])} of reasons quote their page; "
        f"{pct(m['results_fully_cited'])} of results fully cited; failures {m['citation_failures'] or 'none'}",
        f"Field completeness   {pct(m['field_completeness'])}  "
        + " ".join(f"{f} {pct(v)}" for f, v in m["field_completeness_by_field"].items()),
        f"Exposure             {ex['units_exposed']} units shown, gini {ex['gini']}, top1 {pct(ex['top1_share'])}, "
        f"top5 {pct(ex['top5_share'])}, entropy {ex['entropy_norm']}"
        + (f", {pct(ex.get('share_of_index_units_exposed'))} of index units ever shown" if ex.get("units_in_index") else ""),
        "Checks               " + (", ".join(f"{k}={v}" for k, v in m["checks"].items()) or "none"),
        "Recall by category   " + ", ".join(f"{c} {pct(v)}" for c, v in m["recall_by_category"].items()),
    ]
    return lines


def report(res: dict) -> None:
    print()
    for line in summary_lines(res):
        print(line)
    print("\nPer source coverage")
    print(f"  {'source':20} {'pages':>6} {'entities':>8} {'targets':>7} {'recall':>7} {'shown':>6}")
    for s, c in res["metrics"]["per_source_coverage"].items():
        print(f"  {s:20} {c['pages']:>6} {c['entities']:>8} {c['gold_targets']:>7} {pct(c['recall_at_10']):>7} {c['results_shown']:>6}")
    misses = [q for q in res["per_query"] if q.get("recall") == 0]
    if misses:
        print(f"\nMisses ({len(misses)}): " + ", ".join(q["id"] for q in misses))
    fails = [q for q in res["per_query"] if q.get("pass") is False]
    for q in fails:
        print(f"  FAIL {q['id']} [{q['check']}] {q.get('note', '')}")
    print()


def update_doc(res: dict) -> None:
    """Replace the numbers block for this split in docs/eval.md; the prose around it is hand written."""
    if not DOC.exists():
        return
    tag = res["split"] + (":stub" if res["backend"] == "stub" else "")
    start, end = f"<!-- results:{tag} -->", f"<!-- /results:{tag} -->"
    text = DOC.read_text()
    if start not in text:
        return
    m = res["metrics"]
    label = f" **{res['label']}**." if res.get("label") else ""
    body = [start, f"Run {res['run_at']}, backend `{res['backend']}`, k={res['k']}.{label}", "", "```", *summary_lines(res), "```",
            "", "| source | pages | entities | gold targets | recall@10 | results shown |", "|---|---|---|---|---|---|"]
    for s, c in m["per_source_coverage"].items():
        body.append(f"| {s} | {c['pages']} | {c['entities']} | {c['gold_targets']} | {pct(c['recall_at_10'])} | {c['results_shown']} |")
    body.append(end)
    pre, rest = text.split(start, 1)
    post = rest.split(end, 1)[1] if end in rest else ""
    DOC.write_text(pre + "\n".join(body) + post)
    print(f"updated {DOC}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", choices=["dev", "heldout", "synthetic", "all"], default="dev")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--backend", choices=["auto", "real", "stub"], default="auto")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-write", action="store_true", help="print only, write nothing")
    ap.add_argument("--no-doc", action="store_true", help="do not touch docs/eval.md")
    ap.add_argument("--label", help='backend mode for the record, e.g. "degraded: BM25 + expansions, no dense/rerank"')
    ap.add_argument("--lock", action="store_true", help="record the held out hash in eval/heldout.lock and exit")
    a = ap.parse_args(argv)
    if a.lock:
        check_lock(json.loads(GOLD.read_text()), "dev", write=True)
        return
    run(a.split, a.k, a.backend, a.limit, write=not a.no_write, doc=not a.no_doc, label=a.label)


if __name__ == "__main__":
    main()
