# AML Investigation Agent

在 IBM HI-Large v8（Azure VM 上由 Zetaris 提供的 1.8 亿笔交易）上运行的可解释反洗钱调查系统。三个 agent 协作，调查证据反哺规则：

```
规则（决策树，链路召回 ≥ 97%）→ 告警
  → 风险分级 agent：把告警排成高 / 中 / 低并给出优先级
  → 调查 agent：高级调查员分工 → 多名调查员并行追查上下游 → 合并 → 高级调查员研判（证据清单）
  → 规则更新 agent：从调查结果归纳并收紧规则、提出新特征 → 新规则版本（验证集门禁）
```

模型使用 NVIDIA Nemotron（Ultra 负责分级、研判与规则更新，Lightning 负责调查员）；Meterless 引擎负责调查循环（Markovian）、案件记忆（H-MEM）和多调查员共享的案件图（World Model）。调查员看不到标签；标签只在评估侧使用；测试集保持关闭。

数据：IBM AML 合成交易数据集 HI-Large v8（[Kaggle](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml)，CDLA-Sharing-1.0）。

## 先看你要做什么

| 你想… | 需要什么 | 看哪一节 |
|---|---|---|
| 浏览看板：规则、告警追查（已调查案件的关系网与回放）、规则进化 | 只要 Python，不需要任何密钥或数据 | [1. 只看看板](#1-只看看板不需要任何密钥) |
| 在看板上点"立刻调查"，让调查团队实时调查一条告警 | 你自己的 NVIDIA API key + 能查询数据的 Zetaris（二选一） | [2. 实时调查](#2-实时调查立刻调查) |
| 重新学习规则、风险分级、重算指标 | NVIDIA key + 团队 VM 上的 1.8 亿笔全量数据 | [3. 学习与重算](#3-学习与重算需要团队-vm-全量数据) |

所有数据查询都通过 Zetaris（本项目赞助方）的 REST SQL 完成。实时调查的数据有两种来源，查询逻辑完全相同：

- **方式 A：直连团队 VM 上的 Zetaris**。不用下载数据，需要向项目负责人要 VM 访问和 Zetaris API key。
- **方式 B：在自己电脑上运行 Zetaris，装入本地数据包**。数据包约 0.9 GB，包含验证期的 1350 万笔交易和特征。不依赖 VM，但需要 Docker（8 GB 内存）。

## 1. 只看看板（不需要任何密钥）

仓库里提交了看板数据快照：`apps/dashboard/snapshot/data.json` 是规则版本、已调查案件和规则进化数据，`alerts.sqlite.gz` 是验证期全部 35 万条告警及其风险分级，首次启动时自动解压。只需要 Python 3.11+：

```bash
git clone -b open-source git@github.com:YeSheng-Vibe-Coder/demo-repository.git aml-agent && cd aml-agent
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m aml_agent.cli serve-dashboard
```

终端打印出地址后（首次约 20 秒），打开 <http://127.0.0.1:8020/dashboard>。端口被占用时加 `--port 8030`。左下角可切换中文 / English 与白天 / 夜间模式。

| 页面 | 内容 |
|---|---|
| 告警追查 | 验证期交易量、洗钱交易、告警量、链路召回；全部告警列表（按风险等级排序，可按账户 / 日期 / 等级 / 规则 / 调查状态筛选）。点一行打开详情：命中规则、分级依据、调查结论与账户判定、关系网追查（按调查员分色）、调查回放时间线 |
| 规则进化 | 告警 → 风险分级 → 调查 → 证据 → 学习 → 新规则的闭环；三个 agent 每一轮的决策与效果 |
| 规则 | 当前规则（决策树 + 规则卡，含 AUSTRAC 原文与指标公式），可切换历史版本 |

这个只读看板里点"立刻调查"，会提示需要先完成第 2 节的配置。

## 2. 实时调查（"立刻调查"）

### 2.1 共同准备（A、B 都要做）

1. 完成第 1 节的安装（clone、`.venv`、`pip install -e .`）。
2. 安装 **Node.js 22.7 或更新版本**（调查团队的编排器 `services/orchestrator` 用它运行，不需要 `npm install`）。
3. 申请 **NVIDIA API key**：在 <https://build.nvidia.com> 注册后免费申请。调查员、高级调查员、风险分级 agent、规则更新 agent 都用它调用 Nemotron。
4. 复制配置模板：`cp .env.example .env.local`，填入 `NVIDIA_API_KEY`。Zetaris 的三项按下面 A 或 B 填写。`.env.local` 已被 Git 忽略，**不要提交，也不要发到群里**。

### 2.2 方式 A：直连团队 VM

适合不想下载数据、能拿到 VM 访问的成员。

1. 向项目负责人要两样东西（私下发送，不要写进仓库）：
   - **VM 的 SSH 访问**：把你的 SSH 公钥（`~/.ssh/id_ed25519.pub`）发给负责人，由他加到 VM 上。负责人会告诉你用户名和地址。
   - **Zetaris API key 和 Org ID**。
2. 打开 SSH 隧道，把 VM 上的 Zetaris REST 端口转发到本机 8888，并保持这个终端开着：

   ```bash
   ssh -N -L 8888:localhost:8888 <用户名>@<VM 地址>
   ```

3. 在 `.env.local` 里填写：

   ```bash
   ZETARIS_REST_URL=http://localhost:8888/api/v1.0
   ZETARIS_API_KEY=<负责人给你的 key>
   ZETARIS_ORG_ID=<负责人给你的 Org ID>
   ```

4. 继续 [2.4 启动](#24-启动并调查)。

### 2.3 方式 B：本机 Zetaris + 本地数据包

适合想完全在自己电脑上运行的成员。数据包只覆盖实时调查需要的部分：

- 验证期（2022-09-05 至 09-25）告警涉及的交易，加上前 30 天历史（基线比较和新对手方检查要回看 30 天）；
- 共 1350 万笔 ACH / Wire 交易，**不含洗钱标签列**；
- 这些天的特征分片，以及调查需要的规则版本、分级策略、校准数据。

装好后，本机 Zetaris 里的表名、路径和 VM 完全一样（`aml.transactions`、`IBM_AML.agent_day_*`），代码不做任何切换。

**第 1 步：安装 Zetaris 平台。** 从 Zetaris Cloud 的下载页（黑客松账号）下载 `zetaris-platform.zip`，按压缩包里的 README 操作：

```bash
unzip zetaris-platform.zip && cd zetaris-platform
docker login zetarishackathon.azurecr.io   # 粘贴下载页给出的登录命令
./preflight.sh                             # 检查 Docker 内存（需 8 GB）等
docker compose pull
docker compose up -d                       # 首次启动 5-10 分钟
./verify.sh                                # 9 项检查全部通过即可
```

**第 2 步：下载数据包，放进 Zetaris 的数据卷。** 回到本项目目录：

```bash
mkdir -p .runtime/local-data
curl -L -o .runtime/local-data/aml-local-pack.tar \
  https://github.com/YeSheng-Vibe-Coder/demo-repository/releases/download/local-pack-v1/aml-local-pack.tar
tar -xf .runtime/local-data/aml-local-pack.tar -C .runtime/local-data

# 复制到 Zetaris 的 customer-data 卷（容器内路径 /home/zetaris/customer-data/shared/ibm），并交给 Zetaris 的用户
docker run --rm -v zetaris_customer-data:/data -v "$PWD/.runtime/local-data/aml-local-pack/ibm:/pack:ro" alpine \
  sh -c 'mkdir -p /data/shared/ibm && cp -R /pack/. /data/shared/ibm/ && chown -R 1001:1001 /data/shared/ibm'
```

**第 3 步：拿到本机 Zetaris 的 API key。** 打开 <http://localhost:3000>，用 `admin@zetaris.com` / `Zetaris1@` 登录（建议随后修改密码），在网页里生成一个 API key，并记下 Org ID。然后在 `.env.local` 里填写：

```bash
ZETARIS_REST_URL=http://localhost:8888/api/v1.0
ZETARIS_API_KEY=<刚生成的 key>
ZETARIS_ORG_ID=<你的 Org ID>
```

**第 4 步：注册数据并核对。**

```bash
.venv/bin/python -m aml_agent.cli local-pack install
```

这一步会做三件事：

- 在 Zetaris 里注册 `AML.transactions` 和三张 `IBM_AML.agent_day_*` 特征表（Lightning FILESTORE，Parquet）；
- 把调查需要的状态合并进 `.runtime/`；
- 用 Zetaris 查询行数，与数据包里记录的 VM 结果对比。

输出里 `"verified": true` 就表示数据和 VM 一致（13,505,048 笔交易）。可以重复运行。

### 2.4 启动并调查

```bash
.venv/bin/python -m aml_agent.cli status   # 检查 NVIDIA 各角色模型和 Zetaris 是否可达（不打印密钥）
.venv/bin/python -m aml_agent.cli serve    # API + 看板
```

打开 <http://127.0.0.1:8010/dashboard>（注意端口是 **8010**，不是只读看板的 8020）。在"告警追查"里点任意一条验证期告警，再点"立刻调查"。详情里会实时显示进度：

- 准备阶段：预取证据、高级调查员分工，约 1-2 分钟；
- 各调查员的每一步；
- 最后合并结论和高级调查员研判。

一次调查通常需要十分钟左右，同时最多运行两个。失败会自动重试最多 2 次，并在列表里标出原因。关闭详情后，列表状态会显示为"调查中 / 已调查 / 失败"。

### 2.5 常见问题

| 现象 | 原因与处理 |
|---|---|
| 点"立刻调查"提示缺少配置 | `.env.local` 里缺 `NVIDIA_API_KEY` 或 `ZETARIS_API_KEY`；改完后重启 `serve` |
| `status` 里 `zetaris_datasources` 不是 200 | 方式 A：隧道没开或已断开，重新运行 `ssh -N -L ...`。方式 B：Zetaris 没启动好，运行 `./verify.sh` |
| `local-pack install` 报 `verified: false` | 第 2 步的复制没完成或路径不对，重新执行 `docker run ... cp` 那一步，再运行 install |
| 本机查询很慢 | 给 Docker 至少 8 GB 内存；参考 Zetaris README 的 "Tuning the query engine" |
| 调查显示"失败"，原因是模型调用出错 | 多为 NVIDIA 免费额度限流或网络问题；系统会自动重试，也可以稍后再点一次 |

## 3. 学习与重算（需要团队 VM 全量数据）

下面的命令需要完整的 1.8 亿笔数据和评估标签，只能用方式 A 连接团队 VM：

```bash
export PYTHONPATH=services/aml-api
.venv/bin/python -m aml_agent.cli learning-cycle --investigator-round val-r4 --split validation --per-cell 4   # 一轮验证期调查
.venv/bin/python -m aml_agent.cli learning-cycle --tier-agent --tier-patterns   # 风险分级 agent 学习并发布分级策略
.venv/bin/python -m aml_agent.cli learning-cycle --update-from dev-r3           # 规则更新 agent 依据调查结果更新规则
.venv/bin/python -m aml_agent.cli showcase-metrics                             # 重算看板用的验证期链路指标
.venv/bin/python -m aml_agent.cli alert-index --snapshot                       # 导出验证期全部告警（按当前主分级），并更新压缩快照
.venv/bin/python -m aml_agent.cli dashboard --snapshot                         # 导出看板数据，并更新仓库里的快照
```

重新制作本地数据包（项目负责人）：

```bash
AML_VM_SSH="ssh -i <私钥> <用户名>@<VM 地址>" scripts/export_local_pack.sh   # 从 VM 导出交易和特征分片
.venv/bin/python -m aml_agent.cli local-pack build                            # 打包，并从 VM 的 Zetaris 记录核对用的行数
gh release upload local-pack-v1 .runtime/local-data/aml-local-pack.tar --clobber
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
scripts/                     维护脚本：从 VM 导出本地数据包
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
| Zetaris 查询、特征与工具 | `zetaris.py`、`feature_store.py`、`feature_registry.py`、`tools.py`、`evidence.py`、`local_pack.py`（本地数据包） |
| 评估、模型与配置 | `evaluation.py`、`providers.py`、`profile.py`、`settings.py` |

## 更多文档

- [运行指南](docs/operations/RUNBOOK.md)：密钥、命令、特征库、调查方式与限制。
- [看板数据字段](docs/dashboard/DATA_FIELDS.md)：`data.json` 每个字段的含义。
- [规则版本记录](docs/rules/RULE_VERSIONS.md)：每个版本的来源、规则与效果。
- [技术规范](docs/architecture/AML_Agent_Technical_Spec_v1.0.md)、[执行 Harness](docs/architecture/EXECUTION_HARNESS.md)、[配置](docs/architecture/AML_Agent_Config_v1.0.json)（唯一参数来源）。
