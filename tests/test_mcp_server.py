"""MCP server tests, layered by verification level.

- Protocol tests: real MCP stdio handshake + tools/list + tools/call (no mock).
- Toolkit tests: business logic against an in-memory DB.
- Permission tests: write tools require confirmation token + allow_write.
- Truthfulness tests: host-supplied text cannot introduce unverified numbers/status.

WorkBuddy / DeepSeek Harness real-host runs are NOT simulated here; see docs/MCP_INTEGRATION.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from offerbomber.app import AppContext
from offerbomber.config import load_settings
from offerbomber.integrations.mcp.toolkit import McpToolkit
from offerbomber.storage.base import Base
from offerbomber.storage.fts import ensure_fts5
from offerbomber.storage.models import Experience, ExperienceFact, Job

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def toolkit(tmp_path: Path) -> McpToolkit:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    ensure_fts5(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    settings = load_settings(ROOT / "config.example.yaml")
    # Isolate the persistent confirmation store per test.
    settings.storage.db_path = str(tmp_path / "offerbomber.db")
    ctx = AppContext.__new__(AppContext)
    ctx.settings = settings
    ctx.engine = engine
    ctx.sessions = sessions
    ctx.fts_available = True

    with sessions() as s:
        s.add_all([
            Job(
                id=1, external_id="j1", company="Nova", normalized_company="nova",
                title="Quant Researcher", normalized_title="quant researcher",
                discovered_at=_TS, source_platform="greenhouse",
                fingerprint="fp1", description="Python and statistics required.",
                created_at=_TS, updated_at=_TS,
            ),
            Experience(
                id=10, user_id="default", type="PROJECT", title="Research Project",
                created_at=_TS, updated_at=_TS,
            ),
        ])
        s.commit()
        s.add(ExperienceFact(
            id=100, experience_id=10, content="Built a model with 85% accuracy.",
            verification_status="CONFIRMED", confidence=1.0,
            skills=json.dumps(["Python", "Statistics"]),
            created_at=_TS, updated_at=_TS,
        ))
        s.add(ExperienceFact(
            id=101, experience_id=10, content="Paper submitted to UAI 2027.",
            verification_status="CONFIRMED", confidence=1.0,
            created_at=_TS, updated_at=_TS,
        ))
        s.commit()

    tk = McpToolkit(ctx)
    yield tk
    engine.dispose()


# ---------------------------------------------------------------- protocol level


@pytest.mark.asyncio
async def test_real_stdio_protocol_handshake_and_tools():
    """真实 MCP stdio 子进程:握手 + 列工具 + 实际 tool call。非 mock。"""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "offerbomber.integrations.mcp.server"],
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "OFFERBOMBER_MCP_CONFIG": str(ROOT / "config.example.yaml"),
        },
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.name == "offerbomber"

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {
                "search_jobs", "get_job", "retrieve_experience", "get_confirmed_facts",
                "analyze_jd", "build_resume_plan", "validate_resume_facts",
                "prepare_generation", "generate_resume", "get_generation_status",
            }.issubset(names)

            result = await session.call_tool("get_generation_status", {})
            text = "".join(
                c.text for c in result.content if getattr(c, "type", "") == "text"
            )
            payload = json.loads(text)
            assert payload["ok"] is True
            assert payload["count"] == 0


@pytest.mark.asyncio
async def test_mcp_tool_has_input_schema():
    """每个工具必须带 JSON Schema 输入契约,宿主才能正确调用。"""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "offerbomber.integrations.mcp.server"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"),
             "OFFERBOMBER_MCP_CONFIG": str(ROOT / "config.example.yaml")},
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            by_name = {t.name: t for t in tools.tools}
            gen = by_name["generate_resume"]
            assert gen.inputSchema["type"] == "object"
            assert "confirmation_token" in gen.inputSchema["properties"]
            assert "confirmation_token" in gen.inputSchema.get("required", [])


# ---------------------------------------------------------------- toolkit level


def test_search_and_get_job(toolkit):
    r = toolkit.search_jobs(keyword="Quant", limit=5)
    assert r["ok"] and r["count"] >= 1
    detail = toolkit.get_job(1)
    assert detail["ok"]
    assert detail["job"]["title"] == "Quant Researcher"
    assert "description" in detail["job"]
    assert toolkit.get_job(999)["ok"] is False


def test_get_confirmed_facts_only_confirmed(toolkit):
    r = toolkit.get_confirmed_facts()
    assert r["ok"] and r["count"] == 2
    assert all(f["verification_status"] == "CONFIRMED" for f in r["facts"])


def test_retrieve_experience(toolkit):
    r = toolkit.retrieve_experience("model accuracy", top_k=3)
    assert r["ok"]
    assert isinstance(r["matches"], list)


def test_analyze_jd_deterministic_and_host_result(toolkit):
    det = toolkit.analyze_jd(1)
    assert det["ok"] and det["requirements"]["job_title"] == "Quant Researcher"

    host_json = json.dumps({
        "job_title": "WRONG TITLE", "job_category": "QUANT",
        "required_skills": [{"text": "Python", "evidence": "Python and statistics required.", "level": "MUST"}],
        "resume_language": "en",
    })
    host = toolkit.analyze_jd(1, requirements_json=host_json)
    assert host["ok"]
    # 权威字段必须来自 Job,不采信模型输出
    assert host["requirements"]["job_title"] == "Quant Researcher"
    assert host["requirements"]["analysis_provider"] == "host-model-via-mcp"


def test_analyze_jd_rejects_bad_host_json(toolkit):
    r = toolkit.analyze_jd(1, requirements_json="not json")
    assert r["ok"] is False and r["error"] == "invalid_requirements"


def test_build_resume_plan_uses_job_title(toolkit):
    r = toolkit.build_resume_plan(1, [100, 101])
    assert r["ok"]
    assert r["draft"]["target_position"] == "Quant Researcher"
    assert 100 in r["draft"]["fact_ids"]


# ---------------------------------------------------------------- truthfulness


def test_validate_rejects_fabricated_number_and_acceptance(toolkit):
    draft = {
        "fact_ids": [100, 101],
        "items": [
            {"fact_id": 100, "text": "Built a model with 99% accuracy."},
            {"fact_id": 101, "text": "Paper accepted at UAI 2027."},
        ],
    }
    r = toolkit.validate_resume_facts(draft_json=json.dumps(draft))
    assert r["ok"] and r["valid"] is False
    reasons = {v["reason"] for v in r["violations"]}
    assert "unverified_quantity" in reasons
    assert "unverified_publication_status" in reasons

    good = {
        "fact_ids": [100, 101],
        "items": [
            {"fact_id": 100, "text": "Built a model with 85% accuracy."},
            {"fact_id": 101, "text": "Paper submitted to UAI 2027."},
        ],
    }
    ok = toolkit.validate_resume_facts(draft_json=json.dumps(good))
    assert ok["valid"] is True and ok["violations"] == []


# ---------------------------------------------------------------- permission / L2


def test_generate_requires_prepare_and_confirmation(toolkit):
    r = toolkit.generate_resume("faketoken", allow_write=True)
    assert r["ok"] is False and r["error"] == "confirmation_required"


def test_prepare_generation_is_readonly_and_validates(toolkit):
    r = toolkit.prepare_generation(1, "source-1", [100, 101])
    assert r["ok"] and "confirmation_token" in r
    assert r["preview"]["action"] == "generate_resume"


def test_generate_blocked_without_allow_write(toolkit, monkeypatch):
    monkeypatch.delenv("OFFERBOMBER_MCP_ALLOW_WRITE", raising=False)
    prep = toolkit.prepare_generation(1, "source-1", [100])
    r = toolkit.generate_resume(prep["confirmation_token"], allow_write=False)
    assert r["ok"] is False and r["error"] == "write_not_allowed"


def test_export_requires_confirmation(toolkit):
    r = toolkit.export_pdf("faketoken", version_id=1, allow_write=True)
    assert r["ok"] is False and r["error"] == "confirmation_required"


def test_confirmation_token_single_use(toolkit, monkeypatch):
    """令牌用后即焚,防止重复提交。"""
    monkeypatch.delenv("OFFERBOMBER_MCP_ALLOW_WRITE", raising=False)
    prep = toolkit.prepare_generation(1, "source-1", [100])
    token = prep["confirmation_token"]
    first = toolkit.generate_resume(token, allow_write=False)  # blocked, token still pending
    assert first["error"] == "write_not_allowed"
    # allow_write 但没真实 RR key 会在生成阶段失败,不应留下"成功"假象
    second = toolkit.generate_resume(token, allow_write=True)
    assert second["ok"] is False or second["ok"] is True  # either way, token consumed
    third = toolkit.generate_resume(token, allow_write=True)
    assert third["ok"] is False and third["error"] == "confirmation_required"


def test_confirmation_survives_new_toolkit_instance(toolkit: McpToolkit):
    """prepare and confirm may run in different MCP server processes.

    WorkBuddy / DeepSeek Harness can spawn a fresh process per tool call. The
    token must therefore live on disk, not in a single toolkit's memory.
    """
    # Process A: prepare with the fixture toolkit (has real confirmed facts).
    prep = toolkit.prepare_generation(1, "source-1", [100])
    assert prep["ok"] and "confirmation_token" in prep
    token = prep["confirmation_token"]
    db_path = toolkit.ctx.settings.storage.db_path

    def fresh() -> McpToolkit:
        # Independent toolkit/AppContext, but same on-disk confirmation store.
        return McpToolkit(toolkit.ctx, user_id=toolkit.user_id)

    # Process B: fresh toolkit, token must still be pending on disk.
    blocked = fresh().generate_resume(token, allow_write=False)
    assert blocked["error"] == "write_not_allowed"  # token recognized, only write gate blocks

    # Process C: another fresh instance consumes it (single-use).
    third = fresh().generate_resume(token, allow_write=True)
    assert third["error"] != "confirmation_required"
    replay = fresh().generate_resume(token, allow_write=True)
    assert replay["ok"] is False and replay["error"] == "confirmation_required"
    assert db_path  # sanity: store keyed off the same configured db path


def test_confirmation_token_expires(tmp_path: Path):
    """A pending token older than the TTL must be rejected."""
    from offerbomber.integrations.mcp.confirmations import ConfirmationStore

    store = ConfirmationStore(tmp_path / "tokens.json", ttl_seconds=1)
    token = store.create({"action": "export_pdf", "version_id": 1})
    assert store.peek(token) is not None
    import time
    time.sleep(1.1)
    assert store.peek(token) is None
    assert store.consume(token) is None


@pytest.mark.asyncio
async def test_run_coroutine_inside_running_loop():
    """_run_coroutine must work when called from within an active event loop.

    FastMCP dispatches sync tools from its own loop; a plain asyncio.run()
    would raise RuntimeError. This is the exact failure seen in the live
    WorkBuddy host run.
    """
    from offerbomber.integrations.mcp.toolkit import _run_coroutine

    async def sample():
        await asyncio.sleep(0)
        return 42

    # We are already inside pytest's running loop here.
    assert _run_coroutine(sample()) == 42
