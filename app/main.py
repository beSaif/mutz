"""mutz: Research & Expertise Navigator for the Bern Economic Development Agency (prototype).

Server rendered FastAPI + Jinja, plain JS in app/static. Screens follow research/navigator-designs/flows/final/.
Contracts of the other agents are reached only through app/contracts.py (real first, stub fallback)."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import contracts, db, pitchgen, stubs

log = logging.getLogger("mutz.app")
ROOT = Path(__file__).parent
LOGO = Path("assets/private/Logo_SF_BE_EN.png")
EXPORTS = Path("data/app_exports")
UPLOADS = Path("data/app_uploads")

def _warmup():
    """Load the search index and local models once, so the first real query is not cold. Never blocks /health."""
    if {"query", "all"} & contracts.FORCE:
        return
    try:
        import search.query as sq
        t0 = datetime.now()
        st = sq.warmup()
        log.warning("search warmup done in %ss: %s", (datetime.now() - t0).seconds, st)
    except Exception as e:  # missing module or models: the stub answers instead
        log.warning("search warmup skipped: %r", e)


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=_warmup, name="search-warmup", daemon=True).start()
    yield


app = FastAPI(title="mutz Expertise Navigator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
T = Jinja2Templates(directory=ROOT / "templates")
db.init()
db.x("UPDATE lead SET search_state='idle' WHERE search_state='running'")  # a restart killed those jobs

DEMO_TEXT = ("Founded 2023. We build a skin patch that tracks glucose and lactate in women during endurance sport. "
             "Over the next five years we need clinical validation with women who have type 1 diabetes, "
             "a manufacturing partner for the patch, and a European base close to research.")

SEED_LEADS = [  # scenario leads for the demo, labelled demo; briefs are the playbook's test scenarios
    {"title": "Imaging tools for tumour surgery", "summary": "Intraoperative detection · medtech", "origin": "GGBa",
     "sector": "medtech", "text": "We develop imaging tools that detect tumour margins during surgery. We need a clinical research "
     "partner in surgical imaging and access to an operating room research platform."},
    {"title": "Hearing implant software", "summary": "Medtech · audiology", "origin": "S-GE", "sector": "medtech",
     "text": "We write fitting software for cochlear implants. We are looking for a research group in hearing "
     "research and audiology to validate our algorithms with patients."},
    {"title": "Lithium battery recycling plant", "summary": "Cleantech · industrial site", "origin": "S-GE",
     "sector": "cleantech", "text": "We recycle lithium ion batteries. We need a research partner in battery cell "
     "chemistry and recycling processes, and an industrial site."},
    {"title": "Wearable ECG patch", "summary": "Cardiology · medtech", "origin": "Own contact", "sector": "medtech",
     "text": "We build a wearable ECG patch for arrhythmia detection. We need a cardiology research group for "
     "a validation study and signal processing expertise."},
]

TYPE_LABEL = {"person": "Person", "unit": "Unit", "group": "Unit", "study": "Study", "project": "Study",
              "infrastructure": "Infrastructure", "grant": "Grant", "page": "Page"}
TYPE_FILTER = [("all", "All"), ("person", "People"), ("study", "Studies"), ("unit", "Units"), ("grant", "Grants"),
               ("infrastructure", "Infrastructure")]
NEED_LABEL = {"research": "R&D partner", "infrastructure": "Infrastructure", "supplier": "Supplier",
              "funding": "Funding", "transfer": "Tech transfer", "other": "Other"}
STAGES = ["new", "triaged", "pitched", "visit", "won", "lost"]
LOST_REASONS = ["Chose another canton", "No Bern strength", "Project stopped", "No reply"]


# ---------------------------------------------------------------- helpers

def host(url):
    try:
        h = urlparse(url).netloc
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return url


def ftype(t):
    t = (t or "unit").lower()
    return {"group": "unit", "project": "study", "lab": "unit", "centre": "unit", "institute": "unit"}.get(t, t)


def fmt_date(iso, with_time=False):
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    today = datetime.now(timezone.utc).date()
    if d.date() == today:
        return "today" + (d.astimezone().strftime(" %H:%M") if with_time else "")
    return d.strftime("%-d %b") + (d.astimezone().strftime(" %H:%M") if with_time else "")


T.env.filters["host"] = host
T.env.filters["date"] = fmt_date
T.env.filters["tlabel"] = lambda t: TYPE_LABEL.get(ftype(t), (t or "").capitalize())
T.env.filters["nlabel"] = lambda t: NEED_LABEL.get(t, (t or "").capitalize())
T.env.filters["num"] = lambda n: f"{n:,}" if isinstance(n, int) else n
T.env.globals["now"] = lambda: datetime.now()
T.env.filters["cq"] = stubs.clean_quote
def cite_html(text, reasons):
    """Escape a grounded explanation and turn its [k] (k-th quote of this result) into links numbered for the page."""
    from markupsafe import Markup, escape
    h = str(escape(text or ""))
    def rep(m):
        k = int(m.group(1))
        if 1 <= k <= len(reasons):
            s = reasons[k - 1]
            return f'<a href="{escape(s.get("deep_link") or s.get("url"))}" target="_blank" rel="noopener">[{s.get("n", k)}]</a>'
        return ""
    return Markup(re.sub(r"\[(\d+)\]", rep, h))


T.env.filters["cite"] = cite_html


def _archives(ids):
    from app.mutz import archives_label
    return archives_label(ids)


T.env.filters["archives"] = _archives
SRC_NAMES = {"artorg": "ARTORG Center", "dcb": "Diabetes Center Berne", "unibe": "University of Bern"}
T.env.filters["srcname"] = lambda s: SRC_NAMES.get(s) or (f"unibe {s[6:]}" if str(s).startswith("unibe_") else s)


def archive_stats():
    """Pages per archive from the crawl index (the Archives page, the Home aside, Mutz searching state)."""
    idx = Path("data/raw/index.jsonl")
    per, langs, last = Counter(), defaultdict(set), {}
    skipped = Counter()
    if idx.exists():
        with open(idx) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                g = r.get("group") or r.get("source")
                if r.get("status") == 200:
                    per[g] += 1
                    if r.get("lang"):
                        langs[g].add(r["lang"].upper())
                    last[g] = max(last.get(g, ""), r.get("fetched_at") or "")
                else:
                    skipped[g] += 1
    groups = []
    names = {"artorg": ("ARTORG Center", "artorg.unibe.ch"), "dcb": ("Diabetes Center Berne", "dcberne.com"),
             "unibe": ("University of Bern", "life science subdomains")}
    for g, n in per.most_common():
        name, sub = names.get(g, (g, ""))
        if g == "unibe":
            subs = set()
            with open(idx) as f:
                for line in f:
                    if '"group": "unibe"' in line:
                        try:
                            subs.add(json.loads(line)["source"])
                        except Exception:
                            pass
            sub = f"{len(subs)} life science subdomains"
        groups.append({"id": g, "name": name, "sub": sub, "pages": n, "langs": ", ".join(sorted(langs[g])) or "",
                       "last": last.get(g), "skipped": skipped[g]})
    total = sum(per.values())
    lastall = max(last.values()) if last else None
    return {"groups": groups, "total": total, "n": len(groups), "last": lastall}


def kg_counts():
    p = Path("data/kg.sqlite")
    if not p.exists():
        return {}
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        out = {}
        for row in c.execute("SELECT p.sources, COUNT(*) FROM person p GROUP BY p.sources"):
            for s in json.loads(row[0] or "[]"):
                g = "unibe" if s.startswith("unibe") else s
                out[g] = out.get(g, 0) + row[1]
        c.close()
        return out
    except Exception:
        return {}


def ctx(request, **kw):
    lg = LOGO.exists()
    kw.setdefault("lead", None)
    kw.setdefault("mutz", {})
    kw["logo"] = lg
    kw["stubbed"] = contracts.stubbed()
    kw["request"] = request
    return kw


def render(request, tpl, **kw):
    return T.TemplateResponse(request, tpl, ctx(request, **kw))


def pot_of(results_or_cov):
    return results_or_cov or "none"


def need_coverage(n):
    """Honey pot per need from its stored result."""
    if n.get("coverage"):
        return n["coverage"]
    return "none"


def compute_coverage(kind, result, entries):
    if kind == "curated":
        return "strong" if len(entries) >= 2 else ("thin" if entries else "none")
    if kind == "not_covered" or not result:
        return "none"
    res = result.get("results") or []
    if not res:
        return "none"
    covs = [r.get("coverage") for r in res]
    pages = {re.sub(r"#.*", "", s.get("url", "")) for r in res for s in r.get("reasons", [])}
    if covs.count("strong") >= 2 or (covs.count("strong") >= 1 and len(pages) >= 3):
        return "strong"
    if any(c in ("strong", "thin") for c in covs):
        return "thin"
    return "none"


def need_query_text(n):
    on = [t["t"] for t in n["terms"] if t.get("on")]
    return " ".join([n["text"]] + [t for t in on if t.lower() not in n["text"].lower()])


# ---------------------------------------------------------------- search worker

_pool = ThreadPoolExecutor(max_workers=2)
_running = set()
_runlock = threading.Lock()


def run_search(lid, only=None):
    with _runlock:
        if lid in _running:
            return
        _running.add(lid)
    db.update_lead(lid, search_state="running", search_error=None)
    _pool.submit(_search_job, lid, only)


def _search_job(lid, only):
    try:
        lead = db.lead(lid)
        archives = set()
        rows = []
        for n in db.needs(lid):
            if only and n["id"] not in only and n.get("searched_at"):
                rows.append(n)
                continue
            db.update_need(n["id"], searched_at=None, error=None)
            nd = {"id": n["id"], "text": n["text"], "type": n["type"], "priority": n["priority"],
                  "terms": [t["t"] for t in n["terms"] if t.get("on")]}
            try:
                rt = contracts.route(nd) or {"kind": "search", "entries": []}
            except Exception:
                rt = {"kind": "search", "entries": []}
            result = None
            try:
                if rt.get("kind") == "search":
                    # ticked terms join the text; agent C still adds its own expansions (passing ours as
                                    # expansions= replaces them and made the search abstain)
                    result = contracts.query(need_query_text(n), k=10, need_type=n["type"])
                    archives.update(result.get("searched_sources") or [])
                cov = compute_coverage(rt.get("kind"), result, rt.get("entries") or [])
                db.update_need(n["id"], route=rt, result=result, coverage=cov, searched_at=db.now())
            except Exception as e:  # the gateway or the index failed for this need; others go on
                log.exception("search failed for need %s", n["id"])
                db.update_need(n["id"], error=f"{e.__class__.__name__}", searched_at=db.now(), coverage="none")
            rows.append(db.need(n["id"]))
        # LeadSignal at triage: anonymous, one row per need
        sig = [{"concept": concept_id(n), "type": n["type"], "priority": n["priority"],
                "coverage": n.get("coverage") or "none"} for n in rows if n]
        stage = lead["stage"] if lead["stage"] not in ("new",) else "triaged"
        db.write_signals(lid, (lead.get("profile") or {}).get("sector") or lead.get("sector"), sig, stage,
                         sorted(archives), demo=lead.get("demo") or 0)
        errs = [n for n in rows if n and n.get("error")]
        db.update_lead(lid, search_state="error" if errs and len(errs) == len(rows) else "done", stage=stage,
                       step="matches" if lead["step"] in ("brief",) else lead["step"])
    except Exception as e:
        log.exception("search job failed")
        db.update_lead(lid, search_state="error", search_error=str(e)[:300])
    finally:
        with _runlock:
            _running.discard(lid)


FIELDS = [  # normalized concepts for the lead log (no free text from the one pager)
    ("talent", "Hiring and talent", r"\bhir(e|es|ing)\b|talent|recruitment of|recruit(ing)? (staff|people|engineers)|employees|data scientists"),
    ("diabetes-technology", "Diabetes technology", r"diabet|glucose|glyc|insulin|cgm|blutzucker"),
    ("clinical-trials", "Clinical trial support", r"clinical (trial|study|validation)|klinische|essais|recruit"),
    ("biomedical-engineering", "Biomedical engineering", r"sensor|device|implant|biomed|engineering"),
    ("machine-learning", "Machine learning and data", r"machine learning|artificial intelligence|\bai\b|data|algorithm"),
    ("surgical-imaging", "Surgical imaging", r"imaging|surgery|surgical|intraoperative|tumou?r"),
    ("hearing", "Hearing research", r"hearing|cochlea|audiolog"),
    ("cardiology", "Cardiology", r"cardio|heart|ecg|ekg|arrhythm"),
    ("manufacturing", "Small batch manufacturing", r"manufactur|fertigung|production|supplier"),
    ("battery", "Battery technology", r"batter|lithium|cell chemistry"),
    ("cosmetics-packaging", "Cosmetics and packaging", r"cosmetic|packaging"),
    ("funding", "Funding", r"fund|grant|innosuisse"),
    ("tech-transfer", "Technology transfer", r"licen|transfer|patent"),
    ("site-space", "Site and lab space", r"site|space|base|office|lab space"),
]
FIELD_NAME = {f[0]: f[1] for f in FIELDS}


def concept_id(n):
    t = (n.get("text") or "") + " " + " ".join(x["t"] if isinstance(x, dict) else x for x in n.get("terms") or [])
    for fid, _, rx in FIELDS:
        if re.search(rx, t, re.I):
            return fid
    return "other-" + (n.get("type") or "research")


# ---------------------------------------------------------------- pages: home and leads

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/logo.png")
def logo():
    if not LOGO.exists():
        raise HTTPException(404)
    return FileResponse(LOGO, media_type="image/png")


def lead_progress(l):
    ns = db.needs(l["id"])
    acc = db.accepted(l["id"])
    draft, _ = db.pitch_draft(l["id"])
    info = {"needs": len(ns), "accepted": len(acc), "pot": None, "searched": any(n.get("searched_at") for n in ns)}
    covs = [n.get("coverage") for n in ns if n.get("coverage")]
    if covs:
        info["pot"] = "strong" if "strong" in covs else ("thin" if "thin" in covs else "none")
    if draft:
        cards = [c for c in draft["cards"] if not c.get("deleted")]
        info["checked"] = sum(1 for c in cards if c.get("checked"))
        info["cards"] = len(cards)
    return info


@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = ""):
    rows = db.leads()
    if q:
        rows = [r for r in rows if q.lower() in (r["title"] + " " + (r["summary"] or "") + " " + (r["sector"] or "")).lower()]
    groups = {"new": [], "drafts": [], "closed": []}
    for r in rows:
        r["p"] = lead_progress(r)
        if r["stage"] in ("pitched", "visit", "won", "lost"):
            groups["closed"].append(r)
        elif not r["opened"]:
            groups["new"].append(r)
        else:
            groups["drafts"].append(r)
    arch = archive_stats()
    bubble = None
    for r in groups["drafts"]:
        p = r["p"]
        if p.get("cards") and p["cards"] - p["checked"] > 0:
            bubble = f"Grüessech! The {r['title']} pitch is {p['cards'] - p['checked']} ticks from done."
            break
    return render(request, "home.html", groups=groups, arch=arch, q=q, total=len(rows), bubble=bubble,
                  mutz={"screen": "Leads", "bubble": bubble})


@app.post("/leads/new")
def lead_new(title: str = Form("New lead"), origin: str = Form("S-GE")):
    lid = db.create_lead(title.strip() or "New lead", origin=origin)
    db.update_lead(lid, opened=1)
    return RedirectResponse(f"/leads/{lid}/brief", 303)


DEMO_FIXTURE = Path("needs/fixtures/demo_onepager.txt")


@app.post("/leads/demo")
def lead_demo():
    """The demo case is agent E's fixture (Kestrelyn Health Oy, invented), so demo, eval and pitch line up."""
    text, title = DEMO_TEXT, "Sport wearable lead"
    if DEMO_FIXTURE.exists():
        text, title = DEMO_FIXTURE.read_text().strip(), "Kestrelyn Health Oy"
    lid = db.create_lead(title, input_text=text, origin="S-GE", sector="medtech",
                         summary="Glucose and lactate patch for women in endurance sport", demo=1)
    db.update_lead(lid, opened=1)
    return RedirectResponse(f"/leads/{lid}/brief?extract=1", 303)


@app.post("/leads/seed")
def lead_seed():
    for s in SEED_LEADS:
        db.create_lead(s["title"], input_text=s["text"], origin=s["origin"], sector=s["sector"], summary=s["summary"], demo=1)
    return RedirectResponse("/", 303)


@app.post("/leads/{lid}/delete")
def lead_delete(lid: str):
    db.delete_lead(lid)
    return RedirectResponse("/", 303)


def get_lead(lid):
    l = db.lead(lid)
    if not l:
        raise HTTPException(404, "Lead not found")
    return l


def case_ctx(l, step):
    has_matches = any(n.get("searched_at") for n in db.needs(l["id"]))
    draft, saved = db.pitch_draft(l["id"])
    sent = bool(db.versions(l["id"]))
    return {"step": step, "done": {"brief": bool(db.needs(l["id"])), "matches": has_matches,
                                   "map": bool(db.accepted(l["id"])), "pitch": sent},
            "saved": saved, "sent": sent}


@app.get("/leads/{lid}", response_class=HTMLResponse)
def lead_open(lid: str):
    l = get_lead(lid)
    step = {"brief": "brief", "matches": "matches", "map": "map", "pitch": "pitch", "sent": "after"}.get(l["step"], "brief")
    if l["stage"] in ("pitched", "visit", "won", "lost"):
        step = "after"
    return RedirectResponse(f"/leads/{lid}/{step}", 303)


@app.get("/leads/{lid}/brief", response_class=HTMLResponse)
def brief(request: Request, lid: str, mode: str = "", extract: int = 0, err: str = ""):
    l = get_lead(lid)
    if not l["opened"]:
        db.update_lead(lid, opened=1)
    mode = mode or l["input_kind"] or "text"
    ns = db.needs(lid)
    arch = archive_stats()
    return render(request, "brief.html", lead=l, needs=ns, mode=mode, case=case_ctx(l, "brief"), arch=arch,
                  auto_extract=bool(extract and not ns and l["input_text"]), err=err,
                  mutz={"screen": "Brief", "lead": lid, "pot": None})


@app.post("/leads/{lid}/brief")
async def brief_submit(request: Request, lid: str, text: str = Form(""), website: str = Form(""),
                       lang: str = Form("en"), mode: str = Form("text"), keywords: str = Form(""),
                       keep_file: str = Form(""), pdf: UploadFile | None = File(None)):
    l = get_lead(lid)
    pdf_path = None
    upd = {"input_text": text, "website": website.strip(), "lang": lang, "input_kind": mode,
           "keep_file": 1 if keep_file else 0}
    if pdf is not None and pdf.filename:
        UPLOADS.mkdir(parents=True, exist_ok=True)
        pdf_path = UPLOADS / f"{lid}.pdf"
        pdf_path.write_bytes(await pdf.read())
        upd["pdf_name"] = pdf.filename
        upd["input_kind"] = "pdf"
    elif mode == "pdf" and (UPLOADS / f"{lid}.pdf").exists():
        pdf_path = UPLOADS / f"{lid}.pdf"
    db.update_lead(lid, **upd)
    try:
        out = contracts.needs(one_pager_text=text or None, pdf_path=str(pdf_path) if pdf_path else None,
                              website=website.strip() or None, keywords=keywords or None)
    except Exception as e:
        log.exception("needs failed")
        return RedirectResponse(f"/leads/{lid}/brief?mode={mode}&err=gateway", 303)
    finally:
        if pdf_path and not keep_file and pdf_path.exists() and upd.get("input_kind") == "pdf":
            pass  # deleted after the officer approves the needs (Find matches); kept until then for re-reading
    apply_needs(lid, out)
    if text and not l["input_text"]:
        db.add_entry(lid, "one_pager", text[:2000])
    if website.strip():
        db.add_entry(lid, "website", website.strip(), confidential=0)
    return RedirectResponse(f"/leads/{lid}/brief?mode={mode}", 303)


def need_label(n):
    t = (n.get("text") or "").lower()
    if n.get("type") in ("other", None) or n.get("type") not in NEED_LABEL:
        cid = concept_id({"text": n.get("text", ""), "terms": n.get("terms") or []})
        if cid in FIELD_NAME:
            return FIELD_NAME[cid]
    if n.get("type") in ("research", "infrastructure", None) and re.search(r"clinical|study|trial|studie|essai", t):
        return "Clinical study" if n.get("type") != "infrastructure" else "Clinical trial support"
    return NEED_LABEL.get(n.get("type"), "Need")


def apply_needs(lid, out):
    profile = dict(out.get("profile") or {})
    passages = out.get("passages") or []
    lead = db.lead(lid)
    src_text = ((lead or {}).get("input_text") or "") + " " + " ".join(p.get("text", "") for p in passages)
    if profile.get("name") and profile["name"].lower() not in src_text.lower():
        profile.pop("name")  # never show a company name the inputs do not contain
    items = []
    for n in out.get("needs") or []:
        items.append({"text": n.get("text", ""), "label": n.get("label") or need_label(n),
                      "type": n.get("type") or "research", "priority": n.get("priority"),
                      "from_sentence": n.get("from_sentence", ""), "source": n.get("source", "text"),
                      "terms": n.get("terms") or n.get("expansions") or []})
    db.replace_needs(lid, items)
    upd = {"profile": profile, "passages": passages}
    if profile.get("sector"):
        upd["sector"] = profile["sector"]
    if profile.get("name"):
        upd["company"] = profile["name"]
    db.update_lead(lid, **upd)


@app.post("/leads/{lid}/strike")
async def strike(request: Request, lid: str):
    """Toggle a passage as struck ("do not use this") and re-read the brief without it."""
    body = await request.json()
    l = get_lead(lid)
    passages = l["passages"]
    i = int(body.get("i", -1))
    if 0 <= i < len(passages):
        p = passages[i]
        p["status"] = "used" if p.get("status") == "struck" else "struck"
        if p["status"] == "struck":
            p["reason"] = "Struck by you"
        kept = " ".join(x["text"] for x in passages if x.get("status") != "struck")
        try:
            out = contracts.needs(one_pager_text=kept)
            out_p = {x["text"]: x for x in out.get("passages") or []}
            for x in passages:  # keep the officer's strikes, take the new used/ignored marks for the rest
                if x.get("status") != "struck" and x["text"] in out_p:
                    x.update({k: out_p[x["text"]][k] for k in ("status", "reason") if k in out_p[x["text"]]})
            out["passages"] = passages
            apply_needs(lid, out)
        except Exception:
            db.update_lead(lid, passages=passages)
            return JSONResponse({"ok": False, "error": "The Apertus gateway did not answer. Your strike is saved."})
    return {"ok": True}


@app.post("/api/leads/{lid}")
async def lead_update(lid: str, request: Request):
    b = await request.json()
    get_lead(lid)
    if b.get("title", "").strip():
        db.update_lead(lid, title=b["title"].strip()[:120])
    return {"ok": True}


@app.post("/api/needs/{nid}")
async def need_update(nid: str, request: Request):
    body = await request.json()
    n = db.need(nid)
    if not n:
        raise HTTPException(404)
    upd = {}
    if "text" in body:
        upd["text"] = body["text"].strip()[:300]
    if "term" in body:
        terms = n["terms"]
        for t in terms:
            if t["t"] == body["term"]:
                t["on"] = bool(body.get("on"))
        upd["terms"] = terms
    if "add_term" in body and body["add_term"].strip():
        upd["terms"] = n["terms"] + [{"t": body["add_term"].strip(), "on": True}]
    if "shareable" in body:
        upd["shareable"] = 1 if body["shareable"] else 0
    if "type" in body:
        upd["type"] = body["type"]
        upd["label"] = NEED_LABEL.get(body["type"], body["type"])
    if body.get("delete"):
        upd["status"] = "removed"
    if body.get("restore"):
        upd["status"] = "active"
    if upd:
        db.update_need(nid, **upd)
    return {"ok": True}


@app.post("/leads/{lid}/needs/add")
def need_add(lid: str, text: str = Form(...), type: str = Form("research")):
    get_lead(lid)
    if text.strip():
        db.add_need(lid, {"text": text.strip(), "type": type, "label": NEED_LABEL.get(type, "Need"),
                          "terms": stubs.expand(text), "from_sentence": "Added by you"})
    return RedirectResponse(f"/leads/{lid}/brief", 303)


@app.post("/leads/{lid}/search")
def search(lid: str):
    l = get_lead(lid)
    if not db.needs(lid):
        return RedirectResponse(f"/leads/{lid}/brief", 303)
    p = UPLOADS / f"{lid}.pdf"
    if p.exists() and not l["keep_file"]:
        p.unlink()  # extract, then forget
    db.update_lead(lid, step="matches")
    run_search(lid)
    return RedirectResponse(f"/leads/{lid}/matches", 303)


@app.get("/api/leads/{lid}/status")
def search_status(lid: str):
    l = get_lead(lid)
    ns = db.needs(lid)
    return {"state": l["search_state"], "needs": [{"id": n["id"], "done": bool(n.get("searched_at")),
                                                    "coverage": n.get("coverage")} for n in ns]}


# ---------------------------------------------------------------- matches

GENERIC = set(stubs.tokens("study research project development analysis clinical patients health bern swiss "
                           "university new novel role context based using approach data system systems"))


def relevant_grants(grants, n):
    """At most 2 grants whose title shares a term with the need or its terms; the rest are only counted."""
    words = " ".join([n.get("text", "")] + [t["t"] for t in n.get("terms", []) if t.get("on")] +
                     list(((n.get("result") or {}).get("expansions") or [])))
    need_t = set(stubs.tokens(words)) - GENERIC
    keep, other = [], 0
    for g in grants:
        if len(keep) < 2 and need_t & (set(stubs.tokens(g.get("title") or "")) - GENERIC):
            keep.append(g)
        else:
            other += 1
    return keep, other


def decorate(n, lid):
    """Results of one need with decisions, numbering and grants."""
    dec = {(d["need_id"], d["entity_id"]): d for d in db.decisions(lid)}
    res = (n.get("result") or {}).get("results") or []
    out = []
    for r in res:
        r = dict(r)
        seen_q, reasons = set(), []
        for x in r.get("reasons") or []:
            k = stubs.clean_quote(x.get("quote", "")).lower()
            if k not in seen_q:
                seen_q.add(k)
                reasons.append(x)
        r["reasons"] = reasons
        expl = r.get("explanation") or r.get("reason") or ""
        cited = {int(k) for k in re.findall(r"\[(\d+)\]", expl) if 1 <= int(k) <= len(reasons)}
        r["cited"] = sorted(cited) if cited else list(range(1, min(2, len(reasons)) + 1))
        r["ftype"] = ftype(r.get("type"))
        d = dec.get((n["id"], r["entity_id"]))
        r["decision"] = d["decision"] if d else None
        r["added_by"] = d["added_by"] if d else None
        g = contracts.grants_for(r["entity_id"]) if r.get("entity_id") else []
        r["grants"], r["grants_other"] = relevant_grants(g, n)
        out.append(r)
    # matches the officer added anyway (why not) that are not in the list
    for (nid, eid), d in dec.items():
        if nid == n["id"] and d["decision"] == "accept" and d["match"] and all(x["entity_id"] != eid for x in out):
            m = dict(d["match"])
            m.update(ftype=ftype(m.get("type")), decision="accept", added_by=d["added_by"], grants=[], grants_other=0)
            out.append(m)
    for r in out:
        r["is_adjacent"] = bool(r.get("adjacent") or "adjacent" in (r.get("tags") or []))
    out.sort(key=lambda r: r["is_adjacent"])  # direct matches first, adjacent after (stable keeps the rank)
    return out


@app.get("/leads/{lid}/matches", response_class=HTMLResponse)
def matches(request: Request, lid: str, need: str = "", type: str = "all", why: str = ""):
    l = get_lead(lid)
    ns = db.needs(lid)
    if not ns:
        return RedirectResponse(f"/leads/{lid}/brief", 303)
    if (l["search_state"] == "idle" and not all(n.get("searched_at") for n in ns)) or \
            (l["search_state"] == "running" and lid not in _running):
        run_search(lid)
        l = db.lead(lid)
    cur = next((n for n in ns if n["id"] == need), None) or next((n for n in ns if n.get("searched_at") and n.get("coverage") != "none"), None) or ns[0]
    results = decorate(cur, lid) if cur.get("searched_at") else []
    counts = Counter(r["ftype"] for r in results)
    shown = [r for r in results if type == "all" or r["ftype"] == type]
    # number sources across the visible list
    n_src = 0
    for r in shown:
        for s in r.get("reasons", []):
            n_src += 1
            s["n"] = n_src
    bid = sum(1 for n in ns if n.get("coverage") in ("strong", "thin"))
    n_acc = len(db.accepted(lid))
    curated = []
    for n in ns:
        for e in ((n.get("route") or {}).get("entries") or []):
            if e not in curated:
                curated.append(e)
    if not curated:
        curated = [e for e in contracts.curated() if set(e.get("need_types", [])) & {n["type"] for n in ns}][:1]
    searching = l["search_state"] == "running" or any(not n.get("searched_at") for n in ns)
    whynot = None
    if why.strip() and cur.get("searched_at"):
        try:
            whynot = contracts.why_not(why.strip(), need_query_text(cur))
            if whynot and whynot.get("entity_id") and any(r["entity_id"] == whynot["entity_id"] for r in shown):
                whynot["shown"] = True
        except Exception:
            whynot = {"name": why, "in_index": None, "reason": "The ranking log did not answer. Try again in a minute.", "stages": []}
    arch = archive_stats()
    pending = [n for n in ns if not n.get("searched_at")]
    return render(request, "matches.html", lead=l, needs=ns, cur=cur, results=shown, counts=counts, total=len(results),
                  ftype=type, filters=TYPE_FILTER, bid=bid,
                  any_grants=any(r.get("grants") or r.get("grants_other") for r in shown), n_acc=n_acc, curated=curated[:2], searching=searching,
                  pending=pending, arch=arch, case=case_ctx(l, "matches"), whynot=whynot, why=why,
                  mutz={"screen": "Matches", "lead": lid, "need": cur["id"], "need_text": cur["text"],
                        "pot": cur.get("coverage") if cur.get("searched_at") else None,
                        "bubble": ("Search finished." if not searching and request.query_params.get("fresh") else None)})


@app.post("/api/decide")
async def decide(request: Request):
    b = await request.json()
    lid, nid, eid = b["lead"], b["need"], b["entity"]
    n = db.need(nid)
    match = None
    if n and n.get("result"):
        match = next((r for r in n["result"].get("results", []) if r["entity_id"] == eid), None)
    if b.get("match"):
        match = b["match"]
    db.decide(lid, nid, eid, b.get("decision"), match=match, added_by=b.get("added_by", "officer"))
    l = db.lead(lid)
    if l["step"] in ("brief", "matches") and b.get("decision") == "accept":
        db.update_lead(lid, step="matches")
    return {"ok": True, "accepted": len(db.accepted(lid))}


@app.post("/api/missed")
async def missed(request: Request):
    b = await request.json()
    db.x("INSERT INTO missed_expert(query,name,at) VALUES(?,?,?)", (b.get("query", ""), b.get("name", ""), db.now()))
    p = Path("data/eval/missed_experts.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps({"query": b.get("query", ""), "expected": b.get("entity_id") or b.get("name"), "at": db.now(),
                            "origin": "officer flagged a missed expert"}) + "\n")
    return {"ok": True}


# ---------------------------------------------------------------- evidence map

def build_map(lid, only_accepted=True):
    ns = db.needs(lid)
    acc = db.decisions(lid)
    accd = {(d["need_id"], d["entity_id"]) for d in acc if d["decision"] == "accept"}
    cols = []  # each column = one fact/source leaf
    needs_out, matches_out, facts, edges = [], [], [], []
    entity_needs = Counter()
    for n in ns:
        for r in decorate(n, lid):
            if (n["id"], r["entity_id"]) in accd:
                entity_needs[r["entity_id"]] += 1
    W, G = 170, 20
    x = 80
    for ni, n in enumerate(ns):
        ms = [r for r in decorate(n, lid) if (n["id"], r["entity_id"]) in accd or (not only_accepted and r.get("decision") != "reject")]
        if not ms:
            continue
        nx0 = x
        for r in ms[:4]:
            mx0 = x
            leaves = []
            for s in (r.get("reasons") or [])[:2]:
                leaves.append({"kind": "Fact · Read", "label": s.get("quote", ""), "url": s.get("url"), "quote": s.get("quote"),
                               "deep_link": s.get("deep_link") or s.get("url"), "style": "read",
                               "src": host(s.get("url", "")) + (" · " + (r.get("unit") or "")[:20] if r.get("unit") else "")})
            if r.get("function"):
                fsrc = (r.get("reasons") or [{}])[0]
                leaves.insert(0, {"kind": "Function · Read", "label": r["function"], "url": fsrc.get("url"),
                                  "quote": fsrc.get("quote"), "deep_link": fsrc.get("deep_link") or fsrc.get("url"),
                                  "style": "derived", "src": host(fsrc.get("url", ""))})
            elif r["ftype"] == "person":
                leaves.append({"kind": "Function · Missing", "label": "Not stated on any source", "style": "missing"})
            for g in (r.get("grants") or [])[:1]:
                leaves.append({"kind": "Grant · Read", "label": g.get("title", ""), "url": g.get("url"), "quote": g.get("title"),
                               "deep_link": g.get("url"), "style": "read", "src": host(g.get("url", "")) or "SNSF"})
            leaves = leaves[:3] or [{"kind": "Fact · Missing", "label": "No quote stored", "style": "missing"}]
            for lf in leaves:
                lf.update(x=x, need=n["id"], entity=r["entity_id"], mname=r.get("name"))
                facts.append(lf)
                x += W + G
            mx1 = x - G
            mw = max(W, min(220, mx1 - mx0))
            matches_out.append({"x": (mx0 + mx1) / 2 - mw / 2, "w": mw, "cx": (mx0 + mx1) / 2, "need": n["id"],
                                "entity": r["entity_id"], "name": r.get("name"), "type": TYPE_LABEL.get(r["ftype"], "Match"),
                                "badge": entity_needs[r["entity_id"]] if entity_needs[r["entity_id"]] > 1 else 0,
                                "leaves": [lf["x"] + W / 2 for lf in facts if lf["entity"] == r["entity_id"] and lf["need"] == n["id"]]})
        nx1 = x - G
        nw = max(W, min(220, nx1 - nx0))
        needs_out.append({"x": (nx0 + nx1) / 2 - nw / 2, "w": nw, "cx": (nx0 + nx1) / 2, "id": n["id"], "i": ni + 1,
                          "label": n["label"], "text": n["text"], "cov": n.get("coverage") or "none",
                          "kids": [m["cx"] for m in matches_out if m["need"] == n["id"]]})
        x += 20
    return {"needs": needs_out, "matches": matches_out, "facts": facts, "width": max(x, 900), "W": W}


@app.get("/leads/{lid}/map", response_class=HTMLResponse)
def evmap(request: Request, lid: str, all: int = 0):
    l = get_lead(lid)
    m = build_map(lid, only_accepted=not all)
    draft, _ = db.pitch_draft(lid)
    in_pitch = {c.get("entity_id") for c in (draft or {}).get("cards", []) if not c.get("deleted")}
    if l["step"] in ("brief", "matches"):
        db.update_lead(lid, step="map")
    return render(request, "map.html", lead=l, m=m, all=all, in_pitch=list(in_pitch), case=case_ctx(l, "map"),
                  mutz={"screen": "Evidence map", "lead": lid})


# ---------------------------------------------------------------- pitch builder

def ensure_draft(lid):
    draft, _ = db.pitch_draft(lid)
    l = db.lead(lid)
    acc = db.accepted(lid)
    if draft is None:
        draft = pitchgen.new_draft(l, db.needs(lid), acc)
        db.save_pitch(lid, draft)
    else:
        # accepted after the draft was made: append as new match cards, never touch existing ones
        have = {c.get("entity_id") for c in draft["cards"]}
        add = [d for d in acc if d["entity_id"] not in have and d.get("match")]
        if add:
            draft = pitchgen.add_cards(draft, l, db.needs(lid), add)
            db.save_pitch(lid, draft)
    return draft


@app.get("/leads/{lid}/pitch", response_class=HTMLResponse)
def pitch(request: Request, lid: str, section: str = "why"):
    l = get_lead(lid)
    if not db.accepted(lid) and db.pitch_draft(lid)[0] is None:
        return render(request, "pitch_empty.html", lead=l, case=case_ctx(l, "pitch"), mutz={"screen": "Pitch", "lead": lid})
    draft = ensure_draft(lid)
    if l["step"] in ("brief", "matches", "map"):
        db.update_lead(lid, step="pitch")
    return render(request, "pitch.html", lead=l, draft=draft, section=section, case=case_ctx(l, "pitch"),
                  mutz={"screen": "Pitch", "lead": lid, "corner": "left", "bubble": "Ask Mutz about this pitch"})


@app.post("/api/pitch/{lid}")
async def pitch_save(lid: str, request: Request):
    body = await request.json()
    get_lead(lid)
    db.save_pitch(lid, body["draft"])
    return {"ok": True, "saved": fmt_date(db.now(), True)}


@app.post("/api/pitch/{lid}/rewrite")
async def pitch_rewrite(lid: str, request: Request):
    b = await request.json()
    draft, _ = db.pitch_draft(lid)
    card = next((c for c in draft["cards"] if c["card_id"] == b["card_id"]), None)
    if not card:
        raise HTTPException(404)
    if card.get("locked"):
        return {"ok": False, "error": "This card is locked. Choose Allow edits to regenerate it."}
    try:
        text = pitchgen.rewrite(card, b.get("instruction", "shorter"), lang=b.get("lang"))
    except Exception:
        return {"ok": False, "error": "The Apertus gateway did not answer. The card is unchanged. Try again in a minute."}
    return {"ok": True, "text": text}


@app.post("/api/pitch/{lid}/reset")
def pitch_reset(lid: str):
    db.x("DELETE FROM pitch WHERE lead_id=?", (lid,))
    return {"ok": True}


# ---------------------------------------------------------------- review and export

@app.get("/leads/{lid}/review", response_class=HTMLResponse)
def review(request: Request, lid: str, done: int = 0, err: str = ""):
    l = get_lead(lid)
    draft = ensure_draft(lid) if (db.accepted(lid) or db.pitch_draft(lid)[0]) else None
    if not draft:
        return RedirectResponse(f"/leads/{lid}/pitch", 303)
    p = pitchgen.to_contract(draft, l)
    try:
        problems = contracts.validate(p)
    except Exception as e:
        problems = [f"validate failed: {e}"]
    cards = pitchgen.visible_cards(draft)
    stats = {"verified": sum(1 for c in cards if not c.get("edited") and c.get("sources") and all(s.get("quote") for s in c["sources"])),
             "edited": sum(1 for c in cards if c.get("edited") and c.get("sources")),
             "nosource": sum(1 for c in cards if not c.get("sources")),
             "unchecked": sum(1 for c in cards if not c.get("checked"))}
    srcs, seen = [], {}
    for c in cards:
        for s in c.get("sources", []):
            k = (host(s.get("url", "")), stubs.clean_quote(s.get("quote") or "").lower() or s.get("url"))
            if k in seen:
                continue
            seen[k] = len(srcs) + 1
            srcs.append({**s, "card": c, "n": len(srcs) + 1})
    vs = db.versions(lid)
    return render(request, "review.html", lead=l, draft=draft, stats=stats, problems=problems, srcs=srcs, versions=vs,
                  done=done, err=err, case=case_ctx(l, "pitch"),
                  mutz={"screen": "Review", "lead": lid, "corner": "left",
                        "bubble": "Every card has a source. Good to send." if not stats["nosource"] and not problems else None})


@app.post("/leads/{lid}/export")
def export(lid: str, clean: str = Form(""), review_pdf: str = Form(""), pjson: str = Form(""), override: str = Form("")):
    l = get_lead(lid)
    draft = ensure_draft(lid)
    cards = pitchgen.visible_cards(draft)
    if any(not c.get("checked") for c in cards) and not override:
        return RedirectResponse(f"/leads/{lid}/review?err=unchecked", 303)
    p = pitchgen.to_contract(draft, l)
    if override:
        p["override_review_gate"] = True
    n = (db.one("SELECT COALESCE(MAX(n),0)+1 AS n FROM pitch_version WHERE lead_id=?", (lid,)) or {"n": 1})["n"]
    out = EXPORTS / lid / f"v{n}"
    out.mkdir(parents=True, exist_ok=True)
    try:
        if clean or not (review_pdf or pjson):
            contracts.build_pdf(p, "clean", str(out / "clean.pdf"))
        if review_pdf:
            contracts.build_pdf(p, "review", str(out / "review.pdf"))
    except Exception as e:
        probs = getattr(e, "problems", None) or [str(e)]
        log.warning("export refused: %s", probs)
        return RedirectResponse(f"/leads/{lid}/review?err=" + "; ".join(probs)[:400].replace("&", "and"), 303)
    (out / "pitch.json").write_text(json.dumps({**p, "draft": draft}, ensure_ascii=False, indent=1))
    built_from = [e["id"] for e in db.entries(lid)]
    db.add_version(lid, {**p, "draft": draft}, built_from)
    db.add_entry(lid, "pitch", f"Pitch v{n} sent", confidential=0)
    db.update_lead(lid, stage="pitched", step="sent")
    db.update_signal_stage(lid, "pitched")
    return RedirectResponse(f"/leads/{lid}/review?done={n}", 303)


@app.get("/leads/{lid}/export/v{n}/{name}")
def export_file(lid: str, n: int, name: str):
    if name not in ("clean.pdf", "review.pdf", "pitch.json"):
        raise HTTPException(404)
    p = EXPORTS / lid / f"v{n}" / name
    if not p.exists():
        raise HTTPException(404, "Not exported in this version")
    l = db.lead(lid)
    fname = re.sub(r"[^A-Za-z0-9]+", "-", (l or {}).get("title", "pitch")).strip("-") + f"-v{n}-{name}"
    return FileResponse(p, filename=fname)


# ---------------------------------------------------------------- after sending: lead, follow up

@app.get("/leads/{lid}/after", response_class=HTMLResponse)
def after(request: Request, lid: str):
    l = get_lead(lid)
    from app import lifecycle
    return render(request, "lead.html", lead=l, entries=db.entries(lid), versions=db.versions(lid),
                  diff=lifecycle.pending_diff(lid), case=case_ctx(l, "after"), stages=STAGES, lost=LOST_REASONS,
                  mutz={"screen": "After sending", "lead": lid})


@app.post("/leads/{lid}/entry")
def add_info(lid: str, kind: str = Form("note"), text: str = Form(...), confidential: str = Form(""),
             shareable: str = Form("")):
    get_lead(lid)
    if text.strip():
        db.add_entry(lid, kind, text.strip(), confidential=1 if confidential else 0, shareable=1 if shareable else 0)
    return RedirectResponse(f"/leads/{lid}/after", 303)


@app.post("/leads/{lid}/reevaluate")
def reevaluate(lid: str):
    from app import lifecycle
    try:
        lifecycle.reevaluate(lid)
    except Exception:
        log.exception("reevaluate")
        return RedirectResponse(f"/leads/{lid}/after?err=gateway", 303)
    return RedirectResponse(f"/leads/{lid}/after", 303)


@app.post("/leads/{lid}/diff/{action}")
def diff_action(lid: str, action: str):
    from app import lifecycle
    lifecycle.apply(lid, action)
    return RedirectResponse(f"/leads/{lid}/pitch" if action == "build" else f"/leads/{lid}/after", 303)


@app.post("/leads/{lid}/stage")
def set_stage(lid: str, stage: str = Form(...), lost_reason: str = Form("")):
    get_lead(lid)
    if stage not in STAGES:
        raise HTTPException(400)
    if stage == "lost" and not lost_reason:
        return RedirectResponse(f"/leads/{lid}/after?err=reason", 303)
    db.update_lead(lid, stage=stage, lost_reason=lost_reason or None)
    db.update_signal_stage(lid, stage, lost_reason or None)
    db.add_entry(lid, "note", f"Status: {stage}" + (f", {lost_reason}" if lost_reason else ""), confidential=0)
    return RedirectResponse(f"/leads/{lid}/follow" if stage == "visit" else f"/leads/{lid}/after", 303)


@app.get("/leads/{lid}/follow", response_class=HTMLResponse)
def follow(request: Request, lid: str, sel: int = 0):
    l = get_lead(lid)
    from app import lifecycle
    rows = lifecycle.ensure_outreach(lid)
    cur = next((r for r in rows if r["id"] == sel), rows[0] if rows else None)
    return render(request, "follow.html", lead=l, rows=rows, cur=cur, case=case_ctx(l, "after"), visit=lifecycle.visit_rows(lid),
                  mutz={"screen": "Follow up", "lead": lid})


@app.post("/api/outreach/{oid}")
async def outreach_update(oid: int, request: Request):
    b = await request.json()
    from app import lifecycle
    return lifecycle.outreach_update(oid, b)


@app.get("/leads/{lid}/visit.ics")
def visit_ics(lid: str):
    from app import lifecycle
    return Response(lifecycle.ics(lid), media_type="text/calendar",
                    headers={"Content-Disposition": "attachment; filename=visit-day.ics"})


# ---------------------------------------------------------------- pipeline and insights

@app.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request):
    from app import lifecycle
    return render(request, "pipeline.html", **lifecycle.pipeline(), stages=STAGES,
                  mutz={"screen": "Pipeline"})


@app.get("/insights", response_class=HTMLResponse)
def gaps(request: Request, year: int = 0):
    from app import lifecycle
    return render(request, "gaps.html", **lifecycle.gaps(year or datetime.now().year), field_name=FIELD_NAME,
                  mutz={"screen": "Gap report"})


@app.get("/insights/gaps.csv")
def gaps_csv(year: int = 0):
    from app import lifecycle
    return Response(lifecycle.gaps_csv(year or datetime.now().year), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=gap-report.csv"})


@app.post("/insights/seed")
def seed_signals():
    from app import lifecycle
    lifecycle.seed_signals()
    return RedirectResponse("/insights", 303)


@app.get("/insights/capability", response_class=HTMLResponse)
def capability(request: Request, recompute: int = 0):
    from app import lifecycle
    return render(request, "capability.html", **lifecycle.capability(recompute=bool(recompute)),
                  mutz={"screen": "Capability map"})


# ---------------------------------------------------------------- archives, evaluation, states

@app.get("/archives", response_class=HTMLResponse)
def archives(request: Request, url: str = ""):
    arch = archive_stats()
    people = kg_counts()
    proposal = None
    if url:
        proposal = propose_source(url)
    enrich = Path("data/enrich/grants.jsonl")
    snsf = None
    if enrich.exists():
        with open(enrich) as f:
            snsf = sum(1 for _ in f)
    return render(request, "archives.html", arch=arch, people=people, proposal=proposal, url=url, snsf=snsf,
                  corpus=stubs.corpus_stats() if stubs._corpus else None, mutz={"screen": "Archives"})


REFUSED = re.compile(r"(linkedin|facebook|twitter|x\.com|instagram|tiktok|youtube|xing|login|signin|boris\.unibe)", re.I)


def propose_source(url):
    """Builds the config entry for a new archive. No request is made here: crawl.py checks robots.txt
    and fetches politely when the orchestrator runs it."""
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    h = urlparse(url).netloc
    if not h or "." not in h:
        return {"ok": False, "url": url, "reason": "That is not a website address."}
    if REFUSED.search(url):
        return {"ok": False, "url": url, "reason": "LinkedIn, social media, login pages and BORIS are refused."}
    sid = re.sub(r"[^a-z0-9]+", "_", h.replace("www.", "").split(".")[0].lower())
    entry = {"id": sid, "seeds": [url], "allow": [h], "include": [], "exclude": ["/news/", "/search", "/print"], "max_pages": 400}
    body = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in entry.items())
    return {"ok": True, "url": url, "host": h, "id": sid, "entry": "{\n" + body + "\n}"}


@app.get("/eval", response_class=HTMLResponse)
def evaluation(request: Request):
    """Reads agent D's results: data/eval/latest.json plus the per split files when present."""
    def load(name):
        p = Path("data/eval") / name
        try:
            return json.loads(p.read_text()) if p.exists() else None
        except Exception:
            return None
    ev = load("latest.json")
    splits = {k: load(f"latest_{k}.json") for k in ("heldout", "synthetic", "dev")}
    tests = load("pipeline_tests.json") or []
    weakest = None
    rbc = ((ev or {}).get("metrics") or {}).get("recall_by_category") or {}
    if rbc:
        weakest = min(rbc.items(), key=lambda kv: kv[1])
    missed = 0
    mp = Path("data/eval/missed_experts.jsonl")
    if mp.exists():
        missed = sum(1 for _ in open(mp))
    return render(request, "eval.html", ev=ev, splits=splits, tests=tests, weakest=weakest, missed=missed,
                  arch=archive_stats(), usage=gateway_usage(), mutz={"screen": "Evaluation"})


def gateway_usage():
    p = Path("logs/gateway.jsonl")
    calls, tin, tout = 0, 0, 0
    if p.exists():
        with open(p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("cached"):
                    continue
                calls += 1
                tin += r.get("tokens_in") or 0
                tout += r.get("tokens_out") or 0
    return {"calls": calls, "tin": tin, "tout": tout}


@app.get("/states", response_class=HTMLResponse)
def states(request: Request):
    return render(request, "states.html", arch=archive_stats(), mutz={"screen": "States"})


# ---------------------------------------------------------------- Mutz

@app.post("/api/mutz")
async def mutz_ask(request: Request):
    from app import mutz
    b = await request.json()
    return mutz.ask(b)


@app.get("/api/mutz/thread")
def mutz_thread(key: str = "global"):
    return {"messages": db.thread(key)}


@app.post("/api/mutz/confirm")
async def mutz_confirm(request: Request):
    from app import mutz
    b = await request.json()
    return mutz.confirm(b)


@app.exception_handler(500)
async def err500(request: Request, exc):
    return HTMLResponse(T.get_template("error.html").render(ctx(request, msg=str(exc)[:200])), status_code=500)
