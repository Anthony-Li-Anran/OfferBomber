"""Host-agnostic MCP tool implementations.

Every method is plain Python so it can be unit-tested without an MCP client.
A host (WorkBuddy / DeepSeek Harness / any MCP client) supplies reasoning; this
toolkit supplies deterministic capabilities over the local database and the
Reactive Resume adapter. No model HTTP endpoint is called from here.

Write-class tools (generate_resume / export_pdf) require an explicit confirmation
token: the host must first call prepare_generation, surface the preview to the
human, and pass back the returned token. This keeps human-in-the-loop control
over profile changes and file writes.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from ...app import AppContext, resolve_db_path
from ...core.job_analysis import DeterministicJobAnalyzer, JobRequirements
from ...core.resume_plan import CONFIRMED
from ...core.retrieval import FactRetrievalService, RetrievalQuery
from .confirmations import ConfirmationStore
from ...integrations.reactive_resume import ReactiveResumeProvider
from ...models.base import ModelProvider
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
        # Persist confirmations so the prepare -> confirm handshake survives a
        # new server process per tool call (WorkBuddy / DeepSeek Harness hosts).
        db_path = Path(resolve_db_path(ctx.settings))
        store_path = db_path.parent / ".mcp_confirmations.json"
        self._confirmations = ConfirmationStore(store_path)

    # ---------------- L0: discovery ----------------

    def search_jobs(self, keyword: str = "", limit: int = 10, platform: str = "") -> dict[str, Any]:
        limit = max(1, min(limit, 50))
        with self.ctx.session() as session:
            stmt = select(Job)
            if keyword:
                pattern = f"%{keyword}%"
                stmt = stmt.where(
                    (Job.title.like(pattern)) | (Job.company.like(pattern))
                )
            if platform:
                stmt = stmt.where(Job.source_platform == platform)
            rows = session.execute(stmt.limit(limit)).scalars().all()
            return _ok({"jobs": [self._job_dict(row) for row in rows], "count": len(rows)})

    def get_job(self, job_id: int) -> dict[str, Any]:
        with self.ctx.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return _fail("not_found", f"job {job_id} not found")
            return _ok({"job": self._job_dict(job, full=True)})

    # ---------------- L0: experience retrieval ----------------

    def retrieve_experience(
        self, query: str, top_k: int = 5, verified_only: bool = True
    ) -> dict[str, Any]:
        top_k = max(1, min(top_k, 20))
        retrieval = FactRetrievalService(self.ctx.sessions, self.user_id)
        req = RetrievalQuery(
            text=query, direction=query, max_results=top_k,
            verified_only=verified_only,
        )
        matches = retrieval.search(req)[:top_k]
        return _ok({
            "matches": [_truncate(json.dumps(m.as_dict(), ensure_ascii=False)) for m in matches],
            "count": len(matches),
            "truncated": False,
        })

    def get_confirmed_facts(self, experience_id: int | None = None) -> dict[str, Any]:
        with self.ctx.session() as session:
            stmt = select(ExperienceFact).join(
                Experience, Experience.id == ExperienceFact.experience_id
            ).where(
                Experience.user_id == self.user_id,
                ExperienceFact.is_deleted.is_(False),
                ExperienceFact.verification_status == CONFIRMED,
            )
            if experience_id is not None:
                stmt = stmt.where(ExperienceFact.experience_id == experience_id)
            rows = session.execute(stmt).scalars().all()
            return _ok({
                "facts": [self._fact_dict(row) for row in rows],
                "count": len(rows),
            })

    # ---------------- L0: analysis / planning (host supplies reasoning) ----------------

    def analyze_jd(self, job_id: int, *, requirements_json: str = "") -> dict[str, Any]:
        """JD 分析。默认用确定性抽取;宿主模型可把结构化结果通过
        requirements_json 传回,本工具只做校验与规范化,不 reverse-call 宿主。
        """
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
        """基于宿主已选定的 fact_ids 构建 ResumePlan(确定性,无模型调用)。"""
        if not fact_ids:
            return _fail("empty_facts", "fact_ids must not be empty")
        pages = max(1, min(pages, 2))
        with self.ctx.session() as session:
            job = session.get(Job, job_id)
            facts = [
                session.get(ExperienceFact, fid)
                for fid in fact_ids[:40]
            ]
        facts = [f for f in facts if f is not None and not f.is_deleted]
        if not facts:
            return _fail("no_valid_facts", "none of the supplied fact_ids exist")

        profile, educations = self._profile_data()
        title = target_position or (job.title if job else "") or ""
        return _ok({
            "draft": {
                "target_position": title,
                "language": language,
                "target_page_count": pages,
                "job_id": job_id,
                "fact_ids": [f.id for f in facts],
                "items": [
                    {
                        "fact_id": f.id,
                        "text": f.content,
                        "skills": self._fact_skills(f),
                    }
                    for f in facts
                ],
                "profile_name": profile.display_name if profile else "",
                "education_count": len(educations),
            },
            "note": "draft plan; host model may reorder items then call generate_resume with final_facts",
        })

    def validate_resume_facts(
        self, *, draft_json: str = "", fact_ids: list[int] | None = None
    ) -> dict[str, Any]:
        """事实一致性校验:数字/录用状态不得超出原文。"""
        ids = list(fact_ids or [])
        if draft_json:
            try:
                draft = json.loads(draft_json)
            except json.JSONDecodeError as exc:
                return _fail("invalid_draft_json", str(exc))
            ids = ids or [int(x) for x in draft.get("fact_ids", [])]
        if not ids:
            return _fail("empty_facts", "no facts to validate against")
        with self.ctx.session() as session:
            facts = [session.get(ExperienceFact, fid) for fid in ids[:40]]
        facts = [f for f in facts if f is not None]
        if not facts:
            return _fail("no_valid_facts", "none of the supplied fact_ids exist")

        import re

        quant = re.compile(r"\d+(?:\.\d+)?\s*%|\d+\s*(?:x|倍|人|万|k|K)")
        violations: list[dict[str, Any]] = []
        if draft_json:
            rendered = draft.get("items", []) if isinstance(draft, dict) else []
            originals = {f.id: f.content for f in facts}
            for item in rendered:
                if not isinstance(item, dict):
                    continue
                fid = int(item.get("fact_id", -1))
                text = str(item.get("text", ""))
                original = originals.get(fid)
                if original is None:
                    violations.append({"fact_id": fid, "reason": "unknown_fact_id"})
                    continue
                for token in set(quant.findall(text)):
                    if token not in quant.findall(original):
                        violations.append({
                            "fact_id": fid,
                            "reason": "unverified_quantity",
                            "token": token,
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
            "checked_fact_ids": [f.id for f in facts],
        })

    # ---------------- L2: generation (human-in-the-loop) ----------------

    def prepare_generation(
        self,
        job_id: int,
        source_resume_id: str,
        fact_ids: list[int],
        *,
        language: str = "zh",
        pages: int = 1,
    ) -> dict[str, Any]:
        """第一步:生成待确认动作,返回 confirmation_token,不写任何文件。"""
        if not source_resume_id:
            return _fail("missing_source_resume", "source_resume_id is required")
        validation = self.validate_resume_facts(fact_ids=fact_ids)
        if not validation.get("ok") or not validation.get("valid"):
            return _fail("fact_validation_failed", json.dumps(validation, ensure_ascii=False))
        preview = {
            "action": "generate_resume",
            "job_id": job_id,
            "source_resume_id": source_resume_id,
            "fact_ids": fact_ids,
            "language": language,
            "pages": pages,
            "effects": [
                "clones the source resume (source is never modified)",
                "writes generated content into the clone",
                "exports a PDF to the configured output directory",
                "records an immutable ResumeVersion in SQLite",
            ],
        }
        token = self._confirmations.create(preview)
        return _ok({"confirmation_token": token, "preview": preview})

    def generate_resume(
        self,
        confirmation_token: str,
        *,
        final_facts: str = "",
        allow_write: bool | None = None,
    ) -> dict[str, Any]:
        """第二步:确认后执行。需要 confirmation_token + allow_write=true。"""
        if allow_write is None:
            allow_write = os.getenv("OFFERBOMBER_MCP_ALLOW_WRITE", "").lower() in {"1", "true", "yes"}
        preview = self._confirmations.peek(confirmation_token)
        if preview is None:
            return _fail(
                "confirmation_required",
                "call prepare_generation first and pass its confirmation_token; "
                "surface the preview to the human before confirming",
            )
        if not allow_write:
            return _fail(
                "write_not_allowed",
                "set OFFERBOMBER_MCP_ALLOW_WRITE=1 or pass allow_write=true after human approval",
            )
        # Consume only after all guards pass; replay of the same token fails.
        preview = self._confirmations.consume(confirmation_token)
        if preview is None:
            return _fail("confirmation_expired", "confirmation token already used or expired")

        try:
            version = _run_coroutine(self._run_generation(preview, final_facts=final_facts))
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

    async def _run_generation(self, preview: dict[str, Any], *, final_facts: str = "") -> ResumeVersion:
        from ...core.resume_plan import FactAudit, ResumePlan, ResumeSection, ResumeSectionItem

        job_id = int(preview["job_id"])
        source_resume_id = str(preview["source_resume_id"])
        language = str(preview.get("language", "zh"))
        pages = int(preview.get("pages", 1))
        fact_ids = [int(fid) for fid in preview.get("fact_ids", [])]

        # Load authoritative facts so text/provenance never come from the host blindly.
        with self.ctx.session() as session:
            facts = {
                fid: session.get(ExperienceFact, fid)
                for fid in fact_ids
            }

        rendered_by_id: dict[int, str] = {}
        skills_by_id: dict[int, list[str]] = {}
        target_position = str(preview.get("target_position", ""))
        if final_facts:
            payload = json.loads(final_facts)
            if isinstance(payload, dict):
                target_position = str(payload.get("target_position", target_position))
                for item in payload.get("items", []):
                    if isinstance(item, dict) and "fact_id" in item:
                        fid = int(item["fact_id"])
                        rendered_by_id[fid] = str(item.get("text", ""))
                        skills_by_id[fid] = [str(s) for s in item.get("skills", [])]

        section_items: list[ResumeSectionItem] = []
        audits: list[FactAudit] = []
        for fid in fact_ids:
            fact = facts.get(fid)
            if fact is None or fact.is_deleted:
                continue
            original = fact.content
            # Never let host text introduce unverified numbers/status.
            rendered = rendered_by_id.get(fid, original)
            if self._consistency_issue(rendered, original):
                rendered = original
            section_items.append(ResumeSectionItem(
                experience_id=fact.experience_id,
                fact_id=fid,
                text=rendered,
                category=fact.category,
                skills=skills_by_id.get(fid) or self._fact_skills(fact),
                source_type=fact.source_locator,
                file_sha256=None,
            ))
            audits.append(FactAudit(
                fact_id=fid,
                fact_version=fact.version or 1,
                source_id=fact.source_id,
                original_text=original,
                rendered_text=rendered,
                experience_id=fact.experience_id,
            ))

        if not section_items:
            raise ValueError("no confirmed facts available for generation")

        plan = ResumePlan(
            job_id=str(job_id),
            target_position=target_position,
            selected_fact_ids=fact_ids,
            language=language,
            target_page_count=pages,
            sections=[ResumeSection(title="Highlights", items=section_items)],
            fact_audit=audits,
            generation_provider="host-via-mcp",
        )

        profile, educations = self._profile_data()
        with self.ctx.session() as session:
            exp_rows = session.execute(
                select(Experience).where(Experience.user_id == self.user_id)
            ).scalars().all()
            experiences = {e.id: e for e in exp_rows}
        rr = ReactiveResumeProvider()
        output_dir = Path.cwd() / "output" / "mcp"
        return await rr.generate(
            job_id=job_id,
            source_resume_id=source_resume_id,
            plan=plan,
            profile=profile,
            educations=educations,
            experiences=experiences,
            output_dir=output_dir,
        )

    def export_pdf(
        self, confirmation_token: str, *, version_id: int, allow_write: bool | None = None
    ) -> dict[str, Any]:
        """导出/校验已生成版本的 PDF。L2,需确认令牌。"""
        if allow_write is None:
            allow_write = os.getenv("OFFERBOMBER_MCP_ALLOW_WRITE", "").lower() in {"1", "true", "yes"}
        if self._confirmations.peek(confirmation_token) is None:
            return _fail("confirmation_required", "call prepare_export first")
        if not allow_write:
            return _fail("write_not_allowed", "human approval + allow_write=true required")
        self._confirmations.consume(confirmation_token)
        with self.ctx.session() as session:
            version = session.get(ResumeVersion, version_id)
            if version is None:
                return _fail("not_found", f"resume version {version_id} not found")
            path = Path(version.file_path)
        report = ReactiveResumeProvider.validate_pdf(path)
        return _ok({
            "version_id": version.id,
            "file_path": version.file_path,
            "validation": report.as_dict(),
        })

    def prepare_export(self, version_id: int) -> dict[str, Any]:
        token = self._confirmations.create({"action": "export_pdf", "version_id": version_id})
        return _ok({
            "confirmation_token": token,
            "preview": {"action": "export_pdf", "version_id": version_id},
        })

    def get_generation_status(self, job_id: int | None = None) -> dict[str, Any]:
        with self.ctx.session() as session:
            stmt = select(ResumeVersion).order_by(ResumeVersion.id.desc())
            if job_id is not None:
                stmt = stmt.where(ResumeVersion.job_id == job_id)
            rows = session.execute(stmt.limit(20)).scalars().all()
            return _ok({
                "versions": [
                    {
                        "id": r.id, "job_id": r.job_id, "file_path": r.file_path,
                        "sha256": r.file_sha256, "immutable": r.immutable,
                    }
                    for r in rows
                ],
                "count": len(rows),
            })

    # ---------------- helpers ----------------

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
