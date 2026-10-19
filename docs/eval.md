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
Run 2026-10-19T08:30:41+00:00, backend `search.query`, k=10.

```
split dev | backend search.query | kg yes | 46 recall queries, 5 skipped (manual/needs), 0 errors
Recall@10            88%   (hit rate 89%, target >= 80%; adjacent excluded)
Synonym parity       Jaccard 0.421, overlap 0.5 over 3 groups
Citation validity    100% of reasons quote their page; 100% of results fully cited; failures none
Field completeness   100%  name 100% unit 100% source_link 100% function 100% email 100% phone 100%
Exposure             82 units shown, gini 0.485, top1 11%, top5 26%, entropy 0.902, 24% of index units ever shown
Checks               abstain:pass=1, entity_resolution:pass=2, recency_active:pass=1
Recall by category   infrastructure 100%, lay 83%, multilingual 100%, person 100%, service 50%, site_term 89%, synonym 67%, typo 100%
```

| source | pages | entities | gold targets | recall@10 | results shown |
|---|---|---|---|---|---|
| artorg | 363 | 253 | 19 | 79% | 53 |
| dcb | 450 | 86 | 7 | 71% | 23 |
| unibe_bcpm | 183 | 150 | 2 | 100% | 10 |
| unibe_dbmr | 199 | 245 | 2 | 100% | 18 |
| unibe_dcbp | 199 | 164 | 2 | 100% | 15 |
| unibe_iap | 131 | 102 | 2 | 100% | 10 |
| unibe_ibmm | 134 | 109 | 2 | 100% | 15 |
| unibe_ifik | 197 | 162 | 2 | 50% | 10 |
| unibe_ispm | 198 | 228 | 2 | 100% | 11 |
| unibe_izb | 199 | 120 | 2 | 100% | 3 |
| unibe_medizin | 198 | 143 | 2 | 100% | 12 |
| unibe_mic | 96 | 50 | 2 | 100% | 10 |
| unibe_neuroscience | 158 | 146 | 2 | 100% | 20 |
| unibe_ngs | 28 | 20 | 2 | 100% | 2 |
| unibe_pki | 190 | 76 | 2 | 100% | 9 |
| unibe_tki | 103 | 54 | 2 | 100% | 8 |
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

Dev progression, real `search.query` (the 15:44 degraded run was BM25 + 70B expansions with no dense or rerank; the 16:05 run added dense bge-m3, the Apertus 70B judge and the 16:00 KG rebuild; no cross encoder):

| metric | 15:44 degraded | 16:05 dense + 70B judge |
|---|---|---|
| Recall@10 (hit rate) | 52% (56%) | 91% (94%) |
| Synonym parity Jaccard / overlap | 0.20 / 0.40 | 0.48 / 0.60 |
| Citation validity | 100% | 100% |
| Field completeness | 78% (email, phone 0%) | 100% |
| Exposure: units shown, Gini, top 5 share, share of KG units shown | 48, 0.55, 38%, 21% | 107, 0.52, 23%, 32% |
| Checks failed | tc26, tc27, tc36 | tc27 (Kokona and Kokana are still two people), tc36 (no project results) |

The 16:05 run returned 16 results with `adjacent: true`. All 16 were dropped before scoring.

### Held out (run once after the freeze)
<!-- results:heldout -->
Run 2026-10-19T08:34:52+00:00, backend `search.query`, k=10.

```
split heldout | backend search.query | kg yes | 23 recall queries, 4 skipped (manual/needs), 0 errors
Recall@10            89%   (hit rate 91%, target >= 80%; adjacent excluded)
Synonym parity       Jaccard n/a, overlap n/a over 0 groups
Citation validity    100% of reasons quote their page; 100% of results fully cited; failures none
Field completeness   100%  name 100% unit 100% source_link 100% function 100% email 100% phone 100%
Exposure             56 units shown, gini 0.389, top1 8%, top5 29%, entropy 0.934, 17% of index units ever shown
Checks               abstain:pass=1, fields:fail=1
Recall by category   infrastructure 100%, lay 91%, multilingual 92%, service 0%, site_term 100%
```

| source | pages | entities | gold targets | recall@10 | results shown |
|---|---|---|---|---|---|
| artorg | 363 | 253 | 9 | 67% | 21 |
| dcb | 450 | 86 | 3 | 67% | 9 |
| unibe_bcpm | 183 | 150 | 1 | 100% | 11 |
| unibe_dbmr | 199 | 245 | 1 | 100% | 12 |
| unibe_dcbp | 199 | 164 | 1 | 100% | 4 |
| unibe_iap | 131 | 102 | 1 | 100% | 2 |
| unibe_ibmm | 134 | 109 | 1 | 100% | 3 |
| unibe_ifik | 197 | 162 | 1 | 100% | 8 |
| unibe_ispm | 198 | 228 | 1 | 100% | 11 |
| unibe_izb | 199 | 120 | 1 | 100% | 3 |
| unibe_medizin | 198 | 143 | 1 | 100% | 7 |
| unibe_mic | 96 | 50 | 1 | 100% | 3 |
| unibe_neuroscience | 158 | 146 | 1 | 100% | 9 |
| unibe_ngs | 28 | 20 | 1 | 100% | 6 |
| unibe_pki | 190 | 76 | 1 | 100% | 2 |
| unibe_tki | 103 | 54 | 1 | 100% | 4 |
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
