"""Pitch page: it opens at once and drafts its facts in the background, with a wait panel meanwhile.
Run from the repo root with  python -m pytest app/tests -q  (temporary app DB, stubs forced)."""
import json
import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("MUTZ_APP_DB", os.path.join(tempfile.mkdtemp(), "app.sqlite"))
os.environ["MUTZ_STUB"] = "query,why_not"

from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)
BANNED_UI = re.compile(r"\b(unlock|elevate|seamless|innovative|cutting edge|leverage|synergy|forefront|journey|empower|"
                       r"get started|learn more|submit)\b", re.I)
APP = Path(__file__).resolve().parents[1]


def _lead(title, accept=1):
    lid = db.create_lead(title, origin="S-GE")
    client.post(f"/leads/{lid}/brief", data={"mode": "keywords", "keywords": "type 1 diabetes exercise study"}, follow_redirects=False)
    from app import main
    main._search_job(lid, None)
    n = db.needs(lid)[0]
    for m in n["result"]["results"][:accept]:
        client.post("/api/decide", json={"lead": lid, "need": n["id"], "entity": m["entity_id"], "decision": "accept"})
    return lid, n


def test_pitch_opens_before_its_draft_and_builds_it_on_request():
    lid, _n = _lead("Wait lead")
    r = client.get(f"/leads/{lid}/pitch")
    assert r.status_code == 200 and db.pitch_draft(lid)[0] is None           # nothing built in the page request
    assert 'class="pn pwait" id="pwait"' in r.text and "Reading the matches and pulling out the facts…" in r.text
    assert '"pending": ["read", "write", "suggest"]' in r.text and "initPitch(" in r.text
    assert not BANNED_UI.search(re.sub(r"<[^>]+>", " ", r.text))
    b = client.post(f"/api/pitch/{lid}/build").json()
    assert b["ok"] and b["draft"]["cards"] and b["draft"]["suggested_at"]
    assert db.pitch_draft(lid)[0]["created_at"] == b["draft"]["created_at"]   # saved
    again = client.post(f"/api/pitch/{lid}/build").json()
    assert again["draft"]["created_at"] == b["draft"]["created_at"]          # a second call reuses it
    r = client.get(f"/leads/{lid}/pitch")
    assert 'class="pn pwait hidden"' in r.text and '"pending": []' in r.text   # ready: the editor opens with its draft


def test_new_accepted_match_waits_for_its_card_only():
    lid, n = _lead("Top up lead")
    client.get(f"/leads/{lid}/pitch")
    client.post(f"/api/pitch/{lid}/build")
    m = next(x for x in n["result"]["results"][1:] if x["entity_id"] not in {c.get("entity_id") for c in db.pitch_draft(lid)[0]["cards"]})
    client.post("/api/decide", json={"lead": lid, "need": n["id"], "entity": m["entity_id"], "decision": "accept"})
    r = client.get(f"/leads/{lid}/pitch")
    assert '"pending": ["read", "write"]' in r.text
    d = client.post(f"/api/pitch/{lid}/build").json()["draft"]
    assert m["entity_id"] in {c.get("entity_id") for c in d["cards"]}


def test_build_failure_is_a_plain_message(monkeypatch):
    from app import main
    lid, _n = _lead("Broken lead")
    monkeypatch.setattr(main, "ensure_draft", lambda lid: (_ for _ in ()).throw(RuntimeError("db gone")))
    r = client.post(f"/api/pitch/{lid}/build").json()
    assert not r["ok"] and r["error"].startswith("The facts for the pitch could not be drafted this time.")


def test_wait_lines_are_translated_and_accept_all_has_no_toast():
    js = (APP / "static" / "pitch.js").read_text()
    assert "cards accepted" not in js and "card accepted" not in js
    keys = set(re.findall(r'_t\("([^"]+)"', js)) | set(re.findall(r'\["([^"]+…)", \d+\]', js))
    for lang in ("de", "it"):
        have = {}
        for f in (APP / "locales" / lang).glob("*.json"):
            have.update(json.loads(f.read_text()))
        assert not keys - set(have), (lang, keys - set(have))
        assert all(not BANNED_UI.search(k) for k in re.findall(r'\["([^"]+…)", \d+\]', js))
