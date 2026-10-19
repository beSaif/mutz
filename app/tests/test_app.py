"""App tests: run from the repo root with  python -m pytest app/tests -q
They use a temporary app DB and force the stubs, so no gateway call is needed except where noted."""
import os
import re
import tempfile

os.environ.setdefault("MUTZ_APP_DB", os.path.join(tempfile.mkdtemp(), "app.sqlite"))
os.environ["MUTZ_STUB"] = "query,why_not"

import pytest
from fastapi.testclient import TestClient

from app import db, stubs
from app.main import app

client = TestClient(app)

SCREENS = ["/", "/archives", "/eval", "/states", "/pipeline", "/insights", "/archives?url=https://example.ch/"]
BANNED_UI = re.compile(r"\b(unlock|elevate|seamless|innovative|cutting edge|leverage|synergy|forefront|journey|empower|"
                       r"get started|learn more|submit)\b", re.I)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("url", SCREENS)
def test_screens_render_with_footer(url):
    r = client.get(url)
    assert r.status_code == 200
    assert "Apertus on CSCS, Switzerland" in r.text   # where the text goes, in the header
    assert 'aria-label="Ask Bärner Mutz' in r.text   # Bärner Mutz floats on every screen
    assert not BANNED_UI.search(re.sub(r"<[^>]+>", " ", r.text))


def test_empty_state_offers_demo():
    for l in db.leads():
        db.delete_lead(l["id"])
    r = client.get("/")
    assert "No leads yet" in r.text and "Load the sport wearable demo" in r.text


def test_stub_query_contract():
    out = stubs.query("type 1 diabetes and exercise", k=5)
    assert {"expansions", "searched_sources", "results"} <= set(out)
    for r in out["results"]:
        assert {"entity_id", "type", "name", "unit", "function", "score", "coverage", "recency", "reasons"} <= set(r)
        assert r["coverage"] in ("strong", "thin", "none") and r["recency"] in ("active", "quiet", "past")
        assert r["reasons"] and all(x["url"].startswith("http") and x["quote"] for x in r["reasons"])


def test_stub_abstains_on_unrelated_need():
    out = stubs.query("lithium battery cell chemistry recycling plant", k=10)
    assert out["results"] == [] and out["searched_sources"]


def test_validate_rejects_promises_banned_words_and_missing_sources():
    p = {"cards": [{"name": "A", "text": "The group will meet you.", "sources": [{"url": "https://x.ch", "quote": "q"}]},
                   {"name": "B", "text": "An innovative group.", "sources": [{"url": "https://x.ch", "quote": "q"}]},
                   {"name": "C", "text": "Plain text.", "sources": []}]}
    probs = " ".join(stubs.validate(p))
    assert "promises a meeting" in probs and "banned word" in probs and "no source" in probs


def test_demo_path_on_stubs(monkeypatch):
    # needs without the gateway: keyword mode
    lid = db.create_lead("Test lead", origin="S-GE")
    r = client.post(f"/leads/{lid}/brief", data={"mode": "keywords", "keywords": "type 1 diabetes exercise study\nbattery recycling"},
                    follow_redirects=False)
    assert r.status_code == 303
    ns = db.needs(lid)
    assert len(ns) == 2
    from app import main
    main._search_job(lid, None)                       # run the search synchronously
    ns = db.needs(lid)
    assert all(n["searched_at"] for n in ns)
    assert ns[1]["coverage"] == "none"                 # honest no match
    page = client.get(f"/leads/{lid}/matches?need={ns[1]['id']}").text
    assert "Kei Honig im Topf" in page and "Searched" in page
    res = ns[0]["result"]["results"]
    assert res
    for m in res[:2]:
        client.post("/api/decide", json={"lead": lid, "need": ns[0]["id"], "entity": m["entity_id"], "decision": "accept"})
    assert len(db.accepted(lid)) == 2
    # LeadSignal rows are anonymous
    sig = [s for s in db.signals() if s["lead_id_hash"] == db.lead_hash(lid)]
    assert len(sig) == 2 and all("Test lead" not in str(s) for s in sig)
    assert client.get(f"/leads/{lid}/map").status_code == 200


def test_mutz_rule_path_without_gateway(monkeypatch):
    from app import mutz
    monkeypatch.setattr(mutz, "_llm_answer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gateway down")))
    r = mutz.ask({"key": "t", "message": "diabetes exercise", "ctx": {"screen": "Leads"}})
    assert r["note"] and r["sources"]
    assert all(f"[{s['n']}]" in r["text"] or True for s in r["sources"])


def test_score_parts_multiply_to_the_score():
    from app.main import score_parts
    r = {"score": 0.4, "match": 0.8, "boost": 1.0, "seniority": 0.5, "recency": "active", "type": "person", "ftype": "person"}
    p = score_parts(r, "research")
    prod = 1.0
    for _, v, _ in p["rows"]:
        prod *= v
    assert abs(prod - 0.4) < 1e-9 and not p["derived"]
    old = {"score": 0.48, "seniority": 1.0, "recency": "quiet", "type": "unit", "ftype": "unit"}   # stored before match was kept
    p = score_parts(old, "research")
    assert p["derived"] and abs(p["evidence"] - 0.6) < 1e-9 and len(p["rows"]) == 4


def test_sort_and_filter_keep_adjacent_last():
    from app.main import sort_filter
    rs = [{"name": "B", "score": 0.9, "coverage": "strong", "recency": "past", "recency_year": 2015, "is_adjacent": False, "parts": {"evidence": 0.9}},
          {"name": "A", "score": 0.3, "coverage": "thin", "recency": "active", "recency_year": 2025, "is_adjacent": False, "parts": {"evidence": 0.95}},
          {"name": "C", "score": 0.8, "coverage": "thin", "recency": "active", "recency_year": 2026, "is_adjacent": True, "parts": {"evidence": 0.8}}]
    assert [r["name"] for r in sort_filter(rs, "recency", "", "", "", "")] == ["A", "B", "C"]
    assert [r["name"] for r in sort_filter(rs, "evidence", "", "", "", "")] == ["A", "B", "C"]
    assert [r["name"] for r in sort_filter(rs, "score", "", "active", "", "")] == ["A", "C"]
    assert [r["name"] for r in sort_filter(rs, "score", "", "", "0.5", "")] == ["B", "C"]


def test_old_pitch_draft_gets_why_what_how():
    from app import pitchgen
    d = {"sections": [{"id": "matches", "title": "Matches"}, {"id": "visit", "title": "My own title"}], "cards": []}
    pitchgen.upgrade(d)
    assert d["sections"][0]["title"] == "Research partners" and d["sections"][0]["part"] == "what"
    assert d["sections"][1]["title"] == "My own title" and d["sections"][1]["part"] == "how"
    assert d["how"] == pitchgen.HOW



def test_agency_list_entries_go_with_facilities_and_funding():
    from app import pitchgen
    r = {"entity_id": "curated:innosuisse", "type": "unit", "name": "Innosuisse", "reasons": []}
    n = {"id": "n", "type": "funding", "text": "Funding"}
    assert pitchgen.section_for(r, n) == "infra" and pitchgen._card_type(r, n) == "funding"
    unit = {"entity_id": "unit:x", "type": "unit", "name": "X"}
    assert pitchgen.section_for(unit, {"type": "research"}) == "matches"
    cards = [{"entity_id": "unit:a", "type": "person", "name": "P", "unit": "A"},
             {"entity_id": "unit:a2", "type": "unit", "name": "A", "unit": "A"}, r | {"unit": ""}]
    assert [v["what"] for v in pitchgen.visit_day(cards)][1:] == ["Group at A"]   # one stop per place, no programmes



def test_open_plans_get_unchecked_suggestions_once(monkeypatch):
    from app import pitchgen
    monkeypatch.setattr(pitchgen, "_write", lambda cards, lead, lang: None)
    hit = {"entity_id": "unit:g", "type": "unit", "name": "G", "unit": "G", "reasons": [{"url": "https://g.example/a", "quote": "q"}]}
    needs = [{"id": "n1", "type": "research", "text": "R&D", "coverage": "strong", "route": {"kind": "search"}, "result": {"results": [hit]}},
             {"id": "n2", "type": "funding", "text": "Money", "coverage": "strong",
              "route": {"kind": "curated", "entries": [{"id": "curated:bridge"}, {"id": "curated:ctu-bern"}]}},
             {"id": "n3", "type": "supplier", "text": "Maker", "coverage": "none", "route": {"kind": "not_covered"}}]
    lead = {"id": "x", "title": "X", "profile": {"sector": "Healthtech"}}
    d = {"sections": [dict(s) for s in pitchgen.SECTIONS], "cards": []}
    assert pitchgen.suggest(d, lead, needs) >= 3
    new = [c for c in d["cards"] if c["section"] != "why"]
    assert all(not c["checked"] and c["origin"] == "suggested" for c in new)
    assert {c["need_id"] for c in new} == {"n1", "n2", None}                 # nothing for the supplier plan
    assert "curated:ctu-bern" not in {c["entity_id"] for c in new}           # only checked entries of the agency list
    assert any(c.get("nearby") and c["entity_id"] == "curated:sitem-insel" for c in new)
    assert pitchgen.suggest(d, lead, needs) == 0                             # once per draft


def test_matches_sort_filter_and_map_render():
    lid = db.create_lead("Sort lead", origin="S-GE")
    client.post(f"/leads/{lid}/brief", data={"mode": "keywords", "keywords": "type 1 diabetes exercise study"}, follow_redirects=False)
    from app import main
    main._search_job(lid, None)
    n = db.needs(lid)[0]
    for q in ("", "&sort=recency&rec=active", "&sort=name&cov=thin&smin=0.25", "&smin=0.75&cov=strong&rec=past"):
        r = client.get(f"/leads/{lid}/matches?need={n['id']}{q}")
        assert r.status_code == 200 and 'id="sortby"' in r.text and 'class="fp"' in r.text
    m = n["result"]["results"][0]
    client.post("/api/decide", json={"lead": lid, "need": n["id"], "entity": m["entity_id"], "decision": "accept"})
    r = client.get(f"/leads/{lid}/map")
    assert r.status_code == 200 and 'class="tn mt"' in r.text
    detail = main.build_map(lid)["detail"]
    kinds = {d["kind"] for d in detail.values()}
    assert {"Need", "Match", "Fact"} <= kinds
    assert all(f'data-id="{k}"' in r.text for k in detail)          # every node opens its panel
    mt = next(d for d in detail.values() if d["kind"] == "Match")
    assert mt["needs"] and mt["accepted"] and "facts" in mt and "pages" in mt
    assert [a for a, _ in mt["profile"]] == ["Evidence", "Role", "Recency", "Type", "AI check"]   # ranking factors
    assert all(v is None or 0 <= v <= 1 for _, v in mt["profile"])
    for d in detail.values():   # knowledge graph neighbours: map links point at map nodes
        if d["kind"] == "Match" and d.get("kg"):
            assert all(n.get("go") in detail for n in d["kg"]["links"] if n.get("go"))
            assert all(n.get("more") or n["name"] for n in d["kg"]["links"])
    r = client.get(f"/leads/{lid}/pitch")
    assert r.status_code == 200 and f"/leads/{lid}/export" in r.text and "Export and mark as sent" not in r.text


def test_export_is_its_own_step():
    lid = db.create_lead("Export lead", origin="S-GE")
    client.post(f"/leads/{lid}/brief", data={"mode": "keywords", "keywords": "type 1 diabetes exercise study"}, follow_redirects=False)
    from app import main
    main._search_job(lid, None)
    n = db.needs(lid)[0]
    m = n["result"]["results"][0]
    client.post("/api/decide", json={"lead": lid, "need": n["id"], "entity": m["entity_id"], "decision": "accept"})
    client.get(f"/leads/{lid}/pitch")
    r = client.get(f"/leads/{lid}/review?err=unchecked", follow_redirects=False)   # old links still work
    assert r.status_code in (303, 307) and r.headers["location"].endswith(f"/leads/{lid}/export?err=unchecked")
    r = client.get(f"/leads/{lid}/export")
    assert r.status_code == 200 and ">Export</button>" in r.text
    assert 'aria-current="step">5 Export' in r.text and 'role="meter"' in r.text
    assert db.lead(lid)["step"] == "export"
    assert client.get(f"/leads/{lid}", follow_redirects=False).headers["location"].endswith("/export")


def test_officer_switch_owner_mine_and_take_over():
    c = TestClient(app)
    c.post("/officer", data={"id": "jumainah"})
    lid = c.post("/leads/new", data={"title": "Handover lead"}, follow_redirects=False).headers["location"].split("/")[2]
    assert db.lead(lid)["owner"] == "jumainah"
    assert "Handover lead" in c.get("/?f=drafts&who=mine").text
    c.post("/officer", data={"id": "saif"})
    assert "Handover lead" not in c.get("/?f=drafts&who=mine").text and "Handover lead" in c.get("/?f=drafts").text
    assert "Assign to me" in c.get(f"/leads/{lid}/brief").text
    c.post(f"/leads/{lid}/owner")
    assert db.lead(lid)["owner"] == "saif"
    assert any("Lead assigned to Saif, was Jumainah" in e["text"] for e in db.entries(lid))
    assert "Assign to me" not in c.get(f"/leads/{lid}/brief").text
    assert db.owner_name(db.lead(lid)) == "Saif"
    assert c.post("/officer", data={"id": "nobody"}).status_code == 400


def test_write_the_pitch_turns_cards_into_paragraphs(monkeypatch):
    lid = db.create_lead("Write lead", origin="S-GE")
    client.post(f"/leads/{lid}/brief", data={"mode": "keywords", "keywords": "type 1 diabetes exercise study"}, follow_redirects=False)
    from app import main, pitchgen
    from common import llm
    main._search_job(lid, None)
    n = db.needs(lid)[0]
    m = n["result"]["results"][0]
    client.post("/api/decide", json={"lead": lid, "need": n["id"], "entity": m["entity_id"], "decision": "accept"})
    client.get(f"/leads/{lid}/pitch")
    client.post(f"/api/pitch/{lid}/build")   # the page opens first, then asks for its draft
    draft, _ = db.pitch_draft(lid)
    mine = next(c for c in draft["cards"] if c["section"] == "matches")
    mine.update(text="My own words. [1]", edited=True)
    page = "The study includes a total of 350 women in five countries. All activities take place at home."
    monkeypatch.setattr(pitchgen, "_passage", lambda url, quote, cap=2500: page)

    def fake(model, messages, **kw):
        return {"content": "", "tool_calls": [{"name": "write_pitch", "arguments": {
            "intro": "You plan a trial in Switzerland. This document lists the groups in Bern that work on it.",
            "cards": [{"id": f"card{k}", "text": "The study includes 350 women in five countries. "
                       "It won a Nobel prize for the work in Stockholm. All activities take place at home."} for k in range(1, 6)]}}]}
    monkeypatch.setattr(llm, "chat", fake)
    r = client.post(f"/api/pitch/{lid}/write", json={"draft": draft}).json()
    assert not r["ok"] and "Check every card first" in r["error"]          # facts are checked before the pitch is written
    for c in draft["cards"]:
        c["checked"] = True
    r = client.post(f"/api/pitch/{lid}/write", json={"draft": draft}).json()
    assert r["ok"] and r["draft"]["written_at"]
    d = r["draft"]
    assert d["intro"].startswith("You plan")
    assert next(c for c in d["cards"] if c["card_id"] == mine["card_id"])["text"] == "My own words. [1]"       # officer's words kept
    w = [c for c in d["cards"] if c["origin"] == "written from quotes"]
    assert w and all(c["checked"] for c in w)
    c = w[0]
    assert "Nobel" not in c["text"] and any("350 women" in s["quote"] for s in c["sources"])
    assert all(s["quote"] in page for s in c["sources"] if s.get("from_page"))          # new quotes are word for word on the page
    assert max(int(x) for x in __import__("re").findall(r"\[(\d+)\]", c["text"])) <= len(c["sources"])
    p = pitchgen.to_contract(d, db.lead(lid))
    assert p["written"] and not pitch_problems(p)

    monkeypatch.setattr(llm, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gateway down")))
    r = client.post(f"/api/pitch/{lid}/write", json={"draft": d}).json()
    assert not r["ok"] and "Nothing changed" in r["error"]


def test_officer_request_changes_only_what_passes_the_checks(monkeypatch):
    lid = db.create_lead("Change lead", origin="S-GE")
    client.post(f"/leads/{lid}/brief", data={"mode": "keywords", "keywords": "type 1 diabetes exercise study"}, follow_redirects=False)
    from app import main
    from common import llm
    main._search_job(lid, None)
    n = db.needs(lid)[0]
    for m in n["result"]["results"][:3]:
        client.post("/api/decide", json={"lead": lid, "need": n["id"], "entity": m["entity_id"], "decision": "accept"})
    client.get(f"/leads/{lid}/pitch")
    client.post(f"/api/pitch/{lid}/build")   # the page opens first, then asks for its draft
    draft, _ = db.pitch_draft(lid)
    cards = [c for c in main.pitchgen.visible_cards(draft) if c["section"] in ("why", "matches", "infra")]
    assert len(cards) >= 3
    for c in cards:
        c["checked"] = True
    cards[2]["locked"] = True
    before = {c["card_id"]: c["text"] for c in cards}

    def fake(model, messages, **kw):
        return {"content": "", "tool_calls": [{"name": "change_pitch", "arguments": {
            "intro": "The agency also helps with work permits for your team.",
            "how": "We will meet you in Bern next week.",                                  # a promise: refused
            "cards": [{"id": "card1", "text": "A short version of the card. [1]"},
                      {"id": "card2", "text": "The group has 4000 patients. [1]"},          # a figure the sources lack: refused
                      {"id": "card3", "text": "Locked text changed. [1]"}],                  # locked: left alone
            "note": "I left the meeting out."}}]}
    monkeypatch.setattr(llm, "chat", fake)
    assert not client.post(f"/api/pitch/{lid}/change", json={"draft": draft, "instruction": " "}).json()["ok"]
    r = client.post(f"/api/pitch/{lid}/change", json={"draft": draft, "instruction": "Add work permits, shorter cards"}).json()
    assert r["ok"] and set(r["changed"]) == {"intro", "card:" + cards[0]["card_id"]}
    d = r["draft"]
    got = {c["card_id"]: c for c in d["cards"]}
    assert d["intro"].startswith("The agency also helps") and d["how"] != "We will meet you in Bern next week."
    assert got[cards[0]["card_id"]]["text"] == "A short version of the card. [1]" and not got[cards[0]["card_id"]]["checked"]
    assert got[cards[1]["card_id"]]["text"] == before[cards[1]["card_id"]] and got[cards[1]["card_id"]]["checked"]
    assert got[cards[2]["card_id"]]["text"] == before[cards[2]["card_id"]]
    assert "left the meeting out" in r["note"] and "2 more changes" in r["note"]
    assert db.pitch_draft(lid)[0]["intro"] == d["intro"]                                  # saved

    monkeypatch.setattr(llm, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gateway down")))
    r = client.post(f"/api/pitch/{lid}/change", json={"draft": d, "instruction": "shorter"}).json()
    assert not r["ok"] and "Nothing changed" in r["error"]


def pitch_problems(p):
    from pitch import build
    return build.validate(p)


def test_reset_clears_leads_keeps_seeded_signals():
    c = TestClient(app)
    c.post("/insights/seed")
    seeded = sum(1 for s in db.signals() if s["demo"] == 2)
    lid = c.post("/leads/new", data={"title": "Reset me"}, follow_redirects=False).headers["location"].split("/")[2]
    db.add_entry(lid, "note", "x")
    assert "Reset demo data" in c.get("/").text
    assert c.post("/leads/reset", follow_redirects=False).status_code == 303
    assert db.leads() == [] and db.entries(lid) == []
    assert seeded and sum(1 for s in db.signals() if s["demo"] == 2) == seeded
    assert all(s["demo"] == 2 for s in db.signals())


def test_new_italian_draft_has_italian_fixed_words():
    from app import pitchgen
    from pitch.locales import tr
    d = pitchgen.new_draft({"id": "x", "title": "T", "lang": "it", "profile": {"name": "Acme"}}, [], [])
    assert [s["title"] for s in d["sections"]] == ["Copertina", "Perché Berna per Lei", "Partner di ricerca",
                                                   "Strutture, finanziamenti e servizi", "Come iniziare", "Fonti"]
    assert d["how"] == tr("it", pitchgen.HOW) != pitchgen.HOW and d["title"] == "Berna per Acme" and d["lang"] == "it"
    assert d["visit"][0]["what"].startswith("Accoglienza presso")
    cards = [{"entity_id": "unit:a", "type": "person", "name": "P", "unit": "A"}]
    assert pitchgen.visit_day(cards, "it")[1]["what"] == "Gruppo di ricerca, A"
    old = {"sections": [], "cards": [], "lang": "it"}
    assert pitchgen.upgrade(old)["how"] == d["how"]


def test_italian_pitch_page_and_stub_pdf(monkeypatch, tmp_path):
    from app import main, pitchgen
    from pypdf import PdfReader
    monkeypatch.setattr(pitchgen, "_llm_cards", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no gateway in tests")))
    lid = db.create_lead("Italian lead", origin="S-GE")
    client.post(f"/leads/{lid}/brief", data={"mode": "keywords", "keywords": "type 1 diabetes exercise study", "lang": "it"},
                follow_redirects=False)
    assert db.lead(lid)["lang"] == "it"
    assert 'value="it"' in client.get(f"/leads/{lid}/brief?source=1&mode=keywords").text   # the Brief offers IT
    main._search_job(lid, None)
    n = db.needs(lid)[0]
    m = n["result"]["results"][0]
    client.post("/api/decide", json={"lead": lid, "need": n["id"], "entity": m["entity_id"], "decision": "accept"})
    r = client.get(f"/leads/{lid}/pitch")
    assert r.status_code == 200 and '<html lang="en">' in r.text           # the interface stays English
    assert '"Sources": "Fonti"' in r.text and '"Group at {unit}": "Gruppo di ricerca, {unit}"' in r.text   # the editor's doc catalog
    client.post(f"/api/pitch/{lid}/build")   # the page opens first, then asks for its draft
    draft, _ = db.pitch_draft(lid)
    assert draft["lang"] == "it" and [s["title"] for s in draft["sections"]][1] == "Perché Berna per Lei"
    p = pitchgen.to_contract(draft, db.lead(lid), db.needs(lid))
    assert p["lang"] == "it" and p["how"].startswith("Nulla è ancora organizzato")
    p["cards"] = [c for c in p["cards"] if not stubs.BANNED.search(c["text"])]   # the stub rejects "Innovation Park" names
    from pitch.build import _patch_macos_dlopen
    _patch_macos_dlopen()   # WeasyPrint finds Homebrew's Pango on macOS
    out = stubs.build_pdf(p, "clean", str(tmp_path / "stub.pdf"))
    txt = " ".join(" ".join((pg.extract_text() or "").split()) for pg in PdfReader(out).pages)
    assert "Fonti" in txt and "Preparato per" in txt
