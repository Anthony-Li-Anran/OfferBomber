"""Phase 3.3 security regressions — 27 required checks.

Covers: user isolation, content safety, human-approval mechanics, job discovery
SSRF guard, and MCP stdio protocol. These tests are extra; no older test was
removed or weakened.
"""

from __future__ import annotations

import concurrent.futures
import inspect
import json
import os
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from offerbomber.app import AppContext
from offerbomber.config import load_settings
from offerbomber.integrations.mcp.confirmations import ConfirmationStore
from offerbomber.integrations.mcp.toolkit import McpToolkit
from offerbomber.storage.base import Base, make_engine
from offerbomber.storage.fts import ensure_fts5
from offerbomber.storage.models import Experience, ExperienceFact, Job, ResumeVersion

ROOT = Path(__file__).resolve().parents[1]
_TS = __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc)


def _fact(fid, eid, content, status="CONFIRMED", skills=None, deleted=False, version=1):
    return ExperienceFact(
        id=fid, experience_id=eid, content=content,
        verification_status=status, confidence=1.0,
        skills=json.dumps(skills or []), is_deleted=deleted, version=version,
        created_at=_TS, updated_at=_TS,
    )


@pytest.fixture()
def env(tmp_path: Path):
    """File-backed DB shared across toolkit instances (cross-process semantics)."""
    db = tmp_path / "db.sqlite"
    engine = make_engine(db)
    Base.metadata.create_all(engine)
    ensure_fts5(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    settings = load_settings(ROOT / "config.example.yaml")
    settings.storage.db_path = str(db)
    ctx = AppContext.__new__(AppContext)
    ctx.settings = settings
    ctx.engine = engine
    ctx.sessions = sessions
    ctx.fts_available = True

    with sessions() as s:
        s.add_all([
            Job(id=1, external_id="j1", company="Nova", normalized_company="nova",
                title="Quant Researcher", normalized_title="quant researcher",
                discovered_at=_TS, source_platform="greenhouse", fingerprint="fp1",
                description="Python stats.", created_at=_TS, updated_at=_TS),
            Experience(id=10, user_id="alice", type="PROJECT", title="A project",
                       created_at=_TS, updated_at=_TS),
            Experience(id=20, user_id="bob", type="PROJECT", title="B project",
                       created_at=_TS, updated_at=_TS),
            _fact(100, 10, "Built a model with 85% accuracy.", skills=["Python", "PyTorch"]),
            _fact(101, 10, "Paper submitted to UAI 2027."),
            _fact(102, 10, "Unverified claim.", status="UNVERIFIED"),
            _fact(103, 10, "Deleted claim.", deleted=True),
            _fact(104, 20, "Bob secret model 99% accuracy.", skills=["SecretSkill"]),
        ])
        s.commit()
        # A pre-existing version owned by alice.
        s.add(ResumeVersion(id=1, user_id="alice", job_id=1, file_path="/alice.pdf",
                            file_sha256="a" * 64, immutable=True, created_at=_TS, updated_at=_TS))
        s.commit()

    def make(user="alice"):
        return McpToolkit(ctx, user_id=user)

    yield make, ctx, db
    engine.dispose()


# ============================================================ 权限 (1-5)


def test_a_cannot_read_b_fact_id(env):
    make, _, _ = env
    r = make("alice").build_resume_plan(1, [104])  # 104 belongs to bob
    assert r["ok"] is False and r["error"] == "fact_forbidden"


def test_a_cannot_generate_with_b_fact_id(env):
    make, _, _ = env
    r = make("alice").prepare_generation(1, "src", [100, 104])
    assert r["ok"] is False and r["error"] == "fact_forbidden"


def test_unconfirmed_fact_cannot_enter_plan(env):
    make, _, _ = env
    r = make("alice").build_resume_plan(1, [102])
    assert r["ok"] is False and r["error"] == "fact_unconfirmed"


def test_deleted_fact_cannot_generate(env):
    make, _, _ = env
    r = make("alice").prepare_generation(1, "src", [103])
    assert r["ok"] is False and r["error"] == "fact_deleted"


def test_other_user_resume_version_invisible(env):
    make, _, _ = env
    r = make("bob").get_generation_status()
    assert r["ok"] and all(v["id"] != 1 for v in r["versions"])
    r2 = make("bob").validate_generated_pdf(1)
    assert r2["ok"] is False and r2["error"] == "version_forbidden"


def test_partial_bad_fact_fails_whole_request(env):
    """One unauthorized ID among valid ones must fail the entire request."""
    make, _, _ = env
    r = make("alice").build_resume_plan(1, [100, 101, 104])
    assert r["ok"] is False and r["error"] == "fact_forbidden"


# ============================================================ 内容安全 (6-10)


def test_no_final_facts_argument_after_approval(env):
    """generate_resume has no content-override parameter in Phase 3.3."""
    sig = inspect.signature(McpToolkit.generate_resume)
    assert "final_facts" not in sig.parameters


def test_untrusted_skills_stripped(env):
    make, _, _ = env
    tk = make("alice")
    plan = json.dumps({
        "target_position": "ML Engineer",
        "items": [{
            "fact_id": 100,
            "text": "Built a model with 85% accuracy.",
            "skills": ["Python", "PyTorch", "QuantumTeleportation", "ForgedSkill"],
        }],
    })
    r = tk.prepare_generation(1, "src", [100], final_plan_json=plan)
    assert r["ok"]
    skills = r["preview"]["items"][0]["skills"]
    assert set(skills) <= {"Python", "PyTorch"}
    assert "QuantumTeleportation" not in skills and "ForgedSkill" not in skills


def test_fabricated_number_reverted_to_original(env):
    make, _, _ = env
    plan = json.dumps({
        "items": [{"fact_id": 100, "text": "Built a model with 99.9% accuracy."}]
    })
    r = make("alice").prepare_generation(1, "src", [100], final_plan_json=plan)
    assert r["ok"]
    item = r["preview"]["items"][0]
    assert item["rendered_text"] == "Built a model with 85% accuracy."
    assert item["validation_status"] == "REVERTED"
    assert 100 in r["preview"]["reverted_fact_ids"]


def test_submission_not_upgraded_to_acceptance(env):
    make, _, _ = env
    plan = json.dumps({
        "items": [{"fact_id": 101, "text": "Paper accepted at UAI 2027."}]
    })
    r = make("alice").prepare_generation(1, "src", [101], final_plan_json=plan)
    assert r["ok"]
    item = r["preview"]["items"][0]
    assert item["rendered_text"] == "Paper submitted to UAI 2027."


def test_fact_audit_has_traceability_fields(env):
    """FactAudit carries evidence + validation_status (A3)."""
    from offerbomber.core.resume_plan import FactAudit

    audit = FactAudit(
        fact_id=100, fact_version=1, source_id=7,
        original_text="orig", rendered_text="rend", experience_id=10,
        evidence="resume.pdf p2", validation_status="REVERTED",
    )
    data = audit.model_dump()
    assert {"fact_id", "fact_version", "original_text", "rendered_text",
            "source_id", "evidence", "validation_status"} <= set(data)


# ============================================================ 确认机制 (11-17)


def test_unapproved_cannot_write(env):
    make, _, _ = env
    tk = make("alice")
    token = tk.prepare_generation(1, "src", [100])["confirmation_token"]
    r = tk.generate_resume(token, allow_write=True)
    assert r["ok"] is False and r["error"] == "not_approved"


def test_rejected_cannot_write(env):
    make, _, _ = env
    tk = make("alice")
    token = tk.prepare_generation(1, "src", [100])["confirmation_token"]
    assert tk._confirmations.reject(token, user_id="alice")
    r = tk.generate_resume(token, allow_write=True)
    assert r["ok"] is False and r["error"] == "confirmation_required"
    # Rejection is irreversible.
    assert tk._confirmations.approve(token, user_id="alice") is False


def test_expired_token_cannot_write(env):
    _, ctx, db = env
    store = ConfirmationStore(db, session_factory=ctx.sessions, ttl_seconds=1)
    token = store.create({"action": "generate_resume"}, user_id="alice")
    time.sleep(1.1)
    assert store.approve(token, user_id="alice") is False
    assert store.get_record(token) is None


def test_used_token_cannot_reuse(env):
    _, ctx, db = env
    store = ConfirmationStore(db, session_factory=ctx.sessions)
    t = store.create({"action": "x"}, user_id="alice")
    store.approve(t, user_id="alice")
    assert store.consume(t, user_id="alice", require_approved=True) is not None
    assert store.consume(t, user_id="alice", require_approved=True) is None


def test_concurrent_consume_only_one_wins(env):
    """Two processes/threads racing on one token: at most one succeeds."""
    _, ctx, db = env
    store = ConfirmationStore(db, session_factory=ctx.sessions)
    t = store.create({"action": "generate_resume"}, user_id="alice")
    assert store.approve(t, user_id="alice")
    # Independent stores with independent sessions over the same file DB.
    s1 = ConfirmationStore(db)
    s2 = ConfirmationStore(db)
    barrier = __import__("threading").Barrier(2)

    def claim(store):
        barrier.wait()
        return store.consume(t, user_id="alice", require_approved=True) is not None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [s1, s2]))
    assert results.count(True) == 1


def test_different_user_cannot_approve(env):
    make, _, _ = env
    tk = make("alice")
    token = tk.prepare_generation(1, "src", [100])["confirmation_token"]
    assert tk._confirmations.approve(token, user_id="bob") is False
    assert make("bob").generate_resume(token, allow_write=True)["ok"] is False


def test_generation_failure_no_fake_success(env):
    """No RR key -> generation fails explicitly, no version invented."""
    make, ctx, _ = env
    tk = make("alice")
    token = tk.prepare_generation(1, "src", [100])["confirmation_token"]
    tk._confirmations.approve(token, user_id="alice")
    r = tk.generate_resume(token, allow_write=True)
    # Without a valid key it must not claim success.
    if r["ok"] is False:
        assert r["error"] in {"generation_failed", "unavailable"}
    with ctx.session() as s:
        assert s.execute(select(ResumeVersion).where(ResumeVersion.user_id == "alice")).scalar_one_or_none().file_path == "/alice.pdf"


def test_stale_approval_after_fact_version_bump(env):
    """Approval binds fact versions; a bump before generate invalidates it."""
    make, ctx, _ = env
    tk = make("alice")
    token = tk.prepare_generation(1, "src", [100])["confirmation_token"]
    tk._confirmations.approve(token, user_id="alice")
    with ctx.session() as s:
        fact = s.get(ExperienceFact, 100)
        fact.version = 2
        s.commit()
    r = tk.generate_resume(token, allow_write=True)
    assert r["ok"] is False and r["error"] == "stale_approval"


# ============================================================ 招聘发现 (18-21)


def test_discover_uses_configured_sources(env, monkeypatch):
    """discover_jobs routes through DiscoveryService with configured targets."""
    make, _, _ = env
    captured = {}

    class FakeService:
        def __init__(self, settings, sessions):
            captured["companies"] = list(settings.discovery.companies)

        async def run(self, targets):
            captured["targets"] = list(targets)
            from offerbomber.core.discovery_service import RunStats
            st = RunStats()
            st.raw_jobs = 10
            st.new_jobs = 3
            st.duplicate_jobs = 7
            return st

        async def aclose(self):
            pass

    import offerbomber.integrations.mcp.toolkit as tk_mod
    monkeypatch.setattr(tk_mod, "DiscoveryService", FakeService)
    r = make("alice").discover_jobs()
    assert r["ok"] and r["inserted"] == 3 and r["duplicates"] == 7
    assert captured["targets"] == captured["companies"]


def test_discover_dedup_counts_passthrough(env, monkeypatch):
    make, _, _ = env

    class FakeService:
        def __init__(self, *a): pass
        async def run(self, targets):
            from offerbomber.core.discovery_service import RunStats
            st = RunStats(); st.duplicate_jobs = 5; st.new_jobs = 0
            return st
        async def aclose(self): pass

    import offerbomber.integrations.mcp.toolkit as tk_mod
    monkeypatch.setattr(tk_mod, "DiscoveryService", FakeService)
    r = make("alice").discover_jobs()
    assert r["duplicates"] == 5 and r["inserted"] == 0


def test_one_source_failure_isolated(env, monkeypatch):
    make, _, _ = env

    class FakeService:
        def __init__(self, *a): pass
        async def run(self, targets):
            from offerbomber.core.discovery_service import RunStats
            st = RunStats(); st.sources_failed = 1
            st._error("boards.example: timeout")
            return st
        async def aclose(self): pass

    import offerbomber.integrations.mcp.toolkit as tk_mod
    monkeypatch.setattr(tk_mod, "DiscoveryService", FakeService)
    r = make("alice").discover_jobs()
    assert r["ok"] and r["sources_partial"] is True and r["errors"] == 1


def test_no_arbitrary_url_param(env):
    """discover_jobs cannot accept a URL → no SSRF vector from the host model."""
    sig = inspect.signature(McpToolkit.discover_jobs)
    assert "url" not in sig.parameters
    r = env[0]("alice").discover_jobs(platform="https://evil.example/collect")
    assert r["ok"] is False and r["error"] == "no_configured_source"


# ============================================================ 宿主协议 (22-27)


@pytest.fixture()
def stdio_cfg(tmp_path: Path) -> Path:
    import yaml
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    db = tmp_path / "p.db"
    raw["storage"]["db_path"] = str(db)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    engine = make_engine(db)
    Base.metadata.create_all(engine)
    ensure_fts5(engine)
    engine.dispose()
    return cfg


@pytest.mark.asyncio
async def test_stdio_handshake(stdio_cfg):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    p = StdioServerParameters(
        command=sys.executable, args=["-m", "offerbomber.integrations.mcp.server"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
             "OFFERBOMBER_MCP_CONFIG": str(stdio_cfg)}, cwd=str(ROOT))
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            assert init.serverInfo.name == "offerbomber"


@pytest.mark.asyncio
async def test_tool_list_complete_including_new_tools(stdio_cfg):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    p = StdioServerParameters(
        command=sys.executable, args=["-m", "offerbomber.integrations.mcp.server"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
             "OFFERBOMBER_MCP_CONFIG": str(stdio_cfg)}, cwd=str(ROOT))
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
    expected = {
        "search_jobs", "discover_jobs", "get_job", "retrieve_experience",
        "get_confirmed_facts", "analyze_jd", "build_resume_plan",
        "validate_resume_facts", "prepare_generation", "generate_resume",
        "validate_generated_pdf", "prepare_export", "export_pdf",
        "get_generation_status",
    }
    assert expected <= names  # 14 tools, former 12 remain compatible


@pytest.mark.asyncio
async def test_each_tool_has_schema(stdio_cfg):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    p = StdioServerParameters(
        command=sys.executable, args=["-m", "offerbomber.integrations.mcp.server"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
             "OFFERBOMBER_MCP_CONFIG": str(stdio_cfg)}, cwd=str(ROOT))
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for t in (await s.list_tools()).tools:
                assert t.inputSchema["type"] == "object", t.name


@pytest.mark.asyncio
async def test_normal_tool_call_ok(stdio_cfg):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    p = StdioServerParameters(
        command=sys.executable, args=["-m", "offerbomber.integrations.mcp.server"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
             "OFFERBOMBER_MCP_CONFIG": str(stdio_cfg)}, cwd=str(ROOT))
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("get_generation_status", {})
            text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            assert json.loads(text)["ok"] is True


@pytest.mark.asyncio
async def test_error_input_structured(stdio_cfg):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    p = StdioServerParameters(
        command=sys.executable, args=["-m", "offerbomber.integrations.mcp.server"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
             "OFFERBOMBER_MCP_CONFIG": str(stdio_cfg)}, cwd=str(ROOT))
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("get_job", {"job_id": 99999})
            text = "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
            payload = json.loads(text)
            assert payload["ok"] is False and payload["error"] == "not_found"


def test_pending_survives_restart(env):
    """Approval state lives in SQLite, not in MCP process memory."""
    make, ctx, db = env
    tk = make("alice")
    token = tk.prepare_generation(1, "src", [100])["confirmation_token"]
    # Brand-new toolkit (new process equivalent) sees the same pending token.
    fresh = make("alice")
    rec = fresh._confirmations.get_record(token, user_id="alice")
    assert rec is not None and rec["status"] == "PENDING"
    assert fresh._confirmations.approve(token, user_id="alice")
    assert fresh._confirmations.get_record(token, user_id="alice")["status"] == "APPROVED"
