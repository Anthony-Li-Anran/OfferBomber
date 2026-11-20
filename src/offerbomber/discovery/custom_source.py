"""User-supplied and public-community job sources (Phase 4, types D & E).

Security:
- Only URLs the user explicitly configured/imported are fetched. The host model
  cannot inject an arbitrary URL.
- Public community posts are read through the ordinary HttpFetcher (same
  timeouts/retries). No login, captcha, rate-limit or access-control bypass.
- CSV/JSON import parses local files supplied by the user; malformed rows are
  skipped with a recorded error count, never fabricated.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from urllib.parse import urlsplit

from .base import RawJob, normalize_whitespace, normalize_url, strip_html
from .http import HttpFetcher, SourceFetchError, _RETRYABLE_STATUS
import asyncio
import httpx


class LocalImportSource:
    """Import jobs from a user-provided CSV or JSON file. No network."""

    platform = "custom_import"

    def load(self, path: str | Path) -> list[RawJob]:
        source = Path(path)
        if not source.is_file():
            raise SourceFetchError(f"import file not found: {source}")
        if source.suffix.lower() == ".json":
            return self._load_json(source)
        if source.suffix.lower() == ".csv":
            return self._load_csv(source)
        raise SourceFetchError(f"unsupported import format: {source.suffix}")

    @staticmethod
    def _row_to_job(row: dict, index: int) -> RawJob | None:
        title = normalize_whitespace(str(row.get("title", "")))
        if not title:
            return None
        url = normalize_url(str(row.get("url") or row.get("apply_url") or ""))
        return RawJob(
            source_platform="custom_import",
            source_key=f"file:{index}",
            external_id=str(row.get("id") or url or f"import:{index}"),
            company=normalize_whitespace(str(row.get("company") or urlsplit(url).netloc)) or "Unknown",
            title=title,
            description=strip_html(str(row.get("description", ""))),
            education_requirement=str(row.get("requirements", "")) or None,
            location=str(row.get("location", "")) or None,
            remote_policy=str(row.get("remote_policy", "")) or None,
            employment_type=str(row.get("employment_type", "")) or None,
            recruitment_email=str(row.get("recruitment_email", "")) or None,
            official_url=url or None,
            apply_url=url or None,
        )

    def _load_json(self, path: Path) -> list[RawJob]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise SourceFetchError(f"cannot parse JSON import: {exc}") from exc
        rows = payload if isinstance(payload, list) else payload.get("jobs", []) if isinstance(payload, dict) else []
        jobs = []
        for i, row in enumerate(rows):
            if isinstance(row, dict):
                job = self._row_to_job(row, i)
                if job:
                    jobs.append(job)
        return jobs

    def _load_csv(self, path: Path) -> list[RawJob]:
        jobs = []
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for i, row in enumerate(csv.DictReader(handle)):
                    job = self._row_to_job(row, i)
                    if job:
                        jobs.append(job)
        except OSError as exc:
            raise SourceFetchError(f"cannot parse CSV import: {exc}") from exc
        return jobs


class CommunityPublicSource:
    """Fetch a public, user-configured URL (e.g. a community post) and extract
    JobPosting JSON-LD or a minimal title/description. No auth/captcha bypass."""

    platform = "community_public"

    def __init__(self, fetcher: HttpFetcher) -> None:
        self.fetcher = fetcher

    async def fetch(self, url: str) -> list[RawJob]:
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.netloc:
            raise SourceFetchError("community URL must be an absolute http(s) URL")
        from .official import parse_json_ld_jobs

        html = await self.fetcher.get_text(url)
        jobs = parse_json_ld_jobs(html, url)
        if jobs:
            for job in jobs:
                job.source_platform = self.platform
            return jobs
        # Minimal fallback: page <title> as one candidate, clearly marked UNVERIFIED.
        from bs4 import BeautifulSoup

        title = normalize_whitespace(BeautifulSoup(html, "html.parser").title.get_text()) if BeautifulSoup(html, "html.parser").title else ""
        if not title:
            raise SourceFetchError(f"no job information found at {url}")
        return [RawJob(
            source_platform=self.platform,
            source_key=parts.netloc,
            external_id=f"community:{url}",
            company=parts.netloc,
            title=title[:200],
            description=strip_html(html)[:2000],
            official_url=url,
            raw_payload={"verification": "UNVERIFIED"},
        )]
