"""Hard-match filters. Deterministic rules, no LLM involvement."""

from __future__ import annotations

import re
from dataclasses import dataclass

from offerbomber.discovery.base import RawJob, normalize_key
from offerbomber.discovery.http import SourceFetchError

# Titles that indicate a non-job post (aggregate/spam pages). Kept deliberately
# narrow to avoid discarding real roles at the broad-search stage.
_NON_JOB_TITLE = re.compile(
    r"(^|\b)(internship opportunities?|job fair|career fair|all jobs|open roles roundup)\b",
    re.IGNORECASE,
)

_CLOSED_HINT = re.compile(r"\b(position (is )?closed|role (is )?filled|no longer accepting)\b", re.I)


@dataclass(frozen=True)
class FilterDecision:
    passed: bool
    reason: str = ""


class HardFilter:
    def __init__(
        self,
        *,
        enabled: bool = True,
        broad_search: bool = True,
        target_fields: list[str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.broad_search = broad_search
        self.target_fields = [normalize_key(f) for f in (target_fields or []) if normalize_key(f)]

    def evaluate(self, job: RawJob) -> FilterDecision:
        if not self.enabled:
            return FilterDecision(True, "filter disabled")
        if _NON_JOB_TITLE.search(job.title):
            return FilterDecision(False, "non-job title")
        combined = normalize_key(f"{job.title} {job.description}")
        if _CLOSED_HINT.search(combined):
            return FilterDecision(False, "job appears closed")
        if not self.broad_search and self.target_fields:
            if not any(field in combined for field in self.target_fields):
                return FilterDecision(False, "no target-field overlap")
        return FilterDecision(True, "passed hard filters")
