"""Retrieval, ResumePlan authenticity, persistence and migration tests."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.orm import sessionmaker

from offerbomber.core.resume_plan import ResumePlanBuilder
from offerbomber.core.retrieval import FactRetrievalService, RetrievalQuery
from offerbomber.profile.service import ProfileKnowledgeBase
from offerbomber.storage.base import Base, make_engine
from offerbomber.storage.fts import ensure_fts5

REPO_ROOT = Path(__file__).resolve().parents[1]


def _seeded_kb(sessions, user_id="tester"):
    kb = ProfileKnowledgeBase(sessions, user_id)
    experience_id = kb.add_experience(
        {
            "type": "PROJECT",
            "title": "大模型预训练项目",
            "description": "Research on large language model pretraining",
            "tags": ["PyTorch", "分布式训练"],
        },
        facts=[
            {
                "content": "使用 PyTorch 搭建分布式预训练流水线",
                "category": "METHOD",
                "skills": ["PyTorch", "分布式训练"],
            },
            {
                "content": "Evaluated data mixtures on downstream benchmarks",
                "category": "EXPERIMENT",
                "skills": ["Python"],
            },
        ],
        confirm_facts=True,
    )
    return kb, experience_id


def test_chinese_skill_and_project_retrieval(session_factory):
    _seeded_kb(session_factory)
    service = FactRetrievalService(session_factory, "tester")
    matches = service.search(
        RetrievalQuery(text="机器学习 预训练", required_skills=["PyTorch"], max_results=5)
    )
    assert matches
    assert matches[0].experience_id is not None
    assert matches[0].fact_id is not None
    assert matches[0].source_type is not None
    assert any("PyTorch" in reason or "预训练" in reason for reason in matches[0].reasons)


def test_english_skill_and_project_retrieval(session_factory):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    kb.add_experience(
        {"type": "PROJECT", "title": "Search Ranking System", "description": "Ranking pipeline"},
        facts=[{
            "content": "Built a ranking pipeline with Python and SQL",
            "category": "METHOD",
            "skills": ["Python", "SQL"],
        }],
        confirm_facts=True,
    )
    service = FactRetrievalService(session_factory, "tester")
    matches = service.search(
        RetrievalQuery(text="ranking pipeline", required_skills=["SQL"], max_results=5)
    )
    assert matches
    assert "SQL" in matches[0].skills
    assert any("ranking" in reason.lower() for reason in matches[0].reasons)


def test_ranking_by_job_requirements(session_factory):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    kb.add_experience(
        {"type": "PROJECT", "title": "Relevant Project"},
        facts=[{"content": "Built distributed training with Spark", "skills": ["Spark"]}],
        confirm_facts=True,
    )
    kb.add_experience(
        {"type": "PROJECT", "title": "Unrelated Project"},
        facts=[{"content": "Maintained documentation for distributed training"}],
        confirm_facts=True,
    )
    service = FactRetrievalService(session_factory, "tester")
    matches = service.search(
        RetrievalQuery(text="distributed training", required_skills=["Spark"], max_results=5)
    )
    assert matches
    assert matches[0].skills == ("Spark",)
    assert matches[0].score > matches[-1].score


def test_unconfirmed_facts_never_enter_resume_plan(session_factory):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    exp_id = kb.add_experience(
        {"type": "PAPER", "title": "ICML Submission"},
        facts=[
            {"content": "我的论文投了 ICML", "category": "RESULT"},
            {"content": "Reviewed related work on pretraining", "category": "METHOD"},
        ],
        confirm_facts=False,
    )
    unverified_id = kb.detail(exp_id)["facts"][0]["id"]

    retrieval = FactRetrievalService(session_factory, "tester")
    plan = ResumePlanBuilder(retrieval).build(
        RetrievalQuery(text="ICML pretraining", required_skills=[]),
        job_id="job-1",
    )
    assert unverified_id not in plan.selected_fact_ids
    assert any("unconfirmed" in warning for warning in plan.warnings)
    texts = [item.text for section in plan.sections for item in section.items]
    assert not any("Accepted" in text for text in texts)

    kb.verify_fact(unverified_id)
    verified_plan = ResumePlanBuilder(retrieval).build(
        RetrievalQuery(text="ICML", required_skills=[]), job_id="job-1"
    )
    assert unverified_id in verified_plan.selected_fact_ids
    verified_texts = [item.text for section in verified_plan.sections for item in section.items]
    assert "我的论文投了 ICML" in verified_texts
    assert not any("Accepted" in text for text in verified_texts)


def test_fact_edit_does_not_rewrite_history(session_factory):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    exp_id = kb.add_experience(
        {"type": "PROJECT", "title": "Original"},
        facts=[{"content": "Original wording", "category": "METHOD"}],
        confirm_facts=True,
    )
    kb.update(exp_id, {"title": "Renamed"}, note="rename after publication")
    old_snapshot = kb.show_version(exp_id, 1)
    assert old_snapshot["experience"]["title"] == "Original"
    assert old_snapshot["facts"][0]["content"] == "Original wording"
    assert kb.detail(exp_id)["title"] == "Renamed"


def test_data_survives_restart(tmp_path):
    db_path = tmp_path / "nested" / "profile.db"
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    ensure_fts5(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    kb, exp_id = _seeded_kb(sessions)
    engine.dispose()

    engine2 = make_engine(db_path)
    ensure_fts5(engine2)
    sessions2 = sessionmaker(bind=engine2, expire_on_commit=False)
    service = FactRetrievalService(sessions2, "tester")
    matches = service.search(RetrievalQuery(text="预训练", required_skills=["PyTorch"]))
    assert matches
    assert matches[0].experience_id == exp_id
    assert ProfileKnowledgeBase(sessions2, "tester").detail(exp_id)["facts"]
    engine2.dispose()


def test_alembic_migration_executes_on_fresh_database(tmp_path, monkeypatch):
    database = tmp_path / "migration.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OFFERBOMBER_DB_URL", f"sqlite:///{database.as_posix()}")
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(config, "head")

    from sqlalchemy import text

    engine = make_engine(database)
    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert {"experiences", "experience_facts", "experience_versions", "resume_imports"} <= tables
    assert "experience_fact_fts" in tables
    engine.dispose()
