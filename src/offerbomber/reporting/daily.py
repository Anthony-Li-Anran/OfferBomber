"""Daily report and statistics (Phase 6, section 7).

Outputs both Markdown and JSON. No personal data leaves the machine unless an
explicitly configured NotificationProvider is used. Report content is derived
only from rows in the local database - no invented numbers.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from ..storage.models import (
    Application,
    ApplicationEvent,
    EmailMessage,
    Job,
    RecruitmentEvent,
    Task,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DailyReport:
    def __init__(self, sessions: sessionmaker, *, user_id: str = "default") -> None:
        self.sessions = sessions
        self.user_id = user_id

    def build(self, *, days: int = 1) -> dict[str, Any]:
        since = _now() - timedelta(days=max(1, min(days, 30)))
        with self.sessions() as session:
            discovered = int(session.execute(
                select(func.count(Job.id)).where(Job.discovered_at >= since)
            ).scalar_one())
            submitted = session.execute(
                select(func.count(Application.id)).where(Application.status.in_(["SUBMITTED", "ACKNOWLEDGED"]))
            ).scalar_one()
            applications = session.execute(select(Application)).scalars().all()
            by_status: dict[str, int] = {}
            for app in applications:
                by_status[app.status] = by_status.get(app.status, 0) + 1
            replies = session.execute(
                select(func.count(EmailMessage.id)).where(
                    EmailMessage.direction == "inbound", EmailMessage.created_at >= since
                )
            ).scalar_one()
            events = session.execute(
                select(RecruitmentEvent).where(RecruitmentEvent.created_at >= since)
            ).scalars().all()
            upcoming = [
                {
                    "type": e.event_type,
                    "company": e.company,
                    "title": e.title,
                    "starts_at": e.starts_at.isoformat() if e.starts_at else None,
                    "timezone": e.timezone,
                    "confirmed": bool(e.confirmed),
                }
                for e in events
            ]
            failures = session.execute(
                select(func.count(Task.id)).where(Task.status == "FAILED")
            ).scalar_one()
        stats = {
            "generated_at": _now().isoformat(),
            "window_days": days,
            "jobs_discovered": discovered,
            "applications_submitted_total": int(submitted or 0),
            "replies_received": int(replies or 0),
            "events": upcoming,
            "applications_by_status": by_status,
            "failed_tasks": int(failures or 0),
        }
        return stats

    @staticmethod
    def to_markdown(stats: dict[str, Any]) -> str:
        lines = [
            f"# OfferBomber Daily Report — {stats['generated_at'][:10]}",
            "",
            f"- Window: last {stats['window_days']} day(s)",
            f"- Jobs discovered: {stats['jobs_discovered']}",
            f"- Applications submitted (total): {stats['applications_submitted_total']}",
            f"- Recruitment replies: {stats['replies_received']}",
            f"- Failed tasks: {stats['failed_tasks']}",
            "",
            "## Applications by status",
        ]
        if stats["applications_by_status"]:
            for status, count in sorted(stats["applications_by_status"].items()):
                lines.append(f"- {status}: {count}")
        else:
            lines.append("- (none)")
        lines += ["", "## Upcoming interviews / assessments"]
        if stats["events"]:
            for event in stats["events"]:
                when = event["starts_at"] or "time TBD"
                tz = event["timezone"] or "tz unknown"
                flag = "" if event["confirmed"] else " (needs confirmation)"
                lines.append(f"- {event['type']}: {event['company'] or '?'} — {event['title'] or '?'} @ {when} {tz}{flag}")
        else:
            lines.append("- (none)")
        return "\n".join(lines) + "\n"

    def save(self, output_dir: str | Path, *, days: int = 1) -> dict[str, Any]:
        stats = self.build(days=days)
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        md_path = directory / f"report-{stamp}.md"
        json_path = directory / f"report-{stamp}.json"
        md_path.write_text(self.to_markdown(stats), encoding="utf-8")
        json_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "markdown": str(md_path), "json": str(json_path), "stats": stats}
