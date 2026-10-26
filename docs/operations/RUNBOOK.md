# 运行指南

## 1. 实现范围

在 IBM HI-Large（Zetaris 上的 1.8 亿笔交易）上运行完整研究闭环：

```
Zetaris 特征库（按天分片） → 规则学习 agent（决策树，开发集标签）→ 验证集全量比较 → 登记规则版本
  → 开发期全量扫描产生告警 → 调查团队（协调员 + 并行专项调查员 + 汇总员，World Model 共享案件图）
  → 调查结束后逐笔标签核验 → 规则学习 agent 更新 → 验证集比较 → 登记或暂缓 → 监控期回滚检查
```

调查员看不到标签；标签只在评估侧使用。规则最高为实验版本，不具备生产发布资格。

## 2. 配置

- Python 3.11+（项目 `.venv`），Node 22.7+。
- 根目录 `.env.local`（Git 忽略、权限 600）：`NVIDIA_API_KEY`、`ZETARIS_REST_URL`、`ZETARIS_API_KEY`、`ZETARIS_ORG_ID`。
- Zetaris：经 SSH 隧道连接 Azure VM（8888 REST、3000 网页），或在本机 Zetaris 平台装入本地数据包（README 方式 B，同样走 8888 REST 与同一套 SQL）。
- 所有参数来自 `docs/architecture/AML_Agent_Config_v1.0.json`（当前 `aml-config/2.0.0`）。
- 时间划分 `evaluations/split_plan.v2.json`：开发 08-04–09-03（30 天）、验证 09-05–09-26、测试 09-28–10-19（不打开、不物化）、监控 10-21–11-04；11-05 之后的发布方长尾排除。

下列命令均在项目根目录执行，前缀 `PYTHONPATH=services/aml-api .venv/bin/python -m aml_agent.cli`：

| 命令 | 作用 |
|---|---|
| `status` | 配置检查 + NVIDIA / Zetaris / 本地 API 实际连通性（不打印凭据） |
| `serve` | 启动业务 API（仅 127.0.0.1:8010） |
| `materialize-features` | 在 Zetaris 上按天物化特征与评估标签分片（开发、验证、监控），再构建各分区的洗钱链路表（评估用）和开发集总体分位数校准（`.runtime/calibration/development.json`） |
| `learning-cycle --fresh --cases 30 --parallel 3` | 完整闭环（`--fresh` 重新学习初始规则；每个开发日取最高优先级告警做团队调查）；`--resume CYCLE` 从已评分的调查续跑；`--rerun CYCLE` 用同一批告警重新调查并逐案对比 |
| `investigate CASE.json` / `run RUN_ID` | 单案调查（团队模式：协调员 + 并行专项调查员 + World Model 汇总员） |
| `show RUN_ID` / `trace RUN_ID` | 查看案件状态 / 执行时间线 |
| `export-schemas` | 从 Pydantic 契约重新导出 `schemas/` |
| `local-pack build` / `local-pack install` | 打包实时调查所需的数据切片（需先运行 `scripts/export_local_pack.sh`）/ 在本机 Zetaris 注册数据包、合并状态并核对行数 |

## 3. Zetaris 特征库

`materialize-features` 对每个窗口日写三个 parquet 分片（VM 路径 `customer-data/shared/ibm/aml_agent/`），注册为 Lightning 表：

| 表 | 内容 | 读取方 |
|---|---|---|
| `IBM_AML.agent_day_aggregates` | 每个活跃账户日：来源/去向方数、笔数、银行数、透传比例、币种数、活跃度变化 | 规则学习、扫描、调查员 |
| `IBM_AML.agent_day_transit` | 收后转出间隔、对数、回流、换币转出 | 同上 |
| `IBM_AML.agent_eval_account_day_label` | 账户日是否涉及洗钱交易 | 仅评估侧；调查员查询入口拒绝 |

特征只读截止前的数据（前一日 + 当日）。65 天约 2289 万账户日，物化约 1.5 小时。

## 4. 调查

- 规则告警案件在首个决策前由程序预取告警支持证据并登记前 5 个对手账户。
- 团队模式：协调员根据证据分 1–4 路（角色、问题、账户、工具、动作上限）；专项调查员并行，只用分配的工具；每个人的发现写入 World Model（官方参考实现，conformance 8/8）；汇总员读取合并后的案件图还原告警链路并判断每个候选。
- 调查员的输入是分段文本简报：程序生成的带证据引用的观察摘要、H-MEM 按未决问题召回的案件记忆、带 Zetaris 结构标签的交易行、对手特征画像、候选交易。
- 7 个工具各回答一个调查问题：告警依据、账户画像与对手分布、明细、历史基线（7/14/30 天）、两账户往来、多跳资金追踪（2–5 跳，换币与归集保留）、规则修改提议。

## 5. 硬限制与费用

每案（每个专项调查员）最多 18 次模型请求（含修复与重试）、12 个动作、3 次 Replan、24 次 SQL、1500 秒；单请求预留 24000 输入 / 4096 输出 tokens，按实际 usage 结算。输出语法所有字段有长度上限；引用类字段用有界格式并由程序校验成员；流式读取在出现空白失控时中断。剩余调用只够一次修复、动作用尽、重复动作两次无新证据时只允许 finish。连接失败 / 5xx / 429 各重试一次并计入预算。NVIDIA 免费端点账号额度未取得；Zetaris 使用现有 Azure VM。

## 6. 测试

```bash
.venv/bin/python -m pytest -q
node --experimental-transform-types --test services/orchestrator/worker.test.ts
WORLD_MODEL_IMPL=$PWD/vendor/meterless/world-model/src/index.ts node --experimental-transform-types vendor/meterless/world-model/conformance/runner.ts
```

## 7. 尚未完成

- 团队模式的协调员与汇总员输出仍常不合契约（会回退到单一调查员 / 失败），需要继续调试。
- 规则学习 agent 的特征与容量方案曾两次无效而使用兜底配置。
- 自然分布评估之外的场景覆盖、跨案件经验记忆、生产 RBAC 与部署。
- 测试分区未打开，最终效果尚未在测试集上评估。

## 模型分配与自部署（Colab GPU）

各 agent 的默认模型在配置 `model_roles` 中：普通调查员用 Nemotron 3.5 Lightning，高级调查员、分层 agent、规则更新 agent 用 Nemotron 3 Ultra，都走 NVIDIA API。`cli status` 会列出每个角色的模型和连通性。

普通调查员调用量最大，可以把它放到 Colab 上自部署的 Lightning，避开 NVIDIA 的限流：

1. 在 Colab 打开 `deploy/colab/serve_nemotron_vllm.ipynb`，运行时选 **G4 GPU**（或 A100），按顺序运行全部单元格。
2. 最后一个单元格打印 4 行 `AML_INVESTIGATOR_*` 配置，粘贴到本机 `.env.local`。
3. 重启 API（`cli serve`），用 `cli status` 确认 `investigator` 的 endpoint 显示为 `self_hosted`、`reachable` 为 200。

删除这 4 行即回到 NVIDIA API。Ultra 体量太大（4-bit 约 275 GB），Colab 无法部署。Colab 会话结束后服务停止，隧道地址也会变化。
