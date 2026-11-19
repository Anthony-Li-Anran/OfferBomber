"""HTTP client with explicit timeout, bounded retry, exponential backoff + jitter.

All external calls go through this module. A failed source never crashes the
whole discovery run — callers receive a SourceFetchError and record it.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import httpx


class SourceFetchError(RuntimeError):
    """Raised after all bounded retries for one source are exhausted."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.25


_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpFetcher:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        connect_timeout: float = 8.0,
        retry: RetryPolicy | None = None,
        user_agent: str = "OfferBomber/0.1 (+local personal use)",
    ) -> None:
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self._retry = retry or RetryPolicy()
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._client = httpx.AsyncClient(timeout=self._timeout, headers=self._headers, follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(self, url: str, params: dict[str, str] | None = None) -> object:
        """GET *url* and return decoded JSON. Raises SourceFetchError on failure."""
        last_error = ""
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                response = await self._client.get(url, params=params)
                if response.status_code in _RETRYABLE_STATUS:
                    last_error = f"HTTP {response.status_code} from {url}"
                elif response.status_code >= 400:
                    # 4xx other than 408/425/429 is a client error: do not retry.
                    raise SourceFetchError(f"HTTP {response.status_code} from {url}")
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise SourceFetchError(f"invalid JSON from {url}: {exc}") from exc
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"

            if attempt < self._retry.max_attempts:
                await asyncio.sleep(self._backoff(attempt))

        raise SourceFetchError(
            f"failed after {self._retry.max_attempts} attempts for {url}: {last_error}"
        )

    def _backoff(self, attempt: int) -> float:
        delay = min(self._retry.base_delay * (2 ** (attempt - 1)), self._retry.max_delay)
        return delay + random.uniform(0, self._retry.jitter)
