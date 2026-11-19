"""SQLite FTS5 index for fact-level retrieval.

SQLite's unicode61 tokenizer does not segment Chinese text into words, so a
`bigrams` column indexes overlapping two-character CJK tokens alongside whole
runs. Retrieval keeps a LIKE fallback for engines without FTS5.

Index maintenance is centralised here on purpose. Every primitive in this
module mutates ONLY the index table and NEVER commits — the caller owns the
transaction. This makes it possible to commit fact rows and their index rows
atomically, and there is exactly one implementation of "what does a fact look
like in the index" used by incremental writes and full rebuilds alike.
"""

from __future__ import annotations

import re

from sqlalchemy import Engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import Experience, ExperienceFact

FTS_TABLE = "experience_fact_fts"
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")


class IndexingError(RuntimeError):
    """Raised when the FTS index could not be maintained.

    The caller is expected to let this propagate so the surrounding transaction
    rolls back; a half-written fact/index pair must never be committed.
    """


def cjk_bigrams(text: str) -> list[str]:
    tokens: list[str] = []
    for run in _CJK_RUN.findall(text or ""):
        tokens.append(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def search_text(content: str, skills: str, category: str) -> str:
    raw = " ".join(part for part in (content, skills, category) if part)
    return " ".join([raw] + cjk_bigrams(raw))


def ensure_fts5(engine: Engine) -> bool:
    """Create the virtual table when FTS5 is available; return availability."""
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
                    "fact_id UNINDEXED, content, skills, category, bigrams, "
                    "tokenize='unicode61 remove_diacritics 2')"
                )
            )
        return True
    except SQLAlchemyError:
        return False


def fts_available(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text(f"SELECT count(*) FROM {FTS_TABLE}"))
        return True
    except SQLAlchemyError:
        return False


def _index_values(session: Session, fact: ExperienceFact) -> dict | None:
    """Compute the single canonical index row for a fact.

    Returns None when the fact is soft-deleted or its experience is missing,
    so the caller can remove any stale row instead.
    """
    if fact.is_deleted:
        return None
    exp = session.get(Experience, fact.experience_id)
    if exp is None:
        return None
    content = " ".join([fact.content, exp.title, exp.description or ""])
    skills = fact.skills or ""
    category = fact.category or ""
    return {
        "fact_id": fact.id,
        "content": content,
        "skills": skills,
        "category": category,
        "bigrams": " ".join(cjk_bigrams(fact.content + " " + skills + " " + exp.title)),
    }


_INSERT = (
    f"INSERT INTO {FTS_TABLE}(fact_id, content, skills, category, bigrams) "
    "VALUES (:fact_id, :content, :skills, :category, :bigrams)"
)


def remove_fact_index(session: Session, fact_id: int) -> None:
    """Remove one fact from the index. No commit."""
    try:
        session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE fact_id = :fact_id"), {"fact_id": fact_id})
    except SQLAlchemyError as exc:
        raise IndexingError(f"failed to remove fact {fact_id} from index: {exc}") from exc


def index_fact(session: Session, fact: ExperienceFact) -> None:
    """Upsert one fact into the index (idempotent). No commit.

    Deleted facts (or facts whose experience vanished) are removed instead of
    inserted, which keeps the index authoritative after soft deletes.
    """
    try:
        session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE fact_id = :fact_id"), {"fact_id": fact.id})
        values = _index_values(session, fact)
        if values is not None:
            session.execute(text(_INSERT), values)
    except SQLAlchemyError as exc:
        raise IndexingError(f"failed to index fact {fact.id}: {exc}") from exc


def reindex_experience(session: Session, experience_id: int) -> int:
    """Rebuild index rows for every non-deleted fact of one experience. No commit.

    Used after any change that affects indexed content but is not a fact-level
    write, e.g. editing an experience title or description, or restoring a
    version snapshot. Returns the number of facts indexed.
    """
    facts = session.execute(
        select(ExperienceFact).where(ExperienceFact.experience_id == experience_id).order_by(ExperienceFact.id)
    ).scalars().all()
    try:
        session.execute(
            text(f"DELETE FROM {FTS_TABLE} WHERE fact_id IN (SELECT id FROM experience_facts WHERE experience_id = :eid)"),
            {"eid": experience_id},
        )
        count = 0
        for fact in facts:
            values = _index_values(session, fact)
            if values is not None:
                session.execute(text(_INSERT), values)
                count += 1
    except SQLAlchemyError as exc:
        raise IndexingError(f"failed to reindex experience {experience_id}: {exc}") from exc
    return count


def rebuild_fts(session: Session) -> int:
    """Rebuild the whole index from authoritative fact rows. No commit.

    Does not modify any ExperienceFact — it only rewrites the derived index.
    """
    facts = session.execute(
        select(ExperienceFact, Experience.title, Experience.description)
        .join(Experience, Experience.id == ExperienceFact.experience_id)
        .where(ExperienceFact.is_deleted.is_(False))
    ).all()
    try:
        session.execute(text(f"DELETE FROM {FTS_TABLE}"))
        for fact, _exp_title, _exp_description in facts:
            values = _index_values(session, fact)
            if values is not None:
                session.execute(text(_INSERT), values)
    except SQLAlchemyError as exc:
        raise IndexingError(f"failed to rebuild index: {exc}") from exc
    return len(facts)


def consistency_report(session: Session) -> dict:
    """Compare authoritative facts with index rows; read-only diagnostic."""
    expected = {
        row[0]
        for row in session.execute(
            select(ExperienceFact.id).where(ExperienceFact.is_deleted.is_(False))
        ).all()
    }
    indexed = {row[0] for row in session.execute(text(f"SELECT fact_id FROM {FTS_TABLE}")).all()}
    return {
        "expected": len(expected),
        "indexed": len(indexed),
        "missing": sorted(expected - indexed),
        "stale": sorted(indexed - expected),
        "consistent": expected == indexed,
    }
