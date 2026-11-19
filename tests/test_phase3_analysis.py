"""Part C-D: model resume parsing and JD analysis with truthfulness guards."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from offerbomber.core.job_analysis import DeterministicJobAnalyzer, JobAnalyzer, JobRequirements
from offerbomber.models.mock import MockProvider
from offerbomber.models.base import TaskType
from offerbomber.profile.model_parser import ModelResumeParser, text_quality
from offerbomber.profile.pdf import PDFDocument
from offerbomber.profile.schema import CandidateFact, CandidateExperience, CandidateProfile
from offerbomber.storage.models import Job


def _job(**kwargs) -> Job:
    job = Job(
        external_id="job-1", company="Acme", normalized_company="acme",
        title="Machine Learning Engineer", normalized_title="machine learning engineer",
        source_platform="greenhouse", discovered_at=datetime.now(timezone.utc).replace(tzinfo=None),
        fingerprint="x",
    )
    for key, value in kwargs.items():
        setattr(job, key, value)
    return job


def test_deterministic_job_analyzer_basic():
    job = _job(description="Build PyTorch models. Python required.", education_requirement="MSc CS")
    result = DeterministicJobAnalyzer().analyze(job)
    assert result.job_id is None or isinstance(result.job_id, int)
    assert result.analysis_provider == "deterministic_fallback"
    assert result.education_requirements


@pytest.mark.asyncio
async def test_job_analyzer_keeps_separate_levels_and_evidence():
    payload = {
        "job_id": 0,
        "required_skills": [
            {"text": "Python", "evidence": "Python required", "level": "MUST"},
            {"text": "Go", "evidence": "Go is a plus", "level": "NICE_TO_HAVE"},
        ],
        "important_keywords": ["Python", "Go"],
        "resume_language": "en",
    }
    provider = MockProvider(responses={"JOB_ANALYSIS": payload})
    job = _job(description="Python required. Go is a plus.")
    job.id = 42
    result = await JobAnalyzer(provider).analyze(job)
    assert result.job_id == 42
    assert [r.level for r in result.required_skills] == ["MUST", "NICE_TO_HAVE"]
    assert result.required_skills[0].evidence in job.description
    assert result.analysis_provider == "mock"


@pytest.mark.asyncio
async def test_job_analyzer_marks_bad_evidence_unknown():
    payload = {
        "job_id": 0,
        "required_skills": [{"text": "Rust", "evidence": "NOT IN JD", "level": "MUST"}],
    }
    provider = MockProvider(responses={"JOB_ANALYSIS": payload})
    job = _job(description="Python required.")
    job.id = 7
    result = await JobAnalyzer(provider).analyze(job)
    assert result.required_skills[0].level == "UNKNOWN"
    assert result.warnings


@pytest.mark.asyncio
async def test_job_analyzer_falls_back_on_model_error():
    from offerbomber.models.base import ModelErrorKind
    provider = MockProvider(raise_kind=ModelErrorKind.PROVIDER)
    result = await JobAnalyzer(provider).analyze(_job(description="PyTorch modeling"))
    assert result.analysis_provider == "deterministic_fallback"
    assert any("fallback" in w for w in result.warnings)


def test_text_quality_rejects_scanned():
    doc = PDFDocument("s.pdf", "a" * 64, 1, ("",), "", False)
    ok, warnings = text_quality(doc)
    assert not ok and warnings


@pytest.mark.asyncio
async def test_model_parser_drops_fabricated_metric():
    candidate = CandidateProfile(
        display_name="Jane",
        experiences=[CandidateExperience(
            title="Researcher",
            facts=[
                CandidateFact(content="Improved accuracy by 99% on all benchmarks", category="RESULT", locator="p1"),
                CandidateFact(content="Built a data pipeline with PyTorch", category="METHOD", locator="p1"),
            ],
        )],
    )
    provider = MockProvider(responses={"RESUME_EXTRACTION": candidate.model_dump()})
    doc = PDFDocument("r.pdf", "a" * 64, 1, ("Jane Doe. Built a data pipeline with PyTorch. " * 5,), "Jane Doe. Built a data pipeline with PyTorch. " * 5, True)
    result = await ModelResumeParser(provider).parse(doc)
    assert any("unverifiable quantitative" in w for w in result.warnings)
    assert len(result.experiences[0].facts) == 1
    assert result.parser.startswith("model:mock")


@pytest.mark.asyncio
async def test_model_parser_keeps_submitted_not_accepted():
    candidate = CandidateProfile(
        experiences=[CandidateExperience(
            title="Paper",
            facts=[CandidateFact(content="Paper accepted at ICML", category="PAPER", locator="p1")],
        )],
    )
    provider = MockProvider(responses={"RESUME_EXTRACTION": candidate.model_dump()})
    source = "Our manuscript was submitted to ICML and is under review. " * 6
    doc = PDFDocument("r.pdf", "a" * 64, 1, (source,), source, True)
    result = await ModelResumeParser(provider).parse(doc)
    assert result.experiences[0].facts == []
    assert any("submitted but model claimed accepted" in w for w in result.warnings)
