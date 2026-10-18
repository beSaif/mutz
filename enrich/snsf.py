"""SNSF grants for Bern, joined conservatively to people in data/kg.sqlite.

Source: SNSF Data Portal open data exports (https://data.snf.ch/datasets, also listed on
opendata.swiss as "snsf-data-portal-exports"), licensed CC BY-SA 4.0 by the Swiss National
Science Foundation. Every grant we show links back to https://data.snf.ch/grants/grant/<number>
and is labelled "from SNSF".

    grants.csv   one row per grant (title, institution, dates, amount, state)
    persons.csv  one row per SNSF person, with grant numbers per role and an optional ORCID

Both are downloaded once (`python -m enrich.snsf download`, skipped when present) into
data/enrich/raw/ (gitignored). `python -m enrich.snsf build` writes data/enrich/grants.jsonl,
one line per (entity_id, grant). `grants_for(entity_id)` reads that file.

Bern filter
-----------
A grant is a Bern grant when its ResearchInstitution is the University of Bern ("University of
Berne – BE") or UPD Bern, or its Institute text names Inselspital / Universitätsspital Bern /
Universität Bern / University of Bern. Inselspital clinics appear in the export under the
University of Bern. Only Bern grants are written. An SNSF person is a Bern candidate when they
hold any role on a Bern grant, or their own institution is Bern by the same rule.

Matching rule (gap 12: no merge on surname alone, ambiguous means no link)
-------------------------------------------------------------------------
1. ORCID: a kg person whose ORCID (printed on an official page) equals an SNSF person's ORCID
   is linked. Rule "orcid".
2. Name + institution: names are normalised (titles like Prof./Dr./med./PD dropped, initials
   dropped, umlauts folded ü->ue, accents stripped, lower case). The key is
   (first given name, full surname). An SNSF person gives one key; a kg name "A B C" gives
   one key per split point, (a, "b c") and (a, "c"). The first given name must match in
   full: "Hans" never matches "Hans-Peter", and a surname alone never matches.
   A kg person (all kg people are Bern people) is linked to a Bern candidate with the same key
   only when the match is unique in both directions:
     - exactly one Bern SNSF candidate has the key -> rule "name+institution";
     - several candidates, but exactly one also matches the person's unit (a distinctive
       word of a kg unit name, such as "ARTORG", appears in the candidate's SNSF institute
       texts) -> rule "name+institution+unit";
     - otherwise no link.
   If two kg people would link to the same SNSF person (e.g. two records for one name),
   the unit test decides; if it cannot, none of them is linked.
   When a link by rule 2 also passes the unit test, the rule is recorded as
   "name+institution+unit" for display ("confidence: high").
Every line in grants.jsonl carries `match_rule` so the UI can show how the link was made.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "enrich" / "raw"
OUT = ROOT / "data" / "enrich" / "grants.jsonl"
KG = ROOT / "data" / "kg.sqlite"
PAGES = ROOT / "data" / "pages.jsonl"

EXPORTS = {
    "grants.csv": "https://data.snf.ch/datasets/grants.csv",
    "persons.csv": "https://data.snf.ch/datasets/persons.csv",
}
GRANT_URL = "https://data.snf.ch/grants/grant/{}"
SOURCE_LABEL = "SNSF"
LICENCE = "CC BY-SA 4.0, SNSF Data Portal (data.snf.ch)"
USER_AGENT = "mutz research navigator, Hack Apertus Liebefeld (contact: see CRAWL_CONTACT)"

ROLE_COLUMNS = {
    "ResponsibleApplicantGrantNumber": "responsible applicant",
    "CoApplicantGrantNumber": "co-applicant",
    "ApplicantAbroadGrantNumber": "applicant abroad",
    "ProjectPartnerGrantNumber": "project partner",
    "PracticePartnerGrantNumber": "practice partner",
    "ContactPersonGrantNumber": "contact person",
    "EmployeeGrantNumber": "employee",
}
ROLE_ORDER = list(ROLE_COLUMNS.values())

BERN_INSTITUTIONS = ("university of berne", "universitare psychiatrische dienste bern")
BERN_INSTITUTE_WORDS = ("inselspital", "universitatsspital bern", "universitat bern",
                        "university of bern", "universite de berne", "diabetes center berne",
                        "artorg")

TITLE_TOKENS = {
    "prof", "professor", "professorin", "dr", "drs", "med", "dent", "vet", "phil", "nat", "rer",
    "sc", "techn", "habil", "pd", "md", "phd", "msc", "bsc", "mba", "mph", "dipl", "ing",
    "lic", "iur", "jur", "oec", "pharm", "theol", "hc", "emer", "fmh", "frcs", "facc", "fesc",
    "mme", "dmd", "dds", "assoc", "titularprof", "privatdozent", "privatdozentin", "pd-dr",
    "prof-dr",
}   # no real surnames here ("Ma", "Das" are surnames and stay)
UNIT_STOPWORDS = {
    "the", "and", "for", "of", "in", "und", "fur", "der", "die", "das", "des", "de", "du", "la",
    "le", "et", "center", "centre", "zentrum", "institute", "institut", "department",
    "departement", "university", "universitat", "universite", "bern", "berne", "group",
    "research", "lab", "laboratory", "clinic", "klinik", "faculty", "fakultat", "medical",
    "medicine", "medizin", "medizinische", "biomedical", "engineering", "science", "sciences",
    "unit", "team", "division", "platform", "root", "hospital", "spital", "inselspital", "head",
}


# ---------------------------------------------------------------- normalisation

def fold(s: str) -> str:
    """Lower case, umlauts as ae/oe/ue, accents stripped, ascii only."""
    s = (s or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch)).encode("ascii", "ignore").decode()


def fold_plain(s: str) -> str:
    """Like fold() but umlauts lose their dots (ü -> u); for institution text matching."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch)).encode("ascii", "ignore").decode()


def _tokens(s: str) -> list[str]:
    """Name tokens: hyphens kept inside a token, titles and initials dropped."""
    s = fold(s)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z\- ]", " ", s.replace(".", ". "))
    out = []
    for t in s.split():
        t = t.strip("-")
        if not t or len(t) == 1 or t in TITLE_TOKENS:
            continue
        out.append(t)
    return out


def snsf_key(first: str, last: str) -> tuple[str, str] | None:
    given = _tokens(first)
    sur = [p for t in _tokens(last) for p in t.split("-") if p]
    if not given or not sur:
        return None
    return given[0], " ".join(sur)


def kg_keys(name: str) -> set[tuple[str, str]]:
    """Keys for a kg display name; handles "Last, First" too."""
    name = name or ""
    if "," in name:
        last, _, first = name.partition(",")
        k = snsf_key(first, last)
        return {k} if k else set()
    t = _tokens(name)
    keys = set()
    for i in range(1, len(t)):
        sur = [p for x in t[i:] for p in x.split("-") if p]
        keys.add((t[0], " ".join(sur)))
    return keys


def is_bern(institution: str, institute: str) -> bool:
    inst = fold_plain(institution)
    if any(b in inst for b in BERN_INSTITUTIONS):
        return True
    txt = fold_plain(institute)
    return any(w in txt for w in BERN_INSTITUTE_WORDS)


def unit_words(unit_names) -> set[str]:
    """Distinctive words of kg unit names (e.g. 'artorg', 'diabetes', 'gastroenterology')."""
    words = set()
    for n in unit_names:
        for w in re.findall(r"[a-z]{4,}", fold_plain(n or "")):
            if w not in UNIT_STOPWORDS:
                words.add(w)
    return words


def unit_match(words: set[str], institute_texts) -> bool:
    if not words:
        return False
    hay = set()
    for t in institute_texts:
        hay.update(re.findall(r"[a-z]{4,}", fold_plain(t or "")))
    return bool(words & hay)


# ---------------------------------------------------------------- SNSF data

def download(force: bool = False) -> None:
    """One polite GET per export file; skipped when the file is already on disk."""
    import httpx

    RAW.mkdir(parents=True, exist_ok=True)
    contact = os.environ.get("CRAWL_CONTACT", "")
    headers = {"User-Agent": USER_AGENT.replace("see CRAWL_CONTACT", contact or "see repo")}
    for fname, url in EXPORTS.items():
        dest = RAW / fname
        if dest.exists() and dest.stat().st_size > 1000 and not force:
            print(f"{fname}: cached, no request")
            continue
        tmp = dest.with_suffix(".part")
        with httpx.stream("GET", url, headers=headers, timeout=300, follow_redirects=True) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        os.replace(tmp, dest)
        print(f"{fname}: {dest.stat().st_size} bytes from {url}")


def _read_csv(path: Path):
    csv.field_size_limit(10**9)
    with open(path, encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f, delimiter=";")


def _date(s: str) -> str | None:
    return s[:10] if s else None


def _amount(s: str):
    try:
        return round(float(s))
    except (TypeError, ValueError):
        return None


def load_bern_grants(path: Path | None = None) -> dict[str, dict]:
    grants = {}
    for r in _read_csv(path or RAW / "grants.csv"):
        if not is_bern(r.get("ResearchInstitution", ""), r.get("Institute", "")):
            continue
        num = (r.get("GrantNumber") or "").strip()
        if not num:
            continue
        grants[num] = {
            "number": num,
            "title": (r.get("TitleEnglish") or r.get("Title") or "").strip(),
            "title_original": (r.get("Title") or "").strip(),
            "start": _date(r.get("EffectiveGrantStartDate")),
            "end": _date(r.get("EffectiveGrantEndDate")),
            "amount_chf": _amount(r.get("AmountGrantedAllSets")),
            "url": GRANT_URL.format(num),
            "institute": (r.get("Institute") or "").strip(),
            "institution": (r.get("ResearchInstitution") or "").strip(),
            "funding_instrument": (r.get("FundingInstrumentPublished") or "").strip(),
            "discipline": (r.get("MainDiscipline") or "").strip(),
            "keywords": (r.get("Keywords") or "").strip(),
            "state": (r.get("State") or "").strip(),
            "responsible_applicant": (r.get("ResponsibleApplicantName") or "").strip(),
        }
    return grants


def load_bern_people(grants: dict[str, dict], path: Path | None = None) -> list[dict]:
    """SNSF persons who hold a role on a Bern grant, or whose own institution is Bern."""
    people = []
    for r in _read_csv(path or RAW / "persons.csv"):
        roles = {}
        for col, role in ROLE_COLUMNS.items():
            for n in re.split(r"[;,\s]+", r.get(col) or ""):
                if n and n in grants and n not in roles:
                    roles[n] = role
        own_bern = is_bern(r.get("ResearchInstitution", ""), r.get("Institute", ""))
        if not roles and not own_bern:
            continue
        key = snsf_key(r.get("FirstName", ""), r.get("LastName", ""))
        if not key:
            continue
        people.append({
            "person_number": r.get("PersonNumber"),
            "first": r.get("FirstName", ""),
            "last": r.get("LastName", ""),
            "orcid": _orcid(r.get("ORCID")),
            "institute": r.get("Institute", ""),
            "key": key,
            "roles": roles,                       # grant number -> role, Bern grants only
        })
    return people


def _orcid(s) -> str | None:
    m = re.search(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", s or "")
    return m.group(0) if m else None


# ---------------------------------------------------------------- kg people

def load_kg_people(kg_path: Path = KG) -> list[dict]:
    """Read only: id, name, orcid and unit names for every person in kg.sqlite."""
    con = sqlite3.connect(f"file:{kg_path}?mode=ro", uri=True)
    try:
        units = {uid: name for uid, name in con.execute("SELECT id, name FROM unit")}
        aff = defaultdict(set)
        for pid, uid in con.execute("SELECT person_id, unit_id FROM affiliation"):
            aff[pid].add(uid)
        out = []
        for pid, name, orcid, puid in con.execute(
                "SELECT id, name, orcid, primary_unit_id FROM person"):
            uids = set(aff.get(pid, ())) | ({puid} if puid else set())
            names = set()
            for u in uids:
                names.add(units.get(u) or "")
                names.add(u.split(":")[1] if u.count(":") >= 2 else "")   # source group, e.g. artorg
                names.add(u.rsplit(":", 1)[-1].replace("-", " "))
            out.append({"entity_id": pid, "name": name, "orcid": _orcid(orcid),
                        "unit_names": sorted(n for n in names if n)})
        return out
    finally:
        con.close()


def load_page_people(pages_path: Path = PAGES) -> list[dict]:
    """Fallback before kg.sqlite exists: person pages from pages.jsonl, id = page:<page_id>.
    Only to test the join; these ids are not kg ids and are never written to grants.jsonl
    by `build` unless --from-pages is given."""
    out = []
    with open(pages_path) as f:
        for line in f:
            p = json.loads(line)
            if p.get("page_type") != "person":
                continue
            title = re.sub(r"^[^:]{0,40}:\s*", "", p.get("title") or "")
            name = re.split(r"\s+[-|–]\s+", title)[0]
            out.append({"entity_id": f"page:{p['page_id']}", "name": name, "orcid": None,
                        "unit_names": [p.get("group") or "", p.get("source") or ""]})
    return out


# ---------------------------------------------------------------- matching

def match(kg_people: list[dict], snsf_people: list[dict]) -> dict[str, tuple[dict, str]]:
    """entity_id -> (snsf person, rule). Implements the rule in the module docstring."""
    by_key = defaultdict(list)
    by_orcid = defaultdict(list)
    for sp in snsf_people:
        by_key[sp["key"]].append(sp)
        if sp["orcid"]:
            by_orcid[sp["orcid"]].append(sp)

    proposals = {}            # entity_id -> (sp, rule, unit_ok)
    for kp in kg_people:
        words = unit_words(kp.get("unit_names") or [])
        if kp.get("orcid") and len(by_orcid.get(kp["orcid"], [])) == 1:
            sp = by_orcid[kp["orcid"]][0]
            proposals[kp["entity_id"]] = (sp, "orcid", True)
            continue
        cands = {}
        for k in kg_keys(kp.get("name") or ""):
            for sp in by_key.get(k, []):
                cands[sp["person_number"]] = sp
        cands = list(cands.values())
        if not cands:
            continue
        unit_ok = [sp for sp in cands if unit_match(words, _institute_texts(sp))]
        if len(cands) == 1:
            sp = cands[0]
            ok = bool(unit_ok)
            proposals[kp["entity_id"]] = (sp, "name+institution+unit" if ok else "name+institution", ok)
        elif len(unit_ok) == 1:
            proposals[kp["entity_id"]] = (unit_ok[0], "name+institution+unit", True)
        # else: ambiguous, no link

    # one SNSF person claimed by several kg people: keep only a unique unit confirmed claim
    claims = defaultdict(list)
    for eid, (sp, rule, ok) in proposals.items():
        claims[sp["person_number"]].append((eid, rule, ok))
    links = {}
    for pn, lst in claims.items():
        if len(lst) == 1:
            eid, rule, _ = lst[0]
            links[eid] = (proposals[eid][0], rule)
            continue
        strong = [x for x in lst if x[1] == "orcid"] or [x for x in lst if x[2]]
        if len(strong) == 1:
            eid, rule, _ = strong[0]
            links[eid] = (proposals[eid][0], rule)
    return links


def _institute_texts(sp: dict) -> list[str]:
    return [sp.get("institute") or ""] + list(sp.get("_grant_institutes") or [])


def attach_grant_institutes(snsf_people: list[dict], grants: dict[str, dict]) -> None:
    for sp in snsf_people:
        sp["_grant_institutes"] = sorted({grants[n]["institute"] for n in sp["roles"] if n in grants})


def link_records(links, grants) -> list[dict]:
    rows = []
    for eid, (sp, rule) in sorted(links.items()):
        for num, role in sp["roles"].items():
            g = grants.get(num)
            if not g:
                continue
            rows.append({"entity_id": eid, "snsf_person_number": sp["person_number"],
                         "match_rule": rule, "role": role, "source": SOURCE_LABEL, **g})
    return rows


def build(from_pages: bool = False, out: Path = OUT) -> dict:
    grants = load_bern_grants()
    snsf_people = load_bern_people(grants)
    attach_grant_institutes(snsf_people, grants)
    if from_pages or not KG.exists():
        # page ids are not kg ids: never write them to the contract file
        kg_people, origin = load_page_people(), "pages.jsonl (fallback, page ids)"
        if out == OUT:
            out = OUT.with_name("grants_from_pages.jsonl")
    else:
        kg_people, origin = load_kg_people(), "kg.sqlite"
    links = match(kg_people, snsf_people)
    rows = link_records(links, grants)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, out)
    _CACHE.clear()
    rules = defaultdict(int)
    for _, rule in links.values():
        rules[rule] += 1
    stats = {"bern_grants": len(grants), "snsf_bern_people": len(snsf_people),
             "kg_people": len(kg_people), "people_origin": origin,
             "linked_people": len(links), "grant_links": len(rows), "rules": dict(rules),
             "examples": sorted(links)[:3], "written": str(out.relative_to(ROOT))}
    return stats


# ---------------------------------------------------------------- contract

_CACHE: dict = {}
CONTRACT_FIELDS = ("title", "number", "start", "end", "amount_chf", "role", "url")


def _index(path: Path) -> dict[str, list[dict]]:
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return {}
    hit = _CACHE.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    idx = defaultdict(list)
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            idx[r["entity_id"]].append(r)
    _CACHE[str(path)] = (mtime, dict(idx))
    return _CACHE[str(path)][1]


def _resolve(entity_id: str) -> str:
    """Follow kg merge_log to the kept id, if kg.sqlite exists."""
    if not KG.exists():
        return entity_id
    try:
        con = sqlite3.connect(f"file:{KG}?mode=ro", uri=True)
        try:
            seen = {entity_id}
            while True:
                row = con.execute("SELECT kept_id FROM merge_log WHERE merged_id=?", (entity_id,)).fetchone()
                if not row or row[0] in seen:
                    return entity_id
                entity_id = row[0]
                seen.add(entity_id)
        finally:
            con.close()
    except sqlite3.Error:
        return entity_id


def grants_for(entity_id: str, path: Path | None = None) -> list[dict]:
    """[{"title", "number", "start", "end", "amount_chf", "role", "url"}], newest first; [] when unknown.
    Extra fields: source ("SNSF"), match_rule, state, funding_instrument, institute, status
    (active|quiet|past, from the end date, as in the Studies section)."""
    idx = _index(path or OUT)
    rows = idx.get(entity_id)
    if rows is None:
        rows = idx.get(_resolve(entity_id), [])
    out = []
    for r in rows:
        g = {k: r.get(k) for k in CONTRACT_FIELDS}
        g.update({"source": r.get("source", SOURCE_LABEL), "match_rule": r.get("match_rule"),
                  "state": r.get("state"), "funding_instrument": r.get("funding_instrument"),
                  "institute": r.get("institute"), "status": recency(r.get("end"))})
        out.append(g)
    out.sort(key=lambda g: (g["start"] or "", g["number"]), reverse=True)
    return out


def recency(end: str | None, today: str | None = None) -> str | None:
    """Active (ends in the future or within 2 years), quiet (2 to 5 years), past (over 5)."""
    if not end:
        return None
    from datetime import date
    t = date.fromisoformat(today) if today else date.today()
    try:
        e = date.fromisoformat(end[:10])
    except ValueError:
        return None
    years = (t - e).days / 365.25
    return "active" if years <= 2 else "quiet" if years <= 5 else "past"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "download":
        download(force="--force" in sys.argv)
    elif cmd == "build":
        if not all((RAW / f).exists() for f in EXPORTS):
            download()
        print(json.dumps(build(from_pages="--from-pages" in sys.argv), indent=2))
    else:
        print("usage: python -m enrich.snsf [download [--force] | build [--from-pages]]")
