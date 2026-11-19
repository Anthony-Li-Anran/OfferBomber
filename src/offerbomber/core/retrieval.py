"""Fact-level retrieval: structured tags + SQLite FTS5/BM25 with a Chinese fallback.

The result always carries explicit Experience ID and Fact ID. A reranker hook is
reserved for future semantic models but is unused by default.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ..storage.fts import FTS_TABLE, cjk_bigrams, fts_available
from ..storage.models import Experience, ExperienceFact, ExperienceSource

_CJK = re.compile(r"[\u3400-\u9fff]+")
_TOKEN = re.compile(r"[A-Za-z0-9_+#.]+|[\u3400-\u9fff]+")


@dataclass
class RetrievalQuery:
    text: str = ""
    required_skills: list[str] = field(default_factory=list)
    direction: str | None = None
    max_results: int = 20
    verified_only: bool = False


@dataclass(frozen=True)
class FactMatch:
    experience_id: int
    fact_id: int
    experience_title: str
    experience_type: str
    fact_content: str
    category: str | None
    skills: tuple[str, ...]
    source_type: str | None
    source_locator: str | None
    file_sha256: str | None
    verification_status: str
    score: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "experience_id": self.experience_id,
            "fact_id": self.fact_id,
            "experience_title": self.experience_title,
            "experience_type": self.experience_type,
            "fact_content": self.fact_content,
            "category": self.category,
            "skills": list(self.skills),
            "source": {
                "type": self.source_type,
                "locator": self.source_locator,
                "file_sha256": self.file_sha256,
            },
            "verification_status": self.verification_status,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
        }


class Reranker(Protocol):
    def rerank(self, query: RetrievalQuery, matches: list[FactMatch]) -> list[FactMatch]: ...


def fts_expression(query: str) -> str:
    """Build an OR expression; CJK runs and their bigrams cover missing segmentation."""
    terms: list[str] = []
    for match in _TOKEN.finditer(query or ""):
        token = match.group(0)
        if _CJK.fullmatch(token):
            terms.append(f'"{token}"')
            terms.extend(f'"{bigram}"' for bigram in cjk_bigrams(token))
        else:
            safe = token.replace('"', "")
            if safe:
                terms.append(f'"{safe}"*')
    return " OR ".join(dict.fromkeys(terms))[:2000]


class FactRetrievalService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        user_id: str = "default",
        *,
        reranker: Reranker | None = None,
    ) -> None:
        self.sessions = sessions
        self.user_id = user_id
        self.reranker = reranker

    def search(self, query: RetrievalQuery) -> list[FactMatch]:
        with self.sessions() as session:
            rows = session.execute(
                select(ExperienceFact, Experience, ExperienceSource)
                .join(Experience, Experience.id == ExperienceFact.experience_id)
                .outerjoin(ExperienceSource, ExperienceSource.id == ExperienceFact.source_id)
                .where(
                    Experience.user_id == self.user_id,
                    ExperienceFact.is_deleted.is_(False),
                    Experience.status != "ARCHIVED",
                )
            ).all()
            if not rows:
                return []

            bm25 = self._bm25_scores(session, query)
            results: list[FactMatch] = []
            for fact, experience, source in rows:
                if query.verified_only and fact.verification_status != "CONFIRMED":
                    continue
                skills = _parse_json_list(fact.skills)
                reasons: list[str] = []
                score = 0.0

                for required in query.required_skills:
                    needle = required.casefold().strip()
                    if not needle:
                        continue
                    if any(needle == skill.casefold() for skill in skills):
                        score += 4.0
                        reasons.append(f"exact skill tag: {required}")
                    elif any(needle in skill.casefold() for skill in skills):
                        score += 1.5
                        reasons.append(f"related skill tag: {required}")

                haystack = " ".join(
                    [fact.content, fact.category or "", experience.title, experience.description or ""]
                ).casefold()
                for token in self._tokens(query.text):
                    if token.casefold() in haystack:
                        score += 2.0
                        reasons.append(f"keyword: {token}")

                if query.direction and query.direction.casefold() in haystack:
                    score += 1.0
                    reasons.append("direction overlap")

                bm25_score = bm25.get(fact.id)
                if bm25_score is not None:
                    score += min(3.0, max(0.0, bm25_score))
                    if bm25_score > 0:
                        reasons.append(f"BM25 relevance: {bm25_score:.2f}")

                if score <= 0:
                    continue
                results.append(
                    FactMatch(
                        experience_id=experience.id,
                        fact_id=fact.id,
                        experience_title=experience.title,
                        experience_type=experience.type,
                        fact_content=fact.content,
                        category=fact.category,
                        skills=tuple(skills),
                        source_type=source.source_type if source else None,
                        source_locator=source.locator if source else None,
                        file_sha256=source.file_sha256 if source else None,
                        verification_status=fact.verification_status,
                        score=score,
                        reasons=tuple(dict.fromkeys(reasons)),
                    )
                )
        results.sort(key=lambda item: (-item.score, item.fact_id))
        results = results[: query.max_results]
        if self.reranker is not None:
            results = self.reranker.rerank(query, results)
        return results

    def _bm25_scores(self, session: Session, query: RetrievalQuery) -> dict[int, float]:
        expression = fts_expression(query.text)
        if not expression or not fts_available(session.get_bind()):
            return {}
        try:
            rows = session.execute(
                text(
                    f"SELECT fact_id, bm25({FTS_TABLE}, 3.0, 2.0, 1.0, 1.0) AS rank "
                    f"FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :expression "
                    "ORDER BY rank LIMIT 200"
                ),
                {"expression": expression},
            ).all()
        except Exception:
            return {}
        return {int(fact_id): max(0.0, -float(rank)) for fact_id, rank in rows}

    @staticmethod
    def _tokens(text: str) -> list[str]:
        values: list[str] = []
        for match in _TOKEN.finditer(text or ""):
            token = match.group(0)
            if _CJK.fullmatch(token):
                values.append(token)
                values.extend(cjk_bigrams(token))
            else:
                values.append(token)
        return list(dict.fromkeys(values))


def _parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []
