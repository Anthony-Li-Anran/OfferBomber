"""Inbox sync, thread association and recruitment-event extraction (Phase 5).

Association is evidence-based: thread id, original outbound message id, or a
strong (company + title) match. Company name ALONE is insufficient (section 5.2).
Unresolvable mail lands in the human queue - it never silently updates a status.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ..applications.email_provider import EmailProvider, ProviderUnavailable
from ..storage.models import (
    Application,
    ApplicationDraft,
    ApplicationEvent,
    EmailMessage,
    EmailThread,
    RecruitmentEvent,
)
from .classifier import CATEGORIES, Classification, EmailClassifier

_STATUS_BY_CATEGORY = {
    "INTERVIEW_INVITATION": "INTERVIEW",
    "ASSESSMENT_INVITATION": "ASSESSMENT",
    "OFFER": "OFFER",
    "REJECTION": "REJECTED",
}

# Timezone-aware datetime patterns (zh/en). Never guess a timezone if absent.
_DT_PATTERNS = [
    re.compile(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\s+(\d{1,2}:\d{2})\s*\(?(GMT[+-]\d{1,2}|UTC[+-]\d{1,2}|GMT|UTC|CET|EST|PST|[A-Z]{2,5}/[A-Za-z_]+)\)?", re.I),
    re.compile(r"(\d{1,2}\s*(?:January|February|March|April|May|June|July|August|September|October|November|December)\s*20\d{2})[,\s]+(\d{1,2}:\d{2}\s*[AP]M)\s*\(?(GMT[+-]\d{1,2}|UTC[+-]\d{1,2}|GMT|UTC|CET|EST|PST|[A-Z]{2,5}/[A-Za-z_]+)\)?", re.I),
    re.compile(r"(20\d{2}年\d{1,2}月\d{1,2}日)\s*(\d{1,2}:\d{2})\s*(北京时间|GMT[+-]\d{1,2}|GMT|UTC|[A-Za-z_]+/[A-Za-z_]+)?"),
]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_event_time(text: str, default_tz: str = "UTC") -> tuple[datetime | None, str]:
    """Parse a datetime; return (naive_utc, tz_name). No tz -> default_tz only.
    Invalid tz -> (None, '') so the event is flagged for human review.
    """
    for pattern in _DT_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        date_part, time_part = match.group(1), match.group(2)
        tz_name = (match.group(3) or default_tz).strip()
        tz_map = {
            "GMT": "UTC", "UTC": "UTC", "CET": "Europe/Paris", "EST": "America/New_York",
            "PST": "America/Los_Angeles", "北京时间": "Asia/Shanghai",
        }
        # GMT+8 / UTC-5 style fixed offsets -> a fixed-offset tzinfo.
        offset_match = re.match(r"(?:GMT|UTC)([+-])(\d{1,2})$", tz_name, re.I)
        if offset_match:
            sign = 1 if offset_match.group(1) == "+" else -1
            from datetime import timedelta

            tz = timezone(sign * timedelta(hours=int(offset_match.group(2))))
        else:
            tz_name = tz_map.get(tz_name, tz_name)
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                return None, ""
        cleaned_date = date_part.replace("年", "-").replace("月", "-").replace("日", "")
        for sep in ("/", "-"):
            cleaned_date = cleaned_date.replace(sep, "-")
        cleaned_time = time_part.strip().upper().replace("：", ":")
        try:
            fmt = "%Y-%m-%d %H:%M"
            naive = datetime.strptime(f"{cleaned_date} {cleaned_time}", fmt)
            if "AM" in cleaned_time or "PM" in cleaned_time:
                naive = datetime.strptime(f"{cleaned_date} {cleaned_time}", "%Y-%m-%d %I:%M %p")
            aware = naive.replace(tzinfo=tz)
            return aware.astimezone(timezone.utc).replace(tzinfo=None), tz_name
        except ValueError:
            continue
    return None, ""


class InboxSyncService:
    def __init__(self, sessions: sessionmaker, *, user_id: str = "default") -> None:
        self.sessions = sessions
        self.user_id = user_id
        self.classifier = EmailClassifier()

    def _associate(self, session, from_addr: str, subject: str, thread_id: str, body: str) -> Application | None:
        # 1) exact thread association
        if thread_id:
            thread = session.execute(
                select(EmailThread).where(EmailThread.provider_thread_id == thread_id)
            ).scalar_one_or_none()
            if thread and thread.application_id:
                return session.get(Application, thread.application_id)
        # 2) original outbound message id quoted in the body (In-Reply-To style)
        quoted = re.search(r"<([^>]+@[^>]+)>", body or "")
        if quoted:
            msg = session.execute(
                select(EmailMessage).where(
                    EmailMessage.provider_message_id == quoted.group(1),
                    EmailMessage.direction == "outbound",
                )
            ).scalar_one_or_none()
            if msg and msg.application_id:
                return session.get(Application, msg.application_id)
        # 3) company AND title evidence (never company alone)
        title_tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9+#.]{3,}", subject) if len(t) > 3}
        if title_tokens:
            from ..storage.models import Job

            for app in session.execute(select(Application)).scalars().all():
                job = session.get(Job, app.job_id)
                if not job:
                    continue
                company_hit = job.company.lower() in (from_addr + " " + subject).lower()
                title_hit = any(t in job.title.lower() for t in title_tokens) or any(
                    w in subject.lower() for w in job.title.lower().split()[:3] if len(w) > 3
                )
                if company_hit and title_hit:
                    return app
        return None

    def sync(
        self,
        provider: EmailProvider,
        *,
        since_iso: str | None = None,
        limit: int = 50,
        model_classifications: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Incremental sync. Duplicate provider_message_id is skipped (idempotent)."""
        if not provider.is_authorized():
            return {"ok": False, "error": "provider_unavailable", "human_queue": []}
        try:
            listings = provider.list_inbox(since_iso=since_iso, limit=limit)
        except ProviderUnavailable as exc:
            return {"ok": False, "error": "sync_failed", "message": str(exc)}

        counts = {"received": 0, "new": 0, "duplicates": 0, "associated": 0, "human_queue": 0}
        human_queue: list[dict] = []
        model_classifications = model_classifications or {}
        from ..storage.models import Job  # local import to avoid cycle at module load

        for listing in listings:
            provider_id = str(listing.get("id", ""))
            if not provider_id:
                continue
            counts["received"] += 1
            with self.sessions() as session:
                existing = session.execute(
                    select(EmailMessage).where(EmailMessage.provider_message_id == provider_id)
                ).scalar_one_or_none()
                if existing:
                    counts["duplicates"] += 1
                    continue
                # Fetch full message via provider when available.
                detail = listing
                if hasattr(provider, "get_message"):
                    try:
                        detail = provider.get_message(provider_id)
                    except ProviderUnavailable:
                        detail = listing
                subject = str(detail.get("subject", ""))
                body = str(detail.get("body", ""))
                from_addr = str(detail.get("from", ""))
                thread_id = str(detail.get("thread_id", ""))
                sent_raw = detail.get("sent_at")
                sent_at = None
                if isinstance(sent_raw, str):
                    try:
                        sent_at = datetime.fromisoformat(sent_raw.replace("Z", "+00:00")).replace(tzinfo=None)
                    except ValueError:
                        sent_at = None

                proposed = model_classifications.get(provider_id)
                classification: Classification = (
                    self.classifier.classify_with_model(subject, body, proposed)
                    if proposed else self.classifier.classify(subject, body)
                )
                app = self._associate(session, from_addr, subject, thread_id, body)
                if app is None:
                    counts["human_queue"] += 1
                    human_queue.append({
                        "provider_message_id": provider_id,
                        "subject": subject[:200],
                        "classification": classification.as_dict(),
                        "reason": "could not associate with an application",
                    })
                    application_id = None
                else:
                    counts["associated"] += 1
                    application_id = app.id
                    if classification.category in _STATUS_BY_CATEGORY and app.status != _STATUS_BY_CATEGORY[classification.category]:
                        previous = app.status
                        app.status = _STATUS_BY_CATEGORY[classification.category]
                        app.updated_at = _now()
                        session.add(ApplicationEvent(
                            application_id=app.id,
                            event_type=f"email_classification:{classification.category}",
                            payload=f'{{"from":"{previous}","confidence":{classification.confidence}}}',
                            created_at=_now(), updated_at=_now(),
                        ))

                msg = EmailMessage(
                    provider_message_id=provider_id,
                    thread_id=thread_id or None,
                    direction="inbound",
                    from_addr=from_addr[:300] or None,
                    to_addr=str(detail.get("to", "")) or None,
                    subject=subject[:500] or None,
                    body_preview=body[:1000] or None,  # redacted snippet only
                    classification=classification.category,
                    application_id=application_id,
                    sent_at=sent_at,
                    created_at=_now(), updated_at=_now(),
                )
                session.add(msg)
                if thread_id:
                    thread = session.execute(
                        select(EmailThread).where(EmailThread.provider_thread_id == thread_id)
                    ).scalar_one_or_none()
                    if thread is None:
                        session.add(EmailThread(
                            provider_thread_id=thread_id,
                            application_id=application_id,
                            last_message_at=sent_at or _now(),
                            created_at=_now(), updated_at=_now(),
                        ))
                    else:
                        thread.last_message_at = sent_at or _now()

                # Extract interview/assessment events with confirmed timezone only.
                if app is not None and classification.category in {
                    "INTERVIEW_INVITATION", "ASSESSMENT_INVITATION", "SCHEDULING_REQUEST"
                }:
                    starts_at, tz = parse_event_time(subject + "\n" + body)
                    session.add(RecruitmentEvent(
                        application_id=app.id,
                        company=(session.get(Job, app.job_id).company if session.get(Job, app.job_id) else None),
                        title=(session.get(Job, app.job_id).title if session.get(Job, app.job_id) else None),
                        event_type="interview" if classification.category == "INTERVIEW_INVITATION" else "assessment",
                        starts_at=starts_at,
                        timezone=tz or None,
                        meeting_url=(re.search(r"https?://\S+", body).group(0) if re.search(r"https?://\S+", body) else None),
                        confirmed=bool(starts_at and tz),
                        created_at=_now(), updated_at=_now(),
                    ))
                session.commit()
                counts["new"] += 1

        return {"ok": True, **counts, "human_queue": human_queue}
