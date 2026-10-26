# AML Investigation Agent

在 IBM HI-Large v8（Azure VM 上由 Zetaris 提供的 1.8 亿笔交易）上运行的可解释反洗钱调查系统。三个 agent 协作，调查证据反哺规则：

```
规则（决策树，链路召回 ≥ 97%）→ 告警
  → 风险分级 agent：把告警排成高 / 中 / 低并给出优先级
  → 调查 agent：高级调查员分工 → 多名调查员并行追查上下游 → 合并 → 高级调查员研判（证据清单）
  → 规则更新 agent：从调查结果归纳并收紧规则、提出新特征 → 新规则版本（验证集门禁）
```

模型使用 NVIDIA Nemotron（Ultra 负责分级、研判与规则更新，Lightning 负责调查员）；Meterless 引擎负责调查循环（Markovian）、案件记忆（H-MEM）和多调查员共享的案件图（World Model）。调查员看不到标签；标签只在评估侧使用；测试集保持关闭。

## 在本地查看看板（不需要任何密钥）

仓库里提交了看板数据快照：`apps/dashboard/snapshot/data.json`（规则版本、已调查案件的命中规则、风险分级原因、关系网和调查回放、规则进化页面的数据）和 `alerts.sqlite.gz`（验证期全部 35 万条告警及其风险分级，首次启动时自动解压）。只需要 Python 3.11+：

```bash
git clone -b open-source git@github.com:YeSheng-Vibe-Coder/demo-repository.git && cd demo-repository
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m aml_agent.cli serve-dashboard
```

打开 <http://127.0.0.1:8020/dashboard>：

| 页面 | 内容 |
|---|---|
| 告警追查 | 验证期交易量、洗钱交易、告警量、链路召回；已调查告警列表（按风险等级排序，可按账户 / 日期 / 等级 / 规则筛选）。点一行打开详情：命中规则、风险分级原因、关系网追查（按调查员分色）、调查回放时间线 |
| 规则进化 | 告警 → 风险分级 → 调查 → 证据 → 学习 → 新规则的闭环；风险分级 agent 的学习轮次、调查 agent 的验证轮次与工具有效性、规则更新 agent 每个版本的决策与效果、规则版本记录 |
| 规则 | 当前规则（决策树 + 规则卡，含 AUSTRAC 原文与指标公式），可切换历史版本 |

首次启动需要约 20 秒（导入依赖），终端打印出地址后即可访问。左下角可切换中文 / English 与白天 / 夜间模式。端口被占用时加 `--port 8030`。

"立刻调查"（实时调查）需要你自己的 NVIDIA API key 和数据访问，见下一节；只读看板里点击时会提示如何配置。

## 完整运行与实时调查（使用你自己的 NVIDIA API key）

仓库里不包含任何密钥。实时调查、风险分级 agent 学习、规则更新等功能都要调用模型和查询数据，需要你绑定自己的账号：

1. 复制模板并填入自己的值：`cp .env.example .env.local`（`.env.local` 已被 Git 忽略，不要提交）。
   - `NVIDIA_API_KEY`：在 <https://build.nvidia.com> 注册后免费申请，所有 agent（调查员、高级调查员、风险分级、规则更新）都用它调用 Nemotron。
   - `ZETARIS_REST_URL` / `ZETARIS_API_KEY` / `ZETARIS_ORG_ID`：存放 IBM HI-Large 数据的 Zetaris 访问配置（例如通过 SSH 隧道转发到 `localhost:8888`）。
   - 可选 `AML_INVESTIGATOR_*`：把调查员换成自托管的 Nemotron Lightning（见 `deploy/colab`）。
   缺少配置时，点"立刻调查"会直接提示缺了哪一项。
2. 安装编排器依赖：Node.js ≥ 22.7（`services/orchestrator`，无需额外安装包即可运行）。
3. 启动：

```bash
export PYTHONPATH=services/aml-api
.venv/bin/python -m aml_agent.cli status          # 检查 Zetaris、各角色模型是否可达
.venv/bin/python -m aml_agent.cli serve           # API + 看板：http://127.0.0.1:8010/dashboard（含实时调查）
```

常用命令（都会访问 VM 数据或调用模型）：

```bash
.venv/bin/python -m aml_agent.cli learning-cycle --investigator-round val-r4 --split validation --per-cell 4   # 一轮验证期调查
.venv/bin/python -m aml_agent.cli learning-cycle --tier-agent --tier-patterns   # 风险分级 agent 学习并发布分级策略
.venv/bin/python -m aml_agent.cli learning-cycle --update-from dev-r3           # 规则更新 agent 依据调查结果更新规则
.venv/bin/python -m aml_agent.cli showcase-metrics                             # 重算看板用的验证期链路指标
.venv/bin/python -m aml_agent.cli alert-index --snapshot                       # 导出验证期全部告警（按当前主分级），并更新压缩快照
.venv/bin/python -m aml_agent.cli dashboard --snapshot                         # 导出看板数据，并更新仓库里的快照
```

测试：

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
node --experimental-transform-types --test services/orchestrator/worker.test.ts
```

## 目录

```text
apps/dashboard/              看板前端（纯 JS + SVG）；snapshot/ 为提交的数据快照
services/aml-api/aml_agent/  后端：API、CLI、调查工作流、三个 agent、Zetaris 工具、评估与看板数据
services/orchestrator/       TypeScript 编排：team.ts（调查团队）、worker.ts（单个调查员）
vendor/meterless/            固定版本的 Meterless 引擎（Markovian、H-MEM、World Model）与许可
docs/                        规范、运行指南、看板字段说明、规则版本记录；docs/archive 为历史方案
schemas/                     从 Pydantic 契约导出的 JSON Schema（测试保证与代码一致）
tests/                       契约、边界与流程测试
evaluations/                 冻结的时间划分（开发 / 验证 / 测试关闭 / 监控）
sources/                     政策来源快照与数据来源记录
analysis/                    早期实验结果与原始回执（按当时版本保留）
deploy/colab/                在 Colab GPU 上自托管 Nemotron Lightning 的 notebook
.runtime/                    私有运行状态、评估标签、导出数据（Git 忽略）
```

## 代码入口（`services/aml-api/aml_agent/`）

| 职责 | 模块 |
|---|---|
| API、CLI、状态账本 | `api.py`、`cli.py`、`store.py` |
| 看板数据与只读服务 | `dashboard.py`（导出）、`alert_trace.py`（告警追查与回放）、`showcase.py`（三个 agent 的推理与指标）、`dashboard_server.py`、`live.py`（实时调查） |
| 规则：DSL、学习、版本 | `rule_program.py`、`rule_learning.py`、`rule_versions.py`、`rule_changelog.py`、`chains.py`（洗钱链路与链路召回） |
| 风险分级 agent | `tier_agent.py`（策略学习与发布）、`tiering.py`（参考模型） |
| 调查 agent | `workflow.py`（调查循环、团队协调与合并）、`adjudication.py`（高级调查员研判）、`investigator_training.py`、`contracts.py`、`digest.py`、`candidates.py`、`harness.py`、`calibration.py` |
| 规则更新 agent 与闭环 | `learning_cycle.py`、`feature_proposals.py` |
| Zetaris 查询、特征与工具 | `zetaris.py`、`feature_store.py`、`feature_registry.py`、`tools.py`、`evidence.py` |
| 评估、模型与配置 | `evaluation.py`、`providers.py`、`profile.py`、`settings.py` |

## 更多文档

- [运行指南](docs/operations/RUNBOOK.md)：密钥、命令、特征库、调查方式与限制。
- [看板数据字段](docs/dashboard/DATA_FIELDS.md)：`data.json` 每个字段的含义。
- [规则版本记录](docs/rules/RULE_VERSIONS.md)：每个版本的来源、规则与效果。
- [技术规范](docs/architecture/AML_Agent_Technical_Spec_v1.0.md)、[执行 Harness](docs/architecture/EXECUTION_HARNESS.md)、[配置](docs/architecture/AML_Agent_Config_v1.0.json)（唯一参数来源）。
