# Dashboard prototype

Design of the dashboard (pen.dev), implemented 1:1 in `apps/dashboard`.

| File                                   | Screen                                                                                                             |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `prototype/1-rules.png`                | Rules: rule tree and rule cards (conditions, AUSTRAC text)                                                         |
| `prototype/2-alert-tracing.png`        | Alert investigation: validation metrics, filters, and alert list                                                   |
| `prototype/3-alert-detail-drawer.png`  | Alert detail drawer: matched rules, risk-tiering rationale, relationship-network tracing, and investigation replay |
| `prototype/4-rule-evolution.png`       | Rule evolution: feedback loop, risk-tiering agent, investigation agent, and rule update agent × data-change chain  |
| `prototype/5-tool-design-expanded.png` | Investigation agent tool design expanded view                                                                      |
| `prototype/prototype.pdf`              | All pages above                                                                                                    |

The prototype shows sample values; the implemented pages read every number from the exported data.
Later changes made directly in code (rule version record moved to rule evolution, timeline replay,
AND/OR markers) are not drawn here.
