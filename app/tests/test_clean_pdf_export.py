"""What the company gets: the clean PDF written on export carries no verification marks (ticks, "verified quote",
status labels); the review PDF, the agency's own copy, keeps them. Both the real builder and the stub fallback."""
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("MUTZ_APP_DB", os.path.join(tempfile.mkdtemp(), "app.sqlite"))
os.environ["MUTZ_STUB"] = "query,why_not"

from fastapi.testclient import TestClient
from pypdf import PdfReader

from app import contracts, db, stubs
from app.main import app

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]
MARKS = ["verified quote", "edited by you", "added by you", "needs a check", "open at quote", "✓", "✔"]


def _text(path):
    return " ".join(" ".join((pg.extract_text() or "").split()) for pg in PdfReader(str(path)).pages).lower()


def test_stub_pdf_clean_has_no_marks(tmp_path):
    from pitch.build import _patch_macos_dlopen
    _patch_macos_dlopen()   # WeasyPrint finds Homebrew's Pango on macOS
    p = json.loads((ROOT / "pitch" / "fixtures" / "valid.json").read_text("utf-8"))
    for c in p["cards"]:
        c["section"] = "why" if c["type"] == "why" else "matches"
    p["sections"] = [{"id": "why", "title": "Why Bern for you"}, {"id": "matches", "title": "Matches"},
                     {"id": "sources", "title": "Sources"}]
    clean = _text(stubs.build_pdf(p, "clean", str(tmp_path / "clean.pdf")))
    review = _text(stubs.build_pdf(p, "review", str(tmp_path / "review.pdf")))
    for m in MARKS:
        assert m not in clean, m
    assert "[1]" in clean and "sources" in clean
    assert "verified quote" in review and "edited by you" in review


def test_exported_clean_pdf_has_no_marks(monkeypatch, tmp_path):
    from app import main, pitchgen
    monkeypatch.setattr(pitchgen, "_llm_cards", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no gateway in tests")))
    monkeypatch.setattr(main, "EXPORTS", tmp_path)
    lid = db.create_lead("Clean PDF lead", origin="S-GE")
    client.post(f"/leads/{lid}/brief", data={"mode": "keywords", "keywords": "type 1 diabetes exercise study"},
                follow_redirects=False)
    main._search_job(lid, None)
    n = db.needs(lid)[0]
    for m in n["result"]["results"][:2]:
        client.post("/api/decide", json={"lead": lid, "need": n["id"], "entity": m["entity_id"], "decision": "accept"})
    client.get(f"/leads/{lid}/pitch")
    client.post(f"/api/pitch/{lid}/build")
    draft, _ = db.pitch_draft(lid)
    for c in draft["cards"]:
        c["checked"] = True
    db.save_pitch(lid, draft)
    r = client.post(f"/leads/{lid}/export", data={"override": "1"}, follow_redirects=False)
    assert r.headers["location"].endswith(f"/leads/{lid}/after?dl=1"), r.headers["location"]
    assert contracts.STATUS["build_pdf"] == "real"   # what the company gets, not the stub fallback
    (tmp_path / "c.pdf").write_bytes(client.get(f"/leads/{lid}/export/v1/clean.pdf").content)
    (tmp_path / "r.pdf").write_bytes(client.get(f"/leads/{lid}/export/v1/review.pdf").content)
    clean, review = _text(tmp_path / "c.pdf"), _text(tmp_path / "r.pdf")
    for m in MARKS:
        assert m not in clean, m
    assert "[1]" in clean and "sources" in clean
    assert "review copy" in review and any(m in review for m in ("verified quote", "needs a check", "edited by you"))
