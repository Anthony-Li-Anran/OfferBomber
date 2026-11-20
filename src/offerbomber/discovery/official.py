"""Official careers-site discovery (Phase 4, source type A).

Two paths, both public and non-bypassing:
1. ATS JSON APIs published by the site (Greenhouse/Lever/Ashby public boards).
2. Structured JobPosting JSON-LD embedded in a public careers page.

A company is only accepted when a career entry can be verified (HTTP 200 with
parseable job data). No fabricated companies/jobs are ever produced.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import DiscoverySource, RawJob, normalize_key, normalize_whitespace, strip_html
from .http import HttpFetcher, SourceFetchError

# Well-known public ATS board patterns. Detection is by URL shape only; it
# never bypasses auth/captcha and never probes arbitrary hosts from model input.
_ATS_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("greenhouse", re.compile(r"boards\.greenhouse\.io/([^/]+)", re.I), "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"),
    ("lever", re.compile(r"jobs\.lever\.co/([^/]+)", re.I), "https://api.lever.co/v0/postings/{token}?mode=json"),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/]+)", re.I), "https://api.ashbyhq.com/posting-api/job-board/{token}"),
]

_CAREERS_HINTS = ("careers", "jobs", "join-us", "joinus", "recruit", "hiring", "工作", "招聘", "加入我们")


def detect_ats(url: str) -> tuple[str, str] | None:
    """Return (platform, board_token) when the URL is a known public ATS board."""
    for platform, pattern, _ in _ATS_PATTERNS:
        match = pattern.search(url or "")
        if match:
            return platform, match.group(1)
    return None


def ats_api_url(platform: str, token: str) -> str:
    for name, _, template in _ATS_PATTERNS:
        if name == platform:
            return template.format(token=token)
    raise ValueError(f"unsupported ATS platform: {platform}")


def looks_like_careers_url(url: str) -> bool:
    path = (urlsplit(url).path or "").lower()
    return any(hint in path for hint in _CAREERS_HINTS)


def parse_json_ld_jobs(html: str, base_url: str) -> list[RawJob]:
    """Parse embedded JobPosting JSON-LD from a public careers HTML page."""
    soup = BeautifulSoup(html or "", "html.parser")
    jobs: list[RawJob] = []
    seen: set[str] = set()
    for tag in soup.find_all("script", type=lambda t: t and "ld+json" in t.lower()):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph", [node])
            for item in graph:
                if not isinstance(item, dict):
                    continue
                if normalize_key(str(item.get("@type", ""))) not in {"jobposting", "job posting"}:
                    continue
                title = normalize_whitespace(str(item.get("title", "")))
                if not title:
                    continue
                external = str(item.get("identifier") or item.get("url") or title)
                if external in seen:
                    continue
                seen.add(external)
                org = item.get("hiringOrganization")
                company = ""
                if isinstance(org, dict):
                    company = normalize_whitespace(str(org.get("name", "")))
                apply_url = str(item.get("url") or "")
                if apply_url.startswith("/"):
                    apply_url = urljoin(base_url, apply_url)
                desc = strip_html(str(item.get("description", "")))
                location = ""
                place = item.get("jobLocation")
                if isinstance(place, list) and place:
                    place = place[0]
                if isinstance(place, dict):
                    address = place.get("address")
                    if isinstance(address, dict):
                        bits = [str(address.get(k, "")) for k in ("addressLocality", "addressRegion", "addressCountry") if address.get(k)]
                        location = ", ".join(bits)
                jobs.append(RawJob(
                    source_platform="official_careers",
                    source_key=urlsplit(base_url).netloc,
                    external_id=f"jsonld:{external}",
                    company=company or urlsplit(base_url).netloc,
                    title=title,
                    description=desc,
                    location=location or None,
                    official_url=apply_url or base_url,
                    apply_url=apply_url or None,
                ))
    return jobs


class OfficialCareersSource(DiscoverySource):
    """Fetch one verified public careers URL. Model input is a URL from the
    user's configured source list only - never a free-form host."""

    platform = "official_careers"

    def __init__(self, fetcher: HttpFetcher) -> None:
        super().__init__(fetcher)
        self._html_fetcher = fetcher

    def build_url(self, token: str) -> str:
        return token  # token IS the configured, verified careers URL

    async def fetch(self, url: str, company_name: str | None = None) -> list[RawJob]:
        ats = detect_ats(url)
        if ats:
            platform, board_token = ats
            from .greenhouse import GreenhouseSource
            from .lever import LeverSource
            from .ashby import AshbySource

            classes = {"greenhouse": GreenhouseSource, "lever": LeverSource, "ashby": AshbySource}
            source = classes[platform](self.fetcher)
            jobs = await source.fetch(board_token, company_name)
            for job in jobs:
                job.source_platform = "official_careers"
            return jobs
        if not looks_like_careers_url(url):
            raise SourceFetchError(f"URL does not look like a public careers page: {url}")
        html = await self._html_fetcher.get_text(url)
        jobs = parse_json_ld_jobs(html, url)
        if not jobs:
            raise SourceFetchError(f"no structured JobPosting data found at {url}")
        return jobs
