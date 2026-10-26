# Dashboard data fields

The dashboard reads one file, `/dashboard/data.json`. The full API and `serve-dashboard` serve
`.runtime/dashboard/data.json` when it exists, otherwise the committed snapshot `apps/dashboard/snapshot/data.json`.

```bash
PYTHONPATH=services/aml-api .venv/bin/python -m aml_agent.cli showcase-metrics      # chain-based validation metrics (Zetaris)
PYTHONPATH=services/aml-api .venv/bin/python -m aml_agent.cli dashboard --snapshot  # export; --snapshot also updates the committed copy
```

Labels are used only for evaluation after the fact; no agent sees them. Rates are fractions (0.25 = 25%).

## Top level

| Key | Used by | Content |
|---|---|---|
| `generated_at` | sidebar | Export time (epoch seconds) |
| `dataset.window` | feature modal | Definition of the alert window |
| `features{name}` | rules, drawer | `zh`, `calc`, `formula`, `description`, `unit`, `policy[]`, `table`, `distribution` (development p50/p90/p99) |
| `policies{id}` | rules | AUSTRAC indicator: `text` (original), `zh`, `section`, `url` |
| `versions[]`, `active_version` | rules, learning loop | Rule versions: `rules` (groups, atoms, tree), `validation`, `parent_validation`, `plan` (incl. the rule update agent's `rationale`), `fit`, `diff`, `trigger`, `status` |
| `evaluation` | learning loop | `tiering` (risk grading agent vs reference model, per tier), `investigation_rounds{name}`, `feature_proposals[]` |
| `showcase` | learning loop, drawer | Reasoning of the three agents (below) |
| `trace` | alert tracing | Validation headline volumes and the investigated cases with their replay (below) |

## `trace`

`validation`: `period`, `days`, `transactions`, `laundering_transactions`, `in_scope_*` (ACH/Wire), `rule_version`,
`alerts_per_day`, `chain_recall`.

`cases[]` — one per finished validation investigation:

| Field | Meaning |
|---|---|
| `key`, `round`, `account`, `day` | Run id, investigation round, alerted account, alert day |
| `rules[]` | Rule groups that hit |
| `tier` | Risk grading agent's verdict: `tier`, `name`, `priority`, `reason`, `conditions[]` (`feature`, `op`, `value`, `actual`) |
| `decision` | Senior investigator's decision (report / review / dismiss); the final risk level is `detail.senior.risk_level` |
| `chain_accounts` | Accounts judged suspicious on the reconstructed chain |
| `detail.hit_rules[]` | Hit groups with each atom's threshold and value |
| `detail.team_plan`, `detail.members[]` | Senior investigator's split; each investigator's `role`, `question`, `conclusion`, `brief_reason` |
| `detail.steps[]` | Every investigator action in time order: `step`, `member`, `tool`, `reasoning`, `expected`, `found[]`, `transactions[]` (drawn at this step) |
| `detail.nodes[]`, `detail.edges[]` | Network: nodes carry `role` (focus / upstream / downstream / peer); edges carry `step` and `member` (who found them; step 0 = prefetched alert context) |
| `detail.accounts[]` | Account verdicts: `verdict` (suspicious / cleared / insufficient / not_examined; unresolved / flagged / observed before the team concludes), `reason`, `found_by`, `tools` |
| `detail.merge` | Merged verdict: `conclusion`, `brief_reason`, `chain`, `suspect_transaction_ids` |
| `detail.senior` | `prior_level`, `adjustment`, `risk_level`, `decision`, `rationale`, `evidence_flags[]` (flag, account, transaction id), `key_evidence`, `account_risks` |
| `evaluation` | After the fact: `new_chain_accounts` (chain accounts the rules never alerted on) |

## `showcase`

| Key | Content |
|---|---|
| `rule_versions[]` | Chain-based validation metrics per published version: `chain_recall`, `chain_precision`, `full_chain_account_recall`, `window_precision`, `flagged_per_day` |
| `tier_agent` | Primary grading policy: `label`, `rationale`, `segments[]`, `patterns[]`, `investigation_evidence`, `rounds[]` (`analysis`, `rationale`, `high_rules`, `high`, `capacity`, `chosen`) |
| `investigation_agent` | `rounds[]` (`change`/`change_en`, `exclusion_rate`, `chain_accounts_found`, `chain_accounts_in_files`, …) and `tools[]` (`uses`, `found`, `rate` in laundering cases) |
| `rule_updates[]` | Per agent-made version: `inputs` (cases, per-rule outcomes, feature contrast), `reasoning`, `diff_counts`, `effect.before/after` |

## Alert list (`/dashboard/api/alerts`)

Every validation alert of the active rule version, graded by the primary policy, from the SQLite index
(`cli alert-index`; committed as `apps/dashboard/snapshot/alerts.sqlite.gz`, unpacked on first use). Served by both
the full API and `serve-dashboard`.

`GET /dashboard/api/alerts?page=&q=&day=&tier=&rule=&status=` (status: `investigated` / `running` / `failed` / `pending`) returns
`total`, `page`, `pages`, `size` (20) and `rows[]`: `account`, `day`, `tier`, `tier_rule`, `priority`, `rules[]`, `features{}`,
`status`, `error` (why a live investigation failed) and, on the full API, `case` for an alert decided by a live investigation.
Highest risk first; within a tier, investigated alerts first. `GET /dashboard/api/alerts/summary`:
alerts per tier, days, rule groups and the index metadata (rule version, grading policy).

## Live investigation (full API only)

`POST /dashboard/api/live/investigate {alert_id}` (a stored alert id, or `alert:<bank>:<account>:<day>` from the alert list),
`GET /dashboard/api/live/runs/{run_id}` (`phase`, `outcome` investigated / running / failed, `attempt` of `max_attempts`,
`retried_by`, `setup_log`, `error`, `elapsed_seconds`, plus `detail` in the shape of a case's detail), `GET /dashboard/api/live/for?account=&day=`
(the latest live run of an alert, so reopening it resumes). Validation alerts only, at most two live runs at once; decided live runs
join `trace.cases` on the next export. The API token never reaches the browser.
