# OfferBomber

Local-first personal job-hunting automation system. Phase 1 (job discovery) and Phase 2 (personal experience knowledge base) are implemented, tested and operable from the CLI.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Config, SQLite, Greenhouse/Lever/Ashby, dedup, filters, CLI | **Implemented & verified** |
| 2 | PDF import, experience/fact KB, versioning, provenance, fact retrieval, ResumePlan | **Implemented & verified** |
| 3-7 | Model adapters, Reactive Resume rendering, mail, applications, feedback, MCP, scheduler | Not implemented (no fake stubs) |

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
offerbomber resume plan "distributed training" --skill PyTorch --job-id 42 --pages 2
```

Every command accepts `--config PATH`. See `examples/README.md` for an end-to-end walkthrough using a generated sample PDF and an isolated demo database.

## Design principles

- **Truth source:** the experience database is authoritative. Models (when added later) may only organize or rewrite confirmed facts; they may not invent experience, grades, companies or metrics.
- **Explicit IDs:** retrieval results and ResumePlan items carry concrete `Experience ID` and `Fact ID`, plus source type, locator and PDF SHA-256.
- **Immutable history:** each experience change stores an immutable JSON snapshot. Restoring creates a new version; already-generated `resume_versions` snapshots are never modified.
- **Confirmed-only planning:** `ResumePlan` excludes `UNVERIFIED` facts. A statement like "我的论文投了 ICML" stays verbatim and never becomes "ICML Accepted Paper".
- **Bounded parsing:** the PDF parser is deterministic (headings, contacts, verbatim lines), not an LLM. Low-confidence candidates require human confirmation. Scanned PDFs are rejected with an explicit OCR limitation message.

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
PYTHONPATH=src pytest tests/ -q        # 35 passed
```

Coverage includes Chinese dedup, malformed source isolation, single-record isolation, PDF extraction, duplicate PDF import, full experience creation, fact addition, version history, archive/restore, Chinese and English retrieval, requirement ranking, unconfirmed-fact exclusion, snapshot immutability, restart persistence and a fresh Alembic upgrade.

## Known limitations

- PDF parsing is deterministic, not semantic; complex layouts, tables and scanned PDFs need a model/OCR or manual entry.
- `official_careers`, `search_engine`, `xiaohongshu_public` and `auto_expand_companies` are configuration placeholders only.
- No model calls, Reactive Resume rendering, email, applications, reports, MCP or scheduling in this release.
- Recruitment email extraction is not implemented; `apply_method` stays `official_form`/`unknown`.

## License

Apache License 2.0 (`LICENSE`), consistent with `pyproject.toml`.
