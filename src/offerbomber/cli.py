"""OfferBomber command line interface.

Phase 1 discovery and Phase 2 personal experience knowledge base are implemented.
Later-phase commands are intentionally absent rather than stubbed.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import structlog
import typer
from sqlalchemy import select

from .app import AppContext, resolve_db_path
from .config import load_settings, unimplemented_capabilities, write_default_config
from .core.discovery_service import DiscoveryService
from .core.job_analysis import JobAnalyzer
from .core.reranking import ModelReranker
from .core.retrieval import FactRetrievalService, RetrievalQuery
from .core.smart_planning import SmartResumePlanner
from .integrations.reactive_resume import ReactiveResumeProvider
from .models.factory import auto_select, build_provider, is_real
from .profile.importer import ResumeImportService
from .profile.model_parser import ModelResumeParser
from .profile.pdf import PyMuPDFExtractor
from .profile.service import ProfileKnowledgeBase
from .storage.models import Company, Education, Experience, Job, Profile, ResumeVersion, ExperienceFact

app = typer.Typer(
    name="offerbomber",
    help="Local-first personal job-hunting automation system",
    no_args_is_help=True,
    add_completion=False,
)
jobs_app = typer.Typer(help="Inspect discovered jobs")
profile_app = typer.Typer(help="Profile and PDF resume import")
experience_app = typer.Typer(help="Manage experiences")
fact_app = typer.Typer(help="Manage atomic facts")
search_app = typer.Typer(help="Search the experience knowledge base")
resume_app = typer.Typer(help="Resume planning, generation and versions")
model_app = typer.Typer(help="Model provider diagnostics")
mcp_app = typer.Typer(help="Human approval of pending MCP write operations")
for sub_app, name in (
    (jobs_app, "jobs"), (profile_app, "profile"), (experience_app, "experience"),
    (fact_app, "fact"), (search_app, "search"), (resume_app, "resume"),
    (model_app, "model"), (mcp_app, "mcp"),
):
    app.add_typer(sub_app, name=name)

log = structlog.get_logger(__name__)


def _default_config_path() -> Path:
    return Path.cwd() / "config.yaml"


def _require_config(path: Path | None) -> AppContext:
    config_path = path or _default_config_path()
    if not config_path.is_file():
        raise typer.BadParameter(
            f"config not found at {config_path}. Run 'offerbomber init' first."
        )
    return AppContext.from_config(config_path)


def _kb(ctx: AppContext, user_id: str) -> ProfileKnowledgeBase:
    return ProfileKnowledgeBase(ctx.sessions, user_id)


def _split_csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _provider(ctx: AppContext, *, preferred: str | None = None):
    """Build a model provider from config; explicit choice overrides config."""
    name = preferred or ctx.settings.models.preferred
    cfg = ctx.settings.models
    if name == "auto":
        return asyncio.run(auto_select(
            timeout=cfg.timeout_seconds, max_attempts=cfg.max_attempts
        ))
    return build_provider(name, timeout=cfg.timeout_seconds, max_attempts=cfg.max_attempts)


def _profile_data(ctx: AppContext, user_id: str):
    """Load Profile, Education rows and Experience lookup for generation."""
    with ctx.session() as session:
        profile = session.execute(
            select(Profile).where(Profile.user_id == user_id)
        ).scalar_one_or_none()
        educations = session.execute(
            select(Education).where(Education.profile_id == profile.id)
        ).scalars().all() if profile else []
        experiences = {
            exp.id: exp
            for exp in session.execute(
                select(Experience).where(Experience.user_id == user_id)
            ).scalars().all()
        }
        return profile, list(educations), experiences


def _job_by_id(ctx: AppContext, job_id: int) -> Job:
    with ctx.session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise typer.BadParameter(f"job {job_id} not found")
        session.expunge(job)
        return job


@app.command()
def init(
    config: Path = typer.Option(None, "--config", help="target config path (default: ./config.yaml)"),
    force: bool = typer.Option(False, "--force", help="overwrite an existing config"),
) -> None:
    """Create config.yaml and initialize the local SQLite database."""
    target = config or _default_config_path()
    if target.exists() and not force:
        typer.echo(f"config already exists: {target} (use --force to overwrite)")
        raise typer.Exit(code=1)
    if target.exists() and force:
        target.unlink()
    write_default_config(target)
    ctx = AppContext.from_config(target)
    ctx.close()

    # Bring the database to the current schema so commands work right after init.
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig
    from .storage.base import _sqlite_path_to_url

    root = Path(__file__).resolve().parents[2]
    alembic_config = AlembicConfig(str(root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(root / "migrations"))
    previous = os.environ.get("OFFERBOMBER_DB_URL")
    os.environ["OFFERBOMBER_DB_URL"] = _sqlite_path_to_url(ctx.settings.storage.db_path)
    try:
        alembic_command.upgrade(alembic_config, "head")
    finally:
        if previous is None:
            os.environ.pop("OFFERBOMBER_DB_URL", None)
        else:
            os.environ["OFFERBOMBER_DB_URL"] = previous
    typer.echo(f"initialized config: {target}")
    typer.echo("database schema upgraded to head")
    typer.echo("Next: review config.yaml (especially app.timezone), then run 'offerbomber doctor'.")


@app.command()
def doctor(config: Path = typer.Option(None, "--config")) -> None:
    """Check config validity, database reachability and enabled sources."""
    ctx = _require_config(config)
    settings = ctx.settings
    from sqlalchemy import text

    with ctx.session() as session:
        session.execute(text("SELECT 1"))
    enabled = [c.token for c in settings.discovery.companies]
    typer.echo("doctor: OK")
    typer.echo(f"  timezone       : {settings.app.timezone}")
    typer.echo(f"  dry_run        : {settings.app.dry_run}")
    typer.echo(f"  auto_send      : {settings.applications.auto_send}")
    typer.echo(f"  db_path        : {settings.storage.db_path}")
    typer.echo(f"  fts5 available : {ctx.fts_available}")
    typer.echo(f"  configured boards: {len(enabled)}")
    typer.echo("  implemented: config, SQLite, Greenhouse/Lever/Ashby, dedup, filters, profile KB,")
    typer.echo("    model abstraction, model parsing, JD analysis, reranking, smart planning, FTS consistency")
    from .config import CAPABILITY_STATUS

    adapters = {k: v for k, v in CAPABILITY_STATUS.items() if v == "adapter_implemented_not_live_verified"}
    if adapters:
        typer.echo("  adapters implemented but NOT live-verified (need credentials):")
        for capability in sorted(adapters):
            typer.echo(f"    - {capability}")
    typer.echo("  configured but NOT implemented:")
    for capability in unimplemented_capabilities():
        typer.echo(f"    - {capability}")
    ctx.close()


@app.command()
def discover(
    config: Path = typer.Option(None, "--config"),
    limit: int = typer.Option(0, "--limit", min=0, help="process only the first N boards"),
) -> None:
    """Run Phase 1 job discovery against configured public job boards."""
    ctx = _require_config(config)
    targets = list(ctx.settings.discovery.companies)
    if limit:
        targets = targets[:limit]

    async def _run() -> None:
        service = DiscoveryService(ctx.settings, ctx.sessions)
        try:
            stats = await service.run(targets)
        finally:
            await service.aclose()
        typer.echo(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))

    asyncio.run(_run())
    ctx.close()


@jobs_app.command("list")
def jobs_list(
    config: Path = typer.Option(None, "--config"),
    limit: int = typer.Option(20, "--limit", min=1),
    platform: str | None = typer.Option(None, "--platform"),
) -> None:
    """List discovered jobs from the local database."""
    ctx = _require_config(config)
    with ctx.session() as session:
        stmt = select(Job).order_by(Job.discovered_at.desc()).limit(limit)
        if platform:
            stmt = select(Job).where(Job.source_platform == platform).order_by(
                Job.discovered_at.desc()
            ).limit(limit)
        rows = session.execute(stmt).scalars().all()
        for job in rows:
            typer.echo(
                f"[{job.id}] {job.company} | {job.title} | {job.location or '-'} | "
                f"{job.source_platform} | {job.validity_status}"
            )
        typer.echo(f"total shown: {len(rows)}")
    ctx.close()


@jobs_app.command("stats")
def jobs_stats(config: Path = typer.Option(None, "--config")) -> None:
    """Show basic discovery statistics."""
    ctx = _require_config(config)
    from sqlalchemy import func

    with ctx.session() as session:
        job_count = session.execute(select(func.count(Job.id))).scalar_one()
        company_count = session.execute(select(func.count(Company.id))).scalar_one()
        by_platform = session.execute(
            select(Job.source_platform, func.count(Job.id)).group_by(Job.source_platform)
        ).all()
    typer.echo(f"jobs: {job_count}")
    typer.echo(f"companies: {company_count}")
    for platform, count in by_platform:
        typer.echo(f"  {platform}: {count}")
    ctx.close()


# -- Phase 2: profile and PDF import -------------------------------------


@profile_app.command("import")
def profile_import(
    pdf: Path = typer.Argument(..., exists=True, readable=True, help="PDF resume to import"),
    user_id: str = typer.Option("default", "--user-id"),
    confirm_all: bool = typer.Option(False, "--confirm-all", help="confirm every candidate immediately"),
    parser: str = typer.Option("deterministic", "--parser", help="deterministic | model"),
    provider_name: str | None = typer.Option(None, "--provider"),
    as_json: bool = typer.Option(False, "--json"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Extract a PDF resume into reviewable candidates (no facts are written by default)."""
    ctx = _require_config(config)
    service = ResumeImportService(ctx.sessions, user_id)
    if parser == "model":
        model_parser = ModelResumeParser(_provider(ctx, preferred=provider_name))
        preview = asyncio.run(service.apreview(pdf, model_parser))
    elif parser == "deterministic":
        preview = service.preview(pdf)
    else:
        raise typer.BadParameter("--parser must be 'deterministic' or 'model'")
    if as_json:
        _echo_json(
            {
                "import_id": preview.import_id,
                "duplicate": preview.duplicate,
                "file_name": preview.file_name,
                "page_count": preview.page_count,
                "extractable": preview.extractable,
                "warnings": preview.warnings,
                "candidate": preview.candidate.model_dump(),
            }
        )
    else:
        typer.echo(f"import_id={preview.import_id} duplicate={preview.duplicate} pages={preview.page_count}")
        candidate = preview.candidate
        typer.echo(f"name={candidate.display_name or 'UNKNOWN'} contact={candidate.contact or 'UNKNOWN'}")
        for index, education in enumerate(candidate.educations):
            typer.echo(f"  education[{index}] {education.school or 'UNKNOWN'} / {education.degree or 'UNKNOWN'}")
        for index, experience in enumerate(candidate.experiences):
            typer.echo(f"  experience[{index}] {experience.type} | {experience.title} @ {experience.locator}")
            for fact_index, fact in enumerate(experience.facts):
                typer.echo(f"      fact[{fact_index}] {fact.content[:100]}")
        for warning in preview.warnings:
            typer.echo(f"  warning: {warning}")
        typer.echo("review with: offerbomber profile confirm <import_id>")
    if confirm_all and preview.extractable:
        fields = {
            key for key in ("display_name", "contact", "summary", "target_position")
            if getattr(preview.candidate, key) is not None
        }
        result = service.confirm(
            preview.import_id,
            fields=fields,
            experience_indices=list(range(len(preview.candidate.experiences))),
            education_indices=list(range(len(preview.candidate.educations))),
        )
        typer.echo("confirmed: " + json.dumps(result, ensure_ascii=False))
    ctx.close()


@profile_app.command("confirm")
def profile_confirm(
    import_id: int = typer.Argument(...),
    select_all: bool = typer.Option(False, "--all"),
    fields: str = typer.Option("", "--fields", help="comma-separated: display_name,contact,summary,target_position"),
    experiences: str = typer.Option("", "--experiences", help="comma-separated candidate indices"),
    educations: str = typer.Option("", "--educations", help="comma-separated candidate indices"),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Write only explicitly selected candidates as confirmed facts."""
    ctx = _require_config(config)
    service = ResumeImportService(ctx.sessions, user_id)
    if select_all:
        from .storage.models import ResumeImport

        with ctx.session() as session:
            row = session.get(ResumeImport, import_id)
            if row is None or not row.candidate_payload:
                raise typer.BadParameter(f"import {import_id} has no candidate data")
            payload = json.loads(row.candidate_payload)
        selected_fields = {
            key for key in ("display_name", "contact", "summary", "target_position")
            if payload.get(key) is not None
        }
        exp_indices = list(range(len(payload.get("experiences", []))))
        edu_indices = list(range(len(payload.get("educations", []))))
    else:
        selected_fields = set(_split_csv(fields))
        exp_indices = [int(value) for value in _split_csv(experiences)]
        edu_indices = [int(value) for value in _split_csv(educations)]
    result = service.confirm(
        import_id,
        fields=selected_fields,
        experience_indices=exp_indices,
        education_indices=edu_indices,
    )
    _echo_json(result)
    ctx.close()


@profile_app.command("show")
def profile_show(
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Show the profile and all experiences."""
    ctx = _require_config(config)
    _echo_json(_kb(ctx, user_id).show())
    ctx.close()


# -- Phase 2: experience and facts ---------------------------------------


@experience_app.command("list")
def experience_list(
    user_id: str = typer.Option("default", "--user-id"),
    include_archived: bool = typer.Option(False, "--include-archived"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """List experiences in the knowledge base."""
    ctx = _require_config(config)
    _echo_json(_kb(ctx, user_id).list_experiences(include_archived=include_archived))
    ctx.close()


@experience_app.command("show")
def experience_show(
    experience_id: int = typer.Argument(...),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Show one experience with its atomic facts."""
    ctx = _require_config(config)
    _echo_json(_kb(ctx, user_id).detail(experience_id))
    ctx.close()


@experience_app.command("add")
def experience_add(
    type: str = typer.Argument(..., help="WORK|INTERNSHIP|RESEARCH|PROJECT|PAPER|COMPETITION|AWARD|OPEN_SOURCE|CUSTOM"),
    title: str = typer.Argument(...),
    organization: str | None = typer.Option(None),
    location: str | None = typer.Option(None),
    start_date: str | None = typer.Option(None),
    end_date: str | None = typer.Option(None),
    description: str | None = typer.Option(None),
    tags: str = typer.Option("", help="comma-separated skill tags"),
    fact: list[str] | None = typer.Option(None, "--fact", help="repeatable atomic fact"),
    confirm_facts: bool = typer.Option(False, "--confirm-facts"),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Add an experience with optional verbatim facts (UNVERIFIED until confirmed)."""
    ctx = _require_config(config)
    kb = _kb(ctx, user_id)
    experience_id = kb.add_experience(
        {
            "type": type, "title": title, "organization": organization,
            "location": location, "start_date": start_date, "end_date": end_date,
            "description": description, "tags": _split_csv(tags),
        },
        facts=[{"content": content, "category": type.upper()} for content in (fact or [])],
        confirm_facts=confirm_facts,
    )
    typer.echo(f"created experience {experience_id}")
    ctx.close()


@experience_app.command("edit")
def experience_edit(
    experience_id: int = typer.Argument(...),
    title: str | None = typer.Option(None),
    organization: str | None = typer.Option(None),
    location: str | None = typer.Option(None),
    start_date: str | None = typer.Option(None),
    end_date: str | None = typer.Option(None),
    status: str | None = typer.Option(None),
    description: str | None = typer.Option(None),
    tags: str | None = typer.Option(None),
    note: str | None = typer.Option(None, "--note"),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Edit an experience; the previous state is kept as a version snapshot."""
    ctx = _require_config(config)
    changes = {
        "title": title, "organization": organization, "location": location,
        "start_date": start_date, "end_date": end_date, "status": status,
        "description": description,
    }
    if tags is not None:
        changes["tags"] = _split_csv(tags)
    result = _kb(ctx, user_id).update(experience_id, changes, note)
    typer.echo(f"updated experience {experience_id} to version {result['current_version']}")
    ctx.close()


@experience_app.command("history")
def experience_history(
    experience_id: int = typer.Argument(...),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """List immutable versions of an experience."""
    ctx = _require_config(config)
    _echo_json(_kb(ctx, user_id).history(experience_id))
    ctx.close()


@experience_app.command("show-version")
def experience_show_version(
    experience_id: int = typer.Argument(...),
    version: int = typer.Argument(...),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Show a historical snapshot without changing current data."""
    ctx = _require_config(config)
    _echo_json(_kb(ctx, user_id).show_version(experience_id, version))
    ctx.close()


@experience_app.command("restore")
def experience_restore(
    experience_id: int = typer.Argument(...),
    version: int = typer.Argument(...),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Restore an older version as a new version (history is preserved)."""
    ctx = _require_config(config)
    _echo_json(_kb(ctx, user_id).restore(experience_id, version))
    ctx.close()


@experience_app.command("archive")
def experience_archive(
    experience_id: int = typer.Argument(...),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Archive an experience without deleting its history."""
    ctx = _require_config(config)
    _echo_json(_kb(ctx, user_id).archive(experience_id))
    ctx.close()


@experience_app.command("unarchive")
def experience_unarchive(
    experience_id: int = typer.Argument(...),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Restore an archived experience to ACTIVE."""
    ctx = _require_config(config)
    _echo_json(_kb(ctx, user_id).unarchive(experience_id))
    ctx.close()


@fact_app.command("add")
def fact_add(
    experience_id: int = typer.Argument(...),
    content: str = typer.Argument(...),
    category: str = typer.Option("OTHER"),
    skills: str = typer.Option("", help="comma-separated skill tags"),
    locator: str | None = typer.Option(None, help="source locator, e.g. p2:L7"),
    evidence: str | None = typer.Option(None),
    confirm: bool = typer.Option(False, "--confirm", help="mark as user-confirmed fact"),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Add an atomic fact. Facts are UNVERIFIED unless --confirm is given."""
    ctx = _require_config(config)
    fact_id = _kb(ctx, user_id).add_fact(
        experience_id,
        content=content,
        category=category,
        skills=_split_csv(skills),
        locator=locator,
        evidence=evidence,
        confirm=confirm,
    )
    typer.echo(f"created fact {fact_id}")
    ctx.close()


@fact_app.command("verify")
def fact_verify(
    fact_id: int = typer.Argument(...),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Mark a fact as user-confirmed so it may enter resume plans."""
    ctx = _require_config(config)
    _echo_json(_kb(ctx, user_id).verify_fact(fact_id))
    ctx.close()


@fact_app.command("delete")
def fact_delete(
    fact_id: int = typer.Argument(...),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Soft-delete a fact and remove it from the search index."""
    ctx = _require_config(config)
    _kb(ctx, user_id).delete_fact(fact_id)
    typer.echo(f"deleted fact {fact_id}")
    ctx.close()


@search_app.command("facts")
def search_facts(
    text: str = typer.Argument(""),
    skill: list[str] | None = typer.Option(None, "--skill"),
    direction: str | None = typer.Option(None),
    verified_only: bool = typer.Option(False, "--verified-only"),
    limit: int = typer.Option(10, "--limit", min=1),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Retrieve facts with IDs, scores, reasons and provenance."""
    ctx = _require_config(config)
    service = FactRetrievalService(ctx.sessions, user_id)
    query = RetrievalQuery(
        text=text,
        required_skills=list(skill or []),
        direction=direction,
        max_results=limit,
        verified_only=verified_only,
    )
    _echo_json([match.as_dict() for match in service.search(query)])
    ctx.close()


# -- Phase 3: model diagnostics ------------------------------------------


@model_app.command("doctor")
def model_doctor(
    provider_name: str | None = typer.Option(None, "--provider"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Check model provider availability. NEVER reports a mock as a live model."""
    ctx = _require_config(config)
    name = provider_name or ctx.settings.models.preferred
    if name == "auto":
        provider = asyncio.run(auto_select(
            timeout=ctx.settings.models.timeout_seconds,
            max_attempts=ctx.settings.models.max_attempts,
        ))
    else:
        provider = _provider(ctx, preferred=name)
    health = asyncio.run(provider.health_check())
    _echo_json({
        "selected": provider.name,
        "requested": name,
        "available": health.available,
        "detail": health.detail,
        "real_live_verified": health.real_live_verified,
        "is_mock": not is_real(provider),
    })
    ctx.close()


@model_app.command("test")
def model_test(
    provider_name: str | None = typer.Option(None, "--provider"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Run a structured-output smoke test against the selected provider."""
    from .core.job_analysis import JobRequirements
    from .models.base import TaskType

    ctx = _require_config(config)
    provider = _provider(ctx, preferred=provider_name)
    schema = JobRequirements

    async def _run():
        result = await provider.generate_structured(
            TaskType.JOB_ANALYSIS,
            [{"role": "user", "content": "Return a JobRequirements JSON with job_id 0 and no requirements."}],
            schema,
        )
        return result.model_dump()

    try:
        output = asyncio.run(_run())
        _echo_json({
            "success": True,
            "provider": provider.name,
            "is_mock": not is_real(provider),
            "real_live_verified": is_real(provider),
            "output": output,
            "note": "mock success is NOT a real model call" if not is_real(provider) else "real provider responded",
        })
    except Exception as exc:  # noqa: BLE001 - CLI surfaces all errors
        typer.echo(f"model test failed: {exc}", err=True)
        raise typer.Exit(code=1)
    ctx.close()


# -- Phase 3: jobs / search / resume -------------------------------------


@jobs_app.command("analyze")
def jobs_analyze(
    job_id: int = typer.Option(..., "--job-id"),
    provider_name: str | None = typer.Option(None, "--provider"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Analyze a real stored job posting into structured JobRequirements."""
    ctx = _require_config(config)
    job = _job_by_id(ctx, job_id)
    provider = _provider(ctx, preferred=provider_name)
    result = asyncio.run(JobAnalyzer(provider).analyze(job))
    _echo_json(result.model_dump())
    ctx.close()


@search_app.command("rerank")
def search_rerank(
    job_id: int = typer.Option(..., "--job-id"),
    text: str = typer.Argument(""),
    skill: list[str] | None = typer.Option(None, "--skill"),
    provider_name: str | None = typer.Option(None, "--provider"),
    limit: int = typer.Option(20, "--limit", min=1),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Two-stage retrieval: deterministic recall then model reranking."""
    ctx = _require_config(config)
    job = _job_by_id(ctx, job_id)
    provider = _provider(ctx, preferred=provider_name)

    async def _run():
        requirements = await JobAnalyzer(provider).analyze(job)
        retrieval = FactRetrievalService(ctx.sessions, user_id)
        query = RetrievalQuery(
            text=text or job.title,
            required_skills=list(skill or []),
            direction=job.title,
            max_results=limit,
            verified_only=True,
        )
        candidates = retrieval.search(query)
        ranked = await ModelReranker(provider).rerank(query, candidates, requirements)
        return requirements, ranked

    requirements, ranked = asyncio.run(_run())
    _echo_json({
        "job_id": job_id,
        "provider": provider.name,
        "is_mock": not is_real(provider),
        "requirements": requirements.model_dump(),
        "matches": [m.as_dict() for m in ranked],
    })
    ctx.close()


@resume_app.command("plan")
def resume_plan_smart(
    text: str = typer.Argument(""),
    job_id: int | None = typer.Option(None, "--job-id"),
    skill: list[str] | None = typer.Option(None, "--skill"),
    direction: str | None = typer.Option(None),
    language: str = typer.Option("zh"),
    pages: int = typer.Option(1, "--pages", min=1, max=2),
    provider_name: str | None = typer.Option(None, "--provider"),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Build a job-tailored ResumePlan from confirmed facts (no rendering)."""
    ctx = _require_config(config)
    provider = _provider(ctx, preferred=provider_name)
    job = _job_by_id(ctx, job_id) if job_id is not None else None
    profile, educations, _experiences = _profile_data(ctx, user_id)

    async def _run():
        requirements = await JobAnalyzer(provider).analyze(job) if job else None
        retrieval = FactRetrievalService(ctx.sessions, user_id)
        query = RetrievalQuery(
            text=text or (job.title if job else ""),
            required_skills=list(skill or []),
            direction=direction or (job.title if job else None),
            max_results=40,
            verified_only=True,
        )
        candidates = retrieval.search(query)
        ranked = await ModelReranker(provider).rerank(query, candidates, requirements)
        return await SmartResumePlanner(provider, ctx.sessions).plan(
            profile=profile, educations=educations, ranked_facts=ranked,
            requirements=requirements, language=language, target_page_count=pages,
        )

    plan = asyncio.run(_run())
    _echo_json(plan.model_dump())
    ctx.close()


@resume_app.command("generate")
def resume_generate(
    job_id: int = typer.Option(..., "--job-id"),
    source_resume_id: str = typer.Option(..., "--source-resume-id", help="your Reactive Resume base resume id (a clone is made)"),
    provider_name: str | None = typer.Option(None, "--provider"),
    language: str = typer.Option("zh"),
    pages: int = typer.Option(1, "--pages", min=1, max=2),
    output_dir: Path = typer.Option(None, "--output-dir"),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Full JD-to-PDF flow. Requires a configured Reactive Resume API key."""
    ctx = _require_config(config)
    provider = _provider(ctx, preferred=provider_name)
    rr = ReactiveResumeProvider()
    health = asyncio.run(rr.health_check())
    if not health["available"]:
        typer.echo(f"Reactive Resume unavailable: {health['detail']}", err=True)
        raise typer.Exit(code=2)

    job = _job_by_id(ctx, job_id)
    profile, educations, experiences = _profile_data(ctx, user_id)

    async def _run():
        requirements = await JobAnalyzer(provider).analyze(job)
        retrieval = FactRetrievalService(ctx.sessions, user_id)
        query = RetrievalQuery(text=job.title, direction=job.title, max_results=40, verified_only=True)
        candidates = retrieval.search(query)
        ranked = await ModelReranker(provider).rerank(query, candidates, requirements)
        plan = await SmartResumePlanner(provider, ctx.sessions).plan(
            profile=profile, educations=educations, ranked_facts=ranked,
            requirements=requirements, language=language, target_page_count=pages,
        )
        target = output_dir or (Path.cwd() / "output" / "resumes")
        return await rr.generate(
            job_id=job_id, source_resume_id=source_resume_id, plan=plan,
            profile=profile, educations=educations, experiences=experiences,
            output_dir=target,
        )

    version = asyncio.run(_run())
    with ctx.session() as session:
        session.add(version)
        session.commit()
        version_id = version.id
    typer.echo(f"created resume version {version_id}: {version.file_path}")
    typer.echo(f"sha256={version.file_sha256}")
    typer.echo("note: PDF passed automated checks only; review layout manually")
    ctx.close()


@resume_app.command("validate")
def resume_validate(
    file: Path = typer.Option(..., "--file", exists=True, readable=True),
    name: str = typer.Option("", "--name"),
    term: list[str] | None = typer.Option(None, "--term"),
) -> None:
    """Validate a PDF with PyMuPDF (exists/openable/text/name/key content)."""
    report = ReactiveResumeProvider.validate_pdf(
        file, expected_name=name, expected_terms=list(term or [])
    )
    _echo_json(report.as_dict())


@resume_app.command("versions")
def resume_versions(
    job_id: int | None = typer.Option(None, "--job-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """List immutable generated resume versions."""
    ctx = _require_config(config)
    with ctx.session() as session:
        stmt = select(ResumeVersion).order_by(ResumeVersion.id)
        if job_id is not None:
            stmt = stmt.where(ResumeVersion.job_id == job_id)
        rows = session.execute(stmt).scalars().all()
        _echo_json([
            {
                "id": row.id, "job_id": row.job_id, "file_path": row.file_path,
                "file_sha256": row.file_sha256, "immutable": row.immutable,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ])
    ctx.close()


@resume_app.command("show")
def resume_show(
    id: int = typer.Option(..., "--id"),
    config: Path = typer.Option(None, "--config"),
    user_id: str = typer.Option("default", "--user"),
) -> None:
    """Show one generated resume version with its fact snapshot (user-scoped)."""
    from .integrations.mcp.authorization import load_authorized_version

    ctx = _require_config(config)
    with ctx.session() as session:
        row, err = load_authorized_version(session, id, user_id)
        if err is not None or row is None:
            raise typer.BadParameter(f"resume version {id} not visible to user '{user_id}'")
        _echo_json({
            "id": row.id, "job_id": row.job_id, "file_path": row.file_path,
            "file_sha256": row.file_sha256, "immutable": row.immutable,
            "plan_sha256": getattr(row, "plan_sha256", None),
            "fact_snapshot": json.loads(row.fact_snapshot) if row.fact_snapshot else [],
        })
    ctx.close()


def _mcp_store(config: Path | None, user_id: str):
    """Build a confirmation ledger bound to the configured database."""
    ctx = _require_config(config)
    from .integrations.mcp.confirmations import ConfirmationStore

    db_path = Path(resolve_db_path(ctx.settings))
    store = ConfirmationStore(db_path, session_factory=ctx.sessions)
    return ctx, store


@mcp_app.command("pending")
def mcp_pending(
    config: Path = typer.Option(None, "--config"),
    user_id: str = typer.Option("default", "--user"),
) -> None:
    """List write operations awaiting your approval (local human action)."""
    ctx, store = _mcp_store(config, user_id)
    rows = store.list_pending(user_id=user_id)
    if not rows:
        typer.echo("No pending approvals.")
    else:
        _echo_json(rows)
    ctx.close()


@mcp_app.command("preview")
def mcp_preview(
    token: str = typer.Argument(..., help="Full confirmation token from prepare_*"),
    config: Path = typer.Option(None, "--config"),
    user_id: str = typer.Option("default", "--user"),
) -> None:
    """Show the exact approved content behind a token before you approve it."""
    ctx, store = _mcp_store(config, user_id)
    record = store.get_record(token, user_id=user_id)
    if record is None:
        raise typer.BadParameter("token not found, expired, or belongs to another user")
    snapshot = store.peek(token, user_id=user_id)
    _echo_json({
        "action": record["action"],
        "status": record["status"],
        "plan_sha256": record["plan_sha256"],
        "expires_at": record["expires_at"].isoformat() if record["expires_at"] else None,
        "snapshot": snapshot,
    })
    ctx.close()


@mcp_app.command("approve")
def mcp_approve(
    token: str = typer.Argument(..., help="Full confirmation token"),
    config: Path = typer.Option(None, "--config"),
    user_id: str = typer.Option("default", "--user"),
) -> None:
    """Approve a reviewed operation. Only a human at this terminal can call this."""
    ctx, store = _mcp_store(config, user_id)
    if store.approve(token, user_id=user_id):
        typer.echo(f"Approved token for user '{user_id}'. The host may now perform the write.")
    else:
        raise typer.BadParameter(
            "approval failed: token unknown/expired/already decided, or user mismatch"
        )
    ctx.close()


@mcp_app.command("reject")
def mcp_reject(
    token: str = typer.Argument(..., help="Full confirmation token"),
    config: Path = typer.Option(None, "--config"),
    user_id: str = typer.Option("default", "--user"),
) -> None:
    """Reject an operation. This is irreversible."""
    ctx, store = _mcp_store(config, user_id)
    if store.reject(token, user_id=user_id):
        typer.echo("Rejected. The token can no longer be used.")
    else:
        raise typer.BadParameter("reject failed: token unknown/already decided, or user mismatch")
    ctx.close()


@app.command()
def status(config: Path = typer.Option(None, "--config")) -> None:
    """Show the current system status and capability boundary."""
    ctx = _require_config(config)
    typer.echo("OfferBomber 0.3.0 — Phase 1 + Phase 2 + Phase 3")
    typer.echo("implemented: discovery, SQLite schema, FTS consistency, profile KB, PDF import,")
    typer.echo("  versioning, fact retrieval, ModelProvider, model parsing, JD analysis,")
    typer.echo("  reranking, smart ResumePlan, Reactive Resume adapter, PDF validation")
    typer.echo("  NOTE: model host calls and Reactive Resume need real credentials for live use.")
    typer.echo("not implemented (configuration placeholders or later phases):")
    for capability in unimplemented_capabilities():
        typer.echo(f"  - {capability}")
    ctx.close()
