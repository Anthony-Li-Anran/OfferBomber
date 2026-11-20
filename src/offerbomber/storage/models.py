"""ORM models covering the full OfferBomber data model (task book section 15).

Phase 1 actively reads/writes: Company, JobSource, Job, JobSourceRecord, Task,
AuditLog, Setting. The remaining tables are defined here so Alembic generates a
complete, stable schema that later phases extend without destructive migration.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class DateTimeUTC(DateTime):  # type: ignore[misc]
    """Timezone-aware DateTime (stored as UTC, localized at the boundary)."""

    cache_ok = True

    def __init__(self) -> None:
        super().__init__(timezone=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTimeUTC, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTimeUTC,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


# --------------------------------------------------------------------------
# Profile / experience knowledge base (Phase 2)
# --------------------------------------------------------------------------


class Profile(Base, TimestampMixin):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    contact: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    target_position: Mapped[str | None] = mapped_column(String(200))
    target_locations: Mapped[str | None] = mapped_column(Text)  # JSON
    job_search_status: Mapped[str] = mapped_column(
        String(40), default="OPEN", server_default="OPEN"
    )
    job_preferences: Mapped[str | None] = mapped_column(Text)  # JSON
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")


class Education(Base, TimestampMixin):
    __tablename__ = "educations"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    school: Mapped[str] = mapped_column(String(200))
    degree: Mapped[str | None] = mapped_column(String(100))
    major: Mapped[str | None] = mapped_column(String(200))
    start_date: Mapped[str | None] = mapped_column(String(32))
    end_date: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("experience_sources.id", name="fk_educations_source")
    )


class Experience(Base, TimestampMixin):
    __tablename__ = "experiences"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", name="fk_experiences_profile")
    )
    type: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(300))
    organization: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    start_date: Mapped[str | None] = mapped_column(String(32))
    end_date: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(50), default="ACTIVE", server_default="ACTIVE", index=True
    )
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(Text)  # JSON
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("experience_sources.id", name="fk_experiences_source")
    )
    current_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    archived_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)


class ExperienceFact(Base, TimestampMixin):
    __tablename__ = "experience_facts"
    __table_args__ = (Index("ix_fact_verification", "verification_status", "is_deleted"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    experience_id: Mapped[int] = mapped_column(
        ForeignKey("experiences.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(80))
    skills: Mapped[str | None] = mapped_column(Text)  # JSON
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("experience_sources.id", name="fk_facts_source")
    )
    source_locator: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(
        String(30), default="UNVERIFIED", index=True
    )
    confidence: Mapped[float] = mapped_column(default=0.0)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class ExperienceSource(Base, TimestampMixin):
    __tablename__ = "experience_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    source_type: Mapped[str] = mapped_column(String(50), index=True)  # pdf | reactive_resume | manual | user_confirmed
    locator: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(300))
    file_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text)


class ExperienceVersion(Base, TimestampMixin):
    __tablename__ = "experience_versions"
    __table_args__ = (UniqueConstraint("experience_id", "version", name="uq_exp_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    experience_id: Mapped[int] = mapped_column(
        ForeignKey("experiences.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(40), default="UPDATE", server_default="UPDATE")
    snapshot: Mapped[str] = mapped_column(Text)  # JSON: experience + all facts
    change_note: Mapped[str | None] = mapped_column(Text)


class ResumeImport(Base, TimestampMixin):
    __tablename__ = "resume_imports"
    __table_args__ = (UniqueConstraint("user_id", "file_sha256", name="uq_resume_import_hash"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(100), index=True)
    file_name: Mapped[str] = mapped_column(String(300))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    parser: Mapped[str] = mapped_column(String(80), default="deterministic_v1")
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    candidate_payload: Mapped[str | None] = mapped_column(Text)  # JSON
    error_message: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------
# Resume (Phase 3)
# --------------------------------------------------------------------------


class ResumeTemplate(Base, TimestampMixin):
    __tablename__ = "resume_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    direction: Mapped[str | None] = mapped_column(String(120))
    template_payload: Mapped[str | None] = mapped_column(Text)  # JSON


class ResumePlan(Base, TimestampMixin):
    __tablename__ = "resume_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("resume_templates.id"))
    language: Mapped[str] = mapped_column(String(20), default="zh")
    plan_payload: Mapped[str] = mapped_column(Text)  # JSON: selected ids, order, budgets


class ResumeVersion(Base, TimestampMixin):
    __tablename__ = "resume_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(100), default="default", server_default="default", index=True,
        nullable=False,
    )
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("resume_plans.id"))
    file_path: Mapped[str] = mapped_column(Text)
    file_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    immutable: Mapped[bool] = mapped_column(Boolean, default=True)
    fact_snapshot: Mapped[str | None] = mapped_column(Text)  # JSON
    plan_sha256: Mapped[str | None] = mapped_column(String(64), index=True)  # Phase 3.3


# --------------------------------------------------------------------------
# Discovery / jobs (Phase 1)
# --------------------------------------------------------------------------


class Company(Base, TimestampMixin):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("name", name="uq_company_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str | None] = mapped_column(String(200))
    homepage: Mapped[str | None] = mapped_column(Text)
    is_seed: Mapped[bool] = mapped_column(Boolean, default=False)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)

    sources: Mapped[list[JobSource]] = relationship(back_populates="company")


class JobSource(Base, TimestampMixin):
    __tablename__ = "job_sources"
    __table_args__ = (
        UniqueConstraint("platform", "board_token", name="uq_source_platform_token"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    platform: Mapped[str] = mapped_column(String(50), index=True)
    board_token: Mapped[str] = mapped_column(String(200))
    board_url: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    last_error: Mapped[str | None] = mapped_column(Text)

    company: Mapped[Company | None] = relationship(back_populates="sources")


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("external_id", name="uq_job_external_id"),
        Index("ix_job_dedup", "normalized_title", "normalized_company", "location"),
        Index("ix_job_validity", "validity_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(300))
    company: Mapped[str] = mapped_column(String(200), index=True)
    normalized_company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(400))
    normalized_title: Mapped[str] = mapped_column(String(400))
    location: Mapped[str | None] = mapped_column(String(200))
    remote_policy: Mapped[str | None] = mapped_column(String(50))
    employment_type: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[str | None] = mapped_column(Text)
    education_requirement: Mapped[str | None] = mapped_column(String(200))
    internship_duration: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    discovered_at: Mapped[datetime] = mapped_column(DateTimeUTC)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    source_platform: Mapped[str] = mapped_column(String(50), index=True)
    official_url: Mapped[str | None] = mapped_column(Text)
    normalized_url: Mapped[str | None] = mapped_column(String(500), index=True)
    apply_url: Mapped[str | None] = mapped_column(Text)
    apply_method: Mapped[str | None] = mapped_column(String(50))  # email | official_form | unknown
    recruitment_email: Mapped[str | None] = mapped_column(String(200))
    email_source_evidence: Mapped[str | None] = mapped_column(Text)
    validity_status: Mapped[str] = mapped_column(String(30), default="LIKELY_ACTIVE", index=True)
    match_score: Mapped[float | None] = mapped_column()
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)


class JobSourceRecord(Base, TimestampMixin):
    __tablename__ = "job_source_records"
    __table_args__ = (
        UniqueConstraint("job_id", "source_platform", "source_url", name="uq_job_source_record"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    source_platform: Mapped[str] = mapped_column(String(50))
    source_url: Mapped[str] = mapped_column(Text)
    raw_external_id: Mapped[str | None] = mapped_column(String(300))
    seen_at: Mapped[datetime] = mapped_column(DateTimeUTC)


class JobMatch(Base, TimestampMixin):
    __tablename__ = "job_matches"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    score: Mapped[float] = mapped_column()
    rationale: Mapped[str | None] = mapped_column(Text)
    hard_filter_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    model_provider: Mapped[str | None] = mapped_column(String(80))


# --------------------------------------------------------------------------
# Applications / email / recruitment (Phase 4-5)
# --------------------------------------------------------------------------


class Application(Base, TimestampMixin):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("job_id", name="uq_application_job"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(40), index=True, default="DISCOVERED")
    resume_version_id: Mapped[int | None] = mapped_column(ForeignKey("resume_versions.id"))
    channel: Mapped[str | None] = mapped_column(String(40))
    recipient: Mapped[str | None] = mapped_column(String(200))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)


class ApplicationEvent(Base, TimestampMixin):
    __tablename__ = "application_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[str | None] = mapped_column(Text)


class EmailMessage(Base, TimestampMixin):
    __tablename__ = "email_messages"
    __table_args__ = (UniqueConstraint("provider_message_id", name="uq_email_provider_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_message_id: Mapped[str] = mapped_column(String(300))
    thread_id: Mapped[str | None] = mapped_column(String(300), index=True)
    direction: Mapped[str] = mapped_column(String(20))  # inbound | outbound
    from_addr: Mapped[str | None] = mapped_column(String(300))
    to_addr: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(500))
    body_preview: Mapped[str | None] = mapped_column(Text)  # redacted snippet only
    classification: Mapped[str | None] = mapped_column(String(60), index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)


class EmailThread(Base, TimestampMixin):
    __tablename__ = "email_threads"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_thread_id: Mapped[str] = mapped_column(String(300), unique=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)


class RecruitmentEvent(Base, TimestampMixin):
    __tablename__ = "recruitment_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"))
    company: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(300))
    event_type: Mapped[str] = mapped_column(String(60), index=True)  # interview | assessment | deadline
    round_index: Mapped[int | None] = mapped_column(Integer)
    starts_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    timezone: Mapped[str | None] = mapped_column(String(64))
    meeting_url: Mapped[str | None] = mapped_column(Text)
    preparation_notes: Mapped[str | None] = mapped_column(Text)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class CalendarEvent(Base, TimestampMixin):
    __tablename__ = "calendar_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    recruitment_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("recruitment_events.id", ondelete="SET NULL")
    )
    provider: Mapped[str | None] = mapped_column(String(60))
    provider_event_id: Mapped[str | None] = mapped_column(String(300))
    synced_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)


# --------------------------------------------------------------------------
# Infrastructure
# --------------------------------------------------------------------------


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(String(120), unique=True)
    task_type: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[str | None] = mapped_column(Text)  # JSON
    status: Mapped[str] = mapped_column(String(30), index=True, default="PENDING")
    created_at_task: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTimeUTC, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    finished_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    last_error: Mapped[str | None] = mapped_column(Text)


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(60))
    recipients: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(300))
    body_preview: Mapped[str | None] = mapped_column(Text)
    urgency: Mapped[str] = mapped_column(String(20), default="normal")
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    error: Mapped[str | None] = mapped_column(Text)


class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    action_type: Mapped[str] = mapped_column(String(80), index=True)
    resource: Mapped[str] = mapped_column(Text)
    proposed_payload: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    decided_by: Mapped[str | None] = mapped_column(String(80))


class ModelCall(Base, TimestampMixin):
    __tablename__ = "model_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(80))
    task_type: Mapped[str] = mapped_column(String(80))
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(80), default="system")
    action: Mapped[str] = mapped_column(String(120), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(120))
    detail: Mapped[str | None] = mapped_column(Text)  # sanitized JSON, no PII bodies


class Setting(Base, TimestampMixin):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(150), unique=True)
    value: Mapped[str] = mapped_column(Text)


# --------------------------------------------------------------------------
# MCP confirmation ledger (Phase 3.3)
# --------------------------------------------------------------------------


class McpConfirmation(Base):
    """Durable, atomically-consumed human-approval record for MCP write tools.

    Only a hash of the high-entropy token is stored. State transitions:
    PENDING -> APPROVED -> CONSUMED, or PENDING -> REJECTED / EXPIRED.
    """

    __tablename__ = "mcp_confirmations"
    __table_args__ = (Index("ix_mcp_confirmations_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    job_id: Mapped[int | None] = mapped_column(Integer)
    source_resume_id: Mapped[str | None] = mapped_column(String(200))
    plan_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    plan_snapshot: Mapped[str | None] = mapped_column(Text)  # canonical JSON
    fact_versions: Mapped[str | None] = mapped_column(Text)  # JSON
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTimeUTC, server_default=func.current_timestamp(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTimeUTC, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTimeUTC)
