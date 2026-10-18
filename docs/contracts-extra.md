# Extra contracts (orchestrator, on top of run sheet §3)

Agents build against run sheet §3 plus these. Extra fields on records are fine; never remove contract fields.

## data/pages.jsonl extra fields (agent A)
`page_type`: person | group | unit | project | infrastructure | publications | news | other; `breadcrumbs`: [str]; `sibling_group`: id shared by DE/EN/FR versions of one page.

## kg.sqlite schema (agent B)
Documented in `ingest/SCHEMA.md` as soon as it exists. Read only for everyone except B.

## search/query.py (agent C), besides query()
`why_not(entity_or_name: str, query_text: str) -> {"entity_id", "name", "in_index": bool, "rank": int|None,
   "stages": [{"stage", "score", "rank", "note"}], "reason": str, "searched_sources": [...]}`
RankLog lives in `data/ranklog.sqlite`.

## needs/needs.py (agent E), returned dict also has
`"passages": [{"text", "status": "used|ignored|struck", "reason", "source": "text|pdf|website"}]`
and `needs.needs.route(need) -> {"kind": "search"|"curated"|"not_covered", "entries": [...curated entries...]}`.

## enrich/curated.json (agent H), a JSON list of
`{"id", "name", "kind": "transfer|clinical_trials|funding|infrastructure|talent|space|other", "need_types": ["funding", ...],
  "description", "url", "source_url", "region", "checked": bool}`

## enrich/snsf.py (agent H)
`grants_for(entity_id: str) -> [{"title", "number", "start", "end", "amount_chf", "role", "url"}]`, [] when unknown.
Reads `data/enrich/grants.jsonl`.

## pitch (agent G builds the PDF, agent F builds the builder screen)
Pitch dict:
`{"case_id", "company": {"name", "country", "summary"}, "title", "intro", "officer": {"name", "email"},
  "cards": [{"entity_id", "type", "name", "unit", "show_person_name": bool, "text",
             "sources": [{"url", "quote", "deep_link"}]}], "created_at"}`
`pitch.build.validate(pitch) -> [problems]` (empty list = ok);
`pitch.build.build_pdf(pitch, mode="review"|"clean", out_path) -> out_path`, raises `pitch.build.ExportError(problems)` on any problem.

## eval results (agent D, shown by F's Evaluation screen)
`data/eval/latest.json`: `{"run_at", "split", "n", "metrics": {"recall_at_10", "synonym_parity", "citation_validity",
  "field_completeness", "per_source_coverage": {}, "exposure": {}}, "per_query": [...]}`; docs/eval.md for humans.
