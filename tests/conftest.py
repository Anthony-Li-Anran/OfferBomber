"""Shared pytest fixtures: in-memory SQLite and verified-shape API payloads."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from offerbomber.config import load_settings
from offerbomber.storage.base import Base, make_engine
from pathlib import Path


@pytest.fixture()
def settings():
    return load_settings(Path(__file__).resolve().parents[1] / "config.example.yaml")


@pytest.fixture()
def session_factory():
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 101,
            "title": "Machine Learning Intern",
            "updated_at": "2026-11-18T10:00:00-05:00",
            "location": {"name": "New York, NY"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
            "content": "<p>Work on pretraining.</p>",
            "departments": [{"id": 1, "name": "AI"}],
            "offices": [],
        },
        {
            "id": 102,
            "title": "Backend Engineer",
            "updated_at": "2026-11-17T10:00:00-05:00",
            "location": {"name": "Remote"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
            "content": "Build services.",
        },
    ],
    "meta": {"total": 2},
}

LEVER_PAYLOAD = [
    {
        "id": "lever-201",
        "text": "Quantitative Research Intern",
        "categories": {
            "location": "London",
            "commitment": "Internship",
            "team": "Quant",
            "department": "Research",
            "allLocations": ["London"],
        },
        "descriptionPlain": "Research signals for systematic strategies.",
        "hostedUrl": "https://jobs.lever.co/hedge/lever-201",
        "applyUrl": "https://jobs.lever.co/hedge/lever-201/apply",
        "workplaceType": "on-site",
        "country": "GB",
    },
    {
        "id": "lever-202",
        "text": "Data Analyst",
        "categories": {"location": "Remote", "commitment": "Full-time"},
        "descriptionPlain": "Analyze product metrics.",
        "hostedUrl": "https://jobs.lever.co/hedge/lever-202",
        "applyUrl": "https://jobs.lever.co/hedge/lever-202/apply",
        "workplaceType": "remote",
    },
]

ASHBY_PAYLOAD = {
    "apiVersion": "1",
    "jobs": [
        {
            "title": "AI Infrastructure Engineer",
            "location": "San Francisco",
            "secondaryLocations": [],
            "department": "Engineering",
            "team": "Infra",
            "isListed": True,
            "isRemote": False,
            "workplaceType": "OnSite",
            "descriptionHtml": "<p>Train large models.</p>",
            "descriptionPlain": "Train large models.",
            "publishedAt": "2026-11-16T16:21:55.393+00:00",
            "employmentType": "FullTime",
            "jobUrl": "https://jobs.ashbyhq.com/ai-lab/abc123",
            "applyUrl": "https://jobs.ashbyhq.com/ai-lab/abc123/apply",
        },
        {
            "title": "Hidden Role",
            "location": "Remote",
            "isListed": False,
            "jobUrl": "https://jobs.ashbyhq.com/ai-lab/hidden",
        },
    ],
    "compensation": {"compensationTierSummary": "$100K"},
}
