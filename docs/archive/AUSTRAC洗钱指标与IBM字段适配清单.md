# AUSTRAC 洗钱指标与 IBM 数据适配清单

核查日期：2026年10月20日。对象：IBM HI-Small 交易数据。

## 核查范围与判断口径

本次检查 AUSTRAC 行业指标目录中的18类页面，重点审阅洗钱指标、交易行为和相关服务风险段落；补充检查2022年贸易型洗钱指南的金融指标，以及2008、2010、2013、2014年类型与案例报告的相关指标。此范围不等于已经穷尽 AUSTRAC 全站历年法律、报告、附件和案例；不将检查过页面等同于全文法规审查。

以下政策描述均为中文转述，不是逐字译文。链接指向原始官方材料，段落名称用于定位。候选计算条件为本项目提出，尚未通过验证集证明效果。未提供数值的政策条目，不得由模型补写成监管阈值。

“可计算”表示交易行为能被观察，不表示可以认定洗钱。行业页面的产品背景仍然保留；只有一般行为可以迁移，不能把普通银行账户冒充保险、赌场、证券或虚拟资产账户。

IBM 可用字段：时间、付款银行及账户、收款银行及账户、收付金额及币种、支付方式。账户键为银行编号与账户编号组合。没有客户身份、所有权、账户类型、开户日期、国家、余额、交易目的、发票、资金来源、IP或钱包风险资料。标签只供离线评估。

## 一、优先采用的政策条目

| 编号 | 官方条目含义与位置 | 本项目可计算条件 | 未覆盖部分或限制 |
|---|---|---|---|
| R01 | 银行业／洗钱指标：通过多个账户快速或复杂转移资金 | 收款后快速转出、按时序多跳路径 | “复杂”需项目定义；路径不证明资金同一性 |
| R02 | 会计行业／异常交易：账户用于接收并转发资金 | A→B后出现B→C；排除同账户 | 无法确认同一笔资金，但中转行为本身可观察 |
| R03 | 财务规划行业／洗钱指标：从多个来源将资金汇集到自己的账户 | 时间窗口内多个付款账户向B入账 | 账户来源可观察，实际客户及资金来源身份未知；未验证财务规划客户背景 |
| R04 | 非银行贷款行业／洗钱指标：短期内发生大额入账 | 按币种统计短期入账金额及笔数 | 没有贷款账户类型，仅迁移一般入账行为 |
| R05 | 非银行贷款行业／洗钱指标：交易模式或频次异常 | 对比历史窗口内频次、金额及对手方变化 | 不使用该条所举的贷款还款、境外关系等无法核查的例子 |
| R06 | 汇款行业／洗钱指标：金额、频次或交易模式异常或可疑 | 同币种金额分位数、高频、路径结构 | 异常标准由项目校准；不能视为官方定量条件 |

来源：[银行业](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-banking-sector)、[会计行业](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/risk-insights-and-indicators-suspicious-activity-accountants)、[财务规划](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-financial-planners-sector)、[非银行贷款](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-non-bank-lenders-and-financiers)、[汇款行业](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-remittance-service-providers-sector)。

## 二、只能生成部分条件监测的条目

| 编号 | 官方条目含义与位置 | 可计算部分 | 必须保留的未核实条件 |
|---|---|---|---|
| P01 | 会计行业／异常交易：同一组账户快速往返转账，缺乏明显理由 | A→B后B→A；间隔、往返次数 | 无法判断理由 |
| P02 | 虚拟资产行业／洗钱指标：转出后有部分资金返还的U-turn | 同币种往返金额及比例 | 账户不等同钱包；无法验证虚拟资产服务背景、地域及同一资金 |
| P03 | 汇款行业／洗钱指标：活动突然显著增加或减少而缺乏解释 | 交易量、同币种金额变化 | 无法判断经济解释 |
| P04 | 汇款行业／洗钱指标：与既有行为模式不一致 | 历史频次、金额、对手方分布变化 | 历史覆盖短，不等于完整客户行为史 |
| P05 | 银行业／洗钱指标：不活跃账户开始频繁交易 | 观察期无活动后交易频率上升 | 无法确认此前长期不活跃或开户状态 |
| P06 | 银行业／洗钱指标：账户接收多个第三方入账 | 不同付款账户数、集中度 | 不同账户不等于不同第三方 |
| P07 | 汇款行业／洗钱指标：多个客户向同一受益人汇款且无明显联系 | 多付款账户汇入同一账户 | 没有客户实体和关系信息 |
| P08 | 寿险行业／洗钱指标：短期多笔较小付款，可能为规避检测而拆分 | 付款次数、同币种金额分布、聚集程度 | 无保险产品信息，不能确认拆分或规避意图 |
| P09 | 汇款行业／洗钱指标：拆分较大交易以逃避审查 | 同账户或账户对的小额聚集 | 无法还原原始大额交易及意图 |
| P10 | 会计行业／异常交易：整额交易偏离预期 | 按币种检查整额占比及历史差异 | 预期客户行为未知；整额本身不是充分风险 |
| P11 | 会计行业／复杂安排：同日或短期资金进出且无合理理由 | 同日入账与出账、时间间隔 | 无法判断合理理由 |
| P12 | 汇款行业／洗钱指标：新开或休眠账户短期多笔高额交易 | 观察期不活跃后高额高频 | 无开户时间及长期历史 |
| P13 | 汇款行业／洗钱指标：多个客户似乎协调交易 | 不同账户同步、相似金额、共同对手方 | 同步不证明协调；无客户身份 |

来源：P01、P10、P11见[会计行业](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/risk-insights-and-indicators-suspicious-activity-accountants)；P02见[虚拟资产行业](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-virtual-asset-service-providers)；P03、P04、P07、P09、P12、P13见[汇款行业](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-remittance-service-providers-sector)；P05、P06见[银行业](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-banking-sector)；P08见[寿险行业](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-life-insurance-sector)。

## 三、可借用行为特征，但不能声称完成行业条目的规则

| 来源 | 行为内容 | 可借用特征 | 为何不能完整覆盖 |
|---|---|---|---|
| 在线博彩／洗钱 | 短期多笔入账；入账次数或金额增加；频繁改变入账方式 | 入账次数、金额变化、支付方式变化 | 无博彩账户；支付方式字段不等于全部入金渠道 |
| 在线博彩／洗钱 | 入账不久后频繁提款 | 收付时间间隔 | 普通转出不能自动解释为博彩提款 |
| 赌场／洗钱 | 多人汇集资金后单笔转入客户赌场账户 | 多入一出结构 | 无赌场或客户赌场账户标识 |
| 养老金／洗钱 | 多来源汇集后立即全额转出 | 汇集后快速转出 | 无养老金账户、余额，不能证明全额 |
| 证券／洗钱 | 入账后相应提款且几乎没有交易 | 收付间隔、同币种金额相近 | 无证券买卖和交易账户资料 |
| 贸易洗钱指南／金融指标 | 整额；交易量或金额变化；同一对手方集中；同实体反复往返 | 整额、变化率、对手方集中度、双向交易 | 无贸易关系、企业身份和合理性解释 |

来源：[在线博彩](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-online-betting-agencies-sector)、[赌场](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-casino-sector)、[养老金](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-superannuation-sector)、[证券及衍生品](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-securities-and-derivatives-sector)、[2022年贸易型洗钱指南，印刷页14—15](https://www.austrac.gov.au/sites/default/files/2022-11/2022_AUSTRAC_FCG_Preventing_Trade_Based_Money_Laundering_web.pdf)。

## 四、当前不启用的条件

现金类：虽然存在Cash支付方式，但尚未核实其能否区分现实业务的现金存款和取款，不能据此直接实现ATM、网点、现金申报门槛或现金置换规则。

身份和关系类：第三方、个人与企业账户、亲属、实际控制人、公司集团、壳公司、客户职业、收入、制裁或犯罪关联，均缺数据。

地域类：境内外、高风险国家、多地网点、海外回流，均缺国家或地点；银行编号、币种不能替代地理身份。

目的和材料类：缺乏经济合理性、发票异常、货物流、保险退保、贷款提前结清、证券洗售、博彩无投注提款，缺少业务事实。

阈值类：澳大利亚现金申报门槛不是IBM合成数据的通用金额阈值；不将所有币种金额直接按10000比较。

## 五、去重后的实施范围

政策条目数量不等于独立规则数量。R01与R02主要属于中转；R03与P06、P07属于汇集；R05、R06与P03、P04属于异常变化；P01、P02属于往返。

建议维护8类候选行为：快速中转、多来源汇集、短期大额入账、小额交易聚集、快速往返、频次或金额突变、观察期不活跃后激活、整额交易偏离历史。

同日多收款人分发可作为中转/分发扩展，但前次引用的汇款行业“同日向多个受益人付款”位于儿童剥削指标，不列为独立洗钱原文。多跳与循环可由R01支持为调查检查，不宣称官方另有与项目算法完全一致的循环规则。

首版先验证前5类；历史依赖较强的后3类视历史覆盖和验证结果决定是否启用。所有参数均待开发集探索、验证集校准，测试集冻结后评估。本文只确认文本与字段的适配性，未完成这些规则的全量命中与效果测试。

## 六、行业页面核查记录

| 页面 | 对当前IBM方案的结论 |
|---|---|
| 银行业 | 快速多账户、汇入、不活跃及历史变化可作基础或部分条件 |
| 汇款服务 | 频次/金额异常、突变、拆分、高额高频和汇集可提取 |
| 财务规划 | 多来源汇集有直接行为依据；投资、SMSF前提不可验证 |
| 会计 | 中转、往返、整额及短期进出提供较贴近的描述 |
| 非银行贷款 | 短期大额入账、异常频次可迁移；贷款属性不可验证 |
| 寿险 | 多笔小额付款可迁移；保险产品及意图不可验证 |
| 虚拟资产 | 往返、拆分和高额高频可借鉴；钱包风险及产品属性不可验证 |
| 在线博彩 | 入账次数、金额、方式和收付间隔可借鉴；博彩前提不可验证 |
| 证券衍生品 | 部分收付结构可借鉴；交易活动及证券条件不可验证 |
| 养老金 | 汇集后转出结构可借鉴；全额、账户类型不可验证 |
| 赌场 | 汇集后单笔转出结构可借鉴；赌场属性不可验证 |
| 贵金属条块 | 购买、估值、重量及交付信息缺失；不新增完整规则 |
| 珠宝及贵金属产品 | 现金、客户背景、所有权和商品信息缺失；不新增完整规则 |
| 法律专业 | 信托账户、公司结构、资产交易前提缺失；不新增完整规则 |
| 房地产（含过户） | 房产、交易目的、信托款及退款事实缺失；不新增完整规则 |
| 场内博彩公司 | 注单、中奖凭证及现金投注条件缺失 |
| 酒吧及俱乐部 | EGM、TITO和兑付事实缺失 |
| 非营利组织 | 无NPO身份、用途及项目资料；无独立洗钱专节，不套用恐融结论 |

目录来源：[银行业页面中的行业指标导航](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-banking-sector)。其余已查页面：[贵金属条块](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-bullion-sector)、[珠宝及贵金属产品](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/risk-insights-and-indicators-suspicious-activity-dealers-precious-stones-metals-and-other-products)、[法律](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/risk-insights-and-indicators-suspicious-activity-legal-professionals)、[房地产](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/risk-insights-and-indicators-suspicious-activity-real-estate-sector)、[场内博彩公司](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-course-bookmaker-sector)、[酒吧俱乐部](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-pubs-and-clubs-sector)、[非营利组织](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/indicators-suspicious-activity-relating-customers-who-are-not-profit-organisations)。

历史补充：[2008年](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/typologies-and-case-studies-report-2008)、[2010年](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/typologies-and-case-studies-report-2010)、[2013年](https://www.austrac.gov.au/industry-and-business/education-and-resources/publications-and-resources/typologies-and-case-studies-report-2013)、[2014年](https://www.austrac.gov.au/sites/default/files/2019-07/typologies-report-2014.pdf)。历史指标用于行为参考，不替代现行义务；不因案例最终被定罪就把同样交易特征当作定罪标准。
