"""Connector parsing tests using payloads shaped after official API docs."""

from __future__ import annotations

from offerbomber.discovery.ashby import AshbySource
from offerbomber.discovery.greenhouse import GreenhouseSource
from offerbomber.discovery.http import HttpFetcher
from offerbomber.discovery.lever import LeverSource

from .conftest import ASHBY_PAYLOAD, GREENHOUSE_PAYLOAD, LEVER_PAYLOAD


def _sources():
    fetcher = HttpFetcher()
    return GreenhouseSource(fetcher), LeverSource(fetcher), AshbySource(fetcher)


def test_greenhouse_parses_official_fields():
    greenhouse, _, _ = _sources()
    jobs = greenhouse.parse_payload(GREENHOUSE_PAYLOAD, "acme", "Acme Corp")
    assert len(jobs) == 2
    first = jobs[0]
    assert first.source_platform == "greenhouse"
    assert first.external_id == "101"
    assert first.company == "Acme Corp"
    assert first.title == "Machine Learning Intern"
    assert first.location == "New York, NY"
    assert "pretraining" in first.description
    assert first.official_url == "https://boards.greenhouse.io/acme/jobs/101"
    assert first.published_at is not None


def test_lever_uses_categories_not_labels():
    _, lever, _ = _sources()
    jobs = lever.parse_payload(LEVER_PAYLOAD, "hedge", "Hedge")
    assert len(jobs) == 2
    quant = jobs[0]
    assert quant.external_id == "lever-201"
    assert quant.location == "London"
    assert quant.employment_type == "Internship"
    assert quant.remote_policy == "on-site"
    assert quant.apply_url.endswith("/apply")
    # Public Lever list has no createdAt; published_at must stay None (no fabrication).
    assert quant.published_at is None


def test_ashby_skips_unlisted_and_derives_stable_id():
    _, _, ashby = _sources()
    jobs = ashby.parse_payload(ASHBY_PAYLOAD, "ai-lab", "AI Lab")
    assert len(jobs) == 1  # isListed:false filtered out
    job = jobs[0]
    assert job.title == "AI Infrastructure Engineer"
    assert job.external_id == "https://jobs.ashbyhq.com/ai-lab/abc123"
    assert job.employment_type == "fulltime"
    assert job.remote_policy == "onsite"
    assert job.published_at is not None


def test_ashby_id_is_stable_across_parse():
    _, _, ashby = _sources()
    a = ashby.parse_payload(ASHBY_PAYLOAD, "ai-lab", "AI Lab")[0]
    b = ashby.parse_payload(ASHBY_PAYLOAD, "ai-lab", "AI Lab")[0]
    assert a.external_id == b.external_id
    assert a.fingerprint == b.fingerprint


def test_urls_are_built_from_verified_endpoints():
    greenhouse, lever, ashby = _sources()
    assert greenhouse.build_url("acme") == (
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    )
    assert lever.build_url("hedge") == "https://api.lever.co/v0/postings/hedge?mode=json"
    assert ashby.build_url("ai-lab") == (
        "https://api.ashbyhq.com/posting-api/job-board/ai-lab?includeCompensation=true"
    )
