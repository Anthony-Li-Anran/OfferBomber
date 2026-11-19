"""Part E-F: model reranking and truth-preserving intelligent planning."""

from __future__ import annotations

from dataclasses import replace

import pytest

from offerbomber.core.job_analysis import JobRequirements, Requirement
from offerbomber.core.resume_plan import ResumePlan
from offerbomber.core.retrieval import FactMatch, RetrievalQuery
from offerbomber.core.reranking import ModelReranker, RerankItem, RerankResult
from offerbomber.core.smart_planning import PlanDecision, SmartResumePlanner
from offerbomber.models.base import ModelErrorKind
from offerbomber.models.mock import MockProvider


def _fact(fid: int, exp: int, content: str, score: float = 1.0) -> FactMatch:
    return FactMatch(
        experience_id=exp, fact_id=fid,
        experience_title=f"Experience {exp}", experience_type="PROJECT",
        fact_content=content, category="METHOD", skills=("Python",),
        source_type="manual", source_locator=None, file_sha256=None,
        verification_status="CONFIRMED", score=score, reasons=(),
    )


REQS = JobRequirements(
    job_id=1, job_category="ML", seniority="intern",
    required_skills=[Requirement(text="Python", evidence="Python", level="MUST")],
    important_keywords=["Python"],
)


@pytest.mark.asyncio
async def test_rerank_uses_only_real_fact_ids():
    candidates = [_fact(1, 10, "built models", 1.0), _fact(2, 20, "wrote docs", 5.0)]
    payload = RerankResult(items=[
        RerankItem(fact_id=1, relevance_score=10.0, relevance_reason="directly relevant"),
        RerankItem(fact_id=999, relevance_score=99.0, relevance_reason="invented"),
    ]).model_dump()
    provider = MockProvider(responses={"EXPERIENCE_RERANKING": payload})
    result = await ModelReranker(provider).rerank(RetrievalQuery(text="ml"), candidates, REQS)
    assert [m.fact_id for m in result][:2] == [1, 2]
    assert all(m.fact_id != 999 for m in result)
    assert result[0].fact_content == "built models"  # text unchanged


@pytest.mark.asyncio
async def test_rerank_failure_keeps_deterministic_order():
    provider = MockProvider(raise_kind=ModelErrorKind.PROVIDER)
    candidates = [_fact(1, 10, "a"), _fact(2, 20, "b")]
    result = await ModelReranker(provider).rerank(RetrievalQuery(text="x"), candidates, REQS)
    assert [m.fact_id for m in result] == [1, 2]


@pytest.mark.asyncio
async def test_rerank_excludes_unconfirmed():
    unconfirmed = replace(_fact(3, 30, "maybe"), verification_status="UNVERIFIED")
    provider = MockProvider(responses={"EXPERIENCE_RERANKING": {"items": []}})
    result = await ModelReranker(provider).rerank(RetrievalQuery(text="x"), [_fact(1, 10, "a"), unconfirmed], REQS)
    assert all(m.fact_id != 3 for m in result)


@pytest.mark.asyncio
async def test_planner_respects_budget_and_selected_ids():
    facts = [_fact(i, 10 + i, f"fact {i} with detail", 10 - i) for i in range(1, 6)]
    payload = PlanDecision(
        selected_fact_ids=[3, 1, 999, 2], experience_order=[12, 11],
        language="en", target_page_count=1, highlight_skills=["Python"],
    ).model_dump()
    provider = MockProvider(responses={"RESUME_PLANNING": payload})
    plan = await SmartResumePlanner(provider).plan(
        profile=None, educations=[], ranked_facts=facts, requirements=REQS,
        target_page_count=1,
    )
    assert set(plan.selected_fact_ids) == {1, 2, 3}  # 999 rejected, budget respected
    assert len(plan.selected_fact_ids) == 3
    assert plan.language == "en"
    assert plan.generation_provider == "model:mock"
    assert len(plan.fact_audit) == 3
    assert all(a.original_text.startswith("fact") for a in plan.fact_audit)


@pytest.mark.asyncio
async def test_planner_rejects_fabricated_number_and_acceptance():
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
    texts = {item.fact_id: item.text for section in plan.sections for item in section.items}
    assert texts[1] == "Improved accuracy by 12% on validation set"  # reverted
    assert texts[2] == "Paper submitted to ICML and under review"  # reverted
    assert any("new quantification" in w for w in plan.warnings)
    assert any("acceptance" in w for w in plan.warnings)


@pytest.mark.asyncio
async def test_planner_fallback_on_model_failure():
    provider = MockProvider(raise_kind=ModelErrorKind.TIMEOUT)
    facts = [_fact(1, 10, "a"), _fact(2, 20, "b")]
    plan = await SmartResumePlanner(provider).plan(
        profile=None, educations=[], ranked_facts=facts, requirements=REQS,
    )
    assert plan.selected_fact_ids == [1, 2]
    assert any("fallback" in w for w in plan.warnings)


@pytest.mark.asyncio
async def test_two_page_budget_larger_than_one():
    facts = [_fact(i, 10 + i, f"detail {i}") for i in range(1, 16)]
    provider = MockProvider()
    one = await SmartResumePlanner(provider).plan(
        profile=None, educations=[], ranked_facts=facts, requirements=REQS, target_page_count=1,
    )
    two = await SmartResumePlanner(provider).plan(
        profile=None, educations=[], ranked_facts=facts, requirements=REQS, target_page_count=2,
    )
    assert len(two.selected_fact_ids) > len(one.selected_fact_ids)
