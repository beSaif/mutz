"""Extraction: rules first, then Apertus 8B with a forced tool call, then the verifier.

    python -m ingest.extract                       # all sources in data/pages.jsonl
    python -m ingest.extract --sources artorg dcb  # some sources (or a group such as unibe)
    python -m ingest.extract --no-llm              # rules only, no gateway calls
    python -m ingest.extract --limit 50            # first 50 LLM pages, for a smoke run

Reads data/pages.jsonl (agent A) and the raw HTML in data/raw/ (via data/raw/index.jsonl, local disk only).
Writes data/extract/facts.jsonl (one record per page, verified facts only) and logs/extract_stats.json.

Resumable: every gateway call goes through common.llm.chat, which caches on disk by payload hash,
so a crash and rerun pays only for pages not yet cached. Bulk concurrency 8 (docs/gateway.md).
"""
import argparse, hashlib, html as htmlmod, json, os, re, sys, threading, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ingest import verify as V

PAGES = Path("data/pages.jsonl")
RAW = Path("data/raw")
OUT = Path("data/extract/facts.jsonl")
STATS = Path("logs/extract_stats.json")
WORKERS = 8
MAX_CHARS = 7000
EXTRACT_VERSION = "b2"

SKIP_TYPES = {"publications", "news"}
SKIP_URL = re.compile(
    r"data-protection|datenschutz|legal-notice|impressum|/contact|/kontakt|newsletter|/media/|/career|/jobs?/|"
    r"open_positions|offene_stellen|/category/|/search|/suche|sitemap|/login|cookie|disclaimer|/print",
    re.I,
)
PUB_URL = re.compile(r"publi[ck]ation|publikation|/news|/aktuell|media_release|medienmitteilung|/events?/|veranstaltung", re.I)

ROOT_NAMES = {"artorg": "ARTORG Center for Biomedical Engineering Research", "dcb": "Diabetes Center Berne"}
UNIT_SEG = re.compile(
    r"/(?:research_programs|research_groups|research_group|groups|forschungsgruppen|arbeitsgruppen|"
    r"research/groups|abteilungen|departments|forschung/gruppen)/([a-z0-9_\-]+)/", re.I)
PHONE_RX = re.compile(r"[+]4[1][\s\d]{9,14}\d")  # Swiss numbers


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(s):
    s = V.fold(s)
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def h6(s):
    return hashlib.sha1(s.encode()).hexdigest()[:6]


# ---------------------------------------------------------------- units from URL structure

def _root_names():
    names = dict(ROOT_NAMES)
    seeds = Path("inventory/unibe_seeds.txt")
    if seeds.exists():
        for line in seeds.read_text().splitlines():
            if "#" in line and line.strip().startswith("http"):
                url, comment = line.split("#", 1)
                sub = urlparse(url.strip()).netloc.removeprefix("www.").split(".")[0]
                names[f"unibe_{sub}"] = comment.strip()
    return names


ROOTS = _root_names()


def page_unit(page):
    """(unit_id, unit_name_or_None, parent_id) for the unit this page belongs to, from its URL."""
    src = page["source"]
    path = urlparse(page["url"]).path
    root = f"unit:{src}:root"
    if src == "artorg":
        m = re.search(r"/research/([a-z0-9_\-]+)/", path)
        if m and m.group(1) not in ("index_eng.html", "index_ger.html"):
            return f"unit:artorg:{m.group(1)}", None, root
    m = UNIT_SEG.search(path)
    if m:
        return f"unit:{src}:{slug(m.group(1))}", None, root
    return root, ROOTS.get(src), None


# ---------------------------------------------------------------- raw HTML access (local only)

_index = None
_index_lock = threading.Lock()


def _load_index():
    global _index
    with _index_lock:
        if _index is not None:
            return _index
        idx = {}
        f = RAW / "index.jsonl"
        if f.exists():
            for line in f.open():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("status") == 200 and r.get("file"):
                    for u in (r["url"], r.get("final_url")):
                        if u:
                            idx[u] = r["file"]
                            idx.setdefault(u.rstrip("/"), r["file"])
        _index = idx
        return _index


def raw_html(page):
    if page.get("html") is not None:          # fixtures carry their HTML inline
        return page["html"]
    idx = _load_index()
    for u in [page["url"], page["url"].rstrip("/"), page["url"] + "/"] + list(page.get("alt_urls") or []):
        rel = idx.get(u)
        if rel and (RAW / rel).exists():
            return (RAW / rel).read_text(errors="ignore")
    return ""


def decode_email(a):
    href = a.attributes.get("href") or ""
    m = re.search(r"fromCharCode\(([\d,\s]+)\)", href)
    if m:
        return "".join(chr(int(x)) for x in m.group(1).split(",")).strip()
    if href.lower().startswith("mailto:") and "@" in href:
        return href[7:].split("?")[0].strip()
    t = htmlmod.unescape(a.text() or "").strip()
    return t if re.fullmatch(r"[\w.+\-]+@[\w\-]+(\.[\w\-]+)+", t) else None


def clean_text(node):
    return re.sub(r"\s+", " ", htmlmod.unescape(node.text(separator=" ") if node else "")).strip()


def visible_text(tree):
    body = tree.body
    if body is None:
        return ""
    for s in tree.css("script,style,noscript"):
        s.decompose()
    return re.sub(r"\s+", " ", body.text(separator=" ")).strip()


# ---------------------------------------------------------------- rules extractor

PARTICLES = {"von", "van", "de", "der", "den", "di", "da", "la", "le", "du", "dos", "das", "del", "y", "zu", "e", "al", "el", "ter", "ten"}
NOT_NAME = re.compile(r"service|dienst|team|office|sekretariat|secretariat|center|centre|institut|\blab\b|group|"
                      r"director|abteilung|department|direktion|verwaltung|administration|support|infrastructure|"
                      r"facility|contact|kontakt|reception|empfang|platform|unit\b|bern\b", re.I)


GENERIC_BC = {"research", "forschung", "recherche", "group members", "team", "staff", "people", "personen",
              "publications", "teaching", "job vacancies", "facilities", "home", "portal", "about us", "über uns"}
FORMER_RX = re.compile(r"former|alumni|ehemalige|anciens|past members", re.I)
EXTERNAL_HEAD_RX = re.compile(r"partner|collaborat|kooperation|advisory|beirat|board|extern|external|"
                              r"guest|gäste|visiting|clinical (partners|collaborators)", re.I)


def name_like(name):
    """A person's name, not a role or an inbox: 2-6 tokens, capitalised, no org words."""
    n = V.strip_titles(name)
    if not n or NOT_NAME.search(n) or re.search(r"[\d@/|()]", n):
        return False
    toks = [t for t in re.split(r"\s+", n) if t]
    if not 2 <= len(toks) <= 6:
        return False
    return all(t[0].isupper() or t.lower() in PARTICLES or t[0] in "'’" for t in toks)

def rules_extract(page, html):
    """Structured facts from the page markup. Returns (facts, unit_names, subject_name, visible)."""
    facts, unit_names = [], {}
    subject = None
    if not html:
        return facts, unit_names, subject, ""
    tree = HTMLParser(html)
    uid, uname, parent = page_unit(page)
    base = {"page_id": page["page_id"], "url": page["url"], "source": page["source"], "lang": page.get("lang"),
            "fetched_at": page.get("fetched_at"), "method": "rules"}

    # unibe CMS person detail page (ARTORG and all unibe sites): schema.org Person microdata
    det = tree.css_first("article.team-detail")
    if det:
        n = det.css_first('[itemprop="name"]')
        jt = det.css_first('[itemprop="jobTitle"]')
        name, func = clean_text(n), clean_text(jt)
        if name:
            em = det.css_first('[itemprop="email"]')
            tel = det.css_first('[itemprop="telephone"]')
            ps = [clean_text(p) for p in det.css("p") if not p.attributes.get("itemprop")]
            org_line = next((p for p in ps if "," in p or "Bern" in p), None)
            if org_line and "," in org_line and re.match(r"(University of Bern|Universit[äa]t Bern|Universit[ée] de Berne)", org_line) \
                    and uid != f"unit:{page['source']}:root":
                unit_names[uid] = org_line.split(",")[-1].strip()
            phone = clean_text(tel) if tel else None
            facts.append({**base, "kind": "person", "relation": "subject", "name": name, "function": func or None,
                          "unit_id": uid, "email": decode_email(em) if em else None,
                          "phone": phone if phone and PHONE_RX.search(phone) else None,
                          "profile_url": page["url"],
                          "quote": " ".join(x for x in (name, func, org_line) if x)})
            subject = name

    # unibe CMS listing: team boxes grouped under h2 headings
    heading = ""
    for box in tree.css("h2, h3, article.team-box"):
        if box.tag in ("h2", "h3"):
            if not box.css_first("a[href^='#']"):
                heading = clean_text(box)
            continue
        a = box.css_first("dl.left dd a") or box.css_first('[itemprop="name"]')
        name = clean_text(a)
        if not name or "@" in name:
            continue
        jt = box.css_first("dd.jobTitle") or box.css_first('[itemprop="jobTitle"]')
        em = box.css_first('[itemprop="email"]') or box.css_first("dd.mail a")
        tel = box.css_first('[itemprop="telephone"]')
        href = (a.attributes.get("href") or "") if a and a.tag == "a" else ""
        func = clean_text(jt) or None
        phone = clean_text(tel) if tel else None
        rel = "former" if FORMER_RX.search(heading) else "external" if EXTERNAL_HEAD_RX.search(heading) else "member"
        facts.append({**base, "kind": "person", "relation": rel, "section": heading or None, "name": name,
                      "function": func, "unit_id": uid, "email": decode_email(em) if em else None,
                      "phone": phone if phone and PHONE_RX.search(phone) else None,
                      "profile_url": href if "/persons/" in href or "/personen/" in href else None,
                      "quote": " ".join(x for x in (name, func) if x)})

    # DCB WordPress team grid: div.dmach-grid-item.team-<group>, p = function, h2 = name, mailto
    if page["source"] == "dcb":
        for item in tree.css("div.dmach-grid-item"):
            cls = item.attributes.get("class") or ""
            m = re.search(r"\bteam-([a-z0-9\-]+)", cls)
            h = item.css_first("h2") or item.css_first("h3")
            name = clean_text(h)
            if not m or not name:
                continue
            grp = m.group(1)
            guid = f"unit:dcb:{grp}"
            unit_names.setdefault(guid, grp.removeprefix("associated-research-").replace("-", " ").title().replace("Dcb", "DCB"))
            ps = [clean_text(p) for p in item.css("p")]
            func = next((p for p in ps if p and p.lower() not in ("mail", "e-mail") and "@" not in p
                         and not p.lower().startswith("for more")), None)
            mail = next((decode_email(x) for x in item.css('a[href^="mailto:"]')), None)
            facts.append({**base, "kind": "person", "relation": "member", "name": name, "function": func,
                          "unit_id": guid, "unit_parent": "unit:dcb:root", "email": mail, "phone": None,
                          "profile_url": None, "quote": " ".join(x for x in (func, name) if x)})

    # ARTORG style "Affiliated research groups" sections: "<Group>, Prof. Dr. <Name>" one per line
    section = None
    for line in (page.get("text") or "").splitlines():
        line = line.strip()
        if line.startswith("#"):
            hd = line.lstrip("# ").lower()
            section = "affiliated" if re.search(r"affiliated|assoziiert|associ", hd) else \
                "former" if FORMER_RX.search(hd) else None
            continue
        m = re.match(r"^(.{3,90}?)(?:,| - | – )\s*((?:Prof\.|PD|Dr\.)[^,]{3,80})$", line)
        if section and m and name_like(m.group(2)):
            grp, nm = m.group(1).strip(), m.group(2).strip()
            if section == "former":
                facts.append({**base, "kind": "person", "relation": "former", "section": "former research groups",
                              "name": nm, "quote": line})
                continue
            guid = f"unit:{page['source']}:affiliated-{slug(grp)[:50]}"
            unit_names[guid] = grp
            facts.append({**base, "kind": "person", "relation": "member", "name": nm, "function": "Group Head",
                          "unit_id": guid, "unit_parent": f"unit:{page['source']}:root", "email": None,
                          "phone": None, "profile_url": None, "quote": line})

    # unit name from breadcrumbs aligned to URL directories from the end (".../aihn/group_members/x/" ->
    # the breadcrumb at the "aihn" position), never from a sub page title
    if not uid.endswith(":root"):
        seg = uid.split(":")[2]
        dirs = [d for d in urlparse(page["url"]).path.split("/") if d and not re.match(r"index(_\w+)?\.html?$", d)]
        bcs = page.get("breadcrumbs") or []
        idx = [i for i, d in enumerate(dirs) if slug(d) == seg]
        if idx and bcs:
            j = len(bcs) - len(dirs) + idx[0]
            if 0 <= j < len(bcs) and 2 <= len(bcs[j]) <= 80 and bcs[j].lower() not in GENERIC_BC:
                unit_names["bc:" + uid] = bcs[j]
    # unit name from the page heading, only on the unit's own index page (e.g. /research/aihn/index_eng.html)
    if uid not in unit_names and not uid.endswith(":root"):
        h1 = clean_text(tree.css_first("h1"))
        seg = uid.split(":")[2]
        dirs = [d for d in urlparse(page["url"]).path.split("/") if d and not d.startswith("index_")]
        if h1 and dirs and slug(dirs[-1]) == seg and len(h1) <= 80:
            unit_names["h1:" + uid] = h1
    facts = [f for f in facts if f["kind"] != "person" or name_like(f["name"])]
    return facts, unit_names, subject, visible_text(tree)


# ---------------------------------------------------------------- LLM extractor

def _q(desc="One whole sentence (10 or more words) copied word for word from the page that states this fact"):
    return {"type": "string", "description": desc}


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "people": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Full name as written, with first and last name"},
            "function": {"type": "string", "description": "Role as written, e.g. Group Head, PhD student"},
            "unit": {"type": "string", "description": "Group, lab or institute they belong to, as written"},
            "email": {"type": "string"},
            "relation": {"type": "string", "enum": ["member", "head", "external", "author", "mentioned"],
                         "description": "member/head only if the page says they work in this unit; external if "
                                        "they are from another institution or a collaborator; author for "
                                        "publication authors; mentioned otherwise"},
            "quote": _q()}, "required": ["name", "relation", "quote"]}},
        "expertise": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string", "description": "Research topic, method, disease or technology, 1-4 words "
                                                       "that appear literally in the quote"},
            "holder": {"type": "string", "description": "Person or unit name that has this expertise"},
            "quote": _q()}, "required": ["label", "quote"]}},
        "projects": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string", "description": "One sentence, from the page"},
            "start": {"type": "string", "description": "Year or date if stated"},
            "end": {"type": "string", "description": "Year or date if stated"},
            "status": {"type": "string", "enum": ["recruiting", "ongoing", "completed"]},
            "quote": _q()}, "required": ["title", "quote"]}},
        "infrastructure": {"type": "array", "description":
            "Only facilities, platforms or services that people outside the group can use: core facilities, "
            "imaging or sequencing platforms, clinical trial units, biobanks, testing services, labs offered to "
            "partners. NOT devices, sensors, apps, materials, methods or tools used in a study (those belong to "
            "the project).", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": ["facility", "service", "platform"]},
            "quote": _q()}, "required": ["name", "quote"]}},
    },
    "required": ["people", "expertise", "projects", "infrastructure"],
}

SYSTEM = (
    "You extract facts from ONE web page of a Bern research institution. Call record_facts once.\n"
    "Every item MUST include \"quote\": one complete sentence of at least 10 words copied exactly, word for "
    "word, from the page, that contains the item's name and value. Never quote a single word or heading. "
    "If the page does not state something, omit it. Never guess, never use outside knowledge.\n"
    "people: only people the page names. Set relation=member or head ONLY if the page says they work in the "
    "group or institute. People from other institutions, collaborators, guest speakers and publication "
    "authors are external or author.\n"
    "expertise: research topics, methods, diseases, technologies the group or person works on (max 12); "
    "the label must be words that appear in its quote.\n"
    "projects: named projects, studies or trials (devices and tools used in a study are part of the project). "
    "infrastructure: only facilities, platforms or services that outsiders can use (core facility, imaging "
    "platform, clinical trial unit, biobank, testing service); never a device, sensor, app or method."
)

RECORD = {"type": "function", "function": {"name": "record_facts",
                                           "description": "Save facts found on the page, each with an exact quote",
                                           "parameters": EXTRACTION_SCHEMA}}


def llm_messages(page):
    head = (f"Source: {page['source']}\nURL: {page['url']}\nTitle: {page.get('title') or ''}\n"
            f"Breadcrumbs: {' > '.join(page.get('breadcrumbs') or [])}\n\nPAGE TEXT:\n")
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": head + (page.get("text") or "")[:MAX_CHARS]}]


def salvage_json(s):
    """Parse a tool call cut off by max_tokens (the 8B sometimes loops on whitespace): keep complete items."""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        pass
    s = (s or "").rstrip()
    cuts = [i for i, ch in enumerate(s) if ch == "}"][::-1][:200]
    for i in cuts:
        head, stack, in_str, esc = s[:i + 1], [], False, False
        for ch in head:
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "[{":
                stack.append("]" if ch == "[" else "}")
            elif ch in "]}" and stack:
                stack.pop()
        if in_str:
            continue
        try:
            return json.loads(head + "".join(reversed(stack)))
        except json.JSONDecodeError:
            continue
    return {}


def default_llm(page):
    from common import llm
    r = llm.chat(llm.SMALL, llm_messages(page), tools=[RECORD], tool_choice=llm.force("record_facts"),
                 temperature=0, max_tokens=1200, tag="extract")
    args = r["tool_calls"][0]["arguments"] if r.get("tool_calls") else {}
    if isinstance(args, str):
        args = salvage_json(args)
    return (args if isinstance(args, dict) else {}), r.get("cached", False), r.get("usage", {})


def llm_facts(page, args):
    base = {"page_id": page["page_id"], "url": page["url"], "source": page["source"], "lang": page.get("lang"),
            "fetched_at": page.get("fetched_at"), "method": "llm"}
    out = []
    for kind, key in (("person", "people"), ("expertise", "expertise"), ("project", "projects"),
                      ("infrastructure", "infrastructure")):
        for it in args.get(key) or []:
            if isinstance(it, dict):
                vals = {("infra_kind" if k == "kind" else k): v for k, v in it.items()
                        if isinstance(v, (str, type(None))) and k not in base}
                out.append({**base, **vals, "kind": kind})
    return out


# ---------------------------------------------------------------- per page

def wants_llm(page):
    if page.get("page_type") in SKIP_TYPES or SKIP_URL.search(page["url"]) or PUB_URL.search(page["url"]):
        return False
    return len(page.get("text") or "") >= 200


def is_pub_or_news(page):
    return page.get("page_type") in SKIP_TYPES or bool(PUB_URL.search(page["url"]))


def process(page, llm=None, use_llm=True):
    """Extract and verify one page. Returns the page record for facts.jsonl plus counters."""
    html = raw_html(page)
    rfacts, unit_names, subject, vis = rules_extract(page, html)
    uid, uname, parent = page_unit(page)
    kept, dropped, mentions = [], Counter(), []
    pubnews = is_pub_or_news(page)
    for f in rfacts:
        if pubnews:                                    # gap 3: no members from publications or news
            mentions.append({"name": f["name"], "quote": f["quote"], "context": page.get("page_type") or "news"})
            dropped["rules_on_pub_or_news"] += 1
            continue
        if f.get("relation") in ("former", "external"):
            mentions.append({"name": f["name"], "quote": f["quote"], "context": f"{f['relation']}: {f.get('section')}"})
            continue
        g, why = V.check_rules_fact(f, vis or page.get("text") or "")
        if g:
            kept.append(g)
        else:
            dropped[why] += 1
    cached, usage, n_llm = None, {}, 0
    if use_llm and llm is not None and wants_llm(page):
        args, cached, usage = llm(page)
        n_llm = 1
        text = page.get("text") or ""
        rule_names = {V.fold(V.strip_titles(f["name"])) for f in rfacts}
        for f in llm_facts(page, args):
            if f["kind"] == "person" and V.fold(V.strip_titles(f.get("name") or "")) in rule_names:
                continue                               # rules already have this person with structure
            g, why, men = V.check_llm_fact(f, text, page.get("page_type"))
            if men:
                mentions.append(men)
            if g:
                kept.append(g)
            else:
                dropped[why] += 1
    rec = {"page_id": page["page_id"], "url": page["url"], "source": page["source"],
           "group": page.get("group"), "lang": page.get("lang"), "page_type": page.get("page_type"),
           "sibling_group": page.get("sibling_group"), "fetched_at": page.get("fetched_at"),
           "title": page.get("title"), "unit_id": uid, "unit_name": uname, "unit_parent": parent,
           "unit_names": unit_names, "subject": subject, "facts": kept, "mentions": mentions,
           "dropped": dict(dropped), "extract_version": EXTRACT_VERSION}
    return rec, n_llm, cached, usage


def representative(pages):
    """One page per sibling_group gets the LLM call (prefer en, then de, then fr), others get rules only."""
    groups = defaultdict(list)
    for p in pages:
        groups[p.get("sibling_group") or p["page_id"]].append(p)
    rank = {"en": 0, "de": 1, "fr": 2}
    reps = set()
    for g, ps in groups.items():
        ps = sorted(ps, key=lambda p: (rank.get(p.get("lang"), 3), -len(p.get("text") or "")))
        reps.add(ps[0]["page_id"])
    return reps


def load_pages(path=PAGES, sources=None):
    pages = {}
    for line in Path(path).open():
        line = line.strip()
        if not line:
            continue
        try:
            p = json.loads(line)
        except json.JSONDecodeError:
            continue                                   # a half written last line while A is writing
        if sources and p.get("source") not in sources and p.get("group") not in sources:
            continue
        pages[p["page_id"]] = p
    return list(pages.values())


def run(pages, llm=default_llm, use_llm=True, workers=WORKERS, limit=None, out=OUT, stats_path=STATS, keep=None):
    """keep: records from an earlier run (other sources) to carry over into facts.jsonl."""
    t0 = time.time()
    reps = representative(pages)
    llm_pages = [p for p in pages if p["page_id"] in reps and wants_llm(p)]
    if limit is not None:
        allowed = {p["page_id"] for p in llm_pages[:limit]}
    else:
        allowed = {p["page_id"] for p in llm_pages}
    records, totals = [], Counter()
    by_source = defaultdict(Counter)
    tokens = Counter()

    def job(p):
        return process(p, llm=llm, use_llm=use_llm and p["page_id"] in allowed)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(job, p): p for p in pages}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                rec, n_llm, cached, usage = fut.result()
            except Exception as e:                     # one bad page never kills the run
                totals["page_errors"] += 1
                print(f"  ! {p['url']}: {type(e).__name__}: {e}", flush=True)
                continue
            records.append(rec)
            totals["llm_calls"] += n_llm
            if n_llm:
                totals["llm_cached" if cached else "llm_new"] += 1
                tokens["in"] += usage.get("in") or 0
                tokens["out"] += usage.get("out") or 0
            nd = sum(rec["dropped"].values())
            totals["facts_kept"] += len(rec["facts"])
            totals["facts_dropped"] += nd
            totals["mentions"] += len(rec["mentions"])
            for k, v in rec["dropped"].items():
                totals[f"dropped:{k}"] += v
            s = by_source[rec["source"]]
            s["pages"] += 1
            s["facts_kept"] += len(rec["facts"])
            s["facts_dropped"] += nd
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(pages)} pages, {totals['llm_new']} new LLM calls, "
                      f"{totals['llm_cached']} cached, {totals['facts_dropped']} dropped", flush=True)

    records += [r for r in (keep or [])]
    records.sort(key=lambda r: r["page_id"])
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with tmp.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, out)
    stats = {"run_at": now(), "secs": round(time.time() - t0, 1), "pages": len(pages),
             "llm_pages_planned": len(allowed), **dict(totals), "tokens_new": dict(tokens),
             "facts_dropped_all_sources": sum(sum(r["dropped"].values()) for r in records),
             "facts_kept_all_sources": sum(len(r["facts"]) for r in records),
             "by_source": {k: dict(v) for k, v in sorted(by_source.items())}}
    if stats_path:
        sp = Path(stats_path)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(stats, indent=2))
    return records, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", default=str(PAGES))
    ap.add_argument("--sources", nargs="*")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    pages = load_pages(a.pages, set(a.sources) if a.sources else None)
    print(f"{len(pages)} pages loaded", flush=True)
    keep = []
    if a.sources and Path(a.out).exists():             # incremental: keep the other sources' records
        sel = set(a.sources)
        keep = [r for r in (json.loads(l) for l in Path(a.out).open() if l.strip())
                if r["source"] not in sel and r.get("group") not in sel]
    _, stats = run(pages, use_llm=not a.no_llm, workers=a.workers, limit=a.limit, out=a.out, keep=keep)
    print(json.dumps({k: v for k, v in stats.items() if k != "by_source"}, indent=1))
    print(json.dumps(stats["by_source"]))


if __name__ == "__main__":
    main()
