"""SQLite FTS5 index for fact-level retrieval.

SQLite's unicode61 tokenizer does not segment Chinese text into words, so a
`bigrams` column indexes overlapping two-character CJK tokens alongside whole
runs. Retrieval keeps a LIKE fallback for engines without FTS5.
"""

from __future__ import annotations

import re

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from .models import Experience, ExperienceFact

FTS_TABLE = "experience_fact_fts"
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")


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


def rebuild_fts(session) -> int:
    """Rebuild the index from facts. Safe to call repeatedly and after restart."""
    from sqlalchemy import select

    session.execute(text(f"DELETE FROM {FTS_TABLE}"))
    facts = session.execute(
        select(ExperienceFact, Experience.title, Experience.description)
        .join(Experience, Experience.id == ExperienceFact.experience_id)
        .where(ExperienceFact.is_deleted.is_(False))
    ).all()
    for fact, exp_title, exp_description in facts:
        skills = fact.skills or ""
        category = fact.category or ""
        session.execute(
            text(
                f"INSERT INTO {FTS_TABLE}(fact_id, content, skills, category, bigrams) "
                "VALUES (:fact_id, :content, :skills, :category, :bigrams)"
            ),
            {
                "fact_id": fact.id,
                "content": " ".join([fact.content, exp_title, exp_description or ""]),
                "skills": skills,
                "category": category,
                "bigrams": " ".join(cjk_bigrams(fact.content + " " + skills + " " + exp_title)),
            },
        )
    session.commit()
    return len(facts)


def index_fact(session, fact: ExperienceFact) -> None:
    """Incrementally index a single fact after insert or edit."""
    exp = session.get(Experience, fact.experience_id)
    if exp is None:
        return
    session.execute(
        text(f"DELETE FROM {FTS_TABLE} WHERE fact_id = :fact_id"), {"fact_id": fact.id}
    )
    session.execute(
        text(
            f"INSERT INTO {FTS_TABLE}(fact_id, content, skills, category, bigrams) "
            "VALUES (:fact_id, :content, :skills, :category, :bigrams)"
        ),
        {
            "fact_id": fact.id,
            "content": " ".join([fact.content, exp.title, exp.description or ""]),
            "skills": fact.skills or "",
            "category": fact.category or "",
            "bigrams": " ".join(cjk_bigrams(fact.content + " " + (fact.skills or "") + " " + exp.title)),
        },
    )
