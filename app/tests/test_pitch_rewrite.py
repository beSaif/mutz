"""Bärner Mutz rewrites the pitch: run from the repo root with  python -m pytest app/tests -q
They use a temporary app DB and force the stubs, so no gateway call is needed."""
import os
import re
import tempfile

os.environ.setdefault("MUTZ_APP_DB", os.path.join(tempfile.mkdtemp(), "app.sqlite"))
os.environ["MUTZ_STUB"] = "query,why_not"

import pytest
from fastapi.testclient import TestClient

from app import contracts, db, mutz, pitchgen
from app.main import app

client = TestClient(app)


def marks(t):
    return re.findall(r"\[(\d+)\]", t or "")


@pytest.fixture
def pitch_lead(monkeypatch):
    """A lead with three accepted matches and a pitch draft in English; the gateway is never asked."""
    monkeypatch.setattr(pitchgen, "_llm_cards", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no gateway in tests")))
    monkeypatch.setattr(mutz, "_llm_answer", lambda *a, **k: (_ for _ in ()).throw(AssertionError("routed through the model")))
    lid = db.create_lead("Rewrite lead", origin="S-GE")
    client.post(f"/leads/{lid}/brief", data={"mode": "keywords", "keywords": "type 1 diabetes exercise study"}, follow_redirects=False)
    from app import main
    main._search_job(lid, None)
    n = db.needs(lid)[0]
    for m in n["result"]["results"][:3]:
        client.post("/api/decide", json={"lead": lid, "need": n["id"], "entity": m["entity_id"], "decision": "accept"})
    assert client.get(f"/leads/{lid}/pitch").status_code == 200
    assert client.post(f"/api/pitch/{lid}/build").json()["ok"]
    draft, _ = db.pitch_draft(lid)
    for c in draft["cards"]:
        c["checked"] = True
    db.save_pitch(lid, draft)
    return lid


def say(lid, message):
    return client.post("/api/mutz", json={"key": "rw-" + lid, "message": message, "ctx": {"screen": "Pitch", "lead": lid}}).json()


def test_whole_pitch_to_german_on_the_stub(pitch_lead, monkeypatch):
    lid = pitch_lead
    monkeypatch.setattr(contracts, "FORCE", contracts.FORCE | {"rewrite_pitch"})
    before, _ = db.pitch_draft(lid)
    assert before["lang"] == "en"
    r = say(lid, "change the whole pitch to german")
    assert r["reload"] and r["text"] == "Done. The pitch is now in German. Citations kept."
    assert not r["actions"] and "?" not in r["text"]                       # no confirmation, no "which sections"
    d, _ = db.pitch_draft(lid)
    assert d["lang"] == "de" and db.lead(lid)["lang"] == "de"              # the document's fixed words follow
    assert [s["title"] for s in d["sections"]] == ["Deckblatt", "Warum Bern für Sie", "Forschungspartner",
                                                   "Einrichtungen, Förderung und Dienstleistungen", "Erste Schritte", "Quellen"]
    assert d["title"] == "Bern für Rewrite lead" and d["how"].startswith("Noch ist nichts vereinbart")
    assert d["visit"][0]["what"].startswith("Empfang bei")
    old = {c["card_id"]: c for c in before["cards"]}
    texts = [c for c in d["cards"] if c["text"] != old[c["card_id"]]["text"]]
    assert texts and any(re.search(r"\b(und|mit|die|für)\b", c["text"]) for c in texts)
    for c in d["cards"]:
        assert marks(c["text"]) == marks(old[c["card_id"]]["text"])       # same citations, same order
        assert c["sources"] == old[c["card_id"]]["sources"] and c["checked"]
    page = client.get(f"/leads/{lid}/pitch").text
    assert "Warum Bern f\\u00fcr Sie" in page and '"lang": "de"' in page             # the draft as the editor gets it

    r = say(lid, "all of them")                                             # the follow up does not fail either
    assert r["text"] == "The pitch is already in German." and not r.get("reload")
    r = say(lid, "back to English")
    assert r["reload"] and db.pitch_draft(lid)[0]["lang"] == "en"
    assert db.pitch_draft(lid)[0]["sections"][1]["title"] == "Why Bern for you"


def test_failed_check_keeps_the_old_draft(pitch_lead, monkeypatch):
    lid = pitch_lead
    from common import llm
    before, _ = db.pitch_draft(lid)

    def answer(drop=False, stray=False, word=None):
        def fake(model, messages, **kw):
            ids = re.findall(r"^id=(\S+): ", messages[-1]["content"], re.M)
            out = []
            for i in ids:
                t = re.search(rf"^id={re.escape(i)}: (.*)$", messages[-1]["content"], re.M).group(1)
                if drop:
                    t = re.sub(r"\s*\[\d+\]", "", t)                        # loses the markers
                if stray:
                    t = f"[7] {t}"                                          # a marker the old text did not have
                if word:
                    t = f"{word} {t}"
                out.append({"id": i, "text": "Tradotto: " + t})
            return {"content": "", "tool_calls": [{"name": "rewrite_fields", "arguments": {"fields": out}}]}
        return fake

    monkeypatch.setattr(llm, "chat", answer(stray=True))
    r = say(lid, "traduci il pitch in italiano")
    assert r["text"] == "I kept the old draft: the new text lost or moved a citation." and not r.get("reload")
    assert db.pitch_draft(lid)[0] == before and db.lead(lid)["lang"] == "en"

    monkeypatch.setattr(llm, "chat", answer(word="Innovative"))
    r = say(lid, "in italiano")
    assert "marketing word" in r["text"] and db.pitch_draft(lid)[0] == before

    monkeypatch.setattr(llm, "chat", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gateway down")))
    r = say(lid, "make it more formal")
    assert "unchanged" in r["text"] and db.pitch_draft(lid)[0] == before

    monkeypatch.setattr(llm, "chat", answer(drop=True))
    r = say(lid, "in italiano")                                              # the same request, checks pass: the
                                                                             # trailing markers it dropped go back
    d = db.pitch_draft(lid)[0]
    assert r["reload"] and d["lang"] == "it" and d["sections"][1]["title"] == "Perché Berna per Lei"
    assert all(marks(c["text"]) == marks(o["text"]) for c, o in zip(d["cards"], before["cards"]))


@pytest.mark.parametrize("msg,want", [
    ("change the whole pitch to german", True), ("auf Deutsch bitte", True), ("Übersetze den Pitch ins Deutsche", True),
    ("in italiano", True), ("traduci in tedesco", True), ("più breve", True), ("kürzer", True), ("make it more formal", True),
    ("Can you translate the pitch to French?", True), ("Is the intro in German?", False),
    ("who in Bern works on German sensors?", False), ("find a German partner", False), ("summarise this lead", False)])
def test_rewrite_requests_route_to_the_tool(msg, want):
    assert bool(mutz.rewrite_request({"screen": "Pitch", "lead": "x"}, [], msg)) is want
    assert mutz.rewrite_request({"screen": "Matches", "lead": "x"}, [], msg) is None   # only on the Pitch screen


def test_follow_up_repeats_the_request_for_the_whole_pitch():
    h = [{"role": "user", "text": "translate the intro to german"}, {"role": "mutz", "text": "Which sections?"}]
    for m in ("all of them", "Alle", "tutte", "yes"):
        assert mutz.rewrite_request({"screen": "Pitch", "lead": "x"}, h, m) == {"instruction": "translate the intro to german", "sections": None}
    assert mutz.rewrite_request({"screen": "Pitch", "lead": "x"}, h, "thanks") is None
    assert mutz.rewrite_request({"screen": "Pitch", "lead": "x"}, [], "make the intro shorter")["sections"] == ["cover"]
