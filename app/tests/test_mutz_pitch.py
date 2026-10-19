"""A file or a one pager dropped into the Mutz chat goes straight to a drafted pitch: lead, needs, search, picks, draft.
Run from the repo root with  python -m pytest app/tests -q  (temporary app DB, stubs forced)."""
import os
import tempfile
import time

os.environ.setdefault("MUTZ_APP_DB", os.path.join(tempfile.mkdtemp(), "app.sqlite"))
os.environ["MUTZ_STUB"] = "query,why_not"

import pytest
from fastapi.testclient import TestClient

from app import db, main, stubs
from app.main import app

client = TestClient(app)
ONE_PAGER = ("We build a glucose and lactate patch for women in endurance sport. We need a clinical study partner in Bern "
             "for type 1 diabetes and exercise, and a lab that can test sensors on the skin. ") * 3


@pytest.fixture
def needs_seen(monkeypatch):
    """The needs reader without the gateway: keywords give fixed needs; the call's arguments are kept to check."""
    seen = []

    def fake(**kw):
        seen.append(kw)
        return stubs.needs(keywords="type 1 diabetes exercise study; continuous glucose monitoring sensor")
    monkeypatch.setattr(main.contracts, "needs", fake)
    return seen


def _wait(lid, secs=120):
    end = time.time() + secs
    while time.time() < end:
        st = client.get(f"/api/mutz/pitch/{lid}").json()
        if st["step"] == "done":
            return st
        time.sleep(0.2)
    raise AssertionError("the draft did not finish")


def _r(eid, cov="strong", grade=None, adjacent=False, reasons=True):
    return {"entity_id": eid, "name": eid, "coverage": cov, "grade": grade, "adjacent": adjacent,
            "reasons": [{"url": f"https://example.ch/{eid}", "quote": "q"}] if reasons else []}


def test_picks_take_direct_quoted_matches_one_per_need_first():
    ns = [{"id": "a", "result": {"results": [_r("a1"), _r("a2"), _r("a3")]}},
          {"id": "b", "result": {"results": [_r("b0", adjacent=True), _r("b1", grade=1), _r("b2", reasons=False),
                                             _r("b3", cov="none"), _r("b4", grade=0), _r("b5", grade=2)]}},
          {"id": "c", "result": {"results": []}},
          {"id": "d", "route": {"kind": "curated", "entries": [{"id": "inno", "name": "Innosuisse", "url": "https://inno.ch",
                                                               "description": "Funding", "kind": "funding"}]}}]
    picks = main.mutz_picks(ns)
    assert [(n, m["entity_id"]) for n, m in picks] == [("a", "a1"), ("b", "b5"), ("d", "curated:inno"), ("a", "a2")]
    assert picks[2][1]["reasons"][0]["url"] == "https://inno.ch"
    many = [{"id": f"n{i}", "result": {"results": [_r(f"n{i}x"), _r(f"n{i}y")]}} for i in range(6)]
    assert len(main.mutz_picks(many)) == main.PICK_MAX
    assert len({m["entity_id"] for _n, m in main.mutz_picks(many)}) == main.PICK_MAX


def test_pasted_one_pager_becomes_a_lead_with_a_drafted_pitch(needs_seen):
    r = client.post("/api/mutz/pitch", data={"key": "global", "text": ONE_PAGER}).json()
    lid = r["lead"]
    st = _wait(lid)
    assert st.get("lead") == lid and not st.get("error")
    assert needs_seen[0]["one_pager_text"].startswith("We build a glucose") and needs_seen[0]["pdf_path"] is None
    acc = db.accepted(lid)
    assert acc and all(d["added_by"] == "mutz" for d in acc)
    draft, _v = db.pitch_draft(lid)
    cards = [c for c in draft["cards"] if c["origin"] != "suggested"]   # suggestions are labelled as such already
    assert cards and all(c.get("picked_by") == "mutz" and not c["checked"] for c in cards)   # the officer still ticks each
    assert {c["entity_id"] for c in cards if c["section"] != "why"} == {d["entity_id"] for d in acc}
    assert db.lead(lid)["step"] == "pitch"
    links = [a["href"] for a in st["reply"]["actions"]]
    assert f"/leads/{lid}/pitch" in links
    for key in ("global", lid):   # the chat on this screen and the new lead's chat both keep the exchange
        th = db.thread(key)
        assert th[-2]["role"] == "user" and th[-1]["text"] == st["reply"]["text"]
    page = client.get(f"/leads/{lid}/matches")
    assert "picked by Bärner Mutz" in page.text


def _pdf(pages=2):
    import io
    from pypdf import PdfWriter
    w, buf = PdfWriter(), io.BytesIO()
    for _i in range(pages):
        w.add_blank_page(width=595, height=842)
    w.write(buf)
    return buf.getvalue()


def test_pdf_goes_into_the_chat_and_stays_with_the_lead(needs_seen):
    pdf = _pdf()
    r = client.post("/api/mutz/pitch", data={"key": "global", "text": ""},
                    files={"file": ("Kestrelyn_one-pager.pdf", pdf, "application/pdf")}).json()
    lid = r["lead"]
    assert r["file"] == {"name": "Kestrelyn_one-pager.pdf", "size": len(pdf), "kind": "PDF", "pages": 2, "url": f"/leads/{lid}/file"}
    _wait(lid)
    assert needs_seen[0]["pdf_path"].endswith(f"{lid}.pdf")
    l = db.lead(lid)
    assert l["input_kind"] == "pdf" and l["pdf_name"] == "Kestrelyn_one-pager.pdf" and l["keep_file"] == 1
    sent = next(m for m in db.thread(lid) if m["role"] == "user")
    assert sent["file"]["url"] == f"/leads/{lid}/file" and sent["text"] == ""      # the chat shows the file itself
    f = client.get(f"/leads/{lid}/file")
    assert f.status_code == 200 and f.content == pdf and f.headers["content-type"] == "application/pdf"
    assert "inline" in f.headers["content-disposition"] and "Kestrelyn_one-pager.pdf" in f.headers["content-disposition"]
    assert f'href="/leads/{lid}/file"' in client.get(f"/leads/{lid}/brief?source=1&mode=pdf").text


def test_text_file_is_kept_and_read_as_text(needs_seen):
    r = client.post("/api/mutz/pitch", data={"key": "global", "text": "https://kestrelyn.example"},
                    files={"file": ("brief.md", ONE_PAGER.encode(), "text/markdown")}).json()
    lid = r["lead"]
    assert r["file"]["kind"] == "MD" and r["file"]["pages"] is None
    _wait(lid)
    assert needs_seen[0]["one_pager_text"].startswith("We build a glucose") and needs_seen[0]["website"] == "https://kestrelyn.example"
    assert client.get(f"/leads/{lid}/file").text == ONE_PAGER


def test_status_shows_each_need_as_its_search_ends(needs_seen, monkeypatch):
    seen = []
    real = main._search_job

    def watched(lid, only):  # look at the status the chat polls before and after the search, from inside the run
        seen.append(client.get(f"/api/mutz/pitch/{lid}").json())
        real(lid, only)
        seen.append(client.get(f"/api/mutz/pitch/{lid}").json())
    monkeypatch.setattr(main, "_search_job", watched)
    lid = client.post("/api/mutz/pitch", data={"text": ONE_PAGER}).json()["lead"]
    done = _wait(lid)
    before, after = seen
    assert before["step"] == "search" and before["elapsed"] >= 0 and before["brief"]["used"] >= 0
    assert [n["state"] for n in before["needs"]] == ["search", "wait"]
    assert all(n["state"] == "done" and n["coverage"] for n in after["needs"])
    assert any(n["top"] for n in after["needs"])
    assert done["reply"]["table"]["head"] == ["Need", "In the pitch"] and len(done["reply"]["table"]["rows"]) == 2


def test_refuses_what_it_cannot_read():
    assert "Attach a PDF" in client.post("/api/mutz/pitch", data={"text": "  "}).json()["error"]
    r = client.post("/api/mutz/pitch", files={"file": ("deck.pptx", b"\x89PK\x03\x04\xff\xfe", "application/octet-stream")})
    assert r.status_code == 400 and "PDF and plain text" in r.json()["error"]


def test_an_empty_open_lead_takes_the_file(needs_seen):
    lid = db.create_lead("New lead", origin="S-GE")
    r = client.post("/api/mutz/pitch", data={"key": lid, "lead": lid, "text": ONE_PAGER}).json()
    assert r["lead"] == lid
    _wait(lid)
    busy = client.post("/api/mutz/pitch", data={"key": lid, "lead": lid, "text": ONE_PAGER}).json()
    assert busy["lead"] != lid   # a lead with needs and a pitch is never overwritten: a new lead is made
    _wait(busy["lead"])


def test_chat_has_the_attach_button():
    r = client.get("/")
    assert 'id="mutzclip"' in r.text and 'id="mutzfilein"' in r.text
