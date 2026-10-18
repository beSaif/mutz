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


def test_collaborators_and_emeriti_are_not_senior_members(tmp_path):
    """External partner listed on a group page (with a profile page there) and an emeritus get 0.5; project team
    names with a company role are mentions, not people."""
    def box(name, func):
        return (f'<article class="team-box"><dl class="left"><dd><a href="#">{name}</a></dd>'
                f'<dd class="jobTitle">{func}</dd></dl></article>')
    html = ("<html><body><h1>Team</h1><h2>Group Head</h2>" + box("Prof. Dr. Lena Probe", "Group Head") +
            "<h2>Clinical Research Partners</h2>" + box("Prof. Dr. med. Xaver Fremd", "Director and Chief Physician") +
            "<h2>Senior Staff</h2>" + box("Prof. em. Dr. Otto Alt", "Senior Advisor") + "</body></html>")
    base = {"source": "fixture_a", "group": "fixture_a", "lang": "en", "fetched_at": "2026-10-18T12:00:00+00:00",
            "breadcrumbs": [], "emails": []}
    prof = ('<html><body><article class="team-detail"><h1 itemprop="name">Prof. Dr. med. Xaver Fremd</h1>'
            '<p itemprop="jobTitle">Director and Chief Physician</p></article></body></html>')
    pages = [
        {**base, "page_id": "fx_col_list", "url": "https://fixture.example/research_groups/eye_lab/team/index_eng.html",
         "page_type": "group", "sibling_group": "c1", "title": "Team", "html": html,
         "text": "Team Prof. Dr. Lena Probe Group Head Prof. Dr. med. Xaver Fremd Prof. em. Dr. Otto Alt"},
        {**base, "page_id": "fx_col_prof", "url": "https://fixture.example/research_groups/eye_lab/team/fremd/index_eng.html",
         "page_type": "person", "sibling_group": "c2", "title": "Fremd", "html": prof,
         "text": "Prof. Dr. med. Xaver Fremd Director and Chief Physician"},
        {**base, "page_id": "fx_col_proj", "url": "https://fixture.example/en/project/nightwatch/", "page_type": "project",
         "sibling_group": "c3", "title": "Nightwatch", "html": "<html><body><p>x</p></body></html>",
         "text": "Nightwatch\nProject team: Prof. Lena Probe, Karl Firma (Founder & CEO, Example AG)"},
    ]
    recs, _ = E.run(pages, llm=None, use_llm=False, workers=1, out=tmp_path / "f.jsonl", stats_path=None)
    R.build(recs, tmp_path / "kg.sqlite")
    c = sqlite3.connect(tmp_path / "kg.sqlite")
    sen = {n: s for n, s in c.execute("SELECT name, seniority FROM person")}
    assert sen["Xaver Fremd"] == 0.5 and sen["Otto Alt"] == 0.5 and sen["Lena Probe"] == 1.0
    assert "Karl Firma" not in sen
    cls = {r[0] for r in c.execute("SELECT a.function_class FROM affiliation a JOIN person p ON p.id=a.person_id "
                                   "WHERE p.name='Xaver Fremd'")}
    assert cls == {"collaborator"}


def test_spelling_variant(tmp_path):
    """Invented: 'Mira Kalnova' and 'Mira Kalnava' in one lab merge (spelling_variant); a project team line with
    the variant joins her; the same variant in another unit only becomes possible_same."""
    def box(name, func):
        return (f'<article class="team-box"><dl class="left"><dd><a href="#">{name}</a></dd>'
                f'<dd class="jobTitle">{func}</dd></dl></article>')
    base = {"source": "fixture_a", "group": "fixture_a", "lang": "en", "fetched_at": "2026-10-18T12:00:00+00:00",
            "breadcrumbs": [], "emails": []}
    def listing(pid, lab, names):
        return {**base, "page_id": pid, "url": f"https://fixture.example/research_groups/{lab}/team/index_eng.html",
                "page_type": "group", "sibling_group": pid, "title": "Team",
                "html": "<html><body><h1>Team</h1><h2>Team</h2>" + "".join(box(n, f) for n, f in names) + "</body></html>",
                "text": "Team " + " ".join(n for n, _ in names)}
    pages = [
        listing("sv1", "eye_lab", [("Mira Kalnova", "PhD Candidate"), ("Dr. Mira Kalnava", "Research Associate")]),
        {**base, "page_id": "sv2", "url": "https://fixture.example/en/project/insulin-x/", "page_type": "project",
         "sibling_group": "sv2", "title": "Insulin X", "html": "<html><body><p>x</p></body></html>",
         "text": "Insulin X\nProject team: Mira Kalnovaa, Dr. Ole Test"},
        listing("sv3", "bone_lab", [("Mira Kalnovo", "Postdoc")]),
        listing("sv4", "skin_lab", [("Hans Meier", "Group Head"), ("Hans Maier", "Postdoc"), ("Anna Meier", "PhD")]),
    ]
    recs, _ = E.run(pages, llm=None, use_llm=False, workers=1, out=tmp_path / "f.jsonl", stats_path=None)
    R.build(recs, tmp_path / "kg.sqlite")
    c = sqlite3.connect(tmp_path / "kg.sqlite")
    names = sorted(r[0] for r in c.execute("SELECT name FROM person"))
    mira = [n for n in names if n.startswith("Mira")]
    assert len(mira) == 2, names                      # eye lab (merged, + project team) and bone lab
    assert {r[0] for r in c.execute("SELECT rule FROM merge_log")} >= {"spelling_variant"}
    assert c.execute("SELECT count(*) FROM possible_same WHERE reason LIKE 'spelling variant%'").fetchone()[0] >= 1
    # different given names never merge; Meier/Maier in one unit with the same given name do
    assert "Anna Meier" in names and sum(n in ("Hans Meier", "Hans Maier") for n in names) == 1
