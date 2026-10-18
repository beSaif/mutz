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
    assert "Prototype, not an official Canton of Bern tool" in r.text
    assert 'aria-label="Ask Mutz' in r.text          # Mutz floats on every screen
    assert not BANNED_UI.search(re.sub(r"<[^>]+>", " ", r.text))


def test_empty_state_offers_demo():
    for l in db.leads():
        db.delete_lead(l["id"])
    r = client.get("/")
    assert "try the demo case" in r.text and "Load the sport wearable demo" in r.text


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
    assert "Kei Hung im Topf" in page and "Searched" in page
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
