"""Model-driven resume planning with strict truthfulness (task book Part F).

The model decides selection / order / emphasis / language / budget, but content
is drawn ONLY from confirmed experience facts. Every used fact records its id,
version, source and original text. Generated wording is checked against the
original; unverifiable new numbers or invented acceptance claims create a
pending-confirmation warning instead of being silently accepted.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..models.base import ModelError, ModelProvider, TaskType
from ..storage.models import Education, ExperienceFact, Profile
from .job_analysis import JobRequirements
from .retrieval import FactMatch
from .resume_plan import FactAudit, ResumePlan, ResumeSection, ResumeSectionItem

_CONFIRMED = "CONFIRMED"
_QUANT = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|x\b|倍|万|亿|人|个|ms\b|s\b|\$|元|K\b|M\b)", re.I)
_ACCEPTED = re.compile(r"(已录用|accepted|published|录用通知)", re.I)
_SUBMITTED = re.compile(r"(投稿|submitted|under review|在审)", re.I)


class PlannedFact(BaseModel):
    fact_id: int
    rendered_text: str = ""


class PlanDecision(BaseModel):
    selected_fact_ids: list[int] = Field(default_factory=list)
    experience_order: list[int] = Field(default_factory=list)
    language: str = "zh"
    target_page_count: int = 1
    highlight_skills: list[str] = Field(default_factory=list)
    section_order: list[str] = Field(default_factory=list)
    rendered_facts: list[PlannedFact] = Field(default_factory=list)
    rationale: str = ""


class SmartResumePlanner:
    def __init__(self, provider: ModelProvider, sessions: sessionmaker[Session] | None = None) -> None:
        self.provider = provider
        self.sessions = sessions

    def _provenance(self, fact_ids: list[int]) -> dict[int, tuple[int, int | None]]:
        """Return fact_id -> (version, source_id) from authoritative DB rows."""
        if not self.sessions or not fact_ids:
            return {}
        with self.sessions() as session:
            rows = session.execute(
                select(ExperienceFact).where(ExperienceFact.id.in_(fact_ids))
            ).scalars().all()
            return {row.id: (row.version, row.source_id) for row in rows}

    def _context(
        self,
        profile: Profile | None,
        educations: list[Education],
        facts: list[FactMatch],
    ) -> dict[str, Any]:
        return {
            "profile": {
                "display_name": profile.display_name if profile else None,
                "contact": profile.contact if profile else None,
                "summary": profile.summary if profile else None,
                "target_position": profile.target_position if profile else None,
            } if profile else {},
            "educations": [
                {
                    "school": e.school, "degree": e.degree, "major": e.major,
                    "start_date": e.start_date, "end_date": e.end_date,
                    "description": e.description,
                }
                for e in educations
            ],
            "facts": [
                {
                    "fact_id": f.fact_id,
                    "experience_id": f.experience_id,
                    "experience_title": f.experience_title,
                    "experience_type": f.experience_type,
                    "category": f.category,
                    "skills": list(f.skills),
                    "original_text": f.fact_content,
                }
                for f in facts
            ],
        }

    async def plan(
        self,
        *,
        profile: Profile | None,
        educations: list[Education],
        ranked_facts: list[FactMatch],
        requirements: JobRequirements | None,
        language: str = "zh",
        target_page_count: int = 1,
    ) -> ResumePlan:
        confirmed = [f for f in ranked_facts if f.verification_status == _CONFIRMED]
        warnings: list[str] = []
        if len(confirmed) != len(ranked_facts):
            warnings.append("unconfirmed facts excluded from planning")
        if not confirmed:
            return ResumePlan(
                target_position=requirements.job_category if requirements else "",
                language=language,
                target_page_count=target_page_count,
                warnings=warnings + ["no confirmed facts; nothing planned"],
                generation_provider=f"model:{self.provider.name}",
            )

        by_id = {f.fact_id: f for f in confirmed}
        budget = self._budget(target_page_count, len(confirmed))
        context = self._context(profile, educations, confirmed)
        job_text = ""
        if requirements:
            job_text = json.dumps(
                {
                    "category": requirements.job_category,
                    "seniority": requirements.seniority,
                    "required": [r.text for r in requirements.required_skills],
                    "preferred": [r.text for r in requirements.preferred_skills],
                    "responsibilities": [r.text for r in requirements.responsibilities],
                    "keywords": requirements.important_keywords,
                    "language": requirements.resume_language,
                },
                ensure_ascii=False,
            )
        prompt = (
            "Plan a tailored resume using ONLY the supplied confirmed facts.\n"
            "Rules:\n"
            "- You may improve wording, compress, translate to the target language, "
            "and use job terminology, but you may NOT add any new fact, number, "
            "organization, date or acceptance claim.\n"
            "- Keep quantifications and paper status identical to original_text.\n"
            "- Choose a subset of fact_ids within the budget; order experiences.\n"
            "- Preserve education and contact details from the profile context.\n"
            f"Target language: {language}; pages: {target_page_count}; budget facts: {budget}\n"
            f"Job: {job_text}\nProfile context: {json.dumps(context, ensure_ascii=False)}"
        )
        try:
            decision = await self.provider.generate_structured(
                TaskType.RESUME_PLANNING,
                [{"role": "user", "content": prompt}],
                PlanDecision,
            )
        except ModelError as exc:
            warnings.append(f"model planning failed ({exc.kind.value}); deterministic fallback used")
            decision = self._deterministic(confirmed, budget, language, target_page_count)

        # Reject invented IDs, cap budget, enforce page count.
        selected: list[int] = []
        for fid in decision.selected_fact_ids:
            if fid in by_id and fid not in selected and len(selected) < budget:
                selected.append(fid)
        if not selected:
            selected = [f.fact_id for f in confirmed[:budget]]
            warnings.append("model selected no valid facts; fallback selection used")

        order = [x for x in decision.experience_order if isinstance(x, int)]
        rendered_map = {r.fact_id: r.rendered_text for r in decision.rendered_facts}

        sections_by_exp: dict[int, list[FactMatch]] = {}
        for fid in selected:
            fact = by_id[fid]
            sections_by_exp.setdefault(fact.experience_id, []).append(fact)

        def sort_key(pair: tuple[int, list[FactMatch]]) -> tuple[int, int]:
            exp_id = pair[0]
            index = order.index(exp_id) if exp_id in order else len(order)
            return (index, -max(m.score for m in pair[1]))

        sections: list[ResumeSection] = []
        used_fact_ids: list[int] = []
        all_warnings = list(warnings)
        for exp_id, group in sorted(sections_by_exp.items(), key=sort_key):
            section = ResumeSection(title=group[0].experience_title)
            for match in group:
                rendered = rendered_map.get(match.fact_id) or match.fact_content
                issue = self._consistency_issue(rendered, match.fact_content)
                if issue:
                    all_warnings.append(f"fact {match.fact_id}: {issue}; used original text")
                    rendered = match.fact_content
                section.items.append(
                    ResumeSectionItem(
                        experience_id=match.experience_id,
                        fact_id=match.fact_id,
                        text=rendered,
                        category=match.category,
                        skills=list(match.skills),
                        source_type=match.source_type,
                        source_locator=match.source_locator,
                        file_sha256=match.file_sha256,
                    )
                )
                used_fact_ids.append(match.fact_id)
            if section.items:
                sections.append(section)

        provenance = self._provenance(used_fact_ids)
        fact_audit: list[FactAudit] = []
        for fid in used_fact_ids:
            fact = by_id[fid]
            version, source_id = provenance.get(fid, (1, None))
            fact_audit.append(
                FactAudit(
                    fact_id=fid,
                    fact_version=version,
                    source_id=source_id,
                    original_text=fact.fact_content,
                    rendered_text=rendered_map.get(fid, fact.fact_content),
                    experience_id=fact.experience_id,
                )
            )

        return ResumePlan(
            job_id=str(requirements.job_id) if requirements and requirements.job_id is not None else None,
            target_position=(
                profile.target_position if profile and profile.target_position
                else (requirements.resume_language if requirements else "")
            ),
            selected_experience_ids=list(sections_by_exp.keys()),
            selected_fact_ids=used_fact_ids,
            section_order=decision.section_order or [s.title for s in sections],
            language=decision.language or language,
            target_page_count=max(1, min(2, decision.target_page_count)),
            sections=sections,
            warnings=all_warnings,
            fact_audit=fact_audit,
            highlight_skills=decision.highlight_skills[:20],
            generation_provider=f"model:{self.provider.name}",
        )

    @staticmethod
    def _budget(pages: int, available: int) -> int:
        return min(available, max(1, pages * 6))

    @staticmethod
    def _deterministic(
        facts: list[FactMatch], budget: int, language: str, pages: int
    ) -> PlanDecision:
        return PlanDecision(
            selected_fact_ids=[f.fact_id for f in facts[:budget]],
            experience_order=list(dict.fromkeys(f.experience_id for f in facts)),
            language=language,
            target_page_count=pages,
        )

    @staticmethod
    def _consistency_issue(rendered: str, original: str) -> str | None:
        original_numbers = set(_QUANT.findall(original.lower()))
        rendered_numbers = set(_QUANT.findall(rendered.lower()))
        if rendered_numbers - original_numbers:
            return "rendered text introduces a new quantification"
        if _ACCEPTED.search(rendered) and not _ACCEPTED.search(original) and _SUBMITTED.search(original):
            return "rendered text upgrades a submission into acceptance"
        return None
