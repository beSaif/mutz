"""Pitch export: validate a pitch dict and render the Review PDF or the clean PDF.

Contract (docs/contracts-extra.md):
    validate(pitch) -> [problems]            empty list means ok
    build_pdf(pitch, mode="review"|"clean", out_path) -> out_path
    ExportError(problems)                    raised by build_pdf on any problem

CLI:
    python -m pitch.build fixture.json --mode review --out /tmp/x.pdf
    python -m pitch.build fixture.json --check        (validate only)

Optional pitch and card fields understood on top of the contract:
    pitch["lang"]                   "en" (default), "de", "fr": sets hyphenation
    pitch["visit_day"]              [{"time", "topic", "with"}], a draft programme
    pitch["override_review_gate"]   true lets the clean PDF out with unchecked cards
    card["included"]                false hides the card from export and validation
    card["checked"]                 false blocks the clean PDF (review gate)
    card["status"]                  "verified" | "edited" | "needs_check" (else derived)
    card["edited"]                  true marks the card "edited by you"
    card["section"]                 "why" | "matches" | "infrastructure" (else from type)
    card["person"]                  name of a group head; hidden unless show_person_name
    card["notes"]                   officer notes, Review PDF only
    source["title"], source["fetched_at"]   shown in the evidence appendix
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "templates"
LOGO_PATH = ROOT / "assets" / "private" / "Logo_SF_BE_EN.png"
AGENCY_NAME = "Bern Economic Development Agency"
MODES = ("review", "clean")


class ExportError(Exception):
    """Raised by build_pdf when the pitch has problems. .problems is a list of str."""

    def __init__(self, problems):
        self.problems = list(problems)
        lines = "\n".join(f"- {p}" for p in self.problems)
        super().__init__(f"Export stopped, {len(self.problems)} problem(s):\n{lines}")


# --------------------------------------------------------------------------
# Rules. Sources: playbook "Avoiding AI slop" (banned words, banned shapes, the
# BANNED regex) and gap 8 (no line promises a meeting, names off by default).
# --------------------------------------------------------------------------

# (pattern, word to show). "innovative" is banned, "innovation" is not: real
# programme names such as "innovation challenge" appear on the source pages.
BANNED_WORDS = [
    (r"\bunlock\w*", "unlock"),
    (r"\belevat\w*", "elevate"),
    (r"\bseamless\w*", "seamless"),
    (r"\bcutting[\s-]?edge\b", "cutting edge"),
    (r"\binnovative\w*", "innovative"),
    (r"\bleverag\w*", "leverage"),
    (r"\bsynerg\w*", "synergy"),
    (r"\bsupercharg\w*", "supercharge"),
    (r"\brevolutioni[sz]\w*", "revolutionize"),
    (r"\bgame[\s-]?changer\w*", "game changer"),
    (r"\bdelv(e|es|ed|ing)\b", "delve"),
    (r"\bforefront\b", "forefront"),
    (r"\bjourney\w*", "journey"),
    (r"\bempower\w*", "empower"),
    (r"\bworld[\s-]?class\b", "world class"),
    (r"\bbest[\s-]in[\s-]class\b", "best in class"),
]
BANNED_SHAPES = [
    (r"\bit'?s not just\b|\bit is not just\b|\bnot just [^.]{1,60}, (it'?s|but)\b",
     'the "not just X, it\'s Y" shape'),
    (r"\bready to\b[^.?!]*\?", 'a "Ready to...?" question'),
    (r"\?", "a question (rhetorical questions are banned)"),
    (r"!", "an exclamation mark"),
    (r"\b\d{1,3}\s?% (ai )?match\b", 'a made up match percentage'),
]
# Gap 8: the teaser must not promise that anyone will meet the company.
MEETING_PROMISES = [
    r"\bwill (gladly |happily |personally )?(meet|host|receive|welcome|see|show) you\b",
    r"\bwill (gladly |happily |personally )?meet\b",
    r"\b(has|have|had) (already )?(agreed|committed|offered to)\b",
    r"\b(has|have|had) (already )?(confirmed|accepted) (a |the |your )?"
    r"(meeting|visit|invitation|interest|availability|call)\b",
    r"\bagreed to (meet|host|receive|see|talk|present)\b",
    r"\blook(s|ing)? forward\b",
    r"\b(is|are) (happy|glad|keen|eager|ready|available|delighted|pleased|excited|willing) "
    r"to (meet|host|receive|talk|speak|discuss|welcome|see|present|show)\b",
    r"\b(meeting|meetings|visit|call)s? (is |are |has been |have been )?"
    r"(confirmed|arranged|booked|scheduled|set up|agreed)\b",
    r"\b(would|will) (love|like) to (meet|host|welcome|see)\b",
    r"\bawait(s|ing)? (you|your visit)\b",
    r"\bfreu(t|en) sich\b",                     # de: looks forward
    r"\b(hat|haben) (bereits )?zugesagt\b",      # de: has agreed
    r"\bwird (sie|ihnen) (gerne )?treffen\b",    # de: will meet you
    r"\bse r[ée]joui(t|ssent)\b",                # fr: looks forward
    r"\b(a|ont) (d[ée]j[aà] )?accept[ée]\b",     # fr: has agreed
    r"\brencontrera\b",                          # fr: will meet
]
_BANNED_RE = [(re.compile(p, re.I), w) for p, w in BANNED_WORDS]
_SHAPE_RE = [(re.compile(p, re.I), w) for p, w in BANNED_SHAPES]
_MEETING_RE = [re.compile(p, re.I) for p in MEETING_PROMISES]
_TITLE_NAME_RE = re.compile(r"\b(Prof|Dr|PD|Professor|Professorin|Mme|Mrs|Mr|Ms)\.?\s+[A-ZÄÖÜÉÈ][\w-]+")
_TITLE_WORDS = {"prof", "professor", "professorin", "dr", "med", "phil", "rer", "nat",
                "pd", "md", "phd", "msc", "bsc", "ing", "dipl", "sc", "techn", "dent",
                "mr", "mrs", "ms", "mme", "m", "von", "van", "de", "der", "den", "du", "la", "le"}

SECTIONS = [
    ("why", "Why Bern for you"),
    ("matches", "Matches"),
    ("infrastructure", "Infrastructure and services"),
]
# The pitch reads as why Bern, what Bern offers, how to start. Each section carries its part as a kicker.
PARTS = {"why": "Why", "matches": "What", "infrastructure": "What", "visit": "How"}
BLOCKS = ["why", "matches", "infrastructure", "visit"]
VISIT_TITLE = "A suggested visit day"
_SECTION_BY_TYPE = {
    "why": "why", "why_bern": "why", "point": "why", "reason": "why",
    "infrastructure": "infrastructure", "service": "infrastructure",
    "facility": "infrastructure", "clinical_trials": "infrastructure",
    "transfer": "infrastructure", "funding": "infrastructure", "space": "infrastructure",
    "talent": "infrastructure",
}
TYPE_LABELS = {
    "person": "Researcher", "group": "Research group", "unit": "Unit",
    "project": "Project", "study": "Study", "grant": "Grant",
    "infrastructure": "Infrastructure", "service": "Service", "facility": "Facility",
    "clinical_trials": "Clinical trial support", "transfer": "Technology transfer",
    "funding": "Funding", "space": "Space", "talent": "Talent",
}
STATUS_LABELS = {"verified": "Verified quote", "edited": "Edited by you",
                 "needs_check": "Needs a check"}


# --------------------------------------------------------------------------
# Deep links (playbook, Pitch builder: URL text fragments)
# --------------------------------------------------------------------------

def _enc(s: str) -> str:
    return quote(s, safe="").replace("-", "%2D")   # a dash is special in fragments


def deep_link(url: str, q: str) -> str:
    """URL that opens the page scrolled to the quote and highlights it."""
    q = " ".join((q or "").split())
    if not q:
        return url
    w = q.split()
    frag = _enc(q) if len(w) <= 8 else _enc(" ".join(w[:4])) + "," + _enc(" ".join(w[-4:]))
    return f"{url.split('#', 1)[0]}#:~:text={frag}"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _included(pitch) -> list[tuple[int, dict]]:
    return [(i, c) for i, c in enumerate(pitch.get("cards") or [])
            if isinstance(c, dict) and c.get("included", True) is not False]


def _card_label(i: int, card: dict) -> str:
    name = (card.get("unit") if not card.get("show_person_name") and card.get("type") == "person"
            else card.get("name")) or card.get("unit") or card.get("entity_id") or ""
    return f"Card {i + 1}" + (f" ({name})" if name else "")


def _is_http(url) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _name_tokens(name: str) -> list[str]:
    toks = re.findall(r"[^\W\d_][\w'-]*", name or "")
    return [t for t in toks if len(t) >= 3 and t.lower().strip(".") not in _TITLE_WORDS]


def _prose_fields(pitch) -> list[tuple[str, str, int | None]]:
    """(where, text, card index) for every piece of written copy that lands in the PDF."""
    out = [("Title", pitch.get("title") or "", None), ("Intro", pitch.get("intro") or "", None)]
    for i, c in _included(pitch):
        out.append((f"{_card_label(i, c)} text", c.get("text") or "", i))
    if pitch.get("how"):
        out.append(("How to start", pitch.get("how") or "", None))
    for j, v in enumerate(pitch.get("visit_day") or []):
        if isinstance(v, dict):
            out.append((f"Visit day line {j + 1}",
                        " ".join(str(v.get(k) or "") for k in ("topic", "with")), None))
    return out


def validate_detailed(pitch, mode: str | None = None) -> list[dict]:
    """Like validate() but each problem is {"code", "where", "card", "message"}."""
    probs: list[dict] = []

    def add(code, where, msg, card=None):
        probs.append({"code": code, "where": where, "card": card, "message": f"{where}: {msg}"})

    if not isinstance(pitch, dict):
        add("missing_field", "Pitch", "is not a JSON object.")
        return probs
    company = pitch.get("company") or {}
    if not (isinstance(company, dict) and (company.get("name") or "").strip()):
        add("missing_field", "Cover", "the company name is missing.")
    if not (pitch.get("title") or "").strip():
        add("missing_field", "Cover", "the title is missing.")
    officer = pitch.get("officer") or {}
    if not (isinstance(officer, dict) and (officer.get("name") or "").strip()):
        add("missing_field", "Cover", "the contact officer's name is missing.")

    cards = _included(pitch)
    if not cards:
        add("no_cards", "Pitch", "has no cards to export. Add at least one match.")

    # Every card needs at least one public source with a working link.
    for i, c in cards:
        where = _card_label(i, c)
        if not (c.get("text") or "").strip():
            add("missing_field", where, "the card text is empty.", i)
        srcs = [s for s in (c.get("sources") or []) if isinstance(s, dict)]
        if not srcs:
            add("no_source", where, "has no source. Add a source URL or remove the card.", i)
        for k, s in enumerate(srcs):
            url = s.get("url")
            if not _is_http(url):
                add("bad_url", where,
                    f"source {k + 1} has no valid http(s) URL ({url!r}). "
                    "Add the page address or remove the source.", i)
                continue
            dl = s.get("deep_link")
            if dl and (not _is_http(dl) or urlsplit(dl).netloc != urlsplit(url).netloc):
                add("bad_url", where,
                    f"source {k + 1} deep link does not point at the source page ({dl!r}).", i)

    # Copy rules.
    for where, text, ci in _prose_fields(pitch):
        for rx, word in _BANNED_RE:
            m = rx.search(text)
            if m:
                add("banned_word", where,
                    f'uses the banned word "{m.group(0)}" ({word}). Say it plainly.', ci)
        for rx, shape in _SHAPE_RE:
            if rx.search(text):
                add("banned_shape", where, f"uses {shape}. Write a plain statement.", ci)
        for rx in _MEETING_RE:
            m = rx.search(text)
            if m:
                add("meeting_promise", where,
                    f'promises a meeting or a commitment ("{m.group(0)}"). Nobody has been '
                    "asked yet; describe the group's work instead.", ci)
                break

    # Gap 8: names stay off the card unless the officer switched them on.
    shown_names = {(c.get("name") or "").strip() for _, c in cards
                   if c.get("show_person_name") and c.get("type") == "person"}
    shown_names |= {(c.get("person") or "").strip() for _, c in cards
                    if c.get("show_person_name") and c.get("person")}
    hidden = []
    for i, c in cards:
        if c.get("show_person_name"):
            continue
        if c.get("type") == "person" and (c.get("name") or "").strip():
            hidden.append((i, c["name"].strip()))
        if (c.get("person") or "").strip():
            hidden.append((i, c["person"].strip()))
    hidden = [(i, n) for i, n in hidden if n not in shown_names]
    checks = [("Title", pitch.get("title") or "", None), ("Intro", pitch.get("intro") or "", None)]
    for i, c in cards:
        checks.append((f"{_card_label(i, c)} text", c.get("text") or "", i))
        checks.append((f"{_card_label(i, c)} unit", c.get("unit") or "", i))
        if c.get("type") != "person":
            checks.append((f"{_card_label(i, c)} name", c.get("name") or "", i))
    for j, v in enumerate(pitch.get("visit_day") or []):
        if isinstance(v, dict):
            checks.append((f"Visit day line {j + 1}",
                           " ".join(str(v.get(k) or "") for k in ("topic", "with")), None))
    seen = set()
    for hi, name in hidden:
        toks = _name_tokens(name)
        for where, text, ci in checks:
            for t in toks:
                if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", text):
                    key = (where, name)
                    if key not in seen:
                        seen.add(key)
                        add("person_name", where,
                            f'names "{name}", but "name the person" is off for that card. '
                            "Describe the group and its work, or switch the name on.", ci)
                    break
    for i, c in cards:
        if not c.get("show_person_name"):
            m = _TITLE_NAME_RE.search(c.get("text") or "")
            if m and not any(p["code"] == "person_name" and p["card"] == i for p in probs):
                add("person_name", f"{_card_label(i, c)} text",
                    f'names a person ("{m.group(0)}"), but "name the person" is off. '
                    "Describe the group and its work, or switch the name on.", i)

    # Review gate, clean PDF only.
    if mode == "clean" and not pitch.get("override_review_gate"):
        unchecked = [i for i, c in cards if c.get("checked") is False]
        if unchecked:
            add("review_gate", "Review gate",
                f"{len(unchecked)} card(s) not checked yet (cards "
                f"{', '.join(str(i + 1) for i in unchecked)}). Tick them, or override.")
    return probs


def validate(pitch, mode: str | None = None) -> list[str]:
    """Return a list of problems as plain sentences. Empty list means ok."""
    return [p["message"] for p in validate_detailed(pitch, mode)]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _fmt_date(value) -> str:
    if not value:
        return ""
    if isinstance(value, (date, datetime)):
        d = value
    else:
        try:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    return f"{d.day} {d.strftime('%B %Y')}"


def _host(url: str) -> str:
    h = urlsplit(url).netloc
    return h[4:] if h.startswith("www.") else h


def _strip_markers(text: str) -> str:
    return re.sub(r"\s*\[\d+\]", "", text or "").strip()


def _section_of(card: dict) -> str:
    sec = card.get("section")
    if sec in dict(SECTIONS):
        return sec
    return _SECTION_BY_TYPE.get((card.get("type") or "").lower(), "matches")


def _context(pitch: dict, mode: str) -> dict:
    sources: list[dict] = []
    index: dict[tuple, int] = {}
    sections = []
    claim_no = 0
    # order and titles as the officer set them in the editor; the app calls the infrastructure section "infra"
    given = [({"infra": "infrastructure"}.get(sec.get("id"), sec.get("id")), sec) for sec in pitch.get("sections") or []
             if isinstance(sec, dict)]
    titles = {k: sec["title"] for k, sec in given if sec.get("title")}
    order = [k for k, _ in given if k in BLOCKS]
    order += [k for k in BLOCKS if k not in order]
    for key, title in SECTIONS:
        items = []
        for i, c in _included(pitch):
            if _section_of(c) != key:
                continue
            claim_no += 1
            claim_id = f"claim-{claim_no}"
            refs = []
            for s in c.get("sources") or []:
                url = s["url"].strip()
                q = " ".join((s.get("quote") or "").split())
                k = (url, q)
                if k not in index:
                    index[k] = len(sources) + 1
                    sources.append({
                        "n": index[k], "url": url, "quote": q, "host": _host(url),
                        "deep_link": s.get("deep_link") or deep_link(url, q),
                        "title": s.get("title") or "",
                        "fetched": _fmt_date(s.get("fetched_at")),
                        "claim": claim_id, "edited": bool(c.get("edited")),
                    })
                if index[k] not in refs:
                    refs.append(index[k])
            status = c.get("status")
            if status not in STATUS_LABELS:
                status = ("edited" if c.get("edited") else
                          "verified" if all((s.get("quote") or "").strip()
                                            for s in c.get("sources") or []) else "needs_check")
            ptype = (c.get("type") or "").lower()
            show = bool(c.get("show_person_name"))
            if ptype == "person" and not show:
                heading = c.get("unit") or "Research group"
                sub = ""
            else:
                heading = c.get("name") or c.get("unit") or ""
                sub = c.get("unit") if c.get("unit") and c.get("unit") != heading else ""
            items.append({
                "id": claim_id, "heading": heading, "sub": sub,
                "person": c.get("person") if (show and c.get("person")) else "",
                "kind": TYPE_LABELS.get(ptype, ""),
                "text": _strip_markers(c.get("text") or ""), "refs": refs,
                "status": status, "status_label": STATUS_LABELS[status],
                "notes": c.get("notes") or "",
            })
        if items:
            sections.append({"key": key, "title": titles.get(key) or title, "part": PARTS[key], "cards": items})
    for s in sources:  # mark sources whose claim was edited
        s["status"] = "edited" if s["edited"] else ("verified" if s["quote"] else "needs_check")

    logo_uri = LOGO_PATH.as_uri() if LOGO_PATH.is_file() else None
    company = pitch.get("company") or {}
    return {
        "mode": mode, "review": mode == "review", "lang": pitch.get("lang") or "en",
        "title": pitch.get("title") or "", "intro": pitch.get("intro") or "",
        "written": bool(pitch.get("written")), "company": company, "officer": pitch.get("officer") or {},
        "date": _fmt_date(pitch.get("created_at")) or _fmt_date(date.today()),
        "case_id": pitch.get("case_id") or "",
        "sections": sections, "sources": sources, "how": pitch.get("how") or "",
        "blocks": [k for k in order if k == "visit" or any(sec["key"] == k for sec in sections)],
        "visit_title": titles.get("visit") or VISIT_TITLE,
        "visit_day": [v for v in pitch.get("visit_day") or [] if isinstance(v, dict)],
        "logo_uri": logo_uri, "agency": AGENCY_NAME,
        "n_cards": sum(len(s["cards"]) for s in sections),
    }


def render_html(pitch: dict, mode: str = "review") -> str:
    """The HTML that goes into WeasyPrint. Does not validate."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)),
                      autoescape=select_autoescape(["html", "j2"]))
    return env.get_template("pitch.html.j2").render(**_context(pitch, mode))


def _patch_macos_dlopen():
    """Homebrew Pango lives in /opt/homebrew/lib, which dlopen does not search by
    default on macOS. Retry WeasyPrint's library lookups there. No-op elsewhere."""
    if sys.platform != "darwin":
        return
    import cffi
    if getattr(cffi.FFI.dlopen, "_mutz_patched", False):
        return
    orig = cffi.FFI.dlopen
    dirs = [d for d in ("/opt/homebrew/lib", "/usr/local/lib") if os.path.isdir(d)]

    def dlopen(self, name, flags=0):
        try:
            return orig(self, name, flags)
        except OSError:
            if isinstance(name, str) and "/" not in name:
                for d in dirs:
                    for cand in (f"{d}/{name}", f"{d}/{name}.dylib"):
                        if os.path.exists(cand):
                            return orig(self, cand, flags)
            raise

    dlopen._mutz_patched = True
    cffi.FFI.dlopen = dlopen


def build_pdf(pitch: dict, mode: str = "review", out_path=None) -> str:
    """Validate, then write the Review PDF or clean PDF. Returns out_path.
    Raises ExportError(problems) if anything is wrong; no file is written then."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if out_path is None:
        raise ValueError("out_path is required")
    problems = validate(pitch, mode)
    if problems:
        raise ExportError(problems)
    html = render_html(pitch, mode)
    _patch_macos_dlopen()
    from weasyprint import HTML
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(TEMPLATES)).write_pdf(str(out))
    return str(out_path)


def export(pitch: dict, out_dir, stem: str | None = None) -> dict:
    """Write review PDF, clean PDF and the pitch JSON. Returns their paths."""
    out_dir = Path(out_dir)
    stem = stem or re.sub(r"[^\w-]+", "-", str(pitch.get("case_id") or "pitch")).strip("-")
    paths = {
        "review": build_pdf(pitch, "review", out_dir / f"{stem}-review.pdf"),
        "clean": build_pdf(pitch, "clean", out_dir / f"{stem}-clean.pdf"),
        "json": str(out_dir / f"{stem}.json"),
    }
    Path(paths["json"]).write_text(json.dumps(pitch, ensure_ascii=False, indent=2), "utf-8")
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pitch.build",
                                 description="Validate a pitch JSON and export it as PDF.")
    ap.add_argument("pitch_json")
    ap.add_argument("--mode", choices=MODES, default="review")
    ap.add_argument("--out", help="output PDF path")
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    a = ap.parse_args(argv)
    pitch = json.loads(Path(a.pitch_json).read_text("utf-8"))
    if a.check or not a.out:
        problems = validate(pitch, a.mode)
        for p in problems:
            print(f"- {p}", file=sys.stderr)
        print("ok" if not problems else f"{len(problems)} problem(s)",
              file=sys.stderr if problems else sys.stdout)
        return 1 if problems else 0
    try:
        out = build_pdf(pitch, a.mode, a.out)
    except ExportError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
