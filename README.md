# OfferBomber

Local-first personal job-hunting automation system. Phase 1 (job discovery + persistence) is implemented and verified against real public APIs.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Project skeleton, config, SQLite, Greenhouse/Lever/Ashby, dedup, filters, CLI, tests | **Implemented & verified** |
| 2-7 | Profile KB, model adapters, resume, mail, feedback, MCP, scheduling | Not implemented (no fake stubs) |

## Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
offerbomber init
```

`init` creates `config.yaml` from `config.example.yaml`. Review `app.timezone` before use.

## Commands (Phase 1)

```bash
offerbomber doctor                 # validate config + DB connection
offerbomber discover               # fetch all configured boards
offerbomber discover --limit 1     # fetch first board only
offerbomber jobs list --limit 20
offerbomber jobs list --platform greenhouse
offerbomber jobs stats
offerbomber status
```

Any command accepts `--config PATH` to use a non-default config location.

## Configuration

Copy `config.example.yaml`. To add a board:

```yaml
discovery:
  companies:
    - platform: greenhouse   # greenhouse | lever | ashby
      token: <board-token>
      name: <display-name>
```

- `app.dry_run: true` is the default. `applications.auto_send` is `false` and stays false until later phases.
- No credentials live in YAML. See `.env.example` for services that need them in later phases.
- Timezone is validated at load time (requires `tzdata` on Windows, already a dependency).

## Verified data sources (2026-11-19)

| Source | Endpoint | Auth | Verified result |
|---|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | none | 781 jobs (token `stripe`) |
| Lever | `GET api.lever.co/v0/postings/{token}?mode=json` | none | 10 jobs (token `leverdemo`) |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` | none | 81 jobs (token `ashby`) |

API field names come from each provider's official documentation. Notably:
- Lever's public list has **no `createdAt`** field — `published_at` stays `None` instead of being invented.
- Ashby has **no job `id`** — a stable ID is derived from the canonical `jobUrl`.

## Dedup rules (task book 7.3)

1. Same platform + external job ID
2. Same normalized URL (tracking params stripped)
3. Same company + title + location + description similarity >= 0.88

All discovery sources for one job are kept in `job_source_records`; the primary `jobs` row is never duplicated on re-scan.

## Database

All 25 tables from task book section 15 are created by Alembic:

```bash
alembic upgrade head
alembic current
```

Phase 1 actively writes: `companies`, `job_sources`, `jobs`, `job_source_records`. Default DB path: `.offerbomber/offerbomber.db` (gitignored).

## Tests

```bash
PYTHONPATH=src pytest tests/ -q
```

16 tests cover connector parsing against official-shaped payloads, URL/ID normalization, the HTTP retry policy (retry-then-success, bounded exhaustion, no-retry on 4xx), hard filters, three dedup paths, and a full discovery run with failure isolation.

## Known limitations (Phase 1)

- `official_careers`, `search_engine`, `xiaohongshu_public` sources are declared in config but **not implemented** — the CLI logs them as not-implemented rather than pretending to fetch.
- No recruitment email extraction yet, so `apply_method` defaults to `official_form`/`unknown`.
- No scheduling, model calls, resume generation, mail sending, or MCP server yet.
- Live API limits: Greenhouse may rate-limit; 4xx client errors are not retried by design.
