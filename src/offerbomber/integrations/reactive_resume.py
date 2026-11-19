"""Reactive Resume adapter (task book Part G).

Official API contract (verified 2026-11-19):
- Base: https://rxresu.me/api/openapi ; auth header: x-api-key
- GET /resumes ; GET /resumes/{id} ; POST /resumes ; POST /resumes/{id}/copies
- PUT /resumes/{id} (full document) ; JSON Patch on /resumes/{id}
- GET /resumes/{id}/pdf ; GET /api/health (no key)
- 429 on rate limits; errors carry `code`; do NOT auto-retry mutations after
  timeout — read the resource first (per spec description).

The adapter ALWAYS clones a source resume before editing; the user's primary
resume is never overwritten. Without a configured API key, all operations fail
explicitly with `unavailable` and no fake PDF path is produced.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog
from pydantic import BaseModel

from ..core.resume_plan import ResumePlan
from ..storage.models import Education, Experience, Profile, ResumeVersion
from .schema_mapper import map_resume_data

log = structlog.get_logger("reactive_resume")


class ResumeGenerationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ValidationReport:
    valid: bool
    checks: dict[str, bool]
    details: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "checks": self.checks, "details": self.details}


class ReactiveResumeProvider:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.getenv("OFFERBOMBER_RXRESUME_API_KEY", "")
        self.base_url = (base_url or os.getenv("OFFERBOMBER_RXRESUME_URL", "https://rxresu.me/api/openapi")).rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ResumeGenerationError("unavailable", "Reactive Resume API key not configured")
        return {"x-api-key": self.api_key, "Accept": "application/json"}

    async def health_check(self) -> dict[str, Any]:
        if not self.configured:
            return {"available": False, "code": "unavailable", "detail": "OFFERBOMBER_RXRESUME_API_KEY not set"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get("https://rxresu.me/api/health")
                response.raise_for_status()
            return {"available": True, "code": "ok", "detail": "rxresu.me health endpoint reachable"}
        except httpx.HTTPError as exc:
            return {"available": False, "code": "transport", "detail": str(exc)}

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        try:
            body = response.json()
            code = body.get("code", f"http_{response.status_code}")
            message = body.get("message", response.text[:200])
        except ValueError:
            code, message = f"http_{response.status_code}", response.text[:200]
        raise ResumeGenerationError(str(code), str(message))

    async def list_resumes(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        response = await client.get(f"{self.base_url}/resumes", headers=self._headers())
        self._raise_for_response(response)
        data = response.json()
        return data if isinstance(data, list) else []

    async def get_resume(self, client: httpx.AsyncClient, resume_id: str) -> dict[str, Any]:
        response = await client.get(f"{self.base_url}/resumes/{resume_id}", headers=self._headers())
        self._raise_for_response(response)
        return response.json()

    @staticmethod
    def _unique_slug(name: str) -> str:
        """Build a stable, ASCII-only, unique slug for a clone.

        The API slugifies the name when slug is omitted, but CJK/emoji names can
        collapse to a constant value and collide on repeated copies
        (RESUME_SLUG_ALREADY_EXISTS). Supplying an explicit unique slug avoids it.
        """
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "resume"
        return f"{base}-{uuid.uuid4().hex[:12]}"

    async def clone_resume(self, client: httpx.AsyncClient, source_id: str, name: str) -> str:
        response = await client.post(
            f"{self.base_url}/resumes/{source_id}/copies",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"name": name, "slug": self._unique_slug(name)},
        )
        self._raise_for_response(response)
        data = response.json()
        new_id = data.get("id") if isinstance(data, dict) else data
        if not isinstance(new_id, str) or not new_id:
            raise ResumeGenerationError("unexpected_response", "copy response has no resume id")
        return new_id

    async def update_resume(
        self, client: httpx.AsyncClient, resume_id: str, source_doc: dict[str, Any], content: dict[str, Any]
    ) -> None:
        """Replace only content fields; preserve metadata/design/template from clone."""
        document = dict(source_doc)
        # The API may wrap ResumeData under `data`; keep the wrapper and replace
        # content inside it, otherwise replace at the document root.
        target = document.get("data") if isinstance(document.get("data"), dict) else document
        for key, value in content.items():
            target[key] = value
        response = await client.put(
            f"{self.base_url}/resumes/{resume_id}",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=document,
        )
        self._raise_for_response(response)

    async def export_pdf(self, client: httpx.AsyncClient, resume_id: str) -> bytes:
        response = await client.get(
            f"{self.base_url}/resumes/{resume_id}/pdf",
            headers={**self._headers(), "Accept": "application/pdf"},
        )
        self._raise_for_response(response)
        if not response.content.startswith(b"%PDF"):
            raise ResumeGenerationError("invalid_pdf", "export endpoint did not return a PDF")
        return response.content

    @staticmethod
    def validate_pdf(path: str | Path, *, expected_name: str = "", expected_terms: list[str] | None = None) -> ValidationReport:
        """PyMuPDF-based quality checks; never claims visual layout correctness."""
        import pymupdf

        path = Path(path)
        checks: dict[str, bool] = {"exists": path.is_file(), "nonempty": path.is_file() and path.stat().st_size > 500}
        if not all(checks.values()):
            return ValidationReport(False, checks, "missing or empty PDF")
        try:
            with pymupdf.open(path) as document:
                checks["openable"] = document.page_count > 0
                checks["reasonable_pages"] = 1 <= document.page_count <= 4
                text = "\n".join(page.get_text() for page in document)
        except Exception as exc:  # noqa: BLE001 - report, never pretend valid
            return ValidationReport(False, checks, f"PyMuPDF failed to open: {exc}")
        checks["has_text"] = len(text.strip()) >= 50
        checks["name_present"] = (not expected_name) or expected_name in text
        terms = expected_terms or []
        checks["key_content_present"] = all(term in text for term in terms[:5]) if terms else True
        checks["no_blank_pages"] = all(len(page.get_text().strip()) > 0 for page in pymupdf.open(path))
        valid = all(checks.values())
        return ValidationReport(valid, checks, "automated checks only; visual layout requires human review")

    async def generate(
        self,
        *,
        job_id: int | None,
        source_resume_id: str,
        plan: ResumePlan,
        profile: Profile | None,
        educations: list[Education],
        experiences: dict[int, Experience],
        output_dir: str | Path,
    ) -> ResumeVersion:
        """Clone -> fill -> export -> validate. Raises on any failure."""
        if not self.configured:
            raise ResumeGenerationError("unavailable", "Reactive Resume API key not configured")
        content = map_resume_data(
            plan=plan, profile=profile, educations=educations, experiences=experiences
        )
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            clone_id = await self.clone_resume(
                client, source_resume_id, f"OfferBomber job {job_id} {plan.target_position}"
            )
            try:
                # Re-read the clone as the write base: it already inherited the
                # source's design/template but carries its OWN slug and metadata.
                # Writing the source document back would overwrite the clone's
                # slug with the source slug -> RESUME_SLUG_ALREADY_EXISTS.
                clone_doc = await self.get_resume(client, clone_id)
                await self.update_resume(client, clone_id, clone_doc, content)
                pdf_bytes = await self.export_pdf(client, clone_id)
            except ResumeGenerationError:
                log.warning("generation_failed_keeping_clone", clone_id=clone_id, job_id=job_id)
                raise
        pdf_path = output_dir / f"resume_{job_id}_{clone_id}.pdf"
        pdf_path.write_bytes(pdf_bytes)
        report = self.validate_pdf(
            pdf_path,
            expected_name=(profile.display_name if profile else None) or "",
            expected_terms=[item.text[:20] for sec in plan.sections for item in sec.items][:5],
        )
        if not report.valid:
            raise ResumeGenerationError("validation_failed", json.dumps(report.as_dict(), ensure_ascii=False))
        return ResumeVersion(
            job_id=job_id,
            file_path=str(pdf_path.resolve()),
            file_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            immutable=True,
            fact_snapshot=json.dumps(
                [audit.model_dump() for audit in plan.fact_audit], ensure_ascii=False
            ),
        )
