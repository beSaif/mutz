"""query(): expert and unit search over the indexed public pages, per run sheet section 3.

    python -m search.query "glucose sensors"            # prints the JSON
    python -m search.query "CGM" --k 5 --sources artorg dcb
    python -m search.query "glucose sensors" --netlog   # also prints every host this process contacted
    python -m search.query --why-not "Prof. X" "glucose sensors"

Pipeline (each stage is logged to the RankLog, data/ranklog.sqlite, for the top 200 entities):
  1. need type: supplier style needs get "not covered yet" (gap 6)
  2. expansions: Apertus 70B via common.llm (cached), shown to the officer; the caller may pass its own list
  3. retrieval per query string: BM25 (DE/EN/FR tokens) and BGE-M3 dense, fused with RRF (k=60)
  4. aggregation of chunks to people and units through kg.sqlite page_entity (Balog style voting)
  5. local cross encoder (bge-reranker-v2-m3) on the best chunks of the top 50 entities
  6. seniority weight (gap 13) x recency multiplier (active 1.0, quiet 0.8, past 0.6)
  7. per unit cap for fair exposure, abstain with searched_sources and unindexed suggestions (gap 5)

Routing rule (gap 1): query embedding and reranking run on this machine. Models load from the local
cache with HF_HUB_OFFLINE=1. The only outbound call is the expansion request to the Keera gateway.
"""
import argparse, concurrent.futures as cf, hashlib, json, math, os, re, sqlite3, sys, threading, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from search import index as ix
from search.hints import NOT_COVERED_MSG, SOURCE_NAMES, is_supplier_need, suggest_unindexed
from search.text import best_quote, decompound, deep_link, fold, sentences, stem, tokenize

DATA = ix.DATA
KG = DATA / "kg.sqlite"
RANKLOG = DATA / "ranklog.sqlite"

RRF_K = 60
TOP_CHUNKS = 200          # per ranked list
TOP_ENTITIES_RERANK = 50
MAX_RERANK_CHUNKS = int(os.environ.get("MUTZ_MAX_RERANK", "20"))
RERANK_BUDGET = float(os.environ.get("MUTZ_RERANK_BUDGET", "6"))   # seconds per query for the cross encoder
RANKLOG_N = 200
UNIT_CAP = 3              # max results per unit in the top k (fair exposure)
SHOW_BAR = float(os.environ.get("MUTZ_SHOW_BAR", "0.20"))      # reranker prob below which a result is not shown
ABSTAIN_BAR = float(os.environ.get("MUTZ_ABSTAIN_BAR", "0.35"))  # best result below this -> honest no match
STRONG_BAR = 0.5
EXPANSION_WEIGHT = 0.5    # RRF weight of an expansion list vs the original query
EXPANSION_TIMEOUT = float(os.environ.get("MUTZ_EXPANSION_TIMEOUT", "25"))

# chunk -> entity association: how strongly a chunk on this page speaks about the entity
ROLE_W = {"subject": 1.0, "unit_page": 0.8, "member": 0.45, "via_unit": 0.5}
HEAD_SENIORITY = 1.0      # heads and professors inherit their unit's evidence (marked via_unit in reasons)
PAGE_TYPE_W = {"news": 0.4, "publications": 0.6, "other": 0.9}
RECENCY_MULT = {"active": 1.0, "quiet": 0.8, "past": 0.6}
# an infrastructure row shares its unit's pages; it should not push people off the list unless the need asks for it
TYPE_W = {"person": 1.0, "unit": 1.0, "project": 0.9, "infrastructure": 0.6}
JUDGE_N = int(os.environ.get("MUTZ_JUDGE_N", "14"))   # candidates graded by Apertus 70B per query
JUDGE_BATCH = 1           # one candidate per call: batched calls mixed up candidates; at most 4 in flight
JUDGE_TIMEOUT = float(os.environ.get("MUTZ_JUDGE_TIMEOUT", "60"))
SMALL_UNIT_PAGES = 15     # a unit with this many pages or fewer counts as small
SMALL_UNIT_BOOST = 1.1    # small boost for a small unit with a strong quote (fair exposure)
PROXY_ABSTAIN_BAR = 0.85  # degraded mode only (no cross encoder weights): the coverage proxy is cruder
NOW_YEAR = datetime.now(timezone.utc).year
_YEAR = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)")


# ---------------------------------------------------------------- resources (loaded once, reloaded on change)

class Resources:
    def __init__(self):
        self.sig = None
        self.lock = threading.Lock()
        self.embedder = None
        self.reranker = None
        self.degraded = set()

    def signature(self):
        return [ix.file_sig(ix.CHUNKS), ix.file_sig(ix.PAGES), ix.file_sig(KG), ix.file_sig(ix.IDX / "emb.npy")]

    def ensure(self):
        with self.lock:
            sig = self.signature()
            if sig != self.sig:
                self._load()
                self.sig = sig
            return self

    def _load(self):
        t0 = time.time()
        self.chunks = ix.load_chunks()
        self.pages = ix.load_pages()
        self.cid = [c["chunk_id"] for c in self.chunks]
        self.cpos = {c: i for i, c in enumerate(self.cid)}
        self.bm25 = ix.load_or_build_bm25(self.chunks, self.pages)
        if self.bm25.ids != self.cid:     # stale pickle from another chunk order
            self.bm25 = ix.build_bm25(self.chunks, self.pages)
        ids, emb = ix.load_dense()
        # dense matrix aligned to self.chunks; rows without a vector stay zero (BM25 only)
        self.has_dense = np.zeros(len(self.cid), dtype=bool)
        self.emb = None
        if emb is not None and len(ids):
            mat = np.zeros((len(self.cid), emb.shape[1]), dtype=np.float32)
            for j, c in enumerate(ids):
                i = self.cpos.get(c)
                if i is not None:
                    mat[i] = emb[j]
                    self.has_dense[i] = True
            self.emb = mat
        self.page_chunks = defaultdict(list)
        for i, c in enumerate(self.chunks):
            self.page_chunks[c["page_id"]].append(i)
        self._load_entities()
        self.unit_pages = defaultdict(int)
        self.ent_pages = defaultdict(list)
        for pid, ents in self.page_ents.items():
            for eid, role in ents:
                self.ent_pages[eid].append((pid, role))
                if role == "unit_page":
                    self.unit_pages[eid] += 1
        self.unit_people = defaultdict(list)     # unit -> people, most senior first (contact persons)
        for eid, e in self.ent.items():
            if e["type"] == "person" and e.get("unit_id"):
                self.unit_people[e["unit_id"]].append(eid)
        for lst in self.unit_people.values():
            lst.sort(key=lambda p: (-(self.ent[p].get("seniority") or 0), self.ent[p]["name"]))
        self.heads = defaultdict(list)
        for eid, e in self.ent.items():
            if e["type"] == "person" and e.get("unit_id") and (e.get("seniority") or 0) >= HEAD_SENIORITY:
                self.heads[e["unit_id"]].append(eid)
        self.page_years = {pid: _years(" ".join(self.chunks[i]["text"] for i in idx))
                           for pid, idx in self.page_chunks.items()}
        self.sources = sorted({c["source"] for c in self.chunks})
        self.load_secs = round(time.time() - t0, 2)

    def _load_entities(self):
        """page_id -> [(entity_id, role)], entity meta. From kg.sqlite, else a page/unit level fallback."""
        self.page_ents = defaultdict(list)
        self.ent = {}
        self.kg = KG.exists()
        if self.kg:
            try:
                con = sqlite3.connect(f"file:{KG}?mode=ro", uri=True)
                con.row_factory = sqlite3.Row
                self._load_kg(con)
                con.close()
                return
            except sqlite3.Error as e:
                print(f"[search] kg.sqlite unreadable ({e}), using page level fallback", file=sys.stderr)
                self.kg = False
                self.page_ents.clear()
                self.ent.clear()
        self._load_fallback()

    def _load_kg(self, con):
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        merged = {}
        if "merge_log" in tables:
            merged = {r["merged_id"]: r["kept_id"] for r in con.execute("select kept_id, merged_id from merge_log")}

        def resolve(eid):
            seen = set()
            while eid in merged and eid not in seen:
                seen.add(eid)
                eid = merged[eid]
            return eid
        self.resolve = resolve
        for r in con.execute("select * from unit"):
            self.ent[r["id"]] = {"type": "unit", "name": r["name"], "unit_id": r["id"], "unit": r["name"],
                                 "function": r["type"], "seniority": 1.0, "url": r["url"], "source": r["source"],
                                 "parent_id": r["parent_id"]}
        self._fix_unit_names()
        for r in con.execute("select * from person"):
            u = self.ent.get(r["primary_unit_id"] or "", {})
            emails = json.loads(r["emails"] or "[]")
            self.ent[r["id"]] = {"type": "person", "name": display_name(r["name"], emails), "name_raw": r["name"],
                                 "name_norm": r["name_norm"], "emails": emails,
                                 "phones": json.loads(r["phones"] or "[]"),
                                 "unit_id": r["primary_unit_id"], "unit": u.get("name"),
                                 "function": r["primary_function"],
                                 "seniority": r["seniority"] if r["seniority"] is not None else 0.5,
                                 "url": r["profile_url"], "source": (json.loads(r["sources"] or "[]") or [None])[0]}
        if "infrastructure" in tables:
            for r in con.execute("select * from infrastructure"):
                u = self.ent.get(r["unit_id"] or "", {})
                self.ent[r["id"]] = {"type": "infrastructure", "name": r["name"], "unit_id": r["unit_id"],
                                     "unit": u.get("name"), "function": r["kind"], "seniority": 1.0,
                                     "url": r["url"], "source": r["source"]}
        proj_unit = {}
        if "project" in tables:
            for r in con.execute('select id, title, summary, unit_id, start, "end", status, url, source from project'):
                u = self.ent.get(r["unit_id"] or "", {})
                proj_unit[r["id"]] = r["unit_id"]
                self.ent[r["id"]] = {"type": "project", "name": r["title"], "unit_id": r["unit_id"],
                                     "unit": u.get("name"), "function": r["status"], "seniority": 1.0,
                                     "url": r["url"], "source": r["source"], "start": r["start"], "end": r["end"],
                                     "status": r["status"], "summary": r["summary"]}
        for r in con.execute("select page_id, entity_id, role from page_entity"):
            eid = resolve(r["entity_id"])
            role = r["role"]
            if eid in self.ent:
                self.page_ents[r["page_id"]].append((eid, role))
            if eid in proj_unit and proj_unit[eid] in self.ent:   # a project page also counts for its unit
                self.page_ents[r["page_id"]].append((proj_unit[eid], "unit_page"))

    def _fix_unit_names(self):
        """Display name guard: some unit names come out as a page title ('Teaching', 'Publications').
        Use the last breadcrumb of the unit's own page instead. Read only, kg.sqlite is not changed."""
        by_url = {p.get("url"): p for p in self.pages.values()}
        for e in self.ent.values():
            if e["type"] != "unit":
                continue
            n = (e["name"] or "").strip()
            if fold(n) in _GENERIC_UNIT or _GENERIC_UNIT_RX.search(n) or re.search(r"\d{4}|strasse|@", n, re.I) or len(n) > 70 or not n:
                crumbs = (by_url.get(e.get("url")) or {}).get("breadcrumbs") or []
                good = [c for c in crumbs if fold(c) not in _GENERIC_UNIT and not _GENERIC_UNIT_RX.search(c)]
                if good:
                    e["name"] = e["unit"] = good[-1]

    def _load_fallback(self):
        """No kg.sqlite yet: one unit per source (ARTORG: per research group), one person per person page."""
        self.resolve = lambda e: e
        for pid, p in self.pages.items():
            src, url = p.get("source"), p.get("url", "")
            m = re.search(r"artorg\.unibe\.ch/research/([a-z0-9_]+)/", url)
            uid = f"unit:artorg:{m.group(1)}" if m else f"unit:{src}:root"
            if uid not in self.ent:
                crumbs = p.get("breadcrumbs") or []
                name = (crumbs[4] if m and len(crumbs) > 4 else None) or SOURCE_NAMES.get(src) or src
                self.ent[uid] = {"type": "unit", "name": name, "unit_id": uid, "unit": name, "function": None,
                                 "seniority": 1.0, "url": url, "source": src}
            self.page_ents[pid].append((uid, "unit_page"))
            if p.get("page_type") == "person" and p.get("h1"):
                eid = "person:" + hashlib.sha1(fold(p["h1"]).encode()).hexdigest()[:10]
                self.ent.setdefault(eid, {"type": "person", "name": p["h1"], "unit_id": uid,
                                          "unit": self.ent[uid]["name"], "function": None, "seniority": 0.5,
                                          "url": url, "source": src})
                self.page_ents[pid].append((eid, "subject"))

    # models are loaded lazily and kept
    def get_embedder(self):
        if self.embedder is None:
            self.embedder = ix.load_embedder(wait=False)
        return self.embedder

    def get_reranker(self):
        if self.reranker is None:
            self.reranker = ix.load_reranker(wait=False)
        return self.reranker


_GENERIC_UNIT_RX = re.compile(r"^(research|forschung|recherche)\b.*\b(themes?|groups?|gruppen|areas?|topics?)\b|"
                               r"alphabetic|alphabetisch|^(our )?(groups|teams|labs)$", re.I)
_GENERIC_UNIT = {fold(x) for x in ("Teaching", "Publications", "Research", "Job Vacancies", "Facilities", "Home",
                                    "Contact", "Team", "People", "Members", "Group Members", "News", "About us",
                                    "Lehre", "Forschung", "Publikationen", "Kontakt", "Portal", "Persons")}

R = Resources()


def _years(text):
    ys = [int(y) for y in _YEAR.findall(text or "")]
    return [y for y in ys if y <= NOW_YEAR + 6]


# ---------------------------------------------------------------- expansions (Apertus 70B, cached)

_EXPAND_SYS = (
    "You help an economic development officer search Bern university research web pages. "
    "Given a search need, return up to 8 short search terms that a researcher's web page would use for it: "
    "first the plain English translation if the need is not in English; if the need is in lay words, the "
    "technical terms a scientist would use for it (for example 'which proteins are in our samples' -> proteomics, "
    "mass spectrometry); if the need is to find out, measure or analyse something, the name of the method and of "
    "the kind of core facility or service that offers it; then abbreviations, the German and French "
    "equivalents, and one or two closely related fields. "
    "Do not repeat the input. Do not invent names of people or institutes.")

_EXPAND_TOOL = {"type": "function", "function": {
    "name": "expand_query", "description": "Search terms for the need",
    "parameters": {"type": "object", "properties": {
        "terms": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "need_type": {"type": "string", "enum": ["research", "infrastructure", "supplier", "funding", "transfer", "other"]}},
        "required": ["terms", "need_type"]}}}


def expand(text: str):
    """-> (terms, need_type or None, note). Never raises; on any gateway problem returns no expansions."""
    if os.environ.get("MUTZ_NO_LLM") == "1":
        return [], None, "expansions off (MUTZ_NO_LLM=1)"

    def call():
        from common.llm import LARGE, chat
        return chat(LARGE, [{"role": "system", "content": _EXPAND_SYS}, {"role": "user", "content": text}],
                    tools=[_EXPAND_TOOL], tool_choice={"type": "function", "function": {"name": "expand_query"}},
                    temperature=0, max_tokens=300, tag="search.expand", retries=1)

    ex = cf.ThreadPoolExecutor(max_workers=1)
    try:
        out = ex.submit(call).result(timeout=EXPANSION_TIMEOUT)
    except Exception as e:
        ex.shutdown(wait=False)
        return [], None, f"expansions unavailable ({type(e).__name__})"
    ex.shutdown(wait=False)
    args = (out.get("tool_calls") or [{}])[0].get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    seen, terms = {fold(text).strip()}, []
    for t in args.get("terms") or []:
        t = re.sub(r"\s+", " ", str(t)).strip(" .;,")
        if t and fold(t) not in seen and len(t) <= 80:
            seen.add(fold(t))
            terms.append(t)
    return terms[:8], args.get("need_type"), ("cached" if out.get("cached") else "apertus-70b")


# ---------------------------------------------------------------- retrieval

def _rank_lists(res, qstrings, mask):
    """For each query string: BM25 top list and dense top list (chunk indices)."""
    vocab = res.bm25.post
    lists = []
    vecs = None
    if res.emb is not None and os.environ.get("MUTZ_NO_DENSE") != "1":
        try:
            vecs = ix.encode(res.get_embedder(), qstrings)
        except Exception as e:      # degrade to BM25 only, and say so in timings
            res.degraded.add(f"dense unavailable: {type(e).__name__}: {e}"[:200])
    for j, q in enumerate(qstrings):
        toks = tokenize(q)
        extra = [p for t in toks for p in decompound(t, vocab) if t not in vocab]
        s = res.bm25.scores(toks + extra)
        s[~mask] = 0
        top = np.argsort(-s)[:TOP_CHUNKS]
        top = top[s[top] > 0]
        lists.append(("bm25", j, top, s))
        if vecs is not None:
            d = res.emb @ vecs[j].astype(np.float32)
            d[~(mask & res.has_dense)] = -1
            top = np.argsort(-d)[:TOP_CHUNKS]
            top = top[d[top] > 0]
            lists.append(("dense", j, top, d))
    return lists


def _fuse(lists):
    rrf = defaultdict(float)
    info = defaultdict(dict)   # chunk -> {"bm25_rank", "dense_rank", "bm25", "dense"} on the original query, best over all
    for kind, j, top, s in lists:
        w = 1.0 if j == 0 else EXPANSION_WEIGHT
        for r, i in enumerate(top):
            i = int(i)
            rrf[i] += w / (RRF_K + r + 1)
            key = f"{kind}_rank" if j == 0 else f"{kind}_rank_exp"
            if key not in info[i] or r + 1 < info[i][key]:
                info[i][key] = r + 1
                info[i][f"{kind}_score" + ("" if j == 0 else "_exp")] = round(float(s[i]), 4)
    return rrf, info


# ---------------------------------------------------------------- aggregation to entities

def _aggregate(res, rrf):
    """Entity prelim score = sum of chunk RRF x association, with diminishing returns per extra page
    (so a big unit with many pages does not win by volume) and one vote per DE/EN/FR sibling group."""
    contrib = defaultdict(dict)     # eid -> {sibling_group: (score, chunk_idx, role)}
    for i, sc in rrf.items():
        c = res.chunks[i]
        page = res.pages.get(c["page_id"], {})
        pw = PAGE_TYPE_W.get(page.get("page_type"), 1.0)
        sg = page.get("sibling_group") or c["page_id"]
        for eid, role in res.page_ents.get(c["page_id"], []):
            v = sc * ROLE_W.get(role, 0.5) * pw
            cur = contrib[eid]
            # keep the best chunk per sibling group, but remember others for evidence
            best = cur.get(sg)
            if best is None or v > best[0]:
                cur[sg] = (v, i, role)
    # a group head inherits the group's pages: the research text sits on group pages, not on the profile
    for uid, heads in res.heads.items():
        if uid not in contrib:
            continue
        for pid in heads:
            cur = contrib[pid]
            for sg, (v, i, role) in list(contrib[uid].items()):
                w = v / ROLE_W.get(role, 0.5) * ROLE_W["via_unit"]
                if sg not in cur or w > cur[sg][0]:
                    cur[sg] = (w, i, "via_unit")
    prelim = {}
    for eid, groups in contrib.items():
        vals = sorted(groups.values(), key=lambda x: -x[0])
        prelim[eid] = sum(v * (0.6 ** n) for n, (v, _, _) in enumerate(vals))
    return prelim, contrib


def _recency(res, eid, contrib):
    """Active / quiet / past from years written on the entity's own pages (Studies section).
    Projects first use their status and end date when the page states them.
    No year anywhere: 'active', basis 'listed on the live site', since the page exists in today's crawl."""
    e = res.ent[eid]
    if e["type"] == "project":
        end = _YEAR.search(str(e.get("end") or ""))
        st = (e.get("status") or "").lower()
        if st in ("recruiting", "ongoing"):
            return "active", int(end.group(1)) if end else None, f"status on the page: {st}"
        if end:
            y = int(end.group(1))
            label = "active" if y >= NOW_YEAR else "quiet" if y >= NOW_YEAR - 3 else "past"
            return label, y, f"end date on the page: {e.get('end')}"
        if st == "completed":
            return "quiet", None, "status on the page: completed"
    years, basis_url = [], None
    for sg, (v, i, role) in contrib.get(eid, {}).items():
        pid = res.chunks[i]["page_id"]
        ys = res.page_years.get(pid, [])
        if ys and (not years or max(ys) > max(years)):
            basis_url = res.chunks[i]["url"]
        years += ys
    if not years:
        return "active", None, "no dated activity found; listed on a page in the current crawl"
    y = max(years)
    label = "active" if y >= NOW_YEAR - 2 else "quiet" if y >= NOW_YEAR - 5 else "past"
    return label, y, f"latest year on its pages: {y}" + (f" ({basis_url})" if basis_url else "")


def _rerank(res, qtext, chunk_ids, rrf=None, qstrings=()):
    if not chunk_ids:
        return {}
    if os.environ.get("MUTZ_NO_RERANK") == "1" or os.environ.get("MUTZ_CROSS_ENCODER") == "0":
        if os.environ.get("MUTZ_CROSS_ENCODER") == "0":
            res.degraded.add("reranker unavailable: cross encoder switched off (MUTZ_CROSS_ENCODER=0)")
        return _proxy_rerank(res, qstrings, chunk_ids, rrf)
    try:
        model = res.get_reranker()
    except Exception as e:
        res.degraded.add(f"reranker unavailable, term coverage proxy used: {type(e).__name__}: {e}"[:200])
        return _proxy_rerank(res, qstrings, chunk_ids, rrf)
    cap = 1000 if str(getattr(model, "device", "cpu")).startswith("cpu") else 2000   # CPU: shorter passages
    pairs = [(qtext, ix.chunk_text_for_index(res.chunks[i], res.pages.get(res.chunks[i]["page_id"]))[:cap])
             for i in chunk_ids]
    # time budget: batches of 8; stop as soon as the budget is spent or the first batch shows it will be
    t0, out, B = time.time(), [], 8
    for b in range(0, len(pairs), B):
        out.extend(ix.rerank_logits(model, pairs[b:b + B], batch=B))
        spent = time.time() - t0
        left = len(pairs) - len(out)
        if left and (spent > RERANK_BUDGET or spent / len(out) * left + spent > RERANK_BUDGET * 1.5):
            res.degraded.add(f"reranker unavailable: cross encoder over its {RERANK_BUDGET:.0f}s budget "
                             f"({len(out)} of {len(pairs)} pairs in {spent:.1f}s), Apertus grade used")
            return _proxy_rerank(res, qstrings, chunk_ids, rrf)
    lg = np.asarray(out, dtype=np.float64)
    probs = 1 / (1 + np.exp(-lg))
    # bge-reranker-v2-m3 gives keyword style needs very low absolute probabilities (a clear match can be
    # logit -4), so blend the absolute probability with this query's min-max position. Acceptance and
    # abstain then come from the Apertus grade, not from an absolute reranker bar.
    span = float(lg.max() - lg.min()) or 1.0
    rel = (lg - lg.min()) / span
    return {i: float(0.5 * p + 0.5 * q) for i, p, q in zip(chunk_ids, probs, rel)}


def _query_sets(res, qstrings):
    """Token sets per query string, German compounds the index does not know replaced by their known halves.
    -> [(tokens, weight)]: original 1.0, multi word expansion 0.8, single word expansion 0.4 (weak evidence)."""
    vocab = res.bm25.post
    qs = []
    for j, q in enumerate(qstrings or [""]):
        t = set()
        for tok in tokenize(q):
            parts = decompound(tok, vocab) if tok not in vocab else []
            t |= set(parts) if parts else {tok}
        if t:
            qs.append((t, 1.0 if j == 0 else 0.8 if len(t) > 1 else 0.4))
    return qs


def _term_cov(qs, toks):
    """Best weighted share of one query string's terms found in toks (0..1)."""
    return max((w * len(q & toks) / len(q) for q, w in qs), default=0.0)


def _chunk_toks(res, i):
    return set(tokenize(ix.chunk_text_for_index(res.chunks[i], res.pages.get(res.chunks[i]["page_id"]))))


def _proxy_rerank(res, qstrings, chunk_ids, rrf):
    """Stand in when the cross encoder weights are not on disk (flagged as degraded): a chunk scores high
    only if it contains all the terms of the query or of one expansion. coverage^2 x (0.5 + 0.5 x RRF share)."""
    top = max(rrf[i] for i in chunk_ids)
    qs = _query_sets(res, qstrings)
    out = {}
    for i in chunk_ids:
        toks = _chunk_toks(res, i)
        cov = max((w * (len(q & toks) / len(q)) ** 2 for q, w in qs), default=0.0)
        out[i] = cov * (0.5 + 0.5 * rrf[i] / top)
    return out


# ---------------------------------------------------------------- main entry

def _source_mask(res, sources):
    if not sources:
        return np.ones(len(res.chunks), dtype=bool), res.sources
    want = set(sources)
    keep = [s for s in res.sources if s in want or s.split("_")[0] in want]
    ks = set(keep)
    return np.array([c["source"] in ks for c in res.chunks], dtype=bool), keep


def query(text, k=10, sources=None, expansions=None, need_type=None, log=True):
    t0 = time.time()
    text = (text or "").strip()
    res = R.ensure()
    res.degraded = set()      # per query: what ran in a reduced form this time
    mask, searched = _source_mask(res, sources)
    out = {"query": text, "expansions": [], "searched_sources": searched, "results": [],
           "searched_sources_detail": [{"id": s, "name": _source_name(s),
                                        "chunks": int(sum(1 for c in res.chunks if c["source"] == s))}
                                       for s in searched]}
    if not text:
        out.update(abstained=True, message="Empty query.")
        return out

    # 1-2. need type and expansions (in parallel with nothing else; the gateway call is the slow part)
    note = "given by caller"
    llm_type = None
    if expansions is None:
        expansions, llm_type, note = expand(text)
    else:
        expansions = _norm_expansions(expansions)
    out["expansions"] = list(expansions)
    out["expansion_source"] = note
    supplier = (need_type == "supplier") or (need_type is None and (is_supplier_need(text) or llm_type == "supplier"))
    if supplier:
        out.update(abstained=True, coverage="not_covered", need_type="supplier", message=NOT_COVERED_MSG,
                   suggested_sources=suggest_unindexed("supplier company " + text))
        out["timings"] = {"total": round(time.time() - t0, 2)}
        if log:
            out["query_id"] = _log(text, k, searched, out, [], {})
        return out

    # 3. retrieval + RRF
    t1 = time.time()
    qstrings = [text] + list(expansions)
    lists = _rank_lists(res, qstrings, mask)
    rrf, cinfo = _fuse(lists)
    t2 = time.time()

    # 4. aggregation
    prelim, contrib = _aggregate(res, rrf)
    order = sorted(prelim, key=lambda e: -prelim[e])
    cand = order[:RANKLOG_N]
    prelim_rank = {e: n + 1 for n, e in enumerate(order)}

    # 5. rerank: best 2 chunks of each of the top 50 entities, unique chunks, capped
    want, seen = [], set()
    for rnd in range(2):
        for eid in cand[:TOP_ENTITIES_RERANK]:
            chs = sorted(contrib[eid].values(), key=lambda x: -x[0])
            if len(chs) > rnd:
                i = chs[rnd][1]
                if i not in seen and len(want) < MAX_RERANK_CHUNKS:
                    seen.add(i)
                    want.append(i)
    # a short or lay query gets its first two expansions as context for the cross encoder ("CGM" alone is cryptic)
    rr_query = text if len(text.split()) > 4 or not expansions else f"{text} ({'; '.join(expansions[:2])})"
    rr = _rerank(res, rr_query, want, rrf, qstrings)
    t3 = time.time()

    # 6. scores. A person's evidence must name them: their own page, or a chunk that contains their name.
    terms = set(t for q in qstrings for t in tokenize(q))
    qsets = _query_sets(res, qstrings)
    all_q = set().union(*[q for q, _ in qsets]) if qsets else set()
    rows = []
    for eid in cand:
        e = res.ent[eid]
        chs = sorted(contrib[eid].values(), key=lambda x: -x[0])
        named = chs
        if e["type"] == "person":
            named = [c for c in chs if c[2] == "subject" or _names(res, eid, c[1])]
        scored = sorted([(rr[i] * (0.7 if role in ("member", "via_unit") else 1.0), i, role)
                         for _, i, role in named if i in rr], key=lambda x: -x[0])
        match = (scored[0][0] + 0.15 * (scored[1][0] if len(scored) > 1 else 0)) if scored else 0.0
        rec, rec_year, rec_basis = _recency(res, eid, contrib)
        sen = float(e.get("seniority") or 0.5) if e["type"] == "person" else 1.0
        tw = 1.0 if need_type == "infrastructure" else TYPE_W.get(e["type"], 1.0)
        small = res.unit_pages.get(e.get("unit_id") or eid, 99) <= SMALL_UNIT_PAGES
        boost = SMALL_UNIT_BOOST if (small and match >= STRONG_BAR) else 1.0   # fair exposure for small groups
        final = match * sen * RECENCY_MULT[rec] * tw * boost
        strong_pages = {res.pages.get(res.chunks[i]["page_id"], {}).get("sibling_group") or res.chunks[i]["page_id"]
                        for p, i, _ in scored if p >= STRONG_BAR}
        rows.append({
            "eid": eid, "e": e, "match": match, "final": final, "rerank": scored[0][0] if scored else None,
            "seniority": sen, "recency": rec, "recency_year": rec_year, "recency_basis": rec_basis, "boost": boost,
            "prelim": prelim[eid], "prelim_rank": prelim_rank[eid], "named": bool(named),
            "bm25_rank": min((cinfo.get(i, {}).get("bm25_rank", 10**6) for _, i, _ in chs), default=None),
            "dense_rank": min((cinfo.get(i, {}).get("dense_rank", 10**6) for _, i, _ in chs), default=None),
            "pages": len(chs), "strong_pages": len(strong_pages),
            "evidence": [(p, i, role) for p, i, role in scored[:3]]})
    rows.sort(key=lambda r: (-r["final"], -r["prelim"]))
    notes = {}

    # 7. quotes and the padding guard: every reason quote must carry a term of the need or an expansion
    def build_reasons(r):
        e = r["e"]
        name_toks = set(_norm_name(e["name"])) if e["type"] == "person" else set()
        reasons = []
        for p, i, role in r["evidence"]:
            c = res.chunks[i]
            mention = None
            if name_toks and role != "subject":
                sur = set(t for t in _norm_name(e.get("name_raw") or e["name"]) if len(t) > 2)
                naming = [x for x in sentences(c["text"]) if set(_norm_name(x)) & sur]
                cands = [x for x in naming if set(tokenize(x)) & all_q]
                page_type = res.pages.get(c["page_id"], {}).get("page_type")
                if cands:
                    q = max(cands, key=lambda x: len(set(tokenize(x)) & all_q))
                    if len(q.split()) > 45:
                        q = best_quote(q, terms)
                elif page_type in ("group", "unit", "publications", "news") or not naming:
                    # a member list or overview names many people: the quote itself must name this person
                    continue
                else:
                    # a project or topic page that names the person: quote the work, and show the naming line
                    q = best_quote(c["text"], terms)
                    mention = naming[0] if len(naming[0].split()) <= 45 else best_quote(naming[0], sur)
            else:
                q = best_quote(c["text"], terms)
            if not (set(tokenize(q)) & all_q):
                continue
            reasons.append({"chunk_id": c["chunk_id"], "url": c["url"], "quote": q,
                            "deep_link": deep_link(c["url"], q),
                            "rerank": round(p, 3) if p is not None else None,
                            "via": "unit" if role == "via_unit" else "member_list" if role == "member" else "page",
                            **({"mention": mention, "mention_link": deep_link(c["url"], mention)} if mention else {})})
        return reasons

    def passes(r):
        if e_is_person_unnamed(r):
            notes[r["eid"]] = "no indexed page names this person for the need: listed under the unit"
            return False
        if r["rerank"] is None:
            notes[r["eid"]] = "not reranked (outside the top 50)"
            return False
        if r["match"] < SHOW_BAR:
            notes[r["eid"]] = "below the show bar"
            return False
        toks = set().union(*[_chunk_toks(res, i) for _, i, _ in r["evidence"]])
        r["term_cov"] = round(_term_cov(qsets, toks), 2)
        # with the cross encoder the guard is strict; without it (proxy scores) Apertus judges the borderline ones
        min_cov = 0.6 if not proxy_mode else (0.34 if judge_on else 0.6)
        if r["rerank"] < STRONG_BAR and r["term_cov"] < min_cov:
            notes[r["eid"]] = f"thin: evidence holds {r['term_cov']:.0%} of the need's terms and the reranker is unsure"
            return False
        r["reasons"] = build_reasons(r)
        if not r["reasons"]:
            notes[r["eid"]] = "coverage none: no quote carries the need's terms"
            return False
        return True

    def e_is_person_unnamed(r):
        return r["e"]["type"] == "person" and not r["named"]

    proxy_mode = os.environ.get("MUTZ_NO_RERANK") == "1" or any("reranker unavailable" in d for d in res.degraded)
    judge_on = os.environ.get("MUTZ_NO_LLM") != "1" and os.environ.get("MUTZ_NO_JUDGE") != "1"
    ok = [r for r in rows if passes(r)]

    # 8. Apertus 70B grades the top candidates from their quotes only, and writes the [n] cited reason
    t4 = time.time()
    proxy = os.environ.get("MUTZ_NO_RERANK") == "1" or any("reranker unavailable" in d for d in res.degraded)
    judged = _judge(text, expansions, ok[:JUDGE_N])
    qsets_direct = [(q, w) for q, w in qsets if w >= 0.8]     # original query and multi word expansions
    no_support = 0
    if judged is not None:
        kept = []
        for n, r in enumerate(ok):
            j = judged.get(n + 1)
            if j is None:
                if n < JUDGE_N:
                    r["explanation"] = None
                kept.append(r)
                continue
            r["grade"], r["explanation"] = j["grade"], j["explanation"]
            r["names_need"] = _quotes_name_need(r, qsets_direct, res.bm25.idf)
            if r["grade"] >= 3 and not r["names_need"]:
                r["grade"] = 2       # direct match needs the need's own terms (or a full expansion) in the quotes
                j = {**j, "grade": 2}
            if j["grade"] <= 0 and not proxy and r["match"] >= 0.8:
                # the local cross encoder is confident and the quotes carry the terms: keep, flag the disagreement
                j = {"grade": 1, "explanation": None}
                r["grade"], r["explanation"] = 1, None
                notes[r["eid"]] = "Apertus said NO_SUPPORT, cross encoder disagreed (kept as adjacent)"
            if j["grade"] <= 0:
                no_support += 1
                notes[r["eid"]] = "Apertus found no support in the quotes (NO_SUPPORT)"
                continue
            if True:    # the 70B grade, read from the quotes, carries most of the ranking
                r["match"] = 0.6 * j["grade"] / 3 + 0.4 * r["match"]
                r["final"] = r["match"] * r["seniority"] * RECENCY_MULT[r["recency"]] * \
                    (1.0 if need_type == "infrastructure" else TYPE_W.get(r["e"]["type"], 1.0)) * r["boost"]
            kept.append(r)
        ok = sorted(kept, key=lambda r: -r["final"])
    t5 = time.time()

    # 9. name queries ("Lisa Koch") go straight to the Person table
    name_rows = _name_hits(res, text, mask)
    # 10. people next to their unit, cap per unit
    people_of = defaultdict(list)
    for r in ok:
        if r["e"]["type"] == "person" and r["e"].get("unit_id"):
            people_of[r["e"]["unit_id"]].append(r)
    for lst in people_of.values():
        lst.sort(key=lambda r: (-r["seniority"], -r["final"]))
    ordered, placed = list(name_rows), {r["eid"] for r in name_rows}
    if name_rows:      # the query is a person's name: show that person, not topic matches for the name's words
        ok = []
    for r in ok:
        if r["eid"] in placed:
            continue
        ordered.append(r)
        placed.add(r["eid"])
        if r["e"]["type"] == "unit":       # its best evidenced, most senior people follow it
            for p in people_of.get(r["eid"], [])[:2]:
                if p["eid"] not in placed:
                    ordered.append(p)
                    placed.add(p["eid"])
    shown, per_unit, first_quotes = [], defaultdict(int), set()
    for r in ordered:
        unit = r["e"].get("unit_id") or r["eid"]
        fq = r["reasons"][0]["chunk_id"] if r.get("reasons") else None
        if r["e"]["type"] == "infrastructure" and fq in first_quotes:
            notes[r["eid"]] = "same evidence as a result already shown"
            continue
        if len(shown) >= k:
            notes.setdefault(r["eid"], "ranked below the cut")
            continue
        if per_unit[unit] >= UNIT_CAP and not r.get("name_hit"):
            notes[r["eid"]] = f"cut by the fairness cap: its unit already has {UNIT_CAP} of the top {k}"
            continue
        per_unit[unit] += 1
        shown.append(r)
        if fq:
            first_quotes.add(fq)
    best = max([r["match"] for r in ok] + [1.0 if name_rows else 0.0], default=0.0)
    abstain_bar = PROXY_ABSTAIN_BAR if (proxy and judged is None) else ABSTAIN_BAR
    for n, r in enumerate(rows):
        r["final_rank"] = n + 1
    for r in rows:
        if r["eid"] in {s["eid"] for s in shown}:
            notes[r["eid"]] = "shown"

    shown_ids = {r["eid"] for r in shown}
    results = []
    for r in shown:
        e = r["e"]
        cov = "strong" if r.get("name_hit") or r["strong_pages"] >= 2 or (r["strong_pages"] == 1 and r["match"] >= 0.8) \
            else "thin"
        if r.get("grade") == 3 and cov == "thin" and len(r["reasons"]) >= 2:
            cov = "strong"
        item = {
            "entity_id": r["eid"], "type": e["type"], "name": e["name"], "unit": e.get("unit"),
            "unit_id": e.get("unit_id"), "function": e.get("function"), "score": round(r["final"], 4),
            "match": round(r["match"], 4), "boost": r.get("boost", 1.0),   # parts of score, shown to the officer
            "coverage": cov, "recency": r["recency"], "recency_year": r["recency_year"],
            "recency_basis": r["recency_basis"], "seniority": r["seniority"], "url": e.get("url"),
            "source": e.get("source"), "reasons": r["reasons"], "explanation": r.get("explanation"),
            "grade": r.get("grade"), "adjacent": r.get("grade") == 1, "tags": ["adjacent"] if r.get("grade") == 1 else []}
        if e["type"] == "project":
            item.update(status=e.get("status"), start=e.get("start"), end=e.get("end"))
        if e["type"] == "person":
            item["email"] = (e.get("emails") or [None])[0]
            item["phone"] = (e.get("phones") or [None])[0]
            item["contact"] = {"email": (e.get("emails") or [None])[0], "phone": (e.get("phones") or [None])[0],
                               "function": e.get("function"), "profile_url": e.get("url")}
        if e["type"] == "unit":
            # people of this unit: first those the search found (not shown separately), then the most senior
            ppl, seen_p = [], set()
            for p in people_of.get(r["eid"], []) + [x for x in rows if x["e"]["type"] == "person"
                                                    and x["e"].get("unit_id") == r["eid"] and not x["named"]]:
                if p["eid"] in shown_ids or p["eid"] in seen_p:
                    continue
                seen_p.add(p["eid"])
                ppl.append(_person_brief(res, p["eid"], matched=True))
            for pid in res.unit_people.get(r["eid"], []):
                if len(ppl) >= 5:
                    break
                if pid not in shown_ids and pid not in seen_p:
                    seen_p.add(pid)
                    ppl.append(_person_brief(res, pid, matched=False))
            ppl.sort(key=lambda p: (not p["matched"], not _is_head(p["function"]), -(p["seniority"] or 0)))
            item["people"] = ppl[:5]
            item["contacts"] = item["people"]          # older name of the same field
        results.append(item)
    out["results"] = results
    out["abstained"] = not results or best < abstain_bar
    direct_ids = {r["eid"] for r in shown if r.get("name_hit") or (r.get("grade") or 0) >= 3
                  or ((r.get("grade") or 0) >= 2 and r.get("names_need"))}
    if judged is not None and results and not out["abstained"] and not direct_ids:
        # Apertus found only related or adjacent work: say so honestly instead of presenting it as a match
        out["abstained"] = True
        out["adjacent_results"] = results
        out["no_direct_match"] = True
    if out["abstained"]:
        out["results"] = []
        names = ", ".join(_source_name(s) for s in searched)
        out["message"] = (f"No strong Bern match: not found in the indexed sources ({names}). "
                          "This may mean the expertise sits on a site we do not index yet."
                          + (" Related or adjacent work is listed under adjacent_results." if out.get("adjacent_results")
                             else ""))
        out["suggested_sources"] = suggest_unindexed(" ".join(qstrings))
        out["near_misses"] = [{"entity_id": r["eid"], "name": r["e"]["name"], "rerank": round(r["match"], 3)}
                              for r in rows[:3] if r["rerank"] is not None]
    out["coverage"] = "none" if out["abstained"] else ("strong" if any(r["coverage"] == "strong" for r in results)
                                                       else "thin")
    dense_on = res.emb is not None and os.environ.get("MUTZ_NO_DENSE") != "1" and \
        not any("dense unavailable" in d for d in res.degraded)
    out["timings"] = {"retrieve": round(t2 - t1, 2), "rerank": round(t3 - t2, 2),
                      "total": round(time.time() - t0, 2), "reranked_chunks": len(want), "rerank_query": rr_query,
                      "reranker": ("apertus-70b graded" if judged is not None else "term-coverage proxy") if proxy
                      else "bge-reranker-v2-m3" + (" + apertus-70b grade" if judged is not None else ""),
                      "judge": "apertus-70b" if judged is not None else "unavailable", "no_support_dropped": no_support,
                      "kg": res.kg, "degraded": sorted(res.degraded) or None, "dense": dense_on,
                      "stages": {"bm25": True, "dense_bge_m3": dense_on, "cross_encoder": not proxy,
                                 "apertus_expansions": bool(expansions) and note != "given by caller",
                                 "apertus_judge": judged is not None}}
    out["timings"]["judge_secs"] = round(t5 - t4, 2)
    if log:
        out["query_id"] = _log(text, k, searched, out, rows, notes)
    return out

def _quotes_name_need(r, qsets_direct, idf=None):
    """Do the quotes carry the need's own terms (or one expansion phrase)? Coverage is weighted by idf, so the
    distinctive word counts: 'solid state' alone does not cover 'solid-state batteries', but 'trypanosomes'
    and 'gene' cover 'trypanosome gene expression' well."""
    toks = set().union(*[set(tokenize(x["quote"])) for x in r.get("reasons", [])]) if r.get("reasons") else set()
    idf = idf or {}
    top = max(idf.values(), default=10.0)
    for q, _ in qsets_direct:
        if not q:
            continue
        wsum = sum(idf.get(t, top) for t in q)
        if wsum and sum(idf.get(t, top) for t in q & toks) / wsum >= 0.6:
            return True
    return False


def _norm_expansions(x):
    """Caller expansions: a list of strings, a comma/semicolon/newline separated string, or a list of dicts
    like {"term"|"text": ..., "on"|"checked"|"enabled": bool} from the UI. Unticked ones are dropped."""
    if isinstance(x, str):
        x = re.split(r"[;,\n]+", x)
    out = []
    for t in x or []:
        if isinstance(t, dict):
            if not t.get("on", t.get("checked", t.get("enabled", True))):
                continue
            t = t.get("term") or t.get("text") or t.get("label") or ""
        t = re.sub(r"\s+", " ", str(t)).strip(" .;,")
        if t and t not in out:
            out.append(t)
    return out


def display_name(name, emails=()):
    """'Salanti Georgia' -> 'Georgia Salanti' when the email (georgia.salanti@...) shows given name first."""
    toks = (name or "").split()
    if len(toks) < 2:
        return name
    for em in emails or []:
        local = [p for p in re.split(r"[._-]", fold(em.split("@")[0])) if p]
        if len(local) < 2:
            continue
        ft = [fold(t) for t in toks]
        if ft[0] == local[-1] and ft[-1] == local[0]:
            return " ".join(toks[1:] + toks[:1])
    return name


def _names(res, eid, i):
    """Does chunk i name this person? Surname plus given name (or initial) in the chunk text."""
    toks = _norm_name(res.ent[eid].get("name_raw") or res.ent[eid]["name"])
    if len(toks) < 2:
        return False
    text = " " + " ".join(_norm_name(res.chunks[i]["text"])) + " "
    hits = [t for t in toks if len(t) > 1 and f" {t} " in text]
    return len(hits) >= 2


_HEAD = re.compile(r"head|leiter|director|direktor|professor|prof\.|chair|\bpi\b|principal investigator|responsable",
                   re.I)


def _is_head(function):
    return bool(function and _HEAD.search(function))


def _person_brief(res, pid, matched):
    e = res.ent[pid]
    ev = next((p for p, role in res.ent_pages.get(pid, []) if role == "subject"), None)
    return {"entity_id": pid, "name": e["name"], "function": e.get("function"), "seniority": e.get("seniority"),
            "url": e.get("url") or (res.pages.get(ev, {}).get("url") if ev else None),
            "evidence_url": res.pages.get(ev, {}).get("url") if ev else e.get("url"), "matched": matched}


def _name_hits(res, text, mask):
    """A query that is a person's name returns that person first, with their own page as the reason."""
    toks = _norm_name(text)
    if not (2 <= len(toks) <= 4) or len(text) > 60:
        return []
    from rapidfuzz import fuzz
    target = " ".join(sorted(toks))
    allowed = {res.chunks[i]["page_id"] for i in np.nonzero(mask)[0]}
    out = []
    for eid, e in res.ent.items():
        if e["type"] != "person":
            continue
        if fuzz.token_sort_ratio(target, " ".join(sorted(_norm_name(e.get("name_raw") or e["name"])))) < 90:
            continue
        pages = [p for p, role in res.ent_pages.get(eid, []) if p in allowed]
        pages.sort(key=lambda p: 0 if (p, "subject") in res.ent_pages.get(eid, []) else 1)
        reasons = []
        for pid in pages:
            for i in res.page_chunks.get(pid, []):
                if _names(res, eid, i) or (pid, "subject") in res.ent_pages.get(eid, []):
                    c = res.chunks[i]
                    q = best_quote(c["text"], set(tokenize(e["name"])))
                    reasons.append({"chunk_id": c["chunk_id"], "url": c["url"], "quote": q,
                                    "deep_link": deep_link(c["url"], q), "rerank": None, "via": "name"})
                    break
            if len(reasons) >= 2:
                break
        if not reasons:
            continue
        out.append({"eid": eid, "e": e, "match": 1.0, "final": 1.0, "rerank": None, "seniority": e.get("seniority") or 0.5,
                    "recency": "active", "recency_year": None, "recency_basis": "name match", "boost": 1.0,
                    "strong_pages": len(reasons), "reasons": reasons, "name_hit": True, "named": True,
                    "explanation": f"Name match: {e['name']} appears on {len(reasons)} indexed page(s) [1].",
                    "grade": 3})
    return out[:3]


_JUDGE_SYS = (
    "You check search results for an economic development officer. You get one candidate (a group, a person, "
    "a project or a facility) with numbered quotes copied from public web pages. Using ONLY those quotes:\n"
    "grade: 3 = the quotes show work on the need's topic, a synonym or the same technology (continuous glucose "
    "monitoring counts for glucose sensors); 2 = work in the same broader field; 1 = an adjacent field that "
    "could still help; 0 = the quotes do not support a match at all. General chemistry is not battery chemistry; "
    "general imaging is not a specific imaging method.\n"
    "reason: ONE plain sentence for the officer saying what this group or person works on and how that fits the "
    "need, ending with the quote numbers it relies on, like [1] or [1][2]. Example: 'Works on optical sensors "
    "and machine learning for diabetes, which fits continuous glucose tracking [1].' Start with a verb or with "
    "the work, never with 'The quote', 'The candidate' or 'This candidate'. Name the concrete topic, not 'the "
    "need'. If the grade is 0 the reason is exactly NO_SUPPORT. Never add facts that are not in the quotes. "
    "Say what fits, not what is missing. Never promise that anyone will meet or has agreed to anything. "
    "No marketing words.")

_JUDGE_TOOL = {"type": "function", "function": {
    "name": "grade_candidate", "description": "Grade and explain the candidate",
    "parameters": {"type": "object", "properties": {
        "grade": {"type": "integer", "minimum": 0, "maximum": 3},
        "reason": {"type": "string"}}, "required": ["grade", "reason"]}}}


JUDGE_ERRORS = []


def _judge(text, expansions, rows):
    """-> {row index: {"grade", "explanation"}} or None when the gateway is off or failed.
    Batches of JUDGE_BATCH, at most 4 in flight, cached by common.llm. Citations are checked against the
    row's own quotes; an explanation citing a quote number that does not exist is dropped."""
    if not rows or os.environ.get("MUTZ_NO_LLM") == "1" or os.environ.get("MUTZ_NO_JUDGE") == "1":
        return None
    from common.llm import LARGE, chat

    def one(batch):
        lines = [f"Need: {text}"]
        if expansions:
            lines.append("Related terms: " + "; ".join(expansions))
        for n, r in batch:
            e = r["e"]
            lines.append(f"\n{e['type'].capitalize()}: {e['name']} ({e['type']}" + (f", {e.get('unit')}" if e.get("unit") and
                                                                           e.get("unit") != e["name"] else "") + ")")
            for m, rs in enumerate(r["reasons"][:3], 1):
                lines.append(f"[{m}] {rs['quote']}")
        out = chat(LARGE, [{"role": "system", "content": _JUDGE_SYS}, {"role": "user", "content": "\n".join(lines)}],
                   tools=[_JUDGE_TOOL], tool_choice={"type": "function", "function": {"name": "grade_candidate"}},
                   temperature=0, max_tokens=220, tag="search.judge", retries=1)
        args = (out.get("tool_calls") or [{}])[0].get("arguments") or {}
        if isinstance(args, str):      # malformed JSON from the model: pull the two fields out by pattern
            g = re.search(r'"grade"\s*:\s*(\d)', args)
            x = re.search(r'"reason"\s*:\s*"((?:[^"\\]|\\.)*)"', args)
            args = {"grade": int(g.group(1)) if g else None, "reason": x.group(1) if x else ""}
        if args.get("grade") is None:
            raise ValueError("no grade in judge answer")
        n, r = batch[0]
        g = max(0, min(3, int(args["grade"])))
        expl = str(args.get("reason") or args.get("explanation") or "").strip()
        if re.match(r"(the|this) (quote|quotes|candidate)\b", expl, re.I):
            expl = None if g > 0 else expl     # grader's notes, not a reason for the officer
        expl = expl or ""
        nq = len(r["reasons"][:3])
        cites = [int(x) for x in re.findall(r"\[(\d+)\]", expl)]
        if g > 0 and (not cites or any(x < 1 or x > nq for x in cites) or "NO_SUPPORT" in expl):
            expl = None          # uncited or miscited: keep the grade, never show an unsupported sentence
        got = {n: {"grade": g, "explanation": expl if g > 0 else None}}
        return got

    idx = list(enumerate(rows, 1))
    batches = [idx[i:i + JUDGE_BATCH] for i in range(0, len(idx), JUDGE_BATCH)]
    res = {}
    ex = cf.ThreadPoolExecutor(max_workers=min(4, len(batches)))
    futs = [ex.submit(one, b) for b in batches]
    ok = False
    try:
        for f in cf.as_completed(futs, timeout=JUDGE_TIMEOUT):
            try:
                res.update(f.result())
                ok = True
            except Exception as e:
                JUDGE_ERRORS.append(f"{type(e).__name__}: {e}"[:300])
    except cf.TimeoutError:
        pass
    ex.shutdown(wait=False)
    return res if ok else None


def _source_name(s):
    if s in SOURCE_NAMES:
        return SOURCE_NAMES[s]
    return s.replace("unibe_", "UniBE ").upper() if s.startswith("unibe_") else s


def warmup():
    """Load the index and both local models (call once at app start; the first query is then fast)."""
    res = R.ensure()
    status = {"chunks": len(res.chunks), "kg": res.kg, "load_secs": res.load_secs}
    try:
        if res.emb is not None:
            ix.encode(res.get_embedder(), ["warmup"])
        status["dense"] = res.emb is not None
    except Exception as e:
        status["dense"] = f"unavailable: {e}"
    try:
        ix.rerank_logits(res.get_reranker(), [("warmup", "warmup")])
        status["cross_encoder"] = True
    except Exception as e:
        status["cross_encoder"] = f"unavailable: {e}"
    return status


# ---------------------------------------------------------------- RankLog

_SCHEMA = """
create table if not exists rank_query(query_id text primary key, text text, text_norm text, at text, k integer,
  sources text, expansions text, abstained integer, params text);
create table if not exists rank_entry(query_id text, entity_id text, name text, type text, unit_id text,
  final_rank integer, shown integer, final_score real, match real, rerank real, seniority real, recency text,
  recency_year integer, recency_mult real, prelim real, prelim_rank integer, bm25_rank integer, dense_rank integer,
  pages integer, strong_pages integer, evidence text, note text, primary key(query_id, entity_id));
create index if not exists rank_query_text on rank_query(text_norm, at);
"""
_log_lock = threading.Lock()


def _db():
    RANKLOG.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(RANKLOG, timeout=10)
    con.executescript(_SCHEMA)
    return con


def _norm_q(t):
    return re.sub(r"\s+", " ", fold(t)).strip()


def _log(text, k, searched, out, rows, notes):
    at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    qid = hashlib.sha1(f"{text}|{at}".encode()).hexdigest()[:16]
    shown = {r["entity_id"] for r in out.get("results", [])}
    params = {"reranker": out.get("timings", {}).get("reranker"), "rrf_k": RRF_K, "show_bar": SHOW_BAR, "abstain_bar": ABSTAIN_BAR, "unit_cap": UNIT_CAP,
              "expansion_weight": EXPANSION_WEIGHT, "role_w": ROLE_W, "recency_mult": RECENCY_MULT}
    with _log_lock:
        con = _db()
        con.execute("insert into rank_query values (?,?,?,?,?,?,?,?,?)",
                    (qid, text, _norm_q(text), at, k, json.dumps(searched), json.dumps(out.get("expansions", [])),
                     int(bool(out.get("abstained"))), json.dumps(params)))
        R_ = R
        con.executemany("insert or replace into rank_entry values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            (qid, r["eid"], r["e"]["name"], r["e"]["type"], r["e"].get("unit_id"), r["final_rank"],
             int(r["eid"] in shown), r["final"], r["match"], r["rerank"], r["seniority"], r["recency"],
             r["recency_year"], RECENCY_MULT[r["recency"]], r["prelim"], r["prelim_rank"],
             r["bm25_rank"] if r["bm25_rank"] and r["bm25_rank"] < 10**6 else None,
             r["dense_rank"] if r["dense_rank"] and r["dense_rank"] < 10**6 else None,
             r["pages"], r["strong_pages"],
             json.dumps([R_.chunks[i]["chunk_id"] for _, i, _ in r["evidence"]]),
             notes.get(r["eid"], "shown" if r["eid"] in shown else ""))
            for r in rows[:RANKLOG_N]])
        con.commit()
        con.close()
    return qid


# ---------------------------------------------------------------- why not X

_TITLES = re.compile(r"\b(prof|dr|med|phil|nat|rer|pd|dipl|ing|msc|bsc|phd|mme|mr|mrs|ms|frau|herr|em)\b\.?", re.I)


def _norm_name(s):
    s = fold(_TITLES.sub(" ", s or ""))
    return re.sub(r"[^a-z ]+", " ", s).split()


def _find_entity(res, who):
    """Resolve an entity id or a person / unit name to (entity_id, score). Same normalisation as resolve."""
    if who in res.ent:
        return who, 100
    r = res.resolve(who) if hasattr(res, "resolve") else who
    if r in res.ent:
        return r, 100
    from rapidfuzz import fuzz
    target = " ".join(sorted(_norm_name(who)))
    if not target:
        return None, 0
    best, best_s = None, 0
    for eid, e in res.ent.items():
        cand = " ".join(sorted(_norm_name(e["name"])))
        s = fuzz.token_sort_ratio(target, cand)
        if e["type"] == "person":
            s += 1   # prefer people on ties
        if s > best_s:
            best, best_s = eid, s
    return (best, best_s) if best_s >= 85 else (None, best_s)


def why_not(entity_or_name: str, query_text: str):
    """Explain the rank of an entity for a query from the RankLog numbers only. Runs the query first if it
    was never logged. Never invents a reason: every sentence is built from stored scores."""
    res = R.ensure()
    con = _db()
    con.row_factory = sqlite3.Row
    q = con.execute("select * from rank_query where text_norm=? order by at desc limit 1", (_norm_q(query_text),)).fetchone()
    if q is None:
        con.close()
        query(query_text)
        con = _db()
        con.row_factory = sqlite3.Row
        q = con.execute("select * from rank_query where text_norm=? order by at desc limit 1",
                        (_norm_q(query_text),)).fetchone()
    searched = json.loads(q["sources"])
    eid, _ = _find_entity(res, entity_or_name)
    base = {"entity_id": eid, "name": res.ent[eid]["name"] if eid else entity_or_name, "query_id": q["query_id"],
            "searched_sources": searched, "stages": []}
    if eid is None:
        con.close()
        return {**base, "in_index": False, "rank": None,
                "reason": "I have no page about this person in the indexed sources "
                          f"({', '.join(_source_name(s) for s in searched)}). Their profile may be on a site we do "
                          "not crawl; propose their official profile page as a source."}
    row = con.execute("select * from rank_entry where query_id=? and entity_id=?", (q["query_id"], eid)).fetchone()
    shown = con.execute("select * from rank_entry where query_id=? and shown=1 order by final_rank", (q["query_id"],)).fetchall()
    con.close()
    lowest = shown[-1] if shown else None
    rer_label = json.loads(q["params"] or "{}").get("reranker") or "reranker"
    e = res.ent[eid]
    if row is None:
        pages = sorted({p for p, ents in res.page_ents.items() if any(x == eid for x, _ in ents)})
        topics = [res.pages[p].get("title") or res.pages[p].get("url") for p in pages[:3] if p in res.pages]
        return {**base, "in_index": True, "rank": None,
                "stages": [{"stage": "retrieval", "score": None, "rank": None,
                            "note": f"none of the {len(pages)} indexed pages linked to {e['name']} reached the top "
                                    f"{TOP_CHUNKS} chunks for the query or its expansions"}],
                "reason": f"{e['name']} is in the archive, but no page about them matches this need's terms or "
                          f"synonyms. Their pages are about: {'; '.join(topics) or 'no titled pages'}. "
                          "Edit the need's expansions, or accept the answer."}

    def st(stage, score, rank, note):
        return {"stage": stage, "score": None if score is None else round(float(score), 4), "rank": rank, "note": note}

    stages = [
        st("bm25", None, row["bm25_rank"], "best keyword rank of any of its chunks on the original query"),
        st("dense", None, row["dense_rank"], "best BGE-M3 rank of any of its chunks on the original query"),
        st("rrf_aggregate", row["prelim"], row["prelim_rank"], f"chunks fused with RRF (k={RRF_K}), voted up to the "
           f"entity over {row['pages']} page(s)"),
        st("rerank", row["rerank"], None, f"{rer_label} score of its best chunk"
           + ("" if row["rerank"] is not None else ": not reranked, it was outside the top 50 candidates")),
        st("seniority", row["seniority"], None, "function weight (gap 13)"),
        st("recency", row["recency_mult"], None, f"{row['recency']}" + (f", latest year {row['recency_year']}"
                                                                      if row["recency_year"] else "")),
        st("final", row["final_score"], row["final_rank"], row["note"] or ""),
    ]
    if row["shown"]:
        reason = f"{e['name']} is shown at position {sum(1 for s in shown if s['final_rank'] <= row['final_rank'])}."
    else:
        cmp = ""
        if lowest is not None:
            cmp = (f" The lowest shown match, {lowest['name']}, has final {lowest['final_score']:.3f} "
                   f"(rerank {lowest['rerank'] or 0:.2f}, seniority {lowest['seniority']}, {lowest['recency']}, "
                   f"{lowest['strong_pages']} strong page(s)); {e['name']} has final {row['final_score']:.3f} "
                   f"(rerank {row['rerank'] or 0:.2f}, seniority {row['seniority']}, {row['recency']}, "
                   f"{row['strong_pages']} strong page(s)).")
        why = row["note"] or "ranked below the cut"
        if "show bar" in why:
            why = f"its best reranker score {row['match']:.2f} is below the {SHOW_BAR:.2f} bar"
        reason = f"{e['name']} ranks {row['final_rank']} of the logged candidates: {why}.{cmp}"
    return {**base, "in_index": True, "rank": row["final_rank"], "shown": bool(row["shown"]),
            "stages": stages, "reason": reason}


# ---------------------------------------------------------------- CLI

def _install_netlog():
    """Record every host this process resolves or connects to (proof for the routing rule)."""
    import socket
    hosts = []
    orig_gai, orig_conn = socket.getaddrinfo, socket.socket.connect

    def gai(host, *a, **kw):
        hosts.append(("dns", str(host)))
        return orig_gai(host, *a, **kw)

    def conn(self, addr):
        hosts.append(("connect", str(addr[0]) if isinstance(addr, tuple) else str(addr)))
        return orig_conn(self, addr)
    socket.getaddrinfo = gai
    socket.socket.connect = conn
    return hosts


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="+")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--sources", nargs="*")
    ap.add_argument("--no-expand", action="store_true", help="skip the gateway call (no expansions)")
    ap.add_argument("--why-not", metavar="NAME_OR_ID")
    ap.add_argument("--netlog", action="store_true")
    ap.add_argument("--brief", action="store_true", help="top results, one line each")
    a = ap.parse_args(argv)
    hosts = _install_netlog() if a.netlog else None
    text = " ".join(a.text)
    if a.why_not:
        out = why_not(a.why_not, text)
    else:
        out = query(text, k=a.k, sources=a.sources, expansions=[] if a.no_expand else None)
    if a.brief and "results" in out:
        print(f"Q: {text} | expansions: {out['expansions']} | abstained: {out.get('abstained')} | "
              f"{out['timings']}")
        for n, r in enumerate(out["results"], 1):
            q = r["reasons"][0]["quote"][:110] if r["reasons"] else ""
            print(f"{n:2}. {r['score']:.3f} {r['type']:6} {r['name']} | {r['unit']} | {r['function']} | "
                  f"{r['coverage']}/{r['recency']} | \"{q}\"")
        if out.get("abstained"):
            print(out.get("message"), out.get("suggested_sources"))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    if hosts is not None:
        print("NETLOG " + json.dumps(sorted(set(hosts))), file=sys.stderr)


if __name__ == "__main__":
    main()
