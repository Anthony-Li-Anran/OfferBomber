"""Discovery orchestration: fetch sources -> hard filter -> dedup -> persist.

Bounded by the configured company list; a failing source is isolated and
recorded, never aborting the whole run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy.orm import Session, sessionmaker

from offerbomber.config import CompanySource, Settings
from offerbomber.discovery.ashby import AshbySource
from offerbomber.discovery.base import DiscoverySource, RawJob
from offerbomber.discovery.greenhouse import GreenhouseSource
from offerbomber.discovery.http import HttpFetcher, SourceFetchError
from offerbomber.discovery.lever import LeverSource
from offerbomber.storage import repositories as repo

from .dedup import JobDeduplicator
from .policies import HardFilter

log = structlog.get_logger(__name__)

_SOURCE_CLASSES: dict[str, type[DiscoverySource]] = {
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
}
_UNSUPPORTED = {"official_careers", "search_engine", "xiaohongshu_public"}


@dataclass
class RunStats:
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    raw_jobs: int = 0
    filtered_out: int = 0
    new_jobs: int = 0
    duplicate_jobs: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sources_attempted": self.sources_attempted,
            "sources_succeeded": self.sources_succeeded,
            "sources_failed": self.sources_failed,
            "raw_jobs": self.raw_jobs,
            "filtered_out": self.filtered_out,
            "new_jobs": self.new_jobs,
            "duplicate_jobs": self.duplicate_jobs,
            "errors": self.errors,
        }


class DiscoveryService:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.fetcher = HttpFetcher()
        self.hard_filter = HardFilter(
            enabled=settings.matching.exclude_hard_mismatches,
            broad_search=settings.matching.broad_search,
            target_fields=settings.matching.target_fields,
        )

    async def aclose(self) -> None:
        await self.fetcher.aclose()

    async def run(self, companies: list[CompanySource] | None = None) -> RunStats:
        stats = RunStats()
        targets = companies if companies is not None else self.settings.discovery.companies
        with self.session_factory() as session:
            dedup = JobDeduplicator(session)
            for target in targets:
                source_cls = _SOURCE_CLASSES.get(target.platform)
                if source_cls is None:
                    if target.platform in _UNSUPPORTED:
                        log.info("source_not_implemented_in_phase1", platform=target.platform)
                        continue
                    stats.errors.append(f"unknown platform: {target.platform}")
                    stats.sources_failed += 1
                    continue

                stats.sources_attempted += 1
                source = source_cls(self.fetcher)
                company = repo.get_or_create_company(
                    session, target.name or target.token, is_seed=True
                )
                source_row = repo.get_or_create_source(
                    session, company, target.platform, target.token, source.build_url(target.token)
                )
                try:
                    raw_jobs = await source.fetch(target.token, target.name)
                except SourceFetchError as exc:
                    stats.sources_failed += 1
                    stats.errors.append(f"{target.platform}:{target.token}: {exc}")
                    source_row.last_error = str(exc)[:500]
                    source_row.updated_at = repo.utcnow()
                    log.warning("source_fetch_failed", platform=target.platform, token=target.token, error=str(exc))
                    continue

                stats.sources_succeeded += 1
                stats.raw_jobs += len(raw_jobs)
                for raw in raw_jobs:
                    decision = self.hard_filter.evaluate(raw)
                    if not decision.passed:
                        stats.filtered_out += 1
                        continue
                    result = dedup.find(raw)
                    if result.job is not None:
                        repo.add_source_record(session, result.job, raw)
                        repo.touch_job(session, result.job)
                        stats.duplicate_jobs += 1
                        continue
                    new_job = repo.insert_job(session, raw)
                    repo.add_source_record(session, new_job, raw)
                    dedup.add(new_job)
                    stats.new_jobs += 1
                log.info(
                    "source_fetched",
                    platform=target.platform,
                    token=target.token,
                    raw=len(raw_jobs),
                )
            session.commit()
        return stats


def build_source(platform: str, fetcher: HttpFetcher) -> DiscoverySource:
    cls = _SOURCE_CLASSES.get(platform)
    if cls is None:
        raise ValueError(f"unsupported platform: {platform}")
    return cls(fetcher)
