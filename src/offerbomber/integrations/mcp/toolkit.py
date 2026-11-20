"""Host-agnostic MCP tool implementations (Phase 3.3 hardened).

Every method is plain Python so it can be unit-tested without an MCP client.
A host (WorkBuddy / DeepSeek Harness / any MCP client) supplies reasoning; this
toolkit supplies deterministic capabilities over the local database and the
Reactive Resume adapter. No model HTTP endpoint is called from here.

Security invariants (Phase 3.3):
- Fact access is centralized in authorization.load_authorized_facts; one bad
  ID fails the WHOLE request. No silent skipping.
- The approved plan snapshot is what gets rendered. Host text can never
  re-enter after approval (no final_facts argument), and untrusted skills are
  stripped against a trusted set derived from confirmed facts.
- Write tools require a durable token that a HUMAN approved out-of-band via
  the local CLI (offerbomber mcp approve). allow_write is a secondary gate,
  never the human signal itself.
- Resume versions are user-scoped; one host user cannot read another's output.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ...app import AppContext, resolve_db_path
from ...config import CompanySource
from ...core.discovery_service import DiscoveryService
from ...core.job_analysis import DeterministicJobAnalyzer, JobRequirements
from ...core.resume_plan import CONFIRMED
from ...core.retrieval import FactRetrievalService, RetrievalQuery
from .authorization import (
    load_authorized_facts,
    load_authorized_version,
)
from .confirmations import ConfirmationStore
from ...integrations.reactive_resume import ReactiveResumeProvider
from ...storage.models import (
    Education, EmailAccount, Experience, ExperienceFact, Job, Profile, ResumeVersion,
)

log = structlog.get_logger("mcp.toolkit")

MAX_RESULT_CHARS = 8000
_QUANT = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|x\b|倍|万|亿|人|个|ms\b|s\b|\$|元|K\b|M\b)", re.I)
_ACCEPTED = re.compile(r"(已录用|accepted|published|录用通知)", re.I)
_SUBMITTED = re.compile(r"(投稿|submitted|under review|在审)", re.I)


def _truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message}


def _canonical(value: Any) -> str:
    """Deterministic JSON for hashing; the same plan always hashes identically."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_coroutine(coro):
    """Run a coroutine even when called from inside a running event loop.

    FastMCP dispatches sync tool functions from its own asyncio loop, so a
    plain asyncio.run() would raise "cannot be called from a running event
    loop". Offload to a dedicated thread with its own loop in that case.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class McpToolkit:
    """确定性能力集合,由 MCP Server 暴露,也可直接测试。"""

    def __init__(self, ctx: AppContext, *, user_id: str = "default") -> None:
        self.ctx = ctx
        self.user_id = user_id
        # Durable SQLite ledger in the SAME database as application data, so a
        # fresh MCP process sees pending approvals. Own lifecycle: the ledger
        # table is created idempotently on init.
        db_path = Path(resolve_db_path(ctx.settings))
        self._confirmations = ConfirmationStore(db_path, session_factory=ctx.sessions)
        self._ensure_phase46_schema()

    def _ensure_phase46_schema(self) -> None:
        """Idempotently create Phase 4-6 tables for databases initialized before
        these tables existed. CREATE TABLE IF NOT EXISTS never alters existing
        data; Alembic remains the canonical migration path for schema changes.
        """
        from ...storage.models import (
            ApplicationDraft, AutomationState, DiscoveryRun, EmailAccount,
            EmailTemplate, SearchProfile,
        )

        with self.ctx.session() as session:
            bind = session.get_bind()
            for model in (
                SearchProfile, EmailAccount, EmailTemplate, ApplicationDraft,
                AutomationState, DiscoveryRun,
            ):
                try:
                    model.__table__.create(bind=bind, checkfirst=True)
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("schema_ensure_failed", table=model.__tablename__, error=str(exc))

    # ---------------- L0: discovery ----------------

    def search_jobs(self, keyword: str = "", limit: int = 10, platform: str = "") -> dict[str, Any]:
        """L0 read-only. Search already-collected jobs in the local database."""
        limit = max(1, min(limit, 50))
        with self.ctx.session() as session:
            stmt = select(Job)
            if keyword:
                pattern = f"%{keyword}%"
                stmt = stmt.where((Job.title.like(pattern)) | (Job.company.like(pattern)))
            if platform:
                stmt = stmt.where(Job.source_platform == platform)
            rows = session.execute(stmt.limit(limit)).scalars().all()
            return _ok({"jobs": [self._job_dict(row) for row in rows], "count": len(rows)})

    def discover_jobs(
        self,
        platform: str = "",
        max_companies: int = 0,
    ) -> dict[str, Any]:
        """L1 controlled network discovery using ONLY configured board tokens.

        No arbitrary URL is accepted; the host model cannot introduce an SSRF
        target. Rate limits/timeouts come from the existing HttpFetcher. No
        application or email is sent. A failing source is isolated.
        """
        targets: list[CompanySource] = list(self.ctx.settings.discovery.companies)
        if platform:
            targets = [c for c in targets if c.platform == platform]
            if not targets:
                return _fail(
                    "no_configured_source",
                    f"platform '{platform}' has no configured company source",
                )
        if max_companies and max_companies > 0:
            targets = targets[: min(max_companies, 50)]

        async def _run() -> dict[str, Any]:
            service = DiscoveryService(self.ctx.settings, self.ctx.sessions)
            try:
                stats = await service.run(targets)
            finally:
                await service.aclose()
            return stats.as_dict()

        try:
            stats = _run_coroutine(_run())
        except Exception as exc:
            log.warning("discover_failed", error=type(exc).__name__)
            return _fail("discovery_failed", f"{type(exc).__name__}: {exc}")

        return _ok({
            "discovered": stats["raw_jobs"],
            "inserted": stats["new_jobs"],
            "duplicates": stats["duplicate_jobs"],
            "errors": stats["record_errors"] + stats["sources_failed"],
            "sources_partial": stats["sources_partial"] > 0 or stats["sources_failed"] > 0,
            "fully_succeeded": stats["fully_succeeded"],
            "sources_attempted": stats["sources_attempted"],
            "error_summary": stats["errors"][:20],
        })

    def get_job(self, job_id: int) -> dict[str, Any]:
        """L0 read-only. Full JD for a collected job."""
        with self.ctx.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return _fail("not_found", f"job {job_id} not found")
            return _ok({"job": self._job_dict(job, full=True)})

    # ---------------- L0: experience retrieval ----------------

    def retrieve_experience(
        self, query: str, top_k: int = 5, verified_only: bool = True
    ) -> dict[str, Any]:
        """L0 read-only. FTS5 retrieval scoped to the current user."""
        top_k = max(1, min(top_k, 20))
        retrieval = FactRetrievalService(self.ctx.sessions, self.user_id)
        req = RetrievalQuery(
            text=query, direction=query, max_results=top_k, verified_only=verified_only
        )
        matches = retrieval.search(req)[:top_k]
        return _ok({
            "matches": [_truncate(json.dumps(m.as_dict(), ensure_ascii=False)) for m in matches],
            "count": len(matches),
            "truncated": False,
        })

    def get_confirmed_facts(self, experience_id: int | None = None) -> dict[str, Any]:
        """L0 read-only. Confirmed, undeleted facts owned by the current user."""
        with self.ctx.session() as session:
            stmt = (
                select(ExperienceFact)
                .join(Experience, Experience.id == ExperienceFact.experience_id)
                .where(
                    Experience.user_id == self.user_id,
                    ExperienceFact.is_deleted.is_(False),
                    ExperienceFact.verification_status == CONFIRMED,
                )
            )
            if experience_id is not None:
                stmt = stmt.where(ExperienceFact.experience_id == experience_id)
            rows = session.execute(stmt).scalars().all()
            return _ok({"facts": [self._fact_dict(row) for row in rows], "count": len(rows)})

    # ---------------- L0: analysis / planning (host supplies reasoning) ----------------

    def analyze_jd(self, job_id: int, *, requirements_json: str = "") -> dict[str, Any]:
        """L0 read-only. Validate/normalize a host-produced JD analysis."""
        with self.ctx.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return _fail("not_found", f"job {job_id} not found")
            if requirements_json:
                try:
                    parsed = JobRequirements.model_validate_json(requirements_json)
                except Exception as exc:
                    return _fail(
                        "invalid_requirements",
                        f"requirements_json does not match JobRequirements schema: {exc}",
                    )
                parsed.job_id = job.id
                parsed.job_title = job.title or ""
                parsed.analysis_provider = "host-model-via-mcp"
                return _ok({"requirements": parsed.model_dump(mode="json")})
            result = DeterministicJobAnalyzer().analyze(job)
            return _ok({
                "requirements": result.model_dump(mode="json"),
                "note": "deterministic extraction; host model may refine and call again with requirements_json",
            })

    def build_resume_plan(
        self,
        job_id: int,
        fact_ids: list[int],
        *,
        target_position: str = "",
        language: str = "zh",
        pages: int = 1,
    ) -> dict[str, Any]:
        """L0 read-only. Draft plan. Every fact ID is fully authorized or the
        whole request fails."""
        with self.ctx.session() as session:
            auth = load_authorized_facts(session, fact_ids, self.user_id)
        if not auth.ok:
            return _fail(auth.error.code, auth.error.message)
        pages = max(1, min(pages, 2))
        with self.ctx.session() as session:
            job = session.get(Job, job_id)
        profile, educations = self._profile_data()
        title = target_position or (job.title if job else "") or ""
        return _ok({
            "draft": {
                "target_position": title,
                "language": language,
                "target_page_count": pages,
                "job_id": job_id,
                "fact_ids": [f.id for f in auth.facts],
                "items": [
                    {
                        "fact_id": f.id,
                        "text": f.content,
                        "skills": self._fact_skills(f),
                    }
                    for f in auth.facts
                ],
                "profile_name": profile.display_name if profile else "",
                "education_count": len(educations),
            },
            "note": "draft; submit the final plan_json to prepare_generation then obtain human approval",
        })

    def validate_resume_facts(
        self, *, draft_json: str = "", fact_ids: list[int] | None = None
    ) -> dict[str, Any]:
        """L0 read-only. Quantity/publication claims and full fact authorization."""
        ids = list(fact_ids or [])
        draft = None
        if draft_json:
            try:
                draft = json.loads(draft_json)
            except json.JSONDecodeError as exc:
                return _fail("invalid_draft_json", str(exc))
            if not ids and isinstance(draft, dict):
                ids = [int(x) for x in draft.get("fact_ids", [])]
        with self.ctx.session() as session:
            auth = load_authorized_facts(session, ids, self.user_id)
        if not auth.ok:
            return _fail(auth.error.code, auth.error.message)
        originals = {f.id: f.content for f in auth.facts}
        violations: list[dict[str, Any]] = []
        if isinstance(draft, dict):
            for item in draft.get("items", []):
                if not isinstance(item, dict):
                    continue
                fid = int(item.get("fact_id", -1))
                text = str(item.get("text", ""))
                original = originals.get(fid)
                if original is None:
                    violations.append({"fact_id": fid, "reason": "unknown_fact_id"})
                    continue
                for token in set(_QUANT.findall(text)):
                    if token not in _QUANT.findall(original):
                        violations.append({
                            "fact_id": fid, "reason": "unverified_quantity", "token": token
                        })
                for claim in ("已发表", "published", "已录用", "accepted"):
                    if claim.lower() in text.lower() and claim.lower() not in original.lower():
                        violations.append({
                            "fact_id": fid,
                            "reason": "unverified_publication_status",
                            "token": claim,
                        })
        return _ok({
            "valid": not violations,
            "violations": violations,
            "checked_fact_ids": [f.id for f in auth.facts],
        })

    # ---------------- L2: generation (human-in-the-loop) ----------------

    def prepare_generation(
        self,
        job_id: int,
        source_resume_id: str,
        fact_ids: list[int],
        *,
        final_plan_json: str = "",
        language: str = "zh",
        pages: int = 1,
        target_position: str = "",
    ) -> dict[str, Any]:
        """L2 preparation (no writes). Build the immutable normalized plan that
        the human will approve. The approved snapshot is the ONLY thing that
        generate_resume may later render.

        ``final_plan_json`` may contain host-reordered items, but text is
        consistency-checked against source facts and skills are filtered to a
        trusted set derived from confirmed facts — no skill injection.
        """
        if not source_resume_id:
            return _fail("missing_source_resume", "source_resume_id is required")
        with self.ctx.session() as session:
            auth = load_authorized_facts(session, fact_ids, self.user_id)
        if not auth.ok:
            return _fail(auth.error.code, auth.error.message)

        trusted_skills: set[str] = set()
        originals: dict[int, str] = {}
        for fact in auth.facts:
            originals[fact.id] = fact.content
            trusted_skills.update(self._fact_skills(fact))

        host_items: dict[int, dict[str, Any]] = {}
        host_position = ""
        if final_plan_json:
            try:
                payload = json.loads(final_plan_json)
            except json.JSONDecodeError as exc:
                return _fail("invalid_final_plan_json", str(exc))
            if isinstance(payload, dict):
                host_position = str(payload.get("target_position", ""))
                for item in payload.get("items", []):
                    if isinstance(item, dict) and "fact_id" in item:
                        try:
                            fid = int(item["fact_id"])
                        except (TypeError, ValueError):
                            continue
                        host_items[fid] = item

        # Build rendered items in the ORDER given by fact_ids (host may reorder
        # by passing a different fact_ids order). Rewrites are consistency-checked
        # and anything unprovable reverts to the original with a review marker.
        rendered_items: list[dict[str, Any]] = []
        reverted: list[int] = []
        for fact in auth.facts:
            fid = fact.id
            original = originals[fid]
            host = host_items.get(fid)
            rendered = str(host.get("text", original)) if host else original
            status = "VERIFIED"
            if self._consistency_issue(rendered, original):
                rendered = original
                status = "REVERTED"
                reverted.append(fid)
            cand_skills = host.get("skills", []) if host else []
            safe_skills = self._filter_skills(cand_skills, trusted_skills)
            rendered_items.append({
                "fact_id": fid,
                "fact_version": fact.version or 1,
                "source_id": fact.source_id,
                "experience_id": fact.experience_id,
                "original_text": original,
                "rendered_text": rendered,
                "skills": safe_skills,
                "validation_status": status,
            })

        with self.ctx.session() as session:
            job = session.get(Job, job_id)
        title = host_position or target_position or (job.title if job else "") or ""
        plan_snapshot = {
            "action": "generate_resume",
            "job_id": job_id,
            "source_resume_id": source_resume_id,
            "target_position": title,
            "language": language,
            "pages": max(1, min(pages, 2)),
            "items": rendered_items,
            "trusted_skills": sorted(trusted_skills),
            "fact_ids": [f.id for f in auth.facts],
        }
        canonical = _canonical(plan_snapshot)
        plan_hash = _sha256(canonical)
        fact_versions = {str(f.id): (f.version or 1) for f in auth.facts}

        token = self._confirmations.create(
            plan_snapshot,
            user_id=self.user_id,
            action="generate_resume",
            job_id=job_id,
            source_resume_id=source_resume_id,
            plan_sha256=plan_hash,
            plan_snapshot=plan_snapshot,
            fact_versions=fact_versions,
        )
        preview = {
            "action": "generate_resume",
            "job_id": job_id,
            "source_resume_id": source_resume_id,
            "target_position": title,
            "plan_sha256": plan_hash,
            "fact_ids": plan_snapshot["fact_ids"],
            "reverted_fact_ids": reverted,
            "trusted_skills": sorted(trusted_skills),
            "items": [
                {
                    "fact_id": i["fact_id"],
                    "original_text": i["original_text"],
                    "rendered_text": i["rendered_text"],
                    "skills": i["skills"],
                    "validation_status": i["validation_status"],
                }
                for i in rendered_items
            ],
            "effects": [
                "clones the source resume (source is never modified)",
                "writes the APPROVED content into the clone",
                "exports a PDF to the configured output directory",
                "records an immutable, user-scoped ResumeVersion in SQLite",
            ],
            "approval": (
                "A human must approve this exact plan via the local CLI: "
                "offerbomber mcp approve <token>. The model cannot approve it."
            ),
        }
        return _ok({
            "confirmation_token": token,
            "plan_sha256": plan_hash,
            "preview": preview,
        })

    def generate_resume(
        self,
        confirmation_token: str,
        *,
        allow_write: bool | None = None,
    ) -> dict[str, Any]:
        """L2 execute. Only the HUMAN-approved plan snapshot is rendered.

        No final_facts argument: content approved by the human is exactly what
        reaches Reactive Resume. A PENDING/REJECTED token is refused.
        """
        record = self._confirmations.get_record(confirmation_token, user_id=self.user_id)
        if record is None or record.get("status") not in ("PENDING", "APPROVED"):
            return _fail(
                "confirmation_required",
                "call prepare_generation first and obtain human approval via offerbomber mcp approve",
            )
        if record.get("status") != "APPROVED":
            return _fail(
                "not_approved",
                "a human must approve the plan via offerbomber mcp approve <token> before writing",
            )
        if allow_write is None:
            allow_write = os.getenv("OFFERBOMBER_MCP_ALLOW_WRITE", "").lower() in {"1", "true", "yes"}
        if not allow_write:
            return _fail("write_not_allowed", "allow_write=true required after human approval")

        plan = self._confirmations.consume(
            confirmation_token, user_id=self.user_id, require_approved=True
        )
        if plan is None:
            return _fail("confirmation_expired", "token already used/expired or approval lost")
        # Re-verify the snapshot hash binds content to what was shown to the human.
        if _sha256(_canonical(plan)) != record.get("plan_sha256"):
            return _fail("plan_tampered", "approved plan hash mismatch; refusing to render")

        # Fact versions must be unchanged since approval, else stale approval.
        with self.ctx.session() as session:
            auth = load_authorized_facts(session, plan.get("fact_ids", []), self.user_id)
        if not auth.ok:
            return _fail(auth.error.code, "facts changed since approval: " + auth.error.message)
        current_versions = {str(f.id): (f.version or 1) for f in auth.facts}
        if current_versions != json.loads(record.get("fact_versions") or "{}"):
            return _fail("stale_approval", "a fact version changed after approval; re-approve")

        try:
            version = _run_coroutine(self._run_generation(plan, plan_sha256=record["plan_sha256"]))
        except Exception as exc:
            log.warning("mcp_generate_failed", error=type(exc).__name__)
            return _fail("generation_failed", f"{type(exc).__name__}: {exc}")
        return _ok({
            "version_id": version.id,
            "job_id": version.job_id,
            "file_path": version.file_path,
            "sha256": version.file_sha256,
            "immutable": version.immutable,
            "note": "PDF passed automated checks only; review layout manually",
        })

    async def _run_generation(
        self, plan_snapshot: dict[str, Any], *, plan_sha256: str
    ) -> ResumeVersion:
        from ...core.resume_plan import FactAudit, ResumePlan, ResumeSection, ResumeSectionItem

        job_id = int(plan_snapshot["job_id"])
        source_resume_id = str(plan_snapshot["source_resume_id"])
        language = str(plan_snapshot.get("language", "zh"))
        pages = int(plan_snapshot.get("pages", 1))
        target_position = str(plan_snapshot.get("target_position", ""))
        items = plan_snapshot.get("items", [])

        section_items: list[ResumeSectionItem] = []
        audits: list[FactAudit] = []
        for item in items:
            fid = int(item["fact_id"])
            rendered = str(item.get("rendered_text", ""))
            status = str(item.get("validation_status", "VERIFIED"))
            section_items.append(ResumeSectionItem(
                experience_id=int(item["experience_id"]),
                fact_id=fid,
                text=rendered,
                category=None,
                skills=[str(s) for s in item.get("skills", [])],
                source_type=None,
                file_sha256=None,
            ))
            audits.append(FactAudit(
                fact_id=fid,
                fact_version=int(item.get("fact_version", 1)),
                source_id=item.get("source_id"),
                original_text=str(item.get("original_text", "")),
                rendered_text=rendered,
                experience_id=int(item["experience_id"]),
                evidence=str(item.get("source_id") or ""),
                validation_status=status,
            ))
        if not section_items:
            raise ValueError("no approved facts available for generation")

        plan = ResumePlan(
            job_id=str(job_id),
            target_position=target_position,
            selected_fact_ids=[int(i["fact_id"]) for i in items],
            language=language,
            target_page_count=pages,
            sections=[ResumeSection(title="Highlights", items=section_items)],
            fact_audit=audits,
            generation_provider="human-approved-via-mcp",
        )

        profile, educations = self._profile_data()
        with self.ctx.session() as session:
            rows = session.execute(
                select(Experience).where(Experience.user_id == self.user_id)
            ).scalars().all()
            experiences = {e.id: e for e in rows}
        rr = ReactiveResumeProvider()
        output_dir = Path.cwd() / "output" / "mcp"
        version = await rr.generate(
            job_id=job_id,
            source_resume_id=source_resume_id,
            plan=plan,
            profile=profile,
            educations=educations,
            experiences=experiences,
            output_dir=output_dir,
        )
        # Bind ownership + approved-plan hash to the immutable version.
        version.user_id = self.user_id
        version.plan_sha256 = plan_sha256
        with self.ctx.session() as session:
            session.add(version)
            session.commit()
            session.refresh(version)
        return version

    # ---------------- PDF validation / compatibility export ----------------

    def validate_generated_pdf(self, version_id: int) -> dict[str, Any]:
        """L0 read-only. PyMuPDF quality checks on an existing version's PDF."""
        with self.ctx.session() as session:
            version, err = load_authorized_version(session, version_id, self.user_id)
        if err is not None:
            return _fail(err.code, err.message)
        assert version is not None
        path = Path(version.file_path)
        report = ReactiveResumeProvider.validate_pdf(path)
        return _ok({
            "version_id": version.id,
            "file_path": version.file_path,
            "validation": report.as_dict(),
        })

    def prepare_export(self, version_id: int) -> dict[str, Any]:
        """L2 preparation for PDF re-export (no file write yet)."""
        with self.ctx.session() as session:
            version, err = load_authorized_version(session, version_id, self.user_id)
        if err is not None:
            return _fail(err.code, err.message)
        preview = {"action": "export_pdf", "version_id": version_id}
        token = self._confirmations.create(
            preview,
            user_id=self.user_id,
            action="export_pdf",
            plan_snapshot=preview,
        )
        return _ok({"confirmation_token": token, "preview": preview})

    def export_pdf(
        self, confirmation_token: str, *, version_id: int, allow_write: bool | None = None
    ) -> dict[str, Any]:
        """L2 compatibility alias.

        Historical behavior only VALIDATED the existing PDF; it never
        re-exported via Reactive Resume. Use validate_generated_pdf for that
        read-only check. This alias now enforces the same human approval gate
        and remains for backward compatibility (see docs/MCP_INTEGRATION.md).
        """
        record = self._confirmations.get_record(confirmation_token, user_id=self.user_id)
        if record is None or record.get("status") != "APPROVED":
            return _fail(
                "confirmation_required",
                "call prepare_export and obtain human approval via offerbomber mcp approve",
            )
        if allow_write is None:
            allow_write = os.getenv("OFFERBOMBER_MCP_ALLOW_WRITE", "").lower() in {"1", "true", "yes"}
        if not allow_write:
            return _fail("write_not_allowed", "human approval + allow_write=true required")
        claimed = self._confirmations.consume(
            confirmation_token, user_id=self.user_id, require_approved=True
        )
        if claimed is None:
            return _fail("confirmation_expired", "token already used or expired")
        return self.validate_generated_pdf(version_id)

    def get_generation_status(self, job_id: int | None = None) -> dict[str, Any]:
        """L0 read-only. Versions are scoped to the current MCP user."""
        with self.ctx.session() as session:
            stmt = select(ResumeVersion).where(ResumeVersion.user_id == self.user_id)
            if job_id is not None:
                stmt = stmt.where(ResumeVersion.job_id == job_id)
            rows = session.execute(stmt.order_by(ResumeVersion.id.desc()).limit(20)).scalars().all()
            return _ok({
                "versions": [
                    {
                        "id": r.id, "job_id": r.job_id, "file_path": r.file_path,
                        "sha256": r.file_sha256, "immutable": r.immutable,
                        "plan_sha256": getattr(r, "plan_sha256", None),
                    }
                    for r in rows
                ],
                "count": len(rows),
            })

    # ---------------- helpers ----------------

    @staticmethod
    def _filter_skills(candidates: Any, trusted: set[str]) -> list[str]:
        """Keep only trusted skills; dedupe preserving host order."""
        if not isinstance(candidates, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for value in candidates:
            skill = str(value).strip()
            if skill in trusted and skill not in seen:
                result.append(skill)
                seen.add(skill)
        return result

    @staticmethod
    def _consistency_issue(rendered: str, original: str) -> str | None:
        if set(_QUANT.findall(rendered.lower())) - set(_QUANT.findall(original.lower())):
            return "unverified quantification"
        if _ACCEPTED.search(rendered) and not _ACCEPTED.search(original) and _SUBMITTED.search(original):
            return "submission upgraded to acceptance"
        return None

    def _profile_data(self) -> tuple[Profile | None, list[Education]]:
        with self.ctx.session() as session:
            profile = session.execute(
                select(Profile).where(Profile.user_id == self.user_id)
            ).scalar_one_or_none()
            educations = session.execute(
                select(Education).where(Education.profile_id == profile.id)
            ).scalars().all() if profile else []
            return profile, list(educations)

    @staticmethod
    def _fact_skills(fact: ExperienceFact) -> list[str]:
        raw = fact.skills
        if not raw:
            return []
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return []
        return [str(v) for v in value] if isinstance(value, list) else []

    @staticmethod
    def _job_dict(job: Job, *, full: bool = False) -> dict[str, Any]:
        data = {
            "id": job.id, "title": job.title, "company": job.company,
            "location": job.location, "platform": job.source_platform,
            "url": job.official_url,
        }
        if full:
            data.update({
                "description": job.description, "requirements": job.requirements,
                "education_requirement": job.education_requirement,
            })
        return data

    @staticmethod
    def _fact_dict(fact: ExperienceFact) -> dict[str, Any]:
        return {
            "id": fact.id, "experience_id": fact.experience_id,
            "content": fact.content, "skills": McpToolkit._fact_skills(fact),
            "verification_status": fact.verification_status,
        }

    # ====================================================================
    # Phase 4-6: general-purpose job-hunting agent capabilities (L0/L1/L2)
    # ====================================================================

    # ---------- search profiles (L0 read / L1 write) ----------

    def _profile_service(self):
        from ...search_profile import SearchProfileService

        return SearchProfileService(self.ctx.sessions)

    def search_profiles(self) -> dict[str, Any]:
        """[L0 只读] 列出当前用户的所有求职配置(多个求职活动)。"""
        rows = self._profile_service().list(self.user_id)
        return _ok({
            "profiles": [
                {"id": pid, "name": p.name, "criteria": json.loads(p.to_canonical_json())}
                for pid, p in rows
            ],
            "count": len(rows),
        })

    def create_search_profile(self, criteria_json: str) -> dict[str, Any]:
        """[L1 写入] 创建通用求职配置。所有偏好字段可空;未设置=不限制,不推断。"""
        from ...search_profile import JobSearchProfile

        try:
            data = json.loads(criteria_json)
            profile = JobSearchProfile.model_validate(data)
        except (json.JSONDecodeError, ValueError) as exc:
            return _fail("invalid_criteria", str(exc))
        profile.user_id = self.user_id
        if not profile.name:
            return _fail("name_required", "profile name is required")
        created = self._profile_service().create(profile)
        with self.ctx.session() as session:
            from ...storage.models import SearchProfile

            row = session.execute(
                select(SearchProfile).where(
                    SearchProfile.user_id == self.user_id, SearchProfile.name == created.name
                )
            ).scalars().first()
            profile_id = row.id if row else None
        return _ok({"profile_id": profile_id, "name": created.name})

    def update_search_profile(self, profile_id: int, criteria_json: str) -> dict[str, Any]:
        """[L1 写入] 更新当前用户自己的求职配置;不属于本人的 ID 被拒绝。"""
        from ...search_profile import JobSearchProfile

        try:
            profile = JobSearchProfile.model_validate(json.loads(criteria_json))
        except (json.JSONDecodeError, ValueError) as exc:
            return _fail("invalid_criteria", str(exc))
        profile.user_id = self.user_id
        updated = self._profile_service().update(self.user_id, profile_id, profile)
        if updated is None:
            return _fail("profile_not_found", "profile not found or not owned by user")
        return _ok({"profile_id": profile_id, "name": updated.name})

    # ---------- company discovery (L1) ----------

    def discover_companies(self, profile_id: int, max_companies: int = 20) -> dict[str, Any]:
        """[L1 受控网络发现] 从求职配置生成查询,用已配置搜索源自动发现并验证企业招聘入口。不接受任意 URL。"""
        from ...core.company_discovery import CompanyDiscoveryService
        from ...discovery.http import HttpFetcher
        from ...discovery.search_source import SearchProviderConfig

        profile = self._profile_service().get(self.user_id, profile_id)
        if profile is None:
            return _fail("profile_not_found", "profile not found or not owned by user")
        provider = self.ctx.settings.discovery.search_provider
        if provider is None or not provider.base_url:
            return _fail("search_provider_not_configured", "configure discovery.search_provider.base_url first")
        config = SearchProviderConfig(
            base_url=provider.base_url,
            api_key_env=provider.api_key_env,
            result_limit=provider.result_limit,
        )
        fetcher = HttpFetcher()
        try:
            service = CompanyDiscoveryService(fetcher, config)
            result = _run_coroutine(
                service.discover(profile, max_companies=max(1, min(max_companies, 100)))
            )
            return _ok(result.as_dict())
        except Exception as exc:
            return _fail("discovery_failed", f"{type(exc).__name__}: {exc}")
        finally:
            _run_coroutine(fetcher.aclose())

    def import_jobs(self, file_path: str) -> dict[str, Any]:
        """[L1 本地导入] 从用户提供的 CSV/JSON 文件导入岗位。不接受网络 URL。"""
        from ...discovery.custom_source import LocalImportSource
        from ...storage import repositories as repo
        from ...core.dedup import JobDeduplicator

        if not file_path:
            return _fail("file_required", "file_path is required")
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            return _fail("file_not_found", "import file not found")
        try:
            jobs = LocalImportSource().load(path)
        except Exception as exc:
            return _fail("import_failed", f"{type(exc).__name__}: {exc}")
        inserted = duplicates = 0
        with self.ctx.session() as session:
            dedup = JobDeduplicator(session)
            for raw in jobs:
                result = dedup.find(raw)
                if result.job is not None:
                    duplicates += 1
                    continue
                job = repo.insert_job(session, raw)
                repo.add_source_record(session, job, raw)
                dedup.add(job)
                inserted += 1
            session.commit()
        return _ok({"discovered": len(jobs), "inserted": inserted, "duplicates": duplicates})

    # ---------- generic matching (L0) ----------

    def match_jobs(self, profile_id: int, job_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """[L0 只读] 用当前用户的已确认事实,按求职配置对岗位通用匹配(无硬编码职业分类)。"""
        from ...core.job_schema import NormalizedJob
        from ...core.matching import FactView, JobMatcher
        from ...storage.models import Job

        profile = self._profile_service().get(self.user_id, profile_id)
        if profile is None:
            return _fail("profile_not_found", "profile not found or not owned by user")
        # Only CONFIRMED, non-deleted facts of the current user (A1 invariant).
        with self.ctx.session() as session:
            from ...integrations.mcp.authorization import load_authorized_facts

            fact_rows = session.execute(
                select(ExperienceFact).where(
                    ExperienceFact.verification_status == CONFIRMED,
                    ExperienceFact.is_deleted.is_(False),
                )
            ).scalars().all()
            owned: list[ExperienceFact] = []
            for fact in fact_rows:
                exp = session.get(Experience, fact.experience_id)
                prof = session.get(Profile, exp.profile_id) if exp else None
                if prof and prof.user_id == self.user_id:
                    owned.append(fact)
            facts = [FactView(f.id, f.content, self._fact_skills(f), f.version or 1) for f in owned]
            stmt = select(Job)
            if job_id is not None:
                stmt = stmt.where(Job.id == job_id)
            jobs = session.execute(stmt.limit(max(1, min(limit, 100)))).scalars().all()
            normalized = [
                NormalizedJob(
                    job_id=f"db:{j.id}",
                    company=j.company,
                    title=j.title,
                    description=j.description or None,
                    requirements=j.education_requirement or None,
                    employment_type=j.employment_type,
                    location=j.location,
                    remote_policy=j.remote_policy,
                    published_at=j.published_at,
                    source=j.source_platform,
                    source_url=j.official_url,
                    official_apply_url=j.apply_url,
                    recruitment_email=j.recruitment_email,
                    apply_method="EMAIL" if j.recruitment_email else (
                        "OFFICIAL_FORM" if j.apply_url else "UNKNOWN"
                    ),
                    verification_status="LIKELY",
                )
                for j in jobs
            ]
        matcher = JobMatcher()
        results = []
        for j, norm in zip(jobs, normalized):
            out = matcher.match(norm, profile, facts)
            results.append({
                "job_id": j.id, "title": j.title, "company": j.company,
                "match_score": out.match_score, "eligibility": out.eligibility,
                "recommendation": out.application_recommendation,
                "relevant_fact_ids": out.relevant_fact_ids,
                "explanation": out.explanation,
            })
        results.sort(key=lambda r: r["match_score"], reverse=True)
        return _ok({"matches": results, "count": len(results)})

    # ---------- applications (L0 read / L1 prepare / L2 submit) ----------

    def _pipeline(self):
        from ...applications.pipeline import ApplicationPipeline

        return ApplicationPipeline(self.ctx.sessions, self._confirmations, user_id=self.user_id)

    def list_application_candidates(self, limit: int = 20) -> dict[str, Any]:
        """[L0 只读] 列出可申请的岗位候选(已匹配、未被排除、未重复投递)。"""
        with self.ctx.session() as session:
            from ...storage.models import Application, Job

            rows = session.execute(select(Job).limit(max(1, min(limit, 100)))).scalars().all()
            candidates = []
            for j in rows:
                app = session.execute(
                    select(Application).where(Application.job_id == j.id)
                ).scalar_one_or_none()
                candidates.append({
                    "job_id": j.id, "title": j.title, "company": j.company,
                    "recruitment_email": j.recruitment_email,
                    "application_status": app.status if app else None,
                    "eligible": j.recruitment_email is not None or bool(j.apply_url),
                })
            return _ok({"candidates": candidates})

    def prepare_application(
        self,
        job_id: int,
        channel: str = "EMAIL",
        recipient: str = "",
        subject: str = "",
        body: str = "",
        resume_version_id: int | None = None,
    ) -> dict[str, Any]:
        """[L2 准备,无外部写入] 准备申请草稿+审批令牌。仅用岗位证据中的招聘邮箱;host 不能自批。"""
        pipeline = self._pipeline()
        app_id, _ = pipeline.ensure_application(job_id, channel=channel)
        # Recipient MUST come from job evidence. Host-supplied recipient is
        # ignored for EMAIL channel so the model cannot invent an address.
        if channel == "EMAIL":
            with self.ctx.session() as session:
                job = session.get(Job, job_id)
                recipient = (job.recruitment_email or "") if job else ""
        return pipeline.prepare(
            app_id, channel=channel, recipient=recipient, subject=subject, body=body,
            resume_version_id=resume_version_id,
        )

    def preview_application(self, confirmation_token: str) -> dict[str, Any]:
        """[L0 只读] 展示待批准申请草稿的完整内容(人类据此决定)。"""
        record = self._confirmations.get_record(confirmation_token, user_id=self.user_id)
        if record is None:
            return _fail("confirmation_not_found", "token not found")
        snapshot = record.get("plan_snapshot")
        payload = json.loads(snapshot) if isinstance(snapshot, str) else (snapshot or {})
        return _ok({
            "status": record.get("status"),
            "preview": payload.get("preview", payload),
            "plan_sha256": record.get("plan_sha256"),
        })

    def approve_application(self, confirmation_token: str, allow_write: bool = False) -> dict[str, Any]:
        """[L2 写入] 消费已由人类 CLI 批准的令牌,提交申请。模型调用本身不构成批准。"""
        return self._pipeline().submit(confirmation_token, allow_write=allow_write)

    def application_status(self, application_id: int | None = None) -> dict[str, Any]:
        """[L0 只读] 查询申请状态与自动化开关。"""
        return self._pipeline().status(application_id)

    # ---------- email / inbox (L0 read / L1 sync) ----------

    def email_connect_status(self) -> dict[str, Any]:
        """[L0 只读] 查询邮箱连接状态。不返回任何凭证。"""
        with self.ctx.session() as session:
            rows = session.execute(
                select(EmailAccount).where(EmailAccount.user_id == self.user_id)
            ).scalars().all()
            return _ok({
                "accounts": [
                    {"provider": a.provider, "email": a.email, "status": a.status}
                    for a in rows
                ],
                "note": "Credentials are never stored in the database.",
            })

    def inbox_sync(self, since_iso: str = "", limit: int = 50) -> dict[str, Any]:
        """[L1 受控外部操作] 增量同步招聘邮件,分类+关联;无法关联进人工队列,不擅自改状态。"""
        from ...applications.email_provider import GmailOAuthProvider, ProviderUnavailable
        from ...feedback.sync import InboxSyncService

        provider = GmailOAuthProvider()
        try:
            authorized = provider.is_authorized()
        except Exception:
            authorized = False
        if not authorized:
            return _fail("mail_not_authorized", "configure OFFERBOMBER_GMAIL_CREDENTIALS first")
        service = InboxSyncService(self.ctx.sessions, user_id=self.user_id)
        return service.sync(provider, since_iso=since_iso or None, limit=limit)

    def classify_recruitment_email(self, subject: str, body: str) -> dict[str, Any]:
        """[L0 只读] 对招聘邮件做确定性分类(中英文),仅供人类/宿主参考,不单独改状态。"""
        from ...feedback.classifier import EmailClassifier

        return _ok(EmailClassifier().classify(subject, body).as_dict())

    def interviews_list(self) -> dict[str, Any]:
        """[L0 只读] 列出面试/笔试事件(含时区与是否已确认)。"""
        from ...storage.models import RecruitmentEvent

        with self.ctx.session() as session:
            events = session.execute(
                select(RecruitmentEvent).order_by(RecruitmentEvent.id)
            ).scalars().all()
            return _ok({
                "events": [
                    {
                        "id": e.id, "type": e.event_type, "company": e.company,
                        "title": e.title,
                        "starts_at": e.starts_at.isoformat() if e.starts_at else None,
                        "timezone": e.timezone, "confirmed": bool(e.confirmed),
                    }
                    for e in events
                ]
            })

    # ---------- scheduler / report / system (L0 / L1) ----------

    def tasks_list(self, limit: int = 50) -> dict[str, Any]:
        """[L0 只读] 列出持久化任务及状态。"""
        from ...scheduling.task_queue import TaskQueue

        return TaskQueue(self.ctx.sessions, user_id=self.user_id).list_tasks(limit=limit)

    def run_tasks(self, max_tasks: int = 5) -> dict[str, Any]:
        """[L1 受控] 执行到期的持久化任务(受全局暂停开关约束,次数有上限)。"""
        from ...scheduling.task_queue import TaskQueue

        return TaskQueue(self.ctx.sessions, user_id=self.user_id).run_due({}, max_tasks=max_tasks)

    def report_daily(self, days: int = 1) -> dict[str, Any]:
        """[L0 只读] 生成日报(Markdown+JSON),写入本地输出目录。"""
        from ...reporting.daily import DailyReport

        out = Path(self.ctx.settings.storage.db_path).parent.parent / "output" / "reports"
        return DailyReport(self.ctx.sessions, user_id=self.user_id).save(out, days=days)

    def statistics(self) -> dict[str, Any]:
        """[L0 只读] 汇总统计(岗位/申请/回复/事件),数字全部来自本地库。"""
        return _ok(DailyReport(self.ctx.sessions, user_id=self.user_id).build(days=30))

    def pause_automation(self, paused: bool = True) -> dict[str, Any]:
        """[L1 本地控制] 一键暂停/恢复自动执行(默认暂停)。"""
        return self._pipeline().pause(paused=paused)

    def set_automation_mode(self, mode: str, max_per_day: int = 0) -> dict[str, Any]:
        """[L1 本地控制] 设置 MANUAL/ASSISTED/AUTO 模式;AUTO 必须设每日上限。"""
        return self._pipeline().set_mode(mode, max_per_day=max_per_day)

    def system_health(self) -> dict[str, Any]:
        """[L0 只读] 系统健康状态:数据库、各能力实现状态、邮箱授权、自动化开关。"""
        from ...config import CAPABILITY_STATUS

        with self.ctx.session() as session:
            job_count = int(session.execute(select(func.count(Job.id))).scalar_one())
        state = self.application_status().get("automation", {})
        return _ok({
            "database": "ok",
            "fts5": self.ctx.fts_available,
            "jobs_in_db": job_count,
            "automation": state,
            "capabilities": CAPABILITY_STATUS,
        })
