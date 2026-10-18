"""Entity resolution fixtures (gaps 12, 13). Invented people only."""
import json, sqlite3
from pathlib import Path

from ingest import extract as E, resolve as R

FX = Path(__file__).parent / "fixtures" / "extract"


def test_two_muellers_stay_two(tmp_path):
    fx = json.loads((FX / "two_muellers.json").read_text())
    recs, _ = E.run(fx["pages"], llm=None, use_llm=False, workers=2, out=tmp_path / "f.jsonl", stats_path=None)
    R.build(recs, tmp_path / "kg.sqlite")
    c = sqlite3.connect(tmp_path / "kg.sqlite")
    people = c.execute("SELECT id, name, seniority FROM person ORDER BY name").fetchall()
    assert len(people) == fx["expect"]["n_persons"], people
    by = {}
    for pid, name, sen in people:
        by.setdefault(name, []).append((pid, sen))
    assert len(by["Thomas Müller"]) == 1 and len(by["Tobias Müller"]) == 1
    assert len(by["Anna Müller"]) == 2                    # same name, different units, different emails
    thomas = by["Thomas Müller"][0][0]
    units = {r[0] for r in c.execute("SELECT DISTINCT unit_id FROM affiliation WHERE person_id=?", (thomas,))}
    assert len(units) == 2                                 # cardio lab + second source
    rules = {r[0] for r in c.execute("SELECT rule FROM merge_log WHERE kept_id=?", (thomas,))}
    assert "name+email_local" in rules
    assert c.execute("SELECT count(*) FROM possible_same").fetchone()[0] >= 1
    # DE and EN sibling pages: one Anna (PhD) in the cardio lab, with evidence from both languages
    anna_phd = [p for p, s in by["Anna Müller"] if s == 0.4]
    assert len(anna_phd) == 1
    langs = {r[0] for r in c.execute("SELECT DISTINCT lang FROM evidence WHERE entity_id=?", (anna_phd[0],))}
    assert langs == {"de", "en"}
    # former members are mentions, not people
    assert "Otto Gewesen" not in by
    # seniority (gap 13)
    assert by["Thomas Müller"][0][1] == 1.0


def test_resolve_id_follows_merge_log(tmp_path):
    fx = json.loads((FX / "two_muellers.json").read_text())
    recs, _ = E.run(fx["pages"], llm=None, use_llm=False, workers=1, out=tmp_path / "f.jsonl", stats_path=None)
    R.build(recs, tmp_path / "kg.sqlite")
    c = sqlite3.connect(tmp_path / "kg.sqlite")
    for kept, merged in c.execute("SELECT kept_id, merged_id FROM merge_log"):
        assert R.resolve_id(c, merged) == kept


def test_seniority_weights():
    assert R.function_class("Group Head")[1] == 1.0
    assert R.function_class("Senior Scientist")[1] == 0.8
    assert R.function_class("Postdoctoral Researcher")[1] == 0.6
    assert R.function_class("PhD Student")[1] == 0.4
    assert R.function_class("Doktorandin")[1] == 0.4
