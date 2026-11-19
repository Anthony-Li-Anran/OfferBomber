"""Personal experience knowledge base operations.

The database is the source of truth. Every write that changes an experience
creates an immutable version snapshot; future resume generation can only read
facts whose verification_status is CONFIRMED.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..storage.fts import index_fact, reindex_experience
from ..storage.models import (
    Education,
    Experience,
    ExperienceFact,
    ExperienceSource,
    ExperienceVersion,
    Profile,
)

EXPERIENCE_TYPES = {
    "WORK", "INTERNSHIP", "RESEARCH", "PROJECT", "PAPER",
    "COMPETITION", "AWARD", "OPEN_SOURCE", "CUSTOM",
}
# Every operation that changes indexed content must go through one of these
# helpers. Never commit inside an index helper — the caller owns the transaction.
def _sync_fact(session: Session, fact: ExperienceFact) -> None:
    """Index or un-index a fact according to its current state. No commit."""
    index_fact(session, fact)


EXPERIENCE_FIELDS = {
    "type", "title", "organization", "location", "start_date", "end_date",
    "status", "description", "tags",
}
CONFIRMED = "CONFIRMED"
UNVERIFIED = "UNVERIFIED"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ProfileKnowledgeBase:
    def __init__(self, sessions: sessionmaker[Session], user_id: str = "default") -> None:
        self.sessions = sessions
        self.user_id = user_id

    # -- profile ---------------------------------------------------------
    def get_or_create_profile(self, session: Session) -> Profile:
        profile = session.execute(
            select(Profile).where(Profile.user_id == self.user_id)
        ).scalar_one_or_none()
        if profile is None:
            now = utcnow()
            profile = Profile(user_id=self.user_id, created_at=now, updated_at=now)
            session.add(profile)
            session.flush()
        return profile

    def upsert_profile(self, fields: Mapping[str, Any]) -> dict:
        with self.sessions() as session:
            profile = self.get_or_create_profile(session)
            for key in ("display_name", "contact", "summary", "target_position", "job_search_status"):
                if key in fields and fields[key] is not None:
                    setattr(profile, key, fields[key])
            if fields.get("target_locations") is not None:
                profile.target_locations = _dumps(fields["target_locations"])
            if fields.get("job_preferences") is not None:
                profile.job_preferences = _dumps(fields["job_preferences"])
            profile.updated_at = utcnow()
            session.commit()
            return self._profile_dict(profile)

    def show(self) -> dict:
        with self.sessions() as session:
            profile = session.execute(
                select(Profile).where(Profile.user_id == self.user_id)
            ).scalar_one_or_none()
            if profile is None:
                return {"user_id": self.user_id, "profile": None, "educations": [], "experiences": []}
            educations = session.execute(
                select(Education).where(Education.profile_id == profile.id)
            ).scalars().all()
            experiences = session.execute(
                select(Experience)
                .where(Experience.user_id == self.user_id)
                .order_by(Experience.id)
            ).scalars().all()
            return {
                "user_id": self.user_id,
                "profile": self._profile_dict(profile),
                "educations": [self._education_dict(e) for e in educations],
                "experiences": [self._experience_dict(e) for e in experiences],
            }

    @staticmethod
    def _profile_dict(profile: Profile) -> dict:
        return {
            "id": profile.id,
            "user_id": profile.user_id,
            "display_name": profile.display_name,
            "contact": profile.contact,
            "summary": profile.summary,
            "target_position": profile.target_position,
            "target_locations": profile.target_locations,
            "job_search_status": profile.job_search_status,
        }

    @staticmethod
    def _education_dict(education: Education) -> dict:
        return {
            "id": education.id,
            "school": education.school,
            "degree": education.degree,
            "major": education.major,
            "start_date": education.start_date,
            "end_date": education.end_date,
            "description": education.description,
            "source_id": education.source_id,
        }

    @staticmethod
    def _experience_dict(experience: Experience) -> dict:
        return {
            "id": experience.id,
            "type": experience.type,
            "title": experience.title,
            "organization": experience.organization,
            "location": experience.location,
            "start_date": experience.start_date,
            "end_date": experience.end_date,
            "status": experience.status,
            "description": experience.description,
            "tags": experience.tags,
            "source_id": experience.source_id,
            "current_version": experience.current_version,
            "archived_at": experience.archived_at.isoformat() if experience.archived_at else None,
        }

    @staticmethod
    def _fact_dict(fact: ExperienceFact) -> dict:
        return {
            "id": fact.id,
            "experience_id": fact.experience_id,
            "content": fact.content,
            "category": fact.category,
            "skills": fact.skills,
            "source_id": fact.source_id,
            "source_locator": fact.source_locator,
            "verification_status": fact.verification_status,
            "confidence": fact.confidence,
            "version": fact.version,
            "is_deleted": fact.is_deleted,
        }

    # -- sources and snapshots -------------------------------------------
    @staticmethod
    def create_source(
        session: Session,
        *,
        source_type: str = "manual",
        locator: str | None = None,
        evidence: str | None = None,
        file_name: str | None = None,
        file_sha256: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        user_id: str = "default",
    ) -> ExperienceSource:
        now = utcnow()
        source = ExperienceSource(
            user_id=user_id,
            source_type=source_type,
            locator=locator,
            evidence=evidence,
            file_name=file_name,
            file_sha256=file_sha256,
            metadata_json=_dumps(dict(metadata)) if metadata else None,
            created_at=now,
            updated_at=now,
        )
        session.add(source)
        session.flush()
        return source

    def snapshot(self, session: Session, experience: Experience, event: str, note: str | None) -> ExperienceVersion:
        facts = session.execute(
            select(ExperienceFact)
            .where(ExperienceFact.experience_id == experience.id)
            .order_by(ExperienceFact.id)
        ).scalars().all()
        payload = {
            "experience": self._experience_dict(experience),
            "facts": [self._fact_dict(fact) for fact in facts],
        }
        now = utcnow()
        version = ExperienceVersion(
            experience_id=experience.id,
            version=experience.current_version,
            event_type=event,
            snapshot=_dumps(payload),
            change_note=note,
            created_at=now,
            updated_at=now,
        )
        session.add(version)
        session.flush()
        return version

    # -- experience CRUD --------------------------------------------------
    def add_experience(
        self,
        data: Mapping[str, Any],
        *,
        facts: list[Mapping[str, Any]] | None = None,
        source: Mapping[str, Any] | None = None,
        confirm_facts: bool = False,
    ) -> int:
        exp_type = str(data.get("type", "CUSTOM")).upper()
        if exp_type not in EXPERIENCE_TYPES:
            raise ValueError(f"unsupported experience type: {exp_type}")
        title = str(data.get("title") or "").strip()
        if not title:
            raise ValueError("experience title is required")
        with self.sessions() as session:
            profile = self.get_or_create_profile(session)
            source_row = None
            if source:
                source_row = self.create_source(session, user_id=self.user_id, **dict(source))
            now = utcnow()
            experience = Experience(
                user_id=self.user_id,
                profile_id=profile.id,
                type=exp_type,
                title=title,
                organization=data.get("organization"),
                location=data.get("location"),
                start_date=data.get("start_date"),
                end_date=data.get("end_date"),
                status=str(data.get("status") or "ACTIVE").upper(),
                description=data.get("description"),
                tags=_dumps(data["tags"]) if isinstance(data.get("tags"), list) else data.get("tags"),
                source_id=source_row.id if source_row else None,
                current_version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(experience)
            session.flush()
            fact_rows: list[ExperienceFact] = []
            for fact in facts or []:
                content = str(fact.get("content") or "").strip()
                if not content:
                    continue
                fact_source = source_row
                if fact.get("source"):
                    fact_source = self.create_source(
                        session, user_id=self.user_id, **dict(fact["source"])
                    )
                elif fact_source is None:
                    # Manual input is itself a traceable provenance record.
                    fact_source = self.create_source(
                        session,
                        user_id=self.user_id,
                        source_type="user_confirmed" if confirm_facts else "manual",
                        evidence="manual input via experience add",
                    )
                fact_source_id = fact_source.id if fact_source else None
                row = ExperienceFact(
                    experience_id=experience.id,
                    content=content,
                    category=fact.get("category") or exp_type,
                    skills=_dumps(fact["skills"]) if isinstance(fact.get("skills"), list) else fact.get("skills"),
                    source_id=fact_source_id,
                    source_locator=fact.get("locator"),
                    verification_status=CONFIRMED if confirm_facts else UNVERIFIED,
                    confidence=float(fact.get("confidence") or 0.0),
                    confirmed_at=now if confirm_facts else None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                fact_rows.append(row)
            self.snapshot(session, experience, "CREATE", "experience created")
            for row in fact_rows:
                _sync_fact(session, row)
            session.commit()  # fact rows + index rows commit atomically
            return experience.id

    def list_experiences(self, *, include_archived: bool = False) -> list[dict]:
        with self.sessions() as session:
            stmt = select(Experience).where(Experience.user_id == self.user_id)
            if not include_archived:
                stmt = stmt.where(Experience.status != "ARCHIVED")
            rows = session.execute(stmt.order_by(Experience.id)).scalars().all()
            return [self._experience_dict(row) for row in rows]

    def detail(self, experience_id: int) -> dict:
        with self.sessions() as session:
            experience = self._get_experience(session, experience_id)
            facts = session.execute(
                select(ExperienceFact)
                .where(
                    ExperienceFact.experience_id == experience_id,
                    ExperienceFact.is_deleted.is_(False),
                )
                .order_by(ExperienceFact.id)
            ).scalars().all()
            result = self._experience_dict(experience)
            result["facts"] = [self._fact_dict(fact) for fact in facts]
            return result

    def _get_experience(self, session: Session, experience_id: int) -> Experience:
        experience = session.get(Experience, experience_id)
        if experience is None or experience.user_id != self.user_id:
            raise LookupError(f"experience not found: {experience_id}")
        return experience

    def update(self, experience_id: int, changes: Mapping[str, Any], note: str | None = None) -> dict:
        updates = {k: v for k, v in changes.items() if k in EXPERIENCE_FIELDS and v is not None}
        if not updates:
            raise ValueError("no updatable fields provided")
        if "type" in updates:
            exp_type = str(updates["type"]).upper()
            if exp_type not in EXPERIENCE_TYPES:
                raise ValueError(f"unsupported experience type: {exp_type}")
            updates["type"] = exp_type
        if isinstance(updates.get("tags"), list):
            updates["tags"] = _dumps(updates["tags"])
        with self.sessions() as session:
            experience = self._get_experience(session, experience_id)
            for key, value in updates.items():
                setattr(experience, key, value)
            experience.current_version += 1
            experience.updated_at = utcnow()
            self.snapshot(session, experience, "UPDATE", note)
            # The index embeds experience title and description; any edit to
            # them must reindex every fact of this experience in this transaction.
            reindex_experience(session, experience_id)
            session.commit()
            return self._experience_dict(experience)

    def history(self, experience_id: int) -> list[dict]:
        with self.sessions() as session:
            self._get_experience(session, experience_id)
            versions = session.execute(
                select(ExperienceVersion)
                .where(ExperienceVersion.experience_id == experience_id)
                .order_by(ExperienceVersion.version)
            ).scalars().all()
            return [
                {
                    "version": v.version,
                    "event_type": v.event_type,
                    "change_note": v.change_note,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                }
                for v in versions
            ]

    def show_version(self, experience_id: int, version: int) -> dict:
        with self.sessions() as session:
            self._get_experience(session, experience_id)
            row = session.execute(
                select(ExperienceVersion).where(
                    ExperienceVersion.experience_id == experience_id,
                    ExperienceVersion.version == version,
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError(f"version {version} not found for experience {experience_id}")
            return json.loads(row.snapshot)

    def restore(self, experience_id: int, version: int) -> dict:
        """Restore an experience to an older immutable snapshot.

        Invariants (Part A3):
        - Fact IDs stay stable: a fact present in the snapshot is updated in
          place, never re-created with a new ID.
        - Fact versions never regress: restoring content bumps the version
          monotonically instead of writing the old snapshot number back.
        - Confirmation is monotonic: a fact that is currently CONFIRMED cannot
          be downgraded to UNVERIFIED by restoring an older snapshot.
        - Archival status is lifecycle state, not content: it is preserved
          across a content restore.
        - Historical snapshots are immutable: we only append a new RESTORE
          version; previously stored snapshots are never modified.
        - FTS index is rebuilt for the experience inside the same transaction.
        """
        with self.sessions() as session:
            experience = self._get_experience(session, experience_id)
            row = session.execute(
                select(ExperienceVersion).where(
                    ExperienceVersion.experience_id == experience_id,
                    ExperienceVersion.version == version,
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError(f"version {version} not found for experience {experience_id}")
            payload = json.loads(row.snapshot)
            saved = payload["experience"]

            # status/archived_at are lifecycle fields: do not restore them.
            for field in EXPERIENCE_FIELDS - {"status"}:
                if field in saved:
                    setattr(experience, field, saved[field])

            existing = {
                fact.id: fact
                for fact in session.execute(
                    select(ExperienceFact).where(ExperienceFact.experience_id == experience_id)
                ).scalars().all()
            }
            snapshot_ids: set[int] = set()
            for item in payload["facts"]:
                item_id = item.get("id")
                fact = existing.get(item_id)
                if fact is None:
                    # Preserve the original fact ID so provenance and history stay stable.
                    fact = ExperienceFact(
                        id=item_id,
                        experience_id=experience_id,
                        created_at=utcnow(),
                    )
                    session.add(fact)
                    existing[item_id] = fact
                for field in ("content", "category", "skills", "source_id", "source_locator", "confidence"):
                    setattr(fact, field, item.get(field))
                # Confirmation is monotonic — never resurrect an old UNVERIFIED
                # status over an already confirmed fact.
                snapshot_status = item.get("verification_status", UNVERIFIED)
                if fact.verification_status != CONFIRMED:
                    fact.verification_status = snapshot_status
                fact.version = max(int(fact.version or 0), int(item.get("version") or 0)) + 1
                fact.is_deleted = False
                fact.updated_at = utcnow()
                session.flush()
                snapshot_ids.add(item_id)

            # Facts added after the restored snapshot become soft-deleted, not
            # physically removed — their IDs and history remain reserved.
            for fact_id, fact in existing.items():
                if fact_id not in snapshot_ids and not fact.is_deleted:
                    fact.is_deleted = True
                    fact.updated_at = utcnow()

            experience.current_version += 1
            experience.updated_at = utcnow()
            # Append a new immutable RESTORE snapshot; never rewrite `row`.
            self.snapshot(session, experience, "RESTORE", f"restored from version {version}")
            reindex_experience(session, experience_id)
            session.commit()
            return self.detail(experience_id)

    def archive(self, experience_id: int) -> dict:
        return self._set_status(experience_id, "ARCHIVED", "ARCHIVE", "experience archived")

    def unarchive(self, experience_id: int) -> dict:
        return self._set_status(experience_id, "ACTIVE", "RESTORE", "experience restored")

    def _set_status(self, experience_id: int, status: str, event: str, note: str) -> dict:
        with self.sessions() as session:
            experience = self._get_experience(session, experience_id)
            experience.status = status
            experience.archived_at = utcnow() if status == "ARCHIVED" else None
            experience.current_version += 1
            experience.updated_at = utcnow()
            self.snapshot(session, experience, event, note)
            session.commit()
            return self._experience_dict(experience)

    # -- facts ------------------------------------------------------------
    def add_fact(
        self,
        experience_id: int,
        *,
        content: str,
        category: str = "OTHER",
        skills: list[str] | None = None,
        source_type: str = "manual",
        locator: str | None = None,
        evidence: str | None = None,
        confirm: bool = False,
    ) -> int:
        content = content.strip()
        if not content:
            raise ValueError("fact content is required")
        with self.sessions() as session:
            experience = self._get_experience(session, experience_id)
            source = self.create_source(
                session,
                user_id=self.user_id,
                source_type="user_confirmed" if confirm else source_type,
                locator=locator,
                evidence=evidence,
            )
            now = utcnow()
            fact = ExperienceFact(
                experience_id=experience_id,
                content=content,
                category=category,
                skills=_dumps(skills) if skills else None,
                source_id=source.id,
                source_locator=locator,
                verification_status=CONFIRMED if confirm else UNVERIFIED,
                confidence=1.0 if confirm else 0.0,
                confirmed_at=now if confirm else None,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(fact)
            session.flush()
            experience.current_version += 1
            experience.updated_at = now
            self.snapshot(session, experience, "FACT_ADD", "fact added")
            _sync_fact(session, fact)
            session.commit()  # fact + index in one transaction
            return fact.id

    def verify_fact(self, fact_id: int) -> dict:
        with self.sessions() as session:
            fact = session.get(ExperienceFact, fact_id)
            if fact is None or fact.is_deleted:
                raise LookupError(f"fact not found: {fact_id}")
            experience = self._get_experience(session, fact.experience_id)
            if fact.verification_status != CONFIRMED:
                fact.verification_status = CONFIRMED
                fact.confidence = max(fact.confidence, 1.0)
                fact.confirmed_at = utcnow()
                fact.version += 1
                fact.updated_at = utcnow()
                experience.current_version += 1
                experience.updated_at = utcnow()
                self.snapshot(session, experience, "FACT_VERIFY", f"fact {fact_id} confirmed")
                # Reindex so BM25/skills reflect the confirmed fact consistently.
                _sync_fact(session, fact)
                session.commit()
            return self._fact_dict(fact)

    def delete_fact(self, fact_id: int) -> None:
        with self.sessions() as session:
            fact = session.get(ExperienceFact, fact_id)
            if fact is None:
                raise LookupError(f"fact not found: {fact_id}")
            experience = self._get_experience(session, fact.experience_id)
            if not fact.is_deleted:
                fact.is_deleted = True
                fact.updated_at = utcnow()
                experience.current_version += 1
                experience.updated_at = utcnow()
                self.snapshot(session, experience, "FACT_DELETE", f"fact {fact_id} deleted")
                # index_fact sees is_deleted=True and removes the row in-transaction.
                _sync_fact(session, fact)
                session.commit()
