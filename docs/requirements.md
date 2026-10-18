# Requirements and differences from the playbook

Sources read: `research/hackapertus-notion-site-bern-economic-development-agency.md` (challenge guide, wins on conflict), `research/app-sprintd-org-events-hack-apertus-liebefeld-cases-research.md` (case page), the playbook, the Canton of Bern manual.

## What the guide and case page require

- End to end prototype: public web sources → content extraction → semantic enrichment → structured knowledge → natural language and keyword search → source backed results.
- Each result: person / contact, function and affiliation, unit (department, institute, group), expertise, projects or activities, infrastructure or services, a short reason for the match, direct links to source pages.
- Success criteria: (1) ≥80% of expected experts/units in top 10; (2) synonym and lay term parity ("glucose sensors" vs "CGM", "biosensing"); (3) 100% of results link to a supporting public page, nothing invented; (4) required fields filled where the source has them; (5) covers UniBE, ARTORG and Diabetes Center Berne; (6) a new site added by configuration or one documented step.
- Judging criteria: purposeful use of AI, technical rigour, value / cost / scalability, sovereign deployability, implementation feasibility.
- Ethics: traceability, fair discoverability regardless of group size, say clearly when no reliable source exists. nDSG / GDPR, public web content only, source copyright stays with the institutions, outputs are Open Commons (T&C §6).
- Models: Apertus v1.5 8B or 70B (`swiss-ai/Apertus-v1.5-8B`, `swiss-ai/Apertus-v1.5-70B`).

## Differences between the guide and the playbook

| Topic | Playbook | Guide / case page | Decision |
|---|---|---|---|
| Deadline | "Submission: tonight", plan assumes ~22:00 | **Not stated** in either file | Keep the playbook schedule; **human must confirm the exact deadline** |
| Submission format | Repo, 2 min video, slides | **Not stated** | Prepare repo, video and deck; human confirms the format before submitting |
| Gateway | Keera at `gateway.keera.ch/api/v1`, CSCS | **Not mentioned**; guide lists the Hugging Face model pages as sources | Use Keera (smoke test passed, see docs/gateway.md); keep the vLLM "sovereign" profile as proof the stack can run without it |
| Model names | `apertus-v1-5-8b`, `apertus-v1-5-70b`, `apertus-v1-5-70b-thinking` | "Apertus 8B or 70B (v1.5)" | Same models. Gateway ids confirmed: `apertus-v1-5-8b`, `apertus-v1-5-70b`, plus `-thinking` variants of both. Thinking variants reject forced tool_choice |
| Hosting | Docker locally, Swiss host | Must be **entirely outside cantonal ICT**, self contained, able to run **fully air gapped** or on independent cloud | Docker compose as planned; the air gapped story (local vLLM, local embeddings, cached crawl) gets its own slide |
| Logo and template | Keep logo out of the public repo | "Use them ONLY for the need of this challenge" | Logo stays in gitignored `assets/private/`; template used only for the pitch deck |
| unibe inventory size | 42 sites, Notes sheet says 71 | Not stated | Confirmed: the sheet has **42 rows, the Notes sheet says 71**. Flagged to the human; we crawl 14 of the 42 (`inventory/unibe_seeds.txt`), full list in `inventory/unibe_all.tsv` |
| HF credit, Jev, finals date | Described | Not mentioned | Unchanged |

Nothing in the guide changes the plan in a big way. Open questions for the organizers stay as in the playbook's "Ask the organizers now" section.

## Repo hygiene note

`research/supporting materials/` (logo, pptx template, xlsx) was committed in d3a4eba **and that commit is already on the public GitHub repo**. It is now untracked and gitignored; removing it from history needs a force push, which the human must approve.
