# Phase 3 完成情况报告

日期：2026-11-19
范围：Part A（Phase 2 稳定性修复）+ Part B-I（模型接口、智能解析/分析/检索/规划、Reactive Resume、CLI、测试）

## 一、先做的核查

- 拉取远端：`origin/main` 与本地一致（`22f39a3`），未覆盖他人修改。基线测试 16→35（Phase 2 结束时的数量）全绿。
- 官方文档核查（不臆造接口）：
  - WorkBuddy OpenAPI：无 OpenAI 兼容 Chat Completions；可用的是 `POST/GET /openapi/v2/tasks` + ACP（SSE + JSON-RPC `initialize`/`session/load`/`session/prompt`），Bearer 授权，本地助理 API 另列。
  - DeepSeek Harness：读官方仓库 `docs/cli-help.md`，确认 `dsh --profile headless [--json] [--session-id]`、`dsh --profile acp` 真实存在；仓库为 developer preview。
  - Reactive Resume：官方 API 文档 + `/api/openapi/spec.json`，确认 `x-api-key` 认证、`/resumes`、`/resumes/{id}/copies`、JSON Patch/PUT、`/resumes/{id}/pdf`、限流与 `code` 错误契约、`ResumeData` 的 `basics`/`sections`/items 结构。

## 二、Part A：数据一致性修复

- **A1 统一索引维护**：`storage/fts.py` 收敛为一组只改索引、不自行提交的原语（`index_fact`/`remove_fact_index`/`reindex_experience`/`rebuild_fts`/`consistency_report`），`index_fact` 尊重 `is_deleted`。service 与 importer 全部改走同一实现。经历标题/描述编辑、事实增删改确认、版本恢复都会同步索引。
- **A2 原子事务**：删除「先 commit 事实、再 commit 索引」的双提交；数据与索引在同一 `with session` 内一次提交。新增故障注入测试：注入索引失败 → 事务回滚 → 经历/事实/索引均无残留。
- **A3 版本恢复边界**：事实 ID 保持稳定；事实版本单调递增（不回退快照号）；已确认状态不被旧快照降级；归档状态作为生命周期状态不被内容恢复覆盖；历史快照不可变，只追加新 RESTORE 版本；恢复后重建索引且通过一致性报告校验。

## 三、Part B-I：智能能力

- **ModelProvider**：异步、Pydantic 结构化、超时、取消传播、错误分类（超时/取消/传输/认证/限流/非法输出/提供方/不可用）、有界指数退避重试、structlog 日志、失败安全回退。六种任务类型齐全。
- **Provider 实现**：Mock（确定性、可注入故障、永不声称真实）；WorkBuddy（云端任务 + ACP 全生命周期）；DeepSeek Harness（headless CLI 子进程）；可选 OpenAI 兼容。`auto` 优先真实已配置适配器，否则 Mock 并明确标注。`model doctor` / `model test` 输出 `is_mock`、`real_live_verified`。
- **模型简历解析**：`profile import --parser model`；文本质量检查；结构化提取；量化声明与「投稿→录用」守卫；每条关键事实带 `locator`。确定性解析器保留为回退。
- **JobAnalyzer**：`jobs analyze --job-id`；输出 `JobRequirements`（必须/加分/一般、evidence 原文映射、未知标记、语言）；evidence 不在 JD 中则降级为 UNKNOWN；失败回退关键词提取。
- **两阶段检索**：`search rerank --job-id`；第一阶段沿用 FTS5+标签+关键词，第二阶段模型重排；拒绝不存在的 Fact ID，不改写事实正文，失败保留确定性顺序。
- **智能规划**：`resume plan --job-id`；模型决定经历/事实选择、顺序、技能突出、篇幅、语言；一页/两页预算；每条事实记录 `fact_id/fact_version/source_id/original_text/rendered_text`（`fact_audit`）；生成后一致性校验——新出现的数字或「投稿→录用」自动回退原文并告警；未确认事实不进入规划。
- **Reactive Resume**：`resume generate/validate/versions/show`；基于官方契约的适配器：克隆源简历（绝不覆盖主简历）→ 映射内容并保留 metadata/design/template → 导出 PDF → PyMuPDF 质检（存在/可打开/有文本/姓名/关键内容/无空白页）→ 保存不可变 `ResumeVersion`（含事实快照与 SHA-256）。无 key 明确失败，不产生假 PDF。
- **CLI**：全部按要求新增，复用 AppContext 与配置，支持 JSON 输出，错误信息清晰。

## 四、验证

- 测试：`pytest tests/ -q` → **76 passed**（Phase 2 基线 35 → 新增 Part A 8、模型 10、解析分析 7、重排规划 6、ReactiveResume 5、端到端 4）。
- 真实本地端到端（隔离 demo 库）：`discover` 采集真实 Stripe 岗位 → `jobs analyze --job-id 366` → `search rerank "distributed training PyTorch" --job-id 366` → `resume plan` 选中真实 fact_id 与来源 SHA-256。全链路命令可运行、输出结构化、Mock 身份明确标注。
- 未做真实联调：无宿主模型凭证、无 `dsh` 运行环境、无 Reactive Resume API key。模型层与 HTTP 层用 respx/mock 做外部接口模拟测试，**不宣称真实端到端成功**。

## 五、区分四类验证

| 类别 | 本次是否完成 | 证据 |
|---|---|---|
| 本地单元测试 | 是 | 76 passed |
| 外部接口模拟测试 | 是 | respx 覆盖复制/更新/导出/失败；Mock Provider 覆盖故障矩阵 |
| 真实宿主模型联调 | 否（缺凭证/环境） | `model doctor` 明确 `real_live_verified=false` |
| 真实 Reactive Resume 联调 | 否（缺 API key） | `resume generate` 在无 key 时直接失败退出 |

## 六、已知限制与后续

- 扫描件 OCR、视觉排版检查未实现；PDF 质检仅为自动文本层检查，需人工预览。
- WorkBuddy/DeepSeek Harness 适配基于当前官方文档，尚未在授权环境跑过；凭证到位后需执行一次真实联调。
- 缓存复用（相同 job_id + 事实版本 + 模板）的数据结构已预留（`ResumeVersion` + `fact_snapshot`），但自动查重复用逻辑留待有真实服务后启用。
- 邮件、投递、反馈、MCP Server、调度仍属后续阶段，本轮未提前开发。
