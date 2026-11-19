"""Two-stage retrieval: model reranking of deterministic candidates (Part E).

Stage 1 is the existing FactRetrievalService (FTS5 + tags + keywords). Stage 2
asks the model to score/reason over those candidates. Hard invariants:
- output Fact IDs must exist in the candidate set (which came from the DB);
- the model may not modify fact text;
- unconfirmed facts are excluded;
- failures fall back to the original deterministic order.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..models.base import ModelError, ModelProvider, TaskType
from .job_analysis import JobRequirements
from .retrieval import FactMatch, RetrievalQuery


class RerankItem(BaseModel):
    fact_id: int
    relevance_score: float
    relevance_reason: str = ""
    matched_requirements: list[str] = Field(default_factory=list)
    recommended_usage: str = ""


class RerankResult(BaseModel):
    items: list[RerankItem] = Field(default_factory=list)


class ModelReranker:
    def __init__(self, provider: ModelProvider, *, max_candidates: int = 30) -> None:
        self.provider = provider
        self.max_candidates = max_candidates

    async def rerank(
        self,
        query: RetrievalQuery,
        candidates: list[FactMatch],
        requirements: JobRequirements | None = None,
    ) -> list[FactMatch]:
        if not candidates:
            return []
        allowed = [m for m in candidates if m.verification_status == "CONFIRMED"][: self.max_candidates]
        if not allowed:
            return []
        by_id = {m.fact_id: m for m in allowed}
        payload = [
            {
                "fact_id": m.fact_id,
                "experience_id": m.experience_id,
                "experience_title": m.experience_title,
                "category": m.category,
                "skills": list(m.skills),
                "fact_content": m.fact_content,
            }
            for m in allowed
        ]
        job_text = ""
        if requirements is not None:
            job_text = "\n".join(
                [r.text for group in (
                    requirements.required_skills, requirements.preferred_skills,
                    requirements.responsibilities, requirements.research_topics,
                    requirements.hard_constraints,
                ) for r in group]
            )
        prompt = (
            "Rerank candidate resume facts for this job. Return JSON matching the schema.\n"
            "Score on: skill relevance, technical depth, research overlap, personal "
            "contribution, verifiable results, responsibility fit, and non-redundancy.\n"
            "Do NOT merely reward keyword counts. Use ONLY fact_ids from the candidates.\n"
            "Do not rewrite fact content.\n"
            f"Query: {query.text}\nJob requirements:\n{job_text}\nCandidates:\n{payload}"
        )
        try:
            result = await self.provider.generate_structured(
                TaskType.EXPERIENCE_RERANKING,
                [{"role": "user", "content": prompt}],
                RerankResult,
            )
        except ModelError:
            return candidates  # deterministic stage-1 order preserved

        ranked: list[FactMatch] = []
        seen: set[int] = set()
        for item in sorted(result.items, key=lambda i: -i.relevance_score):
            if item.fact_id in seen or item.fact_id not in by_id:
                continue  # reject invented / duplicate IDs
            match = by_id[item.fact_id]
            reason = "; ".join(
                part for part in (
                    item.relevance_reason,
                    "matched: " + ", ".join(item.matched_requirements) if item.matched_requirements else "",
                ) if part
            )
            ranked.append(
                FactMatch(
                    experience_id=match.experience_id,
                    fact_id=match.fact_id,
                    experience_title=match.experience_title,
                    experience_type=match.experience_type,
                    fact_content=match.fact_content,  # verbatim; never model-rewritten
                    category=match.category,
                    skills=match.skills,
                    source_type=match.source_type,
                    source_locator=match.source_locator,
                    file_sha256=match.file_sha256,
                    verification_status=match.verification_status,
                    score=float(item.relevance_score),
                    reasons=tuple([reason] if reason else []),
                )
            )
            seen.add(item.fact_id)
        # Any candidate the model omitted keeps a deterministic tail position.
        for match in allowed:
            if match.fact_id not in seen:
                ranked.append(match)
        return ranked
