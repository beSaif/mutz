# Evaluation

**What Recall@10 does and does not measure (gap 10):** it tells us whether the groups our own team picked
from the same pages we crawled come back in the top 10. It does not tell us whether a company would find
them useful. Only the officer's accept and reject clicks, and company feedback after a pitch, can show that.

## How to run

```
.venv/bin/python -m eval.run_eval                     # dev split: the one we tune on
.venv/bin/python -m eval.run_eval --split heldout     # once, after the 19:00 freeze; every run is logged
.venv/bin/python -m eval.synthetic                    # regenerate synthetic queries (25 cached gateway calls)
.venv/bin/python -m eval.run_eval --split synthetic
.venv/bin/python -m pytest eval/tests -q              # tests for the harness itself
```

`run_eval` calls `search.query.query(text, k=10)` (run sheet §3). If that does not import, it falls back to
`eval/stub_query.py`, a plain BM25 stand-in that follows the same contract, and it says so on every line it
prints and in `backend` in the JSON. Output: `data/eval/latest.json` (the format in docs/contracts-extra.md,
which the Evaluation screen reads), `data/eval/latest_<split>.json`, and the numbers blocks below.

## Gold set (`eval/gold.json`)

| part | cases | where from |
|---|---|---|
| playbook cases | 39 | the "Test cases" table, split as given there |
| unibe slice | 42 | 3 per seed site in `inventory/unibe_seeds.txt` (14 sites): one site term, one lay or service query, one in DE or FR. Expected units checked against the crawled pages in `data/`, not the live web |
| synonym probes | 3 | "glucose sensors", "CGM", "biosensing", the pair the case page names |
| **total** | **84** | **55 dev, 29 held out** |

- **Held out is locked.** `eval/heldout.lock` holds the sha256 of the held out cases and synonym groups.
  `run_eval --split heldout` refuses to run if the set changed, and each held out run is added to
  `data/eval/heldout_runs.jsonl`, so we can show how many times we looked. We do not tune on it.
- **IDs, not contact data (gap 17).** An expected answer is a stable key: a crawl source id plus a URL path
  on that source (for example `artorg` + `/research/hrl/`). Sometimes the key also holds a unit name, or a
  person's name when that is how the case itself states the answer. At test time the key resolves to
  `data/kg.sqlite` entity ids (people, units, projects and infrastructure with evidence on those pages, plus
  members of the units found there). If the cited page is under the key, that also counts, and the per
  query output records which way each hit was made. No emails or phones are stored; a test checks this.
- ARTORG's affiliated groups (Witthauer, Koch, Bastiaansen, Brüningk, Knopf, Haeberlin) appear only as
  entries on `/research/index_eng.html`, which links out to Insel pages that are not crawled. Their keys
  are that page plus the group name.

### What each check does

| check | cases | pass when |
|---|---|---|
| recall | site term, lay, multilingual, typo, service, infrastructure, person, synonym, unibe, synthetic | share of expected targets in the top 10 results that are not tagged adjacent |
| fields | tc25 | the head of MSL is found and email and phone are filled (in the result, or in the KG when the result does not carry them) |
| entity_resolution | tc26, tc27 | exactly one person entity matches the name (Lisa Koch: with both ARTORG and DCB sources) |
| abstain | tc28, tc29 | no result has coverage `strong`, and `searched_sources` is listed |
| recency_active | tc36 | every project result is labelled `active` |
| manual | tc30, tc31, tc37, tc38 | judged by hand at the freeze (adversarial, novelty, past project) |
| needs | tc32 to tc35, tc39 | agent E's needs tests (PDF, injection, website, re-evaluate, one pager) |

**Adjacent results (gap 11).** Any result with `adjacent: true`, or with `adjacent` in `tag`, `kind`, `lane`,
`match_type` or `tags`, is dropped before scoring. It does not count for recall or against it, and it does
not take a rank. We judge adjacent suggestions by hand on 5 cases (table at the end).

## Metrics

- **Recall@10**: mean over recall cases of (targets found in the top 10 / targets). Hit rate means at least one target was found. Also reported by category and by source.
- **Synonym parity**: mean pairwise Jaccard of the top 10 entity id sets for queries that should find the same experts (site term vs lay vs other language; the CGM probes). Overlap = intersection / size of the shorter list.
- **Citation validity**: share of the reasons on shown results whose quote appears word for word (ignoring case, whitespace and markdown; `…` splits the quote into ordered parts) in the cited chunk, or else in the stored text of the cited page. "Results fully cited" is the share of results where every reason passes and there is at least one.
- **Field completeness**: filled / available for name, unit, function (people), source link, and email and phone (people, when the KG has them). "Available" comes from the KG, so a field the source page does not have is not counted against us.
- **Per source coverage**: pages, KG entities, gold targets, recall and results shown, per source.
- **Exposure (fairness)**: how attention spreads across units, with position weight 1/log2(rank+1): units shown, Gini, top 1 and top 5 share, normalised entropy, and the share of the KG's units that are ever shown.

## Synthetic queries (`eval/fixtures/synthetic.json`)

`eval/synthetic.py` samples 25 KG units, round robin over the 16 sources, alternating small units (3 pages or
fewer) and large ones. For each unit, Apertus 70B writes two lay company needs in two languages, without the
unit name, people's names or technical terms. One call per unit, at most 4 in flight, cached. A query is
dropped when more than 30% of its words are rare words copied from the unit's page. The language label is the
one detected, not the one requested. `synthetic_unit` targets one group. `synthetic_site` means the KG had only
a whole-site unit for that source, which is an easier target, so it is reported separately. The two variants
for one unit form a synonym parity pair.

**Deviation from the playbook:** the playbook wants these queries written by a model from a different family
(HF), so the system is not graded on text written by its own model. The run sheet assigned Apertus 70B, so
these queries share a model family with the query expansion step. Treat the synthetic number as optimistic
for that reason.

## Numbers

### Dev (tuning set)
<!-- results:dev -->
Run 2026-10-18T13:44:45+00:00, backend `search.query`, k=10. **degraded: BM25 + expansions, no dense/rerank**.

```
split dev | backend search.query (degraded: BM25 + expansions, no dense/rerank) | kg yes | 46 recall queries, 5 skipped (manual/needs), 0 errors
Recall@10            52%   (hit rate 56%, target >= 80%; adjacent excluded)
Synonym parity       Jaccard 0.2, overlap 0.4 over 3 groups
Citation validity    100% of reasons quote their page; 100% of results fully cited; failures none
Field completeness   78%  name 100% unit 100% source_link 100% function 100% email 0% phone 0%
Exposure             48 units shown, gini 0.553, top1 12%, top5 38%, entropy 0.865, 21% of index units ever shown
Checks               abstain:pass=1, entity_resolution:fail=2, recency_active:fail=1
Recall by category   infrastructure 75%, lay 42%, multilingual 36%, person 100%, service 100%, site_term 61%, synonym 33%, typo 50%
```

| source | pages | entities | gold targets | recall@10 | results shown |
|---|---|---|---|---|---|
| artorg | 363 | 253 | 19 | 47% | 66 |
| dcb | 450 | 70 | 7 | 71% | 9 |
| unibe_bcpm | 183 | 139 | 2 | 100% | 33 |
| unibe_dbmr | 199 | 279 | 2 | 100% | 22 |
| unibe_dcbp | 199 | 166 | 2 | 0% | 8 |
| unibe_iap | 131 | 105 | 2 | 0% | 16 |
| unibe_ibmm | 134 | 102 | 2 | 100% | 11 |
| unibe_ifik | 197 | 136 | 2 | 0% | 15 |
| unibe_ispm | 198 | 217 | 2 | 100% | 11 |
| unibe_izb | 199 | 113 | 2 | 0% | 6 |
| unibe_medizin | 198 | 184 | 2 | 50% | 17 |
| unibe_mic | 96 | 61 | 2 | 0% | 4 |
| unibe_neuroscience | 158 | 189 | 2 | 50% | 19 |
| unibe_ngs | 28 | 18 | 2 | 50% | 2 |
| unibe_pki | 190 | 80 | 2 | 50% | 4 |
| unibe_tki | 103 | 78 | 2 | 50% | 5 |
<!-- /results:dev -->

Stub backend (keyword baseline, harness floor only):
<!-- results:dev:stub -->
Run 2026-10-18T13:20:34+00:00, backend `stub`, k=10.

```
split dev | backend stub | kg yes | 46 recall queries, 5 skipped (manual/needs), 0 errors
Recall@10            54%   (hit rate 59%, target >= 80%; adjacent excluded)
Synonym parity       Jaccard 0.067, overlap 0.1 over 3 groups
Citation validity    100% of reasons quote their page; 100% of results fully cited; failures none
Field completeness   100%  name 100% unit 100% source_link 100%
Exposure             304 units shown, gini 0.377, top1 3%, top5 9%, entropy 0.953, 0% of index units ever shown
Checks               abstain:pass=1, entity_resolution:fail=2, recency_active:fail=1
Recall by category   infrastructure 100%, lay 50%, multilingual 18%, person 0%, service 50%, site_term 86%, synonym 50%, typo 0%
```

| source | pages | entities | gold targets | recall@10 | results shown |
|---|---|---|---|---|---|
| artorg | 363 | 323 | 19 | 53% | 33 |
| dcb | 450 | 97 | 7 | 57% | 134 |
| unibe_bcpm | 183 | 105 | 2 | 50% | 19 |
| unibe_dbmr | 199 | 198 | 2 | 50% | 42 |
| unibe_dcbp | 199 | 145 | 2 | 0% | 8 |
| unibe_iap | 131 | 87 | 2 | 50% | 20 |
| unibe_ibmm | 134 | 96 | 2 | 50% | 4 |
| unibe_ifik | 197 | 121 | 2 | 100% | 34 |
| unibe_ispm | 198 | 187 | 2 | 50% | 11 |
| unibe_izb | 199 | 95 | 2 | 50% | 13 |
| unibe_medizin | 198 | 109 | 2 | 50% | 51 |
| unibe_mic | 96 | 45 | 2 | 50% | 15 |
| unibe_neuroscience | 158 | 99 | 2 | 50% | 32 |
| unibe_ngs | 28 | 19 | 2 | 50% | 1 |
| unibe_pki | 190 | 65 | 2 | 50% | 16 |
| unibe_tki | 103 | 48 | 2 | 50% | 14 |
<!-- /results:dev:stub -->

### Held out (run once after the freeze)
<!-- results:heldout -->
not run yet
<!-- /results:heldout -->

Stub backend (keyword baseline, harness floor only):
<!-- results:heldout:stub -->
not run yet
<!-- /results:heldout:stub -->

### Synthetic
<!-- results:synthetic -->
not run yet
<!-- /results:synthetic -->

Stub backend (keyword baseline, harness floor only):
<!-- results:synthetic:stub -->
Run 2026-10-18T13:20:29+00:00, backend `stub`, k=10.

```
split synthetic | backend stub | kg yes | 47 recall queries, 0 skipped (manual/needs), 0 errors
Recall@10            43%   (hit rate 43%, target >= 80%; adjacent excluded)
Synonym parity       Jaccard 0.009, overlap 0.017 over 23 groups
Citation validity    100% of reasons quote their page; 100% of results fully cited; failures none
Field completeness   100%  name 100% unit 100% source_link 100%
Exposure             231 units shown, gini 0.518, top1 6%, top5 21%, entropy 0.892, 0% of index units ever shown
Checks               none
Recall by category   synthetic_site 61%, synthetic_unit 31%
```

| source | pages | entities | gold targets | recall@10 | results shown |
|---|---|---|---|---|---|
| artorg | 363 | 323 | 6 | 17% | 12 |
| dcb | 450 | 97 | 2 | 100% | 147 |
| unibe_bcpm | 183 | 105 | 2 | 50% | 13 |
| unibe_dbmr | 199 | 198 | 6 | 50% | 42 |
| unibe_dcbp | 199 | 145 | 6 | 17% | 29 |
| unibe_iap | 131 | 87 | 2 | 50% | 4 |
| unibe_ibmm | 134 | 96 | 2 | 0% | 1 |
| unibe_ifik | 197 | 121 | 2 | 100% | 103 |
| unibe_ispm | 198 | 187 | 2 | 100% | 11 |
| unibe_izb | 199 | 95 | 2 | 50% | 13 |
| unibe_medizin | 198 | 109 | 0 | n/a | 31 |
| unibe_mic | 96 | 45 | 2 | 50% | 1 |
| unibe_neuroscience | 158 | 99 | 3 | 33% | 22 |
| unibe_ngs | 28 | 19 | 2 | 50% | 1 |
| unibe_pki | 190 | 65 | 4 | 50% | 35 |
| unibe_tki | 103 | 48 | 4 | 25% | 5 |
<!-- /results:synthetic:stub -->

## Known limits

- The gold set was written by the team from the crawled pages (see the top line). Every gold key was
  checked against `data/`, and `eval/tests` fails if a key points at a page we did not crawl.
- Field completeness can only judge fields the KG knows about. A fact the extractor missed counts as "not
  available", not as "missed". Spot check the person cards by hand.
- The stub backend has no expansion, embeddings or reranker. Its numbers are a floor for the harness, not a
  result for the product.

## Adjacent suggestions, judged by hand (fill in at the freeze)

| case | adjacent result | useful? (y/n) | why |
|---|---|---|---|
| | | | |
