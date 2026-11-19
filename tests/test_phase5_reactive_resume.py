"""Part G/I: Reactive Resume mapping, PDF validation and mock HTTP flow.

These are EXTERNAL-INTERFACE SIMULATION tests (respx mocks). They are NOT a
real Reactive Resume integration and do not claim live verification.
"""

from __future__ import annotations

import json

import pymupdf
import pytest
import respx
import httpx

from offerbomber.core.resume_plan import FactAudit, ResumePlan, ResumeSection, ResumeSectionItem
from offerbomber.integrations.reactive_resume import (
    ReactiveResumeProvider,
    ResumeGenerationError,
)
from offerbomber.integrations.schema_mapper import map_resume_data
from offerbomber.storage.models import Education, Experience, Profile

BASE = "https://rxresu.me/api/openapi"


def _plan() -> ResumePlan:
    item1 = ResumeSectionItem(
        experience_id=1, fact_id=10, text="Built a PyTorch pipeline",
        skills=["Python", "PyTorch"],
    )
    item2 = ResumeSectionItem(
        experience_id=2, fact_id=11, text="Submitted paper to ICML",
        skills=["Research"],
    )
    return ResumePlan(
        target_position="ML Engineer", language="en", target_page_count=1,
        selected_experience_ids=[1, 2], selected_fact_ids=[10, 11],
        sections=[
            ResumeSection(title="Research", items=[item1, item2]),
        ],
        highlight_skills=["Python"],
        fact_audit=[
            FactAudit(fact_id=10, original_text="Built a PyTorch pipeline", rendered_text="Built a PyTorch pipeline", experience_id=1),
            FactAudit(fact_id=11, original_text="Submitted paper to ICML", rendered_text="Submitted paper to ICML", experience_id=2),
        ],
        generation_provider="model:mock",
    )


def _profile() -> Profile:
    return Profile(user_id="u", display_name="Jane Doe", contact="jane@example.com", summary="ML researcher")


def _experiences() -> dict[int, Experience]:
    return {
        1: Experience(id=1, user_id="u", type="PROJECT", title="Side Project", organization=None, current_version=1),
        2: Experience(id=2, user_id="u", type="PAPER", title="A Study", organization="University", current_version=1),
    }


def test_schema_mapper_preserves_provenance_and_types():
    data = map_resume_data(
        plan=_plan(), profile=_profile(),
        educations=[Education(id=1, profile_id=1, school="PKU", degree="MSc", major="CS")],
        experiences=_experiences(),
    )
    assert data["basics"]["name"] == "Jane Doe"
    assert data["basics"]["email"] == "jane@example.com"
    exp_items = data["sections"]["experience"]["items"]
    proj_items = data["sections"]["projects"]["items"]
    pub_items = data["sections"]["publications"]["items"]
    # Personal project must NOT be presented as company experience.
    assert len(exp_items) == 0
    assert len(proj_items) == 1 and "company" not in proj_items[0]
    assert "Built a PyTorch pipeline" in proj_items[0]["description"]
    assert len(pub_items) == 1 and "Submitted paper to ICML" in pub_items[0]["description"]
    assert data["sections"]["education"]["items"][0]["school"] == "PKU"
    assert "Python" in data["sections"]["skills"]["items"][0]["keywords"]


def test_validate_pdf_with_real_file(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Jane Doe", fontsize=14)
    for i in range(3):
        page.insert_text((72, 100 + i * 14), "Built a PyTorch pipeline for research", fontsize=10)
    path = tmp_path / "resume.pdf"
    doc.save(str(path))
    doc.close()

    provider = ReactiveResumeProvider()
    report = provider.validate_pdf(
        path, expected_name="Jane Doe", expected_terms=["PyTorch"]
    )
    assert report.valid is True
    assert report.checks["has_text"] and report.checks["name_present"]

    bad = provider.validate_pdf(tmp_path / "missing.pdf")
    assert bad.valid is False


def test_no_api_key_fails_explicitly():
    provider = ReactiveResumeProvider(api_key="")
    assert provider.configured is False
    with pytest.raises(ResumeGenerationError) as exc:
        import asyncio
        asyncio.run(provider.list_resumes(httpx.AsyncClient()))
    assert exc.value.code == "unavailable"


@pytest.mark.asyncio
async def test_mock_http_generate_clones_never_touches_source(tmp_path):
    provider = ReactiveResumeProvider(api_key="test-key", base_url=BASE)
    source_doc = {
        "id": "source-1", "name": "Main Resume",
        "data": {"basics": {"name": "Original"}, "sections": {}, "metadata": {"template": "onboarding"}, "design": {"font": "serif"}},
    }
    pdf = pymupdf.open(); p = pdf.new_page(); p.insert_text((72, 72), "Jane Doe")
    for i, line in enumerate(["Built a PyTorch pipeline for research", "Submitted paper to ICML and under review"]):
        p.insert_text((72, 90 + i * 14), line)
    pdf_bytes_path = tmp_path / "src.pdf"; pdf.save(str(pdf_bytes_path)); pdf.close()
    pdf_bytes = pdf_bytes_path.read_bytes()

    clone_doc = {"id": "clone-2", "name": "clone", "data": dict(source_doc["data"])}
    with respx.mock:
        respx.post(f"{BASE}/resumes/source-1/copies").mock(return_value=httpx.Response(201, json={"id": "clone-2"}))
        respx.get(f"{BASE}/resumes/clone-2").mock(return_value=httpx.Response(200, json=clone_doc))
        respx.put(f"{BASE}/resumes/clone-2").mock(return_value=httpx.Response(200, json={"id": "clone-2"}))
        respx.get(f"{BASE}/resumes/clone-2/pdf").mock(return_value=httpx.Response(200, content=pdf_bytes))

        version = await provider.generate(
            job_id=42, source_resume_id="source-1", plan=_plan(),
            profile=_profile(), educations=[], experiences=_experiences(),
            output_dir=tmp_path / "out",
        )

    assert version.job_id == 42
    assert len(version.file_sha256) == 64
    assert source_doc["data"]["basics"]["name"] == "Original"  # source untouched
    stored = json.loads(version.fact_snapshot)
    assert [row["fact_id"] for row in stored] == [10, 11]


@pytest.mark.asyncio
async def test_export_failure_produces_no_file(tmp_path):
    provider = ReactiveResumeProvider(api_key="test-key", base_url=BASE)
    with respx.mock:
        respx.post(f"{BASE}/resumes/source-1/copies").mock(return_value=httpx.Response(201, json={"id": "clone-3"}))
        respx.get(f"{BASE}/resumes/clone-3").mock(return_value=httpx.Response(200, json={"id": "clone-3", "data": {}}))
        respx.put(f"{BASE}/resumes/clone-3").mock(return_value=httpx.Response(200, json={}))
        respx.get(f"{BASE}/resumes/clone-3/pdf").mock(return_value=httpx.Response(502, json={"code": "UPSTREAM", "message": "fail"}))
        with pytest.raises(ResumeGenerationError) as exc:
            await provider.generate(
                job_id=43, source_resume_id="source-1", plan=_plan(),
                profile=_profile(), educations=[], experiences=_experiences(),
                output_dir=tmp_path / "out",
            )
    assert exc.value.code == "UPSTREAM"
