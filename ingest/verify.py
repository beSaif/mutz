"""Verifier: a fact survives only if its quote is really on the page and really supports it.

Hardened per playbook gap 3:
- the quote must be found in the page text (exact after normalising, else fuzzy >= MIN_RATIO
  over a window of the same length, so a short quote cannot match "almost anywhere");
- LLM quotes need at least MIN_WORDS words;
- the quote must contain the entity name and the field value it is evidence for;
- a person quote that reads like an external collaborator, a coauthor list or a citation makes
  the person a *mention*, never a Bern member.

Rules facts (structured HTML: schema.org Person blocks, DCB team grid) are checked for presence
of the name in the page's visible text, with no minimum length, because they are not model output.
"""
import re
import unicodedata

from rapidfuzz import fuzz

MIN_WORDS = 8
MIN_RATIO = 90      # quote vs page
FIELD_RATIO = 88    # value vs quote

TITLE_RX = re.compile(
    r"\b(prof(essor)?|dr|pd|med|phil|nat|rer|sc|techn|dent|vet|em|emer|msc|bsc|ma|mba|phd|dipl|ing|habil|"
    r"mag|lic|iur|h\.?\s?c|pharm|biol|chem|dott|mme|mr|mrs|ms|herr|frau)\b\.?",
    re.I,
)

# signals that a person named in the quote is not a member of a Bern unit
EXTERNAL_RX = re.compile(
    r"in collaboration with|collaborat\w* with|in cooperation with|in zusammenarbeit mit|zusammen mit|"
    r"en collaboration avec|partnership with|together with|joint(ly)? with|visiting|guest|invited|"
    r"gastprofessor|keynote|speaker|talk by|lecture by|from (the )?(eth|epfl|mit|harvard|stanford|oxford|"
    r"cambridge|university of (?!bern)|universit[äa]t (?!bern)|universit[ée] (?!de berne)|max planck|"
    r"karolinska|inselspital)|\bof (eth|epfl)\b|\bat (eth|epfl|the university of (?!bern))",
    re.I,
)
CITATION_RX = re.compile(
    r"\bet al\b|\bdoi\b|\(\d{4}\)|\b(19|20)\d{2};|\bvol\.|\bpp\.|\bjournal\b|\bproc\.|"
    r"(\b[A-Z][a-zà-ÿ'\-]+ [A-Z]{1,2}[,.;]\s*){2,}|"          # Müller A, Meier B,
    r"(\b[A-Z][a-zà-ÿ'\-]+, [A-Z]\.(\s?[A-Z]\.)?[,;&]\s*){2,}",  # Müller, A., Meier, B.,
)


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.replace("­", "").replace("’", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip()


def fold(s):
    """norm + strip accents, for name comparison."""
    s = unicodedata.normalize("NFKD", norm(s))
    return "".join(c for c in s if not unicodedata.combining(c))


def strip_titles(name):
    n = TITLE_RX.sub(" ", name or "")
    n = re.sub(r"[.,]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def name_tokens(name):
    return [t for t in re.split(r"[\s\-]+", fold(strip_titles(name))) if len(t) > 1]


def words(s):
    return len(re.findall(r"\w+", s or ""))


def in_page(quote, page_text, min_ratio=MIN_RATIO):
    """Quote occurs in page text: exact after normalising, else fuzzy over a same length window."""
    q, t = norm(quote), norm(page_text)
    if not q or not t:
        return False
    if q in t:
        return True
    if len(q) < 25:          # short quotes must match exactly
        return False
    al = fuzz.partial_ratio_alignment(q, t)
    if al is None or al.score < min_ratio:
        return False
    span = al.dest_end - al.dest_start
    return span >= 0.8 * len(q)


def contains_value(quote, value, ratio=FIELD_RATIO):
    """Field value appears in quote (exact, accent folded, or fuzzy for values of 5+ chars)."""
    if not value:
        return False
    q, v = fold(quote), fold(value)
    if v in q:
        return True
    if len(v) < 5:
        return False
    if fuzz.partial_ratio(v, q) >= ratio:
        return True
    # every content word of the value occurs in the quote (prefix match, so plural/declension pass)
    vt = [w for w in re.findall(r"\w+", v) if len(w) >= 4]
    qt = re.findall(r"\w+", q)
    return len(vt) >= 1 and ratio < 100 and all(any(x.startswith(w[:5]) for x in qt) for w in vt)


def contains_name(quote, name):
    """Every name token (titles stripped) appears in the quote, so 'Müller' alone is not 'Anna Müller'."""
    toks = name_tokens(name)
    if len(toks) < 2:
        return False
    q = fold(quote)
    qtoks = set(re.findall(r"[\w']+", q))
    for t in toks:
        if t in qtoks:
            continue
        if not any(fuzz.ratio(t, w) >= 90 for w in qtoks if abs(len(w) - len(t)) <= 2):
            return False
    return True


def external_or_citation(quote):
    """'external' / 'coauthor' if the quote reads that way, else None."""
    if CITATION_RX.search(quote or ""):
        return "coauthor"
    if EXTERNAL_RX.search(quote or ""):
        return "external"
    return None


SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜÀ-Ý0-9\"'(])|\n+")


def expand_quote(quote, page_text):
    """The 8B often quotes a phrase. If the phrase (2+ words) is on the page verbatim, use the whole page
    sentence that contains it as the quote; the checks below then run on that real sentence."""
    if words(quote) < 2:
        return None
    q = norm(quote)
    for sent in SENT_SPLIT.split(page_text or ""):
        if q in norm(sent):
            return sent.strip()
    return None


def check_llm_fact(fact, page_text, page_type=None):
    """Return (kept_fact_or_None, reason, mention_or_None). Mutates nothing."""
    quote = fact.get("quote") or ""
    kind = fact["kind"]
    if words(quote) < MIN_WORDS:
        longer = expand_quote(quote, page_text)
        if not longer or words(longer) < MIN_WORDS:
            return None, "quote_too_short", None
        fact = {**fact, "quote": longer, "quote_expanded": True}
        quote = longer
    if not in_page(quote, page_text):
        return None, "quote_not_on_page", None
    f = dict(fact)
    if kind == "person":
        name = f.get("name") or ""
        if not contains_name(quote, name):
            return None, "name_not_in_quote", None
        why = external_or_citation(quote)
        if page_type in ("publications", "news"):
            why = why or page_type
        if why or (f.get("relation") or "member") not in ("member", "head"):
            return None, "not_a_member", {"name": name, "quote": quote, "context": why or f.get("relation")}
        # every field must be supported by the same quote, else it is dropped (the person may stay)
        for fld in ("function", "unit"):
            if f.get(fld) and not contains_value(quote, f[fld]):
                f[fld] = None
        if not (f.get("function") or f.get("unit")):
            return None, "membership_not_in_quote", None
        if f.get("email") and fold(f["email"]) not in fold(page_text):
            f["email"] = None
        return f, "ok", None
    key = {"expertise": "label", "project": "title", "infrastructure": "name"}[kind]
    if not contains_value(quote, f.get(key)):
        return None, f"{key}_not_in_quote", None
    if kind == "infrastructure" and not is_infrastructure(f.get("name"), quote):
        return {**f, "kind": "device"}, "ok", None        # resolve.py attaches it to the page's project, if any
    if kind == "project":
        for fld in ("start", "end", "status"):
            if f.get(fld) and not contains_value(quote, str(f[fld]), ratio=100):
                f[fld] = None
    return f, "ok", None


# Infrastructure = a facility, platform or service someone outside the group can use. Devices, materials,
# methods and study tools are not infrastructure; on a project page they become project evidence ("device").
INFRA_WORD = (r"facilit|platform|plattform|plateforme|\bcore\b|service|dienstleist|biobank|trial unit|"
              r"clinical trial|imaging cent|sequencing|bsl-?\d|clean ?room|reinraum|animal facility|tierhaltung|"
              r"storage|equipment|ger[äa]tepark|\bhpc\b|computing cluster|cohort|infrastruct|einrichtung")
INFRA_RX = re.compile(INFRA_WORD, re.I)
DEVICE_RX = re.compile(r"sensor|smartwatch|smartur|watch|wearable|\bapps?\b|appen|application|pump|\bcgm\b|"
                       r"glucose monitor|insulin|radar|implant|pacemaker|catheter|redcap|membrane|hydrogel|organoid|"
                       r"\bchips?\b|diaphragm|micropillar|mesh|\bplate\b|board|tool\b|model|algorithm|software|"
                       r"\bdata\b|stent|valve|electrode|prototype", re.I)
AVAIL_RX = re.compile(r"available to|open to|offer(s|ed)? (to|for)|for external|external users|access for|"
                      r"zur verf[üu]gung|can be booked|book(ing|ed)? |reserv|on a fee basis|for a fee|"
                      r"disponible pour|ouvert aux", re.I)
ORG_RX = re.compile(r"^(artorg|dcb|diabetes cent|sitem|universit|inselspital|insel)\b", re.I)


def is_infrastructure(name, quote):
    n = (name or "").strip()
    if not n or DEVICE_RX.search(n) or n.lower() in ("infrastructure", "facilities", "platform", "services", "lab"):
        return False
    if INFRA_RX.search(n):
        return True
    if ORG_RX.search(n):
        return False
    if AVAIL_RX.search(quote or ""):                     # an instrument offered to others: "available to partners"
        return True
    # a named thing called a facility/platform/service right next to its name: "imaging platform (ACTIO)"
    esc = re.escape(n)
    return bool(re.search(rf"(?:{INFRA_WORD})[^.]{{0,40}}{esc}|{esc}[^.]{{0,40}}(?:{INFRA_WORD})", quote or "", re.I))


def check_rules_fact(fact, visible_text):
    """Rules facts come from page structure; the name must be in the visible text."""
    if not contains_name(visible_text, fact.get("name", "")) and fold(fact.get("name", "")) not in fold(visible_text):
        return None, "rules_name_not_visible"
    return fact, "ok"
