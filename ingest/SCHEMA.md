# data/kg.sqlite schema (owner: agent B, read only for everyone else)

Built by `python -m ingest.resolve` (which runs after `ingest.extract`). Written atomically: built as
`data/kg.sqlite.tmp`, then `os.replace` onto `data/kg.sqlite`, so readers never see a half DB.
Open it read only: `sqlite3.connect("file:data/kg.sqlite?mode=ro", uri=True)`.
All list fields are JSON arrays stored as TEXT. Timestamps are ISO 8601 UTC.

**Rule: no row in `evidence`, no fact in the UI.** Every person, unit, affiliation, expertise link,
project and infrastructure row has at least one `evidence` row with `verified=1`.

## ID formats (stable, deterministic from content, never reused)

| entity | format | key hashed (sha1, first 6 hex) |
|---|---|---|
| person | `person:<slug(name)>-<h6>` | lower(email) if known, else `norm(name)|unit_id` |
| unit | `unit:<source_group>:<slug>` | none, slug of unit name or URL group segment (e.g. `unit:artorg:aihn`, `unit:dcb:precisionlab`, `unit:unibe_dbmr:root`) |
| expertise | `expertise:<slug(label)>` | none |
| project | `project:<source_group>:<slug(title)[:40]>-<h6>` | `norm(title)` |
| infrastructure | `infra:<source_group>:<slug(name)[:40]>-<h6>` (LLM) or `infra:<source_group>:<h6>` (service pages) | `norm(name)`, or the page's `sibling_group` so DE/EN/FR are one entity |

`slug` = NFKD, ascii, lower, non alnum -> `-`. `source_group` is `artorg`, `dcb`, or the unibe site id (`unibe_dbmr`, ...).
When two persons merge, the kept id is the email based one, else the lexicographically smaller id;
the other id is recorded in `merge_log` and never handed out again (use `resolve_id()` below).

## Tables

```sql
person(id TEXT PRIMARY KEY, name TEXT, name_norm TEXT, titles TEXT /*json*/, emails TEXT /*json*/,
       phones TEXT /*json*/, orcid TEXT, profile_url TEXT, sources TEXT /*json source ids*/,
       seniority REAL /*max over affiliations*/, primary_unit_id TEXT, primary_function TEXT)
unit(id TEXT PRIMARY KEY, name TEXT, type TEXT /*faculty|institute|centre|group|lab|platform|team*/,
     parent_id TEXT, source TEXT, url TEXT)
affiliation(person_id TEXT, unit_id TEXT, function TEXT /*as written, e.g. "Group Head"*/,
            function_class TEXT /*head|professor|senior|postdoc|phd|student|staff|other|collaborator|emeritus*/,
            seniority REAL, source TEXT, url TEXT, page_id TEXT,
            PRIMARY KEY(person_id, unit_id, function))
expertise(id TEXT PRIMARY KEY, label TEXT, mesh_id TEXT, synonyms TEXT /*json*/)
entity_expertise(entity_id TEXT, expertise_id TEXT, PRIMARY KEY(entity_id, expertise_id))  -- holder is a person or unit id
project(id TEXT PRIMARY KEY, title TEXT, summary TEXT, unit_id TEXT, start TEXT, "end" TEXT,
        status TEXT /*recruiting|ongoing|completed|null, only if the page says so*/, url TEXT, source TEXT)
infrastructure(id TEXT PRIMARY KEY, name TEXT, kind TEXT /*facility|service|platform*/, unit_id TEXT,
               description TEXT, url TEXT, source TEXT)
evidence(id INTEGER PRIMARY KEY, entity_id TEXT, field TEXT /*name|function|email|phone|unit|expertise|
         title|summary|member_of|...*/, value TEXT, quote TEXT, url TEXT, page_id TEXT, verified INTEGER,
         fetched_at TEXT, lang TEXT, method TEXT /*rules|llm*/, source TEXT)
mention(name TEXT, page_id TEXT, url TEXT, quote TEXT, context TEXT /*publications|news|external|coauthor*/)
       -- people named on a page but NOT members (gap 3). Never shown as Bern experts.
page_entity(page_id TEXT, entity_id TEXT, role TEXT /*subject|member|unit_page*/, PRIMARY KEY(page_id, entity_id, role))
       -- which entities a page is about: lets search aggregate chunks (chunk.page_id) to people and units
merge_log(kept_id TEXT, merged_id TEXT, rule TEXT /*email|unit+name|sibling_group*/, at TEXT)
possible_same(a_id TEXT, b_id TEXT, reason TEXT, score REAL)   -- for human review, never auto merged
meta(key TEXT PRIMARY KEY, value TEXT)                         -- built_at, counts, dropped facts
```

Only `evidence.verified=1` rows are written today (dropped facts are only counted, in
`logs/extract_stats.json` and `meta.dropped_facts`); the column exists so a review mode can keep them.

## Seniority weights (gap 13), on `affiliation.seniority`; `person.seniority` = max

| function_class | matches (DE/EN/FR, case insensitive) | weight |
|---|---|---|
| head | group head, head of, leiter/in, director, direktor/in, CEO, chair, PI, principal investigator, responsable | 1.0 |
| professor | professor, prof., Professorin, assistant/associate/tenure track professor | 1.0 |
| senior | senior scientist/researcher/lecturer, Privatdozent, PD, lecturer, Dozent, group leader, research scientist, Oberassistent, clinical research scientist, project leader | 0.8 |
| postdoc | postdoc, postdoctoral, research associate, wissenschaftliche/r Mitarbeiter/in | 0.6 |
| staff | engineer, manager, coordinator, statistician, data scientist, technician, software developer, administration | 0.5 |
| phd | PhD student/candidate, doctoral student, Doktorand/in, doctorant | 0.4 |
| student | master/bachelor student, intern, Praktikant, student | 0.2 |
| other | anything else, or no function on the page | 0.5 |
| collaborator | listed under a partner/collaborator/external section of the unit's team page (even with a profile page there), or named only in a project team line: **not a member** | 0.5 |
| emeritus | "Prof. em.", "emeritus" in name or function | 0.5 |

The first matching class from the top wins; collaborator and emeritus override the function text. Students still show under their group; ranking multiplies by the weight.

## Unit names (fixed 15:20)

Unit ids come from the URL (`/research/<group>/` on ARTORG, `/research_programs/<lab>/` and similar on unibe,
the `team-<slug>` class of the DCB grid). Names, best first:
1. the page breadcrumb at the position of the unit's URL directory (majority vote over all pages of the unit),
2. the "University of Bern, <centre>, <group>" line of a person page, or the DCB grid group label,
3. the h1 of the unit's own index page.
Never a sub page title ("Teaching", "Publications", "Job Vacancies" were a bug in the first build).

## Infrastructure and projects (fixed 15:45 / 16:00)

Infrastructure = a facility, platform or service people outside the group can use. Sources: (a) rules: every
unibe page under `/services/`, `/dienstleistungen/`, `/core_facilities/`... is one service (name = breadcrumb,
description = first sentence, English name/URL preferred, unit = site root); (b) the 8B, filtered by
`verify.is_infrastructure` (facility/platform/service word in the name, or "available to/open to" in the quote;
devices, sensors, apps, materials, methods are rejected). Rejected study devices on a project page become
`evidence(field='device')` on that project. Project `status`/`start`/`end` are set only when a page sentence
states them (evidence rows with fields status/start/end); dates must contain a year.
unibe research groups: `/research/<group>/` and `/forschung/<group>/` give one unit `unit:<site>:<slug(group)>`.
DCB project pages: "Project Team: A, B, C" lines yield persons (merged into the same name team member if any).

## Merge rules (resolve.py), all logged in merge_log.rule

`email` (same personal email, similar names; generic inboxes never merge), `unit+name` (same unit, token sort
ratio >= 95), `profile_url` (listing links to the profile page), `name+email_local` (exact full name and same
email local part on two domains), `exact_name_cross_source` (exact full name on two different sources, no
conflicting emails: e.g. one person on ARTORG and DCB), `project_team+name` (a project team name joins the one
same-source person with that exact name). Same full name in two units of one source with
different emails stays two people plus a `possible_same` row. Former members and external partners from
team listings, and people on publications/news pages, go to `mention`, never `person`.

## Helpers (ingest/resolve.py)

`resolve_id(conn, entity_id) -> str` follows `merge_log` to the kept id.
`SENIORITY`, `function_class(text) -> (cls, weight)` importable for other agents.
