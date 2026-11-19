"""Pydantic DTOs for import candidates.

Candidate values are deliberately low-confidence suggestions. Nothing here is
written to the knowledge base until a user explicitly selects it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateFact(BaseModel):
    content: str
    category: str = "OTHER"
    skills: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    locator: str | None = None


class CandidateExperience(BaseModel):
    type: str = "CUSTOM"
    title: str
    organization: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    description: str | None = None
    facts: list[CandidateFact] = Field(default_factory=list)
    confidence: float = 0.0
    locator: str | None = None


class CandidateEducation(BaseModel):
    school: str | None = None
    degree: str | None = None
    major: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    description: str | None = None
    confidence: float = 0.0
    locator: str | None = None


class CandidateProfile(BaseModel):
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    contact: str | None = None
    summary: str | None = None
    target_position: str | None = None
    educations: list[CandidateEducation] = Field(default_factory=list)
    experiences: list[CandidateExperience] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    parser: str = "deterministic_v1"
