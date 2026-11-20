# OfferBomber MCP 集成指南

## 架构：宿主推理 + OfferBomber 确定性执行

OfferBomber 通过标准 MCP（Model Context Protocol）向智能体宿主暴露确定性能力。
**推理由宿主大模型负责，OfferBomber 不反向调用任何宿主的大模型 HTTP 接口。**

```
WorkBuddy / DeepSeek Harness 宿主大模型
        │  （理解任务、分析 JD、决策选哪些事实）
        ▼  MCP stdio（本版本仅实现 stdio，Streamable HTTP 未实现）
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

工具分级：**L0** 只读查询；**L1** 受控网络发现/本地数据更新；**L2** 重要文件写入与外部服务操作（需人类批准）。每个工具的 docstring 标注了级别与副作用。

| 工具 | 级别 | 说明 |
|---|---|---|
| `search_jobs(keyword, limit, platform)` | L0 只读 | 搜索已采集岗位 |
| `discover_jobs(platform, max_companies)` | **L1 受控发现** | 仅用配置中已启用的招聘源发现并入库新岗位；不接受任意 URL，不投递/不发邮件 |
| `get_job(job_id)` | L0 | 岗位完整 JD |
| `retrieve_experience(query, top_k, verified_only)` | L0 | FTS5 检索经历事实（仅当前用户已确认事实） |
| `get_confirmed_facts(experience_id?)` | L0 | 已确认、未删除事实（唯一可入简历来源） |
| `analyze_jd(job_id, requirements_json?)` | L0 | JD 分析；宿主可传回结构化结果校验 |
| `build_resume_plan(job_id, fact_ids, ...)` | L0 | 基于选定事实构建规划草稿；任一 Fact ID 无权/未确认整单失败 |
| `validate_resume_facts(draft_json?, fact_ids?)` | L0 | 数字/论文状态一致性校验 + 完整事实授权 |
| `prepare_generation(job_id, source_resume_id, fact_ids, final_plan_json?, ...)` | L2 准备（无写入） | 生成不可变规范化规划 + SHA-256 + 完整预览 + 令牌 |
| `generate_resume(confirmation_token, allow_write)` | **L2 写** | 仅渲染已批准规划快照（无 `final_facts` 参数）；克隆→写回→导出 PDF→记录版本 |
| `validate_generated_pdf(version_id)` | L0 只读 | PyMuPDF 校验已有 PDF，不是重新导出 |
| `prepare_export(version_id)` | L2 准备（无写入） | PDF 重新导出/校验的预览 + 令牌 |
| `export_pdf(confirmation_token, version_id, allow_write)` | **L2 兼容别名** | 历史上只校验不导出；现强制人类批准。只读校验请用 `validate_generated_pdf` |
| `get_generation_status(job_id?)` | L0 | 当前用户版本与状态查询（按 user_id 隔离） |

保留原有 12 个工具兼容；新增 `discover_jobs` 与 `validate_generated_pdf`，共 **14 个**。`generate_resume` 移除了 `final_facts` 参数（破坏性但有意为之）。

**输入/输出 Schema** 由 MCP 工具定义自动生成（`inputSchema`）；所有写工具返回结构化 `{ok, error, message}` 或 `{ok: true, ...}`，不向宿主抛未捕获异常。

## 权限控制（human-in-the-loop，Phase 3.3）

### 事实访问（所有接收 Fact ID 的工具）

一条事实可进入简历，必须同时满足：存在、所属 Experience 属于当前 MCP 用户、`verification_status == CONFIRMED`、`is_deleted == False`。
统一在 `authorization.load_authorized_facts()` 执行，**任一条不满足则整个请求失败，不静默跳过**。适用于 `build_resume_plan`、`validate_resume_facts`、`prepare_generation`、`_run_generation`。
`ResumeVersion` 按 `user_id` 隔离，跨用户不可读。

### 技能白名单

宿主可重排事实，但技能只能从已确认事实派生的可信集合中选取。未知技能（如 `QuantumTeleportation`、`ForgedSkill`）被剔除，无法经 `final_plan_json` 的 `skills` 注入。

### 人类批准（关键架构修复）

宿主模型**永远不是批准者**。流程：

1. 宿主调用 `prepare_generation` / `prepare_export` → `confirmation_token` + **完整最终内容预览**（含每条事实原文/渲染文本/技能/校验状态、plan_sha256）。
2. 宿主把预览展示给人类。
3. **人类在本地终端**（另一进程）执行：
   ```bash
   offerbomber mcp pending                 # 列出待批准操作
   offerbomber mcp preview <token>         # 查看即将生成的内容
   offerbomber mcp approve <token>         # 批准（只有这一步能批准）
   offerbomber mcp reject <token>          # 拒绝（不可逆）
   ```
4. 获批后宿主调用 `generate_resume` / `export_pdf`，传 `confirmation_token` + `allow_write=true`。`allow_write` 只是二级闸门，不是人类信号。
5. 令牌单次消费、原子化（SQLite 条件 UPDATE），TTL 15 分钟；PENDING/REJECTED/EXPIRED 令牌一律拒绝写入。

**为何不用 MCP Elicitation**：MCP 规范明确禁止用表单式 elicitation 索取凭证/令牌，且它只是"向用户请求信息"的机制，不是服务器可验证的授权信号；宿主也未提供可靠的人类批准信号。因此审批放在带外本地 CLI，模型调用 `approve` 工具不算批准。

### 批准内容 = 生成内容

`prepare_generation` 把完整规划规范化为确定性 JSON 并计算 SHA-256，令牌绑定规划快照、哈希、用户、岗位、源简历、事实版本与到期时间。`generate_resume` 只消费该快照，**没有 `final_facts` 覆盖通道**；批准事实版本发生变化则审批失效，需重新批准。未经证实的改写保留原文并标记 `REVERTED`。源简历永远只被克隆，不会被覆盖。

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
| `OFFERBOMBER_MCP_ALLOW_WRITE` | 空 | `1` 表示服务端二级写闸门（仍需人类 CLI 批准的令牌） |
| `OFFERBOMBER_RXRESUME_API_KEY` | 空 | PDF 生成用的简历服务密钥；也可放在 `~/.offerbomber/credentials.json`（仓库外） |

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
随后宿主模型应能看到 `offerbomber` 下的 14 个工具。

WorkBuddy 官方核查结论（2026-11-20）：官方文档与本机 `CODEBUDDY_MCP_CONFIG` 均确认标准
stdio `mcpServers` 结构（command/args/env）；工具发现走 `tools/list`、调用走 `tools/call`，
长任务可异步轮询。但**宿主未提供服务器可验证的用户批准信号**，且 MCP Elicitation 规范
禁止用于授权/凭证，故本项目的 L2 写入审批走本地 CLI（见上节），不依赖宿主确认。

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

1. `discover_jobs()`（可选，L1）从配置源发现新岗位；或 `search_jobs(keyword)` / `get_job(job_id)`。
2. 宿主模型分析 JD（可调用 `analyze_jd`，或自行分析后用 `requirements_json` 回传）。
3. `retrieve_experience(query)` → 宿主据检索结果选择 `fact_ids`（仅当前用户已确认事实）。
4. `build_resume_plan(job_id, fact_ids)` → 草稿；无权/未确认事实整单失败。
5. `validate_resume_facts(draft_json)` → 确认无编造数字/录用状态。
6. `prepare_generation(..., final_plan_json=宿主重排内容)` → 完整预览 + 令牌 + plan_sha256。
7. 宿主展示预览；**人类在本地终端** `offerbomber mcp approve <token>`。
8. `generate_resume(token, allow_write=true)` → 克隆+写回+导出 PDF（渲染已批准快照，无改稿通道）。
9. `validate_generated_pdf(version_id)` / `get_generation_status(job_id)` → 校验与查询。

全程不需要用户提供模型 API Key。Reactive Resume 的 key 仅用于 PDF 生成环节
（这是简历渲染服务，不是大模型接口），通过 `OFFERBOMBER_RXRESUME_API_KEY` 配置。

## 验证等级

| 等级 | 内容 | 状态 |
|---|---|---|
| 单元测试 | toolkit 业务逻辑 | **已跑通**（`pytest -q` 139 passed，2026-11-20） |
| 真实 MCP 协议测试 | stdio 子进程握手、`tools/list`、`tools/call`、Schema | **已跑通**（真实 JSON-RPC，非 mock） |
| 权限/安全测试 | 跨用户隔离、技能注入、确认机制、并发消费、SSRF | **已跑通**（`tests/test_phase33_security.py` 29 项） |
| Mock 宿主测试 | 用假 DiscoveryService 验证发现流程 | **已跑通**（非真实网络） |
| WorkBuddy 真实宿主联调 | 在 WorkBuddy 中注册 MCP、用其模型调用工具 | **部分验证**：当前 WorkBuddy 会话曾真实加载 12 工具并调用；Phase 3.3 的 14 工具与人类审批流程待重启后复验 |
| DeepSeek Harness 真实宿主联调 | 在 dsh profile 注册 MCP、真实调用 | **未验证**：当前环境未安装 dsh 运行时 |
| 完整真实 JD→PDF 端到端 | 宿主模型全程编排至 PDF | **未验证**：需重启加载新代码 + 真实审批流程后执行 |

未验证项不冒充成功。宿主侧注册属于用户环境操作，本项目只提供符合官方格式的 Server 与配置样例。

## 从 GitHub 接入（其他用户）

1. `git clone` 本项目 → `pip install -e .`（依赖含 `mcp>=1.2,<2`）。
2. 运行 `offerbomber init` 生成 `config.yaml` 并初始化数据库。
3. 按上文把 stdio Server 加入自己的 WorkBuddy `mcp.json` 或 dsh profile。
4. 若要生成 PDF，另配 `OFFERBOMBER_RXRESUME_API_KEY` 与源简历 ID。
5. 重启宿主会话，确认工具列表出现 `offerbomber` 的工具。

## 待确认清单

- [待核实: WorkBuddy 官方公开 MCP 文档链接与 UI 信任流程]——依据本机 `CODEBUDDY_MCP_CONFIG` 实际结构确认 stdio 格式；官方文档站未见独立 MCP 页。已确认宿主无服务器可验证的批准信号。
- [待核实: `@deepseek-ai/dsh-mcp-client` 具体版本的配置字段]——依据官方 handbook 文档，部署时核对该版本 README。
- Streamable HTTP 传输本版本未实现；文档不再将其描述为可用。
- 现有真实数据库若由旧 `create_all` 建成、缺新列，需先备份再由用户执行 `alembic upgrade head`（可能报 duplicate column，需人工处理历史状态）；本项目不擅自修改用户数据。
- WorkBuddy / DeepSeek Harness 的 Phase 3.3 真实宿主联调待用户在各自环境完成。
