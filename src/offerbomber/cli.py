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

from .app import AppContext
from .config import load_settings, unimplemented_capabilities, write_default_config
from .core.discovery_service import DiscoveryService
from .core.retrieval import FactRetrievalService, RetrievalQuery
from .core.resume_plan import ResumePlanBuilder
from .profile.importer import ResumeImportService
from .profile.service import ProfileKnowledgeBase
from .storage.models import Company, Job

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
resume_app = typer.Typer(help="Resume content planning (no rendering)")
for sub_app, name in (
    (jobs_app, "jobs"), (profile_app, "profile"), (experience_app, "experience"),
    (fact_app, "fact"), (search_app, "search"), (resume_app, "resume"),
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
    typer.echo("  implemented: config, SQLite, Greenhouse/Lever/Ashby, dedup, filters, profile KB")
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
    as_json: bool = typer.Option(False, "--json"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Extract a PDF resume into reviewable candidates (no facts are written by default)."""
    ctx = _require_config(config)
    service = ResumeImportService(ctx.sessions, user_id)
    preview = service.preview(pdf)
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


@resume_app.command("plan")
def resume_plan(
    text: str = typer.Argument(""),
    skill: list[str] | None = typer.Option(None, "--skill"),
    direction: str | None = typer.Option(None),
    job_id: str | None = typer.Option(None, "--job-id"),
    language: str = typer.Option("zh"),
    pages: int = typer.Option(2, "--pages", min=1, max=5),
    user_id: str = typer.Option("default", "--user-id"),
    config: Path = typer.Option(None, "--config"),
) -> None:
    """Build a ResumePlan from confirmed facts only. No PDF rendering here."""
    ctx = _require_config(config)
    retrieval = FactRetrievalService(ctx.sessions, user_id)
    query = RetrievalQuery(text=text, required_skills=list(skill or []), direction=direction)
    plan = ResumePlanBuilder(retrieval).build(
        query, job_id=job_id, language=language, target_page_count=pages
    )
    _echo_json(plan.model_dump())
    ctx.close()


@app.command()
def status(config: Path = typer.Option(None, "--config")) -> None:
    """Show the current system status and capability boundary."""
    ctx = _require_config(config)
    typer.echo("OfferBomber 0.2.0 — Phase 1 + Phase 2")
    typer.echo("implemented: config, SQLite schema, Greenhouse/Lever/Ashby, dedup, hard filters,")
    typer.echo("  PDF import, experience/fact CRUD, versioning, fact retrieval, ResumePlan candidates")
    typer.echo("not implemented (configuration placeholders or later phases):")
    for capability in unimplemented_capabilities():
        typer.echo(f"  - {capability}")
    ctx.close()
