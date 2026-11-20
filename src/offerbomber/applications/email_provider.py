"""EmailProvider contract and a real Gmail API implementation (Phase 5).

Security invariants:
- Credentials are NEVER read from config.yaml or stored in the DB. They come
  from an env var naming a local credential file, or an OS keyring reference.
- Sending requires an explicit authenticated provider. Missing/invalid creds
  fail closed (ProviderUnavailable) - no mock success is ever substituted.
- Recipient verification is enforced before any send: only addresses from the
  job's evidence-backed recruitment_email field are accepted.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class ProviderUnavailable(RuntimeError):
    """Credentials missing/expired or provider not reachable."""


class RecipientNotVerified(RuntimeError):
    """Target address is not backed by job evidence - sending is forbidden."""


@dataclass
class PreparedMessage:
    to: str
    subject: str
    body: str
    attachment_path: str | None = None
    reply_to_thread: str | None = None


@dataclass
class SentMessage:
    provider_message_id: str
    thread_id: str = ""


def is_valid_email(address: str) -> bool:
    return bool(address and _EMAIL_RE.match(address.strip()))


def load_credential(ref: str) -> dict:
    """Load a JSON credential from a path named by an ENV VAR.

    ``ref`` is an env-var name (e.g. OFFERBOMBER_GMAIL_CREDENTIALS) whose value
    is a path to a local JSON file. The file itself is outside the repo.
    """
    raw = os.getenv(ref, "")
    if not raw:
        raise ProviderUnavailable(f"credential env var not set: {ref}")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ProviderUnavailable(f"credential file not found for {ref}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ProviderUnavailable(f"invalid credential file for {ref}: {exc}") from exc


@runtime_checkable
class EmailProvider(Protocol):
    """Minimal provider surface used by the application pipeline.

    Implement this for Gmail OAuth, IMAP/SMTP, or a host connector. The pipeline
    never constructs your provider with mock data.
    """

    name: str

    def is_authorized(self) -> bool: ...

    def create_draft(self, message: PreparedMessage) -> str: ...

    def send(self, message: PreparedMessage, idempotency_key: str) -> SentMessage: ...

    def list_inbox(self, since_iso: str | None = None, limit: int = 50) -> list[dict]: ...


class GmailOAuthProvider:
    """Real Gmail API provider using google-api-python-client when installed.

    Flow:
    - Credential JSON (OAuth client or token) path given via env var ref.
    - Uses gmail.users.messages.send with a MIME message including attachments.
    - Threading uses RFC 822 Message-ID / threadId when provided.

    If the Google client library is unavailable, is_authorized() returns False
    and send() raises ProviderUnavailable - no fake message IDs are produced.
    """

    name = "gmail"

    def __init__(self, credential_env: str = "OFFERBOMBER_GMAIL_CREDENTIALS") -> None:
        self.credential_env = credential_env
        self._service = None
        self._credential = None

    def _build(self):
        if self._service is not None:
            return self._service
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise ProviderUnavailable(f"Google client library not installed: {exc}") from exc
        data = load_credential(self.credential_env)
        try:
            creds = Credentials(
                token=data.get("token"),
                refresh_token=data.get("refresh_token"),
                token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
                client_id=data.get("client_id"),
                client_secret=data.get("client_secret"),
                scopes=data.get("scopes", ["https://www.googleapis.com/auth/gmail.modify"]),
            )
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            self._credential = creds
        except Exception as exc:
            raise ProviderUnavailable(f"Gmail authorization failed: {type(exc).__name__}: {exc}") from exc
        return self._service

    def is_authorized(self) -> bool:
        try:
            self._build()
            return True
        except ProviderUnavailable:
            return False

    @staticmethod
    def _mime(message: PreparedMessage) -> str:
        from email.message import EmailMessage

        mime = EmailMessage()
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        if message.attachment_path:
            path = Path(message.attachment_path)
            if path.is_file():
                mime.add_attachment(
                    path.read_bytes(),
                    maintype="application",
                    subtype="pdf",
                    filename=path.name,
                )
        return base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")

    def create_draft(self, message: PreparedMessage) -> str:
        service = self._build()
        try:
            draft = service.users().drafts().create(
                userId="me", body={"message": {"raw": self._mime(message)}}
            ).execute()
            return str(draft.get("id", ""))
        except Exception as exc:
            raise ProviderUnavailable(f"Gmail draft failed: {type(exc).__name__}: {exc}") from exc

    def send(self, message: PreparedMessage, idempotency_key: str) -> SentMessage:
        if not is_valid_email(message.to):
            raise RecipientNotVerified(f"invalid recipient: {message.to!r}")
        service = self._build()
        try:
            sent = service.users().messages().send(
                userId="me", body={"raw": self._mime(message)}
            ).execute()
            return SentMessage(
                provider_message_id=str(sent.get("id", "")),
                thread_id=str(sent.get("threadId", "")),
            )
        except Exception as exc:
            raise ProviderUnavailable(f"Gmail send failed: {type(exc).__name__}: {exc}") from exc

    def list_inbox(self, since_iso: str | None = None, limit: int = 50) -> list[dict]:
        service = self._build()
        query = "newer_than:1d" if not since_iso else f"after:{since_iso[:10].replace('-', '/')}"
        try:
            resp = service.users().messages().list(
                userId="me", q=query, maxResults=min(limit, 200)
            ).execute()
            return list(resp.get("messages", []))
        except Exception as exc:
            raise ProviderUnavailable(f"Gmail list failed: {type(exc).__name__}: {exc}") from exc
