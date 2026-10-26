# Dashboard prototype

Design of the dashboard (pen.dev), implemented 1:1 in `apps/dashboard`.

| File | Screen |
|---|---|
| `prototype/1-rules.png` | 规则：规则树、规则卡（条件、AUSTRAC 原文） |
| `prototype/2-alert-tracing.png` | 告警追查：验证期指标、筛选、告警列表 |
| `prototype/3-alert-detail-drawer.png` | 告警详情抽屉：命中规则、风险分级原因、关系网追查、调查回放 |
| `prototype/4-rule-evolution.png` | 规则进化：闭环、风险分级 agent、调查 agent、规则更新 agent × 数据变化链条 |
| `prototype/5-tool-design-expanded.png` | 调查 agent「工具设计」展开态 |
| `prototype/prototype.pdf` | 以上全部页面 |

The prototype shows sample values; the implemented pages read every number from the exported data.
Later changes made directly in code (rule version record moved to rule evolution, timeline replay,
AND/OR markers) are not drawn here.
