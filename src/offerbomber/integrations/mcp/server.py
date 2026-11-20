"""OfferBomber MCP Server (stdio).

A host (WorkBuddy / DeepSeek Harness / any MCP client) owns reasoning and calls
these deterministic tools. OfferBomber never calls a host model endpoint.

Entry point:
    PYTHONPATH=src python -m offerbomber.integrations.mcp.server --config config.yaml

WorkBuddy ~/.workbuddy/mcp.json:
    {"mcpServers": {"offerbomber": {
        "type": "stdio", "command": ".../.venv/Scripts/python.exe",
        "args": ["-m", "offerbomber.integrations.mcp.server"],
        "env": {"PYTHONPATH": "D:/OfferBomber/src", "OFFERBOMBER_MCP_CONFIG": "D:/OfferBomber/config.yaml"}
    }}}
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP

from ...app import AppContext
from .toolkit import McpToolkit


def create_server() -> FastMCP:
    holder: dict[str, McpToolkit] = {}

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        config_path = os.getenv("OFFERBOMBER_MCP_CONFIG", "config.yaml")
        path = Path(config_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        ctx = AppContext.from_config(path)
        holder["toolkit"] = McpToolkit(
            ctx, user_id=os.getenv("OFFERBOMBER_MCP_USER", "default")
        )
        try:
            yield
        finally:
            ctx.close()
            holder.pop("toolkit", None)

    mcp = FastMCP(
        "offerbomber",
        instructions=(
            "OfferBomber exposes deterministic job-hunting capabilities. Tools are graded: "
            "L0 = read-only; L1 = controlled network discovery of configured job boards; "
            "L2 = file writes / external service operations requiring HUMAN approval. "
            "The host model performs all reasoning but is never the approver. For any L2 "
            "write: call the matching prepare_* tool, show the returned preview to the "
            "human, and the human must run `offerbomber mcp approve <token>` in a local "
            "terminal. Only then call generate_resume/export_pdf with the token. "
            "allow_write is a secondary gate, not a substitute for human approval. "
            "The approved plan snapshot is immutable; no post-approval content override exists."
        ),
        lifespan=lifespan,
    )

    def toolkit() -> McpToolkit:
        if "toolkit" not in holder:
            raise RuntimeError("MCP server lifespan not initialized")
        return holder["toolkit"]

    @mcp.tool()
    def search_jobs(keyword: str = "", limit: int = 10, platform: str = "") -> dict[str, Any]:
        """[L0 只读] 按关键词搜索已采集的招聘岗位。关键词匹配职位名或公司名;limit 上限 50。"""
        return toolkit().search_jobs(keyword=keyword, limit=limit, platform=platform)

    @mcp.tool()
    def discover_jobs(platform: str = "", max_companies: int = 0) -> dict[str, Any]:
        """[L1 受控网络发现] 仅用配置中已启用的招聘源发现并入库新岗位。不接受任意 URL,不投递/不发邮件。"""
        return toolkit().discover_jobs(platform=platform, max_companies=max_companies)

    @mcp.tool()
    def get_job(job_id: int) -> dict[str, Any]:
        """[L0 只读] 按 ID 获取岗位完整详情(JD 描述、要求、学历要求)。"""
        return toolkit().get_job(job_id)

    @mcp.tool()
    def retrieve_experience(query: str, top_k: int = 5, verified_only: bool = True) -> dict[str, Any]:
        """[L0 只读] 用 FTS5 检索个人经历事实(仅当前用户已确认事实)。宿主应基于此规划,不要编造。"""
        return toolkit().retrieve_experience(query, top_k=top_k, verified_only=verified_only)

    @mcp.tool()
    def get_confirmed_facts(experience_id: int | None = None) -> dict[str, Any]:
        """[L0 只读] 列出当前用户已确认、未删除的事实。可按经历 ID 过滤。唯一可进入简历的事实来源。"""
        return toolkit().get_confirmed_facts(experience_id)

    @mcp.tool()
    def analyze_jd(job_id: int, requirements_json: str = "") -> dict[str, Any]:
        """[L0 只读] 分析岗位 JD。默认确定性抽取;宿主模型可把结构化结果通过 requirements_json 传回校验。"""
        return toolkit().analyze_jd(job_id, requirements_json=requirements_json)

    @mcp.tool()
    def build_resume_plan(
        job_id: int,
        fact_ids: list[int],
        target_position: str = "",
        language: str = "zh",
        pages: int = 1,
    ) -> dict[str, Any]:
        """[L0 只读] 基于选定的已确认事实构建简历规划草稿。任一 Fact ID 无权/未确认则整单失败。"""
        return toolkit().build_resume_plan(
            job_id, fact_ids, target_position=target_position, language=language, pages=pages
        )

    @mcp.tool()
    def validate_resume_facts(draft_json: str = "", fact_ids: list[int] | None = None) -> dict[str, Any]:
        """[L0 只读] 校验草稿:模型引入的量化数字或论文录用/发表状态如不在原文中即判违规。"""
        return toolkit().validate_resume_facts(draft_json=draft_json, fact_ids=fact_ids)

    @mcp.tool()
    def prepare_generation(
        job_id: int,
        source_resume_id: str,
        fact_ids: list[int],
        final_plan_json: str = "",
        language: str = "zh",
        pages: int = 1,
        target_position: str = "",
    ) -> dict[str, Any]:
        """[L2 准备,无写入] 生成不可变规范化简历规划并返回确认令牌与完整预览。final_plan_json 只能是宿主重排的已确认事实,技能与数字会被校验过滤。人类须在本地 CLI 批准该令牌。"""
        return toolkit().prepare_generation(
            job_id, source_resume_id, fact_ids,
            final_plan_json=final_plan_json, language=language, pages=pages,
            target_position=target_position,
        )

    @mcp.tool()
    def generate_resume(
        confirmation_token: str,
        allow_write: bool = False,
    ) -> dict[str, Any]:
        """[L2 写入] 仅渲染已批准的规划快照(无 final_facts 参数,确认后内容不可改)。需人类 CLI 批准过的令牌 + allow_write。"""
        return toolkit().generate_resume(
            confirmation_token, allow_write=allow_write
        )

    @mcp.tool()
    def validate_generated_pdf(version_id: int) -> dict[str, Any]:
        """[L0 只读] 用 PyMuPDF 校验已生成版本的 PDF(存在/非空/关键内容)。不是重新导出。"""
        return toolkit().validate_generated_pdf(version_id)

    @mcp.tool()
    def prepare_export(version_id: int) -> dict[str, Any]:
        """[L2 准备,无写入] PDF 重新导出/校验的预览,返回 confirmation_token。"""
        return toolkit().prepare_export(version_id)

    @mcp.tool()
    def export_pdf(confirmation_token: str, version_id: int, allow_write: bool = False) -> dict[str, Any]:
        """[L2 兼容别名] 历史上本工具只校验已有 PDF、并未重新导出。现强制人类批准;真正的只读校验请用 validate_generated_pdf。"""
        return toolkit().export_pdf(
            confirmation_token, version_id=version_id, allow_write=allow_write
        )

    @mcp.tool()
    def get_generation_status(job_id: int | None = None) -> dict[str, Any]:
        """[L0 只读] 查询当前用户已生成的简历版本及状态(按 user_id 隔离)。"""
        return toolkit().get_generation_status(job_id)

    # ---------------- Phase 4-6: general-purpose job agent ----------------

    @mcp.tool()
    def search_profiles() -> dict[str, Any]:
        """[L0 只读] 列出当前用户的所有求职配置(支持多个求职活动)。"""
        return toolkit().search_profiles()

    @mcp.tool()
    def create_search_profile(criteria_json: str) -> dict[str, Any]:
        """[L1 写入] 创建通用求职配置。JSON 字段全部可空;未设置=不限制,系统不自行推断或强加限制。"""
        return toolkit().create_search_profile(criteria_json)

    @mcp.tool()
    def update_search_profile(profile_id: int, criteria_json: str) -> dict[str, Any]:
        """[L1 写入] 更新本人求职配置;非本人的 profile_id 被拒绝。"""
        return toolkit().update_search_profile(profile_id, criteria_json)

    @mcp.tool()
    def discover_companies(profile_id: int, max_companies: int = 20) -> dict[str, Any]:
        """[L1 受控网络发现] 从配置生成查询、用已配置搜索源自动发现并验证企业招聘入口。不接受任意 URL,不绕过登录/验证码。"""
        return toolkit().discover_companies(profile_id, max_companies)

    @mcp.tool()
    def import_jobs(file_path: str) -> dict[str, Any]:
        """[L1 本地导入] 从用户提供的本地 CSV/JSON 导入岗位。不接受网络 URL。"""
        return toolkit().import_jobs(file_path)

    @mcp.tool()
    def match_jobs(profile_id: int, job_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """[L0 只读] 用当前用户已确认事实按配置对岗位通用匹配。硬性条件与一般偏好分开,语义不匹配不等于资格不符。"""
        return toolkit().match_jobs(profile_id, job_id, limit)

    @mcp.tool()
    def list_application_candidates(limit: int = 20) -> dict[str, Any]:
        """[L0 只读] 列出可申请的岗位候选(匹配状态/招聘渠道/是否已投递)。"""
        return toolkit().list_application_candidates(limit)

    @mcp.tool()
    def prepare_application(
        job_id: int, channel: str = "EMAIL", recipient: str = "", subject: str = "",
        body: str = "", resume_version_id: int | None = None,
    ) -> dict[str, Any]:
        """[L2 准备,无外部发送] 准备申请草稿并返回审批令牌。收件地址取自岗位证据;模型不能自批。"""
        return toolkit().prepare_application(
            job_id, channel, recipient, subject, body, resume_version_id
        )

    @mcp.tool()
    def preview_application(confirmation_token: str) -> dict[str, Any]:
        """[L0 只读] 展示待批准申请草稿完整内容。人类据此决定,不单独产生发送。"""
        return toolkit().preview_application(confirmation_token)

    @mcp.tool()
    def approve_application(confirmation_token: str, allow_write: bool = False) -> dict[str, Any]:
        """[L2 写入] 提交已由人类 CLI 批准的申请;重复令牌/未批准/超限均拒绝。模型调用不构成批准。"""
        return toolkit().approve_application(confirmation_token, allow_write)

    @mcp.tool()
    def application_status(application_id: int | None = None) -> dict[str, Any]:
        """[L0 只读] 查询申请状态轨迹与自动化开关。"""
        return toolkit().application_status(application_id)

    @mcp.tool()
    def email_connect_status() -> dict[str, Any]:
        """[L0 只读] 查询邮箱连接状态(不含任何凭证)。"""
        return toolkit().email_connect_status()

    @mcp.tool()
    def inbox_sync(since_iso: str = "", limit: int = 50) -> dict[str, Any]:
        """[L1 受控外部操作] 增量同步招聘邮件并分类关联;无法关联进人工队列,不擅自改状态。无授权则明确失败。"""
        return toolkit().inbox_sync(since_iso, limit)

    @mcp.tool()
    def classify_recruitment_email(subject: str, body: str) -> dict[str, Any]:
        """[L0 只读] 招聘邮件中英文确定性分类,仅供人类参考,不单独改申请状态。"""
        return toolkit().classify_recruitment_email(subject, body)

    @mcp.tool()
    def interviews_list() -> dict[str, Any]:
        """[L0 只读] 列出面试/笔试事件(含时区与确认状态)。"""
        return toolkit().interviews_list()

    @mcp.tool()
    def tasks_list(limit: int = 50) -> dict[str, Any]:
        """[L0 只读] 列出持久化任务及状态。"""
        return toolkit().tasks_list(limit)

    @mcp.tool()
    def run_tasks(max_tasks: int = 5) -> dict[str, Any]:
        """[L1 受控] 执行到期任务(受全局暂停约束,次数上限 50)。"""
        return toolkit().run_tasks(max_tasks)

    @mcp.tool()
    def report_daily(days: int = 1) -> dict[str, Any]:
        """[L0 只读] 生成日报(Markdown+JSON)到本地输出目录。"""
        return toolkit().report_daily(days)

    @mcp.tool()
    def statistics() -> dict[str, Any]:
        """[L0 只读] 汇总统计(岗位/申请/回复/事件),数字全部来自本地库。"""
        return toolkit().statistics()

    @mcp.tool()
    def pause_automation(paused: bool = True) -> dict[str, Any]:
        """[L1 本地控制] 一键暂停/恢复自动执行(默认暂停)。"""
        return toolkit().pause_automation(paused)

    @mcp.tool()
    def set_automation_mode(mode: str, max_per_day: int = 0) -> dict[str, Any]:
        """[L1 本地控制] 设置 MANUAL/ASSISTED/AUTO;AUTO 必须给每日上限,默认仍暂停。"""
        return toolkit().set_automation_mode(mode, max_per_day)

    @mcp.tool()
    def system_health() -> dict[str, Any]:
        """[L0 只读] 系统健康:数据库/FTS/岗位数/自动化开关/各能力实现状态。"""
        return toolkit().system_health()

    return mcp


def main() -> None:
    # stdio transport reserves stdout for JSON-RPC; send all logs to stderr.
    structlog.configure(
        logger_factory=structlog.WriteLoggerFactory(file=sys.stderr),
    )
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
