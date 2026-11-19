"""Job deduplication: exact keys first, then near-duplicate detection.

Rules from task book 7.3: official ID, normalized URL, company, title, location,
description similarity. Multiple sources of the same real job collapse into one
primary Job row but keep every discovery source in job_source_records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from offerbomber.discovery.base import RawJob, normalize_key
from offerbomber.storage.models import Job

_DESCRIPTION_PREFIX_LEN = 1200
_SIMILARITY_THRESHOLD = 0.88


@dataclass(frozen=True)
class DedupResult:
    job: Job | None
    reason: str


class JobDeduplicator:
    """In-memory index over existing jobs to avoid O(n²) DB round-trips."""

    def __init__(self, session: Session, *, similarity_threshold: float = _SIMILARITY_THRESHOLD) -> None:
        self.threshold = similarity_threshold
        self._by_external: dict[tuple[str, str], Job] = {}
        self._by_url: dict[str, Job] = {}
        self._by_bucket: dict[tuple[str, str], list[Job]] = {}
        for job in session.execute(select(Job)).scalars().all():
            self._index(job)

    def _index(self, job: Job) -> None:
        self._by_external.setdefault((job.source_platform, job.external_id), job)
        if job.normalized_url:
            self._by_url.setdefault(job.normalized_url, job)
        bucket = (job.normalized_company, job.normalized_title)
        self._by_bucket.setdefault(bucket, []).append(job)

    def find(self, candidate: RawJob) -> DedupResult:
        external_key = (candidate.source_platform, candidate.external_id)
        if external_key in self._by_external:
            return DedupResult(self._by_external[external_key], "same platform external id")

        url = candidate.normalized_url
        if url and url in self._by_url:
            return DedupResult(self._by_url[url], "same normalized URL")

        bucket = (normalize_key(candidate.company), normalize_key(candidate.title))
        for existing in self._by_bucket.get(bucket, []):
            same_location = normalize_key(existing.location) == normalize_key(candidate.location)
            if not same_location:
                continue
            sim = SequenceMatcher(
                None,
                _prefix(existing.description or ""),
                _prefix(candidate.description),
            ).ratio()
            if sim >= self.threshold:
                return DedupResult(existing, f"same title/company, description similarity {sim:.2f}")

        return DedupResult(None, "new job")

    def add(self, job: Job) -> None:
        self._index(job)


def _prefix(text: str) -> str:
    return re.sub(r"\s+", " ", text)[:_DESCRIPTION_PREFIX_LEN]
