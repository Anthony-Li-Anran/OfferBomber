# Phase 2 示例

所有示例都在项目根目录执行，并使用独立的演示数据库 `.offerbomber/demo.db`，不会污染正式库。

## 1. 准备演示库

```bash
# demo.yaml 复制自 config.example.yaml，仅把 db_path 改为 .offerbomber/demo.db
OFFERBOMBER_DB_URL="sqlite:///$(pwd)/.offerbomber/demo.db" alembic upgrade head
```

也可以直接 `offerbomber init --config examples/demo.yaml` 建库（配置文件已存在时需先移除或改用 `--force`）。

## 2. PDF 导入示例

`sample_resume.pdf` 是由 PyMuPDF 生成的示例简历，包含英文正文，便于确定性解析器识别。

```bash
PYTHONPATH=src python -m offerbomber profile import examples/sample_resume.pdf \
  --config examples/demo.yaml
```

输出包含 `import_id`、候选姓名/联系方式，以及按索引编号的候选经历与事实。此时**没有**事实写入正式经历库，只有一条 `PENDING` 的导入记录。

重复执行同一命令会返回 `duplicate=True`，不会重复解析生成第二条导入记录。

## 3. 确认导入

```bash
PYTHONPATH=src python -m offerbomber profile confirm 1 --all --config examples/demo.yaml
```

也可以逐项选择：

```bash
PYTHONPATH=src python -m offerbomber profile confirm 1 \
  --fields display_name,contact --experiences 0,1 --config examples/demo.yaml
```

确认后，被选中的事实状态为 `CONFIRMED`，每条事实都关联到 PDF 来源（文件名 + SHA-256 + 页行定位）。

## 4. 经历库查询示例

```bash
PYTHONPATH=src python -m offerbomber profile show --config examples/demo.yaml
PYTHONPATH=src python -m offerbomber experience list --config examples/demo.yaml
PYTHONPATH=src python -m offerbomber experience show 1 --config examples/demo.yaml
PYTHONPATH=src python -m offerbomber experience history 1 --config examples/demo.yaml
PYTHONPATH=src python -m offerbomber experience show-version 1 1 --config examples/demo.yaml
```

手工补充一条经历与事实（默认 `UNVERIFIED`，需显式确认）：

```bash
PYTHONPATH=src python -m offerbomber experience add PAPER "ICML Submission" \
  --fact "我的论文投了 ICML" --config examples/demo.yaml
PYTHONPATH=src python -m offerbomber fact verify 5 --config examples/demo.yaml
```

归档与恢复：

```bash
PYTHONPATH=src python -m offerbomber experience archive 1 --config examples/demo.yaml
PYTHONPATH=src python -m offerbomber experience list --include-archived --config examples/demo.yaml
PYTHONPATH=src python -m offerbomber experience restore 1 1 --config examples/demo.yaml
```

## 5. 检索示例

```bash
# 英文技能 + 项目关键词
PYTHONPATH=src python -m offerbomber search facts "distributed training" \
  --skill PyTorch --config examples/demo.yaml

# 中文技能 + 项目关键词
PYTHONPATH=src python -m offerbomber search facts "机器学习 预训练" \
  --skill PyTorch --direction ai_infra --config examples/demo.yaml

# 只看已确认事实
PYTHONPATH=src python -m offerbomber search facts "pretraining" --verified-only \
  --config examples/demo.yaml
```

返回每条匹配都包含 `experience_id`、`fact_id`、技能、来源与分数/理由。

## 6. 简历内容规划示例

```bash
PYTHONPATH=src python -m offerbomber resume plan "distributed training" \
  --skill PyTorch --job-id 42 --pages 2 --config examples/demo.yaml
```

`ResumePlan` 只包含 `CONFIRMED` 事实，文本逐字来自知识库，不含模型扩写；下一阶段由独立适配器转换为 Reactive Resume 格式。
