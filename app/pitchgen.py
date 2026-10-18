"""First pitch draft from accepted matches, per card rewrites, and conversion to the pitch contract.

Card text comes from Apertus 70B writing only from the card's own quotes, then is checked in code for
banned words and meeting promises; on any failure the card falls back to the verified quote itself."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from app.stubs import BANNED, PROMISE

SECTIONS = [
    {"id": "cover", "title": "Cover"},
    {"id": "why", "title": "Why Bern for you"},
    {"id": "matches", "title": "Matches"},
    {"id": "infra", "title": "Infrastructure"},
    {"id": "visit", "title": "Suggested visit day"},
    {"id": "sources", "title": "Sources", "auto": True},
]

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
    for it in items:
        qs = " | ".join(f"“{q}”" for q in it["quotes"] if q)
        lines.append(f"id={it['id']} · {it['type']} · {it['name']} ({it['unit']}) · need: {it['need']} · quotes: {qs}")
    lang_name = {"de": "German", "fr": "French"}.get(lang, "English")
    r = llm.chat(llm.LARGE, [
        {"role": "system", "content": STYLE + f" Write in {lang_name}. The company: {company}. Say in one clause why it fits the company's need."},
        {"role": "user", "content": "\n".join(lines)}],
        tools=[tool], tool_choice=llm.force("write_cards"), tag="app.pitch_cards", temperature=0.2)
    out = {}
    if r["tool_calls"] and isinstance(r["tool_calls"][0]["arguments"], dict):
        for c in r["tool_calls"][0]["arguments"].get("cards", []):
            out[str(c.get("id"))] = _clean(c.get("text"))
    return out


def _card(r, n, section, text=None):
    return {"card_id": _cid(), "section": section, "entity_id": r.get("entity_id"), "type": ftype(r),
            "name": r.get("name", ""), "unit": r.get("unit", ""), "function": r.get("function", ""),
            "need_id": n["id"] if n else None, "need": n["text"] if n else "",
            "show_person_name": False, "text": text or _fallback_text(r, n), "sources": _src(r),
            "checked": False, "locked": False, "edited": False, "origin": "generated", "deleted": False}


def _write(cards, lead, lang):
    items = [{"id": c["card_id"], "name": c["name"], "unit": c["unit"], "type": c["type"], "need": c["need"],
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
             "intro": "", "officer": {"name": "[Officer name]", "email": ""}, "lang": lead.get("lang") or "en",
             "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "visit": visit_day(cards)}
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
        if c["type"] == "person" and not c.get("show_person_name"):
            name = f"Group at {c['unit']}" if c.get("unit") else "Research group"
        else:
            name = c["name"]
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
            "title": draft.get("title") or lead["title"], "intro": draft.get("intro", ""),
            "officer": draft.get("officer") or {"name": "", "email": ""}, "cards": cards,
            "sections": [s for s in draft["sections"] if not s.get("hidden") and not s.get("deleted")],
            "visit": draft.get("visit", []), "lang": draft.get("lang", "en"), "created_at": draft.get("created_at"),
            "visit_day": [{"time": v.get("time"), "topic": v.get("what"), "with": ""} for v in draft.get("visit", [])]
            if not any(s["id"] == "visit" and (s.get("hidden") or s.get("deleted")) for s in draft["sections"]) else []}
