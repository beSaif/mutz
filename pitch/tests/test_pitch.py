"""pytest pitch/tests: validation rules, both PDFs, links checked with pypdf."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

from pitch import build
from pitch.build import ExportError, build_pdf, deep_link, validate, validate_detailed

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "pitch" / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text("utf-8"))


@pytest.fixture
def valid():
    return load("valid.json")


def codes(pitch, mode=None):
    return {p["code"] for p in validate_detailed(pitch, mode)}


def links(pdf_path):
    """(uris, internal link count) from the PDF's link annotations."""
    uris, internal = [], 0
    for page in PdfReader(str(pdf_path)).pages:
        for a in page.get("/Annots") or []:
            a = a.get_object()
            if a.get("/Subtype") != "/Link":
                continue
            act = a.get("/A")
            if act is not None:
                act = act.get_object()
                if act.get("/S") == "/URI":
                    uris.append(str(act["/URI"]))
                    continue
                if act.get("/S") == "/GoTo":
                    internal += 1
                    continue
            if a.get("/Dest") is not None:
                internal += 1
    return uris, internal


def text_of(pdf_path):
    raw = " ".join(p.extract_text() or "" for p in PdfReader(str(pdf_path)).pages)
    raw = raw.replace("\u2010\n", "").replace("-\n", "")   # undo hyphenation at line ends
    return " ".join(raw.split())


# ---------------------------------------------------------------- validation

def test_valid_fixture_has_no_problems(valid):
    assert validate(valid) == []
    assert validate(valid, "clean") == []


@pytest.mark.parametrize("name,code", [
    ("invalid_no_source.json", "no_source"),
    ("invalid_banned_words.json", "banned_word"),
    ("invalid_meeting_promise.json", "meeting_promise"),
    ("invalid_person_name.json", "person_name"),
    ("invalid_bad_url.json", "bad_url"),
])
def test_invalid_fixtures_are_rejected(name, code):
    assert code in codes(load(name))


@pytest.mark.parametrize("word", [
    "unlock", "elevate", "seamless", "cutting edge", "cutting-edge", "innovative",
    "leverage", "synergy", "synergies", "supercharge", "revolutionize", "revolutionise",
    "game changer", "delve", "at the forefront", "journey", "empower",
])
def test_banned_words(valid, word):
    valid["cards"][0]["text"] = f"The group works on sensors and will {word} the field."
    assert "banned_word" in codes(valid)


def test_innovation_as_a_name_is_allowed(valid):
    valid["cards"][1]["text"] = "The clinic runs an innovation challenge open to companies."
    assert validate(valid) == []


@pytest.mark.parametrize("text", [
    "Ready to move your team to Bern?",
    "It's not just a lab, it's a partner.",
    "Bern is the place for sensors!",
    "Why not Bern?",
    "A 97% match for your needs.",
])
def test_banned_shapes(valid, text):
    valid["cards"][0]["text"] = text
    assert "banned_shape" in codes(valid)


@pytest.mark.parametrize("text", [
    "The group will meet your team in November.",
    "The head of the lab has agreed to a visit.",
    "The researchers look forward to your visit.",
    "The group looks forward to working with you.",
    "The team is happy to meet you on the visit day.",
    "A meeting is confirmed for 3 November.",
    "Die Gruppe freut sich auf Ihren Besuch.",
    "Le groupe se réjouit de votre visite.",
])
def test_meeting_promises(valid, text):
    valid["cards"][3]["text"] = text
    assert "meeting_promise" in codes(valid)


def test_meeting_promise_in_intro_and_visit_day(valid):
    valid["intro"] = "Three groups have agreed to host you."
    assert "meeting_promise" in codes(valid)
    v = load("valid.json")
    v["visit_day"][0]["topic"] = "The group will meet you in the lab"
    assert "meeting_promise" in codes(v)


def test_scientific_confirmed_is_not_a_promise(valid):
    valid["cards"][3]["text"] = "A trial has confirmed the effect of training on glucose levels."
    assert validate(valid) == []


def test_hidden_person_name_is_rejected(valid):
    card = valid["cards"][2]
    assert card["show_person_name"] is False
    card["text"] = "Muster's group builds skin worn sensors."
    assert "person_name" in codes(valid)
    card["text"] = "The group, led by Prof. Someone, builds sensors."
    assert "person_name" in codes(valid)


def test_hidden_name_elsewhere_in_pitch(valid):
    valid["intro"] = "Erika Muster runs the strongest group for your patch."
    assert "person_name" in codes(valid)


def test_name_allowed_when_switched_on(valid):
    card = valid["cards"][2]
    card["show_person_name"] = True
    card["text"] = "Prof. Erika Muster builds skin worn sensors for glucose estimates."
    assert validate(valid) == []


def test_missing_show_person_name_defaults_to_hidden(valid):
    card = valid["cards"][2]
    del card["show_person_name"]
    card["text"] = "Prof. Erika Muster builds sensors."
    assert "person_name" in codes(valid)


def test_review_gate_blocks_clean_only(valid):
    valid["cards"][0]["checked"] = False
    assert validate(valid, "review") == []
    assert "review_gate" in codes(valid, "clean")
    valid["override_review_gate"] = True
    assert validate(valid, "clean") == []


def test_hidden_card_is_ignored(valid):
    valid["cards"][3]["sources"] = []
    valid["cards"][3]["included"] = False
    assert validate(valid) == []


def test_deep_link_points_at_source_host(valid):
    valid["cards"][0]["sources"][0]["deep_link"] = "https://elsewhere.example.com/#:~:text=x"
    assert "bad_url" in codes(valid)


def test_deep_link_encoding():
    url = "https://research.example.org/g"
    assert deep_link(url, "short - quote") == url + "#:~:text=short%20%2D%20quote"
    long = deep_link(url, "one two three four five six seven eight nine ten")
    assert long == url + "#:~:text=one%20two%20three%20four,seven%20eight%20nine%20ten"
    assert deep_link(url, "") == url


# ---------------------------------------------------------------- export

@pytest.mark.parametrize("mode", ["review", "clean"])
def test_export_fails_loudly_without_source(tmp_path, mode):
    out = tmp_path / "x.pdf"
    with pytest.raises(ExportError) as e:
        build_pdf(load("invalid_no_source.json"), mode, out)
    assert any("has no source" in p for p in e.value.problems)
    assert "has no source" in str(e.value)
    assert not out.exists()


@pytest.mark.parametrize("name", ["invalid_banned_words.json", "invalid_meeting_promise.json",
                                  "invalid_person_name.json", "invalid_bad_url.json"])
def test_export_refuses_invalid(tmp_path, name):
    with pytest.raises(ExportError):
        build_pdf(load(name), "clean", tmp_path / "x.pdf")


def _source_urls(pitch):
    return {s["url"] for c in pitch["cards"] for s in c["sources"]}


def test_review_pdf_links(tmp_path, valid):
    out = build_pdf(valid, "review", tmp_path / "review.pdf")
    uris, internal = links(out)
    assert uris, "no link annotations in the Review PDF"
    deep = [u for u in uris if "#:~:text=" in u]
    for c in valid["cards"]:
        for s in c["sources"]:
            if s.get("quote"):
                assert deep_link(s["url"], s["quote"]) in uris
    assert {u.split("#")[0] for u in deep} <= _source_urls(valid)
    assert _source_urls(valid) <= set(uris)          # "Open page" for every source
    assert internal >= 10                            # [n] -> evidence, back to claim
    txt = text_of(out).replace(" ", "")
    assert "Reviewcopy" in txt
    assert "onleaveinNovember" in txt                # officer notes are in the review
    assert "Verifiedquote" in txt
    assert "skinwornsensorsandsignalprocessingmethods" in txt   # the quote


def test_clean_pdf_links_and_no_internal_notes(tmp_path, valid):
    out = build_pdf(valid, "clean", tmp_path / "clean.pdf")
    uris, internal = links(out)
    assert set(uris) >= _source_urls(valid)
    assert not any("#:~:text=" in u for u in uris)   # plain page links only
    assert {u for u in uris if not u.startswith("mailto:")} == _source_urls(valid)
    assert internal >= 1                             # [n] -> numbered source list
    txt = text_of(out).replace(" ", "")              # font kerning can split words
    for leak in ("on leave in November", "Officer note", "Review copy", "Verified quote",
                 "Invented example. Maker", "demo-0001", "Erika", "Muster"):
        assert leak.replace(" ", "") not in txt, leak
    assert "TalveraSensorikGmbH" in txt
    assert "Prototype,notanofficialCantonofBerntool" in txt
    assert "LedbyDr.MaxBeispiel" in txt              # named because the officer switched it on


def test_pdf_has_bookmarks(tmp_path, valid):
    out = build_pdf(valid, "review", tmp_path / "r.pdf")
    titles = [o.title for o in PdfReader(str(out)).outline if not isinstance(o, list)]
    assert "Matches" in titles and "Evidence" in titles


def test_missing_logo_falls_back_to_text(tmp_path, valid, monkeypatch):
    monkeypatch.setattr(build, "LOGO_PATH", tmp_path / "nope.png")
    out = build_pdf(valid, "clean", tmp_path / "c.pdf")
    txt = text_of(out).replace(" ", "")
    assert "BernEconomicDevelopmentAgency" in txt.split("Bernforwearable")[0]


def test_logo_used_when_present(valid):
    if not build.LOGO_PATH.is_file():
        pytest.skip("logo not on this machine")
    assert build.LOGO_PATH.as_uri() in build.render_html(valid, "clean")


def test_html_is_escaped(valid):
    valid["cards"][0]["text"] = "Sensors <script>x</script> for metabolism."
    html = build.render_html(valid, "clean")
    assert "<script>x" not in html and "&lt;script&gt;" in html


def test_long_german_unit_name_renders(tmp_path, valid):
    valid["lang"] = "de"
    valid["cards"][3]["unit"] = ("Universitätsklinik für Sensorgestützte Stoffwechselüberwachung, "
                                 "Bewegungsphysiologie, Ernährungsmedizin und Metabolismus")
    out = build_pdf(valid, "clean", tmp_path / "de.pdf")
    assert Path(out).stat().st_size > 1000


# ---------------------------------------------------------------- CLI

def test_cli(tmp_path):
    out = tmp_path / "cli.pdf"
    ok = subprocess.run([sys.executable, "-m", "pitch.build", str(FIX / "valid.json"),
                         "--mode", "review", "--out", str(out)],
                        cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert out.exists()
    bad = subprocess.run([sys.executable, "-m", "pitch.build", str(FIX / "invalid_no_source.json"),
                          "--mode", "clean", "--out", str(tmp_path / "bad.pdf")],
                         cwd=ROOT, capture_output=True, text=True)
    assert bad.returncode == 1
    assert "has no source" in bad.stderr
    assert not (tmp_path / "bad.pdf").exists()
