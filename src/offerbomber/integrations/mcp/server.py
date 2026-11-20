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
            "OfferBomber exposes deterministic job-hunting capabilities: job discovery, "
            "confirmed-fact retrieval, JD analysis, resume planning/fact validation and "
            "Reactive Resume PDF generation. The host model performs all reasoning. "
            "Write tools require a confirmation token from the matching prepare_* tool and "
            "explicit human approval (OFFERBOMBER_MCP_ALLOW_WRITE=1 or allow_write=true)."
        ),
        lifespan=lifespan,
    )

    def toolkit() -> McpToolkit:
        if "toolkit" not in holder:
            raise RuntimeError("MCP server lifespan not initialized")
        return holder["toolkit"]

    @mcp.tool()
    def search_jobs(keyword: str = "", limit: int = 10, platform: str = "") -> dict[str, Any]:
        """按关键词搜索已采集的招聘岗位。关键词匹配职位名或公司名;limit 上限 50。"""
        return toolkit().search_jobs(keyword=keyword, limit=limit, platform=platform)

    @mcp.tool()
    def get_job(job_id: int) -> dict[str, Any]:
        """按 ID 获取岗位完整详情(JD 描述、要求、学历要求)。"""
        return toolkit().get_job(job_id)

    @mcp.tool()
    def retrieve_experience(query: str, top_k: int = 5, verified_only: bool = True) -> dict[str, Any]:
        """用 FTS5 检索个人经历事实。宿主模型应基于此形成简历规划,不要编造事实。"""
        return toolkit().retrieve_experience(query, top_k=top_k, verified_only=verified_only)

    @mcp.tool()
    def get_confirmed_facts(experience_id: int | None = None) -> dict[str, Any]:
        """列出已确认事实。可按经历 ID 过滤。这些是可进入简历的唯一事实来源。"""
        return toolkit().get_confirmed_facts(experience_id)

    @mcp.tool()
    def analyze_jd(job_id: int, requirements_json: str = "") -> dict[str, Any]:
        """分析岗位 JD。默认确定性抽取;宿主模型可把结构化结果通过 requirements_json 传回校验。"""
        return toolkit().analyze_jd(job_id, requirements_json=requirements_json)

    @mcp.tool()
    def build_resume_plan(
        job_id: int,
        fact_ids: list[int],
        target_position: str = "",
        language: str = "zh",
        pages: int = 1,
    ) -> dict[str, Any]:
        """基于选定的已确认事实构建简历规划草稿。target_position 优先取岗位名。"""
        return toolkit().build_resume_plan(
            job_id, fact_ids, target_position=target_position, language=language, pages=pages
        )

    @mcp.tool()
    def validate_resume_facts(draft_json: str = "", fact_ids: list[int] | None = None) -> dict[str, Any]:
        """校验草稿:模型引入的量化数字或论文录用/发表状态如不在原文中即判违规。"""
        return toolkit().validate_resume_facts(draft_json=draft_json, fact_ids=fact_ids)

    @mcp.tool()
    def prepare_generation(
        job_id: int,
        source_resume_id: str,
        fact_ids: list[int],
        language: str = "zh",
        pages: int = 1,
    ) -> dict[str, Any]:
        """生成简历的只读预览。返回 confirmation_token;必须先向人类展示预览并取得同意。"""
        return toolkit().prepare_generation(
            job_id, source_resume_id, fact_ids, language=language, pages=pages
        )

    @mcp.tool()
    def generate_resume(
        confirmation_token: str,
        final_facts: str = "",
        allow_write: bool = False,
    ) -> dict[str, Any]:
        """执行简历生成(克隆源简历→写回→导出 PDF→记录版本)。需确认令牌与 allow_write。"""
        return toolkit().generate_resume(
            confirmation_token, final_facts=final_facts, allow_write=allow_write
        )

    @mcp.tool()
    def prepare_export(version_id: int) -> dict[str, Any]:
        """PDF 导出/校验的只读预览,返回 confirmation_token。"""
        return toolkit().prepare_export(version_id)

    @mcp.tool()
    def export_pdf(confirmation_token: str, version_id: int, allow_write: bool = False) -> dict[str, Any]:
        """校验并导出已生成版本的 PDF。需确认令牌与 allow_write。"""
        return toolkit().export_pdf(
            confirmation_token, version_id=version_id, allow_write=allow_write
        )

    @mcp.tool()
    def get_generation_status(job_id: int | None = None) -> dict[str, Any]:
        """查询已生成的简历版本及状态。"""
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
