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

    return mcp


def main() -> None:
    # stdio transport reserves stdout for JSON-RPC; send all logs to stderr.
    structlog.configure(
        logger_factory=structlog.WriteLoggerFactory(file=sys.stderr),
    )
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
