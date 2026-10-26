# 规则版本记录

由 `python -m aml_agent.cli rule-changelog` 根据规则版本库自动生成（2026-10-26 04:26），不要手工修改。
评估都在 Zetaris 内完成：验证期 09-05–09-25 用于门禁，监控期 10-21–11-03 用于回滚检查，测试期始终关闭。

## 总览

| 版本 | 状态 | 来源 | 目标 | 父版本 | 验证期链路召回 | 每天告警 | 门禁结论 |
|---|---|---|---|---|---|---|---|
| v0 | 参考 | 人工设定 | — | — | — | — | — |
| v1 候选 | 暂缓 | 初始训练 | 旧目标 | v0 | — | — | 接受 |
| v1 | 已发布 | 初始训练 | 旧目标 | v0 | — | — | 接受 |
| v2 候选 | 暂缓 | 规则更新 | 旧目标 | v1 | — | — | 暂缓 |
| v2 | 已发布 | 初始训练 | 旧目标 | v0 | — | — | 接受 |
| v3 候选 | 暂缓 | 规则更新 | 旧目标 | v2 | — | — | 暂缓 |
| v3 | 已发布 | 规则更新 agent 触发 | 旧目标 | v2 | — | — | 接受 |
| v4 | 已发布 | 初始训练 | 高召回 | v3 | 99.9% | 58,411 | 接受 |
| v5 | 已发布 | 初始训练 | 高召回 | v4 | 100.0% | 27,233 | 接受 |
| v6 | 已发布 | 规则更新 agent 触发 | 高召回 | v5 | 99.2% | 19,627 | 接受 |
| v7 | 当前生效 | 规则更新 agent 触发 | 高召回 | v6 | 98.7% | 16,672 | 接受 |

## v7 · 当前生效

- 状态：已发布
- 来源：规则更新 agent 触发（依据调查结论与标签反馈制定方案）
- 目标：高召回：验证期链路召回 ≥ 97%，再比告警量与 precision
- 父版本：v6 · 生成时间：2026-10-26 02:49
- 版本 ID：`a1b84d07-d828-4890-bf64-398ab5eca1b7`
- 训练配置：19 个指标，最多 48 个叶子，最小叶子 50；训练反馈包含 24 个调查案件
- 开发集拟合：链路召回 99.4%，选用 17/48 个叶子，合并为 17 条规则
- 门禁结论：接受 · Keeps the chain-recall target with fewer alerts and no lower chain precision
- 监控期检查：未回滚 · 链路召回 97.6%（父版本 98.8%），每天告警 26,264（父版本 29,424）

> 规则更新 agent 的理由：Current recall is 99.4% (well above 97% target) but 820k account-days is high. Key insight: rules g54 (315k days, 4.4% precision) and g52 (300k days, 6.1% precision) generate 615k false-positive days. These rules share: fan_in_48h>=1, new_counterparty_share>=0.0357, onward_currency_switch<=0.5, pass_through_ratio<=3.18. Investigations show g54: 4/8 laundering, g52: 3/8 laundering. Feature contrast

| 验证期指标 | 本版本 | 父版本 |
|---|---|---|
| 链路召回 | 98.7% | 99.2% |
| 每天告警账户日 | 16,672 | 19,627 |
| 窗口 precision | 13.5% | 12.1% |

**相对父版本的规则变化**

- 新增 5 条 · 删除 1 条 · 调整 6 条 · 不变 6 条
- 新增：activity_change_24h ≥ 0.881945 且 amount_vs_history_24h ≥ 0.298659 且 amount_vs_history_24h ≤ 0.939755 且 history_active_days_7d ≥ 6 且 new_counterparty_share_24h ≤ 0.035714 且 round_trip_48h ≤ 0.5
- 新增：activity_change_24h ≥ 0.958334 且 amount_vs_history_24h ≥ 0.939756 且 amount_vs_history_24h ≤ 1.00014 且 fan_in_24h ≤ 2.5 且 history_active_days_7d ≥ 6 且 history_active_days_7d ≤ 6.5 且 new_counterparty_share_24h ≤ 0.035714 且 round_trip_48h ≤ 0.5
- 新增：fan_in_48h ≥ 1 且 history_active_days_7d ≥ 5 且 new_counterparty_share_24h ≥ 0.035715 且 new_counterparty_share_24h ≤ 0.679487 且 onward_currency_switch_48h ≤ 0.5 且 pass_through_ratio_24h ≤ 3.18573
- 新增：fan_in_48h ≥ 1 且 history_active_days_7d ≥ 1 且 new_counterparty_share_24h ≥ 0.679488 且 onward_currency_switch_48h ≤ 0.5 且 pass_through_ratio_24h ≤ 3.18573
- 新增：amount_vs_history_24h ≤ 2.45188 且 fan_in_48h ≥ 1 且 history_active_days_7d ≥ 5 且 new_counterparty_share_24h ≥ 0.035715 且 onward_currency_switch_48h ≤ 0.5 且 pass_through_ratio_24h ≥ 3.18573
- 删除：amount_vs_history_24h ≥ 1.00014 且 new_counterparty_share_24h ≤ 0.035714
- 调整：activity_change_24h ≥ 0.881945 → 0.961539；amount_vs_history_24h ≤ 0.939755 → 0.101439；history_active_days_7d ≤ 5.5 → 4.5；＋ out_banks_24h ≥ 1；－ fan_in_48h ≥ 2；－ history_active_days_7d ≥ 5；－ round_trip_48h ≤ 0.5
- 调整：activity_change_24h ≤ 0.961538 → 0.854167；amount_vs_history_24h ≤ 0.101439 → 0.939755；history_active_days_7d ≤ 2.5 → 4.5；＋ amount_vs_history_24h ≥ 0.10144；＋ history_active_days_7d ≥ 3；＋ round_trip_48h ≥ 1
- 调整：history_active_days_7d ≥ 6 → 5；＋ fan_in_48h ≥ 2；＋ history_active_days_7d ≤ 5.5；－ amount_vs_history_24h ≥ 0.298659
- 调整：amount_vs_history_24h ≥ 650.815 → 93.03；＋ amount_vs_history_24h ≤ 650.815
- 调整：＋ amount_vs_history_24h ≥ 650.815；＋ history_active_days_7d ≤ 0.5；－ history_active_days_7d ≥ 1
- 调整：history_active_days_7d ≥ 5 → 1；＋ history_active_days_7d ≤ 3.5；＋ new_counterparty_share_24h ≤ 0.679487；＋ pass_through_ratio_24h ≤ 3.18573；－ pass_through_ratio_24h ≥ 3.18573

**规则（17 条，任一成立即告警）**

1. `activity_change_24h` ≥ 0.961539 且 `amount_vs_history_24h` ≤ 0.101439 且 `history_active_days_7d` ≤ 4.5 且 `new_counterparty_share_24h` ≤ 0.035714 且 `out_banks_24h` ≥ 1
2. `activity_change_24h` ≤ 0.854167 且 `amount_vs_history_24h` ≥ 0.10144 且 `amount_vs_history_24h` ≤ 0.939755 且 `history_active_days_7d` ≤ 2.5 且 `new_counterparty_share_24h` ≤ 0.035714
3. `activity_change_24h` ≤ 0.854167 且 `amount_vs_history_24h` ≥ 0.10144 且 `amount_vs_history_24h` ≤ 0.939755 且 `history_active_days_7d` ≥ 3 且 `history_active_days_7d` ≤ 4.5 且 `new_counterparty_share_24h` ≤ 0.035714 且 `round_trip_48h` ≥ 1
4. `activity_change_24h` ≥ 0.881945 且 `amount_vs_history_24h` ≤ 0.939755 且 `fan_in_48h` ≥ 2 且 `history_active_days_7d` ≥ 5 且 `history_active_days_7d` ≤ 5.5 且 `new_counterparty_share_24h` ≤ 0.035714 且 `round_trip_48h` ≤ 0.5
5. `activity_change_24h` ≥ 0.881945 且 `amount_vs_history_24h` ≥ 0.298659 且 `amount_vs_history_24h` ≤ 0.939755 且 `history_active_days_7d` ≥ 6 且 `new_counterparty_share_24h` ≤ 0.035714 且 `round_trip_48h` ≤ 0.5
6. `amount_vs_history_24h` ≤ 0.939755 且 `history_active_days_7d` ≥ 5 且 `new_counterparty_share_24h` ≤ 0.035714 且 `round_trip_48h` ≥ 1
7. `activity_change_24h` ≥ 0.958334 且 `amount_vs_history_24h` ≥ 0.939756 且 `amount_vs_history_24h` ≤ 1.00014 且 `fan_in_24h` ≤ 2.5 且 `history_active_days_7d` ≥ 6 且 `history_active_days_7d` ≤ 6.5 且 `new_counterparty_share_24h` ≤ 0.035714 且 `round_trip_48h` ≤ 0.5
8. `amount_vs_history_24h` ≥ 0.939756 且 `amount_vs_history_24h` ≤ 1.00014 且 `new_counterparty_share_24h` ≤ 0.035714 且 `round_trip_48h` ≥ 1
9. `amount_vs_history_24h` ≥ 999.655 且 `fan_in_48h` ≤ 0.5 且 `history_active_days_7d` ≥ 1 且 `history_active_days_7d` ≤ 1.5 且 `new_counterparty_share_24h` ≥ 0.035715
10. `amount_vs_history_24h` ≥ 990.857 且 `fan_in_48h` ≤ 0.5 且 `history_active_days_7d` ≥ 2 且 `new_counterparty_share_24h` ≥ 0.035715
11. `amount_vs_history_24h` ≥ 93.03 且 `amount_vs_history_24h` ≤ 650.815 且 `fan_in_48h` ≥ 1 且 `history_active_days_7d` ≤ 0.5 且 `new_counterparty_share_24h` ≥ 0.035715 且 `onward_currency_switch_48h` ≤ 0.5 且 `pass_through_ratio_24h` ≤ 3.18573
12. `amount_vs_history_24h` ≥ 650.815 且 `fan_in_48h` ≥ 1 且 `history_active_days_7d` ≤ 0.5 且 `new_counterparty_share_24h` ≥ 0.035715 且 `onward_currency_switch_48h` ≤ 0.5 且 `pass_through_ratio_24h` ≤ 3.18573
13. `fan_in_48h` ≥ 1 且 `history_active_days_7d` ≥ 1 且 `history_active_days_7d` ≤ 3.5 且 `new_counterparty_share_24h` ≥ 0.035715 且 `new_counterparty_share_24h` ≤ 0.679487 且 `onward_currency_switch_48h` ≤ 0.5 且 `pass_through_ratio_24h` ≤ 3.18573
14. `fan_in_48h` ≥ 1 且 `history_active_days_7d` ≥ 5 且 `new_counterparty_share_24h` ≥ 0.035715 且 `new_counterparty_share_24h` ≤ 0.679487 且 `onward_currency_switch_48h` ≤ 0.5 且 `pass_through_ratio_24h` ≤ 3.18573
15. `fan_in_48h` ≥ 1 且 `history_active_days_7d` ≥ 1 且 `new_counterparty_share_24h` ≥ 0.679488 且 `onward_currency_switch_48h` ≤ 0.5 且 `pass_through_ratio_24h` ≤ 3.18573
16. `amount_vs_history_24h` ≤ 2.45188 且 `fan_in_48h` ≥ 1 且 `history_active_days_7d` ≥ 5 且 `new_counterparty_share_24h` ≥ 0.035715 且 `onward_currency_switch_48h` ≤ 0.5 且 `pass_through_ratio_24h` ≥ 3.18573
17. `fan_in_48h` ≥ 1 且 `new_counterparty_share_24h` ≥ 0.035715 且 `onward_currency_switch_48h` ≥ 1

## v6

- 状态：已发布
- 来源：规则更新 agent 触发（依据调查结论与标签反馈制定方案）
- 目标：高召回：验证期链路召回 ≥ 97%，再比告警量与 precision
- 父版本：v5 · 生成时间：2026-10-24 10:17
- 版本 ID：`20491172-5117-4a5f-bde6-0e47624a37dc`
- 训练配置：19 个指标，最多 32 个叶子，最小叶子 50；训练反馈包含 48 个调查案件
- 开发集拟合：链路召回 99.4%，选用 13/32 个叶子，合并为 13 条规则
- 门禁结论：接受 · Keeps the chain-recall target with fewer alerts and no lower precision
- 监控期检查：未回滚 · 链路召回 98.8%（父版本 100.0%），每天告警 29,424（父版本 38,468）

> 规则更新 agent 的理由：The current fit has 24 leaves and 11 groups, achieving near-perfect chain recall (0.9999) but alerting over 1.1M account-days. The feature_contrast shows that transit_gap_hours has a very high true_positive/false_positive ratio (43.8 vs 27.4), indicating it is a strong discriminator for laundering vs. noise. Other high-contrast features like amount_vs_history_24h have low precision (4.5 vs 869.2).

| 验证期指标 | 本版本 | 父版本 |
|---|---|---|
| 链路召回 | 99.2% | 100.0% |
| 每天告警账户日 | 19,627 | 27,233 |
| 窗口 precision | 12.1% | 9.2% |

**相对父版本的规则变化**

- 新增 7 条 · 删除 5 条 · 调整 6 条 · 不变 0 条
- 新增：activity_change_24h ≥ 0.881945 且 amount_vs_history_24h ≤ 0.939755 且 fan_in_48h ≥ 2 且 history_active_days_7d ≥ 5 且 history_active_days_7d ≤ 5.5 且 new_counterparty_share_24h ≤ 0.035714 且 round_trip_48h ≤ 0.5
- 新增：activity_change_24h ≥ 0.881945 且 amount_vs_history_24h ≥ 0.298659 且 amount_vs_history_24h ≤ 0.939755 且 history_active_days_7d ≥ 6 且 new_counterparty_share_24h ≤ 0.035714 且 round_trip_48h ≤ 0.5
- 新增：amount_vs_history_24h ≥ 1.00014 且 new_counterparty_share_24h ≤ 0.035714
- 新增：amount_vs_history_24h ≥ 650.815 且 fan_in_48h ≥ 1 且 history_active_days_7d ≤ 0.5 且 new_counterparty_share_24h ≥ 0.035715 且 onward_currency_switch_48h ≤ 0.5 且 pass_through_ratio_24h ≤ 3.18573
- 新增：fan_in_48h ≥ 1 且 history_active_days_7d ≥ 1 且 new_counterparty_share_24h ≥ 0.035715 且 onward_currency_switch_48h ≤ 0.5 且 pass_through_ratio_24h ≤ 3.18573
- 新增：fan_in_48h ≥ 1 且 history_active_days_7d ≥ 5 且 new_counterparty_share_24h ≥ 0.035715 且 onward_currency_switch_48h ≤ 0.5 且 pass_through_ratio_24h ≥ 3.18573
- 新增：fan_in_48h ≥ 1 且 new_counterparty_share_24h ≥ 0.035715 且 onward_currency_switch_48h ≥ 1
- 删除：activity_change_24h ≥ 0.961539 且 amount_vs_history_24h ≤ 0.101439 且 history_active_days_7d ≤ 4.5 且 new_counterparties_24h ≤ 0.5
- 删除：activity_change_24h ≥ 0.854168 且 amount_vs_history_24h ≥ 0.10144 且 amount_vs_history_24h ≤ 0.939755 且 history_active_days_7d ≤ 4.5 且 new_counterparties_24h ≤ 0.5
- 删除：activity_change_24h ≤ 0.354167 且 amount_vs_history_24h ≤ 0.939755 且 fan_in_48h ≥ 2 且 history_active_days_7d ≥ 5 且 new_counterparties_24h ≤ 0.5 且 round_trip_48h ≤ 0.5
- 删除：amount_vs_history_24h ≥ 1.00014 且 new_counterparties_24h ≤ 0.5
- 删除：fan_in_48h ≥ 1 且 new_counterparties_24h ≥ 1
- 调整：＋ new_counterparty_share_24h ≤ 0.035714；－ new_counterparties_24h ≤ 0.5
- 调整：＋ new_counterparty_share_24h ≤ 0.035714；－ new_counterparties_24h ≤ 0.5
- 调整：＋ new_counterparty_share_24h ≤ 0.035714；－ new_counterparties_24h ≤ 0.5
- 调整：＋ new_counterparty_share_24h ≤ 0.035714；－ new_counterparties_24h ≤ 0.5
- 调整：＋ new_counterparty_share_24h ≥ 0.035715；－ new_counterparties_24h ≥ 1
- 调整：＋ new_counterparty_share_24h ≥ 0.035715；－ new_counterparties_24h ≥ 1

**规则（13 条，任一成立即告警）**

1. `activity_change_24h` ≤ 0.961538 且 `amount_vs_history_24h` ≤ 0.101439 且 `history_active_days_7d` ≤ 2.5 且 `new_counterparty_share_24h` ≤ 0.035714
2. `activity_change_24h` ≤ 0.854167 且 `amount_vs_history_24h` ≥ 0.10144 且 `amount_vs_history_24h` ≤ 0.939755 且 `history_active_days_7d` ≤ 2.5 且 `new_counterparty_share_24h` ≤ 0.035714
3. `activity_change_24h` ≥ 0.881945 且 `amount_vs_history_24h` ≤ 0.939755 且 `fan_in_48h` ≥ 2 且 `history_active_days_7d` ≥ 5 且 `history_active_days_7d` ≤ 5.5 且 `new_counterparty_share_24h` ≤ 0.035714 且 `round_trip_48h` ≤ 0.5
4. `activity_change_24h` ≥ 0.881945 且 `amount_vs_history_24h` ≥ 0.298659 且 `amount_vs_history_24h` ≤ 0.939755 且 `history_active_days_7d` ≥ 6 且 `new_counterparty_share_24h` ≤ 0.035714 且 `round_trip_48h` ≤ 0.5
5. `amount_vs_history_24h` ≤ 0.939755 且 `history_active_days_7d` ≥ 5 且 `new_counterparty_share_24h` ≤ 0.035714 且 `round_trip_48h` ≥ 1
6. `amount_vs_history_24h` ≥ 0.939756 且 `amount_vs_history_24h` ≤ 1.00014 且 `new_counterparty_share_24h` ≤ 0.035714 且 `round_trip_48h` ≥ 1
7. `amount_vs_history_24h` ≥ 1.00014 且 `new_counterparty_share_24h` ≤ 0.035714
8. `amount_vs_history_24h` ≥ 999.655 且 `fan_in_48h` ≤ 0.5 且 `history_active_days_7d` ≥ 1 且 `history_active_days_7d` ≤ 1.5 且 `new_counterparty_share_24h` ≥ 0.035715
9. `amount_vs_history_24h` ≥ 990.857 且 `fan_in_48h` ≤ 0.5 且 `history_active_days_7d` ≥ 2 且 `new_counterparty_share_24h` ≥ 0.035715
10. `amount_vs_history_24h` ≥ 650.815 且 `fan_in_48h` ≥ 1 且 `history_active_days_7d` ≤ 0.5 且 `new_counterparty_share_24h` ≥ 0.035715 且 `onward_currency_switch_48h` ≤ 0.5 且 `pass_through_ratio_24h` ≤ 3.18573
11. `fan_in_48h` ≥ 1 且 `history_active_days_7d` ≥ 1 且 `new_counterparty_share_24h` ≥ 0.035715 且 `onward_currency_switch_48h` ≤ 0.5 且 `pass_through_ratio_24h` ≤ 3.18573
12. `fan_in_48h` ≥ 1 且 `history_active_days_7d` ≥ 5 且 `new_counterparty_share_24h` ≥ 0.035715 且 `onward_currency_switch_48h` ≤ 0.5 且 `pass_through_ratio_24h` ≥ 3.18573
13. `fan_in_48h` ≥ 1 且 `new_counterparty_share_24h` ≥ 0.035715 且 `onward_currency_switch_48h` ≥ 1

## v5

- 状态：已发布
- 来源：初始训练（决策树在开发集标签上拟合）
- 目标：高召回：验证期链路召回 ≥ 97%，再比告警量与 precision
- 父版本：v4 · 生成时间：2026-10-23 22:56
- 版本 ID：`f0a4baa9-3a14-4c98-a00c-332edcc10b54`
- 训练配置：19 个指标，最多 24 个叶子，最小叶子 50
- 开发集拟合：链路召回 100.0%，选用 12/24 个叶子，合并为 11 条规则
- 门禁结论：接受 · Keeps the chain-recall target with fewer alerts and no lower precision
- 监控期检查：未回滚 · 链路召回 100.0%（父版本 99.9%），每天告警 38,468（父版本 69,363）

| 验证期指标 | 本版本 | 父版本 |
|---|---|---|
| 链路召回 | 100.0% | 99.9% |
| 每天告警账户日 | 27,233 | 58,411 |
| 窗口 precision | 9.2% | 3.7% |

**相对父版本的规则变化**

- 新增 11 条 · 删除 11 条 · 调整 0 条 · 不变 0 条
- 新增：activity_change_24h ≤ 0.961538 且 amount_vs_history_24h ≤ 0.101439 且 history_active_days_7d ≤ 2.5 且 new_counterparties_24h ≤ 0.5
- 新增：activity_change_24h ≥ 0.961539 且 amount_vs_history_24h ≤ 0.101439 且 history_active_days_7d ≤ 4.5 且 new_counterparties_24h ≤ 0.5
- 新增：activity_change_24h ≤ 0.854167 且 amount_vs_history_24h ≥ 0.10144 且 amount_vs_history_24h ≤ 0.939755 且 history_active_days_7d ≤ 2.5 且 new_counterparties_24h ≤ 0.5
- 新增：activity_change_24h ≥ 0.854168 且 amount_vs_history_24h ≥ 0.10144 且 amount_vs_history_24h ≤ 0.939755 且 history_active_days_7d ≤ 4.5 且 new_counterparties_24h ≤ 0.5
- 新增：activity_change_24h ≤ 0.354167 且 amount_vs_history_24h ≤ 0.939755 且 fan_in_48h ≥ 2 且 history_active_days_7d ≥ 5 且 new_counterparties_24h ≤ 0.5 且 round_trip_48h ≤ 0.5
- 新增：amount_vs_history_24h ≤ 0.939755 且 history_active_days_7d ≥ 5 且 new_counterparties_24h ≤ 0.5 且 round_trip_48h ≥ 1
- 新增：amount_vs_history_24h ≥ 0.939756 且 amount_vs_history_24h ≤ 1.00014 且 new_counterparties_24h ≤ 0.5 且 round_trip_48h ≥ 1
- 新增：amount_vs_history_24h ≥ 1.00014 且 new_counterparties_24h ≤ 0.5
- 新增：amount_vs_history_24h ≥ 999.655 且 fan_in_48h ≤ 0.5 且 history_active_days_7d ≥ 1 且 history_active_days_7d ≤ 1.5 且 new_counterparties_24h ≥ 1
- 新增：amount_vs_history_24h ≥ 990.857 且 fan_in_48h ≤ 0.5 且 history_active_days_7d ≥ 2 且 new_counterparties_24h ≥ 1
- 新增：fan_in_48h ≥ 1 且 new_counterparties_24h ≥ 1
- 删除：activity_change_24h ≥ 0.450001 且 activity_change_24h ≤ 0.904545 且 fan_in_48h ≤ 1.5 且 fan_out_48h ≥ 1 且 fan_out_48h ≤ 1.5 且 onward_currency_switch_48h ≤ 0.5 且 out_count_24h ≤ 0.5 且 round_trip_48h ≤ 0.5
- 删除：activity_change_24h ≤ 0.633333 且 fan_in_48h ≥ 1 且 fan_in_48h ≤ 1.5 且 fan_out_48h ≥ 1 且 fan_out_48h ≤ 1.5 且 onward_currency_switch_48h ≤ 0.5 且 out_count_24h ≥ 1 且 round_trip_48h ≤ 0.5
- 删除：activity_change_24h ≤ 0.904545 且 fan_in_48h ≤ 1.5 且 fan_out_48h ≤ 1.5 且 onward_currency_switch_48h ≤ 0.5 且 round_trip_48h ≥ 1
- 删除：activity_change_24h ≥ 0.414287 且 activity_change_24h ≤ 0.904545 且 fan_in_48h ≤ 1.5 且 fan_out_48h ≥ 2 且 onward_currency_switch_48h ≤ 0.5 且 out_banks_24h ≤ 1.5
- 删除：activity_change_24h ≤ 0.904545 且 fan_in_48h ≤ 1.5 且 fan_out_24h ≤ 2.5 且 fan_out_48h ≥ 3 且 onward_currency_switch_48h ≤ 0.5 且 out_banks_24h ≥ 2 且 round_trip_48h ≤ 0.5
- 删除：activity_change_24h ≤ 0.904545 且 fan_in_48h ≤ 1.5 且 fan_out_48h ≥ 2 且 onward_currency_switch_48h ≤ 0.5 且 out_banks_24h ≥ 2 且 round_trip_48h ≥ 1
- 删除：activity_change_24h ≥ 0.414287 且 activity_change_24h ≤ 0.904545 且 fan_in_48h ≥ 2 且 in_count_24h ≤ 1.5
- 删除：activity_change_24h ≤ 0.904545 且 fan_in_48h ≥ 3 且 in_count_24h ≥ 2
- 删除：activity_change_24h ≥ 0.904546 且 currency_count_24h ≤ 1.5 且 fan_in_48h ≥ 1 且 fan_in_48h ≤ 1.5
- 删除：activity_change_24h ≥ 0.904546 且 currency_count_24h ≥ 2 且 fan_in_48h ≤ 1.5
- 删除：activity_change_24h ≥ 0.904546 且 fan_in_48h ≥ 2

**规则（11 条，任一成立即告警）**

1. `activity_change_24h` ≤ 0.961538 且 `amount_vs_history_24h` ≤ 0.101439 且 `history_active_days_7d` ≤ 2.5 且 `new_counterparties_24h` ≤ 0.5
2. `activity_change_24h` ≥ 0.961539 且 `amount_vs_history_24h` ≤ 0.101439 且 `history_active_days_7d` ≤ 4.5 且 `new_counterparties_24h` ≤ 0.5
3. `activity_change_24h` ≤ 0.854167 且 `amount_vs_history_24h` ≥ 0.10144 且 `amount_vs_history_24h` ≤ 0.939755 且 `history_active_days_7d` ≤ 2.5 且 `new_counterparties_24h` ≤ 0.5
4. `activity_change_24h` ≥ 0.854168 且 `amount_vs_history_24h` ≥ 0.10144 且 `amount_vs_history_24h` ≤ 0.939755 且 `history_active_days_7d` ≤ 4.5 且 `new_counterparties_24h` ≤ 0.5
5. `activity_change_24h` ≤ 0.354167 且 `amount_vs_history_24h` ≤ 0.939755 且 `fan_in_48h` ≥ 2 且 `history_active_days_7d` ≥ 5 且 `new_counterparties_24h` ≤ 0.5 且 `round_trip_48h` ≤ 0.5
6. `amount_vs_history_24h` ≤ 0.939755 且 `history_active_days_7d` ≥ 5 且 `new_counterparties_24h` ≤ 0.5 且 `round_trip_48h` ≥ 1
7. `amount_vs_history_24h` ≥ 0.939756 且 `amount_vs_history_24h` ≤ 1.00014 且 `new_counterparties_24h` ≤ 0.5 且 `round_trip_48h` ≥ 1
8. `amount_vs_history_24h` ≥ 1.00014 且 `new_counterparties_24h` ≤ 0.5
9. `amount_vs_history_24h` ≥ 999.655 且 `fan_in_48h` ≤ 0.5 且 `history_active_days_7d` ≥ 1 且 `history_active_days_7d` ≤ 1.5 且 `new_counterparties_24h` ≥ 1
10. `amount_vs_history_24h` ≥ 990.857 且 `fan_in_48h` ≤ 0.5 且 `history_active_days_7d` ≥ 2 且 `new_counterparties_24h` ≥ 1
11. `fan_in_48h` ≥ 1 且 `new_counterparties_24h` ≥ 1

## v4

- 状态：已发布
- 来源：初始训练（决策树在开发集标签上拟合）
- 目标：高召回：验证期链路召回 ≥ 97%，再比告警量与 precision
- 父版本：v3 · 生成时间：—
- 版本 ID：`073e865b-0e77-4bef-a13f-6d104f4c8e6a`
- 训练配置：15 个指标，最多 24 个叶子，最小叶子 50
- 开发集拟合：链路召回 99.9%，选用 12/24 个叶子，合并为 11 条规则
- 门禁结论：接受 · Meets the 97% chain-recall target, which the parent does not
- 监控期检查：未回滚 · 链路召回 99.9%（父版本 47.8%），每天告警 69,363（父版本 6,144）

| 验证期指标 | 本版本 | 父版本 |
|---|---|---|
| 链路召回 | 99.9% | 47.8% |
| 每天告警账户日 | 58,411 | 5,832 |
| 窗口 precision | 3.7% | 9.7% |

**相对父版本的规则变化**

- 新增 9 条 · 删除 1 条 · 调整 2 条 · 不变 0 条
- 新增：activity_change_24h ≥ 0.450001 且 activity_change_24h ≤ 0.904545 且 fan_in_48h ≤ 1.5 且 fan_out_48h ≥ 1 且 fan_out_48h ≤ 1.5 且 onward_currency_switch_48h ≤ 0.5 且 out_count_24h ≤ 0.5 且 round_trip_48h ≤ 0.5
- 新增：activity_change_24h ≤ 0.633333 且 fan_in_48h ≥ 1 且 fan_in_48h ≤ 1.5 且 fan_out_48h ≥ 1 且 fan_out_48h ≤ 1.5 且 onward_currency_switch_48h ≤ 0.5 且 out_count_24h ≥ 1 且 round_trip_48h ≤ 0.5
- 新增：activity_change_24h ≤ 0.904545 且 fan_in_48h ≤ 1.5 且 fan_out_48h ≤ 1.5 且 onward_currency_switch_48h ≤ 0.5 且 round_trip_48h ≥ 1
- 新增：activity_change_24h ≥ 0.414287 且 activity_change_24h ≤ 0.904545 且 fan_in_48h ≤ 1.5 且 fan_out_48h ≥ 2 且 onward_currency_switch_48h ≤ 0.5 且 out_banks_24h ≤ 1.5
- 新增：activity_change_24h ≤ 0.904545 且 fan_in_48h ≤ 1.5 且 fan_out_24h ≤ 2.5 且 fan_out_48h ≥ 3 且 onward_currency_switch_48h ≤ 0.5 且 out_banks_24h ≥ 2 且 round_trip_48h ≤ 0.5
- 新增：activity_change_24h ≤ 0.904545 且 fan_in_48h ≤ 1.5 且 fan_out_48h ≥ 2 且 onward_currency_switch_48h ≤ 0.5 且 out_banks_24h ≥ 2 且 round_trip_48h ≥ 1
- 新增：activity_change_24h ≥ 0.414287 且 activity_change_24h ≤ 0.904545 且 fan_in_48h ≥ 2 且 in_count_24h ≤ 1.5
- 新增：activity_change_24h ≤ 0.904545 且 fan_in_48h ≥ 3 且 in_count_24h ≥ 2
- 新增：activity_change_24h ≥ 0.904546 且 fan_in_48h ≥ 2
- 删除：activity_change_24h ≥ 0.904546 且 activity_change_24h ≤ 1.875 且 currency_count_24h ≥ 2
- 调整：activity_change_24h ≥ 2.125 → 0.904546；＋ fan_in_48h ≥ 1；＋ fan_in_48h ≤ 1.5
- 调整：activity_change_24h ≥ 1.875 → 0.904546；＋ fan_in_48h ≤ 1.5

**规则（11 条，任一成立即告警）**

1. `activity_change_24h` ≥ 0.450001 且 `activity_change_24h` ≤ 0.904545 且 `fan_in_48h` ≤ 1.5 且 `fan_out_48h` ≥ 1 且 `fan_out_48h` ≤ 1.5 且 `onward_currency_switch_48h` ≤ 0.5 且 `out_count_24h` ≤ 0.5 且 `round_trip_48h` ≤ 0.5
2. `activity_change_24h` ≤ 0.633333 且 `fan_in_48h` ≥ 1 且 `fan_in_48h` ≤ 1.5 且 `fan_out_48h` ≥ 1 且 `fan_out_48h` ≤ 1.5 且 `onward_currency_switch_48h` ≤ 0.5 且 `out_count_24h` ≥ 1 且 `round_trip_48h` ≤ 0.5
3. `activity_change_24h` ≤ 0.904545 且 `fan_in_48h` ≤ 1.5 且 `fan_out_48h` ≤ 1.5 且 `onward_currency_switch_48h` ≤ 0.5 且 `round_trip_48h` ≥ 1
4. `activity_change_24h` ≥ 0.414287 且 `activity_change_24h` ≤ 0.904545 且 `fan_in_48h` ≤ 1.5 且 `fan_out_48h` ≥ 2 且 `onward_currency_switch_48h` ≤ 0.5 且 `out_banks_24h` ≤ 1.5
5. `activity_change_24h` ≤ 0.904545 且 `fan_in_48h` ≤ 1.5 且 `fan_out_24h` ≤ 2.5 且 `fan_out_48h` ≥ 3 且 `onward_currency_switch_48h` ≤ 0.5 且 `out_banks_24h` ≥ 2 且 `round_trip_48h` ≤ 0.5
6. `activity_change_24h` ≤ 0.904545 且 `fan_in_48h` ≤ 1.5 且 `fan_out_48h` ≥ 2 且 `onward_currency_switch_48h` ≤ 0.5 且 `out_banks_24h` ≥ 2 且 `round_trip_48h` ≥ 1
7. `activity_change_24h` ≥ 0.414287 且 `activity_change_24h` ≤ 0.904545 且 `fan_in_48h` ≥ 2 且 `in_count_24h` ≤ 1.5
8. `activity_change_24h` ≤ 0.904545 且 `fan_in_48h` ≥ 3 且 `in_count_24h` ≥ 2
9. `activity_change_24h` ≥ 0.904546 且 `currency_count_24h` ≤ 1.5 且 `fan_in_48h` ≥ 1 且 `fan_in_48h` ≤ 1.5
10. `activity_change_24h` ≥ 0.904546 且 `currency_count_24h` ≥ 2 且 `fan_in_48h` ≤ 1.5
11. `activity_change_24h` ≥ 0.904546 且 `fan_in_48h` ≥ 2

## v3

- 状态：已发布
- 来源：规则更新 agent 触发（依据调查结论与标签反馈制定方案）
- 目标：旧目标：每天 200 个告警下的 TP 与 precision
- 父版本：v2 · 生成时间：—
- 版本 ID：`9ff317ec-53ce-4e01-a3f5-243f96501358`
- 训练配置：3 个指标，树深 3，最小叶子 15
- 门禁结论：接受 · More true positives without lower precision at the same daily alert budget
- 监控期检查：未回滚 · TP 1,776（父版本 1,623），precision 63.4%

> 规则更新 agent 的理由：The feature_contrast data shows that transit_gap_hours has the highest true_positive median (7.933) compared to false positives (3.683), indicating it best separates the investigated true positives from false positives across the groups. activity_change_24h and currency_count_24h have lower separation power (1.0 vs 1.0 and 2.0 vs 2.0 respectively). The current rules rely heavily on activity_change

| 验证期指标 | 本版本 | 父版本 |
|---|---|---|
| 告警真正例 TP | 2,694 | 2,508 |
| 告警 precision | 64.1% | 59.7% |

**相对父版本的规则变化**

- 生成于规则对比功能上线之前，没有记录规则级变化。

**规则（3 条，任一成立即告警）**

1. `activity_change_24h` ≥ 1.875 且 `currency_count_24h` ≥ 2
2. `activity_change_24h` ≥ 0.904546 且 `activity_change_24h` ≤ 1.875 且 `currency_count_24h` ≥ 2
3. `activity_change_24h` ≥ 2.125 且 `currency_count_24h` ≤ 1.5

## v2

- 状态：已发布
- 来源：初始训练（决策树在开发集标签上拟合）
- 目标：旧目标：每天 200 个告警下的 TP 与 precision
- 父版本：v0 · 生成时间：—
- 版本 ID：`2cdca6cd-d985-4024-9141-fd4a5d2e18e0`
- 训练配置：15 个指标，树深 3，最小叶子 50
- 门禁结论：接受 · More true positives without lower precision at the same daily alert budget
- 监控期检查：未回滚 · TP 1,623（父版本 70），precision 58.0%

| 验证期指标 | 本版本 | 父版本 |
|---|---|---|
| 告警真正例 TP | 2,508 | 116 |
| 告警 precision | 59.7% | 2.8% |

**相对父版本的规则变化**

- 生成于规则对比功能上线之前，没有记录规则级变化。

**规则（4 条，任一成立即告警）**

1. `activity_change_24h` ≥ 0.904546 且 `currency_count_24h` ≥ 2 且 `fan_in_48h` ≤ 1.5
2. `activity_change_24h` ≥ 0.904546 且 `fan_in_48h` ≥ 3
3. `activity_change_24h` ≤ 0.904545 且 `fan_in_48h` ≤ 1.5 且 `onward_currency_switch_48h` ≥ 1
4. `activity_change_24h` ≥ 0.904546 且 `fan_in_48h` ≥ 2 且 `fan_in_48h` ≤ 2.5

## v1

- 状态：已发布
- 来源：初始训练（决策树在开发集标签上拟合）
- 目标：旧目标：每天 200 个告警下的 TP 与 precision
- 父版本：v0 · 生成时间：—
- 版本 ID：`adf36ac2-9589-40cd-8be3-cd8d97805744`
- 训练配置：15 个指标，树深 3，最小叶子 50
- 门禁结论：接受 · More true positives without lower precision at the same daily alert budget
- 监控期检查：未回滚 · TP 1,623（父版本 70），precision 58.0%

| 验证期指标 | 本版本 | 父版本 |
|---|---|---|
| 告警真正例 TP | 2,508 | 116 |
| 告警 precision | 59.7% | 2.8% |

**相对父版本的规则变化**

- 生成于规则对比功能上线之前，没有记录规则级变化。

**规则（4 条，任一成立即告警）**

1. `activity_change_24h` ≥ 0.905984 且 `currency_count_24h` ≥ 2 且 `fan_in_48h` ≤ 1.5
2. `activity_change_24h` ≥ 0.905984 且 `fan_in_48h` ≥ 3
3. `activity_change_24h` ≤ 0.905983 且 `fan_in_48h` ≥ 3
4. `activity_change_24h` ≥ 0.905984 且 `fan_in_48h` ≥ 2 且 `fan_in_48h` ≤ 2.5

## v0

- 状态：参考规则
- 来源：人工设定的参考阈值
- 目标：—
- 父版本：无 · 生成时间：—
- 版本 ID：`9116b47a-3a6b-400a-aa31-63273a387e1a`

**规则（1 条，任一成立即告警）**

1. `fan_in_24h` ≥ 3 且 `fan_out_24h` ≥ 3 且 `transit_gap_hours` ≤ 24

共 8 个已发布或参考版本，3 个暂缓候选。
