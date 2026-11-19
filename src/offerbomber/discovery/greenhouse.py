"""Greenhouse public job board connector.

Verified endpoint (official docs, 2026-11-19):
    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
Public read — no auth. Rate limiting is possible.
"""

from __future__ import annotations

from datetime import datetime

from .base import DiscoverySource, RawJob, normalize_whitespace, strip_html


class GreenhouseSource(DiscoverySource):
    platform = "greenhouse"

    def build_url(self, token: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    def parse_payload(self, payload: object, token: str, company_name: str | None) -> list[RawJob]:
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise ValueError(f"unexpected Greenhouse payload shape for token={token}")
        jobs: list[RawJob] = []
        for item in payload["jobs"]:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            description = strip_html(str(item.get("content") or ""))
            title = normalize_whitespace(str(item.get("title") or ""))
            if not title:
                continue
            location_obj = item.get("location")
            location = (
                normalize_whitespace(str(location_obj.get("name") or ""))
                if isinstance(location_obj, dict)
                else None
            )
            published = _parse_iso(item.get("updated_at"))
            absolute_url = str(item.get("absolute_url") or "").strip() or None
            jobs.append(
                RawJob(
                    source_platform=self.platform,
                    source_key=token,
                    external_id=str(item["id"]),
                    company=company_name or token,
                    title=title,
                    description=description,
                    location=location or None,
                    published_at=published,
                    official_url=absolute_url,
                    apply_url=absolute_url,
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
