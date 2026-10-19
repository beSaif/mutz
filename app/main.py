"""mutz: Research & Expertise Navigator for the Bern Economic Development Agency (prototype).

Server rendered FastAPI + Jinja, plain JS in app/static. Screens follow research/navigator-designs/flows/final/.
Contracts of the other agents are reached only through app/contracts.py (real first, stub fallback)."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import contracts, db, i18n, pitchgen, stubs
from pitch import locales as pitch_locales
from app.i18n import _
from search.query import KG, RECENCY_MULT, SMALL_UNIT_BOOST, TYPE_W

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


app = FastAPI(title="Bärner Mutz", lifespan=lifespan)
BASIC_AUTH = os.environ.get("MUTZ_BASIC_AUTH", "")  # "user:password" puts the whole app behind a login, for a shared link


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not BASIC_AUTH or request.url.path == "/health":
        return await call_next(request)
    import base64, secrets
    scheme, _sp, cred = request.headers.get("authorization", "").partition(" ")
    try:
        given = base64.b64decode(cred).decode() if scheme.lower() == "basic" else ""
    except Exception:
        given = ""
    if secrets.compare_digest(given.encode(), BASIC_AUTH.encode()):
        return await call_next(request)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="mutz"'})


@app.middleware("http")
async def language(request: Request, call_next):
    i18n.set_lang(i18n.pick(request))
    return await call_next(request)


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
# Matches screen: how the list can be ordered and narrowed. Every option says what it does (no silent reranking).
SORTS = [("score", "Best match", "Highest score first, as the search ranked it. A unit's best people follow it, at most 3 per unit."),
         ("evidence", "Evidence", "How closely the quotes match the need, before the role, recency and type weights."),
         ("recency", "Most recent", "Latest year found on its public pages, newest first."),
         ("activity", "Active first", "Active, then quiet, then past. Score decides within each group."),
         ("name", "Name", "A to Z.")]
REC_ORDER = {"active": 0, "quiet": 1, "past": 2}
REC_LABEL = {"active": "Active", "quiet": "Quiet", "past": "Past"}
SCORE_BANDS = [("0.75", "Top"), ("0.5", "Good or better"), ("0.25", "Fair or better")]
GRADE_LABEL = {3: "Direct", 2: "Partial", 1: "Adjacent"}
CHECK_LABEL = {"abstain": "Says no match when Bern has nothing, instead of guessing",
               "entity_resolution": "The same person on two sites counts once",
               "recency_active": "Recent work is marked active"}


def fit(score):
    """The score as a word for the Matches list; the number stays in the tooltip and under How the score is built."""
    s = score or 0
    return _("Top") if s >= 0.75 else _("Good") if s >= 0.5 else _("Fair") if s >= 0.25 else _("Weak")
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
        return _("today") + (d.astimezone().strftime(" %H:%M") if with_time else "")
    return i18n.month(d) + (d.astimezone().strftime(" %H:%M") if with_time else "")


T.env.filters["host"] = host
T.env.filters["date"] = fmt_date
T.env.filters["tlabel"] = lambda t: _(TYPE_LABEL.get(ftype(t), (t or "").capitalize()))
T.env.filters["nlabel"] = lambda t: _(NEED_LABEL.get(t, (t or "").capitalize()))
T.env.filters["num"] = lambda n: f"{n:,}" if isinstance(n, int) else n
T.env.globals["now"] = lambda: datetime.now()
T.env.globals["_"] = _
T.env.globals["lang"] = i18n.lang
T.env.globals["LANGS"] = i18n.LANGS
T.env.globals["i18n_js"] = i18n.js
# static URLs carry the file's mtime, so a changed app.js or app.css is never served from the browser cache
T.env.globals["asset"] = lambda f: f"/static/{f}?v={int((ROOT / 'static' / f).stat().st_mtime)}"
T.env.filters["cq"] = stubs.clean_quote


def passage_reason(reason, status):
    """The reason after a passage's status on the Brief. Stored in English by needs/; translated for display,
    including the built ones ("need 1, 2 and company profile"). "struck by you" reads as "Struck by you"."""
    if not reason:
        return ""
    if reason.lower().startswith(status):
        return " " + _(reason[len(status):].strip())
    whole = _(reason)
    if whole == reason and i18n.lang() != "en":
        whole = _(" and ").join(_("need {n}", n=b[5:]) if b.startswith("need ") else _(b) for b in reason.split(" and "))
    return " · " + whole


T.env.filters["preason"] = passage_reason
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
T.env.filters["fit"] = fit
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
             "unibe": ("University of Bern", _("life science subdomains"))}
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
            sub = _("{n} life science subdomains", n=len(subs))
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


def me(request):
    """The officer working in this browser (cookie), the first one if none was picked."""
    return db.officer(request.cookies.get("officer", "")) or db.officers()[0]


def ctx(request, **kw):
    lg = LOGO.exists()
    kw.setdefault("lead", None)
    kw.setdefault("mutz", {})
    kw["logo"] = lg
    kw["stubbed"] = contracts.stubbed()
    kw["request"] = request
    kw["navc"] = nav_counts()
    kw["me"] = me(request)
    kw["officers"] = db.officers()
    kw["names"] = {o["id"]: o["name"] for o in kw["officers"]}
    if isinstance(kw.get("case"), dict):
        kw["case"]["me"] = kw["me"]["id"]
    return kw


CLOSED = ("pitched", "visit", "won", "lost")


def lead_group(r):
    """Which sidebar folder a lead sits in: drafts (opened or not), or sent and closed."""
    return "closed" if r["stage"] in CLOSED else "drafts"


def nav_counts():
    rows = db.leads()
    out = {"all": len(rows), "drafts": 0, "closed": 0, "archives": archive_stats().get("n")}
    for r in rows:
        out[lead_group(r)] += 1
    return out


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
    for fid, _f, rx in FIELDS:
        if re.search(rx, t, re.I):
            return fid
    return "other-" + (n.get("type") or "research")


# ---------------------------------------------------------------- pages: home and leads

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/lang/{code}")
def set_language(code: str, request: Request):
    """The EN / DE / IT switch in the sidebar: remember the choice for a year and go back to the same screen."""
    back = request.headers.get("referer") or "/"
    if urlparse(back).netloc not in ("", request.url.netloc):
        back = "/"
    r = RedirectResponse(back, status_code=303)
    if code in i18n.CODES:
        r.set_cookie(i18n.COOKIE, code, max_age=365 * 86400, samesite="lax")
    return r


@app.get("/logo.png")
def logo():
    if not LOGO.exists():
        raise HTTPException(404)
    return FileResponse(LOGO, media_type="image/png")


def lead_progress(l):
    ns = db.needs(l["id"])
    acc = db.accepted(l["id"])
    draft, _v = db.pitch_draft(l["id"])
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
def home(request: Request, q: str = "", f: str = "", who: str = ""):
    rows = db.leads()
    mine = me(request)["id"]
    n_mine = sum(1 for r in rows if r.get("owner") == mine)
    if who == "mine":
        rows = [r for r in rows if r.get("owner") == mine]
    if q:
        rows = [r for r in rows if q.lower() in (r["title"] + " " + (r["summary"] or "") + " " + (r["sector"] or "")).lower()]
    groups = {"drafts": [], "closed": []}
    for r in rows:
        r["p"] = lead_progress(r)
        groups[lead_group(r)].append(r)
    arch = archive_stats()
    bubble = None
    for r in groups["drafts"]:
        p = r["p"]
        if p.get("cards") and p["cards"] - p["checked"] > 0:
            left = p['cards'] - p['checked']
            bubble = (_("Grüessech! The {title} pitch is {n} tick from done.", title=r['title'], n=left) if left == 1 else
                      _("Grüessech! The {title} pitch is {n} ticks from done.", title=r['title'], n=left))
            break
    f = f if f in groups else ""
    return render(request, "home.html", groups=groups, arch=arch, q=q, f=f, who=who if who == "mine" else "", n_mine=n_mine,
                  total=len(rows), bubble=bubble,
                  mutz={"screen": "Leads", "bubble": bubble})


def back(request, default="/"):
    """Same site path of the page the form was sent from, for redirects after a switch."""
    u = urlparse(request.headers.get("referer", ""))
    return (u.path + (f"?{u.query}" if u.query else "")) if u.path.startswith("/") and not u.path.startswith("//") else default


@app.post("/officer")
def officer_switch(request: Request, id: str = Form(""), name: str = Form("")):
    oid = db.add_officer(name.strip()[:40]) if name.strip() else id
    if not db.officer(oid):
        raise HTTPException(400, _("Unknown officer"))
    r = RedirectResponse(back(request), 303)
    r.set_cookie("officer", oid, max_age=60 * 60 * 24 * 365, samesite="lax")
    return r


@app.post("/leads/{lid}/owner")
def lead_take_over(request: Request, lid: str):
    l = get_lead(lid)
    m = me(request)
    if l.get("owner") != m["id"]:
        prev = db.officer(l.get("owner"))
        db.update_lead(lid, owner=m["id"])
        db.add_entry(lid, "note", f"Lead assigned to {m['name']}" + (f", was {prev['name']}" if prev else ""),
                     confidential=0, who=m["id"])
    return RedirectResponse(back(request, f"/leads/{lid}"), 303)


@app.post("/leads/new")
def lead_new(request: Request, title: str = Form("New lead"), origin: str = Form("S-GE")):
    lid = db.create_lead(title.strip() or "New lead", origin=origin, owner=me(request)["id"])
    db.update_lead(lid, opened=1)
    return RedirectResponse(f"/leads/{lid}/brief", 303)


DEMO_FIXTURE = Path("needs/fixtures/demo_onepager.txt")


@app.post("/leads/demo")
def lead_demo(request: Request):
    """The demo case is agent E's fixture (Kestrelyn Health Oy, invented), so demo, eval and pitch line up."""
    text, title = DEMO_TEXT, "Sport wearable lead"
    if DEMO_FIXTURE.exists():
        text, title = DEMO_FIXTURE.read_text().strip(), "Kestrelyn Health Oy"
    lid = db.create_lead(title, input_text=text, origin="S-GE", sector="medtech",
                         summary="Glucose and lactate patch for women in endurance sport", demo=1, owner=me(request)["id"])
    db.update_lead(lid, opened=1)
    return RedirectResponse(f"/leads/{lid}/brief?extract=1", 303)


@app.post("/leads/seed")
def lead_seed():
    offs = db.officers()
    for i, s in enumerate(SEED_LEADS):  # shared out between the officers, so Mine and All differ
        db.create_lead(s["title"], input_text=s["text"], origin=s["origin"], sector=s["sector"], summary=s["summary"], demo=1,
                       owner=offs[i % len(offs)]["id"])
    return RedirectResponse("/", 303)


@app.post("/leads/reset")
def leads_reset():
    """Clean slate for a demo: every lead, its pitch and exports go. Officers and the seeded gap report rows stay."""
    import shutil
    db.reset()
    shutil.rmtree(EXPORTS, ignore_errors=True)
    return RedirectResponse("/", 303)


@app.post("/leads/{lid}/delete")
def lead_delete(lid: str):
    db.delete_lead(lid)
    return RedirectResponse("/", 303)


def get_lead(lid):
    l = db.lead(lid)
    if not l:
        raise HTTPException(404, _("Lead not found"))
    return l


def case_ctx(l, step):
    has_matches = any(n.get("searched_at") for n in db.needs(l["id"]))
    draft, saved = db.pitch_draft(l["id"])
    sent = bool(db.versions(l["id"]))
    cards = pitchgen.visible_cards(draft) if draft else []
    return {"step": step, "done": {"brief": bool(db.needs(l["id"])), "matches": has_matches,
                                   "map": bool(db.accepted(l["id"])),
                                   "pitch": sent or bool(cards and all(c.get("checked") for c in cards)), "export": sent},
            "saved": saved, "sent": sent, "owner": db.officer(l.get("owner"))}


@app.get("/leads/{lid}", response_class=HTMLResponse)
def lead_open(lid: str):
    l = get_lead(lid)
    step = {"brief": "brief", "matches": "matches", "map": "map", "pitch": "pitch", "export": "export",
            "sent": "after"}.get(l["step"], "brief")
    if l["stage"] in ("pitched", "visit", "won", "lost"):
        step = "after"
    return RedirectResponse(f"/leads/{lid}/{step}", 303)


@app.get("/leads/{lid}/brief", response_class=HTMLResponse)
def brief(request: Request, lid: str, mode: str = "", extract: int = 0, err: str = "", source: int = 0):
    l = get_lead(lid)
    if not l["opened"]:
        db.update_lead(lid, opened=1)
    mode = mode or l["input_kind"] or "text"
    ns = db.needs(lid)
    arch = archive_stats()
    # two steps: the source first, then the needs read from it; ?source=1 goes back to the source
    view = "source" if (not ns or source or err) else "needs"
    return render(request, "brief.html", lead=l, needs=ns, mode=mode, case=case_ctx(l, "brief"), arch=arch, view=view,
                  has_file=bool(source_file(lid)),
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
        db.add_entry(lid, "one_pager", text[:2000], who=me(request)["id"])
    if website.strip():
        db.add_entry(lid, "website", website.strip(), confidential=0, who=me(request)["id"])
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
            return JSONResponse({"ok": False, "error": _("The Apertus gateway did not answer. Your strike is saved.")})
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
        r["who"] = d.get("who") if d else None
        g = contracts.grants_for(r["entity_id"]) if r.get("entity_id") else []
        r["grants"], r["grants_other"] = relevant_grants(g, n)
        out.append(r)
    # matches the officer added anyway (why not) that are not in the list
    for (nid, eid), d in dec.items():
        if nid == n["id"] and d["decision"] == "accept" and d["match"] and all(x["entity_id"] != eid for x in out):
            m = dict(d["match"])
            m.update(ftype=ftype(m.get("type")), decision="accept", added_by=d["added_by"], who=d.get("who"), grants=[], grants_other=0)
            out.append(m)
    for r in out:
        r["is_adjacent"] = bool(r.get("adjacent") or "adjacent" in (r.get("tags") or []))
        r["parts"] = score_parts(r, n.get("type"))
    out.sort(key=lambda r: r["is_adjacent"])  # direct matches first, adjacent after (stable keeps the rank)
    return out


def score_parts(r, need_type):
    """search.query's ranking formula, factor by factor: score = evidence x role x recency x type x boost.
    Results stored before the search recorded evidence and boost get evidence back-derived, boost included."""
    if r.get("score") is None:
        return None
    score = float(r["score"])
    rec = r.get("recency") or "active"
    rm = RECENCY_MULT.get(rec, 1.0)
    sen = float(r["seniority"]) if r.get("seniority") is not None else (0.5 if r["ftype"] == "person" else 1.0)
    tw = 1.0 if need_type == "infrastructure" else TYPE_W.get(r.get("type"), 1.0)
    boost, match = r.get("boost"), r.get("match")
    derived = match is None
    if derived:
        match = score / (sen * rm * tw) if sen * rm * tw else 0.0
    g = r.get("grade")
    ev_note = _("Best quote against the need, local cross encoder")
    if g:
        ev_note = _("Best quote against the need, local cross encoder, blended 60 % with the Apertus 70B grade {g}/3", g=g)
    if derived:
        ev_note += ". " + _("Older search: includes any small-group boost")
    rows = [
        ("Evidence", match, ev_note),
        ("Role", sen, _("Function weight: {f}", f=r.get("function") or _("not stated")) if r["ftype"] == "person" else _("Units, studies and infrastructure count fully")),
        ("Recency", rm, f"{_(REC_LABEL.get(rec, rec))}" + (": " + re.sub(r"https?://\S+?(?=\)|$|\s)", lambda u: host(u.group(0)), r["recency_basis"]) if r.get("recency_basis") else "") + " " + _("(active 1.0, quiet 0.8, past 0.6)")),
        ("Type", tw, _("Infrastructure counts 0.6 unless the need asks for it, studies 0.9") if tw != 1.0 else _("Full weight for this need")),
    ]
    if not derived:
        rows.append(("Small group", boost or 1.0, _("x{boost} for a small unit with a strong quote (fair exposure)", boost=SMALL_UNIT_BOOST) if (boost or 1.0) > 1 else _("No boost")))
    return {"rows": rows, "score": score, "evidence": match, "derived": derived}


def sort_filter(results, sort, cov, rec, smin, grade):
    """Filter on the score signals, then order. Adjacent matches stay after direct ones in every order."""
    out = [r for r in results
           if (not cov or r.get("coverage") == cov) and (not rec or r.get("recency") == rec)
           and (not smin or (r.get("score") or 0) >= float(smin)) and (not grade or str(r.get("grade")) == grade)]
    key = {"evidence": lambda r: -((r.get("parts") or {}).get("evidence") or 0),
           "recency": lambda r: (-(r.get("recency_year") or 0), -(r.get("score") or 0)),
           "activity": lambda r: (REC_ORDER.get(r.get("recency"), 3), -(r.get("score") or 0)),
           "name": lambda r: (r.get("name") or "").lower()}.get(sort)
    if key:
        out.sort(key=key)
    out.sort(key=lambda r: r["is_adjacent"])
    return out


@app.get("/leads/{lid}/matches", response_class=HTMLResponse)
def matches(request: Request, lid: str, need: str = "", type: str = "all", why: str = "", sort: str = "score",
            cov: str = "", rec: str = "", smin: str = "", grade: str = ""):
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
    sort = sort if sort in dict((k, 1) for k, *_ in SORTS) else "score"
    cov = cov if cov in ("strong", "thin") else ""
    rec = rec if rec in REC_ORDER else ""
    smin = smin if smin in dict(SCORE_BANDS) else ""
    grade = grade if grade in ("1", "2", "3") else ""
    typed = [r for r in results if type == "all" or r["ftype"] == type]
    shown = sort_filter(typed, sort, cov, rec, smin, grade)
    facets = {"cov": Counter(r.get("coverage") for r in typed), "rec": Counter(r.get("recency") for r in typed),
              "smin": {b: sum(1 for r in typed if (r.get("score") or 0) >= float(b)) for b, _ in SCORE_BANDS},
              "grade": Counter(str(r.get("grade")) for r in typed if r.get("grade"))}
    state = {"need": cur["id"], "type": type, "sort": sort, "cov": cov, "rec": rec, "smin": smin, "grade": grade}

    def murl(**kw):
        q = {**state, **kw}
        return "?" + urlencode({k: v for k, v in q.items() if v and not (k == "type" and v == "all") and not (k == "sort" and v == "score")})
    # number sources across the visible list
    n_src = 0
    for r in shown:
        for s in r.get("reasons", []):
            n_src += 1
            s["n"] = n_src
    bid = sum(1 for n in ns if n.get("coverage") in ("strong", "thin"))
    n_acc = len(db.accepted(lid))
    searching = l["search_state"] == "running" or any(not n.get("searched_at") for n in ns)
    whynot = None
    if why.strip() and cur.get("searched_at"):
        try:
            whynot = contracts.why_not(why.strip(), need_query_text(cur))
            if whynot and whynot.get("entity_id") and any(r["entity_id"] == whynot["entity_id"] for r in shown):
                whynot["shown"] = True
        except Exception:
            whynot = {"name": why, "in_index": None, "reason": _("The ranking log did not answer. Try again in a minute."), "stages": []}
    arch = archive_stats()
    pending = [n for n in ns if not n.get("searched_at")]
    return render(request, "matches.html", lead=l, needs=ns, cur=cur, results=shown, counts=counts, total=len(results),
                  ftype=type, filters=[(k, _(v)) for k, v in TYPE_FILTER], bid=bid,
                  sorts=[(k, _(v), _(d)) for k, v, d in SORTS], sort=sort, cov=cov, rec=rec, smin=smin, grade=grade, facets=facets,
                  bands=[(k, _(v)) for k, v in SCORE_BANDS], grades={k: _(v) for k, v in GRADE_LABEL.items()},
                  rec_label={k: _(v) for k, v in REC_LABEL.items()}, murl=murl,
                  filtered=bool(cov or rec or smin or grade), n_typed=len(typed),
                  any_grants=any(r.get("grants") or r.get("grants_other") for r in shown), n_acc=n_acc, searching=searching,
                  pending=pending, arch=arch, case=case_ctx(l, "matches"), whynot=whynot, why=why,
                  mutz={"screen": "Matches", "lead": lid, "need": cur["id"], "need_text": cur["text"],
                        "pot": cur.get("coverage") if cur.get("searched_at") else None,
                        "bubble": (_("Search finished.") if not searching and request.query_params.get("fresh") else None)})


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
    db.decide(lid, nid, eid, b.get("decision"), match=match, added_by=b.get("added_by", "officer"), who=me(request)["id"])
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

_PAGES = {}
HOW_NOTE = {"Quoted on page": "Word for word on the page.",
            "Inferred": "Worked out from the page, not quoted word for word. Check before use.",
            "No source": "No page states this. Check before use."}


def page_meta(url):
    """Title and fetch date of a crawled page, by URL without the #fragment."""
    if not url:
        return {}
    c = stubs.corpus()
    if _PAGES.get("c") is not c:
        _PAGES.clear()
        _PAGES.update(c=c, ix={p["url"].split("#")[0]: p for p in c.pages.values()})
    pg = _PAGES["ix"].get(url.split("#")[0]) or {}
    return {"title": pg.get("title"), "fetched": fmt_date(pg.get("fetched_at"))}


def _src(url, quote=None, deep_link=None):
    return {"url": url, "host": host(url or ""), "quote": quote, "deep_link": deep_link or url, **page_meta(url)}


def kg_connections(eids, on_map):
    """The knowledge graph around each match: its units, the heads, colleagues, projects and facilities of
    those units, expertise labels and how many crawled pages link it. on_map maps entity_id -> the map node id
    of that entity, so a neighbour that is also a match on this map links to it. {} when kg.sqlite is missing."""
    if not KG.exists() or not eids:
        return {}
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{KG}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    c.row_factory = sqlite3.Row
    q = lambda sql, *a: c.execute(sql, a).fetchall()

    def unit(uid):
        r = q("SELECT id, name, type, parent_id, url FROM unit WHERE id=?", uid)
        return dict(r[0]) if r else None

    def node(rel, kind, eid, name, sub=None, url=None):
        name = re.sub(r"\s*<br\s*/?>\s*", " · ", name or "").strip()
        return {"rel": rel, "kind": kind, "name": name, "sub": sub or None, "url": url or None, "go": on_map.get(eid)}

    out = {}
    try:
        for eid in eids:
            kind = eid.split(":", 1)[0]
            groups, units = [], []
            if kind == "person":
                p = q("SELECT primary_unit_id FROM person WHERE id=?", eid)
                if not p:
                    continue
                # the most specific unit first (a group before its institute), then the primary one
                affs = q("""SELECT a.unit_id, a.function FROM affiliation a LEFT JOIN unit u ON u.id=a.unit_id WHERE a.person_id=?
                            ORDER BY COALESCE(u.parent_id, '')<>'' DESC, a.unit_id=? DESC, a.seniority DESC""",
                         eid, p[0]["primary_unit_id"] or "")
                for a in affs:
                    u = unit(a["unit_id"])
                    if u and u["id"] not in {x["id"] for x in units}:
                        units.append(u)
                par = unit(units[0]["parent_id"]) if units and units[0]["parent_id"] else None
                # a second unit only from another branch: the same group often has a German and an English unit
                member = units[:1] + [u for u in units[1:] if u["id"] != (par or {}).get("id")
                                      and u["parent_id"] != units[0]["parent_id"]][:1]
                groups.append([node("Member of", "unit", u["id"], u["name"], next((a["function"] for a in affs if a["unit_id"] == u["id"] and a["function"]), None), u["url"])
                               for u in member])
                if par:
                    groups.append([node("Part of", "unit", par["id"], par["name"], par["type"], par["url"])])
                scope = [u["id"] for u in units[:1]]
            elif kind == "unit":
                u = unit(eid)
                if not u:
                    continue
                if u["parent_id"]:
                    par = unit(u["parent_id"])
                    if par:
                        groups.append([node("Part of", "unit", par["id"], par["name"], par["type"], par["url"])])
                kids = q("SELECT id, name, type, url FROM unit WHERE parent_id=? ORDER BY name", eid)
                groups.append([node("Has group", "unit", k["id"], k["name"], k["type"], k["url"]) for k in kids[:3]] +
                              ([{"rel": "Has group", "more": len(kids) - 3}] if len(kids) > 3 else []))
                scope = [eid] + [k["id"] for k in kids]
            else:   # project or infrastructure: the unit that runs it
                t = "project" if kind == "project" else "infrastructure"
                r = q(f"SELECT unit_id FROM {t} WHERE id=?", eid)
                u = unit(r[0]["unit_id"]) if r and r[0]["unit_id"] else None
                if not u:
                    continue
                groups.append([node("Run by", "unit", u["id"], u["name"], u["type"], u["url"])])
                scope = [u["id"]]
            if scope:
                ph = ",".join("?" * len(scope))
                ppl = q(f"""SELECT p.id, p.name, p.profile_url, MAX(a.seniority) sen, MAX(a.function_class IN ('head','professor')) head,
                                   MAX(a.function) fn FROM affiliation a JOIN person p ON p.id=a.person_id
                            WHERE a.unit_id IN ({ph}) AND p.id<>? GROUP BY p.id""", *scope, eid)
                ppl = sorted(ppl, key=lambda r: (r["id"] not in on_map, -(r["head"] or 0), -(r["sen"] or 0), r["name"]))
                heads = [r for r in ppl if r["head"]][:2]
                rest = [r for r in ppl if r not in heads]
                groups.append([node("Head", "person", r["id"], r["name"], r["fn"], r["profile_url"]) for r in heads])
                groups.append([node("Colleague" if kind == "person" else "People", "person", r["id"], r["name"], r["fn"], r["profile_url"])
                               for r in rest[:4]] + ([{"rel": "Colleague" if kind == "person" else "People", "more": len(rest) - 4}] if len(rest) > 4 else []))
                for t, rel, col in (("project", "Project", "title"), ("infrastructure", "Facility", "name")):
                    if t == kind:
                        continue
                    rows = q(f"SELECT id, {col} AS name, url FROM {t} WHERE unit_id IN ({ph}) ORDER BY {col}", *scope)
                    groups.append([node(rel, t, r["id"], r["name"], None, r["url"]) for r in rows[:3]] +
                                  ([{"rel": rel, "more": len(rows) - 3}] if len(rows) > 3 else []))
            exp = [r["label"] for r in q("""SELECT e.label FROM entity_expertise x JOIN expertise e ON e.id=x.expertise_id
                                            WHERE x.entity_id=? ORDER BY e.label LIMIT 8""", eid)]
            npages = q("SELECT COUNT(DISTINCT page_id) n FROM page_entity WHERE entity_id=?", eid)[0]["n"]
            same = [dict(r) for r in q("""SELECT p.name, s.reason FROM possible_same s JOIN person p ON p.id = CASE WHEN s.a_id=? THEN s.b_id ELSE s.a_id END
                                          WHERE ? IN (s.a_id, s.b_id)""", eid, eid)]
            out[eid] = {"links": [n for g in groups for n in g], "expertise": exp, "pages": npages, "possible_same": same}
    except sqlite3.Error:
        log.exception("knowledge graph lookup failed")
    finally:
        c.close()
    return out


def profile(r):
    """A match's ranking factors on one 0-1 scale, for the ranking bars: score_parts' rows plus the Apertus grade."""
    parts = {k: v for k, v, _ in ((r.get("parts") or {}).get("rows") or [])}
    clamp = lambda v: None if v is None else max(0.0, min(1.0, round(float(v), 2)))
    return [[_("Evidence"), clamp(parts.get("Evidence"))], [_("Role"), clamp(parts.get("Role"))],
            [_("Recency"), clamp(parts.get("Recency"))], [_("Type"), clamp(parts.get("Type"))],
            [_("AI check"), clamp(r["grade"] / 3) if r.get("grade") else None]]


def build_map(lid, only_accepted=True):
    """Need -> match -> fact -> source, as a nested tree. The page collapses it to needs and matches.
    detail holds everything the side panel shows for each node, keyed by the node's data-id."""
    ns = db.needs(lid)
    accd = {(d["need_id"], d["entity_id"]) for d in db.decisions(lid) if d["decision"] == "accept"}
    per_need = [(n, decorate(n, lid)) for n in ns]
    entity_needs = Counter(r["entity_id"] for n, rs in per_need for r in rs if (n["id"], r["entity_id"]) in accd)
    in_needs = {}  # entity -> the needs it is accepted for, or suggested in
    for ni, (n, rs) in enumerate(per_need):
        for r in rs:
            if (n["id"], r["entity_id"]) in accd or (not only_accepted and r.get("decision") != "reject"):
                in_needs.setdefault(r["entity_id"], []).append(
                    {"i": ni + 1, "label": _(n["label"]), "accepted": (n["id"], r["entity_id"]) in accd})
    needs_out, detail = [], {}
    for ni, (n, rs) in enumerate(per_need):
        ms = [r for r in rs if (n["id"], r["entity_id"]) in accd or (not only_accepted and r.get("decision") != "reject")]
        if not ms:
            continue
        matches_out = []
        for r in ms:
            acc = (n["id"], r["entity_id"]) in accd
            mid = f"m:{n['id']}:{r['entity_id']}"
            leaves = []
            if r["ftype"] == "person" and r.get("function"):   # a unit's function is its type ("centre"), not a role
                # the role comes from the staff listing, not from the matched quote, so it links the page and quotes nothing
                purl = (r.get("contact") or {}).get("profile_url") or (r.get("reasons") or [{}])[0].get("url")
                leaves.append({"kind": "Role", "how": "Inferred", "label": r["function"], "url": purl, "quote": None,
                               "deep_link": purl, "style": "derived", "src": host(purl or "")})
            elif r["ftype"] == "person":
                leaves.append({"kind": "Role", "how": "No source", "label": _("Not stated on any source"), "style": "missing"})
            for s_ in (r.get("reasons") or [])[:3]:
                leaves.append({"kind": "Fact", "how": "Quoted on page", "label": s_.get("quote", ""), "url": s_.get("url"), "quote": s_.get("quote"),
                               "deep_link": s_.get("deep_link") or s_.get("url"), "style": "read", "src": host(s_.get("url", ""))})
            for g in (r.get("grants") or [])[:1]:
                leaves.append({"kind": "Grant", "how": "Quoted on page", "label": g.get("title", ""), "url": g.get("url"), "quote": g.get("title"),
                               "deep_link": g.get("url"), "style": "read", "src": host(g.get("url", "")) or "SNSF"})
            if not leaves:
                leaves = [{"kind": "Fact", "how": "No source", "label": _("No quote stored"), "style": "missing"}]
            for k, f in enumerate(leaves):
                f["id"], f["sid"] = f"{mid}:f{k}", f"{mid}:s{k}"
                src = _src(f.get("url"), f.get("quote"), f.get("deep_link")) if f.get("url") else None
                detail[f["id"]] = {"kind": "Fact", "kicker": _("Quote") if f["kind"] == "Fact" else _(f["kind"]), "title": f["label"], "how": f["how"],
                                   "how_note": _(HOW_NOTE[f["how"]]), "quote": f.get("quote"), "src": src,
                                   "match": r.get("name"), "match_id": mid, "need": _(n["label"]), "need_i": ni + 1,
                                   "entity": r["entity_id"], "need_id": n["id"], "accepted": acc}
                if src:
                    detail[f["sid"]] = {"kind": "Source", "kicker": _("Source page"), "title": src.get("title") or src["host"], "sub": src["host"], "src": src,
                                        "quote": f.get("quote"), "fact_id": f["id"], "match": r.get("name")}
            seen, pages = set(), []
            for x in (r.get("reasons") or []):
                u = (x.get("url") or "").split("#")[0]
                if u and u not in seen:
                    seen.add(u)
                    pages.append(_src(x.get("url")))
            people = [{"name": p.get("name"), "role": p.get("function")} for p in (r.get("contacts") or r.get("people") or [])]
            officer = db.officer(r.get("who")) if acc else None
            unit = r.get("unit") if r.get("unit") != r.get("name") else None
            head = [r.get("function") or _("Role not stated"), unit] if r["ftype"] == "person" else [(r.get("function") or "").capitalize(), unit]
            detail[mid] = {
                "kind": "Match", "kicker": _(TYPE_LABEL.get(r["ftype"], "Match")), "title": r.get("name"),
                "sub": " · ".join(x for x in head if x) or None, "accepted_by": officer["name"] if officer else None,
                "type": _(TYPE_LABEL.get(r["ftype"], "Match")), "unit": r.get("unit") if r.get("unit") != r.get("name") else None,
                "role": r.get("function"), "person": r["ftype"] == "person", "people": people,
                "recency": " · ".join(x for x in [_(REC_LABEL.get(r.get("recency"), r.get("recency") or "")),
                                                  str(r["recency_year"]) if r.get("recency_year") else ""] if x) or None,
                "recency_basis": re.sub(r"https?://\S+?(?=\)|$|\s)", lambda u: host(u.group(0)), r["recency_basis"]) if r.get("recency_basis") else None,
                "coverage": r.get("coverage"), "grade": GRADE_LABEL.get(r.get("grade")) and f"{r['grade']}/3, {_(GRADE_LABEL[r['grade']]).lower()}",
                "score": round(float(r["score"]), 2) if r.get("score") is not None else None,
                "why": re.sub(r"\s*(\[\d+\])+", "", r.get("explanation") or r.get("reason") or "").strip() or None,
                "facts": [{"quote": x.get("quote"), **_src(x.get("url"), x.get("quote"), x.get("deep_link"))} for x in (r.get("reasons") or [])],
                "grants": [{"title": g.get("title"), "url": g.get("url"), "year": str(g.get("start") or g.get("year") or "")[:4] or None} for g in (r.get("grants") or [])],
                "grants_other": r.get("grants_other") or 0,
                "pages": pages, "needs": in_needs.get(r["entity_id"], []), "profile": profile(r),
                "entity": r["entity_id"], "need_id": n["id"], "accepted": acc, "added_by": r.get("added_by")}
            matches_out.append({"entity": r["entity_id"], "id": mid, "name": r.get("name"), "type": _(TYPE_LABEL.get(r["ftype"], "Match")),
                                "accepted": acc, "facts": leaves,
                                "badge": entity_needs[r["entity_id"]] if entity_needs[r["entity_id"]] > 1 else 0})
        nid = f"n:{n['id']}"
        detail[nid] = {"kind": "Need", "kicker": _("Need {i}", i=ni + 1), "title": _(n["label"]), "text": n["text"],
                       "sentence": n.get("from_sentence") or None,
                       "from": {"pdf": "the PDF", "website": "the website", "keywords": "your keywords"}.get(n.get("source"), "the pasted text"),  # the map script translates "from " + this
                       "cov": n.get("coverage") or "none", "shown": len(ms), "accepted_n": sum(1 for r in ms if (n["id"], r["entity_id"]) in accd),
                       "found": len((n.get("result") or {}).get("results") or []),
                       "terms": [t["t"] for t in n.get("terms") or [] if t.get("on")]}
        needs_out.append({"id": n["id"], "nid": nid, "i": ni + 1, "label": _(n["label"]), "text": n["text"],
                          "cov": n.get("coverage") or "none", "matches": matches_out})
    on_map = {}
    for n in needs_out:
        for mt in n["matches"]:
            on_map.setdefault(mt["entity"], mt["id"])
    kg = kg_connections(list(on_map), on_map)
    for d in detail.values():
        if d["kind"] == "Match":
            d["kg"] = kg.get(d["entity"])
    return {"needs": needs_out, "detail": detail}


@app.get("/leads/{lid}/map", response_class=HTMLResponse)
def evmap(request: Request, lid: str, all: int = 0):
    l = get_lead(lid)
    m = build_map(lid, only_accepted=not all)
    draft, _v = db.pitch_draft(lid)
    in_pitch = {c.get("entity_id") for c in (draft or {}).get("cards", []) if not c.get("deleted")}
    if l["step"] in ("brief", "matches"):
        db.update_lead(lid, step="map")
    return render(request, "map.html", lead=l, m=m, all=all, in_pitch=list(in_pitch), case=case_ctx(l, "map"),
                  mutz={"screen": "Evidence map", "lead": lid})


# ---------------------------------------------------------------- pitch builder

def ensure_draft(lid):
    draft, _v = db.pitch_draft(lid)
    l = db.lead(lid)
    acc = db.accepted(lid)
    if draft is None:
        draft = pitchgen.new_draft(l, db.needs(lid), acc)
        pitchgen.suggest(draft, l, db.needs(lid))
        db.save_pitch(lid, draft)
    else:
        pitchgen.upgrade(draft)
        # accepted after the draft was made: append as new match cards, never touch existing ones
        have = {c.get("entity_id") for c in draft["cards"]}
        add = [d for d in acc if d["entity_id"] not in have and d.get("match")]
        if add:
            draft = pitchgen.add_cards(draft, l, db.needs(lid), add)
            db.save_pitch(lid, draft)
        before = draft.get("suggested_at")
        pitchgen.suggest(draft, l, db.needs(lid))  # once per draft, also for drafts made before suggestions existed
        if draft.get("suggested_at") != before:
            db.save_pitch(lid, draft)
    owner = db.owner_name(l)
    if owner == "[Officer name]":  # the placeholder prints in the pitch, so it is in the pitch language
        owner = pitch_locales.tr(draft.get("lang"), owner)
    if (draft.get("officer") or {}).get("name") != owner:  # the contact on the pitch is whoever owns the lead now
        draft["officer"] = {**(draft.get("officer") or {}), "name": owner}
        db.save_pitch(lid, draft)
    return draft


def draft_pending(lid, draft):
    """The slow steps ensure_draft still has to run, in order, each an Apertus call: [] when the stored draft is ready."""
    if draft is None:
        return ["read", "write", "suggest"]
    have = {c.get("entity_id") for c in draft["cards"]}
    out = ["read", "write"] if any(d["entity_id"] not in have and d.get("match") for d in db.accepted(lid)) else []
    return out + ([] if draft.get("suggested_at") else ["suggest"])


@app.get("/leads/{lid}/pitch", response_class=HTMLResponse)
def pitch(request: Request, lid: str, section: str = "why"):
    l = get_lead(lid)
    stored, _v = db.pitch_draft(lid)
    if not db.accepted(lid) and stored is None:
        return render(request, "pitch_empty.html", lead=l, case=case_ctx(l, "pitch"), mutz={"screen": "Pitch", "lead": lid})
    # a draft still to be written opens at once with a wait panel; the editor then asks /api/pitch/{lid}/build for it
    pending = draft_pending(lid, stored)
    draft = None if pending else ensure_draft(lid)
    lang = (draft or stored or {}).get("lang") or l.get("lang") or "en"
    if l["step"] in ("brief", "matches", "map"):
        db.update_lead(lid, step="pitch")
    # the editor shows the document as it prints: its fixed words in the pitch language, the controls in the interface's
    return render(request, "pitch.html", lead=l, draft=draft, pending=pending, section=section, case=case_ctx(l, "pitch"),
                  today=pitch_locales.fmt_date(datetime.now(), lang), doc=pitch_locales.catalog(lang),
                  mutz={"screen": "Pitch", "lead": lid, "corner": "left", "bubble": _("Ask Bärner Mutz about this pitch")})


_build_locks: dict[str, threading.Lock] = {}


@app.post("/api/pitch/{lid}/build")
def pitch_build(lid: str):
    """The draft for the editor, made or topped up first. One build per lead at a time: a reload waits for the
    running one instead of writing the cards twice."""
    get_lead(lid)
    with _build_locks.setdefault(lid, threading.Lock()):
        try:
            draft = ensure_draft(lid)
        except Exception as e:
            log.warning("pitch build failed: %s", e)
            return {"ok": False, "error": _("The facts for the pitch could not be drafted this time. Nothing was lost. Try again in a minute.")}
    return {"ok": True, "draft": draft}


@app.post("/api/pitch/{lid}")
async def pitch_save(lid: str, request: Request):
    body = await request.json()
    get_lead(lid)
    db.save_pitch(lid, body["draft"])
    return {"ok": True, "saved": fmt_date(db.now(), True)}


@app.post("/api/pitch/{lid}/rewrite")
async def pitch_rewrite(lid: str, request: Request):
    b = await request.json()
    draft, _v = db.pitch_draft(lid)
    card = next((c for c in draft["cards"] if c["card_id"] == b["card_id"]), None)
    if not card:
        raise HTTPException(404)
    if card.get("locked"):
        return {"ok": False, "error": _("This card is locked. Choose Allow edits to regenerate it.")}
    try:
        text = pitchgen.rewrite(card, b.get("instruction", "shorter"), lang=b.get("lang"))
    except Exception:
        return {"ok": False, "error": _("The Apertus gateway did not answer. The card is unchanged. Try again in a minute.")}
    return {"ok": True, "text": text}


@app.post("/api/pitch/{lid}/write")
async def pitch_write(lid: str, request: Request):
    """Turn the checked facts into pitch text. The editor sends its draft so unsaved edits are not lost."""
    b = await request.json()
    l = get_lead(lid)
    draft = b.get("draft") or ensure_draft(lid)
    left = sum(1 for c in pitchgen.visible_cards(draft) if not c.get("checked"))
    if left:  # the pitch is written from checked facts only
        return {"ok": False, "error": _("Check every card first. {n} left.", n=left)}
    try:
        draft = pitchgen.write_pitch(draft, l, db.needs(lid))
    except Exception as e:
        log.warning("pitch write failed: %s", e)
        db.save_pitch(lid, draft)
        return {"ok": False, "error": _("Apertus did not write the pitch this time. Nothing changed. Try again in a minute.")}
    db.save_pitch(lid, draft)
    return {"ok": True, "draft": draft, "saved": fmt_date(db.now(), True)}


@app.post("/api/pitch/{lid}/change")
async def pitch_change(lid: str, request: Request):
    """The box under the pitch: the officer says what to change, Apertus edits only that. The editor sends its draft."""
    b = await request.json()
    l = get_lead(lid)
    draft = b.get("draft") or ensure_draft(lid)
    ask = (b.get("instruction") or "").strip()
    if not ask:
        return {"ok": False, "error": _("Say what to change first.")}
    ui = {"de": "German", "it": "Italian"}.get(i18n.lang(), "English")
    try:
        draft, changed, note, refused = pitchgen.change(draft, l, ask, ui_lang=ui)
    except Exception as e:
        log.warning("pitch change failed: %s", e)
        return {"ok": False, "error": _("The Apertus gateway did not answer. Nothing changed. Try again in a minute.")}
    if not changed:
        return {"ok": False, "error": note or (_("The change did not pass the checks: no marketing words, no promised "
                "meetings, and cards keep their sources. Nothing changed.") if refused else _("Nothing to change for that request."))}
    db.save_pitch(lid, draft)
    if refused:
        extra = (_("1 more change did not pass the checks and was left out.") if refused == 1
                 else _("{n} more changes did not pass the checks and were left out.", n=refused))
        note = f"{note} {extra}".strip()
    return {"ok": True, "draft": draft, "changed": changed, "note": note, "saved": fmt_date(db.now(), True)}


@app.post("/api/pitch/{lid}/reset")
def pitch_reset(lid: str):
    db.x("DELETE FROM pitch WHERE lead_id=?", (lid,))
    return {"ok": True}


# ---------------------------------------------------------------- export: review, PDF, send
# Its own step after the pitch: cards are checked in the pitch editor, export only shows what goes out and sends it.

@app.get("/leads/{lid}/review")
def review_old(request: Request, lid: str):
    q = request.url.query
    return RedirectResponse(f"/leads/{lid}/export" + (f"?{q}" if q else ""), 307)


@app.get("/leads/{lid}/export", response_class=HTMLResponse)
def review(request: Request, lid: str, done: int = 0, err: str = ""):
    l = get_lead(lid)
    if done:  # old link to the sent screen: that is After sending now
        return RedirectResponse(f"/leads/{lid}/after", 303)
    draft = ensure_draft(lid) if (db.accepted(lid) or db.pitch_draft(lid)[0]) else None
    if not draft:
        return RedirectResponse(f"/leads/{lid}/pitch", 303)
    if l["step"] in ("brief", "matches", "map", "pitch"):
        db.update_lead(lid, step="export")
    p = pitchgen.to_contract(draft, l, db.needs(lid))
    try:
        problems = contracts.validate(p)
    except Exception as e:
        problems = [_("validate failed: {e}", e=e)]
    cards = pitchgen.visible_cards(draft)
    stats = {"verified": sum(1 for c in cards if not c.get("edited") and c.get("sources") and all(s.get("quote") for s in c["sources"])),
             "edited": sum(1 for c in cards if c.get("edited") and c.get("sources")),
             "nosource": sum(1 for c in cards if not c.get("sources")),
             "unchecked": sum(1 for c in cards if not c.get("checked")), "total": len(cards)}
    vs = db.versions(lid)
    ready = not problems and not stats["nosource"] and not stats["unchecked"]
    first_unchecked = next((c["card_id"] for c in cards if not c.get("checked")), "")
    from app import lifecycle
    return render(request, "review.html", lead=l, draft=draft, stats=stats, problems=problems,
                  versions=vs, err=err, first_unchecked=first_unchecked, case=case_ctx(l, "export"),
                  mutz={"screen": "Export", "lead": lid, "corner": "left",
                        "bubble": _("Every card is checked. Good to send.") if ready else None})


@app.post("/leads/{lid}/export")
def export(request: Request, lid: str, override: str = Form("")):
    l = get_lead(lid)
    draft = ensure_draft(lid)
    cards = pitchgen.visible_cards(draft)
    if any(not c.get("checked") for c in cards) and not override:
        return RedirectResponse(f"/leads/{lid}/export?err=unchecked", 303)
    p = pitchgen.to_contract(draft, l, db.needs(lid))
    if override:
        p["override_review_gate"] = True
    n = (db.one("SELECT COALESCE(MAX(n),0)+1 AS n FROM pitch_version WHERE lead_id=?", (lid,)) or {"n": 1})["n"]
    out = EXPORTS / lid / f"v{n}"
    out.mkdir(parents=True, exist_ok=True)
    try:
        # the clean PDF is what goes out; the review PDF and JSON are the agency's own copies
        contracts.build_pdf(p, "clean", str(out / "clean.pdf"))
        contracts.build_pdf(p, "review", str(out / "review.pdf"))
    except Exception as e:
        probs = getattr(e, "problems", None) or [str(e)]
        log.warning("export refused: %s", probs)
        return RedirectResponse(f"/leads/{lid}/export?err=" + "; ".join(probs)[:400].replace("&", "and"), 303)
    (out / "pitch.json").write_text(json.dumps({**p, "draft": draft}, ensure_ascii=False, indent=1))
    built_from = [e["id"] for e in db.entries(lid)]
    db.add_version(lid, {**p, "draft": draft}, built_from)
    db.add_entry(lid, "pitch", f"Pitch v{n} sent", confidential=0, who=me(request)["id"])
    db.update_lead(lid, stage="pitched", step="sent")
    db.update_signal_stage(lid, "pitched")
    return RedirectResponse(f"/leads/{lid}/after?dl={n}", 303)  # the next step; the page downloads the clean PDF


@app.get("/leads/{lid}/export/v{n}/{name}")
def export_file(lid: str, n: int, name: str):
    if name not in ("clean.pdf", "review.pdf", "pitch.json"):
        raise HTTPException(404)
    p = EXPORTS / lid / f"v{n}" / name
    if not p.exists():
        raise HTTPException(404, _("Not exported in this version"))
    l = db.lead(lid)
    fname = re.sub(r"[^A-Za-z0-9]+", "-", (l or {}).get("title", "pitch")).strip("-") + f"-v{n}-{name}"
    return FileResponse(p, filename=fname)


# ---------------------------------------------------------------- after sending: lead, follow up

@app.get("/leads/{lid}/after", response_class=HTMLResponse)
def after(request: Request, lid: str):
    l = get_lead(lid)
    from app import lifecycle
    mail = lifecycle.emails(lid) if l["stage"] in ("visit", "won") else None
    return render(request, "lead.html", lead=l, entries=db.entries(lid), versions=db.versions(lid),
                  diff=lifecycle.pending_view(lid), case=case_ctx(l, "after"), stages=STAGES, lost=LOST_REASONS,
                  sent=lifecycle.sent_view(lid), mail=mail, mailto=lifecycle.mailto,
                  mutz={"screen": "After sending", "lead": lid})


@app.post("/leads/{lid}/entry")
def add_info(request: Request, lid: str, kind: str = Form("note"), text: str = Form(...), confidential: str = Form(""),
             shareable: str = Form("")):
    get_lead(lid)
    kind = kind if kind in ("email", "call", "note") else "note"  # the form sends no kind now: a note
    if text.strip():
        db.add_entry(lid, kind, text.strip(), confidential=1 if confidential else 0, shareable=1 if shareable else 0,
                     who=me(request)["id"])
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
def set_stage(request: Request, lid: str, stage: str = Form(...), lost_reason: str = Form("")):
    get_lead(lid)
    if stage not in STAGES:
        raise HTTPException(400)
    if stage == "lost" and not lost_reason:
        return RedirectResponse(f"/leads/{lid}/after?err=reason", 303)
    if stage == "visit":  # the visit stage is the CEO's yes: same entry and email drafts as the button
        from app import lifecycle
        lifecycle.ceo_accepted(lid, who=me(request)["id"])
        return RedirectResponse(f"/leads/{lid}/after#emails", 303)
    db.update_lead(lid, stage=stage, lost_reason=lost_reason or None)
    db.update_signal_stage(lid, stage, lost_reason or None)
    db.add_entry(lid, "note", f"Status: {stage}" + (f", {lost_reason}" if lost_reason else ""), confidential=0,
                 who=me(request)["id"])
    return RedirectResponse(f"/leads/{lid}/after", 303)


@app.post("/leads/{lid}/accepted")
def ceo_accepted(request: Request, lid: str):
    """One click after the CEO's reply: the lead moves to CEO accepted and the researcher emails start drafting."""
    get_lead(lid)
    from app import lifecycle
    lifecycle.ceo_accepted(lid, who=me(request)["id"])
    return RedirectResponse(f"/leads/{lid}/after#emails", 303)


@app.get("/api/leads/{lid}/emails")
def emails_status(lid: str):
    from app import lifecycle
    return lifecycle.emails(lid) or {"emails": [], "running": False}


@app.post("/api/leads/{lid}/emails")
async def email_save(lid: str, request: Request):
    b = await request.json()
    from app import lifecycle
    if b.get("redo"):
        lifecycle.start_emails(lid, b.get("key"))
        return {"ok": True}
    return {"ok": lifecycle.save_email(lid, b.get("key"), b.get("subject"), b.get("body"))}


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
    return render(request, "gaps.html", **lifecycle.gaps(year or datetime.now().year), field_name={k: _(v) for k, v in FIELD_NAME.items()},
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
    cap = lifecycle.capability(recompute=bool(recompute))
    # word cloud: size by supply (units and pages), biggest word in the middle
    top = max([r["units"] + r["pages"] for r in cap["rows"]] or [0]) or 1
    cloud = []
    for i, r in enumerate(sorted(cap["rows"], key=lambda r: -(r["units"] + r["pages"]))):
        w = {**r, "size": round(15 + 45 * ((r["units"] + r["pages"]) / top) ** 0.5)}
        cloud.append(w) if i % 2 else cloud.insert(0, w)
    return render(request, "capability.html", **cap, cloud=cloud, mutz={"screen": "Capability map"})


# ---------------------------------------------------------------- archives, evaluation, states

@app.get("/archives", response_class=HTMLResponse)
@app.get("/archives/add", response_class=HTMLResponse)
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
    view = "add" if request.url.path.endswith("/add") else "sources"
    return render(request, "archives.html", arch=arch, people=people, proposal=proposal, url=url, snsf=snsf, view=view,
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
        return {"ok": False, "url": url, "reason": _("That is not a website address.")}
    if REFUSED.search(url):
        return {"ok": False, "url": url, "reason": _("LinkedIn, social media, login pages and BORIS are refused.")}
    sid = re.sub(r"[^a-z0-9]+", "_", h.replace("www.", "").split(".")[0].lower())
    entry = {"id": sid, "seeds": [url], "allow": [h], "include": [], "exclude": ["/news/", "/search", "/print"], "max_pages": 400}
    body = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in entry.items())
    return {"ok": True, "url": url, "host": h, "id": sid, "entry": "{\n" + body + "\n}"}


@app.get("/eval", response_class=HTMLResponse)
@app.get("/eval/checks", response_class=HTMLResponse)
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
    view = "checks" if request.url.path.endswith("/checks") else "quality"
    return render(request, "eval.html", ev=ev, splits=splits, tests=tests, weakest=weakest, missed=missed, view=view,
                  check_label={k: _(v) for k, v in CHECK_LABEL.items()}, arch=archive_stats(), usage=gateway_usage(), mutz={"screen": "Evaluation"})


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
    return mutz.confirm(b, who=me(request)["id"])


# ---------------------------------------------------------------- Mutz: a file or a one pager in the chat, straight to a pitch

QUICK_MAX_BYTES = 10 * 1024 * 1024
PICK_PER_NEED, PICK_MAX = 2, 8
_quick_pool = ThreadPoolExecutor(max_workers=2)  # its own threads, so a drafting chat never holds up a search


def curated_match(e):
    """A curated offer as a match, the same shape the Accept button on the Matches screen sends."""
    return {"entity_id": "curated:" + str(e.get("id")), "name": e.get("name"), "unit": e.get("region") or "Bern",
            "type": "infrastructure" if e.get("kind") in ("clinical_trials", "infrastructure", "space") else "unit",
            "reasons": [{"url": e.get("source_url") or e.get("url"), "quote": e.get("description"),
                         "deep_link": e.get("source_url") or e.get("url")}]}


def mutz_picks(needs):
    """The matches Mutz accepts when it drafts a pitch on its own: per need the first direct matches with a quote and
    strong or thin coverage (curated offers for funding, transfer and talent), one per need first, then a second,
    at most eight in all. A need with no match stays out of the pitch rather than getting a weak guess."""
    per = []
    for n in needs:
        rt = n.get("route") or {}
        if rt.get("kind") == "curated":
            cands = [curated_match(e) for e in rt.get("entries") or [] if e.get("source_url") or e.get("url")]
        else:
            cands = [r for r in (n.get("result") or {}).get("results") or []
                     if r.get("reasons") and r.get("coverage") in ("strong", "thin") and (r.get("grade") is None or r["grade"] >= 2)
                     and not (r.get("adjacent") or "adjacent" in (r.get("tags") or []))]
        per.append((n["id"], cands[:PICK_PER_NEED]))
    out, seen = [], set()
    for i in range(PICK_PER_NEED):
        for nid, cands in per:
            if i < len(cands) and cands[i]["entity_id"] not in seen and len(out) < PICK_MAX:
                seen.add(cands[i]["entity_id"])
                out.append((nid, cands[i]))
    return out


def _quick_state(lid, **kw):
    """The run's progress under kv quick:<lid>: the step, when each step began, and what the chat shows with it."""
    st = db.kv_get(f"quick:{lid}") or {}
    if kw.get("step") and kw["step"] != st.get("step"):
        st.setdefault("t", {})[kw["step"]] = time.time()
    st.update(kw)
    db.kv_set(f"quick:{lid}", st)


def _quick_table(ns, picks):
    """One row per need: what went into the pitch for it, or that Bern had nothing strong."""
    by = defaultdict(list)
    for nid, m in picks:
        by[nid].append(m.get("name") or "")
    return {"head": [_("Need"), _("In the pitch")],
            "rows": [[need_label(n), ", ".join(by[n["id"]]) or _("No strong match")] for n in ns]}


def _quick_reply(lid, keys, text, actions=None, table=None, **kw):
    reply = {"role": "mutz", "text": text, "sources": [], "actions": actions or [], "table": table, "note": None, "after": None}
    for k in keys:
        db.save_thread(k, db.thread(k) + [reply])
    _quick_state(lid, step="done", reply=reply, **kw)


def _quick_job(lid, keys, pdf_path, text, website, who, lang):
    """Brief, search, picks and draft in one go; the chat polls each step (mutz_pitch_status)."""
    i18n.set_lang(lang)
    brief_link = {"kind": "link", "label": _("Open the brief"), "primary": False, "href": f"/leads/{lid}/brief"}
    try:
        _quick_state(lid, step="read")
        try:
            out = contracts.needs(one_pager_text=text or None, pdf_path=str(pdf_path) if pdf_path else None,
                                  website=website or None)
        except Exception:
            log.exception("quick pitch: needs failed")
            return _quick_reply(lid, keys, _("The Apertus gateway did not read the brief this time. The lead is saved; try again from its Brief."),
                                [brief_link], error=True)
        apply_needs(lid, out)
        l, ns = db.lead(lid), db.needs(lid)
        if l.get("company"):
            db.update_lead(lid, title=l["company"])
        if not ns:
            return _quick_reply(lid, keys, _("I read it but found no need a Bern partner could answer. Add one on the Brief."),
                                [brief_link], error=True)
        _quick_state(lid, step="search")
        with _runlock:
            _running.add(lid)  # _search_job takes it out again; the Matches screen sees the run meanwhile
        db.update_lead(lid, search_state="running", search_error=None)
        _search_job(lid, None)
        if pdf_path and pdf_path.exists() and not db.lead(lid)["keep_file"]:
            pdf_path.unlink()  # extract, then forget, as on the Brief
        ns = db.needs(lid)
        picks = mutz_picks(ns)
        matches_link = {"kind": "link", "label": _("Open the matches"), "primary": not picks, "href": f"/leads/{lid}/matches"}
        if not picks:
            return _quick_reply(lid, keys, _("I searched every need ({n}), but Bern has no strong match for any of them, so I wrote no pitch.", n=len(ns)),
                                [matches_link], _quick_table(ns, picks), error=True)
        labels = {n["id"]: need_label(n) for n in ns}
        _quick_state(lid, step="write", picks=[{"name": m.get("name"), "unit": m.get("unit"), "need": labels.get(nid)} for nid, m in picks])
        for nid, m in picks:
            db.decide(lid, nid, m["entity_id"], "accept", match=m, added_by="mutz", who=who)
        with _build_locks.setdefault(lid, threading.Lock()):
            ensure_draft(lid)
        db.update_lead(lid, step="pitch")
        empty = sum(1 for n in ns if all(nid != n["id"] for nid, _m in picks))
        msg = _("The pitch for {name} is drafted. Matches: {n}. Needs covered: {k} of {total}.", name=db.lead(lid)["title"],
                n=len(picks), k=len(ns) - empty, total=len(ns))
        msg += " " + _("Every card cites its quotes; check each one before the PDF goes out.")
        _quick_reply(lid, keys, msg, [{"kind": "link", "label": _("Open the pitch"), "href": f"/leads/{lid}/pitch"}, matches_link],
                     _quick_table(ns, picks), lead=lid)
    except Exception:
        log.exception("quick pitch failed")
        _quick_reply(lid, keys, _("Something went wrong while drafting. The lead is saved; open its Brief to go on by hand."),
                     [brief_link], error=True)


def _pdf_pages(path):
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def source_file(lid):
    """The file a lead was read from, when it was kept: a PDF, or a text file dropped into the chat."""
    for ext in (".pdf", ".txt"):
        if (UPLOADS / f"{lid}{ext}").exists():
            return UPLOADS / f"{lid}{ext}"
    return None


@app.get("/leads/{lid}/file")
def lead_file(lid: str):
    l = get_lead(lid)
    p = source_file(lid)
    if not p:
        raise HTTPException(404, _("The file was not kept with this lead."))
    return FileResponse(p, media_type="application/pdf" if p.suffix == ".pdf" else "text/plain; charset=utf-8",
                        filename=l.get("pdf_name") or p.name, content_disposition_type="inline")


@app.post("/api/mutz/pitch")
async def mutz_pitch(request: Request, key: str = Form("global"), lead: str = Form(""), text: str = Form(""),
                     file: UploadFile | None = File(None)):
    """A PDF, a text file or a pasted one pager dropped into the chat becomes a lead with a drafted pitch. An open lead
    that is still empty takes it; otherwise a new lead is made. The file is kept with the lead, so the chat and the
    Brief can open it. The work runs in the background; the chat polls the GET below."""
    note = text.strip()
    website = note if re.fullmatch(r"https?://\S+|www\.\S+", note) else ""
    text = "" if website else note
    data, name = (await file.read(QUICK_MAX_BYTES + 1), file.filename or "") if file is not None and file.filename else (b"", "")
    if len(data) > QUICK_MAX_BYTES:
        return JSONResponse({"error": _("That file is larger than 10 MB. Send a shorter PDF or paste the text.")}, 400)
    is_pdf = data[:5] == b"%PDF-"
    if data and not is_pdf:
        try:
            text = (data.decode("utf-8") + "\n" + text).strip()
        except UnicodeDecodeError:
            return JSONResponse({"error": _("I can read PDF and plain text files. Paste the text if it is another format.")}, 400)
    if not (is_pdf or text or website):
        return JSONResponse({"error": _("Attach a PDF or paste the company's one pager.")}, 400)
    l = db.lead(lead) if lead else None
    who = me(request)["id"]
    title = re.sub(r"[_-]+", " ", Path(name).stem).strip() if name else (website and host(website)) or _("Lead from Bärner Mutz")
    if l and not l["input_text"] and not db.needs(lead) and not db.pitch_draft(lead)[0]:
        lid = lead
    else:
        lid = db.create_lead(title or _("Lead from Bärner Mutz"), origin="Own contact", owner=who)
    lang = i18n.lang()
    db.update_lead(lid, opened=1, input_text=text, website=website, lang="de" if lang == "de" else "en", keep_file=1 if data else 0,
                   input_kind="pdf" if is_pdf else ("website" if website else "text"), pdf_name=name or None)
    if text:
        db.add_entry(lid, "one_pager", text[:2000], who=who)
    pdf_path, meta = None, None
    if data:
        UPLOADS.mkdir(parents=True, exist_ok=True)
        path = UPLOADS / f"{lid}{'.pdf' if is_pdf else '.txt'}"
        path.write_bytes(data)
        pdf_path = path if is_pdf else None
        meta = {"name": name, "size": len(data), "kind": "PDF" if is_pdf else (Path(name).suffix[1:].upper() or "TXT"),
                "pages": _pdf_pages(path) if is_pdf else None, "url": f"/leads/{lid}/file"}
    said = note if meta else (website or (text[:280] + ("…" if len(text) > 280 else "")))
    keys = list(dict.fromkeys([key or "global", lid]))
    for k in keys:
        db.save_thread(k, db.thread(k) + [{"role": "user", "text": said, "file": meta}])
    db.kv_set(f"quick:{lid}", {"step": "read", "t": {"read": time.time()}, "started": time.time(), "file": meta,
                                "source": "website" if website else None})
    _quick_pool.submit(_quick_job, lid, keys, pdf_path, text, website, who, lang)
    return {"lead": lid, "said": said, "file": meta}


@app.get("/api/mutz/pitch/{lid}")
def mutz_pitch_status(lid: str):
    """The run as the chat shows it: the step, how long it has run, and what is known so far, read live from the lead,
    so each need ticks off as its search ends."""
    st = db.kv_get(f"quick:{lid}")
    if not st:
        return {"step": "done", "error": True, "reply": {"role": "mutz", "text": _("I have no draft running for this lead.")}}
    now = time.time()
    st["elapsed"] = round(now - (st.get("started") or now))
    st["step_elapsed"] = round(now - (st.get("t") or {}).get(st["step"], now))
    if st["step"] == "done":
        return st
    l = db.lead(lid) or {}
    ps = l.get("passages") or []
    st["brief"] = {"company": l.get("company") or "", "sector": (l.get("profile") or {}).get("sector") or "",
                   "used": sum(1 for p in ps if p.get("status") == "used"),
                   "local": sum(1 for p in ps if p.get("status") != "used")}
    current = None
    st["needs"] = []
    for n in db.needs(lid) if st["step"] != "read" else []:
        rt = n.get("route") or {}
        if n.get("searched_at"):
            res = (n.get("result") or {}).get("results") or [] if rt.get("kind") != "curated" else rt.get("entries") or []
            state = "done"
        else:
            res, state = [], "search" if current is None and st["step"] == "search" else "wait"
            current = current or n["id"]
        st["needs"].append({"label": need_label(n), "text": n["text"], "state": state, "coverage": n.get("coverage") if state == "done" else None,
                            "top": (res[0].get("name") if res and n.get("coverage") != "none" else ""), "more": max(len(res) - 1, 0)})
    return st


@app.exception_handler(500)
async def err500(request: Request, exc):
    return HTMLResponse(T.get_template("error.html").render(ctx(request, msg=str(exc)[:200])), status_code=500)
