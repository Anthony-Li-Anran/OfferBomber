"""General-purpose job matching engine (Phase 4, section 3).

No fixed occupation taxonomy and no global keyword list. A job is scored
against (a) the user's own JobSearchProfile dimensions and (b) the user's
verified experience facts. Hard constraints (excluded companies, explicit
visa/location requirements) are kept separate from soft semantic overlap.
Semantic mismatch != eligibility failure.

The host model may supply a richer structured analysis; deterministic scoring
is always available as a fallback and as a sanity floor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..discovery.base import normalize_key
from ..search_profile import JobSearchProfile
from .job_schema import NormalizedJob

_TOKEN = re.compile(r"[A-Za-z][A-Za-z+#.]{1,}|[\u3400-\u9fff]{2,}")


def tokens(text: str | None) -> set[str]:
    return {normalize_key(t) for t in _TOKEN.findall(text or "") if normalize_key(t)}


class MatchOutput(BaseModel):
    job_id: str = ""
    match_score: float = 0.0  # 0..1
    eligibility: str = "UNKNOWN"  # ELIGIBLE | REVIEW | INELIGIBLE | UNKNOWN
    matched_requirements: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    relevant_fact_ids: list[int] = Field(default_factory=list)
    recommended_resume_version: int | None = None
    application_recommendation: str = "REVIEW"  # APPLY | REVIEW | SKIP
    explanation: str = ""
    hard_constraint_results: dict[str, bool] = Field(default_factory=dict)
    score_breakdown: dict[str, float] = Field(default_factory=dict)


@dataclass
class FactView:
    id: int
    content: str
    skills: list[str] = field(default_factory=list)
    version: int = 1


class JobMatcher:
    """Deterministic, explainable matcher. Weights come from the profile's
    active dimensions only; absent dimensions contribute zero, never a guess.
    """

    def __init__(self, *, threshold_apply: float = 0.55, threshold_review: float = 0.25) -> None:
        self.threshold_apply = threshold_apply
        self.threshold_review = threshold_review

    def match(
        self,
        job: NormalizedJob,
        profile: JobSearchProfile,
        facts: list[FactView],
    ) -> MatchOutput:
        out = MatchOutput(job_id=job.job_id)
        job_text = tokens(f"{job.title} {job.description or ''} {job.requirements or ''} {job.industry or ''}")
        fact_text: set[str] = set()
        for fact in facts:
            fact_text |= tokens(fact.content)
            fact_text |= {normalize_key(s) for s in fact.skills}

        # ---- Hard constraints (only when the user actually set them) ----
        hard: dict[str, bool] = {}
        if profile.excluded_companies:
            hard["company_not_excluded"] = not profile.is_excluded_company(job.company)
        if profile.locations and job.location:
            wanted = {normalize_key(x) for x in profile.locations}
            hard["location_match"] = any(w in tokens(job.location) for w in wanted) or any(
                w in normalize_key(job.location) for w in wanted
            )
        if profile.remote_preferences and job.remote_policy:
            prefs = {str(x) for x in profile.remote_preferences}
            hard["remote_match"] = ("any" in prefs) or (normalize_key(job.remote_policy) in {normalize_key(p) for p in prefs})
        if profile.employment_types and job.employment_type:
            wanted = {normalize_key(x) for x in profile.employment_types}
            hard["employment_type_match"] = normalize_key(job.employment_type) in wanted
        if profile.excluded_keywords:
            excluded = {normalize_key(k) for k in profile.excluded_keywords}
            hard["no_excluded_keyword"] = not (job_text & excluded)
        out.hard_constraint_results = hard

        hard_failed = [name for name, passed in hard.items() if not passed]
        if hard_failed:
            out.eligibility = "INELIGIBLE"
            out.application_recommendation = "SKIP"
            out.explanation = "hard constraints failed: " + ", ".join(hard_failed)
            return out

        # ---- Soft dimensions ----
        breakdown: dict[str, float] = {}
        relevant_ids: list[int] = []
        matched: list[str] = []
        missing: list[str] = []

        # Skills / titles / industries overlap with verified facts + profile.
        if profile.skills:
            skill_hits = [s for s in profile.skills if normalize_key(s) in job_text]
            breakdown["profile_skills"] = len(skill_hits) / len(profile.skills)
            matched += [f"skill:{s}" for s in skill_hits]
            missing += [f"skill:{s}" for s in profile.skills if s not in skill_hits]
        if profile.target_titles:
            title_hits = [t for t in profile.target_titles if normalize_key(t) in tokens(job.title)]
            breakdown["target_title"] = min(1.0, len(title_hits) / max(1, len(profile.target_titles)))
            matched += [f"title:{t}" for t in title_hits]
        if facts:
            overlap = job_text & fact_text
            evidence_factor = min(1.0, len(overlap) / max(1, len(job_text))) if job_text else 0.0
            breakdown["verified_overlap"] = evidence_factor
            for fact in facts:
                if tokens(fact.content) & job_text or ({normalize_key(s) for s in fact.skills} & job_text):
                    relevant_ids.append(fact.id)
        # Requirement coverage against facts (host can enrich via requirements_json).
        req_tokens = tokens(job.requirements or "")
        if req_tokens:
            covered = req_tokens & fact_text
            breakdown["requirement_coverage"] = len(covered) / len(req_tokens)
            missing += [f"requirement:{t}" for t in sorted(req_tokens - fact_text)[:8]]

        # Evidence availability: a job with no JD text cannot be confidently matched.
        breakdown["evidence_available"] = 1.0 if job_text else 0.0

        weights = {
            "profile_skills": 0.30,
            "target_title": 0.20,
            "verified_overlap": 0.35,
            "requirement_coverage": 0.15,
        }
        core = sum(breakdown.get(k, 0.0) * w for k, w in weights.items())
        # Normalize when some dimensions are absent so the score isn't unfairly low.
        active_weight = sum(w for k, w in weights.items() if k in breakdown and k != "evidence_available")
        score = (core / active_weight) if active_weight > 0 else 0.0
        if not job_text:
            score = 0.0
        out.match_score = round(min(1.0, max(0.0, score)), 3)
        out.score_breakdown = {k: round(v, 3) for k, v in breakdown.items()}
        out.relevant_fact_ids = sorted(set(relevant_ids))
        out.matched_requirements = sorted(set(matched))[:20]
        out.missing_requirements = sorted(set(missing))[:20]

        if out.match_score >= self.threshold_apply and relevant_ids:
            out.eligibility = "ELIGIBLE"
            out.application_recommendation = "APPLY"
            out.explanation = "profile and confirmed facts cover the job's key requirements"
        elif out.match_score >= self.threshold_review:
            out.eligibility = "REVIEW"
            out.application_recommendation = "REVIEW"
            out.explanation = "partial overlap; host model/human should review before applying"
        else:
            out.eligibility = "REVIEW"
            out.application_recommendation = "SKIP"
            out.explanation = "insufficient evidence that confirmed facts fit this role"
        # Threshold from the profile overrides defaults when explicitly set.
        threshold = profile.extra.get("apply_threshold")
        if isinstance(threshold, (int, float)):
            self.threshold_apply = float(threshold)
        return out
