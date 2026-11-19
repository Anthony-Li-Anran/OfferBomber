"""PDF import and experience management tests (task book Part C items 3-8)."""

from __future__ import annotations

import json

import pymupdf
import pytest

from offerbomber.profile.importer import ResumeImportService
from offerbomber.profile.pdf import PyMuPDFExtractor
from offerbomber.profile.service import ProfileKnowledgeBase
from offerbomber.storage.models import ResumeImport
from sqlalchemy import select

SAMPLE_RESUME = """Jane Doe
jane.doe@example.com
+86 13800138000
SUMMARY
Machine learning researcher focused on pretraining.
EDUCATION
Peking University Master Computer Science 2020 - 2023
RESEARCH
Research on large model pretraining
- Built a distributed training pipeline with PyTorch
- Evaluated data mixtures on downstream benchmarks
SKILLS
Python, PyTorch, SQL, Linux
"""


def _make_pdf(path):
    document = pymupdf.open()
    page = document.new_page()
    for index, line in enumerate(SAMPLE_RESUME.splitlines()):
        page.insert_text((72, 72 + index * 16), line, fontsize=10)
    document.save(str(path))
    document.close()
    return path


def test_pdf_text_extraction(tmp_path):
    document = PyMuPDFExtractor().extract(_make_pdf(tmp_path / "resume.pdf"))
    assert document.extractable is True
    assert document.page_count == 1
    assert "Machine learning researcher" in document.text
    assert len(document.file_sha256) == 64


def test_duplicate_pdf_import(session_factory, tmp_path):
    pdf = _make_pdf(tmp_path / "resume.pdf")
    service1 = ResumeImportService(session_factory, "tester")
    first = service1.preview(pdf)

    service2 = ResumeImportService(session_factory, "tester")
    second = service2.preview(pdf)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.import_id == first.import_id
    with session_factory() as session:
        assert len(session.execute(select(ResumeImport)).scalars().all()) == 1


def test_confirmed_import_writes_facts_and_source(session_factory, tmp_path):
    service = ResumeImportService(session_factory, "tester")
    preview = service.preview(_make_pdf(tmp_path / "resume.pdf"))
    assert preview.candidate.experiences

    result = service.confirm(
        preview.import_id,
        fields={"display_name", "contact"},
        experience_indices=[0],
    )
    assert result["experience_ids"] and result["fact_ids"]
    kb = ProfileKnowledgeBase(session_factory, "tester")
    detail = kb.detail(result["experience_ids"][0])
    assert all(fact["verification_status"] == "CONFIRMED" for fact in detail["facts"])
    assert all(fact["source_id"] for fact in detail["facts"])
    assert kb.show()["profile"]["display_name"] == "Jane Doe"


def test_create_complete_experience(session_factory):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    kb.upsert_profile(
        {
            "display_name": "Jane Doe",
            "contact": "jane@example.com",
            "target_position": "ML Engineer",
        }
    )
    experience_id = kb.add_experience(
        {
            "type": "RESEARCH", "title": "Pretraining Research",
            "organization": "AI Lab", "location": "Beijing",
            "start_date": "2024-01", "end_date": "2025-01",
            "description": "Research on foundation models",
            "tags": ["Python", "PyTorch"],
        },
        facts=[
            {"content": "Designed the data mixture", "category": "EXPERIMENT", "skills": ["Python"]},
            {"content": "Trained a 7B model", "category": "RESULT", "skills": ["PyTorch"]},
        ],
        confirm_facts=True,
    )
    detail = kb.detail(experience_id)
    assert detail["type"] == "RESEARCH"
    assert detail["organization"] == "AI Lab"
    assert len(detail["facts"]) == 2
    assert json.loads(detail["tags"]) == ["Python", "PyTorch"]
    assert kb.show()["profile"]["target_position"] == "ML Engineer"


def test_add_fact_and_verify(session_factory):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    experience_id = kb.add_experience({"type": "PROJECT", "title": "Ranking System"})
    fact_id = kb.add_fact(
        experience_id,
        content="Implemented BM25 ranking",
        category="METHOD",
        skills=["Python"],
        confirm=False,
    )
    detail = kb.detail(experience_id)
    assert detail["facts"][0]["verification_status"] == "UNVERIFIED"

    verified = kb.verify_fact(fact_id)
    assert verified["verification_status"] == "CONFIRMED"
    assert verified["version"] == 2
    assert kb.detail(experience_id)["facts"][0]["verification_status"] == "CONFIRMED"


def test_edit_and_history_are_immutable(session_factory):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    experience_id = kb.add_experience({"type": "PROJECT", "title": "Original Title"})
    kb.update(experience_id, {"title": "Updated Title"}, note="rename")

    history = kb.history(experience_id)
    assert [item["version"] for item in history] == [1, 2]
    old = kb.show_version(experience_id, 1)
    assert old["experience"]["title"] == "Original Title"
    assert kb.detail(experience_id)["title"] == "Updated Title"


def test_archive_and_unarchive(session_factory):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    experience_id = kb.add_experience({"type": "WORK", "title": "Engineer"})
    kb.archive(experience_id)
    assert experience_id not in [e["id"] for e in kb.list_experiences()]
    assert experience_id in [e["id"] for e in kb.list_experiences(include_archived=True)]

    kb.unarchive(experience_id)
    assert experience_id in [e["id"] for e in kb.list_experiences()]
    assert kb.history(experience_id)[-1]["event_type"] == "RESTORE"


def test_unsupported_experience_type_rejected(session_factory):
    kb = ProfileKnowledgeBase(session_factory, "tester")
    with pytest.raises(ValueError):
        kb.add_experience({"type": "MADE_UP", "title": "Invalid"})
