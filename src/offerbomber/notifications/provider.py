"""NotificationProvider abstraction (Phase 6, section 7).

LocalFileProvider is always available (no channel -> still keep a local record).
HostConnectorProvider is a thin adapter that a host (WorkBuddy) can supply; if no
connector is configured it degrades to local-only and records the reason.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import sessionmaker

from ..storage.models import Notification


@dataclass
class NotificationMessage:
    subject: str
    body: str
    urgency: str = "normal"  # normal | urgent
    recipients: str = ""


@runtime_checkable
class NotificationProvider(Protocol):
    name: str

    def send(self, message: NotificationMessage) -> dict: ...


class LocalFileProvider:
    """Writes notifications to a local JSONL file. Never fails silently."""

    name = "local_file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, message: NotificationMessage) -> dict:
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "subject": message.subject,
            "body": message.body,
            "urgency": message.urgency,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return {"ok": True, "provider": self.name}


class HostConnectorProvider:
    """Delegates to a host-supplied callable (e.g. a WorkBuddy connector).

    The callable is injected by the caller; no internal platform API is called.
    """

    def __init__(self, send_fn) -> None:
        self.name = "host_connector"
        self._send_fn = send_fn

    def send(self, message: NotificationMessage) -> dict:
        try:
            result = self._send_fn(message.subject, message.body, message.urgency)
            return {"ok": True, "provider": self.name, "result": result}
        except Exception as exc:
            return {"ok": False, "provider": self.name, "error": f"{type(exc).__name__}: {exc}"}


class NotificationService:
    def __init__(
        self,
        sessions: sessionmaker,
        providers: list[NotificationProvider],
    ) -> None:
        self.sessions = sessions
        self.providers = providers  # first local, then optional host connector

    def notify(self, message: NotificationMessage) -> dict:
        results = []
        for provider in self.providers:
            try:
                results.append(provider.send(message))
            except Exception as exc:
                results.append({"ok": False, "provider": provider.name, "error": str(exc)})
        delivered = any(r.get("ok") for r in results)
        with self.sessions() as session:
            session.add(Notification(
                channel=",".join(p.name for p in self.providers),
                recipients=message.recipients or None,
                subject=message.subject[:300],
                body_preview=message.body[:1000],
                urgency=message.urgency,
                status="SENT" if delivered else "FAILED",
                error=None if delivered else json.dumps(results, ensure_ascii=False)[:1000],
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            ))
            session.commit()
        return {"ok": delivered, "results": results}
