"""SQLite-backed persistent task queue (Phase 6, section 6).

Recovery: PENDING/RUNNING tasks left by a dead process are reclaimed on startup.
Idempotency: task_id and idempotency_key are UNIQUE; enqueue is a no-op on
conflict. Concurrency: claiming uses a conditional UPDATE (status PENDING ->
RUNNING) so two workers cannot run the same task. One-click pause is global via
AutomationState. When the machine is off, nothing runs - callers are told so.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from ..storage.models import AutomationState, Task

TASK_TYPES = {
    "DISCOVER_JOBS", "MATCH_JOBS", "PREPARE_APPLICATIONS", "PROCESS_SEND_QUEUE",
    "SYNC_EMAIL", "CHECK_EVENTS", "DAILY_REPORT",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TaskQueue:
    def __init__(self, sessions: sessionmaker, *, user_id: str = "default") -> None:
        self.sessions = sessions
        self.user_id = user_id

    def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any] | None = None,
        *,
        scheduled_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if task_type not in TASK_TYPES:
            return {"ok": False, "error": "unknown_task_type"}
        task_id = f"{task_type}:{secrets.token_hex(8)}"
        key = idempotency_key or f"{self.user_id}:{task_type}:{json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)}"
        with self.sessions() as session:
            existing = session.execute(
                select(Task).where(Task.idempotency_key == key)
            ).scalar_one_or_none()
            if existing:
                return {"ok": True, "task_id": existing.task_id, "created": False}
            session.add(Task(
                task_id=task_id,
                task_type=task_type,
                payload=json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                status="PENDING",
                scheduled_at=(scheduled_at or _now()),
                idempotency_key=key,
                created_at_task=_now(),
                created_at=_now(), updated_at=_now(),
            ))
            session.commit()
        return {"ok": True, "task_id": task_id, "created": True}

    def recover(self) -> dict[str, Any]:
        """Reclaim tasks orphaned by a crash. Idempotent; safe to call at startup."""
        stale = _now() - timedelta(minutes=5)
        with self.sessions() as session:
            orphaned = session.execute(
                select(Task).where(
                    Task.status == "RUNNING",
                    Task.started_at < stale,
                )
            ).scalars().all()
            for task in orphaned:
                if task.retry_count < 3:
                    task.status = "PENDING"
                    task.retry_count += 1
                    task.last_error = "reclaimed after worker loss"
                else:
                    task.status = "FAILED"
                    task.last_error = "reclaim limit exceeded"
                task.updated_at = _now()
            session.commit()
        return {"ok": True, "recovered": len(orphaned)}

    def is_paused(self) -> bool:
        with self.sessions() as session:
            state = session.execute(
                select(AutomationState).where(AutomationState.user_id == self.user_id)
            ).scalar_one_or_none()
            return True if state is None else bool(state.paused)

    def claim(self, task_type: str | None = None) -> Task | None:
        """Atomically mark one due PENDING task as RUNNING. None when paused."""
        if self.is_paused():
            return None
        with self.sessions() as session:
            stmt = select(Task).where(
                Task.status == "PENDING",
                Task.scheduled_at <= _now(),
            )
            if task_type:
                stmt = stmt.where(Task.task_type == task_type)
            task = session.execute(stmt.order_by(Task.scheduled_at).limit(1)).scalar_one_or_none()
            if task is None:
                return None
            result = session.execute(
                update(Task)
                .where(Task.id == task.id, Task.status == "PENDING")
                .values(status="RUNNING", started_at=_now(), updated_at=_now())
            )
            session.commit()
            if result.rowcount != 1:
                return None  # another worker claimed it first
            return task

    def complete(self, task_id: str, *, error: str | None = None) -> dict[str, Any]:
        with self.sessions() as session:
            task = session.execute(
                select(Task).where(Task.task_id == task_id)
            ).scalar_one_or_none()
            if task is None:
                return {"ok": False, "error": "task_not_found"}
            task.status = "FAILED" if error else "DONE"
            task.finished_at = _now()
            if error:
                task.last_error = error[:1000]
                if task.retry_count < 3:
                    task.status = "PENDING"
                    task.scheduled_at = _now() + timedelta(minutes=2 ** task.retry_count)
                    task.retry_count += 1
            task.updated_at = _now()
            session.commit()
        return {"ok": True, "task_id": task_id, "status": task.status}

    def run_due(
        self,
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
        *,
        max_tasks: int = 10,
    ) -> dict[str, Any]:
        """Run at most max_tasks due tasks. Never loops without bound (C5)."""
        executed = 0
        results: list[dict] = []
        for _ in range(max(0, min(max_tasks, 50))):
            task = self.claim()
            if task is None:
                break
            handler = handlers.get(task.task_type)
            if handler is None:
                self.complete(task.task_id, error=f"no handler for {task.task_type}")
                executed += 1
                continue
            try:
                payload = json.loads(task.payload or "{}")
                result = handler(payload)
                self.complete(task.task_id, error=result.get("error") if isinstance(result, dict) and not result.get("ok", True) else None)
            except Exception as exc:
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                self.complete(task.task_id, error=result["error"])
            results.append({"task_id": task.task_id, "type": task.task_type, "result": result})
            executed += 1
        return {"ok": True, "executed": executed, "paused": self.is_paused(), "results": results}

    def list_tasks(self, *, limit: int = 50) -> dict[str, Any]:
        with self.sessions() as session:
            rows = session.execute(
                select(Task).order_by(Task.id.desc()).limit(max(1, min(limit, 200)))
            ).scalars().all()
            return {
                "ok": True,
                "tasks": [
                    {
                        "task_id": t.task_id, "type": t.task_type, "status": t.status,
                        "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
                        "retry_count": t.retry_count, "last_error": t.last_error,
                    }
                    for t in rows
                ],
            }
