"""Centralized authorization for MCP fact/resource access.

A fact may enter a resume only when ALL of these hold:
  1. it exists;
  2. its owning Experience belongs to the current MCP user;
  3. verification_status == CONFIRMED;
  4. is_deleted is False.

Every tool that accepts fact IDs must go through :func:`load_authorized_facts`.
One bad ID fails the WHOLE request — no silent skipping. This module is the
single source of truth so the rule cannot drift between tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.resume_plan import CONFIRMED
from ...storage.models import Experience, ExperienceFact, ResumeVersion

MAX_FACTS_PER_REQUEST = 40


@dataclass(frozen=True)
class FactAuthError:
    code: str
    message: str
    fact_id: int | None = None


@dataclass(frozen=True)
class FactAuthResult:
    facts: list[ExperienceFact]
    experiences: dict[int, Experience]
    error: FactAuthError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _dedupe(fact_ids: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for value in fact_ids:
        try:
            fid = int(value)
        except (TypeError, ValueError):
            continue
        if fid in seen:
            continue
        seen.add(fid)
        ordered.append(fid)
    return ordered


def load_authorized_facts(
    session: Session,
    fact_ids: Iterable[int],
    user_id: str,
    *,
    limit: int = MAX_FACTS_PER_REQUEST,
) -> FactAuthResult:
    """Resolve fact IDs under the full authorization rule.

    Returns all facts when every ID is authorized, otherwise an error result.
    The caller MUST treat any error as a hard failure of the entire request.
    """
    ids = _dedupe(fact_ids)
    if not ids:
        return FactAuthResult([], {}, FactAuthError("empty_facts", "fact_ids must not be empty"))
    if len(ids) > limit:
        return FactAuthResult(
            [], {}, FactAuthError("too_many_facts", f"at most {limit} facts per request")
        )

    rows = session.execute(
        select(ExperienceFact, Experience)
        .join(Experience, Experience.id == ExperienceFact.experience_id)
        .where(ExperienceFact.id.in_(ids))
    ).all()
    by_id = {fact.id: (fact, exp) for fact, exp in rows}

    for fid in ids:
        pair = by_id.get(fid)
        if pair is None:
            return FactAuthResult([], {}, FactAuthError("fact_not_found", f"fact {fid} does not exist", fid))
        fact, exp = pair
        if exp.user_id != user_id:
            return FactAuthResult(
                [], {}, FactAuthError("fact_forbidden", f"fact {fid} belongs to another user", fid)
            )
        if fact.is_deleted:
            return FactAuthResult(
                [], {}, FactAuthError("fact_deleted", f"fact {fid} is deleted", fid)
            )
        if fact.verification_status != CONFIRMED:
            return FactAuthResult(
                [], {}, FactAuthError("fact_unconfirmed", f"fact {fid} is not CONFIRMED", fid)
            )

    facts = [by_id[fid][0] for fid in ids]
    experiences = {exp.id: exp for _, exp in (by_id[fid] for fid in ids)}
    return FactAuthResult(facts, experiences)


def load_authorized_version(
    session: Session, version_id: int, user_id: str
) -> tuple[ResumeVersion | None, FactAuthError | None]:
    """A ResumeVersion is visible only to its owning user."""
    version = session.get(ResumeVersion, version_id)
    if version is None:
        return None, FactAuthError("version_not_found", f"resume version {version_id} not found")
    version_user = getattr(version, "user_id", None)
    if version_user is not None and version_user != user_id:
        return None, FactAuthError("version_forbidden", "resume version belongs to another user")
    return version, None
