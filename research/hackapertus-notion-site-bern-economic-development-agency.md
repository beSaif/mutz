[Skip to content](https://hackapertus.notion.site/bern-economic-development-agency#main)

![](https://hackapertus.notion.site/image/attachment%3A1c80d543-3234-4ab7-8d87-98eddea1ec8a%3ANotion-Cover_1500x250_(6).png?table=block&id=fe5b4fec-112a-833d-af94-01a4771a7c12&spaceId=62fb4fec-112a-81c7-851e-0003f9181b0b&width=2000&userId=&cache=v2&imgBuildSrc=requestProxiedImageUrl)

# Bern Economic Development Agency

## Research & Expertise Navigator

### ![🎯](<Base64-Image-Removed>) Challenge Overview

Domain: Research & Innovation

#### → Context & Background

Today, finding expertise often requires knowing where to search before knowing what to search for. Information may be located on a faculty website, an institute page, a research group profile, a centre's website, an individual researcher page or a project description. On top of that, the terminology used by the person searching may differ significantly from the terminology used on the source website. Someone looking for expertise in "glucose sensors", for example, may miss relevant information described as continuous glucose monitoring, biosensing, metabolic monitoring, wearable sensing or CGM. The challenge is therefore to transform this fragmented web landscape into a unified and semantically searchable knowledge resource.

#### → Problem Description

Research institutions generate a considerable amount of valuable information about their people, expertise, research activities, technologies, infrastructures and services. However, this information is distributed across a large and heterogeneous landscape of institutional websites and platforms.

#### → Primary Objective

Create an AI-powered Research & Expertise Navigator that aggregates these distributed public sources and makes their content accessible through a single natural-language and keyword-based search interface.

### ![🔧](<Base64-Image-Removed>) Resources, Tools & Support

#### → Datasets

For the prototype, the system would use three complementary sets of public data sources, treated as one distributed information ecosystem rather than separate databases:

University of Bern web ecosystem — based on the provided inventory of public unibe.ch subdomains (faculties, institutes, departments, research centres, laboratories, services and platforms).

ARTORG Center for Biomedical Engineering Research — website and its associated research, team and project pages.

Diabetes Center Berne — website and its associated research, expertise, team, project and service pages.

The architecture should also be designed so that additional institutional websites and data sources can be added easily later.

#### → Supporting Materials

A supplied inventory of public University of Bern (unibe.ch) subdomains, serving as the initial discovery map for the UniBE ecosystem.

unibe\_sous\_domaines\_publics\_etendus.xlsx

12.6 KiB

The powerpoint template and logo (please use them ONLY for the need of this challenge):

powerpoint\_template.pptx

546.8 KiB

Logo\_SF\_BE\_EN.png

15 KiB

### ![💻](<Base64-Image-Removed>) Technical Preferences

|  |  |
| --- | --- |
| Preferred languages / frameworks | Not specified — no restriction implied |
| Recommended platforms | External environments only — development and deployment must happen entirely outside the Canton of Bern's ICT infrastructure. Use local setups (e.g. Docker) or independent / Swiss Cloud infrastructure. Solutions must be self-contained and run fully air-gapped or on independent cloud instances, with no connection to internal cantonal networks. |
| Specific AI models | Apertus 8B or 70B (v1.5) |
| Model sources | huggingface.co/swiss-ai/Apertus-v1.5-8B<br>huggingface.co/swiss-ai/Apertus-v1.5-70B |

### ![📦](<Base64-Image-Removed>) Deliverables & Judging

#### → Expected outputs

An end-to-end prototype: public web sources → content extraction → semantic enrichment → structured knowledge → natural-language search → source-backed results.

Rather than only a textual answer, each query should return structured, traceable results:

relevant people and contact persons;

their functions and affiliations;

departments / institutes / research groups;

fields of expertise;

related projects or activities;

relevant infrastructure or services;

a short explanation of why each result matches;

direct links to the original source pages.

Every result stays connected to its source and can be verified.

#### → Success criteria

Finds the right people: at least 80% of expected experts/units appear in the top 10 results.

Handles different wording: synonym and lay-term queries (e.g. "glucose sensors" vs. "CGM", "biosensing") find the same experts as the site's own terminology.

Every result is traceable: 100% of results link to a public source page that supports the claim; no invented people, roles or projects.

Structured results: each result fills the required fields (person, role/affiliation, unit, expertise, projects, infrastructure/services, reason for match, source link) where the source provides them.

Covers all three sources: UniBE, ARTORG and Diabetes Center Berne.

Easy to extend: a new institutional website can be added via configuration or a short documented step, without rewriting the pipeline.

#### → Evaluation criteria

Purposeful use of AI

Technical rigour

Value, cost & scalability

Sovereign deployability

Implementation feasibility

### ![⚖️](<Base64-Image-Removed>) Ethics & Compliance

#### → Ethical guidelines

Transparency & traceability: AI outputs must avoid unverified or hallucinated information. Every result, summary and explanation must be fully traceable and back-linked to the verifiable public source URL (e.g. UniBE, ARTORG).

Fair & unbiased representation: the search must ensure fair, objective discoverability across all research groups, institutes and individual researchers, regardless of institutional size or prominence.

Accuracy & reliability: the system must clearly say when no reliable, verifiable or sufficient source information is available to answer a query.

#### → Legal & compliance

Comply with the Swiss Federal Act on Data Protection (nDSG) and GDPR where applicable. Only public web content is used; the copyright of source pages stays with the original institutions. Outputs are published as Open Commons(see Terms & Conditions, §6).