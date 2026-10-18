"""Contract tests on the real index. Skipped when data/chunks.jsonl is not there.

Fast mode (default): MUTZ_NO_RERANK=1, expansions passed as [] so no gateway and no model weights.
Set MUTZ_TEST_MODELS=1 to run the same tests with the real BGE-M3 + reranker.
"""
import json, os, subprocess, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not (ROOT / "data" / "chunks.jsonl").exists(), reason="no chunks yet")

if os.environ.get("MUTZ_TEST_MODELS") != "1":
    os.environ["MUTZ_NO_RERANK"] = "1"
    os.environ["MUTZ_NO_DENSE"] = "1"
    os.environ["MUTZ_NO_JUDGE"] = "1"
os.chdir(ROOT)

from search import query as Q  # noqa: E402


def _chunk_text(cid):
    return next(c["text"] for c in Q.R.chunks if c["chunk_id"] == cid)


@pytest.mark.parametrize("q", ["glucose sensors", "CGM", "hearing implants"])
def test_contract(q):
    out = Q.query(q, k=10, expansions=[])
    assert set(["expansions", "searched_sources", "results"]) <= set(out)
    assert out["searched_sources"] and "artorg" in out["searched_sources"] and "dcb" in out["searched_sources"]
    assert out["results"], out.get("message")
    for r in out["results"]:
        for key in ("entity_id", "type", "name", "unit", "function", "score", "coverage", "recency", "reasons"):
            assert key in r
        assert r["coverage"] in ("strong", "thin", "none")
        assert r["recency"] in ("active", "quiet", "past")
        assert r["reasons"]
        for rs in r["reasons"]:
            assert rs["quote"] and rs["quote"] in _chunk_text(rs["chunk_id"])     # verbatim
            assert rs["deep_link"].startswith(rs["url"] + "#:~:text=")


def test_unit_cap():
    out = Q.query("diabetes", k=10, expansions=[])
    per = {}
    for r in out["results"]:
        per[r["unit_id"] or r["entity_id"]] = per.get(r["unit_id"] or r["entity_id"], 0) + 1
    assert max(per.values()) <= Q.UNIT_CAP


def test_supplier_not_covered():
    out = Q.query("contract manufacturer for microfluidic chips", expansions=[])
    assert out["results"] == [] and out["coverage"] == "not_covered"
    assert "not covered yet" in out["message"]


def test_no_match_lists_sources():
    out = Q.query("zzqx frobnication of quarks in tokamak", expansions=[])
    assert out["results"] == [] and out["abstained"]
    assert out["searched_sources"] and "not found in the indexed sources" in out["message"]
    assert out["suggested_sources"]


def test_sources_filter():
    out = Q.query("glucose", sources=["artorg"], expansions=[])
    assert out["searched_sources"] == ["artorg"]
    assert all(rs["url"].startswith("https://www.artorg") for r in out["results"] for rs in r["reasons"])


def test_why_not():
    q = "hearing implants"
    out = Q.query(q, expansions=[])
    shown = out["results"][0]
    w = Q.why_not(shown["entity_id"], q)
    assert w["in_index"] and w["rank"] and w["shown"] and w["stages"]
    # someone indexed but not shown
    import sqlite3
    con = sqlite3.connect(Q.RANKLOG)
    eid = con.execute("select entity_id from rank_entry where query_id=? and shown=0 and type='person' limit 1",
                      (out["query_id"],)).fetchone()[0]
    w = Q.why_not(Q.R.ent[eid]["name"], q)
    assert w["in_index"] and w["entity_id"] == eid and not w["shown"] and w["reason"]
    # any indexed person, even one never retrieved for this query
    any_person = next(e for e, v in Q.R.ent.items() if v["type"] == "person")
    w = Q.why_not(any_person, q)
    assert w["in_index"] and w["reason"]
    # nobody by that name
    w = Q.why_not("Prof. Dr. Nonexistent Personname", q)
    assert w["in_index"] is False and w["searched_sources"]
