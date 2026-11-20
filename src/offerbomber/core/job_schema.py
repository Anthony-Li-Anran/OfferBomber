"""Universal normalized job schema (Phase 4, section 2.3).

Every source maps into this single model. Unknown fields stay None - salary,
location and published_at are NEVER guessed. This replaces any implicit
personal/premium-job assumptions with a source-agnostic contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..discovery.base import RawJob, normalize_key, normalize_url


class NormalizedJob(BaseModel):
    job_id: str = ""
    company: str = ""
    company_website: str | None = None
    title: str = ""
    description: str | None = None
    requirements: str | None = None
    industry: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    location: str | None = None
    remote_policy: str | None = None
    salary: str | None = None
    currency: str | None = None
    language: str | None = None
    published_at: datetime | None = None
    deadline: datetime | None = None
    source: str = ""
    source_url: str | None = None
    official_apply_url: str | None = None
    recruitment_email: str | None = None
    apply_method: str | None = None
    verification_status: str = "UNKNOWN"  # VERIFIED | LIKELY | UNKNOWN
    last_checked_at: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


def from_raw(raw: RawJob) -> NormalizedJob:
    """Deterministic mapping. No enrichment, no invention."""
    url = normalize_url(raw.official_url or raw.apply_url or "")
    host = url.split("/")[2] if url.startswith("http") and "/" in url[8:] else ""
    apply_method = "EMAIL" if getattr(raw, "recruitment_email", None) else (
        "OFFICIAL_FORM" if raw.apply_url or raw.official_url else "UNKNOWN"
    )
    verification = "LIKELY" if raw.official_url else "UNKNOWN"
    return NormalizedJob(
        job_id=raw.external_id,
        company=raw.company,
        company_website=f"https://{host}" if host else None,
        title=raw.title,
        description=raw.description or None,
        requirements=raw.education_requirement or None,
        employment_type=raw.employment_type,
        location=raw.location,
        remote_policy=raw.remote_policy,
        published_at=raw.published_at,
        source=raw.source_platform,
        source_url=url or None,
        official_apply_url=normalize_url(raw.apply_url or "") or None,
        recruitment_email=raw.recruitment_email,
        apply_method=apply_method,
        verification_status=verification,
        last_checked_at=raw.published_at,  # best-effort; callers may override with now
        extra={"fingerprint": raw.fingerprint, "normalized_title": normalize_key(raw.title)},
    )


def dedup_key(job: NormalizedJob) -> str:
    return normalize_key(f"{job.company}|{job.title}|{job.location or ''}")
