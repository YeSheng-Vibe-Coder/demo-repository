# 业务流程与当前实现对照

当前已贯通第一版规则驱动研究闭环，真实运行和效果边界见 [完整验证报告](../../analysis/README.md)。案件 LLM 审批已移除；规则 Evaluator 仅评审规则更新。

## 业务主线

冻结研究参考规则 → 执行规则产生告警 → Planner 自主调查上下游 → 输出疑似交易 ID → 调查结束后独立逐笔标签校验 → 跨案件特征与反例归纳 → Rule Proposer → 规则编译 → 对实际父版本作独立验证与模型规则评审 → 实验登记或保留旧版本 → 监控检查。

标签不进入调查 Planner/H-MEM。契约、方向、范围、预算与循环保护属于程序完整性约束，不是案件业务审批。保护性停止案件不能冒充模型正常完成。

## 实现与调用入口

| 环节 | 已实现入口 | 本轮证据与边界 |
|---|---|---|
| 初版与参考规则 | experiments.RuleBuilder；rule_program.reference_program | 初版生成候选仍曾 hold；本轮使用明确标识的预登记研究参考规则 |
| 规则告警 | rule_cycle.emit_alerts | Zetaris 冻结开发特征回放产生 15 告警，标签盲排序调查 3 案 |
| Plan / Action / Replan | workflow；官方 Markovian/H-MEM worker | NVIDIA 决策，Zetaris 固定查询；2 案正常结束，1 案循环保护停止 |
| 逐笔标签校验 | cohorts.TransactionLabelReader；rule_cycle.grade_samples | 按实际交易 ID 查 VM 标签；检索与疑似子集分开，不继承账户标签 |
| 跨案归纳 | rule_cycle.aggregate_cases | 至少 2 个有效开发案件，排除保护性停止 |
| 规则修订与编译 | rule_updates.RuleUpdateService；rule_program | 阈值、条件增停、已登记 24/48h 特征、all/any/weighted、受控派生特征 |
| 独立 Rule Evaluator | rule_updates.evaluate | 实际 base_version、相同告警预算、冻结验证标签门禁，再独立模型评审规则 |
| 登记与监控 | rule_updates.publish/monitor_current；store.program_registry | CAS 切换实验别名，保留父版本；退化回滚分支单测通过，无后台调度 |

## 本轮验收与剩余问题

100 个富集验证账户窗口（10 正例）：原规则 TP1/FP12，候选 TP2/FP9；登记为实验版本，不能生产发布。测试集未打开。验证集已被此前原型使用，账户跨分区重叠，不能宣称盲测或总体效果。

两个有效案件均无检索正例、未选疑似样本；第三案检索到 1 个正例但循环停止，未识别。候选仍触发这 3 个告警。调查有效性和定向消除误报仍需改进，规则模型评审中还有来源解释错误，不能用其文字替代程序门禁。

旧八次提示对照仅为历史诊断。reviewer.py / review-controls 不参与主线；默认 baseline，targeted 未推广。自动经验提炼、情境检索、策略晋级、生产 RBAC、全量自然分布评估仍未完成。
