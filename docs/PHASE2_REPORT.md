# Phase 2 完成情况报告

日期：2026-11-19
范围：Part A（Phase 1 审查完善）+ Part B（个人经历知识库）+ Part C（测试）+ Part D（交付）

## 一、实际实现的功能

### Part A — Phase 1 修复

| 项 | 处理结果 |
|---|---|
| A1 | 完整阅读 README、配置、模型、迁移、三个采集器、去重、CLI 与测试；修改前基线 `16 passed` |
| A2 | LICENSE 实为 Apache 2.0，`pyproject.toml` 由 MIT 改为 Apache-2.0 并加入对应 classifier |
| A3 | `normalize_key` 改为 Unicode 安全：NFKC + casefold + 标点分隔 + 保留中日韩字符 + 中文内部空白折叠；仓储层复用同一函数 |
| A4 | 采集器解析异常统一转 `SourceFetchError`；单条记录用 SAVEPOINT 隔离；每个数据源单独提交；失败记录日志并落 `last_error`；新增 `sources_partial`/`record_errors`/`fully_succeeded` |
| A5 | 新增 `CAPABILITY_STATUS` 能力清单；配置模板标注 `[NOT IMPLEMENTED]`；`doctor`/`status` 明确输出未实现项 |

未为了代码风格重构任何已正常工作的模块。

### Part B — Phase 2

- 数据层：复用并扩展 `profiles`、`educations`、`experiences`、`experience_facts`、`experience_sources`、`experience_versions`、`resume_versions`；新增 `resume_imports` 与 FTS5 虚表；新增 Alembic 迁移 `48bbf6632460`。
- PDF 导入：`PyMuPDFExtractor` 文本抽取 → `DeterministicResumeParser` 结构识别 → 候选 DTO → 导入审核表 → 人工确认写入。重复文件按 `(user_id, sha256)` 识别；扫描版明确提示 OCR 限制。
- 经历管理：`profile show/import/confirm`、`experience list/show/add/edit/history/show-version/restore/archive/unarchive`、`fact add/verify/delete`。
- 版本管理：每次变更生成不可变快照；恢复产生新版本，不改写历史。
- 事实检索：结构化标签 + FTS5 BM25 + 关键词打分；中文用「整段 + 二元组」索引与分词回退；预留 `Reranker` 接口。
- 简历规划：`ResumePlan` Pydantic 模型与候选生成服务；仅使用 `CONFIRMED` 事实，文本逐字保留，携带来源 ID/定位/SHA-256。

## 二、测试结果

修改前基线：`16 passed`。

修改后最终：`PYTHONPATH=src pytest tests/ -q` → **35 passed**。

新增测试覆盖任务书 Part C 的 15 项要求：

1. 中文公司与职位名称去重（含不同中文公司不合并、英文能力保留）
2. 异常招聘源隔离（畸形响应不影响其他源）
3. PDF 正文提取（真实生成的 PDF）
4. 重复 PDF 导入
5. 创建完整经历
6. 向经历增加事实片段
7. 修改和查看经历历史
8. 归档经历（及恢复）
9. 检索中文技能与项目
10. 检索英文技能与项目
11. 按岗位要求排序经历
12. 未确认事实不能进入简历规划（含「投了 ICML」不得变成「Accepted Paper」）
13. 修改经历不影响历史版本
14. 数据库迁移可在空库执行至 head
15. 程序重启后数据仍然存在

另附：单条坏记录隔离、能力未实现标注、事实来源存在性、无效经历类型拒绝等测试。

真实命令行联调（隔离演示库）：PDF 导入预览 → 确认 4 条经历/4 条事实 → `profile show` → `search facts "distributed training" --skill PyTorch` 返回带来源与 BM25 理由的匹配 → `resume plan` 输出含 `experience_id`/`fact_id` 的计划。

## 三、需要外部模型或账号才能验证

- 大模型驱动的非结构化简历解析、事实归类与改写：本轮未接通宿主模型，使用确定性解析器；不具备语义理解能力。
- 扫描版 PDF 的 OCR：需要 OCR 服务，本轮明确拒绝而非假装成功。
- 语义嵌入、向量检索与模型重排序：接口已预留，未接入。
- Reactive Resume 实际渲染与账号：本轮只产出中立的 `ResumePlan`，未调用外部服务。

## 四、留到 Phase 3 的功能

- 模型适配层（宿主模型/独立 API）与调用审计。
- `ResumePlan` → Reactive Resume 数据结构的独立适配器与 PDF 生成。
- 岗位与经历的语义匹配、模型重排序。
- 邮件、投递、反馈、MCP Server、定时调度（本轮未提前开发）。

## 五、备注

- 数据库迁移是增量式的：在已有 864 条 Phase 1 岗位数据的库上执行 `alembic upgrade head` 成功，历史数据保留。
- `offerbomber init` 现在会同时把数据库升级到 head，避免「配置已初始化但表不存在」。
- 功能状态以 `offerbomber status` / `doctor` 输出为准，配置文件中未实现项均显式标注。
