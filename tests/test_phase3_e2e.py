"""Part I.5: end-to-end scenarios A-D with Mock provider and isolated DB.

These are LOCAL SIMULATION tests. No real host model or Reactive Resume call is
performed; results are explicitly labelled mock.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from offerbomber.core.job_analysis import JobAnalyzer
from offerbomber.core.reranking import ModelReranker
from offerbomber.core.retrieval import FactRetrievalService, RetrievalQuery
from offerbomber.core.smart_planning import PlanDecision, SmartResumePlanner
from offerbomber.models.base import ModelErrorKind
from offerbomber.models.mock import MockProvider
from offerbomber.profile.service import ProfileKnowledgeBase
from offerbomber.storage.models import Job


def _seed_kb(session_factory) -> ProfileKnowledgeBase:
    kb = ProfileKnowledgeBase(session_factory, "tester")
    kb.upsert_profile({"display_name": "Jane Doe", "contact": "jane@example.com", "target_position": "ML Engineer"})
    kb.add_experience(
        {"type": "RESEARCH", "title": "Pretraining Research", "description": "foundation models"},
        facts=[
            {"content": "Trained a 7B language model using distributed PyTorch", "category": "METHOD", "skills": ["PyTorch", "Python"]},
            {"content": "Evaluated data mixtures on downstream benchmarks", "category": "EXPERIMENT", "skills": ["Python"]},
            {"content": "Paper submitted to ICML and under review", "category": "PAPER", "skills": ["Research"]},
            {"content": "Built a Sharpe ratio backtesting framework for equities", "category": "METHOD", "skills": ["Python", "Quant"]},
        ],
        confirm_facts=True,
    )
    return kb


def _job(title: str, description: str) -> Job:
    return Job(
        external_id="e2e", company="Acme", normalized_company="acme",
        title=title, normalized_title=title.lower(),
        source_platform="greenhouse", discovered_at=datetime.now(timezone.utc).replace(tzinfo=None),
        fingerprint="e2e", description=description,
    )


async def _plan_for(job: Job, session_factory, provider: MockProvider, text: str):
    requirements = await JobAnalyzer(provider).analyze(job)
    retrieval = FactRetrievalService(session_factory, "tester")
    query = RetrievalQuery(text=text, direction=job.title, max_results=30, verified_only=True)
    candidates = retrieval.search(query)
    ranked = await ModelReranker(provider).rerank(query, candidates, requirements)
    with session_factory() as session:
        from offerbomber.storage.models import Profile, Education
        profile = session.execute(select(Profile)).scalar_one_or_none()
        educations = session.execute(select(Education)).scalars().all()
    return await SmartResumePlanner(provider, session_factory).plan(
        profile=profile, educations=list(educations), ranked_facts=ranked,
        requirements=requirements, language="en", target_page_count=1,
    )


@pytest.mark.asyncio
async def test_scenario_a_ml_job_selects_ml_facts(session_factory):
    _seed_kb(session_factory)
    provider = MockProvider(
        responses={
            "JOB_ANALYSIS": {"job_id": 0, "job_category": "ML", "resume_language": "en"},
            "EXPERIENCE_RERANKING": {"items": []},
        }
    )
    job = _job("ML Engineer", "PyTorch pretraining and model evaluation")
    plan = await _plan_for(job, session_factory, provider, "PyTorch pretraining evaluation")
    assert plan.selected_fact_ids
    assert all(a.original_text for a in plan.fact_audit)


@pytest.mark.asyncio
async def test_scenario_b_different_job_different_facts(session_factory):
    _seed_kb(session_factory)
    text_ml = "distributed PyTorch language model"
    text_quant = "Sharpe ratio equities backtesting"
    provider_ml = MockProvider(responses={"EXPERIENCE_RERANKING": {"items": []}})
    provider_quant = MockProvider(responses={"EXPERIENCE_RERANKING": {"items": []}})
    ml_plan = await _plan_for(_job("ML Engineer", text_ml), session_factory, provider_ml, text_ml)
    quant_plan = await _plan_for(_job("Quant Researcher", text_quant), session_factory, provider_quant, text_quant)
    ml_ids = set(ml_plan.selected_fact_ids)
    quant_ids = set(quant_plan.selected_fact_ids)
    assert ml_ids != quant_ids, "different job text should emphasize different facts"


@pytest.mark.asyncio
async def test_scenario_c_submission_never_becomes_acceptance(session_factory):
    _seed_kb(session_factory)
    payload = PlanDecision(
        selected_fact_ids=[3],
        rendered_facts=[{"fact_id": 3, "rendered_text": "Paper accepted at ICML"}],
    ).model_dump()
    provider = MockProvider(
        responses={
            "JOB_ANALYSIS": {"job_id": 0, "resume_language": "en"},
            "EXPERIENCE_RERANKING": {"items": []},
            "RESUME_PLANNING": payload,
        }
    )
    plan = await _plan_for(_job("Researcher", "icml research"), session_factory, provider, "ICML paper")
    texts = [item.text for section in plan.sections for item in section.items]
    assert all("accepted" not in t.lower() for t in texts)
    assert any("acceptance" in w for w in plan.warnings)


@pytest.mark.asyncio
async def test_scenario_d_service_failure_preserves_data(session_factory):
    kb = _seed_kb(session_factory)
    before = kb.detail(kb.list_experiences()[0]["id"])
    provider = MockProvider(raise_kind=ModelErrorKind.UNAVAILABLE)
    requirements_result = await JobAnalyzer(provider).analyze(_job("ML", "ml"))
    assert requirements_result.analysis_provider == "deterministic_fallback"
    after = kb.detail(kb.list_experiences()[0]["id"])
    assert before == after  # no data damage from model failure
