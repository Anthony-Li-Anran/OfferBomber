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
from sqlalchemy import select
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
from ...storage.models import Education, Experience, ExperienceFact, Job, Profile, ResumeVersion

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
