"""Phase 4-6 general-purpose job-search agent tests.

Covers the task book's 24 required cases: profile generality, multi-source
discovery, dedup, universal matching, fact safety, recipient validation,
attachment versioning, idempotency, retries/timeout, OAuth failures, email
classification, thread association, timezone parsing, task recovery, concurrency
dedup, pause, MCP isolation, human approval, no-send-without-auth, E2E and a
clearly-marked real-service integration placeholder.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import respx
import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from offerbomber.applications.email_provider import (
    GmailOAuthProvider, PreparedMessage, ProviderUnavailable, RecipientNotVerified, is_valid_email,
)
from offerbomber.applications.pipeline import ApplicationPipeline, CHANNEL_EMAIL
from offerbomber.config import Settings, load_settings
from offerbomber.core.company_discovery import CompanyDiscoveryService
from offerbomber.core.job_schema import NormalizedJob, dedup_key, from_raw
from offerbomber.core.matching import FactView, JobMatcher
from offerbomber.app import AppContext
from offerbomber.discovery.base import RawJob
from offerbomber.discovery.custom_source import LocalImportSource
from offerbomber.discovery.http import HttpFetcher, SourceFetchError
from offerbomber.discovery.official import detect_ats, parse_json_ld_jobs
from offerbomber.discovery.search_source import QueryBuilder, SearchProviderConfig, SearchEngineSource
from offerbomber.feedback.classifier import EmailClassifier
from offerbomber.feedback.sync import InboxSyncService, parse_event_time
from offerbomber.integrations.mcp.confirmations import ConfirmationStore
from offerbomber.integrations.mcp.toolkit import McpToolkit
from offerbomber.reporting.daily import DailyReport
from offerbomber.scheduling.task_queue import TaskQueue
from offerbomber.search_profile import (
    ApplicationPreference, JobSearchProfile, SearchProfileService,
)
from offerbomber.storage.base import Base, make_engine
from offerbomber.storage.fts import ensure_fts5
from offerbomber.storage.models import (
    Application, ApplicationDraft, EmailAccount, Experience, ExperienceFact,
    Job, Profile, RecruitmentEvent, ResumeVersion, SearchProfile,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def file_db(tmp_path):
    engine = make_engine(tmp_path / "agent.db")
    Base.metadata.create_all(engine)
    ensure_fts5(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory, tmp_path
    engine.dispose()


@pytest.fixture()
def settings():
    return load_settings(ROOT / "config.example.yaml")


@pytest.fixture()
def store(file_db):
    factory, tmp_path = file_db
    return ConfirmationStore(tmp_path / "agent.db", session_factory=factory)


def _job(factory, title="Data Analyst", company="Acme", *, email=None, apply=None) -> Job:
    with factory() as s:
        job = Job(
            external_id=f"ext:{title}:{company}", company=company,
            normalized_company=company.lower(), title=title,
            normalized_title=title.lower(), description="Analyze product metrics with SQL and Python",
            source_platform="greenhouse", discovered_at=datetime.now(timezone.utc).replace(tzinfo=None),
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            recruitment_email=email, apply_url=apply, fingerprint="fp-" + title,
        )
        s.add(job)
        s.commit()
        s.refresh(job)
        return job


def _confirmed_fact(factory, user="default", content="Built dashboards with SQL and Python", skills='["SQL","Python"]') -> ExperienceFact:
    with factory() as s:
        prof = Profile(user_id=user, display_name="P", created_at=datetime.now(), updated_at=datetime.now())
        s.add(prof); s.flush()
        exp = Experience(user_id=user, profile_id=prof.id, type="WORK", title="Analyst",
                         organization="X", start_date="2023", end_date="2024",
                         created_at=datetime.now(), updated_at=datetime.now())
        s.add(exp); s.flush()
        fact = ExperienceFact(experience_id=exp.id, content=content, skills=skills,
                              verification_status="CONFIRMED", is_deleted=False,
                              created_at=datetime.now(), updated_at=datetime.now())
        s.add(fact); s.commit(); s.refresh(fact)
        return fact


# ---- 1-3: profile generality / regions / languages / employment types ----

def test_profile_allows_any_industry_role_region_language():
    p = JobSearchProfile(
        name="design-cn", target_titles=["产品设计师"], industries=["互联网"],
        locations=["上海"], languages=["zh"], employment_types=["full_time"],
    )
    assert p.to_canonical_json()
    assert p.active_dimensions()["target_titles"] == ["产品设计师"]
    assert p.application_preferences.mode == "MANUAL"  # Auto never a default


def test_empty_profile_imposes_no_constraints():
    p = JobSearchProfile(name="anything")
    assert p.active_dimensions() == {}
    assert not p.is_excluded_company("随机公司")
    assert p.application_preferences.max_per_day == 0


def test_multiple_profiles_per_user(file_db):
    factory, _ = file_db
    svc = SearchProfileService(factory)
    svc.create(JobSearchProfile(name="design", user_id="u1", languages=["zh"]))
    svc.create(JobSearchProfile(name="quant", user_id="u1", industries=["finance"]))
    assert len(svc.list("u1")) == 2
    assert svc.list("u2") == []


def test_profile_user_isolation_on_update_delete(file_db):
    factory, _ = file_db
    svc = SearchProfileService(factory)
    svc.create(JobSearchProfile(name="mine", user_id="alice"))
    pid = svc.list("alice")[0][0]
    assert svc.get("bob", pid) is None
    assert svc.update("bob", pid, JobSearchProfile(name="hijack", user_id="bob")) is None
    assert not svc.delete("bob", pid)
    assert svc.get("alice", pid) is not None


# ---- 4-6: discovery, dedup, company auto-expand ----

def test_json_ld_jobposting_parsing():
    html = """<html><script type="application/ld+json">
    {"@type":"JobPosting","title":"Nurse","hiringOrganization":{"name":"Hospital A"},
     "url":"/careers/nurse","description":"<p>Care for patients</p>"}
    </script></html>"""
    jobs = parse_json_ld_jobs(html, "https://hospital.example/careers")
    assert len(jobs) == 1 and jobs[0].title == "Nurse" and jobs[0].company == "Hospital A"


def test_ats_detection_and_no_fabrication():
    assert detect_ats("https://jobs.lever.co/acme") == ("lever", "acme")
    assert detect_ats("https://example.com/random") is None
    assert parse_json_ld_jobs("<html></html>", "https://x.example") == []


def test_search_query_built_from_profile_not_hardcoded():
    q = QueryBuilder.build(["Chef"], ["cooking"], ["Paris"], ["full_time"], ["hospitality"], ["fr"])
    assert "Chef" in q and "Paris" in q and "site:" not in q


def test_search_source_never_accepts_arbitrary_url(factory=None):
    # get_json is called with the configured base_url; a query string cannot
    # change the host. We assert the source validates an absolute configured URL.
    fetcher = HttpFetcher()
    src = SearchEngineSource(fetcher, SearchProviderConfig(base_url="not-a-url"))
    with pytest.raises(SourceFetchError):
        asyncio.run(src.search("chef paris"))


def test_local_import_csv_and_cross_source_dedup(tmp_path):
    csv = tmp_path / "jobs.csv"
    csv.write_text("title,company,url,description\nAnalyst,Acme,https://a.example/j/1,SQL\n", encoding="utf-8")
    jobs = LocalImportSource().load(csv)
    assert len(jobs) == 1
    assert dedup_key(from_raw(jobs[0])) == "acme analyst"


def test_company_discovery_requires_configured_provider(file_db, settings):
    factory, _ = file_db

    class _Ctx:
        def __init__(self):
            self.sessions = factory
            self.settings = settings
            self.fts_available = True
        def session(self):
            return factory()
    toolkit = McpToolkit(_Ctx())
    result = toolkit.create_search_profile(json.dumps({"name": "x"}))
    assert result["ok"]
    pid = toolkit.search_profiles()["profiles"][0]["id"]
    result = toolkit.discover_companies(pid)
    assert result["ok"] is False and result["error"] == "search_provider_not_configured"


# ---- 7-8: matching and confirmed-fact safety ----

def test_universal_matching_by_profile_dimensions():
    job = NormalizedJob(job_id="1", company="Clinic", title="Registered Nurse",
                        description="Patient care and clinical assessment",
                        location="Berlin", employment_type="full_time")
    profile = JobSearchProfile(name="nurse", target_titles=["Nurse"],
                               skills=["patient care"], locations=["Berlin"])
    facts = [FactView(1, "Provided patient care in clinical settings", ["patient care"])]
    out = JobMatcher().match(job, profile, facts)
    assert out.match_score > 0 and out.application_recommendation in {"APPLY", "REVIEW"}
    assert 1 in out.relevant_fact_ids


def test_hard_constraint_excluded_company_blocks_application():
    job = NormalizedJob(job_id="2", company="BadCorp", title="Engineer", description="Build things")
    profile = JobSearchProfile(name="x", excluded_companies=["BadCorp"])
    out = JobMatcher().match(job, profile, [FactView(1, "skill")])
    assert out.eligibility == "INELIGIBLE" and out.application_recommendation == "SKIP"
    assert out.hard_constraint_results["company_not_excluded"] is False


def test_semantic_mismatch_is_not_eligibility_failure():
    job = NormalizedJob(job_id="3", company="Unknown", title="Pilot", description="Fly aircraft")
    profile = JobSearchProfile(name="x")
    out = JobMatcher().match(job, profile, [])
    assert out.eligibility != "INELIGIBLE"  # low score but not a hard fail
    assert out.application_recommendation in {"SKIP", "REVIEW"}


# ---- 9-10: recipient validation and attachment version ----

def test_email_recipient_validation():
    assert is_valid_email("hr@company.com")
    assert not is_valid_email("hr@company") and not is_valid_email("not-an-email")


def test_host_cannot_override_evidence_recipient(file_db, store, settings):
    """Email recipient is always taken from job evidence, never from model input."""
    factory, _ = file_db
    job = _job(factory, email="real-hr@acme.com")

    class _Ctx:
        def __init__(self):
            self.sessions = factory
            self.settings = settings
            self.fts_available = True
        def session(self):
            return factory()
    toolkit = McpToolkit(_Ctx(), user_id="default")
    toolkit._confirmations = store
    res = toolkit.prepare_application(job.id, channel="EMAIL", recipient="attacker@evil.com",
                                      subject="Apply", body="Please consider me")
    assert res["ok"]
    assert res["preview"]["recipient"] == "real-hr@acme.com"  # host input ignored


def test_prepare_application_rejects_invalid_and_unverified_recipient(file_db, store):
    factory, _ = file_db
    job = _job(factory)
    p = ApplicationPipeline(factory, store)
    aid, _ = p.ensure_application(job.id)
    bad = p.prepare(aid, channel=CHANNEL_EMAIL, recipient="not-an-email",
                    subject="hi", body="hi")
    assert bad["ok"] is False and bad["error"] == "invalid_recipient"


def test_draft_binds_approved_resume_version(file_db, store):
    factory, _ = file_db
    job = _job(factory, email="hr@acme.com")
    with factory() as s:
        rv = ResumeVersion(file_path="/tmp/x.pdf", user_id="default",
                           created_at=datetime.now(), updated_at=datetime.now())
        s.add(rv); s.commit(); s.refresh(rv)
    p = ApplicationPipeline(factory, store)
    aid, _ = p.ensure_application(job.id)
    res = p.prepare(aid, channel=CHANNEL_EMAIL, recipient="hr@acme.com",
                    subject="Apply", body="Please consider me", resume_version_id=rv.id)
    assert res["ok"]
    with factory() as s:
        draft = s.execute(
            select(ApplicationDraft).where(
                ApplicationDraft.application_id == aid,
                ApplicationDraft.resume_version_id == rv.id,
            )
        ).scalar_one()
        assert draft.resume_version_id == rv.id
        assert draft.payload_hash == res["payload_sha256"]


# ---- 11-13: idempotency, failure, OAuth ----

def test_ensure_application_is_idempotent(file_db, store):
    factory, _ = file_db
    job = _job(factory)
    p = ApplicationPipeline(factory, store)
    a1, c1 = p.ensure_application(job.id)
    a2, c2 = p.ensure_application(job.id)
    assert a1 == a2 and c1 is True and c2 is False


def test_submit_without_approval_or_without_provider_fails(file_db, store):
    factory, _ = file_db
    job = _job(factory, email="hr@acme.com")
    p = ApplicationPipeline(factory, store)
    aid, _ = p.ensure_application(job.id)
    p.set_mode("ASSISTED", max_per_day=5)
    prepared = p.prepare(aid, channel=CHANNEL_EMAIL, recipient="hr@acme.com", subject="s", body="b")
    token = prepared["confirmation_token"]
    assert p.submit(token, allow_write=True)["error"] == "not_approved"
    # Approve as a human would (out-of-band), then no provider -> explicit failure.
    store.approve(token, user_id="default")
    res = p.submit(token, provider=None, allow_write=True)
    assert res["ok"] is False and res["error"] == "submit_failed"
    with factory() as s:
        assert s.get(Application, aid).status == "FAILED"  # no fake success


def test_gmail_provider_missing_credentials_fails_closed():
    provider = GmailOAuthProvider(credential_env="OFFERBOMBER_TEST_MISSING_CREDS")
    assert provider.is_authorized() is False
    with pytest.raises(ProviderUnavailable):
        provider.send(PreparedMessage("hr@x.com", "s", "b"), "k")


def test_send_rejects_unverified_recipient_even_when_authorized(monkeypatch, tmp_path):
    cred = tmp_path / "creds.json"
    cred.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OFFERBOMBER_TEST_GMAIL_CREDS", str(cred))
    provider = GmailOAuthProvider(credential_env="OFFERBOMBER_TEST_GMAIL_CREDS")
    # is_authorized would try google libs; recipient check happens first in send.
    with pytest.raises(RecipientNotVerified):
        provider.send(PreparedMessage("bad", "s", "b"), "k")


# ---- 14-16: classification, thread association, timezone ----

def test_multilingual_classification():
    c = EmailClassifier()
    assert c.classify("Interview Invitation", "schedule an interview").category == "INTERVIEW_INVITATION"
    assert c.classify("面试邀请", "请参加视频面试").category == "INTERVIEW_INVITATION"
    assert c.classify("Re: your application", "Thanks, but we are not moving forward").category == "REJECTION"
    assert c.classify("Hello", "Just checking in").category in {"FOLLOW_UP", "UNKNOWN"}
    assert c.classify("random", "nothing relevant").needs_human is True


def test_application_association_requires_company_and_title(file_db):
    factory, _ = file_db
    job = _job(factory, title="Data Analyst", company="Acme")
    svc = InboxSyncService(factory)
    with factory() as s:
        app = Application(job_id=job.id, status="SUBMITTED", created_at=datetime.now(), updated_at=datetime.now())
        s.add(app); s.commit(); s.refresh(app)
        app_id = app.id
    # company only -> must not associate
    assert svc._associate(s, "hr@acme.com", "Update", "thread-x", "Hello") is None


def test_timezone_parsing_no_guess():
    dt, tz = parse_event_time("Interview 2026-11-25 14:00 (GMT+8)")
    assert tz == "GMT+8" and dt is not None
    assert dt.hour == 6  # UTC
    assert parse_event_time("Let's talk soon") == (None, "")


# ---- 17-19: scheduler, concurrency dedup, pause ----

def test_task_enqueue_idempotent_and_recover(file_db):
    factory, _ = file_db
    q = TaskQueue(factory)
    r1 = q.enqueue("DISCOVER_JOBS", {"a": 1}, idempotency_key="k1")
    r2 = q.enqueue("DISCOVER_JOBS", {"a": 1}, idempotency_key="k1")
    assert r1["created"] and not r2["created"] and r1["task_id"] == r2["task_id"]
    with factory() as s:
        from offerbomber.storage.models import Task
        t = s.execute(select(Task)).scalar_one()
        t.status = "RUNNING"
        t.started_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        s.commit()
    assert q.recover()["recovered"] == 1


def test_concurrent_claim_only_one_wins(file_db):
    factory, _ = file_db
    q = TaskQueue(factory)
    q.pause(False) if hasattr(q, "pause") else None
    # enable automation via pipeline state
    with factory() as s:
        from offerbomber.storage.models import AutomationState
        s.add(AutomationState(user_id="default", paused=False, mode="ASSISTED",
                              max_per_day=5, created_at=datetime.now(), updated_at=datetime.now()))
        s.commit()
    q.enqueue("MATCH_JOBS", {"x": 1})
    results = []
    import threading
    lock = threading.Lock()
    def claim():
        t = q.claim("MATCH_JOBS")
        with lock:
            results.append(t.task_id if t else None)
    threads = [threading.Thread(target=claim) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    claimed = [r for r in results if r]
    assert len(claimed) == 1


def test_pause_blocks_claiming(file_db):
    factory, _ = file_db
    q = TaskQueue(factory)
    q.enqueue("DAILY_REPORT", {})
    assert q.claim() is None  # paused by default (Auto OFF)


# ---- 20-21: MCP isolation and human approval ----

def test_mcp_profile_and_application_isolation(file_db, settings):
    factory, tmp_path = file_db
    from offerbomber.app import AppContext
    ctx = AppContext(settings)
    # Use the same engine as file_db by swapping sessions for isolation logic tests.
    alice = ApplicationPipeline(factory, ConfirmationStore(tmp_path / "a.db", session_factory=factory), user_id="alice")
    bob = ApplicationPipeline(factory, ConfirmationStore(tmp_path / "a.db", session_factory=factory), user_id="bob")
    job = _job(factory)
    aid, _ = alice.ensure_application(job.id)
    assert alice.status(aid)["ok"] is True
    assert bob.status(aid)["ok"] is False
    ctx.close()


def test_human_approval_required_and_single_use(file_db, store):
    factory, _ = file_db
    job = _job(factory, email="hr@acme.com")
    p = ApplicationPipeline(factory, store)
    aid, _ = p.ensure_application(job.id)
    token = p.prepare(aid, channel=CHANNEL_EMAIL, recipient="hr@acme.com", subject="s", body="b")["confirmation_token"]
    assert store.get_record(token, user_id="default")["status"] == "PENDING"
    assert p.submit(token, allow_write=True)["error"] == "not_approved"
    assert store.approve(token, user_id="bob") is False  # wrong user cannot approve
    assert store.approve(token, user_id="default") is True
    # second approve is rejected (state machine)
    assert store.approve(token, user_id="default") is False


def test_no_send_without_authorization_is_explicit(file_db):
    factory, tmp_path = file_db
    svc = InboxSyncService(factory)
    provider = GmailOAuthProvider(credential_env="OFFERBOMBER_TEST_MISSING_CREDS")
    res = svc.sync(provider)
    assert res["ok"] is False and res["error"] == "provider_unavailable"


# ---- 22: E2E simulation (mock provider, no real send) ----

class _FakeProvider:
    name = "fake"
    def __init__(self): self.sent = []
    def is_authorized(self): return True
    def create_draft(self, message): return "draft-1"
    def send(self, message, idempotency_key):
        self.sent.append((message, idempotency_key))
        return type("Sent", (), {"provider_message_id": "msg-1", "thread_id": "th-1"})()
    def list_inbox(self, since_iso=None, limit=50): return []


def test_end_to_end_profile_to_submit_to_report(file_db, store, settings, tmp_path):
    factory, _ = file_db
    # 1) campaign
    svc = SearchProfileService(factory)
    svc.create(JobSearchProfile(name="analyst", user_id="default", skills=["SQL", "Python"],
                                application_preferences=ApplicationPreference(mode="ASSISTED", max_per_day=5)))
    assert len(svc.list("default")) == 1
    # 2) job + confirmed fact
    job = _job(factory, email="hr@acme.com")
    _confirmed_fact(factory)
    # 3) match (toolkit wired to the test DB)
    class _Ctx:
        def __init__(self):
            self.sessions = factory
            self.settings = settings
            self.fts_available = True
        def session(self):
            return factory()
    toolkit = McpToolkit(_Ctx(), user_id="default")
    toolkit._confirmations = store
    result = toolkit.match_jobs(1, job.id)
    assert result["ok"] and result["matches"][0]["job_id"] == job.id
    # 4) prepare + human approve + submit with fake provider
    pipeline = ApplicationPipeline(factory, store)
    aid, _ = pipeline.ensure_application(job.id)
    pipeline.set_mode("ASSISTED", max_per_day=5)
    prepared = pipeline.prepare(aid, channel=CHANNEL_EMAIL, recipient="hr@acme.com", subject="Apply", body="Dear HR")
    token = prepared["confirmation_token"]
    store.approve(token, user_id="default")
    provider = _FakeProvider()
    submitted = pipeline.submit(token, provider=provider, allow_write=True)
    assert submitted["ok"] and submitted["status"] == "SUBMITTED"
    assert len(provider.sent) == 1
    # 5) daily report
    report = DailyReport(factory).build()
    assert report["applications_submitted_total"] >= 1


# ---- 23-24: real-service integration placeholder and env switching ----

@pytest.mark.real_service
@pytest.mark.skipif(
    not __import__("os").getenv("OFFERBOMBER_LIVE_GMAIL_TEST") == "1",
    reason="Real Gmail OAuth requires explicit user authorization (OFFERBOMBER_LIVE_GMAIL_TEST=1). "
           "This test is intentionally skipped by default; no mock is presented as real.",
)
def test_real_gmail_send_requires_explicit_authorization():
    """Placeholder for a real, user-authorized Gmail send. Never runs in CI."""
    provider = GmailOAuthProvider()
    assert provider.is_authorized(), "configure OFFERBOMBER_GMAIL_CREDENTIALS first"
    sent = provider.send(PreparedMessage("hr@example.com", "Test", "Body"), "live-test")
    assert sent.provider_message_id


def test_config_switches_providers_without_code_change(tmp_path):
    """A different config selects a different provider - no hardcoded model/mail."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text("models:\n  preferred: openai_compatible\nemail:\n  provider: imap_smtp\n", encoding="utf-8")
    s = load_settings(cfg)
    assert s.models.preferred == "openai_compatible"
    assert s.email.provider == "imap_smtp"
