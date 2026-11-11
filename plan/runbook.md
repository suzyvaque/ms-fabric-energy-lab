# Runbook: Contoso Energy on Microsoft Fabric

Sample, not production guidance (see [DISCLAIMER](../DISCLAIMER.md)). Status legend: â¬œ todo Â· ðŸŸ¨ in progress Â· âœ… verified in dev Â· ðŸš€ promoted to prod.

**Tool key**: *Agent* = GitHub Copilot agent mode in VS Code Â· *Core MCP* = Fabric Core MCP (remote, tenant ops) Â· *Fabric MCP* = Fabric MCP (local, item definitions/API specs) Â· *Skills* = Skills for Fabric Â· *fab* = Fabric CLI Â· *Portal* = Fabric/Azure/GitHub UI.
**Capture key**: *Git* = file in this repo Â· *Script* = idempotent script in `/infra` re-creates it Â· *Checklist* = manual step recorded in [checklists](checklists/).

## Prerequisites on your machine
`git`, Python 3.12+, Azure CLI, GitHub CLI (`winget install GitHub.cli`), then:
```powershell
py -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r infra/requirements.txt
pre-commit install
Copy-Item .env.example .env   # fill in; never commit
```

---

## Step 0: Prereqs and platform âœ… (issue #1, PRs #15, #16)

| # | Task | Tool | Git-tracked? | Capture |
|---|---|---|---|---|
| 0.1 | Per-repo Git identity (no-reply email), SSH alias `github-personal`, test commit | git / ssh (local) | Hook only (`tools/check_git_identity.py`) | Checklist Â§A |
| 0.2 | Scaffolding: README, LICENSE, DISCLAIMER, .gitignore, .env.example, gitleaks + pre-commit | Agent | âœ… Git | n/a |
| 0.3 | `dev` branch, secret scanning + push protection, main protection (PR only, admins included, `gitleaks` check), environments, issues | gh â†’ `infra/github_setup.py repo envs issues` | Script yes, settings no | Script |
| 0.4 | Paid F SKU capacity (+ RG) | fab (ARM) â†’ `infra/capacity.py create` | Script | Script |
| 0.5 | Tenant settings (Copilot, data agent, ontology, GitHub sync, SP APIs, workspace monitoring, certification) | fab api â†’ `infra/tenant_settings.py [--apply]`; Core MCP to inspect; Portal for anything unmatched | No (tenant state) | Script + Checklist Â§B |
| 0.6 | Entra groups, CI/CD SP + OIDC federated credentials (no secret) | az â†’ `infra/entra_setup.py` | Script | Script |
| 0.7 | Domains Fleet/Asset/Finance; 6 workspaces `ce-{fleet,asset,finance}-{dev,prod}`; capacity + domain assignment; workspace roles | fab â†’ `infra/fabric_setup.py` (Core MCP equivalent for ad-hoc checks) | Script + `infra/config/lab.json` | Script |
| 0.8 | Fabric GitHub connection (fine-grained PAT, this repo, Contents RW) + Git connect **dev** workspaces â†’ `dev:/workspaces/ce-*` | fab api â†’ `fabric_setup.py --only git` | Script; PAT in .env only | Script + Checklist Â§C (PAT creation) |
| 0.9 | GitHub secrets (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, env-scoped `FABRIC_WS_*`) | gh â†’ `infra/github_setup.py secrets` | No | Script |

Run order:
```powershell
az login --tenant <tenant>; fab auth login
py infra/capacity.py create
py infra/entra_setup.py
py infra/tenant_settings.py            # review, then --apply
py infra/fabric_setup.py
gh auth login                          # personal account
py infra/github_setup.py all
```
**Verify**: `git log -1 --format=%ae` shows the no-reply address; push over `github-personal` succeeds; a direct push to `main` is rejected; `fab ls` shows 6 workspaces on the capacity; dev workspaces show *Source control â†’ dev*; `fabric-deploy` on main is green (no items yet means a no-op).

**Lessons from the first run**
- `tenant_settings.py` must exit 0 before `fabric_setup.py --only git`. With *GitHub sync* off, Git connect fails with `403 FeatureNotAvailable`. Tenant setting changes take up to ~15 min to propagate.
- New GitHub repos send an ID-based OIDC subject (`repo:<owner>@<ownerId>/<repo>@<repoId>:environment:<env>`). `entra_setup.py` registers both the ID-based and name-based subjects (D-006). Without the ID-based one, `azure/login` fails with `AADSTS700213`.

**Flags**
- **Paid F SKU required**. Copilot, data agents and ontology do not run on a Fabric trial capacity. F2 is enough for steps 0â€“5. At F2â€“F32, Power BI report *consumers* need Pro/PPU licenses; free viewers need F64+.
- Preview: data agent, ontology (Fabric IQ), and the Admin API "update tenant setting". Check status when you build.
- Service principals can't use some Git-integration and Copilot APIs. Git connect runs as you (user); prod deploy runs as the SP.

---

## Step 1: Data ðŸŸ¨

### 1a Mock data âœ… (issue #2)
| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Generator package `data-gen/contoso_gen` (batch + stream), tests, event contract | Agent (Python, stdlib) | âœ… Git | n/a |
| Generate history locally | `py -m contoso_gen batch` | Output gitignored (`data-gen/out`) | Re-creatable from seed |
| Unit/contract tests | pytest locally + `tests` workflow | âœ… Git | n/a |

```powershell
cd data-gen; py -m pytest -q
py -m contoso_gen batch --sites 200 --days 7          # ~1.5M telemetry events, master + finance tables
py -m contoso_gen stream --sites 20 --interval-sec 2 --duration-min 1 --sink file --inject INVERTER_FAULT:SITE-000003
```
**Verify**: 8 tests pass; `out/summary.json` shows duplicates, late events, schema v2 events and all 6 anomaly types; PV is 0 at local night; SOC stays within 10â€“100 %; emails are only `example.com`.
**Flags**: none. Runs locally, no Fabric capacity used. Event contract and injected behaviour: [data-gen/README.md](../data-gen/README.md).
**Fabric Git commit**: n/a (no Fabric items in 1a).

### Remaining
- **1b Ingestion comparison** âœ… (issue #3): see [ingestion-comparison.md](ingestion-comparison.md). Recommendation D-009: **Eventstream custom endpoint â†’ Eventhouse direct ingestion**.

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Event Hubs namespace/hub/consumer group, Entra RBAC (SAS off) | az â†’ `infra/eventhubs.py create` | Script | Script |
| `eh_fleet` Eventhouse, `lab_compare` KQL DB, workspace identity, Event Hubs connection, 2 eventstreams; custom endpoint key â†’ `.env` | Fabric REST â†’ `infra/rti_lab.py create` (Fabric MCP for definition schemas) | âœ… items committed **from Fabric** to `dev:/workspaces/ce-fleet` | Script + Git |
| Tee benchmark + KQL scoring | `py data-gen/benchmarks/ingest_compare.py` | Script âœ…; results JSON gitignored | Numbers copied into the comparison doc |
| CU per eventstream (Capacity Metrics app) | Portal | No | Checklist in ingestion-comparison.md |
| Prod | fabric-cicd deploys `eh_fleet`; `lab_*` excluded (`infra/deploy.py`) | âœ… | n/a |

```powershell
py infra/eventhubs.py create; py infra/rti_lab.py create
cd data-gen; py -m pip install -r requirements.txt; py benchmarks/ingest_compare.py
py infra/rti_lab.py pause             # after the benchmark: lab eventstreams cost ~1 CU each while running
py infra/teardown.py --lab --yes      # when done: lab items + Event Hubs (~$22/month at 1 TU)
```
**Flags**:
- **Preview:** the Event Hubs *Extended* source with workspace identity; custom endpoint Entra ID auth.
- **Licensing/capacity:**
  - Workspace identity needs an F SKU, not a trial.
  - Event Hubs needs an Azure subscription.
  - Each eventstream with a processor uses â‰ˆ 1 CU continuously (â‰ˆ 50 % of an F2). Pause the lab streams when idle.

**Notes**:
- Fabric Git serializes dev workspace/item/connection GUIDs into definitions. These are identifiers, not secrets.
- A failed KQL DB create can leave an orphan database with the same pretty name. That breaks name-based queries and destinations, so use unique item names and query by database GUID.

- **1c Eventhouse medallion** âœ… (issue #4)

```
devices â”€â–¶ es_fleet (custom endpoint, direct ingestion, no processor)
        â”€â–¶ bronze_events (raw + envelope, streaming ingestion, 7d / hot 1d)
        â”€update policyâ”€â–¶ silver_telemetry (typed, v1/v2 â†’ kW, late/future/outlier flags, 30d / hot 7d)
                       â”€â–¶ silver_alerts (90d)
        â”€MV take_any by event_id (lookback 6h)â”€â–¶ silver_telemetry_dedup / silver_alerts_dedup
        â”€MV over MVâ”€â–¶ gold_device_latest (arg_max per device) Â· gold_site_15m (sums + counts, 365d / hot 31d)
        â”€functionsâ”€â–¶ Fleet API: FleetDeviceStatus, FleetOfflineSites, FleetOpenAlerts, FleetKpis,
                     FleetSiteEnergy, FleetBatteryAnomalies, FleetInverterFaults, FleetDataQuality (+ GoldSiteProfile)
```
| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| KQL sources (bronze/silver/gold/API) | Agent; Fabric MCP for KQL DB definition rules | âœ… `infra/kql/eh_fleet/*.kql` (source of truth) | n/a |
| Apply to `eh_fleet` via item definition | Fabric REST â†’ `infra/fleet_medallion.py apply` | âœ… Fabric commits `DatabaseSchema.kql` to `workspaces/ce-fleet/...` | Script + Git |
| `es_fleet` eventstream (custom endpoint â†’ direct ingestion) | Fabric REST â†’ `fleet_medallion.py eventstream` | âœ… (prod IDs rebound by `workspaces/ce-fleet/parameter.yml`) | Script + Git |
| History + dims | `contoso_gen batch` + `replay`; `fleet_medallion.py load-dims` (inline ingest) | Script (data not in Git) | Script |
| Verification | `fleet_medallion.py verify` (10 checks + recall vs ground truth) | Script | Output below |

```powershell
py infra/fleet_medallion.py apply; py infra/fleet_medallion.py eventstream
cd data-gen; py -m contoso_gen batch --sites 100 --days 2; py -m contoso_gen replay --target 'fleet=conn:ESTREAM_FLEET_CONNECTION_STRING'
py infra/fleet_medallion.py load-dims; py infra/fleet_medallion.py verify
```
**Verified (dev)**:
- **Volumes:** bronze = silver = 183,230 telemetry rows; dedup removed 0.47 % duplicates.
- **Schema v2:** normalized, with 0 null `pv_kw`.
- **Data quality:** 1.07 % late events flagged.
- **Gold:** 384/384 devices have a latest reading; 19,170 15-minute site bins; average yield 1.56 kWh/kWp/day (late autumn).
- **Detection recall vs ground truth:**
  - battery overtemp 3/3;
  - inverter faults 4/4;
  - stuck SOC 1/3. The 2 misses sat at 10 % reserve with 0 kW, where a stuck reading looks exactly like a correct one, so they're undetectable from the data.

**Flags**:
- Direct ingestion plus update policies run in Eventhouse, with no Eventstream processor CU.
- The MVs and update policies are GA.
- An Eventhouse on a small F SKU is always on (uptime CU). Use "minimum consumption" only when you need guaranteed performance.

**Gotchas**:
- `DatabaseSchema.kql` only accepts `create-or-alter` for MVs, so there's no backfill. When an MV's aggregation schema changes: drop it, recreate it once with `.create async materialized-view with (backfill=true)`, then re-run `apply` to restore its policies.
- `avgif` yields NaN for empty groups, so store sums and counts.
- `dcount` is approximate, so use exact two-stage counts for duplicate percentages.
- **1d Finance lakehouse medallion** âœ… (issue #5)

```
loan servicing (simulated OLTP) â”€parquet change filesâ”€â–¶ mir_loan_servicing (OPEN MIRRORING, landing zone)
                                                          â”‚ Tables/dbo/customers|contracts|payments (Delta)
lh_asset (Asset domain) â—€â”€Load-to-Tableâ”€ asset CSVs       â”‚
   â”‚ Tables/sites                                         â–¼
   â””â”€â”€OneLake shortcutâ”€â”€â–¶ lh_finance  Tables/ref_sites  Â·  bronze_customers|contracts|payments (shortcuts, zero copy)
   credit bureau CSVs â”€OneLake APIâ”€â–¶ Files/landing/credit_bureau â”€â–¶ bronze_credit_bureau
                         nb_finance_medallion (PySpark) â”€â–¶ silver_customers, silver_contracts, silver_payments (dedup),
                                                           silver_customer_credit_history (SCD2)
                                                       â”€â–¶ gold_dim_date|customer|contract, gold_fact_payment,
                                                           gold_fact_contract_month (arrears, DPD buckets), gold_dq_results
```
| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Open mirroring item + start; landing-zone producer (snapshot, inserts, updates, deletes) | Fabric REST + OneLake DFS API â†’ `infra/loan_servicing.py` | âœ… `mir_loan_servicing` (from Fabric) | Script |
| `lh_asset` + asset tables (Asset domain data product) | Lakehouse Load-to-Table API â†’ `finance_lakehouse.py asset` | âœ… lakehouse definition; data via script | Script |
| `lh_finance`, shortcuts, partner files, notebook | Fabric REST + OneLake â†’ `finance_lakehouse.py create` | âœ… lakehouse (incl. `shortcuts.metadata.json`) + notebook (from Fabric); PySpark source in `infra/notebooks/` | Script + Git |
| Small Spark pool (F2) | Fabric REST â†’ `finance_lakehouse.py spark-pool` | No (workspace setting) | Script |
| Run + verify | Jobs API + SQL analytics endpoint (`mssql-python`, Entra token) â†’ `run`, `verify` | Script | Output below |
| Prod rebinding (notebook default lakehouse, shortcut targets incl. cross-workspace) | `workspaces/ce-finance/parameter.yml` (`$items`, `$workspace.ce-asset-prod...`) | âœ… | n/a |

```powershell
cd data-gen; py -m contoso_gen batch --sites 100 --days 2 --no-telemetry      # finance + asset + credit bureau files
py infra/loan_servicing.py create; py infra/loan_servicing.py snapshot
py infra/finance_lakehouse.py asset; py infra/finance_lakehouse.py create; py infra/finance_lakehouse.py run; py infra/finance_lakehouse.py verify
py infra/loan_servicing.py changes --payments 20 --status-updates 3 --delete-customer; py infra/loan_servicing.py latency
# prod data (before the first PR deploys lh_finance): create/snapshot --env prod; asset --env prod; spark-pool --env prod
# prod after deploy: finance_lakehouse.py landing --env prod; run --env prod; verify --env prod (verify forces a SQL endpoint refreshMetadata)
```
**Verified (dev)**:
- **Open mirroring:** a change is visible in the SQL analytics endpoint after **38 s**. Inserts, updates and a delete (a right-to-erasure request) were applied (97 â†’ 96 customers).
- **Data quality:** all 11 checks pass:
  - 13 re-posted payments deduplicated;
  - 37 late-arriving records flagged;
  - 0 primary-key duplicates or orphans;
  - all 21 live mirrored payments reached gold.
- **KPIs:** collection rate 95.9 %; 69 delinquent contract-months (30+ DPD, of which 62 are 90+).

**Why not Azure SQL and ADLS?** New SQL servers and storage accounts in this tenant are forced to *public network access = Disabled* by tenant governance, not Azure Policy, so it can't be exempted. Mirroring would then need a VNet data gateway, and an ADLS shortcut can't work at all (trusted workspace access requires "selected networks"). See D-014. A private-networking module is a possible later add-on.

**Flags**:
- Open mirroring is GA. Mirroring compute is free, and storage is free up to the capacity's allowance.
- Notebook runs consume Spark CU.
- **F2 + Starter Pool (Medium nodes) never starts a session.** The job sits in *NotStarted* and other Spark calls return `TooManyRequestsForCapacity`. Use a Small-node pool (D-015).
- Load-to-Table doesn't support schema-enabled lakehouses, hence the bronze/silver/gold name prefixes (D-016).
- Everything runs on a trial capacity.
- **1e API layer**: GraphQL or User Data Functions over gold.

## Step 2: Real-Time dashboard + Activator â¬œ
Device fault, offline device, pipeline failure. Activator runs consume CU.

## Step 3: Data agents â¬œ
Eventhouse, lakehouse, combined; publish to Teams/M365 Copilot. **Preview; paid F SKU + Copilot tenant settings; not on trial.** M365 Copilot publishing needs M365 Copilot licenses for consumers.

## Step 4: Semantic model + Power BI KPIs â¬œ
Direct Lake model, KPIs (DAX), certify one gold KPI (certification tenant setting + certifier group). Consumers need Pro below F64.

## Step 5: Fabric IQ ontology â¬œ
Site, Device, Battery, Alert, Contract, Customer + ontology agent; contrast with data agent. **Preview; tenant setting.**

## Step 6: Capacity monitoring â¬œ
Gaps of the Capacity Metrics app first; then (ii) RTI capacity events + workspace monitoring â†’ Eventhouse â†’ RTD + Activator @70 %; (iii) REST/Admin APIs + Semantic Link + Cost Management export â†’ lakehouse â†’ report. Check FUAM (fabric-toolbox, community, unsupported) first. Load generator on a small SKU: throttling, surge protection, pause/resize. Capacity events and parts of workspace monitoring are **preview**. Cost export needs Azure Cost Management Reader.

## Step 7: Observability â¬œ
Failure injection â†’ detection â†’ alert routing; Monitor hub; workspace monitoring (uses CU and Eventhouse storage).

## Step 8: Governance â¬œ
OneLake catalog, domains, endorsement, lineage/impact, **OneLake security RLS/CLS (preview)**, shortcut security, workspace roles.

## Step 9: Purview â¬œ
Sensitivity labels (M365 E3/E5 or equivalent label licensing), DLP for lakehouse/semantic model (E5 / E5 Compliance-class licensing; some Fabric DLP features preview), audit, Data Map for external sources (Purview pay-as-you-go). **Licensing flag.**

---

## Per-step close-out (working rules)
1. Work on `dev` in small commits that reference the issue (`#N`).
2. Verify in the dev workspace, then commit it to Git from Fabric (Source control â†’ Commit).
3. Update this runbook (status) and [decision-log.md](decision-log.md).
4. Open a PR dev â†’ main. Merging deploys prod via fabric-cicd.
5. Stop and summarize.
