"""Ashby Public Job Posting API connector.

Verified endpoint (official docs, updated ~2026-10):
    GET https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true
Public — no auth. Response fields: title, location, department, team,
isListed, isRemote, workplaceType, descriptionHtml, descriptionPlain,
publishedAt, employmentType, jobUrl, applyUrl, address, compensation.

NOTE: this API has no job `id` field. A stable ID is derived from the canonical
job URL (fallback: title+location+publishedAt), not fabricated as an API field.
"""

from __future__ import annotations

from datetime import datetime

from .base import DiscoverySource, RawJob, normalize_whitespace, normalize_url, sha256_of, strip_html


class AshbySource(DiscoverySource):
    platform = "ashby"

    def build_url(self, token: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"

    def parse_payload(self, payload: object, token: str, company_name: str | None) -> list[RawJob]:
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError(f"unexpected Ashby payload shape for token={token}")
        jobs: list[RawJob] = []
        for item in payload["jobs"]:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            if item.get("isListed") is False:
                continue
            title = normalize_whitespace(str(item.get("title") or ""))
            job_url = str(item.get("jobUrl") or "").strip() or None
            canonical = normalize_url(job_url)
            published_raw = item.get("publishedAt")
            published = _parse_iso(published_raw)
            external_id = (
                canonical
                if canonical
                else sha256_of(
                    token, title, str(item.get("location") or ""), str(published_raw or "")
                )
            )
            description = normalize_whitespace(
                strip_html(str(item.get("descriptionPlain") or ""))
                or strip_html(str(item.get("descriptionHtml") or ""))
            )
            jobs.append(
                RawJob(
                    source_platform=self.platform,
                    source_key=token,
                    external_id=external_id,
                    company=company_name or token,
                    title=title,
                    description=description,
                    location=normalize_whitespace(str(item.get("location") or "")) or None,
                    remote_policy=normalize_whitespace(str(item.get("workplaceType") or "")).lower() or None,
                    employment_type=normalize_whitespace(str(item.get("employmentType") or "")).lower()
                    or None,
                    published_at=published,
                    official_url=job_url,
                    apply_url=str(item.get("applyUrl") or "").strip() or None,
                    raw_payload=item,
                )
            )
        return jobs


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
