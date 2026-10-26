# IBM 交易风险调查项目可行性报告

本方案使用 IBM 交易数据与 AUSTRAC 风险指标，建立从规则生成、初始告警、Agent 动态调查到规则完善和案件展示的完整流程。开发阶段形成规则与调查策略，验证阶段自动评估正式规则，测试阶段执行冻结版本并展示案件。规则更新采用自动处理流程，不设置人工审批环节。

## 一 IBM字段和业务含义

IBM 数据以单笔交易为一行，记录付款账户、收款账户、时间、金额、币种和支付方式。账户与银行编号组合用于关联交易，上下游路径由多笔交易连接形成。

| 原始字段 | 业务含义 | 在项目中的用途 |
|---|---|---|
| Timestamp | 交易发生时间 | 计算收付间隔、统计窗口和路径顺序 |
| From Bank | 付款银行编号 | 与付款账户组成账户唯一键 |
| Account 付款侧 | 付款账户编号 | 追溯资金来源和付款对象 |
| To Bank | 收款银行编号 | 与收款账户组成账户唯一键 |
| Account 收款侧 | 收款账户编号 | 追踪资金去向和收款来源 |
| Amount Paid | 付款金额 | 识别分币种高额、小额和累计付款 |
| Payment Currency | 付款币种 | 确定付款金额的统计口径 |
| Amount Received | 收款金额 | 分析入账规模与累计入账 |
| Receiving Currency | 收款币种 | 确定入账口径及币种变化 |
| Payment Format | 支付方式 | 区分ACH、Wire等交易方式 |
| Is Laundering | 是否属于模拟洗钱交易 | 用于离线规则校准及结果评估 |

原始表的两个账户列同名，接入时分别命名为付款账户和收款账户。金额按币种统计，不直接汇总不同币种。数据不包含客户身份、实际控制人、国家、开户日期、余额、合同或交易目的；相应判断列为未核实条件。

标签不作为告警规则和在线调查的输入。Agent先依据可见交易开展调查，再由独立评估程序对照标签，统计新增发现的打标交易。

数据来源：[IBM官方数据集](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml)。

## 二 AUSTRAC规则文本与字段适配

下表为可以使用或提取部分条件的AUSTRAC指标。规则描述采用中文转述，原文链接列可直接打开来源。项目依据这些文本形成监测条件；时间窗口、账户数量和金额阈值通过开发与验证确定。

| 编号 | AUSTRAC规则描述 | IBM字段与计算条件 | 适配边界 | 原文 |
|---|---|---|---|---|
| R01 | 多账户快速或复杂资金转移 | 账户、时间；检查快速中转及多跳 | 行为可计算；复杂程度由项目定义 | [银行](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-banking-sector) |
| R02 | 账户接收资金并转发给其他账户 | 账户、时间；检查A→B→C | 行为可计算；无法认定同一资金 | [会计](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/risk-insights-and-indicators-suspicious-activity-accountants) |
| R03 | 多来源资金汇集到同一账户 | 账户、时间；统计不同付款账户数 | 账户级可计算；未验证客户及行业身份 | [财务规划](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-financial-planners-sector) |
| R04 | 短期内发生大额入账 | 金额、币种、时间；分币种累计 | 一般行为可计算；无贷款账户身份 | [非银行贷款](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-non-bank-lenders-and-financiers) |
| R05 | 交易模式或频次异常 | 时间、账户、金额；比较历史分布 | 需历史基准；无客户背景 | [非银行贷款](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-non-bank-lenders-and-financiers) |
| R06 | 交易金额频次或模式异常 | 金额、币种、时间、账户；异常分位数 | 异常标准需校准；无客户背景 | [汇款](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-remittance-service-providers-sector) |
| R07 | 快速往返转账且无明显理由 | 账户、时间、金额及币种；A→B→A | 仅覆盖往返；无法核实理由 | [会计](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/risk-insights-and-indicators-suspicious-activity-accountants) |
| R08 | 活动突然显著增减且缺乏解释 | 交易次数、同币种金额；变化率 | 仅覆盖突变；解释未知 | [汇款](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-remittance-service-providers-sector) |
| R09 | 拆分较大交易以逃避审查 | 账户、时间、金额；小额聚集 | 不能确认原始大额交易和意图 | [汇款](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-remittance-service-providers-sector) |
| R10 | 原本不活跃的账户开始频繁交易 | 账户、时间；观察期静默后激活 | 无长期历史；不能确认真实休眠 | [银行](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-banking-sector) |
| R11 | 短期多笔较小付款且可能为规避检测 | 账户、时间、金额；多笔小额 | 仅覆盖聚集；无保险业务和意图 | [寿险](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-life-insurance-sector) |
| R12 | 转出资金后有部分金额返还 | 账户、时间、同币种金额；返还比例 | 无虚拟资产业务；不能确认同一资金 | [虚拟资产](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-virtual-asset-service-providers) |
| R13 | 整额交易偏离预期 | 金额、币种；整额占比及历史差异 | 只覆盖数值形态和历史偏离 | [会计](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/risk-insights-and-indicators-suspicious-activity-accountants) |
| R14 | 多个第三方向同一账户入账 | 付款账户、收款账户、时间 | 只能确认不同账户，非不同所有人 | [银行](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-banking-sector) |

银行、财务规划、非银行贷款、汇款、寿险及虚拟资产页面的对应条目主要位于“Money laundering indicators”；会计页面对应“Unusual transactions and requests”。跨行业使用时保留一般交易行为，不将普通账户认定为贷款、保险或虚拟资产账户。

以上指标去重后主要对应快速中转、多来源汇集、大额入账、异常频次、快速往返、小额聚集、活动突变和整额偏离。相似条目共享特征，不作为相互独立证据重复加分。涉及经济合理性、第三方身份或犯罪意图的部分，在报告中保留为待核实事项。

## 三 实现方案

整体流程为：初始规则生成 → Agent接收告警 → 动态规划上下游查询 → 发现更多关联风险交易 → 规则补充和权限更新 → 正式规则生成 → 验证集评估 → 测试集扫描和告警 → 案件展示。

### 1 初始规则生成

建立固定规则模板，至少包含政策来源、行为类型、适用字段、触发条件、待定阈值、证据输出和不支持的条件。Agent读取指定AUSTRAC段落与字段说明，将可计算的内容填入模板；程序自动检查字段、条件及配置格式，并转换为查询。

初始模板优先选择快速中转、多来源汇集和短期大额入账。开发集用于确认收付间隔、不同对手方数量、分币种累计金额等特征，比较候选阈值。规则组合先采用任一规则命中即告警，重叠命中合并为同一账户的相关告警。

本步骤产出初始规则配置、对应的政策依据和告警查询。参数作为可调整配置保存，不由模型将自主提出的数值表述为官方要求。

### 2 Agent接收告警

系统在开发数据上执行初始规则，将焦点账户、触发交易、命中条件、政策来源和调查截止时间交给Agent。Agent建立案件状态，记录当前已知事实、未解决问题、已查账户及允许调用的工具。

初始告警只表示发现值得调查的行为。Agent首先明确本案需要回答的问题，例如资金是否来自多个账户、是否继续经过其他中间账户、是否回流，以及是否存在重复往返。

### 3 动态规划查询上下游信息

LLM依据当前证据选择下一项调查动作，调用工具后读取结果，再调整后续计划。Zetaris执行交易查询，Meterless管理多轮执行、状态与记忆；程序负责参数检查、路径计算及证据保存。

| 当前观察 | 可选择的下一步 | 调查目的 |
|---|---|---|
| 收款后快速转出 | 查询付款来源的上游 | 判断是否属于更长的转移路径 |
| 多账户向同一账户入账 | 查询该账户后续付款 | 判断是否汇集后继续转移或分发 |
| 向多个账户付款 | 查询这些账户的后续收付 | 查找共同收款点或进一步分层 |
| 下游出现已访问账户 | 核对路径及时间顺序 | 判断是否形成循环或往返 |
| 出现多笔相似或平行交易 | 查询相关账户历史及完整明细 | 保留候选路径，避免错误归因 |
| 未发现新关联或已达到调查边界 | 结束并汇总已知与未知 | 输出证据范围内的结论 |

计划由查询结果驱动，不预设每个案件必须按相同顺序查询。每步保存调查问题、动作选择理由、查询参数、返回证据和判断变化，使展示页面能够复现决策过程。

### 4 发现更多关联风险交易

Agent将初始告警之外发现的交易连接成候选资金网络，识别多跳、汇集分发、往返及循环等结构。程序核对账户连接、时间顺序和交易编号，保留原始证据与候选路径。

开发与评估阶段，由独立程序将检索出的交易编号与Is Laundering标签对照，确认其中新增覆盖的打标交易。运行中的Agent不读取标签，也不将所有关联交易自动判定为洗钱。

本步骤产出扩展后的账户关系、交易明细、结构判断和新增风险线索，作为规则完善与案件解释的依据。

### 5 规则补充和权限更新

Agent比较初始规则与扩展调查结果，提出可复用的补充条件，例如在快速中转基础上增加多来源汇集、重复往返或路径层数。规则更新只针对可查询字段和已定义特征，不将单个案例的账户编号写成通用规则。

| 更新对象 | 自动处理方式 | 输出 |
|---|---|---|
| 规则条件 | Agent提出增删条件，程序检查字段与逻辑 | 补充规则草案 |
| 阈值及权重 | 在开发集比较候选参数，去除重复特征计分 | 参数及排序配置 |
| 查询权限范围 | Agent提出需要的工具或查询范围；系统在预设可授权清单内更新 | 可执行权限配置 |

查询权限更新指调整可调用工具、可访问字段、历史窗口和查询预算。Agent不自行获取预设范围之外的数据访问权；不支持的请求自动返回限制说明。该流程由配置程序执行，不设置人工审批。

可增加一个Agent自检步骤，核对补充规则是否有政策来源、是否使用缺失字段、是否遗漏条件或夸大结论。自检属于辅助能力；配置格式、参数范围和访问权限由程序检查，效果由验证集评估。该自检机制作为实现选项，不作为已经证明有效的功能。

### 6 正式规则生成

系统将初始规则、补充条件、阈值、排序权重和权限范围整理为待验证的正式规则版本，记录版本号、政策来源、适用范围及生效条件。正式规则与Agent调查配置分别保存，便于追溯哪个版本产生告警、哪个配置执行调查。

规则生成程序自动检查配置是否完整、查询是否可执行、字段是否存在、权限是否在允许范围，以及金额和时间口径是否一致。通过检查后进入验证集评估。

### 7 验证集验证正式规则

在验证集上执行正式规则，并对告警运行Agent调查。独立评估程序对照标签，检查初始告警覆盖、新增打标交易覆盖、候选交易量、查询次数及证据完整性。

同时检查调查记录是否支持最终结论，是否出现重复查询、未来数据使用或缺失证据。未达到预设目标时返回开发阶段调整；达到目标后自动发布并冻结正式规则、提示词、工具清单和权限配置。测试阶段不再使用测试标签修改这些配置。

### 8 在测试集扫描并产生告警

使用冻结后的正式规则扫描测试集，生成告警并合并相关案件。Agent使用相同调查配置动态选择上下游查询，系统保存实际调用记录及证据网络。

测试标签仅在调查结束后由独立评估程序读取。跨时间边界的路径只使用案件截止前已经发生的交易，不读取未来交易补齐资金链路。

### 9 生成案件展示

从测试运行结果中选择代表案件，展示从初始告警到关联交易发现的完整过程。案件可包含形成循环的路径，也可包含调查后证据不足或未形成预期结构的情况。

| 展示内容 | 页面呈现 |
|---|---|
| 告警起点 | 命中规则、政策来源、触发账户与交易 |
| 动态调查 | 每步问题、选择的工具、选择理由及返回结果 |
| 关联网络 | 上游、下游、汇集分发及往返路径 |
| 打标交易对照 | 调查结束后标出与数据标签一致的交易，区分初始与新增发现 |
| 结果解释 | 支持风险判断的证据、候选路径及仍无法确认的条件 |
| 规则演进 | 展示开发阶段形成的补充条件和最终冻结版本 |

展示中的标签对照属于事后评估视图，不作为Agent决策输入。规则演进发生在开发验证阶段；测试展示展示版本关系，不现场修改测试规则。

### 10 开发验证和测试的分工

| 数据部分 | 工作内容 | 阶段产出 |
|---|---|---|
| 开发集 | 初始规则、阈值探索、Agent调查动作、补充规则与权限配置 | 可执行规则和调查流程 |
| 验证集 | 自动评估正式规则、调查效果及证据完整性 | 通过验证的冻结版本 |
| 测试集 | 正式扫描、告警、动态追查及标签对照 | 案件展示和总体评估 |

此处的前期训练主要是规则参数校准、提示词和调查策略开发，不要求微调大语言模型权重。数据按时间划分，保留必要历史；同一参考模式跨分区时单独处理，避免将其反复用于开发和评分。

整体技术分工为：LLM解析文本和规划调查，Zetaris连接数据并执行查询，Meterless维护执行状态与记忆，程序完成计算、自动检查和标签评估，前端展示规则依据、决策过程及交易证据。
