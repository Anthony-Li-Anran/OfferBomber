# Phase 3 完成情况报告

日期：2026-11-19（Phase 3.2 加固：2026-11-20）
范围：Part A（Phase 2 稳定性修复）+ Part B-I（模型接口、智能解析/分析/检索/规划、Reactive Resume、CLI、测试）+ Phase 3.2（生产加固）

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

- 测试：`pytest tests/ -q` → 原 Phase 3 基线 **76 passed**（Phase 2 基线 35 → 新增 Part A 8、模型 10、解析分析 7、重排规划 6、ReactiveResume 5、端到端 4）。Phase 3.2 加固后总计 **93 passed**（见第八节）。
- 真实本地端到端（隔离 demo 库）：`discover` 采集真实 Stripe 岗位 → `jobs analyze --job-id 366` → `search rerank "distributed training PyTorch" --job-id 366` → `resume plan` 选中真实 fact_id 与来源 SHA-256。全链路命令可运行、输出结构化、Mock 身份明确标注。
- 未做真实联调：无宿主模型凭证、无 `dsh` 运行环境、无 Reactive Resume API key。模型层与 HTTP 层用 respx/mock 做外部接口模拟测试，**不宣称真实端到端成功**。

## 五、区分四类验证

| 类别 | 本次是否完成 | 证据 |
|---|---|---|
| 本地单元测试 | 是 | 76 passed |
| 外部接口模拟测试 | 是 | respx 覆盖复制/更新/导出/失败；Mock Provider 覆盖故障矩阵 |
| 真实宿主模型联调 | 否（缺凭证/环境） | `model doctor` 明确 `real_live_verified=false` |
| 真实 Reactive Resume 联调 | **是（2026-11-20 补测）** | 真实 key + 真实简历跑通克隆→填充→导出→质检，生成 PDF 70KB；见第七节 |

## 六、已知限制与后续

- 扫描件 OCR、视觉排版检查未实现；PDF 质检仅为自动文本层检查，需人工预览。
- WorkBuddy/DeepSeek Harness 适配基于当前官方文档，尚未在授权环境跑过；凭证到位后需执行一次真实联调。
- 缓存复用（相同 job_id + 事实版本 + 模板）的数据结构已预留（`ResumeVersion` + `fact_snapshot`），但自动查重复用逻辑留待有真实服务后启用。
- 邮件、投递、反馈、MCP Server、调度仍属后续阶段，本轮未提前开发。

## 七、2026-11-20 真实 Reactive Resume 联调（补测）

用户提供真实 API key 与一份中文简历 PDF（测试样本 `sample_resume_zh.pdf`，文档中姓名/文件名均为虚构），在本地跑通真实联调；明确：模型层仍用确定性构建器（无宿主模型凭证），只有 Reactive Resume HTTP 链路是真实的。真实 key 仅通过环境变量传入，从未写入仓库。

- 连通性：`GET /api/health` 200（v6.1.1）；`GET /resumes` 用真实 key 列出源简历 `General Resume`（示例 ID `00000000-0000-0000-0000-000000000000`，此处为脱敏占位）。
- 真实链路：本地导入并确认简历事实（23 条事实、8 段经历、2 条教育，隔离 user）→ 确定性 `ResumePlan`（6 条事实、2 段）→ 克隆源简历 → 写回内容 → `GET /resumes/{id}/pdf` → PyMuPDF 质检。**结果：生成 PDF 成功（70,246 bytes，8/8 质检项通过，姓名与关键内容命中）。**
- 联调暴露并修复 3 个 mock 测试覆盖不到的真实缺陷：
  1. **日期格式校验**：6.1.1 对 `dates.start/end` 强制 `/^\d{4}(?:-(?:0[1-9]|1[0-2]))?$/`。中文简历的 `2026.07`/`2024.09` 原样透传导致 `BAD_REQUEST`。`schema_mapper._normalize_date` 统一归一到 `YYYY`/`YYYY-MM`，兼容 `.` `/` `-` `年` 及 `至今/present`。
  2. **克隆 slug 冲突**：中文/符号标题 slug 化后恒定，重复生成触发 `RESUME_SLUG_ALREADY_EXISTS`。`clone_resume` 显式传入带 uuid 的 ASCII 唯一 slug。
  3. **写回基底错误**：写回时误用源简历文档做基底，会把克隆体 slug 覆盖成源 slug（同样触发冲突）。改为克隆后重读克隆体文档做基底，仅替换内容字段，设计/模板保持不变。
  - 附带修复联系方式拆分：邮箱支持多级域名、与电话分离（此前把整段联系方式塞进 email）。
- 安全：源简历 `General Resume` 全程未被修改；测试产生的中间克隆副本已通过 `DELETE /resumes/{id}` 清理，仅保留 1 个成功副本；本地数据库已还原，临时脚本已删除。
- 交付物：`output/deliverable/resume_live_test_sample.pdf`（已加入 .gitignore，不入库；文件名脱敏，姓名为占位文本）。
- 复核：`pytest -q` 仍 **76 passed**；测试 mock 已同步反映「重读克隆体做基底」的新调用顺序。

## 八、Phase 3.2 生产加固（2026-11-20）

本轮为可靠性加固，不重构架构。新增 17 项测试（`tests/test_phase32_hardening.py`），总计 **93 passed**（原 76 项全部保留、未放宽任何断言）。

### P0 — FactAudit 一致性
- 根因：一致性检查把正文回退为原始事实文本，但 `FactAudit.rendered_text` 仍取自模型的 `rendered_map`，二者可能不一致；`fact_snapshot` 由 `fact_audit` 序列化而来，因此也会与 PDF 正文不符。
- 修复：`SmartResumePlanner.plan` 引入 `final_text_by_fact`，正文、`FactAudit.rendered_text`、`fact_snapshot` 全部复用同一最终文本；被拒绝的改写记录回退结果（warning + 原文）。
- 测试：注入编造数字（`12%`→`99%`）与错误录用（`submitted`→`accepted`），断言正文 / 审计 / 快照三者文本完全相同，且无编造内容残留。

### P0 — 防止模型编造技能
- 根因：`decision.highlight_skills` 未经证据校验直接进入 `ResumePlan`，并在 `schema_mapper` 与正文 skill 标签合并后进入 PDF。
- 修复：双层防御。规划层用 `_trusted_skills`（已确认事实的 skills 并集）过滤 `_filter_highlight_skills`，大小写不敏感、按模型顺序保留可信技能、丢弃其余并告警；映射层 `_safe_skills` 只接受同时出现在已入计划事实 item 里的技能。
- 测试：模型返回 `QuantumTeleportation`/`ForgedSkill` 等不存在的技能，断言其不进入 plan 也不进入最终 ResumeData keywords。

### P1 — 科研 / 论文分类与发表状态
- 根因：`schema_mapper` 把 `RESEARCH` 与 `PAPER` 都映射到 `publications`，科研项目会被误呈现为论文发表。
- 修复：`WORK/INTERNSHIP→experience`、`PROJECT→projects`、`RESEARCH→projects`（研究经历，非论文）、`PAPER→publications`、`COMPETITION/AWARD→awards`。`PAPER` 条目的 description 前置真实状态标签，`_publication_status` 区分 `SUBMITTED / ACCEPTED / PUBLISHED / UNKNOWN`，绝不把投稿升级为录用或发表。
- 测试：RESEARCH 不进 publications；PAPER 保留 "Submitted / under review" 且不出现 "Published"；各类型进入正确 section；状态分类不升级。

### P1 — 目标职位传递
- 根因：`target_position` 回退用到 `requirements.resume_language`，把语言代码当作职位名称；`JobRequirements` 原本也没有岗位名称字段。
- 修复：`JobRequirements` 新增 `job_title`，由 `JobAnalyzer` 从权威 `Job.title` 填充（不采信模型输出）；`SmartResumePlanner._target_position` 优先级为「原岗位名称 → profile 默认目标职位 → 空串」，绝不使用 `resume_language` 或 `job_category`。
- 测试：有岗位时用岗位名称且不等于语言/类别；无岗位时回退 profile 默认值；`JobRequirements` 携带原始 title。

### P1 — Reactive Resume 认证检查
- 根因：`health_check` 只访问公开 `/api/health`，无法证明 key 有效，且把健康地址硬编码到公共域名，忽略自定义 `base_url`。
- 修复：保留公开健康检查（由 `_public_health_url` 从 `base_url` 推导，支持自定义地址），再用真实 key 发起 `GET /resumes`；区分 `ok / unavailable / service_unavailable / auth_failed / rate_limited / transport / unexpected_response`；detail 不含 key、日志不记录 key。
- 测试：respx 覆盖成功（自定义 base_url）、401、429、503、超时、无 key 六种情况。

### 隐私与凭证
- 全面扫描 Git 跟踪文件：真实姓名、简历文件名、简历 ID 已从文档替换为虚构/脱敏值；真实邮箱、电话、绝对路径、仓库归属均未出现。
- 完整 Git 历史中未发现 API key；`.env`、`config.yaml`、数据库、`output/` 均未被跟踪。
- 结论：**无需轮换凭证**（真实 key 从未进入仓库），真实个人信息仅曾存在于未跟踪的本地交付 PDF（`output/`，已 gitignore）。

### 能力状态
- 新增权威状态文件 `CAPABILITY_STATUS`；README 状态表、Reactive Resume 段落、测试数量同步更新。所有"适配器已实现"与"已真实联调"严格区分。

## 九、真实模型联调补充（2026-11-20，OpenAI-compatible / DeepSeek）

- 用户提供真实 DeepSeek key 与 `https://api.deepseek.com/v1`（`deepseek-chat`）。凭证仅通过环境变量 `OFFERBOMBER_MODEL_API_KEY` / `OFFERBOMBER_MODEL_BASE_URL` / `OFFERBOMBER_MODEL_NAME` 传入，**未写入任何文件、未提交**。
- 真实链路：`POST /chat/completions` → 真实模型返回 → `generate_structured(TaskType.JOB_ANALYSIS, ..., JobRequirements)` → Pydantic 校验 → `JobAnalyzer.analyze()` 不变量。
- 结果：HTTP 200；`required_skills` 正确区分 MUST（Python、Statistics）与 NICE_TO_HAVE（C++、论文），每条带原文 evidence；`job_title` 由权威 `Job.title` 注入、未被模型覆盖；`job_id` 注入；`resume_language=en`；`analysis_provider=openai_compatible`；未出现证据幻觉告警。
- 结论：**`openai_compatible` 适配器为真实联调通过**（真实 key、真实模型输出）；`workbuddy` 与 `deepseek_harness` 本轮未涉及，仍为"适配器已实现、待真实联调"。Mock 与真实调用严格区分，未虚报。
- 复测方式：设置三个环境变量后运行 `offerbomber model test` 或任意走 `JOB_ANALYSIS` 的命令。
