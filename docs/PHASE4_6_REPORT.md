# Phase 4–6 — General-Purpose AI Job Hunting Agent

日期：2026-11-20
分支：main（遵循用户硬规则，不新建开发分支）
起点：main `4d99da9`（Phase 3.3）

## 定位纠正

OfferBomber 从"个人求职助手"改为**通用、可安装、多用户**的 AI 求职 Agent。
所有偏好集中在用户自建的 `JobSearchProfile`：

- 全字段可选；未设置 = 不施加约束，系统**不推断、不强加**任何职业/城市/学校/语言/工作类型默认值。
- 每个用户可建多个求职活动（多配置）。
- `config.example.yaml` 中原有的 `target_fields: [pretraining, quant, ...]` 个人偏好已清空；
  `companies` 默认空，不再写死任何公司。

## 里程碑

### M1 通用配置与招聘覆盖
- `search_profile.py`：`JobSearchProfile`（Pydantic，字段全可选）+ `SearchProfileService`（SQLite CRUD，按 user_id 硬隔离）。
- 数据源插件：
  - `discovery/official.py`：ATS 识别（Greenhouse/Lever/Ashby 公开板）+ 官网 JSON-LD `JobPosting` 解析。
  - `discovery/search_source.py`：配置的官方搜索 API，查询由 profile 维度生成，base_url 固定（模型无法注入任意 URL，防 SSRF）。
  - `discovery/custom_source.py`：用户本地 CSV/JSON 导入 + 公开招聘页/社区 URL（不绕过登录/验证码）。
  - `discovery/http.py`：`get_text()` + headers 支持，沿用超时/退避/重试。
- `core/company_discovery.py`：搜索查询 → 候选公司 → 招聘入口检测 → 仅保存可验证公司，不生成假岗位。
- `core/job_schema.py`：统一 `NormalizedJob`；薪资/地点/发布日期未知即留空，不臆测。
- `core/matching.py`：通用匹配——硬性条件（排除公司/地点/远程/工作类型/关键词）与软性语义重叠分离；未确认事实不参与；语义不匹配 ≠ 资格不合格。

### M2 申请准备与邮件投递
- `applications/email_provider.py`：`EmailProvider` 协议 + `GmailOAuthProvider`（真实 OAuth；凭证从环境变量指向的本地文件读取，绝不入库；无授权即 fail-closed）。收件地址正则校验。
- `applications/pipeline.py`：申请状态机（DISCOVERED→…→SUBMITTED→…）、幂等键、`ensure_application` 去重、草稿绑定已批准简历版本；**提交必须复用 Phase 3.3 SQLite 审批令牌**；模型调用 `approve_application` 本身不构成批准（需 CLI 带外批准）；Auto 默认关闭且需每日上限。

### M3 招聘反馈闭环
- `feedback/classifier.py`：中英文确定性分类（面试/笔试/拒绝/Offer/排期/补材料/确认收到/跟进/无关/未知）。
- `feedback/sync.py`：增量同步（provider_message_id 去重），线程 + 原始消息 ID + **公司且职位**联合关联（公司名单独不足以改状态）；无法关联进人工队列；面试时间时区解析（`GMT+8`/`Asia/Shanghai`/固定偏移，缺时区不猜测、标记待确认）。

### M4 调度与日报
- `scheduling/task_queue.py`：SQLite 持久化队列，条件 UPDATE 原子领取（并发只一个成功）、幂等入队、崩溃恢复、全局一键暂停（默认暂停）。
- `notifications/provider.py`：本地文件提供方（始终可用）+ 可选宿主连接器；通知落库。
- `reporting/daily.py`：Markdown + JSON 日报与统计，数字全部来自本地库。

### M5 MCP 扩展
- 工具从 14 增至 **36**，新增：search/create/update_search_profile、discover_companies、import_jobs、match_jobs、list_application_candidates、prepare/preview/approve_application、application_status、email_connect_status、inbox_sync、classify_recruitment_email、interviews_list、tasks_list、run_tasks、report_daily、statistics、pause_automation、set_automation_mode、system_health。
- 全部经 Python 权限层与新审批机制；工具分级 L0/L1/L2 标注在 docstring。
- toolkit 初始化幂等补齐 Phase 4-6 表（不改动已有数据；Alembic 仍是规范迁移路径）。

### M6 工程
- Alembic 新迁移 `a7c3f90b1d24`（search_profiles / email_accounts / email_templates / application_drafts / automation_states / discovery_runs）。
- CLI 新增 `campaign`（template/create/list）与 `agent`（match/report/pause/status）命令组。
- 文档（README / CAPABILITY_STATUS / config.example.yaml）状态统一，测试数以实跑为准。

## 验证结果

- `pytest -q`：**171 passed, 1 skipped**（原 139 + 新增 32；唯一跳过项为真实 Gmail 发送，需用户显式授权 `OFFERBOMBER_LIVE_GMAIL_TEST=1`，不以 Mock 冒充）。
- 迁移链：隔离临时库 `upgrade head` + `downgrade base` 均成功，7 张目标表齐全。
- 真实 MCP stdio：`tools/list` 发现 **36** 个工具；`system_health` 调用返回 ok=True（真实 JSON-RPC，非 Mock）。
- 隐私扫描：密钥、Token、个人简历、数据库、本地配置均未进入仓库。
- GitHub Actions：`.github/workflows/ci.yml` 已更新（Python 3.12、compileall、迁移含新表断言、无凭证 pytest）；实际云端运行结果需在 GitHub 侧查看。

## 明确未做真实联调（不虚报）

- 真实 Gmail OAuth 发送/收件：需用户提供凭证与明确授权；默认 fail-closed。
- WorkBuddy / DeepSeek Harness 宿主端到端：当前会话加载的是旧版工具，需重启会话后用 36 工具复验；本轮不声称完成真实宿主端到端。
- 搜索引擎/社区源真实联网：需用户配置官方搜索 API 与公开 URL；未提供时使用确定性失败，不伪造发现结果。

## 剩余限制与建议
1. 旧库若由 `create_all` 建成且缺新表，toolkit 会在启动时幂等补齐；规范做法仍是由用户择机 `alembic upgrade head`（不要强制覆盖用户数据）。
2. Auto 模式只做受控邮箱渠道；官网表单/ATS 提交记录为人工待办，不绕过验证码/反自动化。
3. 低风险自动回复模板尚未实现（保持人工处理），需后续按用户批准的模板与白名单开放。
