"""Model-driven job description analysis (task book Part D).

Every requirement carries `evidence` quoted from the source JD. MUST requirements
are separated from NICE_TO_HAVE; missing information is marked UNKNOWN rather
than guessed. The analyzer never invents a requirement absent from the JD.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..models.base import ModelError, ModelErrorKind, ModelProvider, TaskType
from ..storage.models import Job


class Requirement(BaseModel):
    text: str
    evidence: str = ""
    level: str = "MUST"  # MUST | NICE_TO_HAVE | UNKNOWN


class JobRequirements(BaseModel):
    job_id: int | None = None
    job_category: str = "UNKNOWN"
    seniority: str = "UNKNOWN"
    required_skills: list[Requirement] = Field(default_factory=list)
    preferred_skills: list[Requirement] = Field(default_factory=list)
    responsibilities: list[Requirement] = Field(default_factory=list)
    research_topics: list[Requirement] = Field(default_factory=list)
    education_requirements: list[Requirement] = Field(default_factory=list)
    hard_constraints: list[Requirement] = Field(default_factory=list)
    resume_language: str = "UNKNOWN"
    important_keywords: list[str] = Field(default_factory=list)
    evidence_available: bool = False
    analysis_provider: str = "UNKNOWN"
    warnings: list[str] = Field(default_factory=list)


# Deterministic fallback used when no model is configured or a call fails.
class DeterministicJobAnalyzer:
    def analyze(self, job: Job) -> JobRequirements:
        text = " ".join(
            part for part in (job.description or "", job.requirements or "") if part
        )
        keywords: list[str] = []
        for token in re.findall(r"[A-Za-z+#.]{2,}|[\u3400-\u9fff]{2,}", text):
            if token.lower() not in [k.lower() for k in keywords]:
                keywords.append(token)
        return JobRequirements(
            job_id=job.id,
            required_skills=[Requirement(text=k, evidence=k, level="UNKNOWN") for k in keywords[:15]],
            important_keywords=keywords[:20],
            education_requirements=(
                [Requirement(text=job.education_requirement, evidence=job.education_requirement or "")]
                if job.education_requirement else []
            ),
            evidence_available=bool(text.strip()),
            analysis_provider="deterministic_fallback",
            warnings=["model analysis unavailable; keyword extraction only"],
        )


class JobAnalyzer:
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider
        self.fallback = DeterministicJobAnalyzer()

    def _job_text(self, job: Job) -> str:
        return "\n".join(
            f"{label}: {value}"
            for label, value in (
                ("title", job.title),
                ("company", job.company),
                ("location", job.location),
                ("description", job.description),
                ("requirements", job.requirements),
                ("education_requirement", job.education_requirement),
                ("employment_type", job.employment_type),
                ("remote_policy", job.remote_policy),
            )
            if value
        )

    async def analyze(self, job: Job) -> JobRequirements:
        text = self._job_text(job)
        if not text.strip():
            result = self.fallback.analyze(job)
            result.warnings.append("JD text empty")
            return result
        prompt = (
            "Analyze this job posting into structured JSON matching the schema.\n"
            "Rules:\n"
            "- Separate MUST requirements from NICE_TO_HAVE; do not promote a nice-to-have to MUST.\n"
            "- For EVERY requirement, quote the exact supporting substring from the JD in 'evidence'.\n"
            "- Mark uncertain fields 'UNKNOWN'; never guess.\n"
            "- resume_language: choose zh, en or UNKNOWN based on the JD language.\n"
            f"Job posting:\n{text}"
        )
        try:
            result = await self.provider.generate_structured(
                TaskType.JOB_ANALYSIS,
                [{"role": "user", "content": prompt}],
                JobRequirements,
            )
        except ModelError:
            result = self.fallback.analyze(job)
            result.warnings.append("model analysis failed; deterministic fallback used")
            return result

        # Enforce invariants regardless of provider behaviour.
        result.job_id = job.id
        result.analysis_provider = self.provider.name
        result.evidence_available = bool(text.strip())
        for group in (
            result.required_skills, result.preferred_skills, result.responsibilities,
            result.research_topics, result.education_requirements, result.hard_constraints,
        ):
            for item in group:
                if item.evidence and item.evidence not in text:
                    result.warnings.append(f"evidence not found in JD, marked UNKNOWN: {item.text[:60]}")
                    item.level = "UNKNOWN"
        return result
