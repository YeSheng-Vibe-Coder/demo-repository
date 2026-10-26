# 交易风险调查 Agent 技术实现规范

本文按“总体方案 → 数据基础 → 整体流程 → 分阶段实现 → 支撑机制 → 接口附录”组织。第 1–3 节说明系统如何运行，第 4 节定义三个业务阶段的实现，第 5–8 节规定上下文、评估、状态及验收，第 9 节集中列出开发接口。

主线实现差异见 [业务流程对照](IMPLEMENTATION_ALIGNMENT_AUDIT.md)。

实际实现、启动方法及尚未通过的验收见 [运行指南](../operations/RUNBOOK.md)。下述流程为设计目标；实现状态不能仅从设计表推断。

当前验证状态见第 8.1 节，实施任务见第 8.2 节。本文定义目标实现，不表示全部功能已完成；配置文件中的参数仍需应用实现执行与校验。

## 1. 总体方案

系统从可追溯的 AUSTRAC 指标生成可计算规则草案，利用 Zetaris 计算开发特征，在隔离环境校准并验证初版规则包。规则执行产生告警后，由 NVIDIA 模型驱动案件规划、动作选择和 Replan，通过 Zetaris 逐步取证。开发案件可触发规则修订；独立 Evaluator 决定候选接受、暂缓或拒绝。

首版交付为可复现的研究/实验系统。规则接受状态为 `accept_for_experiment`，不等同生产部署资格。全局规则版本、模型/提示词版本、数据快照与评估样本必须可追溯。

### 1.1 固定技术选型

| 层 | 选型 | 实施要求 |
|---|---|---|
| 数据与特征执行 | Azure VM 上已有 Zetaris；Lightning SQL / REST | 复用已注册 HI-Large；聚合、窗口及有界关系连接优先在 Zetaris 执行 |
| 模型服务入口 | NVIDIA 官方托管 API，应用直接调用 | 固定模型；无自动选模、额外 judge/debate 或付费回退 |
| 默认模型 | NVIDIA `nvidia/nemotron-3.5-lightning-30b-a3b` | 使用固定 NVIDIA 模型 ID 调用；核对返回模型与实际 usage |
| 深度模型 | NVIDIA `nvidia/nemotron-3.5-super-vl-120b-a12b` | 可选；通过能力与预算验收后启用 |
| 执行循环 | Meterless Markovian 官方参考实现 | 固定 commit；复用 stepFn、carryover、轮数与停止机制 |
| 记忆 | Meterless H-MEM 官方参考实现 | 固定 commit；业务服务实施案件/时间/样本隔离 |
| 编排 | TypeScript / Node.js | 接入官方 Markovian/H-MEM；业务 API 内统一执行模型适配与 Pydantic 契约校验 |
| 业务 API 与计算适配 | Python / FastAPI / Pydantic | 复用现有查询/评估代码；固定模板、证据、状态、规则编译 |
| 状态存储 | SQLite WAL，Python 服务唯一写入 | 以事务保存证据、动作、版本、预算；不引入消息中间件 |
| 展示 | Web 界面或首轮 CLI | 仅调用业务 API；不持有外部服务密钥 |

首版采用定向样本学习与策略版本优化，暂不使用强化学习或模型参数训练；不使用任意模型生成 SQL、全表无限深度图遍历或自动付费回退。其他模型 provider 不属于 v1.0 必须实现范围。

## 2. 数据基础与分析能力

### 2.1 调查字段

| 字段 | 类型/语义 |
|---|---|
| transaction_id | 稳定 bigint，一基原始行号；与 snapshot 组合引用 |
| transaction_time | 时间戳；当前数据到分钟，不推断真实秒序 |
| from_bank/from_account | 付款银行/账号字符串，保留前导零 |
| to_bank/to_account | 收款银行/账号字符串，保留前导零 |
| amount_received/receiving_currency | 收款金额 Decimal(38,8) 及币种 |
| amount_paid/payment_currency | 付款金额 Decimal(38,8) 及币种 |
| payment_format | 支付格式字符串 |

`is_laundering`、发布方模式答案和回顾性选样信息仅进入评估域。账号≠客户身份，币种/银行≠地理位置，时序连接≠同一资金。

调查身份必须无法读取标签。现有管理员 key 的无标签视图仅实现开发程序隔离；数据接入必须验收最小权限。平台不能实现所需隔离时，使用无标签物理来源和独立凭据，评估进程保留原始来源访问。

### 2.2 计算职责

| 需求 | 首选执行位置 | 首版范围 |
|---|---|---|
| 次数、金额、distinct 对手数、首末时间 | Zetaris GROUP BY/COUNT/SUM/MIN/MAX | 按银行+账户、方向、窗口及币种定义口径 |
| 历史变化、交易间隔 | Zetaris 窗口/历史查询 | LAG 已验证；其他表达式逐项兼容性验收 |
| 汇集/分发 | Zetaris 聚合 | 保留不同对手与交易引用 |
| A→B→C、固定深度回流候选 | Zetaris 有界自连接 | 严格递增时间、不同交易 ID、显式深度/账户限制 |
| 固定特征准备和质量流程 | Zetaris Pipeline | Projection/Join/Aggregation/Custom SQL；先验证具体节点/接口 |
| 平台未覆盖的复杂图算法 | 项目确定性算法 | 仅处理已授权的有界证据子图；不得另开本地 Spark 冒充 Zetaris |

Zetaris 原生任意深度图遍历、社区发现和最短路接口未确认，不纳入首版承诺。可视 Pipeline 的计算依赖图不作为账户交易图使用。SQL 能力验证不替代 GUI Pipeline 或全量性能验收。

### 2.3 特征注册表

每个特征登记 feature_id/version、类型、单位、方向、币种策略、缺失语义、required_history、模板版本、data_as_of、代码版本和验证样例。历史未覆盖返回 insufficient_history，不填零。跨币种金额不求和；跨币种对手数仅在规则声明该口径时计算。

先执行按账户/窗口的完整聚合并缓存。派生账户日特征按 snapshot/feature_version/closed_at 管理：案件截止到中午时，不可使用当天全天聚合。允许完整历史桶加当前部分桶实时计算。新增物化表、Pipeline 或源数据重排由数据工作包实施并验证，不作为模型运行时任意动作。

## 3. 整体业务流程

系统按三个阶段运行：初版规则形成可执行告警入口；案件调查围绕证据逐轮推进；跨案件规则更新在独立评估后形成新版本。

### 3.1 初版规则生成与告警

输入为 AUSTRAC 来源、IBM 字段和已支持特征模板。NVIDIA 生成规则草案，程序编译，Zetaris 计算特征，隔离程序校准并验证，只有通过的实验规则包才产生告警。

```mermaid
flowchart TD
    A[官方段落与字段/能力注册] --> B[Initial Rule Builder 直接调用 NVIDIA Nemotron]
    B --> C[RuleDraft 来源和字段校验]
    C --> D[固定模板编译]
    D --> E[Zetaris 开发特征]
    E --> F[开发参数校准]
    F --> G[冻结候选]
    G --> H[独立验证集 Evaluator]
    H -->|accept_for_experiment| I[Registry 初版实验规则包]
    H -->|hold 或 reject| J[保留草案及原因]
    I --> K[Zetaris 执行规则并产生告警]
    K --> L[案件 Planner]
```

输出为版本化 RulePackage、告警及支持证据。字段、阈值和校准细则见第 4.1 节。

### 3.2 案件调查循环

Planner 使用 NVIDIA Nemotron，统一由业务 API 的 NVIDIA Adapter 直接调用。每轮根据 Observation 选择查询、分析、Replan 或结束；Zetaris 执行查询及可计算分析，Markovian/H-MEM 承担执行循环、上下文承接与记忆。

```mermaid
flowchart TD
    AGENT_CASE_CONTEXT[告警、规则与案件上下文] --> AGENT_CASE_PLANNER[Planner：Plan / Action / Replan]
    AGENT_CASE_PLANNER -->|action| AGENT_CASE_ROUTER[工具校验与预算检查]
    AGENT_CASE_ROUTER --> AGENT_CASE_QUERY[Zetaris 数据查询]
    AGENT_CASE_QUERY --> AGENT_CASE_ANALYSIS[特征与结构分析]
    AGENT_CASE_ANALYSIS --> AGENT_CASE_OBSERVATION[Observation 与证据保存]
    AGENT_CASE_OBSERVATION --> AGENT_CASE_PLANNER
    AGENT_CASE_OBSERVATION --> AGENT_CASE_MEMORY[H-MEM 记忆]
    AGENT_CASE_MEMORY --> AGENT_CASE_PLANNER
    AGENT_CASE_PLANNER -->|finish| AGENT_CASE_REPORT[调查样本 ID、案件特征与未知项]
    AGENT_CASE_REPORT --> AGENT_CASE_LABELS[独立程序：疑似交易标签校验]
    AGENT_CASE_LABELS --> AGENT_CASE_AGG[开发案件特征与反例归纳]
    AGENT_CASE_AGG --> AGENT_CASE_REVISION[进入规则修订与规则 Evaluator]
```

输出为带证据、反证、未知项和停止原因的案件结论。Planner 协议、工具与单步执行见第 4.2 节。

### 3.3 跨案件规则更新

开发案件触发修改建议，经过编译、Zetaris 回放、开发校准和独立验证后决定接受、暂缓或拒绝。规则模型只提出建议，Evaluator 独立决定是否允许注册实验版本。

```mermaid
flowchart TD
    RULE_UPDATE_CASES[开发案件、证据与当前规则] --> RULE_UPDATE_MODEL[Rule Proposer：NVIDIA Adapter → Nemotron]
    RULE_UPDATE_MODEL --> RULE_UPDATE_DRAFT[规则修改建议与支持反证]
    RULE_UPDATE_DRAFT --> RULE_UPDATE_COMPILE[程序校验与固定模板编译]
    RULE_UPDATE_COMPILE --> RULE_UPDATE_FEATURES[Zetaris 特征重算与规则回放]
    RULE_UPDATE_FEATURES --> RULE_UPDATE_CALIBRATE[开发集参数校准]
    RULE_UPDATE_CALIBRATE --> RULE_UPDATE_FREEZE[冻结候选]
    RULE_UPDATE_FREEZE --> RULE_UPDATE_EVAL[独立 Evaluator：验证集]
    RULE_UPDATE_EVAL -->|accept_for_experiment| RULE_UPDATE_REGISTRY[注册新实验版本]
    RULE_UPDATE_EVAL -->|hold 或 reject| RULE_UPDATE_KEEP[保留旧版本与评估原因]
    RULE_UPDATE_REGISTRY --> RULE_UPDATE_NEW[新案件使用新版本]
    RULE_UPDATE_REGISTRY --> RULE_UPDATE_ROLLBACK[监测与自动回滚]
```

输出为新实验版本或保留旧版本的评估记录。规则修改范围、版本固定及回滚细则见第 4.3 节。

## 4. 分阶段实现细节

本节定义第 3 节三个流程的输入、输出、约束和执行细节。模型调用统一遵守附录 9.1，业务端点与数据契约统一遵守附录 9.2。

### 4.1 初版规则生成

Initial Rule Builder 直接调用 NVIDIA Nemotron，输入为已登记的政策段落、字段字典、特征/模板能力和规则 schema，输出 RuleDraft。模型不读标签；规则编译、参数校准与独立验证依次执行，未完成校准的草案不可产生正式告警。

#### 4.1.1 Policy Registry 与候选指标

Policy Registry 为每个段落登记 source_ref、官方 URL、章节/段落定位、取得日期、原文快照及 SHA256、适用行业、版本和有效性检查。模型生成的政策主张必须回指可解引用段落。

当前 IBM 指标映射如下。编号是项目映射，不是法规条款号；行业前提缺失时只能覆盖可观测行为。正式实现以实际原文逐段登记和字段校验为准。

| 项目映射 | 行为家族 | 主要边界 |
|---|---|---|
| IBM-MAP-R01 | 快速/多账户转移 | 复杂程度为项目定义 |
| IBM-MAP-R02 | 接收后转发 | 不证明同一资金 |
| IBM-MAP-R03 | 多来源汇集 | 缺客户及行业身份 |
| IBM-MAP-R04 | 短时大额入账 | 阈值需校准；缺业务身份 |
| IBM-MAP-R05 | 模式/频次异常 | 需要历史基准 |
| IBM-MAP-R06 | 金额/频次异常 | 需要同口径历史及业务背景 |
| IBM-MAP-R07 | 快速往返 | 无法判断经济理由 |
| IBM-MAP-R08 | 活动突变 | 无法解释变化原因 |
| IBM-MAP-R09 | 小额聚集/疑似拆分 | 无法确认规避意图或原始大额 |
| IBM-MAP-R10 | 观察静默后活跃 | 无法证明真实长期休眠 |
| IBM-MAP-R11 | 多笔小额付款 | 缺保险业务与意图 |
| IBM-MAP-R12 | 转出后部分返还 | 缺虚拟资产业务，不能证明同一资金 |
| IBM-MAP-R13 | 整额形态偏离历史 | 仅数值及历史异常 |
| IBM-MAP-R14 | 多个账户汇入 | 不证明不同实际所有人 |

共享家族可合并特征，政策引用仍分别保留，禁止重复加权。PaySim 历史规则的 R01 等编号不迁移为 IBM 同名规则。来源与规则均使用数据集/命名空间。

#### 4.1.2 RuleDraft / RulePackage

RuleDraft 必填：schema_version、rule_id、dataset、source_refs、policy_indicator_refs、behavior_family、template_id/version、field_bindings、feature_ids、expression、parameters、parameter_basis、scope、self_transfer_policy、currency_policy、required_history、missing_context、coverage、status、support_evidence_spec。

`coverage` 为 full/partial/unsupported/needs_history。参数依据为 policy_explicit/project_constraint/needs_calibration。policy_explicit 必须有原文数值引用；needs_calibration 的值为 null，不允许执行。缺字段或未知模板返回 unsupported/feature_request，不生成任意代码绕过限制。

Rule Compiler / Registry 对已支持草案生成固定模板执行计划。参数校准完成后冻结为 RulePackage，包含 source/feature/template/compiler/config/parameter 版本与 SHA、开发/验证 manifest、评估记录和作用域。

#### 4.1.3 首批模板与校准

首批模板为 fan_in、fan_out、receive_then_send。初始实验范围为 ACH/Wire、排除自转账、严格后续时间与不同交易 ID；支付格式属于项目约束，不是 AUSTRAC 规定。模板与指标关联由 Policy Registry/Initial Rule Builder/Rule Compiler / Registry 核验，不宣称覆盖全部指标。

- fan_in/out：窗口 24/48 小时、distinct 对手数 3/5/10，各六个配置，加停用各七种。
- receive_then_send：最大间隔 6/24/48 小时，加停用四种。
- 合并参数组合最多 `7×7×4=196`。这是开发初始搜索空间，不是已校准最优参数。

特征与结构分析工具在开发范围内优先批量计算可复用统计；独立 Evaluator 派生并比较组合，禁止默认发起 196 次全表扫描。校准只使用开发集；标签仅由隔离 calibration worker 读取，不进入规则模型提示词。

告警按焦点账户/观察日合并，保留所有触发模板及支持边。完整历史不满足所需窗口则不可评估。开发处理预算初值为每日最多 50 个合并告警；如果只评估抽样账户，该预算仅对样本成立。满足预算下优先开发正例覆盖，再按 precision/查询负担作预先定义的并列决胜。

初版独立验证与预登记简单基线和停用方案比较。数据不足或无可接受规则时保持 needs_calibration/hold，不发布可执行包。首版通过后注册 experiment_v1，再生成规则告警。人工固定 baseline 和显式账户调查必须单独标记。

### 4.2 案件调查

#### 4.2.1 Planner 决策契约

Planner 输入 EvidencePacket、冻结规则、当前问题及状态、候选交易、允许工具、账户/窗口引用和剩余预算。每次调用只输出一个决策；拿到结果后再决定下一步，不每轮重写计划。

决策公共字段：schema_version、decision、based_on_state_version、plan_version、brief_reason、evidence_refs、question_updates。

| decision | 附加字段 | 行为 |
|---|---|---|
| action | initial_plan（仅首次，2–3 个问题）、next_action | 选择一个动作，next_action.question_id 指明服务的问题 |
| replan | reason、previous_plan_version、plan_patch（≤3 个问题）、next_action（必填） | 仅在重大变化时改写未完成问题，计一次 Replan |
| finish | conclusion、candidate_decisions、suspect_transaction_ids、supported_findings、counterevidence、unresolved_questions、stop_reason | 程序校验后结束，不再执行查询 |

**初始 Plan**：两三个要回答的问题，不是工具清单。典型为：资金来自哪里、是否多来源汇集；汇入后去了哪里、是否继续分发或回流；目前证据支持什么、与历史相比有何变化、还有哪些未知。问题按 1..n 编号，状态为 open / answered / unanswerable；answered 必须引用证据，unanswerable 只用于完整范围无结果或超出授权范围。

**动作**：next_action 含 tool、arguments、expected_observation、question_id。expected_observation 说明缺失事实以及不同结果如何改变下一步。参数必须满足工具专用 schema，引用必须属于本 run；禁止任意 SQL、shell、改写截止时间或读取标签。

**Replan 触发条件**（reason 必填，程序按最新观察校验，不满足则拒绝）：

| reason | 程序校验 |
|---|---|
| hypothesis_refuted | evidence_refs 引用最新观察 |
| new_branch | 最新观察登记了新账户或返回了路线 |
| direction_exhausted | 最新观察为空、重复或可证明为空 |
| budget_low | 剩余模型调用 ≤5 或剩余动作 ≤3 |

另需：两次 Replan 之间有新观察，最多 3 次，Replan 必须同时给出下一个动作。

**候选交易**：程序根据 Zetaris 结构结果计算候选（规则支持边、路线、金额保留 0.8–1.05、6 小时内快速转移、归集、回到起点），排序权重未校准，不读标签。finish 必须对每个候选给出 confirm / exclude / unknown、理由和证据引用；confirm 的交易必须在 suspect_transaction_ids 中，后者可再包含其他有证据的交易，也可为空。

**Finish 条件**：所有问题已回答或无法回答；或剩余动作不可能改变任何候选判断；或程序强制 finish（剩余调用只够一次修复、动作预算用尽、达到 maxChunks 前一轮、重复动作两次无新证据）。保护性情形不再直接终止案件，而是要求模型基于已有证据 finish。程序只检查数据契约、引用和作用域；案件结束不调用 LLM 业务审批。疑似交易 ID 与独立标签逐笔对照，标签不回传给正在调查的 Planner。

**分工**：程序约束能做什么（前置条件、预算、重复、短路、强制 finish），提示与示范引导该做什么，模型决定做哪一个。程序不编码固定调查路径。

#### 4.2.2 工具清单

一个工具回答一个调查问题，不把每个 SQL 都做成工具。分页、网络重试和可证明为空的范围由程序处理。

| 工具 | 回答的问题 | 输入 | 执行 |
|---|---|---|---|
| get_alert_context | 为什么告警，命中了哪个规则版本？ | 无（规则告警由程序在首个决策前预取） | Zetaris 取回截止前 48 小时焦点账户明细，按触发条件给出支持对手与交易 ID；收后转出条件另做 through 追踪；登记前 5 个支持对手 |
| summarize_account | 收付规模、频次、对手分布？ | account_refs（1–5）、window_ref | Zetaris 单次扫描：按账户/方向/币种精确汇总加前 5 对手；登记对手 |
| query_account_transactions | 窗口内谁付款给它、它付款给谁？ | account_ref、window_ref、direction | Zetaris 固定 SQL，程序固定 200 行上限；重复同一查询由程序返回下一页 |
| compare_account_baseline | 与此前历史相比有何变化？ | account_ref、direction | Zetaris 同宽度当前/历史聚合，或 insufficient_history |
| query_between_accounts | 两个账户之间是否有直接转账或往返？ | 两个已登记 account_ref、window_ref | Zetaris 有界查询 |
| check_structure | 资金从哪来、去了哪、是否汇集/分发/回流？ | account_ref、window_ref、direction（upstream/downstream/through）、max_gap_hours（6/24/48）、depth（2–5） | Zetaris 按跳迭代：每跳同币种、严格时序、间隔上限、金额连续（下游 0.05–1.05 倍，上游 0.95–20 倍），每笔保留金额最接近的 3 条，最多 60 个节点；程序逐跳复核；返回路线、金额保留比例、归集账户、回到起点；完整证据已证明该方向无交易时不发 SQL |
| propose_rule_change | 证据是否支持一个未批准的规则修改假设？ | 冻结 base_version、证据、类型化变更 | 业务 API 候选队列，不发布 |

实时模式下所有追踪不超过案件截止时间；回顾模式（允许查看截止之后的交易）未实现，启用前需单独评估。小额聚集、整额偏离和历史异常用对应特征工具，不能用图连接替代。

#### 4.2.3 单步执行与恢复

编排器在 Markovian stepFn 内执行：读取并核对 state_version → 检查预算/取消 → H-MEM 授权检索 → 业务 API 构造业务证据 → 模型适配器调模型 → schema 校验 → 业务 API 事务化接收决策并至多执行一个业务动作 → Case Validator 校验 → 保存 Observation/预算/版本 → 返回 checkpoint。

Markovian 负责通用循环、carryover、最大 chunk 和停止；业务 API 负责精确状态、证据、作用域与幂等。H-MEM 写入通过 outbox，失败不重跑已完成查询。恢复时先检查动作状态；同 action_id 不重复执行。原子 lease/fencing 防止过期编排器提交。

case 结束状态：completed、insufficient_evidence、budget_exhausted、tool_failed、model_unavailable、cancelled。completed 仅表示调查流程结束，不是确认洗钱。模型不可用时不将固定程序结果标记成 AI 调查。

### 4.3 跨案件规则更新

#### 4.3.1 修改建议输入与输出

Rule Proposer 仅读取开发案件及其证据摘要，输出 base_version、operation、policy_refs、支持/反例、表达式差异、预期收益、负担和未知项。案件模型的 propose_rule_change 仅保存提议。

#### 4.3.2 可修改范围

当前 RuleProgram 支持阈值、已登记 24/48 小时特征、已有条件新增/停用，以及 all/any/weighted 分组。五个基础特征为 fan_in_24h/48h、fan_out_24h/48h、transit_gap_hours；派生特征只允许同单位输入的 sum/difference/ratio，零分母返回未知。限制 8 条规则、4 组、3 个派生特征；新增规则须新 ID、有效 P02/P03 引用，停用保留父版本记录。未登记 extractor 或任意执行代码一律拒绝。

#### 4.3.3 编译、评估与实验版本

修订路径：提议 → Rule Compiler / Registry 编译与适配检查 → 独立 Evaluator 开发校准 → 冻结 → 独立验证 → 自动接受/暂缓/拒绝 → Registry 实验包。bootstrap 不带 base_version，revision 必须带；不能覆盖已冻结来源解释。接受版本只影响新案件，旧案件继续原包。

#### 4.3.4 版本固定与回滚

版本登记保存父版本、差异、评估和回滚目标。上线实验前冻结告警量/资源错误/性能的回滚阈值；触发后自动回指旧包并保留新版本记录。未配置可验证回滚门槛时不得开启自动实验发布。

## 5. 上下文、记忆与运行预算

### 5.1 证据数据路径

Zetaris 原始数据 → 完整授权聚合/有界关系计算 → 业务证据存储 → 本轮 EvidencePacket。交易全集不输入模型，不对全表 embedding 代替精确交易查询。

Packet 必须包含 run/state/version、as_of、允许范围、当前规则与问题、最新 Observation、完整性、支持和反证、frontier 引用、未知项、剩余预算及检索记忆来源。

### 5.2 展示与查询完整性

默认每轮最多展示 30 条相关交易、15 个节点、25 条摘要边，仍受 token 预算约束。这些为可版本化展示参数，不是免费 API 权益或模型容量。优先触发边、边界、支持/反证和模型明确索取的证据，记录 selection_reason、rows_shown 与 omissions。

完整聚合的真实总量不从展示样本推算。源查询、实际取回明细和本轮显示范围分别记录；未展示的明细可由重复同一查询分页读取（程序处理，不发新 SQL）。若明细取回不足或窗口未覆盖，必须保留 partial/unknown，不将其转成完整否定证据。

### 5.3 能力归属

| 内容 | 直接复用 | 项目适配 |
|---|---|---|
| 跨轮压缩、carryover、轮数 | Markovian | 短 checkpoint 与事实引用，不重写通用摘要循环 |
| 记忆检索 | H-MEM | tenant/case/split/as_of/版本过滤 |
| 模型服务 | NVIDIA 官方托管 API | 固定模型、账号权限核验与真实用量；不使用 Nightshift 路由 |
| 交易相关性、范围与完整性 | Zetaris + 业务 API | 业务 EvidencePacket 与反证选择 |
| 真实调用用量与案件预算 | 可用 provider/Runtime usage | 预扣/结算、SQL/图/时间边界，未知用量保守记账 |

业务库保存精确事实；carryover/H-MEM 只用于上下文导航和记忆。模型摘要不覆盖金额、时间、证据覆盖和待办状态。默认不启用模型 compressor；启用后所有压缩调用经模型适配器 预算接口并计入该 run。

### 5.4 Token、查询与运行预算

配套 JSON 为配置唯一参数源；以下数值为 v1.0 开发 profile，变更须记录 config_version。模型/网关/账号限制更严格时取更低上限。

| 维度 | 默认值 |
|---|---:|
| 单请求输入目标 / 硬限 | 8000 / 24000 tokens |
| 单请求总输出含可计量推理 | 2048 tokens；可见 JSON 目标 800 |
| 单案件实际模型 HTTP 请求（含修复/重试/模型压缩） | 10 |
| 单案件输入 / 输出总限 | 120000 / 20480 tokens |
| 业务工具动作 / Replan | 8 / 2 |
| 物理 SQL 请求（含重试与分片） | 16 |
| 模型 / SQL 并发 | 各 1 |
| 单明细保留 / overflow 探针 | 200 / 201 行 |
| 历史 / 图深度 / 案件账户 | 截止前 7 天 / 3 / 20 |
| 单次新增账户 | 5 |
| 单案件派发截止 | 720 秒；派发前预留请求 timeout |
| 模型 / SQL 客户端等待上限 | 各 180 秒 |
| SQL 正常延迟目标 | 30 秒；不表示服务端取消 |
| 单动作重试 / 单输出修复 | 各最多 1 次，仍受全局限额 |
| 案件 Markovian | chunkSize=32000，maxChunks=10，carryoverTokens=1024，overlapTokens=0，reflect=false |
| 初版构建模型请求 / maxChunks | 各 12；每批最多 4 条政策指标 |
| 初版/修订参数组合 | 196 |
| 初版/修订批任务 SQL / 墙钟 / 样本窗口上限 | 64 次 / 1800 秒 / 10000 个账户窗口，必须提供冻结 manifest |
| 规则修订模型请求总上限 | 12 次（含修复/压缩）；base_version 必填 |
| worker lease / 续约间隔 | 420 / 30 秒；提交必须携带 fencing token |
| 可选模型 reviewer | 默认关闭；若启用另设至多 2 次请求预算 |
| 付费回退 / fusion | 关闭 |

输入计数使用对应 tokenizer；不可得时采用保守估算并标 estimated，实际 usage 标 measured。最终请求包装后再检查，不能用字符数除四当准确计费。超过软限先去重、使用官方 carryover、减少展示与记忆；必须保留截止时间、权限、完整性、反证和未知项。仍超硬限则不发请求，返回 context_budget_exceeded。

调用前事务预扣最坏额度；返回后结算。缺 usage 不记零。401/403 不重试；429 按 Retry-After 且受剩余时限限制；超时核查 tool/model call 状态后再重试。达到派发截止不派新请求，在途查询另列 outstanding。

NVIDIA 公开页面当前提供免费原型端点，但账号资格、配额、Nightshift 权益必须实测。规则更新批任务使用独立 run ledger，不借用案件剩余额度；重用每请求 profile 和统一预算 adapter。调用成本按实际 provider/model/usage 单独计量，基础设施费用单列。

## 6. 独立校验与效果评估

### 6.1 调查样本校验与程序完整性

案件调查不插入 LLM 业务评审门禁。程序只检查引用、交易 ID、真实方向、时间范围、完整性、权限、幂等和预算，保持 Planner 自主调查。

调查结束后，在独立评估侧对明确疑似 transaction_id 子集逐笔查询数据集标签，区分真阳性、假阳性、未知/缺失。不能用账户窗口选样标签替代逐笔标签，也不能把所有检索交易自动计为疑似交易。标签不进入进行中的调查上下文；开发结果用于后续特征归纳和规则修订，验证/测试标签不用于开发调参。

Rule Evaluator 放在规则更新阶段，评估旧规则与候选规则的差异、逻辑、适配性和独立回放指标；模型评审不能取代标签指标。案件表述质量的诊断可单独离线运行，但不属于主线审批。

### 6.2 样本与指标

划分调试回顾性样本、开发集、冻结验证集、最终测试集。调试正例按标签选样时不得报告总体效果；已经查看的样本不得重新当作独立验证。候选与评价准则在查看验证结果前固定；反复针对验证优化需新的保留批次。

时间切分遵守 data_as_of，检查关联账户/网络组件跨集泄漏。必要时采用账户/组件隔离和 purge/embargo；不满足独立性时披露并按关联组件重采样。人为平衡的正负样本不直接估计总体 precision；分层样本保留采样概率与权重。

指标分开：规则账户窗口/支持交易 precision-recall；调查证据或已知链路覆盖；Planner 有效动作、非法参数/引用、事实性、停止质量；物理查询、延迟、token 和费用。只有存在可对齐真值时计算完整链路指标。

等告警预算比较规则，等工具/时间预算比较调查。无正例时 recall=null；样本不足、证据不全或区间过宽返回 hold。不能用账户某笔标签阳性证明模型已定位该笔或重建完整链路。

### 6.3 自动实验版本准则

硬门槛：schema/来源/字段/模板合法，无标签与未来泄漏，币种/缺失语义正确，证据覆盖合格。实验初值：验证至少 100 个账户窗口和 10 个正例；完整证据；等预算下真阳性提高且 precision 不降低。此门槛不构成生产资格；正式发布前仍须确定统计区间、场景覆盖、性能与回归标准。

输出 `accept_for_experiment` / `hold` / `reject`，附 metrics、scope、completeness、failure_codes、版本与 manifest。缺证据为 hold；违反硬约束为 reject。接受后 Rule Compiler / Registry 自动注册实验包；首版 production_eligible=false。该流程不设置人工审批步骤。

### 6.4 Planner 定向样本学习与策略改进闭环

首版采用上下文中的定向少样本学习，基础模型权重不变，不执行强化学习。模型保留自主推理、Plan、Action、Observation 和 Replan 的职责；经验用于说明证据判断原则和常见错误，不定义固定动作序列。未来只有在轨迹质量、规模和稳定错误类型足够时，才评估 SFT；托管推理 API 不代表具备训练权限或免费训练额度。

闭环：**记录调查动作与结果 → 独立评分 → 归纳失败原因 → 生成候选策略 → 固定样本验证 → 发布或拒绝 → 新调查记录**。仅将记忆写入数据库不会自动改进 Planner。

| 环节 | 输入与输出 | 能力与实现边界 |
|---|---|---|
| 轨迹记录 | 可观察状态、动作、Observation、最终结论、证据/查询覆盖、成本；固定模型/提示/工具/数据版本 | 项目 Store/Tracing + Markovian 历史；不记录密钥或隐藏推理 |
| 独立评分 | 疑似样本逐笔标签结果、取证覆盖、重复请求、成本及后续规则改进 | 独立标签评估程序；规则 Evaluator 位于规则更新阶段，案件诊断不作为主线审批 |
| 失败归因与定向样本 | 从开发轨迹提炼事实误读、动作选错、工具返回缺口等类型，形成正反例 | 项目样本整理/反思步骤；去除具体账户标签与未来信息；区分工具问题和规划问题 |
| 候选策略 | 版本化提示、少样本示例、经验检索与工具说明 | 项目策略优化器；H-MEM 存取经验，不自动训练模型；不允许修改权限、真值或发布门禁 |
| 固定对照验证 | 候选与当前策略在相同样本、预算下比较 | 项目 Eval Harness；支持接口回归，改变查询动作的策略需要重新执行，不能仅靠旧轨迹回放证明效果 |
| 版本发布与回滚 | 满足预先冻结条件才注册实验策略版本 | 项目策略注册；新版本仅用于新案件，失败保留原版本；不得自行覆盖生产策略 |

第一批样本重点覆盖：完整单笔结果与截断的区别；本账户未见出账不等于对手账户没有活动；相同动作被阻止不等于所有查询被禁止；调查未知分支与合理停止的选择。样本可以展示不同有效方案，不要求所有案件走相同路径。

训练/经验提炼仅使用 development，validation 只做冻结候选选择，test 保留最终验收。标签只进入隔离评分，不进入当前案 EvidencePacket；经验按时间、数据分区、策略版本和来源管理。限制对验证集的反复适配，不将已调试样本重新作为独立验收。

验收同时比较事实性、证据引用、合理弃答、有效动作、重复率及 SQL/Token 成本；不能只奖励少查询、完成状态或可疑标签命中。只有存在对应真值时才报告规则/调查效果，单个案例改善不代表总体改善。

**当前状态：**逐笔标签校验、跨案归纳、实际 base_version 对照的独立 Rule Evaluator、实验登记与监控检查已完成一轮真实研究验证。15 告警中调查 3 案，2 案有效、1 案循环保护排除；100 个富集验证窗口中 TP1→2、FP12→9，已登记实验版本。正例调查、定向消除误报和总体效果尚未验收；定向样例未推广，自动经验提炼/情境检索/策略晋级仍待实施。详见 [实现对照](IMPLEMENTATION_ALIGNMENT_AUDIT.md)。

## 7. 状态、版本与权限

业务 API 最少实现 sources、feature_registry、rule_drafts、rule_packages、alerts、cases、plans、case_steps、tool_runs、evidence、observations、model_calls、budget_ledger、jobs、memory_outbox。评估标签及 evaluation_results 保存在隔离访问域，展示结果经去标签接口发布。

事务提交顺序必须保证：已返回给模型的证据已持久化；state_version 单调增加；动作、预算与 observation 同步提交；规则发布和当前版本指针原子更新。H-MEM/Markovian history 不作为唯一事实源或事务日志。

每个 run 固定规则版本；新版本只用于新案件。日志保存 SQL 模板/参数、source/version/hash、模型 route/底层 model/usage、prompt/schema/config hash、action/query ID、错误/取消和回执。禁记密钥、原始 connect link 与不可访问标签。

## 8. 实施与验收

### 8.1 当前验证状态与复用入口

| 能力 | 实际入口 | 已验证范围 |
|---|---|---|
| 模型与数据 | aml_agent.providers / zetaris | NVIDIA 真实 API、Azure VM 上 IBM 数据及 Zetaris 只读查询 |
| 案件循环 | workflow.py；services/orchestrator/worker.ts | 官方 Markovian/H-MEM、Plan/Action/Replan、执行保护与预算 |
| 规则闭环 | rule_cycle.py / rule_updates.py / rule_program.py | 真实 15 告警→3 调查→2 案归纳→独立规则评估→实验登记 |
| 逐笔标签 | cohorts.TransactionLabelReader | 模型结束后按真实交易 ID 对齐，不将标签暴露给调查模型 |
| 验证与回滚 | tests/test_rule_cycle.py；analysis/README.md | 100 个富集验证窗口的实际父版本比较；回滚退化分支单测 |

未完成：自然分布/新账户验证、稳定正例调查、完整政策特征覆盖、后台监控调度、生产权限强隔离与生产部署。本轮起点是研究参考规则，不是已获批的自动生成初版。测试集未打开，重复使用的富集验证不作为最终盲测。

### 8.2 实施任务与集成顺序

#### 8.2.1 实施任务

| 任务 | 计划交付路径 | 依赖 | 验收关键项 |
|---|---|---|---|
| WP01 契约与存储 | `schemas/`、`services/aml-api/` migrations、版本注册 | 本规范 | schema/状态机、事务、409/幂等/lease 测试 |
| WP02 模型与 Runtime | `services/orchestrator/provider/` | 账号与 Nightshift；WP01 预算接口 | models/chat、错误认证、结构化输出、真实 Nemotron 回执与用量 |
| WP03 Zetaris 工具 | `services/aml-api/zetaris/`、`sql/features/`、`sql/tools/` | 现有实例；WP01 | 聚合/明细/历史、完整性、scope、无标签、查询性能 |
| WP04 政策与初版规则 | `policies/`、`prompts/rule-builder/`、`services/aml-api/rules/`、`schemas/rule-draft.schema.json` | WP01/02/03 | 来源逐段映射、参数空位、缺字段/行业限制、编译验证 |
| WP05 调查 Planner | `prompts/planner/`、`schemas/planner-decision.schema.json`、`services/orchestrator/planner/` | WP01/02/03 | 初始问题、不同观察产生不同动作、Replan、合法终止 |
| WP06 Meterless 集成 | `services/orchestrator/engines/`、受控官方源码锁 | WP01/02/05 | 唯一外层循环、H-MEM 过滤、outbox、恢复、reflect=false、conformance |
| WP07 校准与评估 | `evaluations/`、manifest、metrics、calibration worker | WP01/03/04 | dev 校准、冻结验证、标签隔离、统计/预算口径、hold/reject |
| WP08 规则闭环 | `services/aml-api/registry/`、`prompts/rule-revision/`、rollback | WP04/06/07 | base_version、候选差异、自动实验版本与回滚 |
| WP09 展示与回执 | `apps/dashboard/` 或 CLI | WP01；逐步接入其余 | 来源→告警→Plan→证据→评估全链路，状态真实 |

以上路径为目标实现位置；除第 8.1 节列出的文件外，不代表已经存在。各实现任务可以并行准备，但不得更改公共 schema、端点或状态字段而不更新契约和版本。首轮不启动模型多角色并发，代码分工不改变运行并发限制。

#### 8.2.2 集成顺序

1. 冻结 WP01 的 schema、内部 API 和配置；以 mock 对接模块。
2. 验证 WP02 的真实模型连接；完成 WP03 的无标签工具。
3. WP04 → WP07 完成初版构建/校准/独立验证；未通过时保留草案。
4. WP05 → WP06 完成真实模型动态调查；只允许已验收工具。
5. 按冻结规则产生告警；没有可用规则时可运行明确标记的手动账户调查，不替代自动告警验收。
6. 独立保留样本比较 baseline 与模型调查，记录质量、延迟、token 与预算。
7. WP08 验证接受和拒绝/暂缓分支及回滚；WP09 展示完整回执。

#### 8.2.3 必须提供的验收材料

每个工作包提交 commit、接口/schema/config 版本、测试命令与结果、mock/live/replay 类型、官方能力调用位置、未完成项和失败码。模型相关工作附底层模型回执；SQL 相关工作附模板、scope、完整性与耗时。

关键场景：来源缺失、未知字段/特征、阈值未校准、partial/empty/error 区分、展示裁剪后续取证、未来范围/未知账户拒绝、同分钟交易不伪造顺序、重复动作/崩溃恢复、预算耗尽、模型不可用、标签不可访问、降噪但漏正例的候选 hold/reject、版本固定与自动回滚。

### 8.3 展示与运行回执

展示初版规则来源/适配/参数状态，告警来源，案件问题与 plan 版本，每步工具、简短依据、Observation 覆盖与证据，已验证结构与未知项，剩余预算、模型回执、规则评估和版本差异。

必须区分真实模型、deterministic baseline、mock 和 replay。仅 healthz 成功不显示模型已就绪；仅生成规则文本不显示规则已发布；查询/显示裁剪必须可见。前端 不在浏览器保存模型、Runtime、内部服务或 Zetaris 密钥。

## 9. 附录：调用接口与数据契约

本附录用于开发对接；阅读业务流程时不需要先理解端点、认证和幂等细节。外部服务接口与本项目待实现接口分别列出，不相互替代。

### 9.1 外部服务接口

#### 9.1.1 NVIDIA 模型 API

| 项 | 契约 |
|---|---|
| 基地址 | `https://integrate.api.nvidia.com/v1` |
| 文本生成 | `POST /chat/completions` |
| 认证 | `Authorization: Bearer <NVIDIA_API_KEY>`；密钥仅在本机私有配置 |
| model | `nvidia/nemotron-3.5-lightning-30b-a3b`；返回其他模型则停止 |
| 输出协议 | `response_format=json_object`，再由 Pydantic 校验 Decision/RuleDraft 与工具专用参数 |
| 推理与输出 | 结构化输出使用 `enable_thinking=false`、`temperature=0`、`max_tokens=2048`；thinking 模式另行验收 |
| 预算 | 调用前持久化最坏预留；响应后按 provider usage 结算；缺少 usage 则停止 |
| 重试 | 无透明网络重试；无效 JSON/契约最多一次修复，单独计入预算 |
| 超时 | 180 秒，不自动切换其他 provider |

Nightshift M1–M4 实测会用应用绑定的 Auto 模式覆盖具体模型，并执行额外质量步骤，且部分推理参数不透传。经项目确认，首版不使用该应用。Meterless Markovian/H-MEM 继续在应用内部运行，二者不依赖 Nightshift。公开免费端点可调用不等于已知账号剩余额度；不得将目录标价 0 当作账单证明。

#### 9.1.2 Zetaris REST

| 项 | 契约 |
|---|---|
| 基地址 | `ZETARIS_REST_URL`，现有配置形态为 `http://localhost:8888/api/v1.0`，经 SSH 隧道到 Azure |
| SQL | `POST {ZETARIS_REST_URL}/sql-editor/sqls/run` |
| headers | Bearer API key、数字 `X-Org-ID`、每次新 UUID 的 `X-Request-ID`、JSON Content-Type |
| 请求体 | `queryId`：UUID；`sql`：单条已编译 SQL；`source`：`SqlEditor`；`limit`：显式整数 |
| 已验证响应 | JSON 对象的 `data` 为行数组；数值/时间可能为字符串；必须按字段字典解析 |
| 连接检查 | `GET /datasource/datasources`、`SELECT 1`、`SHOW LIGHTNING DATABASES`；使用仓库 preflight |
| 调查源 | `aml.transactions` |
| 评估源 | `IBM_AML.hi_large_transactions`，仅评估访问域 |

每个请求一条语句、不带尾分号；银行/账户保留字符串，金额用 Decimal，时间保持实际精度。模型原文不得进入自由 SQL。固定模板只接收经过校验的参数，动态标识符来自注册表白名单。

明细请求取 `cap+1` 行判定 overflow，SQL LIMIT 与请求 limit 一致。缺失/异常 `data`、列结构不匹配或出界记录为 tool error，不视为空结果。当前不提供未经验证的 queryToken 分页；continuation 只有真实实现后才可返回。

服务端查询取消能力未验证。客户端超时后标记 outstanding，确认状态后再决定重试；不得立即重复扫描或报告源查询已被取消。

#### 9.1.3 Meterless 引擎

| 能力 | 调用面 | 宿主要求 |
|---|---|---|
| Markovian | `new Markovian({chunkConfig,...}).run({goal,stepFn,stopWhen,abortSignal,reflect:false})` | 固定官方 commit；一个 step 至多一个业务动作；调用前检查硬预算 |
| stepFn | `{goal,carryover,step,prompt} → {content}` | stepFn 中调用模型适配器/业务 API，事务提交后返回规范 checkpoint 或完成标记 |
| H-MEM | `new HMEM({persistDir,actor,...})`；`add`、`query`、`feedback` | 应用限制 case/split/as_of/source_version；只输出授权记忆 |
| 压缩 | 官方 Carryover Service；可选 `compressorFn` | 首版生成简短 checkpoint，不另写通用压缩器；模型压缩调用必须计费计次 |

`maxChunks` 限制 chunk 数，不等于所有模型 HTTP 请求数。参考 token 统计为估算，provider usage 单独采集。`stopWhen` 在 chunk 后检查，不能代替调用前预算。`reflect=false` 防止带工具的 stepFn 被反思阶段再次调用。

#### 9.1.4 凭据契约

应用模型配置：`NVIDIA_API_KEY`。数据配置统一保存在项目根目录私有 `.env.local`，包括 `ZETARIS_REST_URL`、`ZETARIS_ORG_ID`、`ZETARIS_API_KEY`。`ZETARIS_USERNAME/PASSWORD` 仅在获取 API 规范等必要操作中使用，不向模型提供。

新增 `AML_INTERNAL_TOKEN` 用于 TS→Python 内部接口。凭据保存在本机私有环境或 secret store；不进入配置分发包、提示词、截图、日志或 Git。每位成员使用被授权的环境，不通过共享文档分发密钥。

### 9.2 本项目内部接口与数据契约

本节是项目业务接口契约，不是 Zetaris/NVIDIA 原生接口；已实现路由以 FastAPI /docs 为准。Python 提供 `AML_API_BASE_URL`，默认 loopback `http://127.0.0.1:8010`。实现可配置端口，不自动占用外部端口。内部请求携带 `Authorization: Bearer <AML_INTERNAL_TOKEN>`。

#### 9.2.1 公开业务 API

| Method / Path | 调用者 → 提供者 | 请求核心字段 | 返回 |
|---|---|---|---|
| `POST /v1/bootstrap-runs` | UI/CLI → 业务 API | source_set_version、dataset_snapshot、template_registry_version、development_manifest、validation_manifest、config_version | run_id、queued 状态 |
| `POST /v1/cases` | UI/告警服务 → 业务 API | alert_id 或显式 focal_account、as_of、rule_package_version、snapshot、run_config_version | case_id、run_id |
| `GET /v1/runs/{run_id}` | UI → 业务 API | 路径 ID | 状态、计划、已提交步骤、预算、模型回执摘要 |
| `GET /v1/evidence/{evidence_id}` | UI → 业务 API | 路径 ID；按身份限制字段 | 来源、范围、交易或验证结果 |
| `POST /v1/runs/{run_id}/cancel` | UI → 业务 API | reason | 已请求取消；实际在途状态另列 |
| `POST /v1/rule-revision-runs` | UI/开发批任务 → 业务 API | base_version、case_bundle_id | candidate_version |
| `POST /v1/rule-evaluations` | UI/开发批任务 → 业务 API | candidate_version | 独立规则评估 artifact、accept_for_experiment 或 hold |


案件创建接口使用 `Idempotency-Key`；规则修订和评估返回不可变 artifact ID。重试同键返回同一任务；同键不同请求体返回 409。指定账户调查标记 `origin=manual`，规则产生的告警标记 `origin=rule_alert`。

#### 9.2.2 编排器内部 API

| Method / Path | 调用者 → 提供者 | 行为 |
|---|---|---|
| `POST /internal/v1/jobs/claim` | 编排器 → 业务 API | 通过 SQLite 原子领取 queued 任务；返回 lease_id/fencing_token/到期时间 |
| `POST /internal/v1/jobs/{run_id}/renew` | 编排器 → 业务 API | 续约；过期 worker 禁止提交 |
| `POST /internal/v1/runs/{run_id}/context` | 编排器 → 业务 API | 输入 expected_state_version；输出精确状态、允许工具/范围、EvidencePacket、剩余预算 |
| `POST /internal/v1/runs/{run_id}/model-calls/reserve` | 模型适配器 → 业务 API | 原子预扣最坏 token/请求额度；返回 call_id 或拒绝 |
| `POST /internal/v1/model-calls/{call_id}/settle` | 模型适配器 → 业务 API | 记录响应/错误/usage_source，释放未用预扣；未知用量保留保守预扣 |
| `POST /internal/v1/runs/{run_id}/decisions` | 编排器 → 业务 API | 校验决策和 state_version；记录 plan；至多执行一个工具或结束校验；返回新 Observation/state_version |
| `GET /internal/v1/tool-runs/{action_id}` | 编排器 → 业务 API | 恢复/超时核查已完成、失败或在途动作，不重复提交 |
| `POST /internal/v1/runs/{run_id}/rule-drafts` | Initial Rule Builder/Rule Proposer 经 编排器 → 业务 API | 保存草案/修改建议，返回 compiler 检查结果；无发布权限 |

内部写入需 run lease/fencing_token、expected_state_version 与幂等 action_id。版本过期返回 409，并重新读取状态；权限/范围错误返回 403 或明确 validation error；预算耗尽返回结构化 `budget_exhausted`，不得伪造业务 Observation。最终发布由 独立 Evaluator 评估结果与 Rule Registry 事务完成，不提供模型可调用的 promote 工具。

#### 9.2.3 通用标识

- `transaction_ref = snapshot_id + transaction_id`；transaction_id 为原 CSV 一基行号。
- `account_ref` 解析为 `(bank:string, account:string)`；引用只在授权 run 中有效。
- `window_ref` 解析为 `[start_inclusive,end_exclusive)`，且 `end <= case_as_of`。
- 每个 run 固定 dataset_snapshot、schema/feature/template/policy/rule/prompt/config 版本及 split。
- 交易范围接口采用 ISO 8601 文本并声明 time_basis。HI-Large 未提供时区，使用 dataset_naive，不推定为 UTC 或悉尼时间；混合时间基准拒绝。系统日志/lease 时间使用带时区 UTC，与交易时钟分别保存。
- 模型只使用服务端提供的引用，不改变 as_of、权限、标签域或版本。

#### 9.2.4 Observation

必填字段：schema_version、observation_id、query_id、action_id、scope、as_of、source_snapshot、template_version、status、complete、rows_fetched、rows_shown、display_truncated、coverage、verified_findings、evidence_refs、new_account_refs、elapsed_ms。

允许可空字段：rows_matched、continuation_ref、error_code。未知总数必须为 null。`status` 为 ok/partial/empty/error；empty 必须是完整授权范围内无结果，error 不产生“无交易”事实。

`complete` 表示该次定义明确的源查询是否覆盖其 scope；`display_truncated` 表示本轮仅显示部分已获取证据。完整聚合与完整明细分别记录。查询、聚合或图结构的 verified_findings 均需可回查证据。

## 10. 附录：外部依据与实施前检查

以下链接为能力及接入依据，核查日期 2026-10-22。实施时固定实际版本与账号权益；外部服务变更不自动改写本规范。

| 来源 | 使用范围 |
|---|---|
| [Meterless live Power Prompt](https://meterless.ai/engines.html) | Runtime provider 接入与主要入口要求 |
| [Meterless provider 简版](https://github.com/Meterless/Meterless/blob/main/docs/agent-ready/add-meterless-provider.md) | 参考接入；版本差异以 live contract 和实际安装能力确认 |
| [Markovian orchestrator](https://github.com/Meterless/Meterless/blob/main/engines/markovian/reference/src/orchestrator.ts)、[types](https://github.com/Meterless/Meterless/blob/main/engines/markovian/reference/src/types.ts)、[carryover](https://github.com/Meterless/Meterless/blob/main/engines/markovian/reference/src/carryover.ts) | stepFn、循环、预算参数、carryover、reflect 与停止 |
| [H-MEM 接口](https://github.com/Meterless/Meterless/blob/main/engines/hmem/reference/src/index.ts) | 记忆 add/query/feedback，参考实现非业务事务库 |
| [NVIDIA Lightning](https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b)、[Super VL](https://build.nvidia.com/nvidia/nemotron-3.5-super-vl-120b-a12b) | 默认/可选模型与原型服务资格 |
| [Zetaris SQL Manual](https://kbase.zetaris.com/knowledge/sql-manual)、[Virtual Pipeline Guide](https://kbase.zetaris.com/knowledge/virtual-pipeline-guide) | 聚合、窗口、连接与固定数据准备 |
| [AUSTRAC 银行业](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-banking-sector) | R01/R10/R14 等来源适配 |
| [AUSTRAC 会计](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/risk-insights-and-indicators-suspicious-activity-accountants) | R02/R07/R13 等来源适配 |
| [AUSTRAC 财务规划](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-financial-planners-sector) | R03 来源适配 |
| [AUSTRAC 非银行贷款](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-non-bank-lenders-and-financiers) | R04/R05 来源适配 |
| [AUSTRAC 汇款](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-remittance-service-providers-sector) | R06/R08/R09 来源适配 |
| [AUSTRAC 寿险](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-life-insurance-sector)、[虚拟资产](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-virtual-asset-service-providers) | R11/R12 来源适配及行业限制 |

交付前记录依赖版本锁、Runtime 发现结果、实际账号配额、数据/模板版本和源段落 hash；运行环境不满足时在工作包验收中明确阻塞项。依赖安装、账号开通、计算资源变更按团队环境授权执行。
