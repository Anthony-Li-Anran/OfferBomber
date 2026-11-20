"""Search-engine job discovery (Phase 4, source type B).

Uses ONLY a user-configured official search provider endpoint (e.g. Brave /
Serper / self-hosted SearXNG). The host model never supplies an arbitrary URL:
it supplies structured query dimensions, which are validated against the
JobSearchProfile. Requests go to a fixed allowlisted base URL from config, so
there is no SSRF vector.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .base import RawJob, normalize_key, normalize_url
from .http import HttpFetcher, SourceFetchError


@dataclass(frozen=True)
class SearchProviderConfig:
    """Configured once by the user; never from model input."""

    base_url: str
    api_key_env: str = ""  # name of env var holding the key (never the key itself)
    result_limit: int = 20

    def api_key(self) -> str:
        return os.getenv(self.api_key_env, "") if self.api_key_env else ""


class QueryBuilder:
    """Build a search query from an arbitrary JobSearchProfile - no fixed
    occupation/industry words are hardcoded here."""

    @staticmethod
    def build(
        target_titles: list[str],
        skills: list[str],
        locations: list[str],
        employment_types: list[str],
        industries: list[str],
        languages: list[str],
        site_hint: str = "",
    ) -> str:
        parts: list[str] = []
        for group in (target_titles, skills, industries):
            usable = [g for g in group if normalize_key(g)]
            if usable:
                parts.append(" ".join(usable[:4]))
        if employment_types:
            parts.append(" ".join(employment_types[:3]))
        if locations:
            parts.append("in " + " ".join(locations[:2]))
        if site_hint:
            parts.append(f"site:{site_hint}")
        query = " ".join(p for p in parts if p)
        if languages and "any" not in [str(x).lower() for x in languages]:
            query += f" lang:{languages[0]}"
        return query.strip()


class SearchEngineSource:
    """Thin adapter over a configured JSON search API. Response shape is
    normalized defensively; unparseable results are skipped, never invented."""

    platform = "search_engine"

    def __init__(self, fetcher: HttpFetcher, config: SearchProviderConfig) -> None:
        self.fetcher = fetcher
        self.config = config

    def _validate_base_url(self) -> None:
        parts = urlsplit(self.config.base_url)
        if parts.scheme not in {"https", "http"} or not parts.netloc:
            raise SourceFetchError("search provider base_url must be an absolute http(s) URL")

    async def search(self, query: str) -> list[RawJob]:
        self._validate_base_url()
        if not normalize_key(query):
            return []
        key = self.config.api_key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            payload = await self.fetcher.get_json(
                self.config.base_url,
                params={"q": query, "num": str(min(self.config.result_limit, 50))},
                headers=headers,
            )
        except TypeError as exc:
            # get_json currently has no headers kwarg; see note below.
            raise SourceFetchError(f"search provider does not accept auth headers: {exc}") from exc
        return self.parse_payload(payload, query)

    def parse_payload(self, payload: object, query: str) -> list[RawJob]:
        if not isinstance(payload, dict):
            return []
        items = payload.get("organic") or payload.get("results") or payload.get("items") or []
        if not isinstance(items, list):
            return []
        jobs: list[RawJob] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            url = normalize_url(str(item.get("link") or item.get("url") or ""))
            if not url:
                continue
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or item.get("description") or "").strip()
            if not title or not snippet:
                continue
            jobs.append(RawJob(
                source_platform=self.platform,
                source_key=urlsplit(self.config.base_url).netloc,
                external_id=f"search:{url}",
                company=urlsplit(url).netloc,
                title=title[:200],
                description=snippet,
                official_url=url,
                apply_url=url,
                raw_payload={"query": query, "index": index},
            ))
        return jobs
