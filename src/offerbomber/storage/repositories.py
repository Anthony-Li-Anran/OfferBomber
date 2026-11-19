"""Persistence helpers for Phase 1 discovery output."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from offerbomber.discovery.base import RawJob
from offerbomber.storage.models import Company, Job, JobSource, JobSourceRecord


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_or_create_company(session: Session, name: str, *, is_seed: bool = False) -> Company:
    company = session.execute(select(Company).where(Company.name == name)).scalar_one_or_none()
    if company is None:
        company = Company(name=name, is_seed=is_seed, created_at=utcnow(), updated_at=utcnow())
        session.add(company)
        session.flush()
    return company


def get_or_create_source(
    session: Session,
    company: Company,
    platform: str,
    token: str,
    board_url: str | None,
) -> JobSource:
    source = session.execute(
        select(JobSource).where(
            JobSource.platform == platform, JobSource.board_token == token
        )
    ).scalar_one_or_none()
    if source is None:
        source = JobSource(
            company_id=company.id,
            platform=platform,
            board_token=token,
            board_url=board_url,
            enabled=True,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(source)
        session.flush()
    source.last_scanned_at = utcnow()
    source.last_error = None
    return source


def insert_job(session: Session, raw: RawJob, *, validity_status: str = "LIKELY_ACTIVE") -> Job:
    now = utcnow()
    job = Job(
        external_id=raw.external_id,
        company=raw.company,
        normalized_company=_norm(raw.company),
        title=raw.title,
        normalized_title=_norm(raw.title),
        location=raw.location,
        remote_policy=raw.remote_policy,
        employment_type=raw.employment_type,
        description=raw.description,
        education_requirement=raw.education_requirement,
        internship_duration=raw.internship_duration,
        published_at=raw.published_at.replace(tzinfo=None) if raw.published_at else None,
        discovered_at=now,
        last_verified_at=now,
        source_platform=raw.source_platform,
        official_url=raw.official_url,
        normalized_url=raw.normalized_url or None,
        apply_url=raw.apply_url,
        apply_method="official_form" if raw.apply_url else "unknown",
        recruitment_email=raw.recruitment_email,
        email_source_evidence=raw.email_source_evidence,
        validity_status=validity_status,
        fingerprint=raw.fingerprint,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    return job


def add_source_record(session: Session, job: Job, raw: RawJob) -> None:
    url = raw.official_url or raw.apply_url or ""
    if not url:
        return
    exists = session.execute(
        select(JobSourceRecord).where(
            JobSourceRecord.job_id == job.id,
            JobSourceRecord.source_platform == raw.source_platform,
            JobSourceRecord.source_url == url,
        )
    ).scalar_one_or_none()
    if exists:
        exists.seen_at = utcnow()
        exists.updated_at = utcnow()
        return
    session.add(
        JobSourceRecord(
            job_id=job.id,
            source_platform=raw.source_platform,
            source_url=url,
            raw_external_id=raw.external_id,
            seen_at=utcnow(),
            created_at=utcnow(),
            updated_at=utcnow(),
        )
    )


def touch_job(session: Session, job: Job) -> None:
    job.last_verified_at = utcnow()
    job.updated_at = utcnow()


def count_jobs(session: Session) -> int:
    return int(session.execute(select(func.count(Job.id))).scalar_one())


def _norm(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
