"""Configuration loading and validation for OfferBomber.

Design notes:
- Pydantic models are the single source of truth for config shape.
- YAML is loaded from an explicit path; missing fields fall back to documented defaults.
- Credentials are NEVER read from config.yaml (see .env.example / OS keyring).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class AppConfig(BaseModel):
    name: str = "OfferBomber"
    mode: str = "personal"
    timezone: str = "Asia/Shanghai"
    dry_run: bool = True


class CompanySource(BaseModel):
    platform: Literal["greenhouse", "lever", "ashby", "official_careers"]
    token: str
    name: str | None = None


class DiscoveryConfig(BaseModel):
    enabled: bool = True
    auto_expand_companies: bool = True
    interval_hours: int = Field(default=4, ge=1)
    sources: list[str] = Field(default_factory=list)
    companies: list[CompanySource] = Field(default_factory=list)


class MatchingConfig(BaseModel):
    broad_search: bool = True
    exclude_hard_mismatches: bool = True
    target_fields: list[str] = Field(default_factory=list)


class ProfileConfig(BaseModel):
    allow_pdf_import: bool = True
    allow_manual_updates: bool = True
    store_fact_provenance: bool = True
    require_fact_verification: bool = True


class ResumeConfig(BaseModel):
    provider: str = "reactive_resume"
    tailor_per_job: bool = True
    preserve_original: bool = True
    validate_pdf: bool = True
    cache_base_versions: bool = True


class ApplicationsConfig(BaseModel):
    auto_prepare: bool = True
    auto_send: bool = False
    max_emails_per_day: int = Field(default=20, ge=0)
    require_verified_recipient: bool = True
    prevent_duplicates: bool = True


class EmailConfig(BaseModel):
    provider: str = "gmail"
    sync_minutes: int = Field(default=10, ge=1)
    auto_reply_low_risk: bool = False


class ModelsConfig(BaseModel):
    preferred: str = "auto"
    reuse_host_model: bool = True
    standalone_api_optional: bool = True


class NotificationsConfig(BaseModel):
    use_host_connectors: bool = True
    urgent_enabled: bool = True


class ReportsConfig(BaseModel):
    daily_enabled: bool = True
    daily_time: str = "21:00"


class StorageConfig(BaseModel):
    backend: str = "sqlite"
    backups_enabled: bool = True
    db_path: str = ".offerbomber/offerbomber.db"


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    resume: ResumeConfig = Field(default_factory=ResumeConfig)
    applications: ApplicationsConfig = Field(default_factory=ApplicationsConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

    @field_validator("app")
    @classmethod
    def _check_timezone(cls, value: AppConfig) -> AppConfig:
        # ZoneInfo raises ZoneInfoNotFoundError on an invalid timezone name.
        from zoneinfo import ZoneInfo

        ZoneInfo(value.timezone)
        return value


def load_settings(path: str | Path) -> Settings:
    """Load YAML config from *path*. A missing file raises FileNotFoundError."""
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping: {config_path}")
    return Settings.model_validate(raw)


def write_default_config(path: str | Path, source: str | Path | None = None) -> Path:
    """Create a config file at *path*, optionally copying from a template.

    Used by `offerbomber init`. Existing files are never overwritten.
    """
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"config already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    template = Path(source) if source else Path(__file__).resolve().parents[2] / "config.example.yaml"
    if not template.is_file():
        raise FileNotFoundError(f"config template not found: {template}")
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return target
