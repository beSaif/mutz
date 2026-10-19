"""After sending: the sent screen, the CEO's yes, the email to the researchers and sent versions.
Run from the repo root with  python -m pytest app/tests -q  (temporary app DB, stubs, no gateway)."""
import os
import re
import tempfile

os.environ.setdefault("MUTZ_APP_DB", os.path.join(tempfile.mkdtemp(), "app.sqlite"))
os.environ["MUTZ_STUB"] = "query,why_not"

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import contracts, db, lifecycle, main, pitchgen, stubs
from app.main import app

client = TestClient(app)
BANNED_UI = re.compile(r"\b(unlock|elevate|seamless|innovative|cutting edge|leverage|synergy|forefront|journey|empower|"
                       r"get started|learn more|submit)\b", re.I)


def _src(url, quote):
    return {"url": url, "quote": quote, "deep_link": url}


UNIT = {"entity_id": "unit:dcb:root", "type": "unit", "name": "Diabetes Center Berne", "unit": "Diabetes Center Berne",
        "unit_id": "unit:dcb:root", "reasons": [_src("https://www.dcberne.com/en/times-study/",
                                                     "Premenopausal women with type 1 diabetes often experience changes in glucose control.")]}
PERSON = {"entity_id": "person:ann-muster", "type": "person", "name": "Ann Muster", "function": "Project lead",
          "unit": "Diabetes Center Berne", "unit_id": "unit:dcb:root",
          "reasons": [_src("https://www.dcberne.com/en/team/", "Ann Muster leads the sensing projects at the centre.")]}
OTHER = {"entity_id": "unit:artorg:hearing", "type": "unit", "name": "Hearing Research Laboratory", "unit": "ARTORG Center",
         "reasons": [_src("https://www.artorg.unibe.ch/hearing", "The laboratory studies signal processing for hearing.")]}
SERVICE = {"entity_id": "curated:curated:unitectra", "type": "unit", "name": "Unitectra", "unit": "Bern, Basel, Zurich",
           "reasons": [_src("https://www.unitectra.ch/", "Unitectra is the technology transfer office.")]}


@pytest.fixture(autouse=True)
def no_gateway(monkeypatch, tmp_path):
    """No Apertus and no files under data/: card text stays the quote, the PDF builder writes a stand-in file."""
    monkeypatch.setattr(pitchgen, "_write", lambda cards, lead, lang: None)
    monkeypatch.setattr(main, "EXPORTS", tmp_path / "exports")
    monkeypatch.setattr(contracts, "build_pdf", lambda p, mode, out: Path(out).write_bytes(b"%PDF-1.4 test"))
    monkeypatch.setattr(contracts, "FORCE", contracts.FORCE | {"email"})


def _wait(lid):
    t = lifecycle._mail_jobs.get(lid)
    if t:
        t.join(10)


def sent_lead(matches=(UNIT, PERSON, SERVICE)):
    """A lead with needs, accepted matches and every card checked, exported once through the real route."""
    lid = db.create_lead("Kestrelyn Health Oy", origin="S-GE", owner="saif")
    db.update_lead(lid, profile={"name": "Kestrelyn Health Oy", "city": "Tampere", "country": "Finland",
                                 "product": "a skin patch that measures glucose and lactate"})
    n1 = db.add_need(lid, {"text": "Clinical study with women who have type 1 diabetes", "type": "research", "label": "Clinical study"})
    n2 = db.add_need(lid, {"text": "Licensing of university sensor technology", "type": "transfer", "label": "Tech transfer"})
    for m in matches:
        db.decide(lid, n2 if m is SERVICE else n1, m["entity_id"], "accept", match=m)
    draft = main.ensure_draft(lid)
    for c in draft["cards"]:
        c["checked"] = True
    db.save_pitch(lid, draft)
    r = client.post(f"/leads/{lid}/export", data={"override": "1"}, follow_redirects=False)
    assert r.headers["location"].endswith(f"/leads/{lid}/after?dl=1")   # on to After sending, which downloads the PDF
    return lid


def text(html):
    import html as h
    return h.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", html, flags=re.S))))


def test_after_send_screen_says_what_went_out_to_whom_and_what_is_next():
    lid = sent_lead()
    assert client.get(f"/leads/{lid}/export?done=1", follow_redirects=False).headers["location"].endswith(f"/leads/{lid}/after")
    page = client.get(f"/leads/{lid}/after?dl=1").text
    t = text(page)
    assert "Export, the last step" not in t and "Frozen as v1" not in t
    assert f'a.href = "/leads/{lid}/export/v1/clean.pdf"; a.download' in page   # the export click downloads the PDF
    assert "What was sent" in t and "Pitch v1, clean PDF" in t and f"/leads/{lid}/export/v1/clean.pdf" in page
    assert "The CEO of Kestrelyn Health Oy" in t and "Tampere, Finland" in t
    assert "When" in t and "by Saif" in t
    assert "Waiting for the CEO's reply" in t and "What happens next" not in t
    # one primary action: the CEO's yes
    assert re.findall(r'class="b p"[^>]*>([^<]+)<', page) == ["CEO accepted the intro"]
    assert f'action="/leads/{lid}/accepted"' in page
    assert not BANNED_UI.search(t)
    assert client.get(f"/leads/{lid}/export/v1/clean.pdf").content.startswith(b"%PDF")


def test_ceo_accepted_moves_the_stage_writes_the_timeline_and_drafts_the_email():
    lid = sent_lead()
    assert db.lead(lid)["stage"] == "pitched"
    r = client.post(f"/leads/{lid}/accepted", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith(f"/leads/{lid}/after#emails")
    assert db.lead(lid)["stage"] == "visit"
    acc = [e for e in db.entries(lid) if e["kind"] == "accepted"]
    assert len(acc) == 1 and acc[0]["text"] == "CEO accepted the intro" and acc[0]["at"]
    assert db.outreach(lid)                                   # Follow up and the pipeline see the researchers
    _wait(lid)
    page = client.get(f"/leads/{lid}/after").text
    t = text(page)
    # once the CEO accepted, the page is only the emails: no timeline, no status card
    assert "Timeline" not in t and "What was sent" not in t and "Add information" not in t and "Sent versions" not in t
    assert "Email to the researchers" in t and "Open in mail" in t and "Draft again" in t and "Copy" in t
    assert "mailto:?subject=" in page
    assert "Waiting for the CEO's reply" not in t
    assert not BANNED_UI.search(t)
    # a second click does not add a second entry or redraft
    first = lifecycle.emails(lid)["emails"][0]["body"]
    client.post(f"/leads/{lid}/accepted")
    assert len([e for e in db.entries(lid) if e["kind"] == "accepted"]) == 1
    assert lifecycle.emails(lid)["emails"][0]["body"] == first


def test_status_select_visit_is_the_same_ceo_yes():
    lid = sent_lead()
    r = client.post(f"/leads/{lid}/stage", data={"stage": "visit"}, follow_redirects=False)
    assert r.headers["location"].endswith(f"/leads/{lid}/after#emails")
    assert db.lead(lid)["stage"] == "visit" and any(e["kind"] == "accepted" for e in db.entries(lid))
    _wait(lid)
    assert lifecycle.emails(lid)["emails"]


def test_one_email_per_research_group_from_the_approved_pitch():
    lid = sent_lead((UNIT, PERSON, OTHER, SERVICE))
    lifecycle.ceo_accepted(lid)
    _wait(lid)
    d = lifecycle.emails(lid)
    assert [e["to"] for e in d["emails"]] == ["Diabetes Center Berne", "ARTORG Center"]   # the person joins their centre
    dcb = d["emails"][0]
    assert set(dcb["entities"]) == {UNIT["entity_id"], PERSON["entity_id"]}
    assert d["other"] == ["Unitectra"]                                                    # a service, not a research group
    assert dcb["state"] == "done" and dcb["source"] == "template"
    assert "Kestrelyn Health Oy" in dcb["body"] and "Tampere, Finland" in dcb["body"]
    assert "Clinical study with women who have type 1 diabetes" in dcb["body"]
    assert "Premenopausal women" in dcb["body"]                                           # a fact from the sent pitch
    assert "Saif" in dcb["body"] and "[Date]" in dcb["body"]
    assert dcb["subject"] == "Introduction: Kestrelyn Health Oy and Diabetes Center Berne"
    assert not stubs.BANNED.search(dcb["body"])
    page = client.get(f"/leads/{lid}/after").text
    assert page.count('role="tab"') == 2 and "Unitectra" in text(page)
    # the same text shows on Follow up for the group's rows
    rows = {r["entity_id"]: r["draft"] for r in db.outreach(lid)}
    assert rows[PERSON["entity_id"]] == dcb["body"]


def test_email_uses_apertus_and_is_cached(monkeypatch):
    from common import llm
    calls = []

    def fake(model, messages, **kw):
        calls.append(kw)
        assert kw["tag"] == "app.researcher_email" and "Premenopausal women" in messages[1]["content"]
        return {"content": "", "tool_calls": [{"name": "write_email", "arguments": {
            "subject": "Meeting request: Kestrelyn Health Oy", "body": "Dear colleagues at the Diabetes Center Berne,\n\n"
            "Kestrelyn Health Oy makes a skin patch. Would you meet the company for 45 minutes on [Date]?\n\nKind regards\n[Officer name]"}}]}
    monkeypatch.setattr(contracts, "FORCE", contracts.FORCE - {"email", "all"})
    monkeypatch.setattr(llm, "chat", fake)
    lid = sent_lead((UNIT,))
    client.post(f"/leads/{lid}/accepted")
    _wait(lid)
    e = lifecycle.emails(lid)["emails"][0]
    assert e["source"] == "apertus" and e["subject"] == "Meeting request: Kestrelyn Health Oy"
    assert e["body"].endswith("Kind regards\nSaif") and calls[0]["cache"] is True
    client.get(f"/leads/{lid}/after")                                  # reloading does not draft again
    assert len(calls) == 1
    page = text(client.get(f"/leads/{lid}/after").text)
    assert "Drafted by Apertus 70B" in page


def test_edits_persist_and_draft_again_rewrites():
    lid = sent_lead((UNIT,))
    lifecycle.ceo_accepted(lid)
    _wait(lid)
    key = lifecycle.emails(lid)["emails"][0]["key"]
    r = client.post(f"/api/leads/{lid}/emails", json={"key": key, "subject": "My subject", "body": "My own words."})
    assert r.json() == {"ok": True}
    e = lifecycle.emails(lid)["emails"][0]
    assert e["subject"] == "My subject" and e["body"] == "My own words." and e["edited"]
    page = client.get(f"/leads/{lid}/after").text
    assert "My own words." in page and 'value="My subject"' in page and "Your edits are saved." in page
    assert client.post(f"/api/leads/{lid}/emails", json={"key": "nope", "body": "x"}).json() == {"ok": False}
    # Draft again: the edit is replaced by a new draft
    assert client.post(f"/api/leads/{lid}/emails", json={"key": key, "redo": True}).json() == {"ok": True}
    _wait(lid)
    d = client.get(f"/api/leads/{lid}/emails").json()
    assert not d["running"]
    e = d["emails"][0]
    assert e["body"] != "My own words." and "Diabetes Center Berne" in e["body"] and not e["edited"]


def test_drafting_shows_a_loading_state():
    lid = sent_lead((UNIT,))
    db.update_lead(lid, stage="visit")
    groups, other = lifecycle.email_groups(lid)
    db.kv_set(f"mail:{lid}", {"at": db.now(), "other": other,
                              "emails": [{**g, "subject": "", "body": "", "state": "running", "source": None, "edited": False}
                                         for g in groups]})
    lifecycle._mail_jobs[lid] = None  # pretend a job is running, so the page does not start one
    try:
        page = client.get(f"/leads/{lid}/after").text
        assert "Drafting the email to the researchers…" in page and 'role="status"' in page
        assert client.get(f"/api/leads/{lid}/emails").json()["running"] is True
    finally:
        lifecycle._mail_jobs.pop(lid, None)
    _wait(lid)


def test_add_information_without_a_kind_is_a_note():
    lid = sent_lead((UNIT,))
    page = client.get(f"/leads/{lid}/after").text
    assert "Add information" not in page and "Timeline" not in text(page)    # the form is gone; the route still takes a note
    r = client.post(f"/leads/{lid}/entry", data={"text": "The CEO called back.", "confidential": "1"}, follow_redirects=False)
    assert r.status_code == 303
    e = db.entries(lid)[-1]
    assert e["kind"] == "note" and e["text"] == "The CEO called back."
    client.post(f"/leads/{lid}/entry", data={"text": "Old form.", "kind": "email"})
    assert db.entries(lid)[-1]["kind"] == "email"                        # an old form with a kind still works
    client.post(f"/leads/{lid}/entry", data={"text": "Odd kind.", "kind": "pitch"})
    assert db.entries(lid)[-1]["kind"] == "note"


def test_recheck_and_sent_versions_read_plainly():
    lid = sent_lead((UNIT,))
    page = client.get(f"/leads/{lid}/after").text
    t = text(page)
    assert "New information?" not in t and "Set status" not in t and f'action="/leads/{lid}/reevaluate"' not in page
    assert "Compare with the needs" not in t and "Re-evaluate on new information" not in t
    assert "Sent versions" in t and "v1 sent today" in t and f"/leads/{lid}/export/v1/clean.pdf" in page
    assert "built from" not in t
    # JSON and the review PDF sit folded under Agency copies
    folded = page.split('<details class="vl-more">', 1)[1].split("</details>", 1)[0]
    assert "pitch.json" in folded and "review.pdf" in folded
    assert page.count("pitch.json") == 1
    # the pitched lead's one red action is the CEO's yes
    assert re.findall(r'class="b p"[^>]*>([^<]+)<', page) == ["CEO accepted the intro"]
