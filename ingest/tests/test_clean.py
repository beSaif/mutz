"""Tests for ingest/clean.py on invented fixtures (no real people). Run: python -m pytest ingest/tests/test_clean.py"""
import json
from pathlib import Path

import pytest

from ingest import clean

FX = Path(__file__).parent / "fixtures" / "clean" / "raw"
EMAIL = "mira.testfeld" + "@" + "example.org"


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    d = tmp_path_factory.mktemp("clean_out")
    stats = clean.run(FX, d, force=True, workers=1, quiet=True, cache_dir=d / "cache")
    pages = [json.loads(l) for l in open(d / "pages.jsonl")]
    chunks = [json.loads(l) for l in open(d / "chunks.jsonl")]
    return d, stats, pages, chunks


def person_pages(pages):
    return [p for p in pages if "testfeld" in p["url"]]


def test_contract_fields(out):
    _, _, pages, chunks = out
    for p in pages:
        for k in ("page_id", "url", "source", "group", "lang", "title", "text", "emails", "siblings", "fetched_at",
                  "page_type", "breadcrumbs", "sibling_group"):
            assert k in p, k
        assert p["page_id"] == p["content_hash"][:16]
    for c in chunks:
        for k in ("chunk_id", "page_id", "url", "source", "lang", "heading", "text"):
            assert k in c, k


def test_obfuscated_email_fills_email_field(out):
    _, _, pages, _ = out
    pp = person_pages(pages)
    assert len(pp) == 3
    for p in pp:
        assert p["emails"] == [EMAIL]
        assert EMAIL in p["text"]  # visible next to the name, so a quote can carry it


def test_three_languages_one_sibling_group(out):
    _, _, pages, _ = out
    pp = person_pages(pages)
    assert sorted(p["lang"] for p in pp) == ["de", "en", "fr"]
    assert len({p["sibling_group"] for p in pp}) == 1
    for p in pp:
        assert len(p["siblings"]) == 2 and p["page_id"] not in p["siblings"]
        assert p["page_type"] == "person"
        assert p["breadcrumbs"][-1] == "Dr. Mira Testfeld"


def test_dedupe_identical_content_and_skip_non_200(out):
    _, stats, pages, _ = out
    assert stats["raw_records"] == 5  # 404 record not counted
    de = [p for p in person_pages(pages) if p["lang"] == "de"]
    assert len(de) == 1 and de[0]["url"].endswith("index_ger.html") and de[0]["alt_urls"]
    assert len({p["page_id"] for p in pages}) == len(pages)


def test_team_grid_keeps_every_card(out):
    _, _, pages, chunks = out
    team = next(p for p in pages if p["source"] == "dcb_example")
    for name in ("Alva Beispiel", "Bruno Muster", "Dr. Carla Probe", "Dario Exempel", "Elin Platzhalter", "Fynn Vorlage"):
        assert name in team["text"]
    assert "carla.probe@example.org" in team["emails"]
    assert "info@example.org" not in team["emails"]  # footer address is not the page's content
    assert team["page_type"] == "group"
    assert any("Core Team Operations" in c["headings"] for c in chunks if c["page_id"] == team["page_id"])


def test_chunks_keep_heading_and_size():
    text = "# Title\n\nintro words here\n\n## Methods\n\n" + " ".join(["word"] * 900) + "\n\n## Small\n\nshort part"
    ch = clean.chunk_text(text, "Title")
    assert all(t.lstrip().startswith("#") for _, t in ch)
    assert all(clean.words(t) <= clean.CHUNK_MAX + 10 for _, t in ch)
    assert sum(1 for h, _ in ch if h == "Methods") >= 3


def test_email_styles():
    assert clean.emails_in_text("Kontakt: anna.muster [at] example [dot] org") == ["anna.muster@example.org"]
    assert clean.emails_in_text("write to b.probe(at)example.org today") == ["b.probe@example.org"]
    assert clean.href_email("mailto:c.test@example.org?subject=hi") == "c.test@example.org"
    assert clean.href_email("mailto:?body=https://x.test/") is None
    js = "javascript:void(location.href='mailto:'+String.fromCharCode(100,64,101,120,97,109,112,108,101,46,111,114,103))"
    assert clean.href_email(js) == "d@example.org"


def test_language_detection_order():
    assert clean.detect_lang("de-CH", "https://x.test/a/index_eng.html", "")[0] == "de"
    assert clean.detect_lang(None, "https://x.test/a/index_fra.html", "")[0] == "fr"
    assert clean.detect_lang(None, "https://x.test/a/", "Das ist ein deutscher Satz über die Forschung an der Universität.")[0] == "de"


def test_rerun_uses_cache_and_warns_on_drop(out, tmp_path, capsys):
    d, _, _, _ = out
    stats = clean.run(FX, d, workers=1, quiet=True, cache_dir=d / "cache")
    assert stats["parsed_now"] == 0
    # gap 16: half the cache gone -> warning
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "fx").mkdir()
    keep = [json.loads(l) for l in open(FX / "index.jsonl")][3:4]  # only the team page survives
    for r in keep:
        (raw / r["file"]).write_bytes((FX / r["file"]).read_bytes())
    (raw / "index.jsonl").write_text("".join(json.dumps(r) + "\n" for r in keep))
    stats = clean.run(raw, d, workers=1, quiet=True, cache_dir=d / "cache")
    assert any("unibe_beispiel" in w for w in stats["warnings"])
    assert "WARNING" in capsys.readouterr().err


def test_no_network(tmp_path, monkeypatch):
    """clean.py never opens a socket (gap 14): a full run with sockets disabled still works."""
    import socket

    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    stats = clean.run(FX, tmp_path, force=True, workers=1, quiet=True, cache_dir=tmp_path / "cache")
    assert stats["pages"] == 4
