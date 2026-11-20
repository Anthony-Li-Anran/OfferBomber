# OfferBomber MCP 集成指南

## 架构：宿主推理 + OfferBomber 确定性执行

OfferBomber 通过标准 MCP（Model Context Protocol）向智能体宿主暴露确定性能力。
**推理由宿主大模型负责，OfferBomber 不反向调用任何宿主的大模型 HTTP 接口。**

```
WorkBuddy / DeepSeek Harness 宿主大模型
        │  （理解任务、分析 JD、决策选哪些事实）
        ▼  MCP stdio / Streamable HTTP
OfferBomber MCP Server
        │  （检索、校验、规划落盘、Reactive Resume、PDF）
        ├── 招聘发现 / 岗位详情
        ├── 经历检索 / 已确认事实
        ├── JD 分析 / 简历规划 / 事实校验
        └── 生成简历 / 导出 PDF / 版本查询
        ▼
SQLite + Reactive Resume → PDF
```

这同时支持两种模式：

- **模式 A 独立运行**：`offerbomber` CLI 自己调用配置的模型（`OFFERBOMBER_MODEL_*`），适合无宿主的脚本场景。已用真实 DeepSeek key 联调通过（`docs/PHASE3_REPORT.md` 第九节）。
- **模式 B 宿主托管**：宿主模型负责推理，通过 MCP 调用本项目的工具。**不需要任何额外模型 API Key**。本文档的主体。

## 工具清单

| 工具 | 级别 | 说明 |
|---|---|---|
| `search_jobs(keyword, limit, platform)` | L0 只读 | 搜索已采集岗位 |
| `get_job(job_id)` | L0 | 岗位完整 JD |
| `retrieve_experience(query, top_k, verified_only)` | L0 | FTS5 检索经历事实 |
| `get_confirmed_facts(experience_id?)` | L0 | 已确认事实（唯一可入简历来源） |
| `analyze_jd(job_id, requirements_json?)` | L0 | JD 分析；宿主可传回结构化结果校验 |
| `build_resume_plan(job_id, fact_ids, ...)` | L0 | 基于选定事实构建规划草稿 |
| `validate_resume_facts(draft_json?, fact_ids?)` | L0 | 数字/论文状态一致性校验 |
| `prepare_generation(...)` | L0 预览 | 返回 confirmation_token，不写文件 |
| `generate_resume(confirmation_token, final_facts?, allow_write)` | **L2 写** | 克隆→写回→导出 PDF→记录版本 |
| `prepare_export(version_id)` | L0 预览 | 返回 confirmation_token |
| `export_pdf(confirmation_token, version_id, allow_write)` | **L2 写** | PDF 校验导出 |
| `get_generation_status(job_id?)` | L0 | 版本与状态查询 |

**输入/输出 Schema** 由 MCP 工具定义自动生成（`inputSchema`）；所有写工具返回结构化 `{ok, error, message}` 或 `{ok: true, ...}`，不向宿主抛未捕获异常。

## 权限控制（human-in-the-loop）

写类工具采用两阶段确认，防止宿主模型误触发不可逆操作：

1. 宿主调用 `prepare_generation` / `prepare_export` → 得到 `confirmation_token` + 动作预览。
2. **宿主必须把预览展示给人类并取得同意。**
3. 宿主调用 `generate_resume` / `export_pdf`，同时传 `confirmation_token` 与 `allow_write=true`；
   或服务端设置 `OFFERBOMBER_MCP_ALLOW_WRITE=1`。
4. 令牌单次使用，用后即焚。

数据源自事实库：宿主传入的 `final_facts` 文本会经过与 Phase 3.2 相同的一致性守卫——
未经证实的数字、把投稿升级为录用/发表都会被回退为原文，正文与 FactAudit 使用同一最终文本。
源简历永远只被克隆，不会被覆盖。

## 启动 Server

```bash
cd /d/OfferBomber
# 独立调试（会阻塞，等待 MCP 客户端连接）
PYTHONPATH=src .venv/Scripts/python.exe -m offerbomber.integrations.mcp.server
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PYTHONPATH` | — | 指向 `<仓库>/src`，或用已安装包 |
| `OFFERBOMBER_MCP_CONFIG` | `config.yaml` | 配置文件路径（含 SQLite 路径） |
| `OFFERBOMBER_MCP_USER` | `default` | 个人知识库用户标识 |
| `OFFERBOMBER_MCP_ALLOW_WRITE` | 空 | `1` 表示服务端允许写类工具（仍需确认令牌） |

## 接入 WorkBuddy

WorkBuddy 使用标准 `~/.workbuddy/mcp.json` 中的 `mcpServers` 配置，支持 stdio 类型
（`command` / `args` / `env`）。在配置文件中加入（路径按你的实际安装位置调整）：

```json
{
  "mcpServers": {
    "offerbomber": {
      "type": "stdio",
      "command": "D:/OfferBomber/.venv/Scripts/python.exe",
      "args": ["-m", "offerbomber.integrations.mcp.server"],
      "env": {
        "PYTHONPATH": "D:/OfferBomber/src",
        "OFFERBOMBER_MCP_CONFIG": "D:/OfferBomber/config.yaml"
      }
    }
  }
}
```

注册后重启 WorkBuddy 会话以加载新工具。在连接器/MCP 管理界面信任（Trust）该条目。
随后宿主模型应能看到 `offerbomber` 下的 12 个工具。

> 说明：WorkBuddy 官方文档未提供独立的 MCP 页面；上述格式依据本机
> `CODEBUDDY_MCP_CONFIG` 中的实际 `mcpServers` 结构（已含多个 stdio/http 连接器）确认。
> [待核实: WorkBuddy 官方公开 MCP 文档链接]

## 接入 DeepSeek Harness

DeepSeek Harness 官方提供 `@deepseek-ai/dsh-mcp-client` 插件，支持 stdio 与
Streamable HTTP。在项目 profile 的 composition 中增加一条：

```yaml
- id: mcp-offerbomber
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: offerbomber
    transport: stdio
    command: D:/OfferBomber/.venv/Scripts/python.exe
    args: ['-m', 'offerbomber.integrations.mcp.server']
    env:
      PYTHONPATH: D:/OfferBomber/src
      OFFERBOMBER_MCP_CONFIG: D:/OfferBomber/config.yaml
```

工具会以 `mcp__offerbomber__<tool>` 形式注册到 `ctx.tools`。
[待核实: 上述字段以你部署的 `@deepseek-ai/dsh-mcp-client` 版本 README 为准；
配置格式来自官方 deepseek-harness-handbook 的 MCP 集成文档]

## 端到端验收流程（模式 B）

给宿主一份 JD，然后依次调用：

1. `search_jobs(keyword)` 或 `get_job(job_id)` → 拿到岗位。
2. 宿主模型分析 JD（可调用 `analyze_jd`，或自行分析后用 `requirements_json` 回传）。
3. `retrieve_experience(query)` → 宿主据检索结果选择 `fact_ids`。
4. `build_resume_plan(job_id, fact_ids)` → 草稿。
5. `validate_resume_facts(draft_json)` → 确认无编造数字/录用状态。
6. `prepare_generation(...)` → 向人类展示预览并取得同意。
7. `generate_resume(token, final_facts, allow_write=true)` → 克隆+写回+导出 PDF。
8. `get_generation_status(job_id)` → 查询版本。

全程不需要用户提供模型 API Key。Reactive Resume 的 key 仅用于 PDF 生成环节
（这是简历渲染服务，不是大模型接口），通过 `OFFERBOMBER_RXRESUME_API_KEY` 配置。

## 验证等级

| 等级 | 内容 | 状态 |
|---|---|---|
| MCP 协议测试 | 真实 stdio 子进程握手、`tools/list`、`tools/call` | **已跑通**（`tests/test_mcp_server.py`，真实 JSON-RPC，非 mock） |
| Mock 宿主测试 | toolkit 业务逻辑、权限守卫、事实一致性 | **已跑通**（内存 DB） |
| WorkBuddy 真实宿主联调 | 在 WorkBuddy 中注册 MCP、用其模型调用工具 | **未验证**：需用户在 WorkBuddy 端注册并重启会话后执行 |
| DeepSeek Harness 真实宿主联调 | 在 dsh profile 注册 MCP、真实调用 | **未验证**：当前环境未安装 dsh 运行时 |

未验证项不冒充成功。宿主侧注册属于用户环境操作，本项目只提供符合官方格式的 Server 与配置样例。

## 从 GitHub 接入（其他用户）

1. `git clone` 本项目 → `pip install -e .`（依赖含 `mcp>=1.2,<2`）。
2. 运行 `offerbomber init` 生成 `config.yaml` 并初始化数据库。
3. 按上文把 stdio Server 加入自己的 WorkBuddy `mcp.json` 或 dsh profile。
4. 若要生成 PDF，另配 `OFFERBOMBER_RXRESUME_API_KEY` 与源简历 ID。
5. 重启宿主会话，确认工具列表出现 `offerbomber` 的工具。

## 待确认清单

- [待核实: WorkBuddy 官方公开 MCP 文档链接与 UI 信任流程]——依据本机实际配置确认格式，官方文档站未见独立 MCP 页。
- [待核实: `@deepseek-ai/dsh-mcp-client` 具体版本的配置字段]——依据官方 handbook 文档，部署时核对该版本 README。
- WorkBuddy / DeepSeek Harness 的真实宿主联调待用户在各自环境完成。
