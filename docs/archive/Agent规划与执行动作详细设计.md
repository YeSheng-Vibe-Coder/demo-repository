# Agent 规划与执行动作详细设计

> 2026-10-22 更新：实现主方案已收敛为 [正式技术实现规范 v1.0](../architecture/AML_Agent_Technical_Spec_v1.0.md)。接口、预算、模型入口、数据划分与已实现状态以新文档为准；本文件保留历史设计。

2026-10-22。设计状态，尚未实现或宣称通过 Meterless conformance。沿用整体业务流程，补充案件调查与规则迭代两个循环。

## 1. 首版选择：轻量 Plan + 单步 Action/Observation + 按需 Replan

这里的 ReAct 指 Reason + Act，不是前端 React。采用其观察结果驱动下一步的方式，不要求模型输出内部思维过程。对用户记录简短选择依据、目标问题和证据引用。

不做动态规划算法、强化学习、模型权重训练或复杂多 Agent 编排。首版一个模型适配器、一个案件执行器，业务工具可以确定性测试。

初始 Plan：根据告警、规则和截止时间生成 2–4 个调查子目标，以及第一个动作。子目标是待解问题，不是必须顺序执行的脚本。
每轮：读取精确状态与相关记忆 → 选择一个动作 → 程序校验 → 执行工具 → 保存 Observation → 程序更新事实与预算 → 下一轮。
Replan：只在原计划受影响时替换未完成部分，保留历史计划和变更理由；普通新证据只更新当前步骤和候选分支。

Replan 触发：关键假设被否定；出现计划外的汇集、分发或回流分支；当前方向查询完整且没有新证据；结果截断需要改变查询范围；连续工具错误使计划不可执行；剩余预算不足以执行原计划。
这些触发并不总意味着调用 LLM：网络重试、固定预算缩减、分页续取等可以由程序处理。是否需要业务方向调整才交给 Planner。

## 2. 两个独立执行范围

案件内循环：只读交易与规则，写本案状态、证据和候选经验，不发布全局规则，也不改变本案固定规则版本和截止时间。
规则迭代循环：汇总开发案件，提出候选修改，在开发集搜索参数，独立验证后自动发布。新案件使用新版本，正在调查的案件继续原版本。
对比实验需要另建 case/run，不能原地覆盖旧轨迹。

## 3. 初始计划和单步动作协议

计划记录 plan_id、version、case_id、目标问题、子目标状态、候选分支、first_action、简短依据、支持证据 ID、预算和适用范围。
每轮决策结构固定为 action / replan / finish 之一；action 必须来自允许的工具清单。

```json
{
  "decision": "action",
  "plan_version": 1,
  "goal_id": "trace_destination",
  "action": "query_account_transactions",
  "arguments": {
    "account_ref": "已发现的账户引用",
    "direction": "outgoing",
    "window_ref": "本案允许的窗口引用"
  },
  "brief_reason": "需核实汇集资金是否继续向外分发",
  "evidence_ids": ["已持久化的交易引用"],
  "expected_observation": "后续付款及对手账户，或完整范围内无付款"
}
```

模型使用 account_ref、window_ref、evidence_ids，服务端解析真实银行、账户和时间范围；不接受模型修改 case_as_of、数据分区或权限。计划调整不等于全局规则修改。

## 4. 案件调查动作清单

以下全是本项目业务工具，不是声称 Meterless 或 Zetaris 已提供同名工具。

| 工具 | 输入范围 | 输出与用途 | 执行位置 |
|---|---|---|---|
| get_alert_context | 当前告警 ID | 命中规则、版本、触发交易、窗口、截止时间 | 业务库及 Zetaris |
| summarize_account | 已见银行加账户、允许窗口 | 按方向和币种分别统计次数、金额、对手数 | Zetaris 固定 SQL |
| query_account_transactions | 已见账户、incoming/outgoing/both、窗口 | 上游或下游明细、稳定交易 ID、范围完整性 | Zetaris 固定 SQL |
| compare_account_baseline | 账户、当前窗口、截止前历史窗口 | 频次、对手数、同币种金额的变化；历史不足明确返回 | Zetaris 和程序 |
| query_between_accounts | 两个已见账户、允许窗口 | 两账户直接或反向交易，核对往返和平行交易 | Zetaris 固定 SQL |
| check_structure | 已持久化证据 ID、结构类型 | 时序多跳、汇集分发、往返或候选循环及核验边界 | 确定性图算法 |
| fetch_more | 当前 query_id 和有效续取引用 | 后续结果；仅在实例真实接口支持时启用 | Adapter |
| propose_rule_change | 当前规则版本、案件证据、修改假设 | 持久化规则候选建议；不改生产规则 | 规则建议收集器 |
| finish_case | 事实、证据 ID、未知项、结束原因 | 程序核查后形成案件结论 | 业务服务与 LLM |

基础状态和 H-MEM 的读取、Observation 保存、预算扣减、日志写入由框架自动进行，不作为每次都让 LLM 选择的工具。
首版可先实现 get_alert_context、summarize_account、query_account_transactions、check_structure、finish_case；其余按业务样例补齐，不一次实现所有能力。

### 扩展规则

动作粒度是一个有业务意义的问题，不是一个 SQL 关键字，也不是一次查完整个网络。
扩大调查账户必须来自已观察交易；扩大窗口不得越过截止时间。跨多个账户的工具内部批处理需显式限制账户数并逐个报告完整性。
默认不提供任意 execute_sql、shell、DROP 或数据库权限修改工具。
同银行加同账户作为账户键，保留字符串前导零。金额按币种和收付侧统计。
同一分钟的时间戳只能给出部分顺序，不能伪造秒级先后；交易相连或金额相近不证明同一资金。

## 5. Tool Router 与 Observation

调用前检查 JSON schema、动作权限、引用归属、窗口、预算、重复查询范围和查询版本。随后编译参数化或严格编码的固定 SQL；禁止字符串直接拼接模型原文。
每次查询保存查询快照、模板版本、参数、时间范围、耗时、证据和结果是否完整。所有证据写入成功后才允许交给模型使用。

```json
{
  "tool_status": "ok",
  "query_id": "本次查询标识",
  "scope_ref": "具体账户方向和窗口",
  "complete": false,
  "rows_returned": 100,
  "new_evidence_ids": ["真实证据引用"],
  "new_account_refs": ["真实已见账户引用"],
  "continuation_ref": "服务端续取引用或null",
  "verified_findings": [],
  "error_code": null,
  "elapsed_ms": 0
}
```

以上数值是协议示例，不是实测。字段由程序填写；若 complete=false 且无可用续取能力，必须缩小窗口或按确定边界拆分并避免漏行，不能假装完整。
Observation 分为 ok / partial / empty / error；empty 仅用于完整范围内确实无结果。工具错误不变成业务事实。
程序核实数量、边连接、时间和 ID；模型基于核实结果解释其业务意义，不能自行宣告循环成立或交易属于洗钱。

## 6. 每轮模型调用与停止

首版初始 Plan 一次模型调用；每轮把最新 Observation 和状态一并交给 Planner，用一次调用完成观察解释及下一动作选择。没有必要固定安排一次 Reason 和一次 Replan 双调用。
结束后另一次报告生成，程序核验交易引用、政策来源和未知条件。出现结构不合法的模型输出只允许有限次纠正。

停止状态区分 completed、insufficient_evidence、budget_exhausted、tool_failed、cancelled。
LLM 可建议结束，但程序可拒绝引用不存在、把 partial 当完整或必答问题尚未标为未知的结束请求。
预算和停止条件是上限，不是“必须每个方向都查一遍”。初始开发配置可试 8 个调查动作、最多 2 次语义 Replan，每次查询 30 秒墙钟目标、最多一次传输重试；这些不是经过验证的最终业务阈值。
逻辑动作、实际 HTTP 请求、SQL 执行、重试、返回行、模型调用和真实供应商 tokens 分开计数。有限动作数不自动限制所有底层成本。
适配器超时后查询可能仍在服务端运行，应记录不确定状态并查询或取消，不能无条件重发。

## 7. Meterless 的真实边界

核对的官方参考接口：Markovian.run({stepFn, stopWhen, memoryContext, abortSignal, reflect})，构造配置包含 chunkConfig 和 historyDir；HMEM.add、query、feedback，构造配置含 persistDir。

Markovian 提供有界 chunk 循环、carryover、进度、运行历史和结束标记。stepFn 内由我们实现 Planner → Tool Router → Zetaris → Observation → 状态更新。不能只把 LLM 原文返回给引擎，就假定它自动执行 SQL 或理解反洗钱业务。
参考源码的 stopWhen 检查在一个 chunk 完成后发生，因此执行 SQL 前还要单独检查预算和取消状态。
参考实现默认有最后 reflection；首版设 reflect=false，由单独报告步骤汇总，避免 stepFn 在 reflection 中被再次调用而误执行调查工具。
historyDir 保存运行记录不等于业务的事务检查点或 exactly-once 恢复，必须由我们保存案件与动作执行状态。
memoryContext 默认主要用于初始步骤；每轮需要的精确状态与 H-MEM 召回由 stepFn 主动读取，不假设记忆自动逐轮注入。
引擎的 chars/4 是估算值，模型费用使用供应商实测 usage。

H-MEM 提供记忆存取、召回、来源追踪和冲突处理的参考能力，不负责判断金融事实是否正确。我们指定哪些 Observation 可转成事实，哪些只能存为假设，以及本案、运行、分区和规则版本的隔离。
完整证据、游标、预算和动作状态在业务数据库；H-MEM 存摘要及证据引用。首版不启用 Dreaming、自动推广或后台合成作为规则发布通道，也不声称该记忆能力在训练策略模型。
业务状态和 memory_outbox 同事务写入；异步写记忆并按 outbox ID 去重。H-MEM 不可用时保留证据并标记记忆待同步。

官方参考：
- https://github.com/Meterless/Meterless/blob/main/engines/markovian/reference/src/types.ts
- https://github.com/Meterless/Meterless/blob/main/engines/markovian/reference/src/orchestrator.ts
- https://github.com/Meterless/Meterless/blob/main/engines/hmem/reference/src/index.ts

## 8. 规则迭代动作，不混进案件工具权限

| 动作 | 调用者 | 内容 |
|---|---|---|
| summarize_rule_outcomes | 批次工作流 | 汇总开发案件、查询完整性、覆盖和预算 |
| propose_rule_revision | LLM | 提出阈值、特征、权重、AND/OR组合或新增规则的可检验假设 |
| compile_candidate | 程序 | 检查字段、政策引用、特征定义和参数范围，生成版本化 DSL/SQL |
| evaluate_candidates | 程序 | 开发集候选搜索，消融对比，隔离标签评分 |
| validate_candidate | 固定验收工作流 | 有限验证集验收，不据验证结果反复微调到通过 |
| promote_or_reject | 发布程序 | 达到冻结标准自动发布，否则保留旧版本及拒绝原因 |

候选建议必须说明支持与反例、哪些案件完整调查、预期改善、可能扩大告警的代价，不以 H-MEM 的 confidence 作为规则效果分数。
程序搜索阈值和权重属于参数校准，不是 reinforcement learning。规则发布采用项目既定自动校验与评估，不设置人工审批步骤。
调查身份不读取标签；离线评估使用单独身份。管理员 API key 的替换与真实字段权限限制仍是待实现工作，视图去掉标签本身不足以完成隔离。

## 9. 实施与验收

第一条真实链路：输入银行加账户及截止时间 → 生成简短计划 → 查收付 → 根据 Observation 选择上游或下游 → 核验结构 → 结束并保存证据；接入 Markovian 和案件状态存储。
第二条链路：上述流程引入 H-MEM 摘要召回，程序重启后恢复，不重复已完成动作，不混入其他案件或未来数据。
第三条链路：从多个开发案件产生一个规则修改候选，程序编译和比较，通过预设验证标准后自动发布下一版本；不得仅演示写入一段规则文本。

验收覆盖：不同反馈导致不同下一动作；空结果、partial、错误、重复和预算分别处理；参数与未来数据限制；恢复后的幂等性；记忆来源及隔离；新旧规则及回滚；官方 Meterless conformance；真实模型和真实 Zetaris 小范围联调。mock 测试与真实业务联调分别报告。
