"""Entity resolution and the kg.sqlite build (schema in ingest/SCHEMA.md).

    python -m ingest.resolve                 # data/extract/facts.jsonl -> data/kg.sqlite (atomic)

Merge rules (playbook gap 12), nothing else merges automatically:
  email            same personal email (generic inboxes like info@ never merge) and similar names
  unit+name        same unit and near exact name (token sort ratio >= 95, titles stripped, accents folded)
  profile_url      a listing links to the person's own profile page
  name+email_local exact same full name and the same email local part on two domains (ARTORG + DCB)
Anything else that looks alike (same full name, different unit) goes to possible_same for review.
"""
import argparse, hashlib, json, os, re, sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from ingest import verify as V

FACTS = Path("data/extract/facts.jsonl")
KG = Path("data/kg.sqlite")
STATS = Path("logs/extract_stats.json")

SENIORITY = {"head": 1.0, "professor": 1.0, "senior": 0.8, "postdoc": 0.6, "staff": 0.5, "phd": 0.4,
             "student": 0.2, "other": 0.5, "collaborator": 0.5, "emeritus": 0.5}
EMERITUS_RX = re.compile(r"\bem(er)?\.|emerit", re.I)
_FUNC_RX = [
    ("postdoc", r"post-?doc|postdoctoral"),
    ("phd", r"\bph\.?\s?d\b|doctoral|doktorand|doctorant|graduate student"),
    ("student", r"\b(master|bachelor|msc|bsc)\b|student|\bintern\b|praktikant|stagiaire|hilfsassistent"),
    ("head", r"\bhead\b|leiter|leiterin|leitung|director|direktor|\bceo\b|\bcto\b|\bcfo\b|\bchair\b|"
             r"principal investigator|\bpi\b|responsable|\bchef|pr[äa]sident|president|founder|gr[üu]nder"),
    ("professor", r"professor|\bprof\b"),
    ("senior", r"senior|privatdozent|\bpd\b|lecturer|dozent|group leader|gruppenleiter|research scientist|"
               r"oberassistent|project leader|projektleiter|scientist|wissenschaftler|ma[iî]tre|charg[ée]|physician|\barzt|[äa]rztin"),
    ("postdoc", r"research associate|research fellow|wissenschaftliche|collaborat(eur|rice) scientifique"),
    ("staff", r"engineer|ingenieur|manager|coordinator|koordinator|statistician|data scientist|technician|"
              r"laborant|software|developer|admin|secretar|sekretar|assistant|assistent|officer|specialist|\blab"),
]
GENERIC_MAIL = re.compile(r"^(info|office|sekretariat|secretariat|secretary|contact|admin|mail|kontakt|empfang|"
                          r"reception|team|hello|media|kommunikation|communication|webmaster|direktion|dekanat)\b", re.I)
TITLE_PREFIX = re.compile(r"^((?:prof|dr|pd|med|phil|nat|rer|sc|techn|dent|vet|em|emer|h\.?\s?c|pharm|habil|"
                          r"msc|bsc|dipl|ing|mag|lic|iur|biol|chem)\.?[\s,]+)+", re.I)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def function_class(text):
    t = V.fold(text or "")
    if not t:
        return "other", SENIORITY["other"]
    for cls, rx in _FUNC_RX:
        if re.search(rx, t):
            return cls, SENIORITY[cls]
    return "other", SENIORITY["other"]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", V.fold(s)).strip("-")


def h6(s):
    return hashlib.sha1(s.encode()).hexdigest()[:6]


def name_norm(name):
    return " ".join(V.name_tokens(name))


def split_titles(name):
    m = TITLE_PREFIX.match(name.strip())
    titles = [t for t in re.split(r"[\s,]+", m.group(0)) if t] if m else []
    return titles, V.strip_titles(name)


def spelling_variant(a, b):
    """Same given name(s), surname within edit distance 1 (2 for surnames of 8+ letters): 'Kokona' / 'Kokana'."""
    ta, tb = a.split(), b.split()
    if a == b or len(ta) != len(tb) or len(ta) < 2 or ta[:-1] != tb[:-1]:
        return False
    la, lb = ta[-1], tb[-1]
    return Levenshtein.distance(la, lb) <= (2 if min(len(la), len(lb)) >= 8 else 1)


def person_id(name, email=None, unit_id=None):
    n = name_norm(name)
    key = email.lower() if email and not GENERIC_MAIL.match(email) else f"{n}|{unit_id}"
    return f"person:{slug(n)[:40]}-{h6(key)}"


def resolve_id(conn, entity_id):
    seen = set()
    while entity_id not in seen:
        seen.add(entity_id)
        row = conn.execute("SELECT kept_id FROM merge_log WHERE merged_id=?", (entity_id,)).fetchone()
        if not row:
            return entity_id
        entity_id = row[0]
    return entity_id


class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)
            return True
        return False


DDL = """
CREATE TABLE person(id TEXT PRIMARY KEY, name TEXT, name_norm TEXT, titles TEXT, emails TEXT, phones TEXT,
  orcid TEXT, profile_url TEXT, sources TEXT, seniority REAL, primary_unit_id TEXT, primary_function TEXT);
CREATE TABLE unit(id TEXT PRIMARY KEY, name TEXT, type TEXT, parent_id TEXT, source TEXT, url TEXT);
CREATE TABLE affiliation(person_id TEXT, unit_id TEXT, function TEXT, function_class TEXT, seniority REAL,
  source TEXT, url TEXT, page_id TEXT, PRIMARY KEY(person_id, unit_id, function));
CREATE TABLE expertise(id TEXT PRIMARY KEY, label TEXT, mesh_id TEXT, synonyms TEXT);
CREATE TABLE entity_expertise(entity_id TEXT, expertise_id TEXT, PRIMARY KEY(entity_id, expertise_id));
CREATE TABLE project(id TEXT PRIMARY KEY, title TEXT, summary TEXT, unit_id TEXT, start TEXT, "end" TEXT,
  status TEXT, url TEXT, source TEXT);
CREATE TABLE infrastructure(id TEXT PRIMARY KEY, name TEXT, kind TEXT, unit_id TEXT, description TEXT,
  url TEXT, source TEXT);
CREATE TABLE evidence(id INTEGER PRIMARY KEY, entity_id TEXT, field TEXT, value TEXT, quote TEXT, url TEXT,
  page_id TEXT, verified INTEGER, fetched_at TEXT, lang TEXT, method TEXT, source TEXT);
CREATE TABLE mention(name TEXT, page_id TEXT, url TEXT, quote TEXT, context TEXT);
CREATE TABLE page_entity(page_id TEXT, entity_id TEXT, role TEXT, PRIMARY KEY(page_id, entity_id, role));
CREATE TABLE merge_log(kept_id TEXT, merged_id TEXT, rule TEXT, at TEXT);
CREATE TABLE possible_same(a_id TEXT, b_id TEXT, reason TEXT, score REAL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX ev_entity ON evidence(entity_id);
CREATE INDEX ev_page ON evidence(page_id);
CREATE INDEX aff_unit ON affiliation(unit_id);
CREATE INDEX pe_entity ON page_entity(entity_id);
"""


def unit_type(uid, name):
    src, code = uid.split(":")[1], uid.split(":")[2]
    n = (name or "").lower()
    if code == "root":
        if src in ("artorg", "dcb"):
            return "centre"
        if any(w in n for w in ("platform", "imaging center", "sequencing")):
            return "platform"
        if "faculty" in n or "faculté" in n or "fakultät" in n:
            return "faculty"
        return "institute"
    if src == "dcb" and "lab" not in n:
        return "team"
    return "lab" if "lab" in n or "lab" in code else "group"


def load_records(path=FACTS):
    recs = []
    for line in Path(path).open():
        if line.strip():
            recs.append(json.loads(line))
    return recs


def build(records, out=KG, stats=None):
    out = Path(out)
    tmp = out.with_suffix(".sqlite.tmp")
    if tmp.exists():
        tmp.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(DDL)
    at = now()
    ev_rows = []

    def ev(entity_id, field, value, quote, r, method=None):
        ev_rows.append((entity_id, field, value, quote, r["url"], r["page_id"], 1, r.get("fetched_at"),
                        r.get("lang"), method or "rules", r["source"]))

    # ------------------------------------------------ units
    units = {}
    unit_ev = {}
    for r in records:
        cands = [(r["unit_id"], r.get("unit_name"), r.get("unit_parent"))]
        cands += [(u, n, r.get("unit_parent") if not u.startswith("unit:dcb:") else "unit:dcb:root")
                  for u, n in (r.get("unit_names") or {}).items() if not u.startswith(("h1:", "bc:"))]
        for f in r["facts"]:
            if f.get("unit_id"):
                cands.append((f["unit_id"], None, f.get("unit_parent") or (r["unit_parent"] if f["unit_id"] == r["unit_id"] else f"unit:{r['source']}:root")))
        for uid, name, parent in cands:
            u = units.setdefault(uid, {"id": uid, "name": None, "parent_id": None, "source": r["source"], "url": None})
            if name and not u["name"]:
                u["name"] = name
                unit_ev[uid] = (name, r)
            if parent and not u["parent_id"] and parent != uid:
                u["parent_id"] = parent
            if uid == r["unit_id"] and (u["url"] is None or len(r["url"]) < len(u["url"])):
                u["url"] = r["url"]
    # unit names, best first: breadcrumb (majority vote over all pages of the unit), then the
    # "University of Bern, <centre>, <group>" line of person pages / DCB grid label, then the h1 of the
    # unit's own index page. Never a sub page title.
    votes = defaultdict(Counter)
    vote_rec = {}
    for r in records:
        for u, n in (r.get("unit_names") or {}).items():
            if u.startswith("bc:"):
                votes[u[3:]][n] += 1
                vote_rec.setdefault((u[3:], n), r)
    for uid, cnt in votes.items():
        if uid in units:
            n = cnt.most_common(1)[0][0]
            units[uid]["name"] = n
            unit_ev[uid] = (n, vote_rec[(uid, n)])
    for r in records:
        for u, n in (r.get("unit_names") or {}).items():
            if u.startswith("h1:") and u[3:] in units and not units[u[3:]]["name"]:
                units[u[3:]]["name"] = n
                unit_ev[u[3:]] = (n, r)
    for uid, u in list(units.items()):
        if u["parent_id"] and u["parent_id"] not in units:
            src = u["parent_id"].split(":")[1]
            units[u["parent_id"]] = {"id": u["parent_id"], "name": None, "parent_id": None, "source": src, "url": None}
    for uid, u in units.items():
        if not u["name"]:
            code = uid.split(":")[2]
            u["name"] = code.upper() if uid.startswith("unit:artorg:") and len(code) <= 6 else code.replace("_", " ").replace("-", " ").title()
        u["type"] = unit_type(uid, u["name"])
        conn.execute("INSERT INTO unit VALUES(?,?,?,?,?,?)", (uid, u["name"], u["type"], u["parent_id"], u["source"], u["url"]))
        if uid in unit_ev:
            name, r = unit_ev[uid]
            ev(uid, "name", name, name, r)
        elif u["url"]:
            ev_rows.append((uid, "name", u["name"], None, u["url"], None, 1, None, None, "url_structure", u["source"]))

    # ------------------------------------------------ person candidates
    cands = []
    for r in records:
        for f in r["facts"]:
            if f["kind"] == "person":
                c = dict(f)
                c["unit_id"] = f.get("unit_id") or r["unit_id"]
                c["nn"] = name_norm(f["name"])
                if len(c["nn"].split()) < 2:
                    continue
                c["email"] = (f.get("email") or "").lower() or None
                c["pid"] = person_id(f["name"], c["email"], c["unit_id"])
                c["rec"] = r
                cands.append(c)

    uf = UF()
    rule_for = {}
    for c in cands:
        uf.find(c["pid"])

    def join(a, b, rule):
        if a != b and uf.union(a, b):
            rule_for[(min(a, b), max(a, b))] = rule

    # the same name in the same unit with and without email: the no-email key joins the email key
    by_unit_name = defaultdict(list)
    for c in cands:
        by_unit_name[c["unit_id"]].append(c)
    for uid, cs in by_unit_name.items():
        for i, a in enumerate(cs):
            for b in cs[i + 1:]:
                if a["pid"] != b["pid"] and fuzz.token_sort_ratio(a["nn"], b["nn"]) >= 95:
                    if a["email"] and b["email"] and a["email"] != b["email"] and a["nn"] != b["nn"]:
                        continue
                    join(a["pid"], b["pid"], "unit+name")
    by_email = defaultdict(list)
    for c in cands:
        if c["email"] and not GENERIC_MAIL.match(c["email"]):
            by_email[c["email"]].append(c)
    possible = []
    for e, cs in by_email.items():
        for b in cs[1:]:
            a = cs[0]
            if fuzz.token_set_ratio(a["nn"], b["nn"]) >= 80:
                join(a["pid"], b["pid"], "email")
            else:
                possible.append((a["pid"], b["pid"], f"same email {e.split('@')[0]}@..., different names", 0.5))
    by_profile = defaultdict(list)
    for c in cands:
        if c.get("profile_url"):
            by_profile[re.sub(r"index_(eng|ger|fra)\.html$", "", c["profile_url"])].append(c)
    for k, cs in by_profile.items():
        for b in cs[1:]:
            if fuzz.token_sort_ratio(cs[0]["nn"], b["nn"]) >= 90:
                join(cs[0]["pid"], b["pid"], "profile_url")
    by_name = defaultdict(list)
    for c in cands:
        by_name[c["nn"]].append(c)
    for n, cs in by_name.items():
        for i, a in enumerate(cs):
            for b in cs[i + 1:]:
                if uf.find(a["pid"]) == uf.find(b["pid"]):
                    continue
                la = a["email"].split("@")[0] if a["email"] else None
                lb = b["email"].split("@")[0] if b["email"] else None
                if la and la == lb and len(n.split()) >= 2:
                    join(a["pid"], b["pid"], "name+email_local")
                elif a["source"] != b["source"] and not (a["email"] and b["email"] and a["email"] != b["email"]) \
                        and len(n.split()) >= 2:
                    join(a["pid"], b["pid"], "exact_name_cross_source")
                else:
                    possible.append((a["pid"], b["pid"], f"same full name, units {a['unit_id']} / {b['unit_id']}", 0.7))

    # project team names (no unit function, no email) join the one same-source person with that exact name
    for c in cands:
        if c.get("relation") != "project_team":
            continue
        others = {uf.find(o["pid"]) for o in by_name[c["nn"]]
                  if o["source"] == c["source"] and o.get("relation") != "project_team"}
        if len(others) == 1:
            join(c["pid"], others.pop(), "project_team+name")

    # spelling variants (playbook data traps): same given name, surname edit distance <= 1 (2 if long).
    # Same unit -> merge; a project team name -> the one same-source member with that variant; else review.
    names_list = sorted(by_name)
    for i, na in enumerate(names_list):
        for nb in names_list[i + 1:]:
            if not spelling_variant(na, nb):
                continue
            for a in by_name[na]:
                for b in by_name[nb]:
                    if uf.find(a["pid"]) == uf.find(b["pid"]):
                        continue
                    if a["email"] and b["email"] and a["email"] != b["email"]:
                        possible.append((a["pid"], b["pid"], f"spelling variant {na} / {nb}, different emails", 0.6))
                    elif a["unit_id"] == b["unit_id"]:
                        join(a["pid"], b["pid"], "spelling_variant")
                    elif a["source"] == b["source"] and "project_team" in (a.get("relation"), b.get("relation")) \
                            and not (a.get("relation") == b.get("relation") == "project_team"):
                        join(a["pid"], b["pid"], "spelling_variant")
                    else:
                        possible.append((a["pid"], b["pid"], f"spelling variant {na} / {nb}, units "
                                         f"{a['unit_id']} / {b['unit_id']}", 0.6))

    clusters = defaultdict(list)
    for c in cands:
        clusters[uf.find(c["pid"])].append(c)

    # people listed under external/partner sections of a unit's team page are collaborators of that unit,
    # even when they also have a profile page there (gap 13)
    collab_keys = set()
    for r in records:
        for m in r.get("mentions") or []:
            if str(m.get("context") or "").startswith("external"):
                collab_keys.add((r["unit_id"], name_norm(m["name"])))
    pid_map = {}
    persons = 0
    for root, cs in clusters.items():
        ids = sorted({c["pid"] for c in cs})
        email_ids = sorted({c["pid"] for c in cs if c["email"] and not GENERIC_MAIL.match(c["email"])})
        kept = email_ids[0] if email_ids else ids[0]
        for i in ids:
            pid_map[i] = kept
            if i != kept:
                rule = next((v for (a, b), v in rule_for.items() if i in (a, b)), "cluster")
                conn.execute("INSERT INTO merge_log VALUES(?,?,?,?)", (kept, i, rule, at))
        names = Counter(split_titles(c["name"])[1] for c in cs)
        name = sorted(names.items(), key=lambda kv: (-kv[1], -any(ord(ch) > 127 for ch in kv[0]), -len(kv[0])))[0][0]
        titles = sorted({t for c in cs for t in split_titles(c["name"])[0]})
        emails = sorted({c["email"] for c in cs if c["email"]})
        phones = sorted({c["phone"] for c in cs if c.get("phone")})
        profile = next((c["profile_url"] for c in cs if c.get("relation") == "subject"), None) or \
            next((c["profile_url"] for c in cs if c.get("profile_url")), None)
        sources = sorted({c["source"] for c in cs})
        affs = {}
        for c in cs:
            func = c.get("function") or ""
            cls, w = function_class(func)
            if c.get("relation") == "project_team" or (c["unit_id"], c["nn"]) in collab_keys:
                cls, w = "collaborator", SENIORITY["collaborator"]     # not a member of this unit
            elif EMERITUS_RX.search(c["name"]) or EMERITUS_RX.search(func):
                cls, w = "emeritus", SENIORITY["emeritus"]
            key = (c["unit_id"], func)
            if key not in affs:
                affs[key] = (cls, w, c)
        if any(c.get("relation") != "project_team" for c in cs):
            affs = {k: v for k, v in affs.items() if v[2].get("relation") != "project_team"}
        # drop the empty function row when the same unit has a real function
        for (u, fn) in list(affs):
            if fn == "" and any(k[0] == u and k[1] for k in affs):
                del affs[(u, fn)]
        best = max(affs.items(), key=lambda kv: kv[1][1])
        conn.execute("INSERT INTO person VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (kept, name, name_norm(name), json.dumps(titles), json.dumps(emails), json.dumps(phones), None,
                      profile, json.dumps(sources), best[1][1], best[0][0], best[0][1] or None))
        for (u, fn), (cls, w, c) in affs.items():
            conn.execute("INSERT OR IGNORE INTO affiliation VALUES(?,?,?,?,?,?,?,?)",
                         (kept, u, fn or None, cls, w, c["source"], c["url"], c["page_id"]))
        for c in cs:
            r = c["rec"]
            ev(kept, "name", c["name"], c["quote"], r, c["method"])
            if c.get("function"):
                ev(kept, "function", c["function"], c["quote"], r, c["method"])
            ev(kept, "member_of", c["unit_id"], c["quote"], r, c["method"])
            if c["email"]:
                ev(kept, "email", c["email"], c["email"], r, c["method"])
            if c.get("phone"):
                ev(kept, "phone", c["phone"], c["phone"], r, c["method"])
            role = "subject" if c.get("relation") == "subject" else "member"
            conn.execute("INSERT OR IGNORE INTO page_entity VALUES(?,?,?)", (c["page_id"], kept, role))
        persons += 1
    seen_pairs = set()
    for a, b, reason, score in possible:
        a, b = pid_map.get(a, a), pid_map.get(b, b)
        if a != b and (min(a, b), max(a, b)) not in seen_pairs:
            seen_pairs.add((min(a, b), max(a, b)))
            conn.execute("INSERT INTO possible_same VALUES(?,?,?,?)", (a, b, reason, score))

    # ------------------------------------------------ expertise, projects, infrastructure
    subj_by_page = {}
    names_by_page = defaultdict(dict)
    for c in cands:
        if c.get("relation") == "subject":
            subj_by_page[c["page_id"]] = pid_map[c["pid"]]
        names_by_page[c["page_id"]][c["nn"]] = pid_map[c["pid"]]
    # sibling pages share their subject (the LLM ran on one language version)
    sib_subject = {}
    for r in records:
        if r.get("sibling_group") and r["page_id"] in subj_by_page:
            sib_subject[r["sibling_group"]] = subj_by_page[r["page_id"]]
    n_exp = n_proj = n_inf = 0
    exp_ids = set()
    for r in records:
        conn.execute("INSERT OR IGNORE INTO page_entity VALUES(?,?,?)", (r["page_id"], r["unit_id"], "unit_page"))
        subject = subj_by_page.get(r["page_id"]) or sib_subject.get(r.get("sibling_group"))
        for f in r["facts"]:
            k = f["kind"]
            if k == "expertise":
                label = re.sub(r"\s+", " ", f["label"]).strip()
                if not label or len(label) > 80:
                    continue
                eid = f"expertise:{slug(label)[:60]}"
                holder = subject or r["unit_id"]
                h = V.fold(V.strip_titles(f.get("holder") or ""))
                if h and " ".join(V.name_tokens(h)) in names_by_page[r["page_id"]] and V.contains_name(f["quote"], f["holder"]):
                    holder = names_by_page[r["page_id"]][" ".join(V.name_tokens(h))]
                if eid not in exp_ids:
                    conn.execute("INSERT INTO expertise VALUES(?,?,?,?)", (eid, label, None, "[]"))
                    exp_ids.add(eid)
                    n_exp += 1
                conn.execute("INSERT OR IGNORE INTO entity_expertise VALUES(?,?)", (holder, eid))
                ev(holder, "expertise", label, f["quote"], r, "llm")
            elif k == "project":
                pid = f"project:{r['source']}:{slug(f['title'])[:40]}-{h6(V.fold(f['title']))}"
                if conn.execute("SELECT 1 FROM project WHERE id=?", (pid,)).fetchone() is None:
                    conn.execute("INSERT INTO project VALUES(?,?,?,?,?,?,?,?,?)",
                                 (pid, f["title"], f.get("summary"), r["unit_id"], f.get("start"), f.get("end"),
                                  f.get("status"), r["url"], r["source"]))
                    n_proj += 1
                ev(pid, "title", f["title"], f["quote"], r, "llm")
                for fld in ("status", "start", "end"):
                    if f.get(fld):
                        ev(pid, fld, f[fld], f.get(fld + "_quote") or f["quote"], r, "llm")
                        conn.execute(f'UPDATE project SET "{fld}"=coalesce("{fld}", ?) WHERE id=?', (f[fld], pid))
                conn.execute("INSERT OR IGNORE INTO page_entity VALUES(?,?,?)", (r["page_id"], pid, "subject"))
            elif k == "infrastructure" and f.get("method") == "rules":
                # service pages: one entity per sibling group (DE/EN/FR), English name and URL preferred
                iid = f"infra:{r['source']}:{h6(f.get('key') or r['page_id'])}"
                row = conn.execute("SELECT 1 FROM infrastructure WHERE id=?", (iid,)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO infrastructure VALUES(?,?,?,?,?,?,?)",
                                 (iid, f["name"], "service", f"unit:{r['source']}:root", f.get("description"),
                                  r["url"], r["source"]))
                    n_inf += 1
                elif r.get("lang") == "en":
                    conn.execute("UPDATE infrastructure SET name=?, url=?, description=coalesce(?, description) "
                                 "WHERE id=?", (f["name"], r["url"], f.get("description"), iid))
                ev(iid, "name", f["name"], f["quote"], r, "rules")
                if f.get("description"):
                    ev(iid, "description", f["description"][:200], f["description"], r, "rules")
                conn.execute("INSERT OR IGNORE INTO page_entity VALUES(?,?,?)", (r["page_id"], iid, "subject"))
            elif k == "infrastructure":
                iid = f"infra:{r['source']}:{slug(f['name'])[:40]}-{h6(V.fold(f['name']))}"
                if conn.execute("SELECT 1 FROM infrastructure WHERE id=?", (iid,)).fetchone() is None:
                    kind = f.get("infra_kind") if f.get("infra_kind") in ("facility", "service", "platform") else "facility"
                    conn.execute("INSERT INTO infrastructure VALUES(?,?,?,?,?,?,?)",
                                 (iid, f["name"], kind, r["unit_id"], f["quote"], r["url"], r["source"]))
                    n_inf += 1
                ev(iid, "name", f["name"], f["quote"], r, "llm")
                conn.execute("INSERT OR IGNORE INTO page_entity VALUES(?,?,?)", (r["page_id"], iid, "subject"))
        page_projects = [f"project:{r['source']}:{slug(f['title'])[:40]}-{h6(V.fold(f['title']))}"
                         for f in r["facts"] if f["kind"] == "project"]
        for f in r["facts"]:
            if f["kind"] == "device" and page_projects:    # study devices are project evidence, not infrastructure
                ev(page_projects[0], "device", f["name"], f["quote"], r, "llm")
        for m in r.get("mentions") or []:
            conn.execute("INSERT INTO mention VALUES(?,?,?,?,?)", (m["name"], r["page_id"], r["url"], m["quote"], m.get("context")))

    conn.executemany("INSERT INTO evidence(entity_id, field, value, quote, url, page_id, verified, fetched_at, lang,"
                     " method, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ev_rows)
    dropped = sum(sum(r["dropped"].values()) for r in records)
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
              ("person", "unit", "affiliation", "expertise", "entity_expertise", "project", "infrastructure",
               "evidence", "mention", "merge_log", "possible_same")}
    per_source = {s: n for s, n in conn.execute(
        "SELECT a.source, count(DISTINCT a.person_id) FROM affiliation a GROUP BY a.source")}
    meta = {"built_at": at, "dropped_facts": dropped, "counts": counts, "persons_by_source": per_source,
            "pages": len(records)}
    if stats:
        meta["extract_stats"] = stats
    for k, v in meta.items():
        conn.execute("INSERT INTO meta VALUES(?,?)", (k, json.dumps(v)))
    conn.commit()
    conn.close()
    os.replace(tmp, out)
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts", default=str(FACTS))
    ap.add_argument("--out", default=str(KG))
    a = ap.parse_args()
    stats = json.loads(STATS.read_text()) if STATS.exists() else None
    meta = build(load_records(a.facts), a.out, stats)
    print(json.dumps({k: v for k, v in meta.items() if k != "extract_stats"}, indent=1))


if __name__ == "__main__":
    main()
