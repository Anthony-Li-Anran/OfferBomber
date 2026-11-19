"""Dedup, filters, persistence and full discovery-service tests."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from offerbomber.config import CompanySource
from offerbomber.core.dedup import JobDeduplicator
from offerbomber.core.discovery_service import DiscoveryService
from offerbomber.core.policies import HardFilter
from offerbomber.discovery.base import RawJob
from offerbomber.discovery.http import HttpFetcher, SourceFetchError, RetryPolicy
from offerbomber.storage.models import Job, JobSourceRecord
from sqlalchemy import select

from .conftest import ASHBY_PAYLOAD, GREENHOUSE_PAYLOAD, LEVER_PAYLOAD


def _raw(platform="greenhouse", token="acme", eid="1", title="ML Intern", desc="pretraining research"):
    return RawJob(
        source_platform=platform,
        source_key=token,
        external_id=eid,
        company="Acme",
        title=title,
        description=desc,
        location="Remote",
    )


def test_hard_filter_rejects_non_job_and_closed():
    f = HardFilter()
    assert f.evaluate(_raw(title="Job Fair 2026")).passed is False
    assert f.evaluate(_raw(desc="This position is closed")).passed is False
    assert f.evaluate(_raw()).passed is True


def test_broad_search_off_requires_target_overlap():
    f = HardFilter(broad_search=False, target_fields=["quantitative"])
    assert f.evaluate(_raw(desc="pretraining research")).passed is False
    assert f.evaluate(_raw(desc="quantitative research")).passed is True


def test_dedup_same_external_id(session_factory):
    with session_factory() as s:
        from offerbomber.storage.repositories import insert_job

        job = insert_job(s, _raw(eid="1", title="ML Intern"))
        s.commit()
        d = JobDeduplicator(s)
        result = d.find(_raw(eid="1", title="ML Intern"))
        assert result.job is not None and result.job.id == job.id
        assert "external id" in result.reason


def test_dedup_same_normalized_url(session_factory):
    with session_factory() as s:
        from offerbomber.storage.repositories import insert_job

        base = _raw(platform="lever", eid="a", title="Engineer")
        base.apply_url = None
        base.official_url = "https://jobs.lever.co/x/1?utm_source=google"
        job = insert_job(s, base)
        s.commit()
        d = JobDeduplicator(s)
        other = _raw(platform="lever", eid="b", title="Engineer")
        other.official_url = "https://jobs.lever.co/x/1"
        result = d.find(other)
        assert result.job is not None and result.job.id == job.id
        assert "URL" in result.reason


def test_dedup_description_similarity(session_factory):
    with session_factory() as s:
        from offerbomber.storage.repositories import insert_job

        original = _raw(platform="ashby", eid="url-1", title="ML Engineer")
        original.description = "We research large scale pretraining and posttraining methods for foundation models."
        job = insert_job(s, original)
        s.commit()
        d = JobDeduplicator(s)
        dupe = _raw(platform="ashby", eid="different-id", title="ML Engineer")
        dupe.location = "Remote"
        dupe.official_url = "https://other.example/job/2"
        dupe.description = "We research large scale pretraining and posttraining methods for foundation models!"
        result = d.find(dupe)
        assert result.job is not None and result.job.id == job.id
        assert "similarity" in result.reason


def test_distinct_jobs_not_collapsed(session_factory):
    with session_factory() as s:
        from offerbomber.storage.repositories import insert_job

        insert_job(s, _raw(eid="1", title="ML Intern", desc="pretraining"))
        insert_job(s, _raw(eid="2", title="Backend Intern", desc="distributed systems"))
        s.commit()
        d = JobDeduplicator(s)
        assert d.find(_raw(eid="1", title="ML Intern", desc="pretraining")).job is not None
        assert d.find(_raw(eid="2", title="Backend Intern", desc="distributed systems")).job is not None


def test_http_retry_then_success():
    async def _run():
        fetcher = HttpFetcher(retry=RetryPolicy(max_attempts=3, base_delay=0.001, jitter=0))
        with respx.mock:
            route = respx.get("https://api.example/jobs").mock(
                side_effect=[httpx.Response(503), httpx.Response(200, json={"jobs": []})]
            )
            payload = await fetcher.get_json("https://api.example/jobs")
            assert route.call_count == 2
            assert payload == {"jobs": []}
        await fetcher.aclose()

    asyncio.run(_run())


def test_http_gives_up_after_bounded_attempts():
    async def _run():
        fetcher = HttpFetcher(retry=RetryPolicy(max_attempts=3, base_delay=0.001, jitter=0))
        with respx.mock:
            route = respx.get("https://api.example/fail").mock(return_value=httpx.Response(500))
            with pytest.raises(SourceFetchError):
                await fetcher.get_json("https://api.example/fail")
            assert route.call_count == 3
        await fetcher.aclose()

    asyncio.run(_run())


def test_http_4xx_is_not_retried():
    async def _run():
        fetcher = HttpFetcher(retry=RetryPolicy(max_attempts=3, base_delay=0.001, jitter=0))
        with respx.mock:
            route = respx.get("https://api.example/denied").mock(return_value=httpx.Response(403))
            with pytest.raises(SourceFetchError):
                await fetcher.get_json("https://api.example/denied")
            assert route.call_count == 1
        await fetcher.aclose()

    asyncio.run(_run())


def test_full_discovery_run_persists_and_ignores_duplicates(settings, session_factory):
    """End-to-end: 3 sources -> filtered -> dedup -> DB, run twice, no duplicate rows."""
    service = DiscoveryService(settings, session_factory)
    service.fetcher = HttpFetcher(retry=RetryPolicy(max_attempts=1, base_delay=0.001, jitter=0))

    async def _run():
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
                return_value=httpx.Response(200, json=GREENHOUSE_PAYLOAD)
            )
            respx.get("https://api.lever.co/v0/postings/hedge?mode=json").mock(
                return_value=httpx.Response(200, json=LEVER_PAYLOAD)
            )
            respx.get(
                "https://api.ashbyhq.com/posting-api/job-board/ai-lab?includeCompensation=true"
            ).mock(return_value=httpx.Response(200, json=ASHBY_PAYLOAD))
            targets = [
                CompanySource(platform="greenhouse", token="acme", name="Acme"),
                CompanySource(platform="lever", token="hedge", name="Hedge"),
                CompanySource(platform="ashby", token="ai-lab", name="AI Lab"),
            ]
            first = await service.run(targets)
            second = await service.run(targets)
        await service.aclose()
        return first, second

    first, second = asyncio.run(_run())
    assert first.sources_succeeded == 3
    assert first.raw_jobs == 5  # greenhouse 2 + lever 2 + ashby 1 (unlisted skipped at parse)
    assert first.filtered_out == 0
    assert first.new_jobs == 5
    assert second.new_jobs == 0
    assert second.duplicate_jobs == first.new_jobs

    with session_factory() as s:
        jobs = s.execute(select(Job)).scalars().all()
        assert len(jobs) == first.new_jobs
        assert all(job.fingerprint for job in jobs)
        records = s.execute(select(JobSourceRecord)).scalars().all()
        assert len(records) >= len(jobs)


def test_failed_source_is_isolated(settings, session_factory):
    service = DiscoveryService(settings, session_factory)
    service.fetcher = HttpFetcher(retry=RetryPolicy(max_attempts=1, base_delay=0.001, jitter=0))

    async def _run():
        with respx.mock:
            respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
                return_value=httpx.Response(500)
            )
            respx.get("https://api.lever.co/v0/postings/hedge?mode=json").mock(
                return_value=httpx.Response(200, json=LEVER_PAYLOAD)
            )
            respx.get(
                "https://api.ashbyhq.com/posting-api/job-board/ai-lab?includeCompensation=true"
            ).mock(return_value=httpx.Response(200, json=ASHBY_PAYLOAD))
            targets = [
                CompanySource(platform="greenhouse", token="acme", name="Acme"),
                CompanySource(platform="lever", token="hedge", name="Hedge"),
                CompanySource(platform="ashby", token="ai-lab", name="AI Lab"),
            ]
            stats = await service.run(targets)
        await service.aclose()
        return stats

    stats = asyncio.run(_run())
    assert stats.sources_failed == 1
    assert stats.sources_succeeded == 2
    assert stats.new_jobs >= 3
    assert any("greenhouse" in err for err in stats.errors)
