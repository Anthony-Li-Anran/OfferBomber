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
    sources_partial: int = 0
    sources_failed: int = 0
    raw_jobs: int = 0
    filtered_out: int = 0
    new_jobs: int = 0
    duplicate_jobs: int = 0
    record_errors: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sources_attempted": self.sources_attempted,
            "sources_succeeded": self.sources_succeeded,
            "sources_partial": self.sources_partial,
            "sources_failed": self.sources_failed,
            "raw_jobs": self.raw_jobs,
            "filtered_out": self.filtered_out,
            "new_jobs": self.new_jobs,
            "duplicate_jobs": self.duplicate_jobs,
            "record_errors": self.record_errors,
            "fully_succeeded": self.sources_failed == 0 and self.sources_partial == 0,
            "errors": self.errors,
        }

    def _error(self, message: str) -> None:
        if len(self.errors) < 100:
            self.errors.append(message)


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
                        log.info(
                            "source_not_implemented",
                            platform=target.platform,
                            note="configuration only; no collector ships in this release",
                        )
                        continue
                    stats.sources_failed += 1
                    stats._error(f"unknown platform: {target.platform}")
                    log.warning("source_unknown", platform=target.platform)
                    continue

                stats.sources_attempted += 1
                label = f"{target.platform}:{target.token}"
                source = source_cls(self.fetcher)
                try:
                    company = repo.get_or_create_company(
                        session, target.name or target.token, is_seed=True
                    )
                    source_row = repo.get_or_create_source(
                        session, company, target.platform, target.token, source.build_url(target.token)
                    )
                    raw_jobs = await source.fetch(target.token, target.name)
                except Exception as exc:
                    stats.sources_failed += 1
                    stats._error(f"{label}: {type(exc).__name__}: {exc}")
                    log.warning(
                        "source_failed",
                        platform=target.platform,
                        token=target.token,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    session.rollback()
                    try:
                        repo.record_source_error(
                            session, target.platform, target.token, str(exc)
                        )
                    except Exception as log_exc:
                        log.warning("source_error_persist_failed", error=str(log_exc))
                        session.rollback()
                    dedup = JobDeduplicator(session)
                    continue

                stats.raw_jobs += len(raw_jobs)
                source_errors = 0
                for index, raw in enumerate(raw_jobs):
                    try:
                        decision = self.hard_filter.evaluate(raw)
                        if not decision.passed:
                            stats.filtered_out += 1
                            continue
                        # SAVEPOINT keeps one malformed record from rolling back
                        # the other records of the same source.
                        with session.begin_nested():
                            result = dedup.find(raw)
                            if result.job is not None:
                                repo.add_source_record(session, result.job, raw)
                                repo.touch_job(session, result.job)
                                stats.duplicate_jobs += 1
                            else:
                                new_job = repo.insert_job(session, raw)
                                repo.add_source_record(session, new_job, raw)
                                dedup.add(new_job)
                                stats.new_jobs += 1
                    except Exception as exc:
                        source_errors += 1
                        stats.record_errors += 1
                        detail = (
                            f"{label} record[{index}] "
                            f"{getattr(raw, 'external_id', '?')}: {type(exc).__name__}: {exc}"
                        )
                        stats._error(detail)
                        log.warning(
                            "job_record_failed",
                            platform=target.platform,
                            token=target.token,
                            record_index=index,
                            external_id=getattr(raw, "external_id", None),
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )

                # One commit per source: a later database failure cannot undo an
                # earlier source's successfully persisted records.
                try:
                    session.commit()
                except Exception as exc:
                    stats.sources_failed += 1
                    stats.record_errors += source_errors
                    stats._error(f"{label} commit failed: {type(exc).__name__}: {exc}")
                    log.warning(
                        "source_commit_failed",
                        platform=target.platform,
                        token=target.token,
                        error=str(exc),
                    )
                    session.rollback()
                    dedup = JobDeduplicator(session)
                    continue

                if source_errors:
                    stats.sources_partial += 1
                    stats._error(f"{label}: {source_errors} record(s) failed")
                else:
                    stats.sources_succeeded += 1
                log.info(
                    "source_processed",
                    platform=target.platform,
                    token=target.token,
                    raw=len(raw_jobs),
                    new=stats.new_jobs,
                    record_errors=source_errors,
                    partial=source_errors > 0,
                )
        log.info(
            "discovery_run_finished",
            fully_succeeded=stats.as_dict()["fully_succeeded"],
            **{k: v for k, v in stats.as_dict().items() if k not in {"fully_succeeded", "errors"}},
        )
        return stats


def build_source(platform: str, fetcher: HttpFetcher) -> DiscoverySource:
    cls = _SOURCE_CLASSES.get(platform)
    if cls is None:
        raise ValueError(f"unsupported platform: {platform}")
    return cls(fetcher)
