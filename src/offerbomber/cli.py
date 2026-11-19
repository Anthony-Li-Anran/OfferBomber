"""OfferBomber command line interface (Phase 1 commands are fully implemented).

Later-phase commands are intentionally absent rather than stubbed, per task book
section 23: "no fake implementations".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import structlog
import typer
from sqlalchemy import select

from .app import AppContext
from .config import load_settings, write_default_config
from .core.discovery_service import DiscoveryService
from .storage.models import Company, Job

app = typer.Typer(
    name="offerbomber",
    help="Local-first personal job-hunting automation system",
    no_args_is_help=True,
    add_completion=False,
)
jobs_app = typer.Typer(help="Inspect discovered jobs")
app.add_typer(jobs_app, name="jobs")

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
    typer.echo(f"initialized config: {target}")
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
    typer.echo(f"  configured boards: {len(enabled)}")
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


@app.command()
def status(config: Path = typer.Option(None, "--config")) -> None:
    """Show the current system status and Phase 1 capability boundary."""
    ctx = _require_config(config)
    typer.echo("OfferBomber 0.1.0 — Phase 1 (discovery + persistence)")
    typer.echo("implemented: config, SQLite schema, Greenhouse/Lever/Ashby, dedup, hard filters, CLI")
    typer.echo("not implemented yet (Phases 2-7): profile KB, model adapters, resume, mail, reports, MCP")
    ctx.close()
