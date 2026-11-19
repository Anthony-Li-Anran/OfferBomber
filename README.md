# OfferBomber

Local-first personal job-hunting automation system. Phase 1 (discovery), Phase 2 (experience knowledge base) and Phase 3 (model-driven JD → resume → PDF closed loop) are implemented, tested and operable from the CLI.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Config, SQLite, Greenhouse/Lever/Ashby, dedup, filters, CLI | **Implemented & verified** |
| 2 | PDF import, experience/fact KB, versioning, provenance, FTS5 retrieval, ResumePlan | **Implemented & verified** |
| 3 | ModelProvider, host adapters, model parsing, JD analysis, reranking, smart planning, Reactive Resume adapter, PDF validation | **Implemented; adapters not live-verified (need credentials)** |
| 4-7 | Email, applications, feedback, MCP, scheduler | Not implemented (no fake stubs) |

`offerbomber model doctor` and `offerbomber resume generate` report the exact live status. Mock results are never presented as real model calls or real PDF generation.

`offerbomber status` / `offerbomber doctor` print the authoritative capability list. Config keys that exist for later phases are labelled `[NOT IMPLEMENTED]` in `config.example.yaml` and are never reported as working.

## Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
offerbomber init                   # creates config.yaml AND upgrades the DB to head
```

## Commands

### Phase 1 — discovery

```bash
offerbomber doctor
offerbomber discover                # all configured boards
offerbomber discover --limit 1
offerbomber jobs list --limit 20
offerbomber jobs stats
```

### Phase 2 — profile and experience knowledge base

```bash
# 1. Extract candidates from a PDF (nothing is written as confirmed fact yet)
offerbomber profile import examples/sample_resume.pdf
offerbomber profile import examples/sample_resume.pdf --json

# 2. Confirm selected candidates (or all) into the knowledge base
offerbomber profile confirm 1 --all
offerbomber profile confirm 1 --fields display_name,contact --experiences 0,1

# 3. Inspect and maintain
offerbomber profile show
offerbomber experience list --include-archived
offerbomber experience show 1
offerbomber experience add RESEARCH "Pretraining Research" --organization "AI Lab" \
  --fact "Designed the data mixture" --tag PyTorch
offerbomber experience edit 1 --title "Renamed" --note "after publication"
offerbomber experience history 1
offerbomber experience show-version 1 1
offerbomber experience restore 1 1
offerbomber experience archive 1
offerbomber experience unarchive 1

# 4. Atomic facts and verification
offerbomber fact add 1 "我的论文投了 ICML" --category RESULT --confirm
offerbomber fact verify 7
offerbomber fact delete 7

# 5. Retrieval and resume planning
offerbomber search facts "机器学习 预训练" --skill PyTorch --direction ai_infra
offerbomber search facts "distributed training" --skill PyTorch

# 6. Phase 3 — model-driven JD-to-resume pipeline (uses configured ModelProvider)
offerbomber model doctor                       # shows mock vs real, live-verified status
offerbomber model test                         # structured-output smoke test
offerbomber jobs analyze --job-id 42           # structured JobRequirements with JD evidence
offerbomber search rerank "pytorch" --job-id 42
offerbomber resume plan "pytorch" --job-id 42 --pages 1
offerbomber resume generate --job-id 42 --source-resume-id <your-rxresume-id>
offerbomber resume validate --file output/resumes/resume_42_x.pdf --name "Jane Doe"
offerbomber resume versions --job-id 42
offerbomber resume show --id 1
```

Every command accepts `--config PATH`. See `examples/README.md` for an end-to-end walkthrough using a generated sample PDF and an isolated demo database.

## Design principles

- **Truth source:** the experience database is authoritative. Models may only organize or rewrite confirmed facts; they may not invent experience, grades, companies or metrics.
- **Three-level verification:** `unit test` ≠ `mock end-to-end` ≠ `real host model / real Reactive Resume call`. Only a real credential-backed call sets `real_live_verified`; mock output is always labelled.
- **Explicit IDs:** retrieval results and ResumePlan items carry concrete `Experience ID` and `Fact ID`, plus source type, locator and PDF SHA-256.
- **Immutable history:** each experience change stores an immutable JSON snapshot. Restoring creates a new version; already-generated `resume_versions` snapshots are never modified.
- **Confirmed-only planning:** `ResumePlan` excludes `UNVERIFIED` facts. A statement like "我的论文投了 ICML" stays verbatim and never becomes "ICML Accepted Paper".
- **Bounded parsing:** `DeterministicResumeParser` stays as fallback. `profile import --parser model` uses a ModelProvider, but every candidate still requires human confirmation and quantitative/acceptance claims are checked against source text before entering candidates.
- **Host model access:** a host calling OfferBomber over MCP does not grant OfferBomber the right to call the host model in reverse. Real access uses dedicated adapters (WorkBuddy cloud tasks + ACP, DeepSeek Harness headless CLI) with explicit credentials — never internal account secrets.

## Model providers (verified interfaces, 2026-11-19)

| Provider | Interface | Live status |
|---|---|---|
| `mock` | deterministic offline | test/demo only, never real |
| `workbuddy` | `POST/GET /openapi/v2/tasks` + ACP SSE/JSON-RPC (`open.workbuddy.cn/docs/openapi`); no OpenAI-compatible endpoint | adapter implemented, **not live-verified** (needs `OFFERBOMBER_WORKBUDDY_TOKEN`) |
| `deepseek_harness` | `dsh --profile headless [--json] [--session-id]` (`deepseek-ai/deepseek-harness` official CLI help) | adapter implemented, **not live-verified** (needs `dsh` on PATH) |
| `openai_compatible` | generic `/chat/completions` for user-supplied endpoint | optional, off by default |

Task types: `RESUME_EXTRACTION`, `JOB_ANALYSIS`, `EXPERIENCE_RERANKING`, `RESUME_PLANNING`, `RESUME_REWRITING`, `FACT_CONSISTENCY_CHECK`. All calls are async, Pydantic-validated, timed out, cancellable, classified, bounded-retry and logged; failures fall back safely.

## Reactive Resume (verified interface, 2026-11-19)

Adapter uses the official `/api/openapi` REST API with `x-api-key` auth: list/get resumes, `POST /resumes/{id}/copies` (clone; the primary resume is **never** edited), update content while preserving metadata/design/template, `GET /resumes/{id}/pdf`, PyMuPDF validation. No key → explicit failure, no fake PDF. Rate limits (1000/h per key, 5 PDF/min per resume) and errors (`code` field, 429) are handled. **Not live-verified** without `OFFERBOMBER_RXRESUME_API_KEY`.

## Data sources (verified 2026-11-19)

| Source | Endpoint | Auth | Verified result |
|---|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | none | 781 jobs (token `stripe`) |
| Lever | `GET api.lever.co/v0/postings/{token}?mode=json` | none | 10 jobs (token `leverdemo`) |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` | none | 81 jobs (token `ashby`) |

Field names come from official docs. Lever's public list has no `createdAt`; Ashby has no job `id` — neither is fabricated.

## Dedup and resilience

1. Same platform + external ID; 2. same canonical URL; 3. same company/title/location with description similarity ≥ 0.88.
`normalize_key` is Unicode-safe: NFKC, casefold, punctuation→separator, CJK characters preserved, whitespace inside CJK runs collapsed. Distinct Chinese company or job names are not merged.

A malformed response, a bad record or a failed DB write in one source is isolated (SAVEPOINT per record, commit per source) and logged; `sources_partial`, `record_errors` and `fully_succeeded` prevent a partial run from being reported as a full success.

## Retrieval

Structured skill tags + SQLite FTS5 BM25 + deterministic keyword scoring. Because SQLite's default tokenizer does not segment Chinese, CJK runs and their bigrams are indexed in a separate column, with a LIKE-style token fallback. A `Reranker` protocol is reserved for future semantic models; no vector database is used yet.

## Database

Alembic owns the schema:

```bash
alembic upgrade head
alembic current
```

The full 25-table task-book schema plus `resume_imports` and the FTS5 virtual table. Phase 1 writes discovery tables; Phase 2 writes profile/education/experience/fact/source/version/import tables. Default DB: `.offerbomber/offerbomber.db` (gitignored).

## Tests

```bash
PYTHONPATH=src pytest tests/ -q        # 76 passed
```

Coverage includes Chinese dedup, malformed source isolation, single-record isolation, PDF extraction, duplicate PDF import, full experience creation, fact addition, version history, archive/restore, Chinese and English retrieval, requirement ranking, unconfirmed-fact exclusion, snapshot immutability, restart persistence, a fresh Alembic upgrade, FTS/revision consistency and atomic rollback on index failure (Phase 3 Part A), all model failure classes and safety guards, model parsing truthfulness, JD evidence separation, reranking ID validation, smart-planning budget/provenance, Reactive Resume mapping/validation/mock-HTTP flow, and four end-to-end scenarios (ML job, quant job, submission-not-accepted, service failure).

## Known limitations

- Scanned-PDF OCR is not available; the model parser explicitly reports unusable text instead of pretending success. Complex two-column reading order needs manual review.
- `official_careers`, `search_engine`, `xiaohongshu_public` and `auto_expand_companies` are configuration placeholders only.
- Model host adapters are implemented against documented interfaces but **not live-verified** in this environment (no credentials). Without credentials, `auto` uses Mock and says so.
- Reactive Resume generation requires a real API key and a source resume id; automated PDF checks cannot certify visual layout — human preview is required.
- Email, applications, reports, MCP server and scheduling are not implemented in this release.
- Recruitment email extraction is not implemented; `apply_method` stays `official_form`/`unknown`.

## License

Apache License 2.0 (`LICENSE`), consistent with `pyproject.toml`.
