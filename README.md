# AML Investigation Agent

An explainable anti-money laundering investigation system running on IBM HI-Large v8 (1.8 billion transactions provided on an Azure VM by Zetaris). Three agents collaborate, and investigation evidence feeds back into the rule system:

```
Rules (decision tree, link recall ≥ 97%) → Alerts
  → Risk-tiering agent: ranks alerts into high / medium / low and assigns priority
  → Investigation agent: senior investigator assigns tasks → multiple investigators trace upstream/downstream in parallel → merge results → senior investigator adjudicates (evidence list)
  → Rule update agent: summarizes findings from investigations, tightens rules, proposes new features → new rule version (validation-set gate)
```

The models use NVIDIA Nemotron (Ultra handles tiering, adjudication, and rule updates; Lightning handles investigators). The Meterless engine is responsible for the investigation loop (Markovian), case memory (H-MEM), and the shared case graph for multiple investigators (World Model). Investigators do not see labels; labels are used only on the evaluation side; the test set remains closed.

Data: IBM AML synthetic transaction dataset HI-Large v8 ([Kaggle](https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml), CDLA-Sharing-1.0).

## First, decide what you want to do

| You want to…                                                                                                                      | What you need                                                            | See section                                                                                  |
| --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| Browse the dashboard: rules, alert investigation (relationship network and replay for investigated cases), and rule self-learning | Only Python; no keys or data required                                    | [1. Just view the dashboard](#1-just-view-the-dashboard-no-keys-required)                    |
| Click "Investigate Now" on the dashboard to let the investigation team investigate an alert in real time                          | Your own NVIDIA API key + accessible Zetaris data source (either option) | [2. Live investigation](#2-live-investigation-investigate-now)                               |
| Re-learn rules, re-tier risk, and recompute metrics                                                                               | NVIDIA key + the full 1.8B dataset on the team VM                        | [3. Learning and recomputation](#3-learning-and-recomputation-requires-team-vm-full-dataset) |

All data queries are executed through Zetaris (the project sponsor) via REST SQL. Real-time investigation data can come from two sources, with identical query logic:

- **Option A: Connect directly to the team VM's Zetaris**. No data download is required; you need VM access and a Zetaris API key from the project lead.
- **Option B: Run Zetaris locally and load the local data package**. The package is about 0.9 GB and contains 13.5 million validation-period transactions and features. It does not depend on the VM, but requires Docker (8 GB memory).

## 1. Just view the dashboard (no keys required)

The repository includes a dashboard data snapshot: `apps/dashboard/snapshot/data.json` contains rule versions, investigated cases, and rule self-learning data, while `alerts.sqlite.gz` contains all 350,000 alerts in the validation period and their risk tiers; it is automatically decompressed on first launch. Only Python 3.11+ is required:

```bash
git clone -b open-source git@github.com:YeSheng-Vibe-Coder/demo-repository.git aml-agent && cd aml-agent
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m aml_agent.cli serve-dashboard
```

After the terminal prints the address (about 20 seconds on first run), open <http://127.0.0.1:8020/dashboard>. If the port is occupied, add `--port 8030`. In the lower-left corner, you can switch between Chinese / English and day / night mode.

| Page                | Content                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Alert investigation | Validation-period transaction volume, money laundering transactions, alert volume, and link recall; full alert list (sorted by risk level, filterable by account / date / level / rule / investigation status). Click a row to open details: matching rules, tiering rationale, investigation conclusion and account determination, relationship network tracing (colored by investigator), and investigation replay timeline |
| Rule self-learning  | Alert → risk tiering → investigation → evidence → learning → closed loop for new rules; each round of decisions and effects for the three agents                                                                                                                                                                                                                                                                              |
| Rules               | Current rules (decision tree + rule cards with AUSTRAC text and metric formulas), with historical versions available                                                                                                                                                                                                                                                                                                          |

In this read-only dashboard, clicking "Investigate Now" will prompt you to complete the setup in section 2 first.

## 2. Live investigation ("Investigate Now")

### 2.1 Shared setup (required for both A and B)

1. Complete the setup in section 1 (clone, `.venv`, `pip install -e .`).
2. Install **Node.js 22.7 or later** (the investigation team's orchestrator `services/orchestrator` runs on it; no `npm install` is required).
3. Request a **NVIDIA API key**: sign up at <https://build.nvidia.com> and apply for a free one. Investigators, senior investigators, risk-tiering agent, and rule update agent all call Nemotron with it.
4. Copy the config template: `cp .env.example .env.local`, then fill in `NVIDIA_API_KEY`. Fill in the three Zetaris settings based on option A or B below. `.env.local` is already ignored by Git; **do not commit it and do not send it to chat groups**.

### 2.2 Option A: Connect directly to the team VM

Suitable for members who do not want to download data and who can access the VM.

1. Ask the project lead for two things (send privately, do not write them into the repo):
   - **VM SSH access**: send your SSH public key (`~/.ssh/id_ed25519.pub`) to the lead so it can be added to the VM. The lead will tell you the username and address.
   - **Zetaris API key and Org ID**.
2. Open an SSH tunnel and forward the VM's Zetaris REST port to local port 8888, keeping this terminal open:

   ```bash
   ssh -N -L 8888:localhost:8888 <username>@<VM address>
   ```

3. Fill in `.env.local` as follows:

   ```bash
   ZETARIS_REST_URL=http://localhost:8888/api/v1.0
   ZETARIS_API_KEY=<key provided by the lead>
   ZETARIS_ORG_ID=<Org ID provided by the lead>
   ```

4. Continue to [2.4 Start](#24-start-and-investigate).

### 2.3 Option B: Local Zetaris + local data package

Suitable for members who want to run everything on their own machine. The data package covers only the portion needed for real-time investigation:

- Validation-period alerts (2022-09-05 to 09-25), plus the previous 30 days of history (baseline comparison and new-counterparty checks look back 30 days);
- A total of 13.5 million ACH/Wire transactions, **without the money-laundering label column**;
- Feature shards for those days, along with the rule versions, tiering strategy, and calibration data needed for investigation.

After setup, the table names, paths, and VM are exactly the same on local Zetaris (`aml.transactions`, `IBM_AML.agent_day_*`); the application does not switch logic.

**Step 1: Install Zetaris.** Download `zetaris-platform.zip` from the Zetaris Cloud download page (hackathon account), and follow the README in the archive:

```bash
unzip zetaris-platform.zip && cd zetaris-platform
docker login zetarishackathon.azurecr.io   # paste the login command provided on the download page
./preflight.sh                             # check Docker memory (requires 8 GB) and other prerequisites
docker compose pull
docker compose up -d                       # first start takes 5-10 minutes
./verify.sh                                # all 9 checks must pass
```

**Step 2: Download the data package and place it into Zetaris's data volume.** Return to this project directory:

```bash
mkdir -p .runtime/local-data
curl -L -o .runtime/local-data/aml-local-pack.tar \
  https://github.com/YeSheng-Vibe-Coder/demo-repository/releases/download/local-pack-v1/aml-local-pack.tar
tar -xf .runtime/local-data/aml-local-pack.tar -C .runtime/local-data

# Copy into Zetaris's customer-data volume (container path /home/zetaris/customer-data/shared/ibm) and hand it to the Zetaris user
docker run --rm -v zetaris_customer-data:/data -v "$PWD/.runtime/local-data/aml-local-pack/ibm:/pack:ro" alpine \
  sh -c 'mkdir -p /data/shared/ibm && cp -R /pack/. /data/shared/ibm/ && chown -R 1001:1001 /data/shared/ibm'
```

**Step 3: Obtain your local Zetaris API key.** Open <http://localhost:3000>, log in with `admin@zetaris.com` / `Zetaris1@` (recommended to change the password afterward), generate an API key in the UI, and record the Org ID. Then fill in `.env.local`:

```bash
ZETARIS_REST_URL=http://localhost:8888/api/v1.0
ZETARIS_API_KEY=<key you just generated>
ZETARIS_ORG_ID=<your Org ID>
```

**Step 4: Register the data and verify it.**

```bash
.venv/bin/python -m aml_agent.cli local-pack install
```

This does three things:

- Registers `AML.transactions` and the three `IBM_AML.agent_day_*` feature tables in Zetaris (Lightning FILESTORE, Parquet);
- Merges the investigation-required state into `.runtime/`;
- Uses a Zetaris row-count query to compare against the VM result recorded in the data package.

If the output contains `"verified": true`, the data matches the VM state (13,505,048 transactions). You can run this repeatedly.

### 2.4 Start and investigate

```bash
.venv/bin/python -m aml_agent.cli status   # check whether the NVIDIA roles and Zetaris are reachable (does not print secrets)
.venv/bin/python -m aml_agent.cli serve    # API + dashboard
```

Open <http://127.0.0.1:8010/dashboard> (note the port is **8010**, not the 8020 read-only dashboard). In the "Alert Investigation" screen, click any validation-period alert, then click "Investigate Now". The details view displays progress in real time:

- Preparation stage: prefetch evidence, assign tasks to senior investigators, roughly 1-2 minutes;
- Each step taken by each investigator;
- Final merged conclusion and senior investigator adjudication.

A single investigation usually takes about ten minutes, and at most two can run at once. Failures automatically retry up to 2 times and are marked with the reason in the list. After closing the detail panel, the list status shows "Investigating / Investigated / Failed".

### 2.5 Common issues

| Symptom                                                          | Cause and fix                                                                                                                           |
| ---------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| Clicking "Investigate Now" prompts that configuration is missing | `.env.local` is missing `NVIDIA_API_KEY` or `ZETARIS_API_KEY`; restart `serve` after fixing it                                          |
| `status` shows `zetaris_datasources` is not 200                  | Option A: tunnel is not open or was disconnected; rerun `ssh -N -L ...`. Option B: Zetaris was not started correctly; run `./verify.sh` |
| `local-pack install` reports `verified: false`                   | Step 2 copy did not complete or the path was wrong; rerun the `docker run ... cp` step and then run install again                       |
| Local queries are slow                                           | Give Docker at least 8 GB of memory; see Zetaris README section "Tuning the query engine"                                               |
| Investigation shows "Failed" because the model call failed       | Usually due to NVIDIA rate limiting or network issues; the system retries automatically, or you can try again later                     |

## 3. Learning and recomputation (requires the full team VM dataset)

The following commands require the complete 1.8 billion transaction dataset and evaluation labels and can only connect via option A to the team VM:

```bash
export PYTHONPATH=services/aml-api
.venv/bin/python -m aml_agent.cli learning-cycle --investigator-round val-r4 --split validation --per-cell 4   # one round of validation-period investigation
.venv/bin/python -m aml_agent.cli learning-cycle --tier-agent --tier-patterns   # risk-tiering agent learns and publishes the tiering policy
.venv/bin/python -m aml_agent.cli learning-cycle --update-from dev-r3           # rule update agent updates rules based on investigation results
.venv/bin/python -m aml_agent.cli showcase-metrics                             # recompute validation-period link metrics for the dashboard
.venv/bin/python -m aml_agent.cli alert-index --snapshot                       # export all validation-period alerts (using the current primary tiering) and update the compressed snapshot
.venv/bin/python -m aml_agent.cli dashboard --snapshot                         # export dashboard data and refresh the repository snapshot
```

To recreate the local data package (project lead):

```bash
AML_VM_SSH="ssh -i <private key> <username>@<VM address>" scripts/export_local_pack.sh   # export transactions and feature shards from the VM
.venv/bin/python -m aml_agent.cli local-pack build                            # package and verify the row count against the VM Zetaris record
gh release upload local-pack-v1 .runtime/local-data/aml-local-pack.tar --clobber
```

Testing:

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
node --experimental-transform-types --test services/orchestrator/worker.test.ts
```

## Directory structure

```text
apps/dashboard/              dashboard frontend (plain JS + SVG); snapshot/ contains the committed data snapshot
services/aml-api/aml_agent/  backend: API, CLI, investigation workflow, three agents, Zetaris tools, evaluation, and dashboard data
services/orchestrator/       TypeScript orchestration: team.ts (investigation team), worker.ts (single investigator)
vendor/meterless/            pinned Meterless engine (Markovian, H-MEM, World Model) and license
docs/                        specifications, runbooks, dashboard field descriptions, and rule version records; docs/archive contains historical proposals
schemas/                     JSON Schema exported from Pydantic contracts (tests guarantee this remains consistent with code)
tests/                       contract, boundary, and workflow tests
evaluations/                 frozen time splits (development / validation / test-closed / monitoring)
sources/                     policy source snapshots and data-source records
scripts/                     maintenance scripts: exporting local data packages from the VM
analysis/                    early experiments and raw receipts (preserved by version)
deploy/colab/                notebook for self-hosting Nemotron Lightning on Colab GPU
.runtime/                    private runtime state, evaluation labels, and exported data (Git ignored)
```

## Code entry points (`services/aml-api/aml_agent/`)

| Responsibility                         | Module                                                                                                                                                                                                                     |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API, CLI, state ledger                 | `api.py`, `cli.py`, `store.py`                                                                                                                                                                                             |
| Dashboard data and read-only services  | `dashboard.py` (export), `alert_trace.py` (alert investigation and replay), `showcase.py` (reasoning and metrics for three agents), `dashboard_server.py`, `live.py` (live investigation)                                  |
| Rules: DSL, learning, versioning       | `rule_program.py`, `rule_learning.py`, `rule_versions.py`, `rule_changelog.py`, `chains.py` (money-laundering chains and link recall)                                                                                      |
| Risk-tiering agent                     | `tier_agent.py` (policy learning and publishing), `tiering.py` (reference model)                                                                                                                                           |
| Investigation agent                    | `workflow.py` (investigation loop, team coordination, and merge), `adjudication.py` (senior investigator review), `investigator_training.py`, `contracts.py`, `digest.py`, `candidates.py`, `harness.py`, `calibration.py` |
| Rule update agent and closed loop      | `learning_cycle.py`, `feature_proposals.py`                                                                                                                                                                                |
| Zetaris queries, features, and tooling | `zetaris.py`, `feature_store.py`, `feature_registry.py`, `tools.py`, `evidence.py`, `local_pack.py` (local data package)                                                                                                   |
| Evaluation, models, and config         | `evaluation.py`, `providers.py`, `profile.py`, `settings.py`                                                                                                                                                               |

## More documentation

- [Runbook](docs/operations/RUNBOOK.md): keys, commands, feature store, investigation modes, and limitations.
- [Dashboard data fields](docs/dashboard/DATA_FIELDS.md): the meaning of each field in `data.json`.
- [Rule version history](docs/rules/RULE_VERSIONS.md): the source, rules, and impact of each version.
- [Technical specification](docs/architecture/AML_Agent_Technical_Spec_v1.0.md), [execution harness](docs/architecture/EXECUTION_HARNESS.md), and [configuration](docs/architecture/AML_Agent_Config_v1.0.json) (the single source of parameters).
