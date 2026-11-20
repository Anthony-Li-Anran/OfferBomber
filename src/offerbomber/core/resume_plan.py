"""Reactive-Resume-independent content plan.

ResumePlan is a neutral contract: an adapter in a later phase may convert it to
Reactive Resume's schema. Nothing here invents wording — every item is a verbatim
confirmed fact with traceable provenance.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .retrieval import FactMatch, FactRetrievalService, RetrievalQuery

CONFIRMED = "CONFIRMED"


class ResumeSectionItem(BaseModel):
    experience_id: int
    fact_id: int
    text: str
    category: str | None = None
    skills: list[str] = Field(default_factory=list)
    source_type: str | None = None
    source_locator: str | None = None
    file_sha256: str | None = None


class ResumeSection(BaseModel):
    title: str
    items: list[ResumeSectionItem] = Field(default_factory=list)


class FactAudit(BaseModel):
    """Provenance for one fact used in a generated resume.

    Phase 3.3 adds evidence and validation_status so a rewrite is traceable:
    every rendered claim must map back to source evidence, and anything the
    system cannot prove stays as the original text with a review marker.
    """
    fact_id: int
    fact_version: int = 1
    source_id: int | None = None
    original_text: str
    rendered_text: str
    experience_id: int
    evidence: str | None = None
    validation_status: str = "VERIFIED"  # VERIFIED | REVIEW_REQUIRED | REVERTED


class ResumePlan(BaseModel):
    job_id: str | None = None
    target_position: str
    selected_experience_ids: list[int] = Field(default_factory=list)
    selected_fact_ids: list[int] = Field(default_factory=list)
    section_order: list[str] = Field(default_factory=list)
    language: str = "zh"
    target_page_count: int = Field(default=2, ge=1, le=5)
    sections: list[ResumeSection] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Phase 3: structured provenance. Optional so Phase 2 callers stay valid.
    fact_audit: list[FactAudit] = Field(default_factory=list)
    highlight_skills: list[str] = Field(default_factory=list)
    generation_provider: str = "deterministic"


class ResumePlanBuilder:
    def __init__(self, retrieval: FactRetrievalService) -> None:
        self.retrieval = retrieval

    def build(
        self,
        query: RetrievalQuery,
        *,
        job_id: str | None = None,
        language: str = "zh",
        target_page_count: int = 2,
    ) -> ResumePlan:
        matches = self.retrieval.search(query)
        confirmed = [match for match in matches if match.verification_status == CONFIRMED]
        rejected = len(matches) - len(confirmed)
        warnings: list[str] = []
        if rejected:
            warnings.append(f"{rejected} unconfirmed fact(s) excluded from the resume plan")

        budget = max(1, target_page_count * 6)
        by_experience: dict[int, list[FactMatch]] = {}
        for match in confirmed[: budget * 2]:
            by_experience.setdefault(match.experience_id, []).append(match)

        sections: list[ResumeSection] = []
        selected_facts: list[int] = []
        selected_experiences: list[int] = []
        used = 0
        for experience_id, group in sorted(
            by_experience.items(), key=lambda pair: -max(m.score for m in pair[1])
        ):
            if used >= budget:
                break
            section = ResumeSection(title=group[0].experience_title)
            for match in group:
                if used >= budget:
                    break
                section.items.append(
                    ResumeSectionItem(
                        experience_id=match.experience_id,
                        fact_id=match.fact_id,
                        text=match.fact_content,
                        category=match.category,
                        skills=list(match.skills),
                        source_type=match.source_type,
                        source_locator=match.source_locator,
                        file_sha256=match.file_sha256,
                    )
                )
                selected_facts.append(match.fact_id)
                used += 1
            if section.items:
                selected_experiences.append(experience_id)
                sections.append(section)

        if not sections:
            warnings.append("no confirmed facts matched; nothing was planned")

        return ResumePlan(
            job_id=job_id,
            target_position=query.direction or query.text[:120],
            selected_experience_ids=selected_experiences,
            selected_fact_ids=selected_facts,
            section_order=[section.title for section in sections],
            language=language,
            target_page_count=target_page_count,
            sections=sections,
            warnings=warnings,
        )
