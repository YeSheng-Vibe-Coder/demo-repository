# AML Agent 团队实施入口

阅读版：[HTML 技术规范](AML_Agent_Technical_Spec_v1.0.html)，可离线阅读、下载内嵌配置或打印保存 PDF。

Agent/源码版：[Markdown 技术规范](AML_Agent_Technical_Spec_v1.0.md)。

正式配置：[AML_Agent_Config_v1.0.json](AML_Agent_Config_v1.0.json)。

工作包：[IMPLEMENTATION_TASKS_v1.0.json](IMPLEMENTATION_TASKS_v1.0.json)。

## 执行要求

1. 先阅读规范第 1–4、8–9 节，确认公共契约、能力来源、配置和现有状态。
2. 按团队分配的工作包实施；执行流程和能力调用见规范第 3–4 节，任务依赖和验收见第 8 节。依赖未就绪时使用契约 mock，记录未验收项。
3. 实现相关业务章节；所有新增项目接口必须按规范标识，不误当第三方原生 API。
4. 外部服务、引擎和现有代码按规范列出的入口复用，固定依赖版本；不共享私钥或密钥。
5. 按工作包验收条件提交代码、版本、测试和真实调用回执；区分 mock、replay、baseline 与 live。
6. 公共 schema、端点、预算、数据口径或版本行为变更，同步更新正式规范与 config_version；不单方面改变接口。

`docs/archive/` 中的 `Agent技术实现方案_v1.md` 和 `agent-budget.example.json` 为历史讨论/配置草案，已被正式版本替代，不作为当前实现契约。

## 分发包

`output/AML_Agent_Technical_Spec_v1.0.zip` 包含 HTML/Markdown 正式规范、配置、工作包和统一实验 README；不包含应用凭据或原始交易数据。包中只附最新脱敏 JSON 回执；历史完整 JSON/CSV 在仓库 analysis/records/，不打包原始交易数据。实验结果统一见 [实验 README](../../analysis/README.md)，不能代替待完成的验收。规范中的仓库路径指项目源码位置；文档包不是可运行安装包。

当前实现（Zetaris 特征库、决策树规则学习 agent、调查团队与 World Model、上下文与执行规则）见 [运行指南](../operations/RUNBOOK.md) 与 [执行 Harness](EXECUTION_HARNESS.md)。不执行强化学习或模型权重训练。

上一版的实现对照与 Planner 学习设计已移至 `docs/archive/`（`*_v1`），按当时版本保留。
