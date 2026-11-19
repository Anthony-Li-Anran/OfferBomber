"""Part A: FTS index consistency, transactional writes, version-restore edges."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from offerbomber.core.retrieval import FactRetrievalService, RetrievalQuery
from offerbomber.profile import service as service_module
from offerbomber.profile.service import ProfileKnowledgeBase
from offerbomber.storage.fts import IndexingError, consistency_report, rebuild_fts


def _confirmed_fact(session_factory, *, title="Distributed Training", content="built a distributed training pipeline with PyTorch", skills=None):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    eid = kb.add_experience(
        {"type": "RESEARCH", "title": title, "description": "foundation models"},
        facts=[{"content": content, "category": "METHOD", "skills": skills or ["PyTorch"]}],
        confirm_facts=True,
    )
    return kb, eid


def _search(session_factory, query, **kwargs):
    return FactRetrievalService(session_factory, "tester").search(
        RetrievalQuery(text=query, max_results=10, **kwargs)
    )


def test_edit_experience_title_updates_search(session_factory):
    kb, eid = _confirmed_fact(session_factory, title="Alpha Project")
    assert _search(session_factory, "Alpha")

    kb.update(eid, {"title": "Beta Project"}, note="rename")
    after = _search(session_factory, "Beta")
    assert after and after[0].experience_title == "Beta Project"
    assert not _search(session_factory, "Alpha")
    with session_factory() as session:
        assert consistency_report(session)["consistent"]


def test_edit_description_updates_index_content(session_factory):
    kb, eid = _confirmed_fact(session_factory)
    kb.update(eid, {"description": "quantum annealing research"})
    assert _search(session_factory, "quantum annealing")
    with session_factory() as session:
        assert consistency_report(session)["consistent"]


def test_deleted_fact_not_searchable(session_factory):
    kb, eid = _confirmed_fact(session_factory, content="unique token zebra technique")
    assert _search(session_factory, "zebra")
    fact_id = kb.detail(eid)["facts"][0]["id"]

    kb.delete_fact(fact_id)
    assert not _search(session_factory, "zebra")
    with session_factory() as session:
        report = consistency_report(session)
        assert not report["stale"]  # deleted fact removed from index
        assert report["consistent"]


def test_restore_rebuilds_index_and_preserves_invariants(session_factory):
    kb, eid = _confirmed_fact(session_factory, title="Original Title", content="original content alpha")
    fact_id = kb.detail(eid)["facts"][0]["id"]

    kb.update(eid, {"title": "Changed Title"}, note="rename")
    kb.add_fact(eid, content="later added beta fact", confirm=True)
    assert _search(session_factory, "beta")

    result = kb.restore(eid, 1)

    assert result["title"] == "Original Title"
    assert _search(session_factory, "alpha")
    assert not _search(session_factory, "beta")
    # Lifecycle status is a normal ACTIVE experience after a plain content restore.
    assert result["status"] == "ACTIVE"
    restored = [f for f in result["facts"] if f["id"] == fact_id]
    assert restored and restored[0]["version"] > 1
    assert restored[0]["verification_status"] == "CONFIRMED"
    # Historical snapshot is immutable.
    assert kb.show_version(eid, 1)["experience"]["title"] == "Original Title"
    with session_factory() as session:
        assert consistency_report(session)["consistent"]


def test_content_restore_preserves_archived_status(session_factory):
    kb, eid = _confirmed_fact(session_factory, title="Was Active", content="archived content zeta")
    kb.archive(eid)
    assert kb.detail(eid)["status"] == "ARCHIVED"

    result = kb.restore(eid, 1)  # snapshot v1 was taken while ACTIVE
    assert result["status"] == "ARCHIVED"  # lifecycle state not overwritten by content restore
    with session_factory() as session:
        assert consistency_report(session)["consistent"]


def test_restore_does_not_downgrade_confirmed_fact(session_factory):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    eid = kb.add_experience(
        {"type": "PROJECT", "title": "Draft Work"},
        facts=[{"content": "unverified claim gamma", "category": "METHOD"}],
        confirm_facts=False,
    )
    kb.verify_fact(kb.detail(eid)["facts"][0]["id"])  # v2: CONFIRMED
    kb.update(eid, {"title": "Renamed"})
    kb.restore(eid, 1)  # snapshot v1 had UNVERIFIED

    fact = kb.detail(eid)["facts"][0]
    assert fact["verification_status"] == "CONFIRMED"  # never downgraded
    assert _search(session_factory, "gamma", verified_only=True)


def test_index_failure_rolls_back_fact(session_factory, monkeypatch):
    kb = ProfileKnowledgeBase(session_factory, "tester")

    def boom(session, fact):
        raise IndexingError("injected index failure")

    monkeypatch.setattr(service_module, "index_fact", boom)
    with pytest.raises(IndexingError):
        kb.add_experience(
            {"type": "PROJECT", "title": "Atomic Test"},
            facts=[{"content": "should not persist delta"}],
            confirm_facts=True,
        )

    assert not _search(session_factory, "Atomic Test")
    assert not _search(session_factory, "delta")
    with session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM experiences")).scalar() == 0
        assert session.execute(text("SELECT count(*) FROM experience_facts")).scalar() == 0


def test_rebuild_does_not_mutate_facts(session_factory):
    kb, eid = _confirmed_fact(session_factory, content="rebuild me epsilon")
    before = kb.detail(eid)
    with session_factory() as session:
        indexed = rebuild_fts(session)
        session.commit()
    after = kb.detail(eid)
    assert indexed == 1
    assert before == after
    assert _search(session_factory, "epsilon")
