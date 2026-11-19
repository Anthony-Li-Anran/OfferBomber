"""Normalized job representation and discovery source contract."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .http import HttpFetcher, SourceFetchError


def strip_html(value: str | None) -> str:
    """Convert HTML to readable plain text; returns '' for empty input."""
    if not value:
        return ""
    return BeautifulSoup(value, "html.parser").get_text(separator=" ", strip=True)


def normalize_whitespace(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "ref", "source")


def normalize_url(value: str | None) -> str:
    """Canonicalize a URL for dedup: lowercase host, drop tracking params/fragment."""
    if not value:
        return ""
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return value.strip().rstrip("/")
    host = parts.netloc.lower()
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(sorted(query)), ""))


def normalize_key(value: str | None) -> str:
    """Unicode-safe key normalization for dedup and filters.

    NFKC folds width/compatibility variants, casefold handles non-ASCII case,
    punctuation becomes a separator, and CJK characters are preserved. Whitespace
    *inside a CJK run* is removed so "字节 跳动" matches "字节跳动"; English word
    boundaries and distinct Chinese names are never collapsed.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    parts: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category[0] in {"L", "M", "N"}:
            parts.append(char)
        else:
            parts.append(" ")
    collapsed = re.sub(r"\s+", " ", "".join(parts)).strip()
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", collapsed)


def sha256_of(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(p or "" for p in parts).encode("utf-8")).hexdigest()


@dataclass
class RawJob:
    """Platform-agnostic normalized job produced by a discovery source."""

    source_platform: str
    source_key: str
    external_id: str
    company: str
    title: str
    description: str
    location: str | None = None
    remote_policy: str | None = None
    employment_type: str | None = None
    education_requirement: str | None = None
    internship_duration: str | None = None
    published_at: datetime | None = None
    official_url: str | None = None
    apply_url: str | None = None
    recruitment_email: str | None = None
    email_source_evidence: str | None = None
    raw_payload: dict = field(default_factory=dict, repr=False)

    @property
    def normalized_url(self) -> str:
        return normalize_url(self.official_url or self.apply_url)

    @property
    def fingerprint(self) -> str:
        return sha256_of(
            self.source_platform,
            self.source_key,
            self.external_id,
            normalize_key(self.company),
            normalize_key(self.title),
            normalize_key(self.location),
        )


class DiscoverySource:
    """Base class. Subclasses implement parse_payload(); network is handled here."""

    platform: str = "base"

    def __init__(self, fetcher: HttpFetcher) -> None:
        self.fetcher = fetcher

    def build_url(self, token: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def parse_payload(self, payload: object, token: str, company_name: str | None) -> list[RawJob]:  # pragma: no cover
        raise NotImplementedError

    async def fetch(self, token: str, company_name: str | None = None) -> list[RawJob]:
        url = self.build_url(token)
        try:
            payload = await self.fetcher.get_json(url)
        except SourceFetchError:
            raise
        except httpx.HTTPError as exc:  # defensive: HttpFetcher normally wraps this
            raise SourceFetchError(f"{self.platform} network error: {exc}") from exc
        try:
            return self.parse_payload(payload, token, company_name)
        except SourceFetchError:
            raise
        except Exception as exc:
            # An unexpected response shape must not abort the whole discovery run.
            raise SourceFetchError(
                f"{self.platform} payload parse error for {token}: {type(exc).__name__}: {exc}"
            ) from exc
