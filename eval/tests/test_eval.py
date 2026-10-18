"""Tests for the eval harness itself.  Run: .venv/bin/python -m pytest eval/tests -q  (from the repo root)."""
import json
import re
from pathlib import Path

import pytest

from eval import data, metrics
from eval.run_eval import GOLD, LOCK, SYNTH, eval_case, heldout_digest
from eval.targets import KG, key_exists_in_crawl, key_matches_url, result_matches

GOLD_D = json.loads(GOLD.read_text())
HAVE_CRAWL = data.PAGES.exists()


# ---- gap 17: no contact data in committed eval files ----

@pytest.mark.parametrize("path", [GOLD, SYNTH, LOCK])
def test_no_contact_data(path):
    if not path.exists():
        pytest.skip(f"{path} not generated yet")
    text = path.read_text()
    # pattern built from parts so the gap 17 repo grep does not match this test file itself
    assert not re.search("|".join(["@" + d for d in ("unibe", "dcberne", "insel")] + [r"\+" + "41"]), text)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "email address in eval file"
    assert not re.search(r"\b0\d{2}[ ]?\d{3}[ ]?\d{2}[ ]?\d{2}\b", text), "phone number in eval file"


def test_gold_shape_and_size():
    cases = GOLD_D["cases"]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    assert sum(c["origin"] == "playbook" for c in cases) == 39
    unibe = [c for c in cases if c["origin"] == "unibe_slice"]
    assert len({c["id"].rsplit("_", 1)[0] for c in unibe}) == 14 and len(unibe) == 42  # 3 per unibe site
    for c in cases:
        assert c["split"] in ("dev", "held")
        if c["check"] in ("recall", "fields"):
            assert c["targets"], c["id"]
        for t in c["targets"]:
            for k in t["keys"]:
                assert k["path"].startswith("/") and "@" not in json.dumps(k)


def test_heldout_lock_matches():
    assert LOCK.exists(), "run python -m eval.run_eval --lock"
    assert json.loads(LOCK.read_text())["sha256"] == heldout_digest(GOLD_D)[0], "held out set changed after locking"


@pytest.mark.skipif(not HAVE_CRAWL, reason="no crawl snapshot")
def test_every_gold_key_is_a_crawled_page():
    missing = [(c["id"], k) for c in GOLD_D["cases"] for t in c["targets"] for k in t["keys"] if not key_exists_in_crawl(k)]
    assert not missing, missing


# ---- gap 11: adjacent results never count for recall ----

def _res(eid, url, adjacent=False, **kw):
    r = {"entity_id": eid, "type": "unit", "name": eid, "unit": eid, "function": None, "score": 1.0,
         "coverage": "strong", "recency": "active", "reasons": [{"chunk_id": None, "url": url, "quote": "x", "deep_link": url}]}
    if adjacent:
        r["tags"] = ["adjacent"]
    r.update(kw)
    return r


@pytest.mark.skipif(not HAVE_CRAWL, reason="no crawl snapshot")
def test_adjacent_excluded_from_recall():
    case = next(c for c in GOLD_D["cases"] if c["id"] == "tc04")  # cochlear implant -> HRL
    hrl = "https://www.artorg.unibe.ch/research/hrl/index_eng.html"
    other = "https://www.artorg.unibe.ch/research/msl/index_eng.html"
    kg = KG()
    kg.available = False  # judge by URL keys only
    only_adjacent = {"results": [_res("x:hrl", hrl, adjacent=True)], "searched_sources": ["artorg"]}
    assert eval_case(case, only_adjacent, kg, 10)["recall"] == 0
    core = {"results": [_res("x:hrl", hrl, adjacent=True), _res("y:msl", other), _res("z:hrl", hrl)], "searched_sources": ["artorg"]}
    rec = eval_case(case, core, kg, 10)
    assert rec["recall"] == 1 and rec["targets"][0]["rank"] == 2   # adjacent one does not take a rank either
    assert metrics.is_adjacent({"adjacent": True}) and metrics.is_adjacent({"tag": "adjacent"})
    assert not metrics.is_adjacent({"tags": ["strong"]})


def test_key_matching():
    data.host_sources.cache_clear()
    hs = data.host_sources()
    if "www.artorg.unibe.ch" not in hs:
        pytest.skip("no crawl index")
    k = {"source": "artorg", "path": "/research/msl/"}
    assert key_matches_url(k, "https://www.artorg.unibe.ch/research/msl/facilities/index_eng.html")
    assert not key_matches_url(k, "https://www.artorg.unibe.ch/research/hrl/index_eng.html")
    exact = {"source": "artorg", "path": "/research/index_eng.html", "exact": True, "names": ["Witthauer"]}
    assert not key_matches_url(exact, "https://www.artorg.unibe.ch/research/msl/index_eng.html")
    t = {"label": "x", "keys": [exact]}
    r = _res("e", "https://www.artorg.unibe.ch/research/index_eng.html", name="Prof. Dr. phil. Lilian Witthauer")
    kg = KG()
    assert result_matches(r, t, set(), kg) == "reason_url"
    r["name"] = r["unit"] = "Someone Else"
    assert result_matches(r, t, set(), kg) is None


# ---- citation validity ----

def test_quote_matching():
    text = "# Group\n\n- Name / Titel\n- The   lab develops **optical** sensors for surgery. It also builds devices."
    assert metrics.quote_in_text("The lab develops optical sensors for surgery.", text)
    assert metrics.quote_in_text("the lab develops optical sensors … builds devices", text)
    assert not metrics.quote_in_text("The lab develops glucose sensors", text)
    assert not metrics.quote_in_text("", text)


@pytest.mark.skipif(not data.CHUNKS.exists(), reason="no chunks")
def test_check_reason_against_real_chunk():
    c = next(iter(data.chunks().values()))
    words = c["text"].split()
    good = {"chunk_id": c["chunk_id"], "url": c["url"], "quote": " ".join(words[:8])}
    assert metrics.check_reason(good)[0]
    bad = dict(good, quote="this sentence was invented by the model and is on no page")
    assert metrics.check_reason(bad) == (False, "quote_not_found")
    assert metrics.check_reason(dict(good, url=""))[1] == "no_url"


# ---- other metrics ----

def test_parity_and_exposure():
    assert metrics.jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)
    assert metrics.overlap(["a", "b"], ["b"]) == 1.0
    p = metrics.pairwise({"g": [["a", "b"], ["a", "b"], ["c"]]})
    assert p["pairs"] == 3 and 0 < p["jaccard"] < 1
    even = metrics.exposure([(u, 1) for u in "abcd"])
    skew = metrics.exposure([("a", 1)] * 10 + [("b", 2)])
    assert even["gini"] == 0 and skew["gini"] > 0 and skew["top1_share"] > even["top1_share"]
    assert metrics.exposure([], {"u1"})["units_exposed"] == 0


def test_stub_follows_contract():
    if not data.CHUNKS.exists():
        pytest.skip("no chunks")
    from eval.stub_query import query
    out = query("cochlear implant", k=5)
    assert set(out) >= {"expansions", "searched_sources", "results"}
    for r in out["results"]:
        assert set(r) >= {"entity_id", "type", "name", "unit", "function", "score", "coverage", "recency", "reasons"}
        assert r["coverage"] in ("strong", "thin", "none")
        for x in r["reasons"]:
            assert set(x) >= {"chunk_id", "url", "quote", "deep_link"}
            assert metrics.check_reason(x)[0]
