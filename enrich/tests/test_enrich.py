"""Tests for enrich/: curated list format, the conservative SNSF matching rule, grants_for()."""
import json
import re
from pathlib import Path

import pytest

from enrich import snsf

ROOT = Path(__file__).resolve().parents[2]
CURATED = ROOT / "enrich" / "curated.json"
KINDS = {"transfer", "clinical_trials", "funding", "infrastructure", "talent", "space", "other"}
NEED_TYPES = {"research", "infrastructure", "supplier", "funding", "transfer", "other"}
FIELDS = {"id", "name", "kind", "need_types", "description", "url", "source_url", "region", "checked"}


# ---------------------------------------------------------------- curated.json

def test_curated_every_entry_has_source_url_and_contract_fields():
    entries = json.loads(CURATED.read_text())
    assert isinstance(entries, list) and len(entries) >= 10
    ids = set()
    for e in entries:
        assert FIELDS <= set(e), e.get("id")
        assert re.match(r"^https://[^\s/]+\.[a-z]{2,}(/\S*)?$", e["source_url"]), e["id"]
        assert e["url"].startswith("https://"), e["id"]
        assert e["kind"] in KINDS, e["id"]
        assert e["need_types"] and set(e["need_types"]) <= NEED_TYPES, e["id"]
        assert isinstance(e["checked"], bool)
        assert e["description"].strip() and e["name"].strip()
        assert e["id"] not in ids
        ids.add(e["id"])


def test_curated_unibe_and_dcberne_entries_are_unchecked():
    # we never fetch unibe.ch or dcberne.com outside crawl.py, so those entries cannot be checked
    for e in json.loads(CURATED.read_text()):
        if re.search(r"(unibe\.ch|dcberne\.com)", e["source_url"]):
            assert e["checked"] is False, e["id"]


def test_curated_covers_the_gap7_needs():
    entries = json.loads(CURATED.read_text())
    kinds = {e["kind"] for e in entries}
    assert {"transfer", "clinical_trials", "funding", "infrastructure", "talent", "space"} <= kinds
    names = " ".join(e["name"] for e in entries)
    for must in ("Unitectra", "CTU Bern", "Innosuisse", "sitem-insel", "Biel/Bienne", "Bern Economic Development Agency"):
        assert must in names
    assert any("funding" in e["need_types"] for e in entries)


def test_curated_has_no_personal_data():
    text = CURATED.read_text()
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", text)      # no emails
    assert not re.search(r"\+41|\b0\d{2} ?\d{3} ?\d{2} ?\d{2}\b", text)   # no phones
    assert not re.search(r"\b(Prof|Dr)\.", text)


# ---------------------------------------------------------------- normalisation

def test_name_keys():
    assert snsf.snsf_key("Hans-Peter", "Müller") == ("hans-peter", "mueller")
    assert ("anna", "mueller") in snsf.kg_keys("Prof. Dr. med. Anna Maria Müller")
    assert ("anna", "maria mueller") in snsf.kg_keys("Prof. Dr. med. Anna Maria Müller")
    assert snsf.kg_keys("Müller, Anna") == {("anna", "mueller")}
    assert snsf.kg_keys("Dr. Mueller") == set()                       # surname alone: no key
    assert snsf.kg_keys("Carla Pou i Miralbell") >= {("carla", "pou miralbell")} or \
        ("carla", "miralbell") in snsf.kg_keys("Carla Pou i Miralbell")
    assert ("ying", "ma") in snsf.kg_keys("Dr. Ying Ma")              # short surnames survive


def test_is_bern():
    assert snsf.is_bern("University of Berne – BE", "")
    assert snsf.is_bern("Other Hospitals – ASPIT", "Universitätsklinik für Kardiologie Inselspital Bern")
    assert not snsf.is_bern("University of Zurich – ZH", "Universitätsspital Zürich")
    assert not snsf.is_bern("Berne University of Applied Sciences – BFH", "Departement Technik")


# ---------------------------------------------------------------- matching rule (gap 12)

def sp(num, first, last, institute="", orcid=None, roles=None):
    return {"person_number": num, "first": first, "last": last, "orcid": orcid,
            "institute": institute, "key": snsf.snsf_key(first, last), "roles": roles or {},
            "_grant_institutes": []}


def kp(eid, name, units=(), orcid=None):
    return {"entity_id": eid, "name": name, "orcid": orcid, "unit_names": list(units)}


def test_unique_full_name_links():
    links = snsf.match([kp("person:a", "Prof. Dr. Stavroula Mougiakakou", ["ARTORG Center"])],
                       [sp("1", "Stavroula", "Mougiakakou", "ARTORG Center Universität Bern")])
    assert links["person:a"][0]["person_number"] == "1"
    assert links["person:a"][1] == "name+institution+unit"


def test_surname_alone_never_links():
    links = snsf.match([kp("person:a", "Hans Müller")], [sp("1", "Peter", "Müller")])
    assert links == {}
    links = snsf.match([kp("person:a", "Hans Müller")], [sp("1", "Hans-Peter", "Müller")])
    assert links == {}


def test_two_snsf_candidates_without_unit_evidence_is_ambiguous():
    links = snsf.match([kp("person:a", "Thomas Müller", ["Institute of Pathology"])],
                       [sp("1", "Thomas", "Müller", "Institut für Geologie"),
                        sp("2", "Thomas", "Müller", "Historisches Institut")])
    assert links == {}


def test_two_snsf_candidates_unit_breaks_the_tie():
    links = snsf.match([kp("person:a", "Thomas Müller", ["Institute of Pathology"])],
                       [sp("1", "Thomas", "Müller", "Institut für Geologie"),
                        sp("2", "Thomas", "Müller", "Institute of Pathology University of Bern")])
    assert links["person:a"][0]["person_number"] == "2"


def test_two_kg_mullers_one_snsf_record():
    # two kg records with one name in different units (gap 12 fixture): only the unit confirmed one links
    kg = [kp("person:m1", "Anna Müller", ["ARTORG Center"]),
          kp("person:m2", "Anna Müller", ["Institute of Social and Preventive Medicine"])]
    links = snsf.match(kg, [sp("9", "Anna", "Müller", "ARTORG Center for Biomedical Engineering")])
    assert set(links) == {"person:m1"}
    # and when the unit says nothing, neither links
    links = snsf.match(kg, [sp("9", "Anna", "Müller", "Departement Biologie")])
    assert links == {}


def test_orcid_wins():
    links = snsf.match([kp("person:a", "A. Smith", orcid="0000-0002-1825-0097")],
                       [sp("5", "Alice", "Smith", orcid="0000-0002-1825-0097")])
    assert links["person:a"][1] == "orcid"


def test_recency_labels():
    assert snsf.recency("2027-01-31", today="2026-10-18") == "active"
    assert snsf.recency("2022-01-31", today="2026-10-18") == "quiet"
    assert snsf.recency("2015-01-31", today="2026-10-18") == "past"
    assert snsf.recency(None) is None


# ---------------------------------------------------------------- grants_for contract

def test_grants_for_contract_on_a_small_file(tmp_path):
    f = tmp_path / "grants.jsonl"
    rows = [
        {"entity_id": "person:x", "title": "Old", "number": "1", "start": "2010-01-01", "end": "2013-01-01",
         "amount_chf": 100, "role": "co-applicant", "url": "https://data.snf.ch/grants/grant/1",
         "match_rule": "name+institution", "source": "SNSF"},
        {"entity_id": "person:x", "title": "New", "number": "2", "start": "2024-01-01", "end": "2028-01-01",
         "amount_chf": 200, "role": "responsible applicant", "url": "https://data.snf.ch/grants/grant/2",
         "match_rule": "name+institution", "source": "SNSF"},
    ]
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = snsf.grants_for("person:x", path=f)
    assert [g["number"] for g in out] == ["2", "1"]                    # newest first
    for g in out:
        assert set(snsf.CONTRACT_FIELDS) <= set(g)
    assert snsf.grants_for("person:unknown", path=f) == []
    assert snsf.grants_for("person:x", path=tmp_path / "missing.jsonl") == []


@pytest.mark.skipif(not (snsf.OUT.exists() and snsf.KG.exists()), reason="needs data/enrich/grants.jsonl and kg.sqlite")
def test_real_grants_show_for_kg_people():
    import sqlite3
    ids = {json.loads(l)["entity_id"] for l in open(snsf.OUT)}
    assert len(ids) >= 20
    con = sqlite3.connect(f"file:{snsf.KG}?mode=ro", uri=True)
    kg_ids = {r[0] for r in con.execute("SELECT id FROM person")}
    con.close()
    linked_in_kg = ids & kg_ids
    assert len(linked_in_kg) >= 0.9 * len(ids)       # kg may have been rebuilt since; most ids stay stable
    eid = sorted(linked_in_kg)[0]
    gs = snsf.grants_for(eid)
    assert gs and all(g["url"].startswith("https://data.snf.ch/grants/grant/") for g in gs)
