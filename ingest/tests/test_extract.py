"""Extraction + verifier fixtures (gap 3). Invented text only, no gateway calls: the 8B output is canned."""
import json, sqlite3
from pathlib import Path

import pytest

from ingest import extract as E, resolve as R, verify as V

FX = Path(__file__).parent / "fixtures" / "extract"


def load(name):
    return json.loads((FX / name).read_text())


def build(fx, tmp_path):
    canned = fx["llm"]
    calls = []

    def fake_llm(page):
        calls.append(page["page_id"])
        return canned.get(page["page_id"], {}), False, {}

    recs, stats = E.run(fx["pages"], llm=fake_llm, workers=2, out=tmp_path / "facts.jsonl", stats_path=None)
    R.build(recs, tmp_path / "kg.sqlite")
    return sqlite3.connect(tmp_path / "kg.sqlite"), stats, calls


def names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM person")}


def test_planted_lie(tmp_path):
    fx = load("planted_lie.json")
    conn, stats, _ = build(fx, tmp_path)
    ex = fx["expect"]
    assert names(conn) == set(ex["persons"])
    fn = {r[0]: r[1] for r in conn.execute("SELECT p.name, a.function FROM person p JOIN affiliation a ON a.person_id=p.id")}
    assert fn == ex["functions"]                          # the planted "Director of the Institute..." is gone
    labels = {r[0] for r in conn.execute("SELECT label FROM expertise")}
    assert set(ex["expertise"]) <= labels and not labels & set(ex["not_expertise"])
    assert conn.execute("SELECT count(*) FROM infrastructure").fetchone()[0] == ex["infrastructure"]
    assert stats["facts_dropped"] >= 4                     # lie role, invented person, invented topic, 1 word quote
    # every fact in the KG has evidence whose quote is on the page
    text = fx["pages"][0]["text"]
    for q, in conn.execute("SELECT quote FROM evidence WHERE method='llm'"):
        assert V.in_page(q, text)


def test_external_collaborator(tmp_path):
    fx = load("external_collaborator.json")
    conn, _, _ = build(fx, tmp_path)
    assert names(conn) == {"Lena Probe"}
    assert "Xaver Fremd" not in names(conn)
    men = {r[0] for r in conn.execute("SELECT name FROM mention")}
    assert "Prof. Xaver Fremd" in men


def test_coauthor_list(tmp_path):
    fx = load("coauthor_list.json")
    conn, _, calls = build(fx, tmp_path)
    assert names(conn) == set()                            # zero invented Bern people
    assert "fx_pub_0000001" not in calls                   # publications pages never go to the 8B


def test_verifier_rules():
    page = "Dr. Anna Beispiel is the group head of the Retinal Imaging Lab and leads a team of six researchers."
    assert not V.in_page("diabetes mellitus type two", page)          # short quotes need an exact match
    f = {"kind": "expertise", "label": "diabetes", "quote": "diabetes"}
    assert V.check_llm_fact(f, page + " diabetes")[1] == "quote_too_short"
    assert V.words("a b c d e f g") < V.MIN_WORDS
    assert V.contains_name(page, "Prof. Dr. Anna Beispiel")
    assert not V.contains_name("Beispiel leads a team of six researchers in the lab today", "Anna Beispiel")
    assert V.external_or_citation("In collaboration with Prof. X Y from ETH Zurich we test things") == "external"
    assert V.external_or_citation("Probe L, Fremd X, Muster K. Title. J Sens. 2023;12:45") == "coauthor"
    assert V.external_or_citation(page) is None


def test_salvage_truncated_tool_call():
    s = '{"expertise": [{"label": "a b", "quote": "q"}, {"label": "c", "quote": "unfinished'
    assert E.salvage_json(s) == {"expertise": [{"label": "a b", "quote": "q"}]}


def test_infrastructure_not_devices(tmp_path):
    fx = load("infrastructure_vs_devices.json")
    conn, _, _ = build(fx, tmp_path)
    infra = {r[0] for r in conn.execute("SELECT name FROM infrastructure")}
    assert infra == set(fx["expect"]["infrastructure"])
    kinds = {r[0]: r[1] for r in conn.execute("SELECT name, kind FROM infrastructure")}
    assert kinds["biomechanical testing service"] == "service"
    dev = {r[0] for r in conn.execute("SELECT value FROM evidence WHERE field='device' AND entity_id LIKE 'project:%'")}
    assert dev == set(fx["expect"]["devices_on_project"])
