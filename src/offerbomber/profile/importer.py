"""PDF import workflow: extract -> candidate -> review -> confirm -> persist."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..storage.models import Education, Experience, ExperienceFact, ResumeImport
from .parsing import DeterministicResumeParser, ResumeParser
from .pdf import PDFDocument, PDFTextExtractor, PyMuPDFExtractor
from .schema import CandidateProfile
from .service import (
    CONFIRMED,
    ProfileKnowledgeBase,
    _dumps,
    _sync_fact,
    utcnow,
)


@dataclass(frozen=True)
class ImportPreview:
    import_id: int
    duplicate: bool
    file_name: str
    page_count: int
    extractable: bool
    candidate: CandidateProfile
    warnings: list[str]


class ResumeImportService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        user_id: str = "default",
        *,
        extractor: PDFTextExtractor | None = None,
        parser: ResumeParser | None = None,
    ) -> None:
        self.sessions = sessions
        self.user_id = user_id
        self.extractor = extractor or PyMuPDFExtractor()
        self.parser = parser or DeterministicResumeParser()
        self.kb = ProfileKnowledgeBase(sessions, user_id)

    def preview(self, path: str | Path) -> ImportPreview:
        document: PDFDocument = self.extractor.extract(path)
        candidate = self.parser.parse(document)
        return self._store(document, candidate)

    async def apreview(self, path: str | Path, model_parser) -> ImportPreview:
        """Async preview using a model parser (e.g. ModelResumeParser.parse)."""
        document: PDFDocument = self.extractor.extract(path)
        candidate = await model_parser.parse(document)
        return self._store(document, candidate)

    def _store(self, document: PDFDocument, candidate: CandidateProfile) -> ImportPreview:
        with self.sessions() as session:
            existing = session.execute(
                select(ResumeImport).where(
                    ResumeImport.user_id == self.user_id,
                    ResumeImport.file_sha256 == document.file_sha256,
                )
            ).scalar_one_or_none()
            if existing is not None:
                stored_candidate = (
                    CandidateProfile.model_validate(json.loads(existing.candidate_payload))
                    if existing.candidate_payload
                    else CandidateProfile()
                )
                return ImportPreview(
                    import_id=existing.id,
                    duplicate=True,
                    file_name=existing.file_name,
                    page_count=existing.page_count or 0,
                    extractable=existing.status != "REJECTED",
                    candidate=stored_candidate,
                    warnings=[f"duplicate import (existing id={existing.id}, status={existing.status})"],
                )

            now = utcnow()
            rejected = not document.extractable
            row = ResumeImport(
                user_id=self.user_id,
                file_name=document.file_name,
                file_sha256=document.file_sha256,
                page_count=document.page_count,
                text_sha256=document.text_sha256,
                parser=candidate.parser,
                status="REJECTED" if rejected else "PENDING",
                candidate_payload=None if rejected else _dumps(candidate.model_dump()),
                error_message="no usable text layer; OCR is not available" if rejected else None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            return ImportPreview(
                import_id=row.id,
                duplicate=False,
                file_name=document.file_name,
                page_count=document.page_count,
                extractable=not rejected,
                candidate=candidate,
                warnings=candidate.warnings + ([row.error_message] if rejected else []),
            )

    def confirm(
        self,
        import_id: int,
        *,
        fields: set[str] | None = None,
        experience_indices: list[int] | None = None,
        education_indices: list[int] | None = None,
        fact_indices: dict[int, list[int]] | None = None,
    ) -> dict:
        """Write only explicitly selected candidates as verified facts."""
        selected_fields = fields or set()
        selected_exp = set(experience_indices or [])
        selected_edu = set(education_indices or [])
        fact_map = fact_indices or {}

        with self.sessions() as session:
            row = session.get(ResumeImport, import_id)
            if row is None or row.user_id != self.user_id:
                raise LookupError(f"import not found: {import_id}")
            if row.status == "IMPORTED":
                raise ValueError(f"import {import_id} was already confirmed")
            if not row.candidate_payload:
                raise ValueError(f"import {import_id} has no candidate data")
            candidate = CandidateProfile.model_validate(json.loads(row.candidate_payload))
            profile = self.kb.get_or_create_profile(session)
            pdf_source = self.kb.create_source(
                session,
                user_id=self.user_id,
                source_type="pdf",
                locator=row.file_name,
                evidence=f"sha256={row.file_sha256}",
                file_name=row.file_name,
                file_sha256=row.file_sha256,
                metadata={"page_count": row.page_count, "text_sha256": row.text_sha256},
            )

            field_values: dict[str, Any] = {}
            if "display_name" in selected_fields:
                field_values["display_name"] = candidate.display_name
            if "contact" in selected_fields:
                field_values["contact"] = candidate.contact
            if "summary" in selected_fields:
                field_values["summary"] = candidate.summary
            if "target_position" in selected_fields:
                field_values["target_position"] = candidate.target_position
            for key, value in field_values.items():
                if value is not None:
                    setattr(profile, key, value)
            profile.updated_at = utcnow()

            education_ids: list[int] = []
            for index in sorted(selected_edu):
                if index >= len(candidate.educations):
                    continue
                item = candidate.educations[index]
                now = utcnow()
                education = Education(
                    profile_id=profile.id,
                    school=item.school or "UNKNOWN",
                    degree=item.degree,
                    major=item.major,
                    start_date=item.start_date,
                    end_date=item.end_date,
                    description=item.description,
                    source_id=pdf_source.id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(education)
                session.flush()
                education_ids.append(education.id)

            experience_ids: list[int] = []
            fact_ids: list[int] = []
            indexed: list[ExperienceFact] = []
            for index in sorted(selected_exp):
                if index >= len(candidate.experiences):
                    continue
                item = candidate.experiences[index]
                now = utcnow()
                experience = Experience(
                    user_id=self.user_id,
                    profile_id=profile.id,
                    type=item.type,
                    title=item.title,
                    organization=item.organization,
                    location=item.location,
                    start_date=item.start_date,
                    end_date=item.end_date,
                    status="ACTIVE",
                    description=item.description,
                    tags=_dumps(sorted({s for f in item.facts for s in f.skills})) if item.facts else None,
                    source_id=pdf_source.id,
                    current_version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(experience)
                session.flush()
                experience_ids.append(experience.id)

                chosen = fact_map.get(index)
                chosen_set = set(chosen) if chosen is not None else set(range(len(item.facts)))
                for fact_index in sorted(chosen_set):
                    if fact_index >= len(item.facts):
                        continue
                    fact_item = item.facts[fact_index]
                    fact = ExperienceFact(
                        experience_id=experience.id,
                        content=fact_item.content,
                        category=fact_item.category,
                        skills=_dumps(fact_item.skills) if fact_item.skills else None,
                        source_id=pdf_source.id,
                        source_locator=fact_item.locator,
                        verification_status=CONFIRMED,
                        confidence=fact_item.confidence,
                        confirmed_at=now,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(fact)
                    session.flush()
                    fact_ids.append(fact.id)
                    indexed.append(fact)
                self.kb.snapshot(session, experience, "IMPORT", f"imported from {row.file_name}")
            row.status = "IMPORTED"
            row.updated_at = utcnow()
            for fact in indexed:
                _sync_fact(session, fact)
            session.commit()  # import + facts + index commit atomically
        return {
            "import_id": import_id,
            "experience_ids": experience_ids,
            "education_ids": education_ids,
            "fact_ids": fact_ids,
        }
