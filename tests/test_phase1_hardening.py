"""Regression tests for Phase 1 hardening: Unicode dedup and failure isolation."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from offerbomber.config import CompanySource
from offerbomber.core.dedup import JobDeduplicator
from offerbomber.core.discovery_service import DiscoveryService
from offerbomber.discovery.base import RawJob, normalize_key
from offerbomber.discovery.http import HttpFetcher, RetryPolicy
from offerbomber.storage.repositories import insert_job

from .conftest import GREENHOUSE_PAYLOAD, LEVER_PAYLOAD


def _raw(company: str, title: str, eid: str) -> RawJob:
    return RawJob(
        source_platform="greenhouse",
        source_key="cn",
        external_id=eid,
        company=company,
        title=title,
        description="研究大模型预训练",
        location="北京",
    )


def test_normalize_key_preserves_chinese_and_unicode_case():
    assert normalize_key("字节跳动") == "字节跳动"
    assert normalize_key("字 节 跳 动") == "字节跳动"
    assert normalize_key("字节跳动（北京）") == "字节跳动北京"
    assert normalize_key("UBER Technologies") == "uber technologies"
    assert normalize_key(" Straße  ") == "strasse"
    assert normalize_key("字节跳动") != normalize_key("字节互联")


def test_chinese_jobs_from_different_companies_are_not_collapsed(session_factory):
    with session_factory() as s:
        insert_job(s, _raw("字节跳动", "机器学习实习生", "1"))
        insert_job(s, _raw("腾讯科技", "机器学习实习生", "2"))
        s.commit()
        dedup = JobDeduplicator(s)
        assert dedup.find(_raw("字节跳动", "机器学习工程师", "3")).job is None
        assert dedup.find(_raw("字节跳动", "机器学习实习生", "4")).job is not None


def test_malformed_source_does_not_abort_other_sources(settings, session_factory):
    service = DiscoveryService(settings, session_factory)
    service.fetcher = HttpFetcher(retry=RetryPolicy(max_attempts=1, base_delay=0.001, jitter=0))

    async def _run():
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
                return_value=httpx.Response(200, json={"unexpected": "shape"})
            )
            respx.get("https://api.lever.co/v0/postings/hedge?mode=json").mock(
                return_value=httpx.Response(200, json=LEVER_PAYLOAD)
            )
            stats = await service.run(
                [
                    CompanySource(platform="greenhouse", token="acme", name="Acme"),
                    CompanySource(platform="lever", token="hedge", name="Hedge"),
                ]
            )
        await service.aclose()
        return stats

    stats = asyncio.run(_run())
    assert stats.sources_failed == 1
    assert stats.sources_succeeded == 1
    assert stats.new_jobs == 2
    assert any("payload parse error" in err for err in stats.errors)
    assert stats.as_dict()["fully_succeeded"] is False


def test_one_bad_record_does_not_drop_other_records(settings, session_factory, monkeypatch):
    import offerbomber.core.discovery_service as module

    original = module.repo.insert_job
    calls = {"n": 0}

    def flaky(session, raw):
        calls["n"] += 1
        if raw.external_id == "101":
            raise RuntimeError("simulated bad record")
        return original(session, raw)

    monkeypatch.setattr(module.repo, "insert_job", flaky)
    service = DiscoveryService(settings, session_factory)
    service.fetcher = HttpFetcher(retry=RetryPolicy(max_attempts=1, base_delay=0.001, jitter=0))

    async def _run():
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
                return_value=httpx.Response(200, json=GREENHOUSE_PAYLOAD)
            )
            stats = await service.run(
                [CompanySource(platform="greenhouse", token="acme", name="Acme")]
            )
        await service.aclose()
        return stats

    stats = asyncio.run(_run())
    assert stats.record_errors == 1
    assert stats.new_jobs == 1
    assert stats.sources_partial == 1
    assert stats.as_dict()["fully_succeeded"] is False
