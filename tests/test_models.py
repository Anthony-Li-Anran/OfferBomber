"""Part B/I: ModelProvider abstraction, validation, retries, safety, providers."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, Field

from offerbomber.models.base import (
    ModelError,
    ModelErrorKind,
    ModelProvider,
    RetryPolicy,
    TaskType,
    extract_json_block,
)
from offerbomber.models.deepseek_harness import DeepSeekHarnessProvider
from offerbomber.models.factory import auto_select, build_provider, is_real
from offerbomber.models.mock import MockProvider
from offerbomber.models.openai_compatible import OpenAICompatibleProvider
from offerbomber.models.workbuddy import WorkBuddyProvider


class Out(BaseModel):
    summary: str
    count: int = 0
    tags: list[str] = Field(default_factory=list)


MSG = [{"role": "user", "content": "analyze this"}]


@pytest.mark.asyncio
async def test_mock_returns_valid_structured_output():
    provider = MockProvider(responses={"JOB_ANALYSIS": {"summary": "ok", "count": 2, "tags": ["ml"]}})
    result = await provider.generate_structured(TaskType.JOB_ANALYSIS, MSG, Out)
    assert result.summary == "ok" and result.count == 2
    assert not is_real(provider)
    health = await provider.health_check()
    assert health.real_live_verified is False


@pytest.mark.asyncio
async def test_invalid_json_is_classified_and_retried():
    provider = MockProvider(invalid_json=True, retry=RetryPolicy(max_attempts=2, base_delay=0))
    with pytest.raises(ModelError) as exc:
        await provider.generate_structured(TaskType.JOB_ANALYSIS, MSG, Out)
    assert exc.value.kind == ModelErrorKind.INVALID_OUTPUT
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_timeout_is_classified():
    provider = MockProvider(delay=5.0, timeout=0.05, retry=RetryPolicy(max_attempts=1))
    with pytest.raises(ModelError) as exc:
        await provider.generate_structured(TaskType.JOB_ANALYSIS, MSG, Out)
    assert exc.value.kind == ModelErrorKind.TIMEOUT


@pytest.mark.asyncio
async def test_unrecoverable_error_stops_immediately():
    provider = MockProvider(
        raise_kind=ModelErrorKind.AUTH,
        retry=RetryPolicy(max_attempts=3, base_delay=0),
    )
    with pytest.raises(ModelError) as exc:
        await provider.generate_structured(TaskType.JOB_ANALYSIS, MSG, Out)
    assert exc.value.kind == ModelErrorKind.AUTH
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_cancellation_propagates():
    class Cancelling(ModelProvider):
        name = "cancel"

        async def _generate(self, task, messages, schema):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await Cancelling().generate_structured(TaskType.JOB_ANALYSIS, MSG, Out)


def test_extract_json_block_handles_prose_and_fences():
    text = 'Here you go:\n```json\n{"summary": "x", "count": 1}\n```\nThanks.'
    assert '"summary": "x"' in extract_json_block(text)


def test_all_six_task_types_exist():
    expected = {
        "RESUME_EXTRACTION", "JOB_ANALYSIS", "EXPERIENCE_RERANKING",
        "RESUME_PLANNING", "RESUME_REWRITING", "FACT_CONSISTENCY_CHECK",
    }
    assert {t.value for t in TaskType} == expected


@pytest.mark.asyncio
async def test_host_unavailable_reports_not_success():
    wb = WorkBuddyProvider(access_token="")
    health = await wb.health_check()
    assert health.available is False
    assert health.real_live_verified is False
    with pytest.raises(ModelError) as exc:
        await wb.generate_structured(TaskType.JOB_ANALYSIS, MSG, Out)
    assert exc.value.kind == ModelErrorKind.AUTH

    dsh = DeepSeekHarnessProvider(binary="definitely-not-a-real-binary-dsh")
    dsh_health = await dsh.health_check()
    assert dsh_health.available is False

    oai = OpenAICompatibleProvider(base_url="", api_key="", model="")
    oai_health = await oai.health_check()
    assert oai_health.available is False


@pytest.mark.asyncio
async def test_auto_select_never_claims_mock_is_real():
    provider = await auto_select()
    if provider.name == "mock":
        assert not is_real(provider)
    health = await provider.health_check()
    assert isinstance(health.real_live_verified, bool)


def test_factory_rejects_unknown_provider():
    with pytest.raises(ValueError):
        build_provider("not_a_provider")
