"""General-purpose, user-owned job-search configuration (Phase 4).

A JobSearchProfile is the ONLY place where job preferences live. Every field is
optional: an unset field imposes no constraint. No industry, role, city, school,
employment type or language is hardcoded anywhere in the core code. Multiple
profiles per user are supported (separate job-search campaigns).

The model is pure Pydantic so it can be used by CLI, MCP and tests without a
database; SearchProfileService adds SQLite persistence with user isolation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .storage.models import SearchProfile

# Schema version lets us evolve criteria without breaking stored campaigns.
CRITERIA_SCHEMA_VERSION = 1

RemotePreference = Literal["onsite", "hybrid", "remote", "any"]
EmploymentType = Literal[
    "full_time", "part_time", "internship", "contract", "temporary", "apprenticeship"
]
Seniority = Literal["student", "entry", "junior", "mid", "senior", "lead", "executive"]
LanguagePreference = Literal["zh", "en", "ja", "ko", "fr", "de", "es", "any"]


class SalaryExpectation(BaseModel):
    """Optional. Either/both fields may be set; never inferred."""

    min: float | None = None
    max: float | None = None
    currency: str = ""
    period: str = ""  # year | month | day | hour


class VisaRequirement(BaseModel):
    requires_sponsorship: bool | None = None
    authorized_countries: list[str] = Field(default_factory=list)
    notes: str = ""


class ApplicationPreference(BaseModel):
    """How this campaign should submit applications. Auto is off unless the
    user explicitly sets mode=AUTO here AND grants mail access."""

    mode: Literal["MANUAL", "ASSISTED", "AUTO"] = "MANUAL"
    max_per_day: int = 0
    channels: list[str] = Field(default_factory=list)  # EMAIL|OFFICIAL_FORM|ATS|...
    require_approval: bool = True
    active_hours: str = ""  # optional local-time window description


class JobSearchProfile(BaseModel):
    """Transportable, serializable job-search preference set.

    All collection fields default to empty. An empty field means "no filter",
    never "exclude everything" and never an inferred default such as
    'internship', 'AI/ML', 'China' or 'new graduate'.
    """

    schema_version: int = CRITERIA_SCHEMA_VERSION
    user_id: str = "default"
    name: str = ""

    target_titles: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_preferences: list[RemotePreference] = Field(default_factory=list)
    employment_types: list[EmploymentType] = Field(default_factory=list)
    experience_levels: list[Seniority] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    salary_expectations: SalaryExpectation | None = None
    languages: list[LanguagePreference] = Field(default_factory=list)
    visa_requirements: VisaRequirement | None = None
    excluded_companies: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    preferred_sources: list[str] = Field(default_factory=list)
    application_preferences: ApplicationPreference = Field(
        default_factory=ApplicationPreference
    )
    # Free-form extension: e.g. {"portfolio_required": true} for design roles.
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_default(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _non_negative_caps(self) -> "JobSearchProfile":
        if self.application_preferences.max_per_day < 0:
            raise ValueError("max_per_day must be >= 0")
        return self

    # ------- matching helpers (preference-driven, no global rules) -------

    def is_excluded_company(self, company: str) -> bool:
        """Exact/normalized match only; an empty exclusion list excludes none."""
        from .discovery.base import normalize_key  # local import avoids cycles

        target = normalize_key(company)
        return any(normalize_key(c) == target for c in self.excluded_companies if c)

    def active_dimensions(self) -> dict[str, list[str]]:
        """Return only the preference dimensions the user actually set."""
        dims: dict[str, list[str]] = {}
        simple = {
            "target_titles": self.target_titles,
            "industries": self.industries,
            "skills": self.skills,
            "locations": self.locations,
            "employment_types": [e.value if hasattr(e, "value") else str(e) for e in self.employment_types],
            "experience_levels": [e.value if hasattr(e, "value") else str(e) for e in self.experience_levels],
            "languages": [e.value if hasattr(e, "value") else str(e) for e in self.languages],
        }
        for key, values in simple.items():
            if values:
                dims[key] = [str(v) for v in values if v]
        return dims

    def to_canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_canonical_json(cls, raw: str) -> "JobSearchProfile":
        return cls.model_validate(json.loads(raw))


class SearchProfileService:
    """SQLite CRUD with hard user isolation."""

    def __init__(self, sessions) -> None:
        self.sessions = sessions

    def create(self, profile: JobSearchProfile) -> JobSearchProfile:
        if not profile.name:
            raise ValueError("profile name is required")
        with self.sessions() as session:
            row = SearchProfile(
                user_id=profile.user_id,
                name=profile.name,
                criteria=profile.to_canonical_json(),
                enabled=True,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_model(row)

    def update(self, user_id: str, profile_id: int, profile: JobSearchProfile) -> JobSearchProfile | None:
        with self.sessions() as session:
            row = session.get(SearchProfile, profile_id)
            if row is None or row.user_id != user_id:
                return None
            row.name = profile.name or row.name
            row.criteria = profile.to_canonical_json()
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.commit()
            session.refresh(row)
            return self._to_model(row)

    def get(self, user_id: str, profile_id: int) -> JobSearchProfile | None:
        with self.sessions() as session:
            row = session.get(SearchProfile, profile_id)
            if row is None or row.user_id != user_id:
                return None
            return self._to_model(row)

    def list(self, user_id: str, *, enabled_only: bool = False) -> list[tuple[int, JobSearchProfile]]:
        with self.sessions() as session:
            stmt = select(SearchProfile).where(SearchProfile.user_id == user_id)
            if enabled_only:
                stmt = stmt.where(SearchProfile.enabled.is_(True))
            rows = session.execute(stmt.order_by(SearchProfile.id)).scalars().all()
            return [(r.id, self._to_model(r)) for r in rows]

    def delete(self, user_id: str, profile_id: int) -> bool:
        with self.sessions() as session:
            row = session.get(SearchProfile, profile_id)
            if row is None or row.user_id != user_id:
                return False
            session.delete(row)
            session.commit()
            return True

    def set_enabled(self, user_id: str, profile_id: int, enabled: bool) -> bool:
        with self.sessions() as session:
            row = session.get(SearchProfile, profile_id)
            if row is None or row.user_id != user_id:
                return False
            row.enabled = enabled
            row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.commit()
            return True

    @staticmethod
    def _to_model(row: SearchProfile) -> JobSearchProfile:
        profile = JobSearchProfile.from_canonical_json(row.criteria)
        profile.user_id = row.user_id
        profile.name = row.name
        return profile
