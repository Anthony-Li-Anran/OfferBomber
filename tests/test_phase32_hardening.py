"""Phase 3.2 production-hardening regression tests.

Covers: FactAudit/body/snapshot text identity, untrusted-skill rejection,
research vs publication classification, publication-status preservation,
target-position resolution and Reactive Resume authenticated health checks.
All HTTP is mocked; nothing here is a real model or real API call.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
import respx

from offerbomber.core.job_analysis import JobRequirements
from offerbomber.core.resume_plan import FactAudit, ResumePlan, ResumeSection, ResumeSectionItem
from offerbomber.core.retrieval import FactMatch
from offerbomber.core.smart_planning import PlanDecision, SmartResumePlanner
from offerbomber.integrations.reactive_resume import ReactiveResumeProvider
from offerbomber.integrations.schema_mapper import (
    _publication_status,
    map_resume_data,
)
from offerbomber.models.mock import MockProvider
from offerbomber.storage.models import Education, Experience, Profile

BASE = "https://rxresu.me/api/openapi"


def _fact(fid: int, exp: int, content: str, skills=("Python",), exp_type="PROJECT") -> FactMatch:
    return FactMatch(
        experience_id=exp, fact_id=fid,
        experience_title=f"Experience {exp}", experience_type=exp_type,
        fact_content=content, category="METHOD", skills=tuple(skills),
        source_type="manual", source_locator="p1:L1", file_sha256="a" * 64,
        verification_status="CONFIRMED", score=1.0, reasons=(),
    )


REQS = JobRequirements(
    job_id=1, job_title="Machine Learning Engineer", job_category="ML",
    resume_language="zh", seniority="intern",
)


# ---- P0: FactAudit / body / snapshot text identity -----------------------


@pytest.mark.asyncio
async def test_rejected_rewrite_audit_matches_body_exactly():
    facts = [
        _fact(1, 10, "Improved accuracy by 12% on validation set"),
        _fact(2, 20, "Paper submitted to ICML and under review"),
    ]
    payload = PlanDecision(
        selected_fact_ids=[1, 2],
        rendered_facts=[
            {"fact_id": 1, "rendered_text": "Improved accuracy by 99% on validation set"},
            {"fact_id": 2, "rendered_text": "Paper accepted at ICML"},
        ],
    ).model_dump()
    provider = MockProvider(responses={"RESUME_PLANNING": payload})
    plan = await SmartResumePlanner(provider).plan(
        profile=None, educations=[], ranked_facts=facts, requirements=REQS,
    )
    body = {item.fact_id: item.text for section in plan.sections for item in section.items}
    audit = {a.fact_id: a.rendered_text for a in plan.fact_audit}
    # No fabricated number or acceptance claim survives into either place.
    assert body == {1: "Improved accuracy by 12% on validation set",
                    2: "Paper submitted to ICML and under review"}
    assert audit == body, "FactAudit.rendered_text must equal the body text exactly"
    # fact_snapshot is built from fact_audit — verify that serialization too.
    snapshot = json.loads(json.dumps([a.model_dump() for a in plan.fact_audit]))
    assert {row["fact_id"]: row["rendered_text"] for row in snapshot} == body
    assert all("99%" not in a and "accepted" not in a.lower() for a in body.values())


@pytest.mark.asyncio
async def test_accepted_rewrite_audit_matches_body_exactly():
    facts = [_fact(1, 10, "Built a data pipeline with PyTorch")]
    payload = PlanDecision(
        selected_fact_ids=[1],
        rendered_facts=[{"fact_id": 1, "rendered_text": "Built a data pipeline using PyTorch"}],
    ).model_dump()
    provider = MockProvider(responses={"RESUME_PLANNING": payload})
    plan = await SmartResumePlanner(provider).plan(
        profile=None, educations=[], ranked_facts=facts, requirements=REQS,
    )
    body = plan.sections[0].items[0].text
    assert body == "Built a data pipeline using PyTorch"
    assert plan.fact_audit[0].rendered_text == body


def test_pdf_content_uses_audit_text_not_model_text():
    """The text mapped into ResumeData (and thus the PDF) comes from the body,
    which now equals the audited final text."""
    original = "Paper submitted to ICML and under review"
    plan = ResumePlan(
        target_position="ML Engineer",
        sections=[ResumeSection(title="Research", items=[
            ResumeSectionItem(experience_id=20, fact_id=2, text=original),
        ])],
        fact_audit=[FactAudit(
            fact_id=2, original_text=original, rendered_text=original, experience_id=20,
        )],
    )
    experiences = {20: Experience(
        id=20, user_id="u", type="PAPER", title="A Study", current_version=1,
    )}
    data = map_resume_data(plan=plan, profile=None, educations=[], experiences=experiences)
    pubs = data["sections"]["publications"]["items"]
    assert pubs and original in pubs[0]["description"]
    assert "Published" not in pubs[0]["description"]


# ---- P0: untrusted skills -------------------------------------------------


@pytest.mark.asyncio
async def test_model_cannot_invent_skills_in_plan():
    facts = [_fact(1, 10, "Built a model", skills=("Python", "PyTorch"))]
    payload = PlanDecision(
        selected_fact_ids=[1],
        highlight_skills=["Python", "QuantumTeleportation", "FakeSkill123"],
    ).model_dump()
    provider = MockProvider(responses={"RESUME_PLANNING": payload})
    plan = await SmartResumePlanner(provider).plan(
        profile=None, educations=[], ranked_facts=facts, requirements=REQS,
    )
    assert "Python" in plan.highlight_skills
    assert "QuantumTeleportation" not in plan.highlight_skills
    assert "FakeSkill123" not in plan.highlight_skills
    assert any("unverifiable" in w for w in plan.warnings)


def test_model_cannot_invent_skills_in_final_pdf_data():
    """Even a plan carrying a fabricated highlight cannot leak it into ResumeData."""
    plan = ResumePlan(
        target_position="ML Engineer",
        sections=[ResumeSection(title="Work", items=[
            ResumeSectionItem(experience_id=1, fact_id=1, text="Built a model", skills=["Python"]),
        ])],
        highlight_skills=["Python", "ForgedSkill"],
    )
    experiences = {1: Experience(
        id=1, user_id="u", type="WORK", title="Engineer", current_version=1,
    )}
    data = map_resume_data(plan=plan, profile=None, educations=[], experiences=experiences)
    keywords = data["sections"]["skills"]["items"][0]["keywords"]
    assert "Python" in keywords
    assert "ForgedSkill" not in keywords


# ---- P1: research vs publication classification --------------------------


def test_research_is_not_a_publication_and_paper_keeps_status():
    plan = ResumePlan(
        target_position="Researcher",
        sections=[
            ResumeSection(title="Research project", items=[
                ResumeSectionItem(experience_id=1, fact_id=1, text="Built analysis framework"),
            ]),
            ResumeSection(title="Paper", items=[
                ResumeSectionItem(
                    experience_id=2, fact_id=2,
                    text="Paper submitted to ICML and under review",
                ),
            ]),
        ],
    )
    experiences = {
        1: Experience(id=1, user_id="u", type="RESEARCH", title="AutoSTAT framework", current_version=1),
        2: Experience(id=2, user_id="u", type="PAPER", title="A Study", current_version=1),
    }
    data = map_resume_data(plan=plan, profile=None, educations=[], experiences=experiences)
    projects = data["sections"]["projects"]["items"]
    pubs = data["sections"]["publications"]["items"]
    assert any("AutoSTAT" in p["name"] for p in projects), "RESEARCH must be a project, not a publication"
    assert all("AutoSTAT" not in p["title"] for p in pubs)
    assert len(pubs) == 1
    assert "Submitted / under review" in pubs[0]["description"]
    assert "Published" not in pubs[0]["description"].replace("Publication status: Published", "")


def test_publication_status_never_upgrades_submission():
    assert _publication_status("submitted to ICML and under review") == "SUBMITTED"
    assert _publication_status("投稿 UAI 2027，在审") == "SUBMITTED"
    assert _publication_status("Paper accepted at NeurIPS") == "ACCEPTED"
    assert _publication_status("已发表在 JMLR") == "PUBLISHED"
    assert _publication_status("Wrote a research report") == "UNKNOWN"


def test_work_internship_project_award_go_to_correct_sections():
    plan = ResumePlan(
        target_position="Engineer",
        sections=[
            ResumeSection(title="w", items=[ResumeSectionItem(experience_id=1, fact_id=1, text="a")]),
            ResumeSection(title="i", items=[ResumeSectionItem(experience_id=2, fact_id=2, text="b")]),
            ResumeSection(title="p", items=[ResumeSectionItem(experience_id=3, fact_id=3, text="c")]),
            ResumeSection(title="a", items=[ResumeSectionItem(experience_id=4, fact_id=4, text="d")]),
        ],
    )
    experiences = {
        1: Experience(id=1, user_id="u", type="WORK", title="Engineer", current_version=1),
        2: Experience(id=2, user_id="u", type="INTERNSHIP", title="Intern", current_version=1),
        3: Experience(id=3, user_id="u", type="PROJECT", title="Side Project", current_version=1),
        4: Experience(id=4, user_id="u", type="AWARD", title="Prize", current_version=1),
    }
    data = map_resume_data(plan=plan, profile=None, educations=[], experiences=experiences)
    assert len(data["sections"]["experience"]["items"]) == 2
    assert len(data["sections"]["projects"]["items"]) == 1
    assert len(data["sections"]["awards"]["items"]) == 1
    assert len(data["sections"]["publications"]["items"]) == 0


# ---- P1: target position --------------------------------------------------


@pytest.mark.asyncio
async def test_target_position_uses_job_title_not_language_or_category():
    profile = Profile(user_id="u", display_name="Test User", target_position="Default Role")
    facts = [_fact(1, 10, "Built a model")]
    provider = MockProvider(responses={"RESUME_PLANNING": {"selected_fact_ids": [1]}})
    plan = await SmartResumePlanner(provider).plan(
        profile=profile, educations=[], ranked_facts=facts,
        requirements=REQS, language="zh",
    )
    assert plan.target_position == "Machine Learning Engineer"
    assert plan.target_position != REQS.resume_language
    assert plan.target_position != REQS.job_category


@pytest.mark.asyncio
async def test_target_position_falls_back_to_profile_without_job():
    profile = Profile(user_id="u", display_name="Test User", target_position="Default Role")
    facts = [_fact(1, 10, "Built a model")]
    provider = MockProvider(responses={"RESUME_PLANNING": {"selected_fact_ids": [1]}})
    plan = await SmartResumePlanner(provider).plan(
        profile=profile, educations=[], ranked_facts=facts, requirements=None,
    )
    assert plan.target_position == "Default Role"


def test_job_requirements_carries_original_title():
    assert REQS.job_title == "Machine Learning Engineer"


# ---- P1: authenticated health check --------------------------------------


@pytest.mark.asyncio
async def test_health_ok_with_custom_base_url():
    custom = "https://custom.example.com/api/openapi"
    provider = ReactiveResumeProvider(api_key="test-key", base_url=custom)
    with respx.mock as router:
        router.get("https://custom.example.com/api/health").mock(
            return_value=httpx.Response(200, json={"status": "healthy"})
        )
        router.get(f"{custom}/resumes").mock(
            return_value=httpx.Response(200, json=[{"id": "r1"}])
        )
        result = await provider.health_check()
    assert result["code"] == "ok"
    assert result["available"] is True
    assert result["resume_count"] == 1


@pytest.mark.asyncio
async def test_health_auth_failed_401():
    provider = ReactiveResumeProvider(api_key="bad-key", base_url=BASE)
    with respx.mock as router:
        router.get(f"{BASE[:-8]}/health").mock(return_value=httpx.Response(200, json={}))
        router.get(f"{BASE}/resumes").mock(
            return_value=httpx.Response(401, json={"code": "UNAUTHORIZED", "message": "bad key"})
        )
        result = await provider.health_check()
    assert result["code"] == "auth_failed"
    assert result["available"] is False
    assert "bad-key" not in result["detail"]


@pytest.mark.asyncio
async def test_health_rate_limited_429():
    provider = ReactiveResumeProvider(api_key="test-key", base_url=BASE)
    with respx.mock as router:
        router.get(f"{BASE[:-8]}/health").mock(return_value=httpx.Response(200, json={}))
        router.get(f"{BASE}/resumes").mock(return_value=httpx.Response(429, json={"code": "RATE_LIMIT"}))
        result = await provider.health_check()
    assert result["code"] == "rate_limited"
    assert result["available"] is False


@pytest.mark.asyncio
async def test_health_service_unavailable():
    provider = ReactiveResumeProvider(api_key="test-key", base_url=BASE)
    with respx.mock as router:
        router.get(f"{BASE[:-8]}/health").mock(return_value=httpx.Response(503, text="down"))
        result = await provider.health_check()
    assert result["code"] == "service_unavailable"
    assert result["available"] is False


@pytest.mark.asyncio
async def test_health_timeout_is_transport():
    provider = ReactiveResumeProvider(api_key="test-key", base_url=BASE)
    with respx.mock as router:
        router.get(f"{BASE[:-8]}/health").mock(side_effect=httpx.ConnectTimeout("timeout"))
        result = await provider.health_check()
    assert result["code"] == "transport"
    assert result["available"] is False


@pytest.mark.asyncio
async def test_health_without_key_is_unavailable():
    provider = ReactiveResumeProvider(api_key="", base_url=BASE)
    result = await provider.health_check()
    assert result["code"] == "unavailable"
    assert result["available"] is False
