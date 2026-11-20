"""SQLite-backed, atomically-consumed confirmation ledger (Phase 3.3).

A host (WorkBuddy / DeepSeek Harness) may spawn a fresh MCP process per tool
call, so confirmations must be durable. This store keeps only a SHA-256 hash of
a high-entropy token in SQLite and drives a strict state machine:

    PENDING -> APPROVED -> CONSUMED
           \\-> REJECTED / EXPIRED

Approval is a separate human action (local CLI), never a host tool argument.
Consumption uses a conditional UPDATE in a single transaction; two processes
racing on the same token cannot both win (rowcount must equal 1).

The public create/peek/consume signatures remain compatible with earlier
callers; new callers should pass user_id and use approve/reject/list_pending.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from ...storage.base import make_engine
from ...storage.models import McpConfirmation

DEFAULT_TTL_SECONDS = 15 * 60

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_CONSUMED = "CONSUMED"
STATUS_REJECTED = "REJECTED"
STATUS_EXPIRED = "EXPIRED"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; normalize to aware UTC for comparison."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ConfirmationStore:
    """Durable token ledger backed by SQLite.

    ``path`` is the SQLite database file. When several store instances point
    at the same file (as separate MCP processes do), they share state.
    """

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        session_factory: sessionmaker | None = None,
    ) -> None:
        self.path = Path(path)
        self.ttl = ttl_seconds
        if session_factory is not None:
            self._sessions = session_factory
            self._owns_engine = False
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            engine = make_engine(self.path)
            from sqlalchemy.orm import sessionmaker as _sm

            self._sessions = _sm(bind=engine, expire_on_commit=False)
            self._owns_engine = True
        # Idempotent: create only the confirmation table if absent.
        from sqlalchemy.orm import Session as _Session

        with self._sessions() as session:
            if isinstance(session, _Session):
                McpConfirmation.__table__.create(bind=session.bind, checkfirst=True)

    # ---------------- creation ----------------

    def create(
        self,
        preview: dict[str, Any],
        *,
        user_id: str = "default",
        action: str | None = None,
        job_id: int | None = None,
        source_resume_id: str | None = None,
        plan_sha256: str | None = None,
        plan_snapshot: dict[str, Any] | None = None,
        fact_versions: dict[str, int] | None = None,
        status: str = STATUS_PENDING,
    ) -> str:
        token = secrets.token_urlsafe(32)
        now = _utcnow()
        expires = now + timedelta(seconds=self.ttl)
        snapshot = plan_snapshot if plan_snapshot is not None else preview
        row = McpConfirmation(
            token_hash=_hash_token(token),
            user_id=user_id,
            action=action or str(preview.get("action", "unknown")),
            job_id=job_id if job_id is not None else preview.get("job_id"),
            source_resume_id=source_resume_id or preview.get("source_resume_id"),
            plan_sha256=plan_sha256 or preview.get("plan_sha256"),
            plan_snapshot=_canonical_json(snapshot),
            fact_versions=_canonical_json(fact_versions) if fact_versions else None,
            status=status,
            created_at=now,
            expires_at=expires,
            approved_at=now if status == STATUS_APPROVED else None,
        )
        with self._sessions() as session:
            session.add(row)
            session.commit()
        return token

    # ---------------- lookup ----------------

    def _row(self, token: str):
        with self._sessions() as session:
            row = session.execute(
                select(McpConfirmation).where(
                    McpConfirmation.token_hash == _hash_token(token)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.status == STATUS_PENDING and (_as_utc(row.expires_at) or _utcnow()) < _utcnow():
                session.execute(
                    update(McpConfirmation)
                    .where(McpConfirmation.token_hash == _hash_token(token))
                    .where(McpConfirmation.status == STATUS_PENDING)
                    .values(status=STATUS_EXPIRED)
                )
                session.commit()
                return None
            # detach a plain snapshot dict before the session closes
            return {
                "id": row.id,
                "user_id": row.user_id,
                "action": row.action,
                "job_id": row.job_id,
                "source_resume_id": row.source_resume_id,
                "plan_sha256": row.plan_sha256,
                "plan_snapshot": row.plan_snapshot,
                "fact_versions": row.fact_versions,
                "status": row.status,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "approved_at": row.approved_at,
                "consumed_at": row.consumed_at,
            }

    def get_record(self, token: str, *, user_id: str | None = None) -> dict[str, Any] | None:
        row = self._row(token)
        if row is None:
            return None
        if user_id is not None and row["user_id"] != user_id:
            return None
        return row

    def peek(self, token: str, *, user_id: str | None = None) -> dict[str, Any] | None:
        """Return the pending snapshot without consuming it (compat API)."""
        row = self.get_record(token, user_id=user_id)
        if row is None or row["status"] not in (STATUS_PENDING, STATUS_APPROVED):
            return None
        try:
            value = json.loads(row["plan_snapshot"] or "{}")
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def list_pending(self, *, user_id: str | None = None) -> list[dict[str, Any]]:
        """List approvals awaiting a human decision (for the local CLI)."""
        with self._sessions() as session:
            stmt = select(McpConfirmation).where(McpConfirmation.status == STATUS_PENDING)
            if user_id is not None:
                stmt = stmt.where(McpConfirmation.user_id == user_id)
            rows = session.execute(stmt.order_by(McpConfirmation.id)).scalars().all()
            now = _utcnow()
            return [
                {
                    "token_prefix": r.token_hash[:12],
                    "user_id": r.user_id,
                    "action": r.action,
                    "job_id": r.job_id,
                    "plan_sha256": r.plan_sha256,
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                    "expired": (_as_utc(r.expires_at) or now) < now,
                }
                for r in rows
            ]

    # ---------------- human approval (local CLI only) ----------------

    def approve(self, token: str, *, user_id: str) -> bool:
        """Human-approved transition PENDING -> APPROVED. User-scoped."""
        now = _utcnow()
        with self._sessions() as session:
            result = session.execute(
                update(McpConfirmation)
                .where(McpConfirmation.token_hash == _hash_token(token))
                .where(McpConfirmation.user_id == user_id)
                .where(McpConfirmation.status == STATUS_PENDING)
                .where(McpConfirmation.expires_at > now)
                .values(status=STATUS_APPROVED, approved_at=now)
            )
            session.commit()
            return result.rowcount == 1

    def reject(self, token: str, *, user_id: str) -> bool:
        """Human rejection PENDING|APPROVED -> REJECTED. Irreversible."""
        now = _utcnow()
        with self._sessions() as session:
            result = session.execute(
                update(McpConfirmation)
                .where(McpConfirmation.token_hash == _hash_token(token))
                .where(McpConfirmation.user_id == user_id)
                .where(McpConfirmation.status.in_([STATUS_PENDING, STATUS_APPROVED]))
                .values(status=STATUS_REJECTED)
            )
            session.commit()
            return result.rowcount == 1

    # ---------------- atomic consumption ----------------

    def consume(
        self,
        token: str,
        *,
        user_id: str | None = None,
        require_approved: bool = True,
    ) -> dict[str, Any] | None:
        """Atomically claim a token. Conditional UPDATE guarantees single use.

        Default requires an APPROVED record; ``require_approved=False`` is kept
        only for legacy callers/tests that model approval out-of-band.
        """
        now = _utcnow()
        allowed = [STATUS_APPROVED] if require_approved else [STATUS_APPROVED, STATUS_PENDING]
        with self._sessions() as session:
            stmt = (
                update(McpConfirmation)
                .where(McpConfirmation.token_hash == _hash_token(token))
                .where(McpConfirmation.status.in_(allowed))
                .where(McpConfirmation.expires_at > now)
                .values(status=STATUS_CONSUMED, consumed_at=now)
            )
            if user_id is not None:
                stmt = stmt.where(McpConfirmation.user_id == user_id)
            result = session.execute(stmt)
            if result.rowcount != 1:
                session.rollback()
                return None
            row = session.execute(
                select(McpConfirmation).where(
                    McpConfirmation.token_hash == _hash_token(token)
                )
            ).scalar_one()
            session.commit()
            try:
                return json.loads(row.plan_snapshot or "{}")
            except (ValueError, TypeError):
                return None
