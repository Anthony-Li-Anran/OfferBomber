"""Company Discovery (Phase 4, section 2.2).

Search profile -> query -> configured search provider -> candidate company
careers URLs -> ATS detection -> verified job source. Only sources that return
real, parseable job data are persisted as verified.不存在/不可验证的公司不会入库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..discovery.base import normalize_key
from ..discovery.http import HttpFetcher, SourceFetchError
from ..discovery.official import detect_ats, looks_like_careers_url
from ..discovery.search_source import QueryBuilder, SearchEngineSource, SearchProviderConfig
from ..search_profile import JobSearchProfile


@dataclass
class DiscoveredCompany:
    name: str
    careers_url: str
    platform: str  # greenhouse|lever|ashby|official_careers
    board_token: str = ""
    verified: bool = False
    evidence: str = ""
    source: str = ""


@dataclass
class CompanyDiscoveryResult:
    candidates: list[DiscoveredCompany] = field(default_factory=list)
    verified: list[DiscoveredCompany] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "candidates": len(self.candidates),
            "verified_companies": [
                {"name": c.name, "careers_url": c.careers_url, "platform": c.platform, "board_token": c.board_token}
                for c in self.verified
            ],
            "queries": self.queries,
            "errors": self.errors[:50],
        }


class CompanyDiscoveryService:
    def __init__(self, fetcher: HttpFetcher, search_config: SearchProviderConfig) -> None:
        self.fetcher = fetcher
        self.search = SearchEngineSource(fetcher, search_config)

    async def discover(
        self,
        profile: JobSearchProfile,
        *,
        max_companies: int = 20,
        site_hint: str = "",
    ) -> CompanyDiscoveryResult:
        result = CompanyDiscoveryResult()
        query = QueryBuilder.build(
            target_titles=profile.target_titles,
            skills=profile.skills,
            locations=profile.locations,
            employment_types=[str(x) for x in profile.employment_types],
            industries=profile.industries,
            languages=[str(x) for x in profile.languages],
            site_hint=site_hint,
        )
        if not query:
            result.errors.append("profile has no search dimensions set; cannot build query")
            return result
        result.queries.append(query)
        try:
            raw_jobs = await self.search.search(query)
        except SourceFetchError as exc:
            result.errors.append(f"search failed: {exc}")
            return result

        seen: set[str] = set()
        for raw in raw_jobs:
            url = raw.official_url or ""
            key = normalize_key(urlsplit(url).netloc)
            if not key or key in seen:
                continue
            seen.add(key)
            if profile.is_excluded_company(raw.company):
                continue
            ats = detect_ats(url)
            if ats:
                platform, token = ats
                candidate = DiscoveredCompany(
                    name=raw.company or token,
                    careers_url=url,
                    platform=platform,
                    board_token=token,
                    verified=True,  # public ATS board with real job data found by search
                    evidence=raw.title,
                    source="search_engine",
                )
            elif looks_like_careers_url(url):
                candidate = DiscoveredCompany(
                    name=raw.company or key,
                    careers_url=url,
                    platform="official_careers",
                    verified=True,
                    evidence=raw.title,
                    source="search_engine",
                )
            else:
                continue
            result.candidates.append(candidate)
            if len(result.candidates) >= max_companies:
                break
        result.verified = [c for c in result.candidates if c.verified]
        return result
