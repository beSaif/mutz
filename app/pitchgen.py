"""First pitch draft from accepted matches, per card rewrites, and conversion to the pitch contract.

Card text comes from Apertus 70B writing only from the card's own quotes, then is checked in code for
banned words and meeting promises; on any failure the card falls back to the verified quote itself."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from app.stubs import BANNED, PROMISE

# The pitch answers three questions in order: why Bern, what Bern offers, how to start.
SECTIONS = [
    {"id": "cover", "title": "Cover"},
    {"id": "why", "title": "Why Bern for you", "part": "why"},
    {"id": "matches", "title": "Research partners", "part": "what"},
    {"id": "infra", "title": "Labs, facilities and services", "part": "what"},
    {"id": "visit", "title": "How to start", "part": "how"},
    {"id": "sources", "title": "Sources", "auto": True},
]
HOW = ("Nothing is arranged yet. If you plan a visit, tell us which groups interest you most. "
       "The agency then asks each group whether they are available and proposes a day in Bern.")

STYLE = ("Write like a Bernese civil servant briefing a CEO: plain, concrete, short. Max 2 sentences per card. "
         "Use nouns from the quotes (devices, methods, patient groups). Only state what the quotes say. "
         "Write about the group and its work; do not name a person unless asked. Never promise a meeting: no 'will meet', "
         "'has agreed', 'looks forward'. Never use: unlock, elevate, seamless, innovative, cutting edge, leverage, synergy, "
         "forefront, journey, empower. No exclamation marks. End each card with its source marker like [1].")


def _cid():
    return uuid.uuid4().hex[:8]


def _clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def _ok(text):
    return text and not BANNED.search(text) and not PROMISE.search(text) and "!" not in text


def _src(r):
    out = []
    for s in (r.get("reasons") or [])[:2]:
        if s.get("url"):
            from app.stubs import clean_quote
            out.append({"url": s["url"], "quote": clean_quote(s.get("quote", "")), "deep_link": s.get("deep_link") or s["url"],
                        "fetched_at": r.get("fetched_at")})
    return out


def _fallback_text(r, n=None):
    q = ((r.get("reasons") or [{}])[0].get("quote") or "").strip()
    who = r.get("unit") or "A Bern group"
    if ftype(r) == "person":
        who = f"A group at {r.get('unit') or 'the University of Bern'}"
    elif r.get("name"):
        who = r["name"]
    q = " ".join(q.split()[:35])
    return f"{who}: “{q}” [1]" if q else f"{who}. [1]"


def ftype(r):
    t = (r.get("type") or "unit").lower()
    return {"group": "unit", "project": "study"}.get(t, t)


def _llm_cards(items, company, lang="en"):
    """items: [{"id", "name", "unit", "type", "need", "quotes": [..]}] -> {id: text}. One call for all cards."""
    from common import llm
    tool = llm.tool("write_cards", "Write one pitch card per match, from its quotes only.", {
        "type": "object", "properties": {"cards": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "text": {"type": "string"}}, "required": ["id", "text"]}}}, "required": ["cards"]})
    lines = []
    for k, it in enumerate(items, 1):
        it = dict(it, id=f"card{k}")
        qs = " | ".join(f"“{q}”" for q in it["quotes"] if q)
        lines.append(f"id={it['id']} · {it['type']} · {it['name']} ({it['unit']}) · need: {it['need']} · quotes: {qs}")
    lang_name = {"de": "German", "fr": "French"}.get(lang, "English")
    r = llm.chat(llm.LARGE, [
        {"role": "system", "content": STYLE + f" Write in {lang_name}. The company: {company}. Say in one clause why it fits the company's need. "
         "Return exactly one card per id given (card1, card2, ...), with that id. Never state anything the quotes do not say."},
        {"role": "user", "content": "\n".join(lines)}],
        tools=[tool], tool_choice=llm.force("write_cards"), tag="app.pitch_cards", temperature=0.2)
    got = []
    if r["tool_calls"] and isinstance(r["tool_calls"][0]["arguments"], dict):
        got = [c for c in r["tool_calls"][0]["arguments"].get("cards", []) if isinstance(c, dict)]
    by_id = {str(c.get("id")): _clean(c.get("text")) for c in got}
    out = {}
    for k, it in enumerate(items, 1):
        t = by_id.get(f"card{k}") or by_id.get(str(k))
        if t is None and len(got) == len(items):
            t = _clean(got[k - 1].get("text"))
        if t:
            out[it["id"]] = t
    return out


def _card(r, n, section, text=None):
    return {"card_id": _cid(), "section": section, "entity_id": r.get("entity_id"), "type": ftype(r),
            "name": r.get("name", ""), "unit": r.get("unit", ""), "function": r.get("function", ""),
            "need_id": n["id"] if n else None, "need": n["text"] if n else "",
            "show_person_name": False, "text": text or _fallback_text(r, n), "sources": _src(r),
            "checked": False, "locked": False, "edited": False, "origin": "generated", "deleted": False}


def _write(cards, lead, lang):
    items = [{"id": c["card_id"], "need": c["need"], "type": "research group" if c["type"] == "person" else c["type"],
              "name": f"a research group at {c['unit'] or 'the University of Bern'}" if c["type"] == "person" and not c.get("show_person_name") else c["name"],
              "unit": c["unit"],
              "quotes": [s["quote"] for s in c["sources"]]} for c in cards if c["sources"]]
    if not items:
        return
    company = (lead.get("profile") or {}).get("product") or lead.get("summary") or lead.get("title")
    try:
        texts = _llm_cards(items, company, lang)
    except Exception:
        texts = {}
    for c in cards:
        t = texts.get(c["card_id"])
        if t and _ok(t):
            if not re.search(r"\[\d+\]", t):
                t += " [1]"
            c["text"] = t
            c["origin"] = "verified quote"
        else:
            c["origin"] = "verified quote"


def new_draft(lead, needs, accepted):
    needs_by = {n["id"]: n for n in needs}
    cards = []
    for d in accepted:
        r = d.get("match") or {}
        if not r:
            continue
        n = needs_by.get(d["need_id"])
        sec = "infra" if ftype(r) == "infrastructure" or (n and n["type"] == "infrastructure") else "matches"
        c = _card(r, n, sec)
        if d.get("added_by") and d["added_by"] != "officer":
            c["origin"] = "added by you"
        cards.append(c)
    _write(cards, lead, lead.get("lang") or "en")
    # Why Bern: up to three points, one per need, from the strongest accepted card
    why, seen, used = [], set(), set()
    for pass_ in (0, 1):  # one point per need first, then fill up to three
        for c in cards:
            if len(why) == 3 or not c["sources"] or c["card_id"] in used or (pass_ == 0 and c["need_id"] in seen):
                continue
            seen.add(c["need_id"])
            used.add(c["card_id"])
            why.append(dict(c, card_id=_cid(), section="why", entity_id=c["entity_id"], origin="verified quote"))
    draft = {"case_id": lead["id"], "sections": [dict(s, hidden=False) for s in SECTIONS], "cards": why + cards,
             "title": f"Bern for {(lead.get('profile') or {}).get('name') or lead['title']}",
             "intro": "", "how": HOW, "officer": {"name": "[Officer name]", "email": ""}, "lang": lead.get("lang") or "en",
             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "visit": visit_day(cards)}
    return draft


OLD_TITLES = {"Matches": "Research partners", "Infrastructure": "Labs, facilities and services",
              "Suggested visit day": "How to start"}


def upgrade(draft):
    """Drafts made before the why, what, how structure: default titles renamed, part and how text filled in.
    Titles the officer changed are kept."""
    parts = {s["id"]: s.get("part") for s in SECTIONS}
    for s in draft.get("sections", []):
        if s.get("title") in OLD_TITLES and s["id"] in ("matches", "infra", "visit"):
            s["title"] = OLD_TITLES[s["title"]]
        if parts.get(s["id"]) and not s.get("part"):
            s["part"] = parts[s["id"]]
    draft.setdefault("how", HOW)
    return draft


def add_cards(draft, lead, needs, accepted):
    needs_by = {n["id"]: n for n in needs}
    new = []
    for d in accepted:
        r = d.get("match") or {}
        n = needs_by.get(d["need_id"])
        sec = "infra" if ftype(r) == "infrastructure" else "matches"
        c = _card(r, n, sec)
        if d.get("added_by") and d["added_by"] != "officer":
            c["origin"] = "added by you"
        new.append(c)
    _write(new, lead, draft.get("lang") or "en")
    draft["cards"] += new
    draft["visit"] = visit_day([c for c in draft["cards"] if c["section"] in ("matches", "infra") and not c.get("deleted")])
    return draft


def visit_day(cards):
    slots = ["10:00", "11:00", "13:30", "14:30", "15:30", "16:30"]
    out = [{"time": "09:00", "what": "Welcome at the Bern Economic Development Agency", "status": "draft"}]
    for t, c in zip(slots, cards):
        who = c["name"] if c["type"] != "person" or c.get("show_person_name") else f"Group at {c['unit']}"
        out.append({"time": t, "what": who, "entity_id": c["entity_id"], "status": "draft, not yet asked"})
    return out


def rewrite(card, instruction, lang=None):
    from common import llm
    quotes = " | ".join(f"“{s['quote']}”" for s in card.get("sources", []) if s.get("quote"))
    instr = {"shorter": "Make it shorter, one sentence.", "jargon": "Use fewer technical terms, plain words for a CEO.",
             "de": "Write it in German (Swiss spelling, ss not ß).", "fr": "Write it in French.", "en": "Write it in English."}.get(
        instruction, instruction[:200])
    r = llm.chat(llm.LARGE, [
        {"role": "system", "content": STYLE + " Rewrite only from the quotes given. Keep the [n] markers. Return only the card text."},
        {"role": "user", "content": f"Card: {card['text']}\nQuotes: {quotes}\nInstruction: {instr}"}],
        tag="app.pitch_rewrite", temperature=0.2, max_tokens=200)
    t = _clean(r["content"]).strip('"“”')
    if not _ok(t):
        r = llm.chat(llm.LARGE, [
            {"role": "system", "content": STYLE + " Rewrite plainly, no marketing words. Return only the card text."},
            {"role": "user", "content": f"Card: {t}\nQuotes: {quotes}"}], tag="app.pitch_rewrite2", temperature=0)
        t = _clean(r["content"]).strip('"“”')
    if not _ok(t):
        raise ValueError("rewrite kept a banned word")
    if card.get("sources") and not re.search(r"\[\d+\]", t):
        t += " [1]"
    return t


PITCH_STYLE = ("You turn checked, sourced facts into a pitch letter from the Bern Economic Development Agency to a company. "
               "Address the company as 'you'. It reads as one argument: why Bern, then what Bern offers for each of the company's plans. "
               "Rewrite every card in your own words, never copy its current text. Each card becomes a paragraph of 3 to 5 sentences: "
               "start from the company's own need ('For your plan to ..., ...'), then say what the group does, what it has done "
               "(studies, methods, patient groups, countries, sizes) and how the company could work with it. "
               "A card in section why says why this matters for the company; a card in section matches or infra describes the group's work. "
               "Two cards never get the same text. Every fact comes from that card's page passages, close to their wording; "
               "do not guess what the group could do beyond what the passages say. No source markers. The intro only restates the company's own plans and says what the document lists; "
               "it never names a group and never states a fact about Bern.")


def _norm(t):
    t = re.sub(r"[#*_>`]", " ", t or "")
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t).strip().lower()


_CHUNKS = None


def _passage(url, quote, cap=2500):
    """The text around a quote on its crawled page: the chunk that holds it and the one after."""
    global _CHUNKS
    if _CHUNKS is None:
        import json
        from app.stubs import DATA
        _CHUNKS = {}
        p = DATA / "chunks.jsonl"
        if p.exists():
            with p.open() as f:
                for line in f:
                    try:
                        c = json.loads(line)
                    except ValueError:
                        continue
                    _CHUNKS.setdefault(c.get("url"), []).append((c.get("chunk_id") or "", c.get("text") or ""))
        for v in _CHUNKS.values():
            v.sort()
    chunks = [t for _, t in _CHUNKS.get(url) or []]
    if not chunks:
        return ""
    q = _norm(quote)[:80]
    i = next((k for k, t in enumerate(chunks) if q and q in _norm(t)), 0)
    return "\n".join(chunks[i:i + 2])[:cap]


def _deep(url, q):
    from urllib.parse import quote as enc
    w = q.split()
    frag = enc(q) if len(w) <= 8 else enc(" ".join(w[:4])) + "," + enc(" ".join(w[-4:]))
    return f"{url.split('#', 1)[0]}#:~:text={frag}"


_STOP = set("this that with from have their they them there these those which while where when what about into over also than then such each more most other only very will would could should been being were your yours does done make made uses used using across after before under between within study".split())


def _words(t):
    t = re.sub(r"[-‐‑‒–—]", " ", _norm(t))  # real-world and real‑world are the same words
    return {w for w in re.findall(r"[a-zà-ÿ0-9]{4,}", t) if w not in _STOP}


def _sentences(t):
    t = re.sub(r"^#+.*$", "", t or "", flags=re.M)
    return [x.strip() for x in re.split(r"(?<![\s(][A-Z]\.)(?<=[.!?])\s+|\n+", t) if len(x.strip()) >= 30]


def _ground(c, text, passages, need):
    """Cite the written card in code, sentence by sentence. Each sentence is matched with the sentences on the
    card's own pages: a close match becomes its source, quoted word for word; a sentence that only restates the
    company's need stays without one; anything else has nothing behind it and is dropped."""
    srcs = list(c.get("sources") or [])
    page = [(u, x) for u, t in passages.items() for x in _sentences(t)]
    page += [(x["url"], x["quote"]) for x in srcs if x.get("quote")]
    nw = _words(need)
    own = _words(" ".join(x for x in (c.get("name"), c.get("unit")) if x))  # the group's own name needs no source
    out = []
    for sent in re.split(r"(?<![\s(][A-Z]\.)(?<=[.?!])\s+(?=[A-ZÄÖÜÀ-Ý])", re.sub(r"\s*\[[^\]]{0,6}\]", "", text)):
        w = _words(sent) - own
        if not w:
            continue
        ranked = sorted(page, key=lambda p: -len(w & _words(p[1])))
        got, cov = [], set()
        for u, q in ranked[:3]:
            gain = (w & _words(q)) - cov
            if len(gain) >= 2:
                got.append((u, q))
                cov |= gain
        backed, restates = len(cov) >= 0.5 * len(w), len(w & nw) + len(cov) >= 0.6 * len(w)
        if backed or (restates and got):
            marks = []
            for u, q in got:
                i = next((k for k, x in enumerate(srcs) if _norm(x.get("quote")) == _norm(q)), None)
                if i is None:
                    fetched = next((x.get("fetched_at") for x in srcs if x.get("url") == u), None)
                    srcs.append({"url": u, "quote": q, "deep_link": _deep(u, q), "fetched_at": fetched, "from_page": True})
                    i = len(srcs) - 1
                if f"[{i + 1}]" not in marks:
                    marks.append(f"[{i + 1}]")
            out.append(sent.rstrip() + " " + "".join(marks))
        elif restates:
            out.append(sent.rstrip())
    return " ".join(out).strip(), srcs


def _written_ok(text, need_marker=False):
    return _ok(text) and (not need_marker or re.search(r"\[\d+\]", text))


def write_pitch(draft, lead, needs):
    """Fill the document once every card is checked: a cover opening and every card as a
    paragraph tied to the company's need, written from the passages around its quotes. How to start keeps its fixed
    wording, since it is where a promised meeting would slip in. Locked and hand-edited cards keep their words. Each
    field is checked like a card; a field that fails keeps what it had. Raises when the gateway does not answer."""
    from common import llm
    prof = lead.get("profile") or {}
    company = prof.get("name") or lead.get("title")
    hidden = {s["id"] for s in draft["sections"] if s.get("hidden") or s.get("deleted")}
    secs = [s for s in draft["sections"] if s["id"] in ("why", "matches", "infra") and s["id"] not in hidden]
    cards = [c for c in visible_cards(draft) if c["section"] in {s["id"] for s in secs}]
    free = [c for c in cards if not c.get("locked") and not c.get("edited")]
    ids = {f"card{k}": c for k, c in enumerate(free, 1)}
    secs = [s for s in secs if any(c["section"] == s["id"] for c in cards)]  # an empty section gets no opening to fill
    covered = {c.get("need_id") for c in cards}  # the intro may only mention needs this document answers
    passages = {}
    lines = [f"Company: {company}. What it makes: {prof.get('product') or ''}. About it: {prof.get('summary') or lead.get('summary') or ''}",
             "Its plans this document answers: " + "; ".join(n["text"] for n in needs if n.get("text") and n["id"] in covered),
             "Its plans the agency is still looking into: " + ("; ".join(n["text"] for n in needs if n.get("text") and n["id"] not in covered) or "none"), ""]
    for s in secs:
        lines.append(f"## Section {s['id']} · {s['title']}")
        for c in cards:
            if c["section"] != s["id"]:
                continue
            cid = next((k for k, v in ids.items() if v is c), None)
            who = c["name"] if c["type"] != "person" or c.get("show_person_name") else f"a research group at {c['unit'] or 'the University of Bern'}"
            if not cid:
                lines.append(f"- fixed, do not rewrite · {who}: {c['text']}")
                continue
            pg = passages.setdefault(cid, {})
            for x in c.get("sources") or []:
                if x.get("url") and x["url"] not in pg:
                    pg[x["url"]] = _passage(x["url"], x.get("quote")) or x.get("quote") or ""
            lines.append(f"- id={cid} · {who} · the company's need: {c.get('need') or '-'}\n  checked quotes: " +
                         " | ".join(f"“{x['quote']}”" for x in c.get("sources", []) if x.get("quote")) +
                         "".join(f"\n  page passage ({u}):\n  {t}" for u, t in pg.items()))
    tool = llm.tool("write_pitch", "Write the pitch text from the checked facts and their page passages.", {
        "type": "object", "properties": {
            "intro": {"type": "string", "description": "Cover opening, 3 to 5 sentences: what the company is building and looking for, "
                      "what this document lists, and which of its plans the agency is still looking into. No figures about the company."},
            "cards": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "text": {"type": "string", "description": "3 to 5 sentences."}},
                "required": ["id", "text"]}}},
        "required": ["intro", "cards"]})
    lang_name = {"de": "German", "fr": "French"}.get(draft.get("lang"), "English")
    style = STYLE.replace("Max 2 sentences per card. ", "").replace(" End each card with its source marker like [1].", "")
    msgs = [{"role": "system", "content": PITCH_STYLE + " " + style + f" Write in {lang_name}. "
             "Return one card per id given (card1, card2, ...); leave out cards marked fixed."},
            {"role": "user", "content": "\n".join(lines)}]
    got = None
    for temp in (0.3, 0.1):  # Apertus now and then runs on to the token limit and the call does not parse: once more, cooler
        r = llm.chat(llm.LARGE, msgs, tools=[tool], tool_choice=llm.force("write_pitch"), tag="app.pitch_write",
                     temperature=temp, max_tokens=3000)
        got = r["tool_calls"][0]["arguments"] if r["tool_calls"] else None
        if isinstance(got, dict) and got.get("cards"):
            break
    if not isinstance(got, dict):
        raise ValueError("Apertus returned no pitch text")
    changed = 0
    intro = _clean(got.get("intro"))
    if _written_ok(intro):
        draft["intro"] = intro
        changed += 1
    old = {_norm(re.sub(r"\[\d+\]", "", c["text"])) for c in cards}
    for it in got.get("cards") or []:
        c = ids.get(str((it or {}).get("id")).strip())
        if not c:
            continue
        t, srcs = _ground(c, _clean(it.get("text")), passages.get(next(k for k, v in ids.items() if v is c), {}), c.get("need") or "")
        if not _written_ok(t, need_marker=bool(srcs)) or _norm(re.sub(r"\[\d+\]", "", t)) in old:
            continue
        # the facts were checked before writing, and every new sentence is cited from its page in code: it stays checked
        c["text"], c["sources"], c["origin"] = t, srcs, "written from quotes"
        changed += 1
    if not changed:
        raise ValueError("No written text passed the checks")
    draft["written_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return draft


def visible_cards(draft):
    hidden = {s["id"] for s in draft["sections"] if s.get("hidden") or s.get("deleted")}
    order = [s["id"] for s in draft["sections"]]
    cards = [c for c in draft["cards"] if not c.get("deleted") and c["section"] not in hidden]
    return sorted(cards, key=lambda c: order.index(c["section"]) if c["section"] in order else 99)


def to_contract(draft, lead):
    """Pitch dict as in docs/contracts-extra.md (extra fields kept)."""
    prof = lead.get("profile") or {}
    cards = []
    for c in visible_cards(draft):
        text = c["text"]
        name = c["name"]  # pitch.build hides a person's name itself unless show_person_name is on
        cards.append({"entity_id": c.get("entity_id"), "type": c["type"], "name": name, "unit": c.get("unit", ""),
                      "person": c["name"] if c["type"] == "person" else None, "included": True,
                      "show_person_name": bool(c.get("show_person_name")), "text": text,
                      "sources": [{"url": s["url"], "quote": s.get("quote", ""), "deep_link": s.get("deep_link") or s["url"],
                                   "fetched_at": s.get("fetched_at"), "added_by_officer": bool(s.get("added_by_officer"))}
                                  for s in c.get("sources", [])],
                      "section": {"infra": "infrastructure"}.get(c["section"], c["section"]),
                      "edited": bool(c.get("edited")), "checked": bool(c.get("checked")),
                      "status": "edited" if c.get("edited") else ("verified" if c.get("sources") and all(s.get("quote") for s in c["sources"]) else "needs_check")})
    return {"case_id": lead["id"], "company": {"name": prof.get("name") or lead.get("company") or lead["title"],
                                               "country": prof.get("country", ""), "summary": prof.get("product") or lead.get("summary") or ""},
            "title": draft.get("title") or lead["title"], "intro": draft.get("intro", ""), "how": draft.get("how", HOW),
            "written": bool(draft.get("written_at")),
            "officer": draft.get("officer") or {"name": "", "email": ""}, "cards": cards,
            "sections": [s for s in draft["sections"] if not s.get("hidden") and not s.get("deleted")],
            "visit": draft.get("visit", []), "lang": draft.get("lang", "en"), "created_at": draft.get("created_at"),
            "visit_day": [{"time": v.get("time"), "topic": v.get("what"), "with": ""} for v in draft.get("visit", [])]
            if not any(s["id"] == "visit" and (s.get("hidden") or s.get("deleted")) for s in draft["sections"]) else []}
