"""Application pipeline (Phase 5, section 4).

State machine:
DISCOVERED -> MATCHED -> PREPARING -> READY -> PENDING_APPROVAL ->
SUBMITTING -> SUBMITTED -> ACKNOWLEDGED -> ASSESSMENT/INTERVIEW/OFFER/REJECTED
Any failure -> FAILED; user can CLOSE.

Safety:
- prepare_* creates a draft + confirmation token (via Phase 3.3 ConfirmationStore).
- submit_* requires HUMAN approval of that exact draft; the model cannot approve.
- Idempotency key is deterministic (user+job+channel+payload hash); a submitted
  job can never be re-submitted, including after retries or multi-source dupes.
- Auto mode is OFF by default and bounded by per-user daily caps.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ..integrations.mcp.confirmations import ConfirmationStore
from ..storage.models import (
    Application,
    ApplicationDraft,
    ApplicationEvent,
    AutomationState,
    Job,
    ResumeVersion,
)
from .email_provider import (
    EmailProvider,
    PreparedMessage,
    ProviderUnavailable,
    RecipientNotVerified,
    is_valid_email,
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERED": {"MATCHED", "PREPARING", "CLOSED"},
    "MATCHED": {"PREPARING", "CLOSED"},
    "PREPARING": {"READY", "FAILED"},
    "READY": {"PENDING_APPROVAL", "FAILED", "CLOSED"},
    "PENDING_APPROVAL": {"SUBMITTING", "REJECTED", "CLOSED"},
    "SUBMITTING": {"SUBMITTED", "FAILED"},
    "SUBMITTED": {"ACKNOWLEDGED", "FAILED"},
    "ACKNOWLEDGED": {"ASSESSMENT", "INTERVIEW", "OFFER", "REJECTED", "FAILED"},
    "ASSESSMENT": {"INTERVIEW", "OFFER", "REJECTED", "CLOSED"},
    "INTERVIEW": {"OFFER", "REJECTED", "CLOSED"},
    "OFFER": {"CLOSED"},
    "REJECTED": {"CLOSED"},
    "FAILED": {"PREPARING", "CLOSED"},
    "CLOSED": set(),
}

CHANNEL_EMAIL = "EMAIL"
CHANNEL_FORM = "OFFICIAL_FORM"
CHANNEL_ATS = "ATS"
CHANNEL_EXTERNAL = "EXTERNAL_PLATFORM"
CHANNEL_MANUAL = "MANUAL"
CHANNEL_UNKNOWN = "UNKNOWN"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ApplicationPipeline:
    def __init__(
        self,
        sessions: sessionmaker,
        confirmations: ConfirmationStore,
        *,
        user_id: str = "default",
    ) -> None:
        self.sessions = sessions
        self.confirmations = confirmations
        self.user_id = user_id

    # ---------------- state helpers ----------------

    def _get_app(self, session, application_id: int) -> Application | None:
        row = session.get(Application, application_id)
        if row is None:
            return None
        job = session.get(Job, row.job_id) if row.job_id else None
        # user isolation is enforced by drafts/versions owned by the user;
        # applications themselves carry no user_id, so we verify ownership via
        # any draft row or via a user-owned resume version.
        draft = session.execute(
            select(ApplicationDraft).where(
                ApplicationDraft.application_id == row.id,
                ApplicationDraft.user_id == self.user_id,
            ).limit(1)
        ).scalar_one_or_none()
        if draft is None:
            return None
        return row

    def _transition(self, session, app: Application, target: str, *, detail: str = "") -> bool:
        if target not in ALLOWED_TRANSITIONS.get(app.status, set()):
            return False
        previous = app.status
        app.status = target
        app.updated_at = _now()
        session.add(ApplicationEvent(
            application_id=app.id,
            event_type=f"{previous}->{target}",
            payload=_canonical({"detail": detail, "at": _now().isoformat()}),
            created_at=_now(),
            updated_at=_now(),
        ))
        return True

    def ensure_application(self, job_id: int, *, channel: str = CHANNEL_UNKNOWN) -> tuple[int, bool]:
        """Idempotent per job: returns (application_id, created)."""
        with self.sessions() as session:
            if session.get(Job, job_id) is None:
                raise ValueError(f"job {job_id} not found")
            row = session.execute(
                select(Application).where(Application.job_id == job_id)
            ).scalar_one_or_none()
            if row is not None:
                # Ensure a user-scoped draft anchor exists for isolation checks.
                draft = session.execute(
                    select(ApplicationDraft).where(
                        ApplicationDraft.application_id == row.id,
                        ApplicationDraft.user_id == self.user_id,
                    ).limit(1)
                ).scalar_one_or_none()
                if draft is None:
                    session.add(ApplicationDraft(
                        application_id=row.id,
                        user_id=self.user_id,
                        channel=channel,
                        status="ANCHOR",
                        created_at=_now(),
                        updated_at=_now(),
                    ))
                    session.commit()
                return row.id, False
            row = Application(
                job_id=job_id,
                status="DISCOVERED",
                channel=channel,
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(row)
            session.flush()
            session.add(ApplicationDraft(
                application_id=row.id,
                user_id=self.user_id,
                channel=channel,
                status="ANCHOR",
                created_at=_now(),
                updated_at=_now(),
            ))
            session.commit()
            return row.id, True

    # ---------------- prepare ----------------

    def prepare(
        self,
        application_id: int,
        *,
        channel: str,
        recipient: str,
        subject: str,
        body: str,
        resume_version_id: int | None = None,
        attachment_path: str | None = None,
    ) -> dict[str, Any]:
        """Create an immutable draft + approval token. No external side effects."""
        if channel == CHANNEL_EMAIL and not is_valid_email(recipient):
            return {"ok": False, "error": "invalid_recipient"}
        if attachment_path:
            from pathlib import Path
            if not Path(attachment_path).is_file():
                return {"ok": False, "error": "attachment_not_found"}

        payload = {
            "application_id": application_id,
            "user_id": self.user_id,
            "channel": channel,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "resume_version_id": resume_version_id,
            "attachment_path": attachment_path,
        }
        payload_hash = _sha256(_canonical(payload))

        with self.sessions() as session:
            app = self._get_app(session, application_id)
            if app is None:
                return {"ok": False, "error": "application_not_found"}
            if app.status not in {"DISCOVERED", "MATCHED", "PREPARING", "READY", "FAILED"}:
                return {"ok": False, "error": "invalid_state", "status": app.status}
            if resume_version_id is not None:
                version = session.get(ResumeVersion, resume_version_id)
                if version is None or getattr(version, "user_id", None) != self.user_id:
                    return {"ok": False, "error": "unauthorized_resume_version"}
            if app.status in {"DISCOVERED", "MATCHED", "FAILED"}:
                self._transition(session, app, "PREPARING", detail="preparing application")
            self._transition(session, app, "READY", detail="draft ready for approval")
            app.channel = channel
            app.recipient = recipient if channel == CHANNEL_EMAIL else None
            session.commit()

            existing = session.execute(
                select(ApplicationDraft).where(
                    ApplicationDraft.application_id == application_id,
                    ApplicationDraft.user_id == self.user_id,
                    ApplicationDraft.status == "READY",
                ).limit(1)
            ).scalar_one_or_none()
            if existing:
                session.delete(existing)
            draft = ApplicationDraft(
                application_id=application_id,
                user_id=self.user_id,
                channel=channel,
                recipient=recipient,
                subject=subject,
                body=body,
                resume_version_id=resume_version_id,
                attachment_path=attachment_path,
                payload_hash=payload_hash,
                status="READY",
                created_at=_now(),
                updated_at=_now(),
            )
            session.add(draft)
            session.commit()
            session.refresh(draft)
            draft_id = draft.id

        token = self.confirmations.create(
            {"action": "submit_application", "preview": payload, "draft_id": draft_id},
            user_id=self.user_id,
            action="submit_application",
            job_id=application_id,
            plan_sha256=payload_hash,
            plan_snapshot=payload,
        )
        with self.sessions() as session:
            draft = session.get(ApplicationDraft, draft_id)
            draft.confirmation_token = token
            draft.status = "PENDING_APPROVAL"
            app = session.get(Application, application_id)
            self._transition(session, app, "PENDING_APPROVAL", detail="awaiting human approval")
            session.commit()
        return {
            "ok": True,
            "application_id": application_id,
            "draft_id": draft_id,
            "confirmation_token": token,
            "payload_sha256": payload_hash,
            "preview": {
                "channel": channel,
                "recipient": recipient,
                "subject": subject,
                "body_preview": body[:400],
                "resume_version_id": resume_version_id,
                "attachment": attachment_path,
            },
            "approval": "A human must run offerbomber mcp approve <token> in a local terminal.",
        }

    # ---------------- submit ----------------

    def submit(
        self,
        confirmation_token: str,
        provider: EmailProvider | None = None,
        *,
        allow_write: bool = False,
    ) -> dict[str, Any]:
        record = self.confirmations.get_record(confirmation_token, user_id=self.user_id)
        if record is None or record.get("status") != "APPROVED":
            return {"ok": False, "error": "not_approved"}
        if not allow_write:
            return {"ok": False, "error": "write_not_allowed"}
        payload = self.confirmations.consume(
            confirmation_token, user_id=self.user_id, require_approved=True
        )
        if payload is None:
            return {"ok": False, "error": "confirmation_expired"}
        snapshot = payload.get("preview", payload)
        if _sha256(_canonical(snapshot)) != record.get("plan_sha256"):
            return {"ok": False, "error": "plan_tampered"}

        application_id = int(snapshot["application_id"])
        channel = snapshot["channel"]
        with self.sessions() as session:
            app = self._get_app(session, application_id)
            if app is None or app.status != "PENDING_APPROVAL":
                return {"ok": False, "error": "invalid_state"}
            # hard idempotency: never re-submit an already submitted application
            if app.status in {"SUBMITTED", "ACKNOWLEDGED", "OFFER"}:
                return {"ok": False, "error": "already_submitted"}
            if not self._auto_allowed(session):
                return {"ok": False, "error": "automation_paused"}
            self._transition(session, app, "SUBMITTING", detail="submitting approved draft")
            session.commit()

        try:
            if channel == CHANNEL_EMAIL:
                if provider is None or not provider.is_authorized():
                    raise ProviderUnavailable("no authorized email provider")
                message = PreparedMessage(
                    to=snapshot["recipient"],
                    subject=snapshot["subject"],
                    body=snapshot["body"],
                    attachment_path=snapshot.get("attachment_path"),
                )
                sent = provider.send(message, f"app:{application_id}:{record.get('plan_sha256')}")
                external_id = sent.provider_message_id
            elif channel in {CHANNEL_FORM, CHANNEL_ATS, CHANNEL_EXTERNAL, CHANNEL_MANUAL}:
                # No automated external submission: record a human TODO.
                external_id = ""
            else:
                raise ProviderUnavailable(f"unsupported channel: {channel}")
        except (ProviderUnavailable, RecipientNotVerified) as exc:
            with self.sessions() as session:
                app = session.get(Application, application_id)
                if app and app.status == "SUBMITTING":
                    self._transition(session, app, "FAILED", detail=str(exc))
                    session.commit()
            return {"ok": False, "error": "submit_failed", "message": str(exc)}

        with self.sessions() as session:
            app = session.get(Application, application_id)
            self._transition(session, app, "SUBMITTED", detail="submitted")
            app.idempotency_key = f"app:{application_id}:{record.get('plan_sha256')}"
            draft = session.execute(
                select(ApplicationDraft).where(
                    ApplicationDraft.application_id == application_id,
                    ApplicationDraft.confirmation_token == confirmation_token,
                )
            ).scalar_one_or_none()
            if draft:
                draft.status = "SUBMITTED"
            self._increment_daily(session)
            session.commit()
        return {
            "ok": True,
            "application_id": application_id,
            "status": "SUBMITTED",
            "channel": channel,
            "external_id": external_id,
            "manual_action_required": channel != CHANNEL_EMAIL,
        }

    # ---------------- automation control ----------------

    def _auto_allowed(self, session) -> bool:
        state = session.execute(
            select(AutomationState).where(AutomationState.user_id == self.user_id)
        ).scalar_one_or_none()
        if state is None or state.paused or state.mode == "MANUAL":
            return False
        today = datetime.now().strftime("%Y-%m-%d")
        if state.counter_date != today:
            state.sent_today = 0
            state.counter_date = today
        return state.sent_today < state.max_per_day

    def _increment_daily(self, session) -> None:
        state = session.execute(
            select(AutomationState).where(AutomationState.user_id == self.user_id)
        ).scalar_one_or_none()
        if state is None:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if state.counter_date != today:
            state.sent_today = 0
            state.counter_date = today
        state.sent_today += 1

    def set_mode(self, mode: str, *, max_per_day: int = 0, paused: bool | None = None) -> dict[str, Any]:
        if mode not in {"MANUAL", "ASSISTED", "AUTO"}:
            return {"ok": False, "error": "invalid_mode"}
        if mode == "AUTO" and max_per_day <= 0:
            return {"ok": False, "error": "auto_requires_daily_cap"}
        with self.sessions() as session:
            state = session.execute(
                select(AutomationState).where(AutomationState.user_id == self.user_id)
            ).scalar_one_or_none()
            if state is None:
                # MANUAL stays paused (Auto OFF by default). ASSISTED/AUTO are
                # explicit user opt-ins via this call, so they start unpaused;
                # every individual submit still needs a human-approved token.
                state = AutomationState(
                    user_id=self.user_id, mode=mode, max_per_day=max_per_day,
                    paused=(mode == "MANUAL") if paused is None else paused,
                    created_at=_now(), updated_at=_now(),
                )
                session.add(state)
            else:
                state.mode = mode
                state.max_per_day = max_per_day
                if paused is not None:
                    state.paused = paused
                else:
                    state.paused = (mode == "MANUAL")
                state.updated_at = _now()
            session.commit()
        return {"ok": True, "mode": mode, "max_per_day": max_per_day, "paused": state.paused}

    def pause(self, paused: bool = True) -> dict[str, Any]:
        with self.sessions() as session:
            state = session.execute(
                select(AutomationState).where(AutomationState.user_id == self.user_id)
            ).scalar_one_or_none()
            if state is None:
                state = AutomationState(
                    user_id=self.user_id, paused=paused, created_at=_now(), updated_at=_now()
                )
                session.add(state)
            else:
                state.paused = paused
                state.updated_at = _now()
            session.commit()
        return {"ok": True, "paused": state.paused}

    def status(self, application_id: int | None = None) -> dict[str, Any]:
        with self.sessions() as session:
            if application_id is not None:
                app = self._get_app(session, application_id)
                if app is None:
                    return {"ok": False, "error": "application_not_found"}
                events = session.execute(
                    select(ApplicationEvent).where(
                        ApplicationEvent.application_id == application_id
                    ).order_by(ApplicationEvent.id)
                ).scalars().all()
                return {
                    "ok": True,
                    "application": {
                        "id": app.id, "job_id": app.job_id, "status": app.status,
                        "channel": app.channel, "recipient": app.recipient,
                    },
                    "events": [{"type": e.event_type, "payload": e.payload} for e in events],
                }
            state = session.execute(
                select(AutomationState).where(AutomationState.user_id == self.user_id)
            ).scalar_one_or_none()
            rows = session.execute(
                select(Application.id, Application.status, Application.job_id)
                .join(ApplicationDraft, ApplicationDraft.application_id == Application.id)
                .where(ApplicationDraft.user_id == self.user_id)
            ).all()
            return {
                "ok": True,
                "automation": {
                    "mode": state.mode if state else "MANUAL",
                    "paused": state.paused if state else True,
                    "max_per_day": state.max_per_day if state else 0,
                    "sent_today": state.sent_today if state else 0,
                },
                "applications": [
                    {"id": r[0], "status": r[1], "job_id": r[2]} for r in rows
                ],
            }
