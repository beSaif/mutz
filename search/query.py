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
from search.text import best_quote, decompound, deep_link, fold, tokenize

DATA = ix.DATA
KG = DATA / "kg.sqlite"
RANKLOG = DATA / "ranklog.sqlite"

RRF_K = 60
TOP_CHUNKS = 200          # per ranked list
TOP_ENTITIES_RERANK = 50
MAX_RERANK_CHUNKS = int(os.environ.get("MUTZ_MAX_RERANK", "64"))
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
TYPE_W = {"person": 1.0, "unit": 1.0, "infrastructure": 0.6}
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
            self.ent[r["id"]] = {"type": "person", "name": r["name"], "name_norm": r["name_norm"],
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
            proj_unit = {r["id"]: r["unit_id"] for r in con.execute("select id, unit_id from project")}
        for r in con.execute("select page_id, entity_id, role from page_entity"):
            eid = resolve(r["entity_id"])
            role = r["role"]
            if eid in proj_unit:                       # a project page counts for its unit
                eid, role = proj_unit[eid], "unit_page"
            if eid in self.ent:
                self.page_ents[r["page_id"]].append((eid, role))

    def _fix_unit_names(self):
        """Display name guard: some unit names come out as a page title ('Teaching', 'Publications').
        Use the last breadcrumb of the unit's own page instead. Read only, kg.sqlite is not changed."""
        by_url = {p.get("url"): p for p in self.pages.values()}
        for e in self.ent.values():
            if e["type"] != "unit":
                continue
            n = (e["name"] or "").strip()
            if fold(n) in _GENERIC_UNIT or re.search(r"\d{4}|strasse|@", n, re.I) or len(n) > 70 or not n:
                crumbs = (by_url.get(e.get("url")) or {}).get("breadcrumbs") or []
                good = [c for c in crumbs if fold(c) not in _GENERIC_UNIT]
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
    "first the plain English translation if the need is not in English, then technical terms and abbreviations, "
    "the German and French equivalents, and one or two closely related fields. "
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
    No year anywhere: 'active', basis 'listed on the live site', since the page exists in today's crawl."""
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
    if os.environ.get("MUTZ_NO_RERANK") == "1":   # tests without model weights
        return _proxy_rerank(res, qstrings, chunk_ids, rrf)
    try:
        model = res.get_reranker()
    except Exception as e:
        res.degraded.add(f"reranker unavailable, term coverage proxy used: {type(e).__name__}: {e}"[:200])
        return _proxy_rerank(res, qstrings, chunk_ids, rrf)
    pairs = [(qtext, ix.chunk_text_for_index(res.chunks[i], res.pages.get(res.chunks[i]["page_id"]))[:2000])
             for i in chunk_ids]
    logits = ix.rerank_logits(model, pairs)
    probs = 1 / (1 + np.exp(-np.asarray(logits, dtype=np.float64)))
    return {i: float(p) for i, p in zip(chunk_ids, probs)}


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

    # 6. scores
    terms = set(t for q in qstrings for t in tokenize(q))
    rows = []
    for eid in cand:
        e = res.ent[eid]
        chs = sorted(contrib[eid].values(), key=lambda x: -x[0])
        scored = sorted([(rr[i] * (0.7 if role in ("member", "via_unit") else 1.0), i, role)
                         for _, i, role in chs if i in rr], key=lambda x: -x[0])
        if scored:
            match = scored[0][0] + 0.15 * (scored[1][0] if len(scored) > 1 else 0)
        else:
            match = 0.0
        rec, rec_year, rec_basis = _recency(res, eid, contrib)
        sen = float(e.get("seniority") or 0.5) if e["type"] == "person" else 1.0
        tw = 1.0 if need_type == "infrastructure" else TYPE_W.get(e["type"], 1.0)
        final = match * sen * RECENCY_MULT[rec] * tw
        strong_pages = {res.pages.get(res.chunks[i]["page_id"], {}).get("sibling_group") or res.chunks[i]["page_id"]
                        for p, i, _ in scored if p >= STRONG_BAR}
        best_chunk = (scored[0][1] if scored else chs[0][1])
        bi = cinfo.get(best_chunk, {})
        rows.append({
            "eid": eid, "e": e, "match": match, "final": final, "rerank": scored[0][0] if scored else None,
            "seniority": sen, "recency": rec, "recency_year": rec_year, "recency_basis": rec_basis,
            "prelim": prelim[eid], "prelim_rank": prelim_rank[eid],
            "bm25_rank": min((cinfo.get(i, {}).get("bm25_rank", 10**6) for _, i, _ in chs), default=None),
            "dense_rank": min((cinfo.get(i, {}).get("dense_rank", 10**6) for _, i, _ in chs), default=None),
            "pages": len(chs), "strong_pages": len(strong_pages),
            "evidence": [(p, i, role) for p, i, role in scored[:3]] or [(None, i, role) for _, i, role in chs[:2]],
            "best_info": bi})
    rows.sort(key=lambda r: (-r["final"], -r["prelim"]))

    # 7. show bar, padding filter, people next to their unit, cap per unit
    qsets = _query_sets(res, qstrings)
    notes = {}

    def passes(r):
        if r["rerank"] is None:
            notes[r["eid"]] = "not reranked (outside the top 50)"
            return False
        if r["match"] < SHOW_BAR:
            notes[r["eid"]] = "below the show bar"
            return False
        # padding guard: a thin match whose evidence carries few of the need's terms is not shown
        toks = set().union(*[_chunk_toks(res, i) for _, i, _ in r["evidence"]])
        r["term_cov"] = round(_term_cov(qsets, toks), 2)
        if r["rerank"] < STRONG_BAR and r["term_cov"] < 0.6:
            notes[r["eid"]] = f"thin: evidence holds {r['term_cov']:.0%} of the need's terms and the reranker is unsure"
            return False
        return True

    ok = [r for r in rows if passes(r)]
    people_of = defaultdict(list)
    for r in ok:
        if r["e"]["type"] == "person" and r["e"].get("unit_id"):
            people_of[r["e"]["unit_id"]].append(r)
    for lst in people_of.values():
        lst.sort(key=lambda r: (-r["seniority"], -r["final"]))
    ordered, placed = [], set()
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
    shown, per_unit = [], defaultdict(int)
    for r in ordered:
        unit = r["e"].get("unit_id") or r["eid"]
        if len(shown) >= k:
            notes[r["eid"]] = "ranked below the cut"
            continue
        if per_unit[unit] >= UNIT_CAP:
            notes[r["eid"]] = f"cut by the fairness cap: its unit already has {UNIT_CAP} of the top {k}"
            continue
        per_unit[unit] += 1
        shown.append(r)
    best = max((r["match"] for r in rows), default=0.0)
    proxy = os.environ.get("MUTZ_NO_RERANK") == "1" or any("reranker unavailable" in d for d in res.degraded)
    abstain_bar = PROXY_ABSTAIN_BAR if proxy else ABSTAIN_BAR
    for n, r in enumerate(rows):
        r["final_rank"] = n + 1

    results = []
    for r in shown:
        e = r["e"]
        reasons = []
        for p, i, role in r["evidence"]:
            c = res.chunks[i]
            q = best_quote(c["text"], terms)
            reasons.append({"chunk_id": c["chunk_id"], "url": c["url"], "quote": q,
                            "deep_link": deep_link(c["url"], q),
                            "rerank": round(p, 3) if p is not None else None,
                            "via": "unit" if role == "via_unit" else "member_list" if role == "member" else "page"})
        cov = "strong" if r["strong_pages"] >= 2 or (r["strong_pages"] == 1 and r["match"] >= 0.8) else "thin"
        results.append({
            "entity_id": r["eid"], "type": e["type"], "name": e["name"], "unit": e.get("unit"),
            "unit_id": e.get("unit_id"), "function": e.get("function"), "score": round(r["final"], 4),
            "coverage": cov, "recency": r["recency"], "recency_year": r["recency_year"],
            "recency_basis": r["recency_basis"], "seniority": r["seniority"], "url": e.get("url"),
            "source": e.get("source"), "reasons": reasons,
            "contacts": [{"entity_id": pid, "name": res.ent[pid]["name"], "function": res.ent[pid].get("function"),
                          "url": res.ent[pid].get("url")}
                         for pid in res.unit_people.get(r["eid"], [])[:3]] if e["type"] == "unit" else []})
    out["results"] = results
    out["abstained"] = not results or best < abstain_bar
    if out["abstained"]:
        out["results"] = []
        names = ", ".join(_source_name(s) for s in searched)
        out["message"] = (f"No strong Bern match: not found in the indexed sources ({names}). "
                          "This may mean the expertise sits on a site we do not index yet.")
        out["suggested_sources"] = suggest_unindexed(" ".join(qstrings))
        out["near_misses"] = [{"entity_id": r["eid"], "name": r["e"]["name"], "rerank": round(r["match"], 3)}
                              for r in rows[:3] if r["rerank"] is not None]
    out["coverage"] = "none" if out["abstained"] else ("strong" if any(r["coverage"] == "strong" for r in results)
                                                       else "thin")
    out["timings"] = {"retrieve": round(t2 - t1, 2), "rerank": round(t3 - t2, 2), "total": round(time.time() - t0, 2),
                      "reranked_chunks": len(want), "rerank_query": rr_query,
                      "reranker": "term-coverage proxy" if proxy else "bge-reranker-v2-m3", "kg": res.kg,
                      "degraded": sorted(res.degraded) or None,
                      "dense": res.emb is not None and os.environ.get("MUTZ_NO_DENSE") != "1"}
    if log:
        out["query_id"] = _log(text, k, searched, out, rows, notes)
    return out


def _source_name(s):
    if s in SOURCE_NAMES:
        return SOURCE_NAMES[s]
    return s.replace("unibe_", "UniBE ").upper() if s.startswith("unibe_") else s


def warmup():
    """Load the index and both local models (call once at app start; the first query is then fast)."""
    res = R.ensure()
    if res.emb is not None:
        ix.encode(res.get_embedder(), ["warmup"])
    ix.rerank_logits(res.get_reranker(), [("warmup", "warmup")])
    return {"chunks": len(res.chunks), "dense": res.emb is not None, "kg": res.kg, "load_secs": res.load_secs}


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
    params = {"rrf_k": RRF_K, "show_bar": SHOW_BAR, "abstain_bar": ABSTAIN_BAR, "unit_cap": UNIT_CAP,
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
        st("rerank", row["rerank"], None, "local cross encoder probability of its best chunk"
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
