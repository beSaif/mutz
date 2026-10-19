"""The clean PDF the company gets reads as a plain document: claims with [n] and the source list, no
verification marks. The review copy for the agency keeps every mark (status, quote, "Open at quote")."""
import pytest

from pitch import build
from pitch.build import build_pdf
from pitch.locales import tr
from pitch.tests.test_pitch import load, text_of

# the verification marks, as English keys of pitch/locales; each is looked for in the pitch's own language
MARKS = ["Verified quote", "Edited by you", "Card edited by you", "Needs a check", "Open at quote",
         "verified quote", "edited by you", "added by you", "No quote stored. Check the page by hand.",
         "Review copy. Every claim shows its quote and source. Send the clean PDF to the company."]
TICKS = ["✓", "✔", "☑", "&#10003;", "&#x2713;"]   # ✓ ✔ ☑


@pytest.fixture
def pitch():
    p = load("valid.json")
    p["cards"][1]["sources"][0]["quote"] = ""   # one card without a quote: "Needs a check" in review
    return p                                    # card 5 is edited: "Edited by you" in review


def _marks(lang):
    return [tr(lang, m) for m in MARKS]


@pytest.mark.parametrize("lang", ["en", "de", "fr", "it"])
def test_clean_html_has_no_verification_marks(pitch, lang):
    pitch["lang"] = lang
    clean = build.render_html(pitch, "clean")
    for m in _marks(lang) + TICKS:
        assert m not in clean, m
    for cls in ('class="status', 'class="evline', 'class="ev"', 'class="banner', 'class="internal'):
        assert cls not in clean, cls
    assert "#:~:text=" not in clean                    # no deep links to the quote either
    for c in pitch["cards"]:
        for s in c["sources"]:
            if s.get("quote"):
                assert s["quote"] not in clean
    assert '<sup class="ref"><a href="#src-1">[1]</a></sup>' in clean   # the claims keep their citations
    assert '<ol class="sources">' in clean and 'id="src-1"' in clean    # and the source list stays


@pytest.mark.parametrize("lang", ["en", "de", "fr", "it"])
def test_review_html_keeps_the_marks(pitch, lang):
    pitch["lang"] = lang
    review = build.render_html(pitch, "review")
    for status in ("verified", "edited", "needs_check"):
        assert f'<span class="status {status}">{tr(lang, build.STATUS_LABELS[status])}</span>' in review
    for m in ("Verified quote", "Open at quote", "Card edited by you", "No quote stored. Check the page by hand."):
        assert tr(lang, m) in review, m
    assert 'class="evline"' in review and "#:~:text=" in review


@pytest.mark.parametrize("lang", ["en", "de"])
def test_clean_pdf_text_has_no_marks_and_review_does(tmp_path, pitch, lang):
    pitch["lang"] = lang
    clean = text_of(build_pdf(pitch, "clean", tmp_path / "clean.pdf")).replace(" ", "")
    review = text_of(build_pdf(pitch, "review", tmp_path / "review.pdf")).replace(" ", "")
    for m in _marks(lang) + TICKS:
        assert m.replace(" ", "") not in clean, m
    assert "[1]" in clean and "https://research.example.org/groups/metabolic-sensing" in clean
    for m in ("Verified quote", "Edited by you", "Needs a check", "Open at quote"):
        assert tr(lang, m).replace(" ", "") in review, m
