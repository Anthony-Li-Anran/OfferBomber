"""Lever Postings API connector.

Verified endpoint (official repo docs, 2026-11-19):
    GET https://api.lever.co/v0/postings/{site}?mode=json
Public for published postings — no auth. HTTPS only.

Field names come from the official doc: id, text, categories{location,commitment,
team,department,allLocations}, descriptionPlain, hostedUrl, applyUrl,
workplaceType, country, lists. NOTE: the public list response has NO createdAt
field, so published_at is left None rather than fabricated.
"""

from __future__ import annotations

from .base import DiscoverySource, RawJob, normalize_whitespace, strip_html


class LeverSource(DiscoverySource):
    platform = "lever"

    def build_url(self, token: str) -> str:
        return f"https://api.lever.co/v0/postings/{token}?mode=json"

    def parse_payload(self, payload: object, token: str, company_name: str | None) -> list[RawJob]:
        if not isinstance(payload, list):
            raise ValueError(f"unexpected Lever payload shape for token={token}")
        jobs: list[RawJob] = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            title = normalize_whitespace(str(item.get("text") or ""))
            if not title:
                continue
            categories = item.get("categories")
            categories = categories if isinstance(categories, dict) else {}
            location = normalize_whitespace(str(categories.get("location") or "")) or None
            commitment = normalize_whitespace(str(categories.get("commitment") or "")) or None
            workplace = str(item.get("workplaceType") or "").strip().lower() or None
            description = normalize_whitespace(
                strip_html(str(item.get("descriptionPlain") or ""))
                or strip_html(str(item.get("description") or ""))
            )
            jobs.append(
                RawJob(
                    source_platform=self.platform,
                    source_key=token,
                    external_id=str(item["id"]),
                    company=company_name or token,
                    title=title,
                    description=description,
                    location=location,
                    remote_policy=workplace,
                    employment_type=commitment,
                    official_url=str(item.get("hostedUrl") or "").strip() or None,
                    apply_url=str(item.get("applyUrl") or "").strip() or None,
                    raw_payload=item,
                )
            )
        return jobs
