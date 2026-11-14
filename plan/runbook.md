# Runbook: Contoso Energy on Microsoft Fabric

Sample, not production guidance (see [DISCLAIMER](../DISCLAIMER.md)). Status legend: ⬜ todo · 🟨 in progress · ✅ verified in dev · 🚀 promoted to prod.

**Tool key**: *Agent* = GitHub Copilot agent mode in VS Code · *Skills* = Skills for Fabric · *Fabric REST* = `infra/fabric_rest.py` (the Fabric MCP servers were planned but not connected to the build session; see [review.md](review.md#tools)) · *fab* = Fabric CLI · *Portal* = Fabric/Azure/GitHub UI.
**Capture key**: *Git* = file in this repo · *Script* = idempotent script in `/infra` re-creates it · *Checklist* = manual step recorded in [checklists](checklists/).

## Prerequisites on your machine
`git`, Python 3.12+, Azure CLI, GitHub CLI (`winget install GitHub.cli`), then:
```powershell
py -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r infra/requirements.txt
pre-commit install
Copy-Item .env.example .env   # fill in; never commit
```

---

## Step 0: Prereqs and platform ✅ (issue #1, PRs #15, #16)

| # | Task | Tool | Git-tracked? | Capture |
|---|---|---|---|---|
| 0.1 | Per-repo Git identity (no-reply email), SSH alias `github-personal`, test commit | git / ssh (local) | Hook only (`tools/check_git_identity.py`) | Checklist §A |
| 0.2 | Scaffolding: README, LICENSE, DISCLAIMER, .gitignore, .env.example, gitleaks + pre-commit | Agent | ✅ Git | n/a |
| 0.3 | `dev` branch, secret scanning + push protection, main protection (PR only, admins included, `gitleaks` check), environments, issues | gh → `infra/github_setup.py repo envs issues` | Script yes, settings no | Script |
| 0.4 | Paid F SKU capacity (+ RG) | fab (ARM) → `infra/capacity.py create` | Script | Script |
| 0.5 | Tenant settings (Copilot, data agent, ontology, GitHub sync, SP APIs, workspace monitoring, certification) | fab api → `infra/tenant_settings.py [--apply]`; Portal for anything unmatched | No (tenant state) | Script + Checklist §B |
| 0.6 | Entra groups, CI/CD SP + OIDC federated credentials (no secret) | az → `infra/entra_setup.py` | Script | Script |
| 0.7 | Domains Fleet/Asset/Finance; 6 workspaces `ce-{fleet,asset,finance}-{dev,prod}`; capacity + domain assignment; workspace roles | fab → `infra/fabric_setup.py` | Script + `infra/config/lab.json` | Script |
| 0.8 | Fabric GitHub connection (fine-grained PAT, this repo, Contents RW) + Git connect **dev** workspaces → `dev:/workspaces/ce-*` | fab api → `fabric_setup.py --only git` | Script; PAT in .env only | Script + Checklist §C (PAT creation) |
| 0.9 | GitHub secrets (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, env-scoped `FABRIC_WS_*`) | gh → `infra/github_setup.py secrets` | No | Script |

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
**Verify**: `git log -1 --format=%ae` shows the no-reply address; push over `github-personal` succeeds; a direct push to `main` is rejected; `fab ls` shows 6 workspaces on the capacity; dev workspaces show *Source control → dev*; `fabric-deploy` on main is green (no items yet means a no-op).

**Lessons from the first run**
- `tenant_settings.py` must exit 0 before `fabric_setup.py --only git`. With *GitHub sync* off, Git connect fails with `403 FeatureNotAvailable`. Tenant setting changes take up to ~15 min to propagate.
- New GitHub repos send an ID-based OIDC subject (`repo:<owner>@<ownerId>/<repo>@<repoId>:environment:<env>`). `entra_setup.py` registers both the ID-based and name-based subjects (D-006). Without the ID-based one, `azure/login` fails with `AADSTS700213`.

**Flags**
- **Paid F SKU required**. Copilot, data agents and ontology do not run on a Fabric trial capacity. F2 is enough for steps 0–5. At F2–F32, Power BI report *consumers* need Pro/PPU licenses; free viewers need F64+.
- Preview: data agent, ontology (Fabric IQ), and the Admin API "update tenant setting". Check status when you build.
- Service principals can't use some Git-integration and Copilot APIs. Git connect runs as you (user); prod deploy runs as the SP.

---

## Step 1: Data ✅

### 1a Mock data ✅ (issue #2)
| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Generator package `data-gen/contoso_gen` (batch + stream), tests, event contract | Agent (Python, stdlib) | ✅ Git | n/a |
| Generate history locally | `py -m contoso_gen batch` | Output gitignored (`data-gen/out`) | Re-creatable from seed |
| Unit/contract tests | pytest locally + `tests` workflow | ✅ Git | n/a |

```powershell
cd data-gen; py -m pytest -q
py -m contoso_gen batch --sites 200 --days 7          # ~1.5M telemetry events, master + finance tables
py -m contoso_gen stream --sites 20 --interval-sec 2 --duration-min 1 --sink file --inject INVERTER_FAULT:SITE-000003
```
**Verify**: 8 tests pass; `out/summary.json` shows duplicates, late events, schema v2 events and all 6 anomaly types; PV is 0 at local night; SOC stays within 10–100 %; emails are only `example.com`.
**Flags**: none. Runs locally, no Fabric capacity used. Event contract and injected behaviour: [data-gen/README.md](../data-gen/README.md).
**Fabric Git commit**: n/a (no Fabric items in 1a).

### Remaining
- **1b Ingestion comparison** ✅ (issue #3): see [ingestion-comparison.md](ingestion-comparison.md). Recommendation D-009: **Eventstream custom endpoint → Eventhouse direct ingestion**.

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Event Hubs namespace/hub/consumer group, Entra RBAC (SAS off) | az → `infra/eventhubs.py create` | Script | Script |
| `eh_fleet` Eventhouse, `lab_compare` KQL DB, workspace identity, Event Hubs connection, 2 eventstreams; custom endpoint key → `.env` | Fabric REST → `infra/rti_lab.py create` (definition schemas from the Learn item-definition articles) | ✅ items committed **from Fabric** to `dev:/workspaces/ce-fleet` | Script + Git |
| Tee benchmark + KQL scoring | `py data-gen/benchmarks/ingest_compare.py` | Script ✅; results JSON gitignored | Numbers copied into the comparison doc |
| CU per eventstream (Capacity Metrics app) | Portal | No | Checklist in ingestion-comparison.md |
| Prod | fabric-cicd deploys `eh_fleet`; `lab_*` excluded (`infra/deploy.py`) | ✅ | n/a |

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
  - Each eventstream with a processor uses ≈ 1 CU continuously (≈ 50 % of an F2). Pause the lab streams when idle.

**Notes**:
- Fabric Git serializes dev workspace/item/connection GUIDs into definitions. These are identifiers, not secrets.
- A failed KQL DB create can leave an orphan database with the same pretty name. That breaks name-based queries and destinations, so use unique item names and query by database GUID.

- **1c Eventhouse medallion** ✅ (issue #4)

```
devices ─▶ es_fleet (custom endpoint, direct ingestion, no processor)
        ─▶ bronze_events (raw + envelope, streaming ingestion, 7d / hot 1d)
        ─update policy─▶ silver_telemetry (typed, v1/v2 → kW, late/future/outlier flags, 30d / hot 7d)
                       ─▶ silver_alerts (90d)
        ─MV take_any by event_id (lookback 6h)─▶ silver_telemetry_dedup / silver_alerts_dedup
        ─MV over MV─▶ gold_device_latest (arg_max per device) · gold_site_15m (sums + counts, 365d / hot 31d)
        ─functions─▶ Fleet API: FleetDeviceStatus, FleetOfflineSites, FleetOpenAlerts, FleetKpis,
                     FleetSiteEnergy, FleetBatteryAnomalies, FleetInverterFaults, FleetDataQuality (+ GoldSiteProfile)
```
| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| KQL sources (bronze/silver/gold/API) | Agent; KQL DB definition rules from Learn | ✅ `infra/kql/eh_fleet/*.kql` (source of truth) | n/a |
| Apply to `eh_fleet` via item definition | Fabric REST → `infra/fleet_medallion.py apply` | ✅ Fabric commits `DatabaseSchema.kql` to `workspaces/ce-fleet/...` | Script + Git |
| `es_fleet` eventstream (custom endpoint → direct ingestion) | Fabric REST → `fleet_medallion.py eventstream` | ✅ (prod IDs rebound by `workspaces/ce-fleet/parameter.yml`) | Script + Git |
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
- **1d Finance lakehouse medallion** ✅ (issue #5)

```
loan servicing (simulated OLTP) ─parquet change files─▶ mir_loan_servicing (OPEN MIRRORING, landing zone)
                                                          │ Tables/dbo/customers|contracts|payments (Delta)
lh_asset (Asset domain) ◀─Load-to-Table─ asset CSVs       │
   │ Tables/sites                                         ▼
   └──OneLake shortcut──▶ lh_finance  Tables/ref_sites  ·  bronze_customers|contracts|payments (shortcuts, zero copy)
   credit bureau CSVs ─OneLake API─▶ Files/landing/credit_bureau ─▶ bronze_credit_bureau
                         nb_finance_medallion (PySpark) ─▶ silver_customers, silver_contracts, silver_payments (dedup),
                                                           silver_customer_credit_history (SCD2)
                                                       ─▶ gold_dim_date|customer|contract, gold_fact_payment,
                                                           gold_fact_contract_month (arrears, DPD buckets), gold_dq_results
```
| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Open mirroring item + start; landing-zone producer (snapshot, inserts, updates, deletes) | Fabric REST + OneLake DFS API → `infra/loan_servicing.py` | ✅ `mir_loan_servicing` (from Fabric) | Script |
| `lh_asset` + asset tables (Asset domain data product) | Lakehouse Load-to-Table API → `finance_lakehouse.py asset` | ✅ lakehouse definition; data via script | Script |
| `lh_finance`, shortcuts, partner files, notebook | Fabric REST + OneLake → `finance_lakehouse.py create` | ✅ lakehouse (incl. `shortcuts.metadata.json`) + notebook (from Fabric); PySpark source in `infra/notebooks/` | Script + Git |
| Small Spark pool (F2) | Fabric REST → `finance_lakehouse.py spark-pool` | No (workspace setting) | Script |
| Run + verify | Jobs API + SQL analytics endpoint (`mssql-python`, Entra token) → `run`, `verify` | Script | Output below |
| Prod rebinding (notebook default lakehouse, shortcut targets incl. cross-workspace) | `workspaces/ce-finance/parameter.yml` (`$items`, `$workspace.ce-asset-prod...`) | ✅ | n/a |

```powershell
cd data-gen; py -m contoso_gen batch --sites 100 --days 2 --no-telemetry      # finance + asset + credit bureau files
py infra/loan_servicing.py create; py infra/loan_servicing.py snapshot
py infra/finance_lakehouse.py asset; py infra/finance_lakehouse.py create; py infra/finance_lakehouse.py run; py infra/finance_lakehouse.py verify
py infra/loan_servicing.py changes --payments 20 --status-updates 3 --delete-customer; py infra/loan_servicing.py latency
# prod data (before the first PR deploys lh_finance): create/snapshot --env prod; asset --env prod; spark-pool --env prod
# prod after deploy: finance_lakehouse.py landing --env prod; run --env prod; verify --env prod (verify forces a SQL endpoint refreshMetadata)
```
**Verified (dev)**:
- **Open mirroring:** a change is visible in the SQL analytics endpoint after **38 s**. Inserts, updates and a delete (a right-to-erasure request) were applied (97 → 96 customers).
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
- **1e API layer** ✅ (issue #6)

| API | Over | Auth | Exposes | Tool / Git |
|---|---|---|---|---|
| `gql_finance` (API for GraphQL) | `lh_finance` SQL analytics endpoint, gold star schema | Entra (SSO, caller's permissions) | `customer`, `contract`, `payment`, `contract_month`, `calendar_date` + relationships; **query-only; no PII** (`customer_name`, `email`, `phone` not mapped) | `infra/api_layer.py graphql` → item committed from Fabric; prod rebinding via `$items.Lakehouse.lh_finance.$sqlendpointid` |
| Fleet API | `eh_fleet` stored functions (`FleetKpis`, `FleetOfflineSites`, `FleetOpenAlerts`, `FleetSiteEnergy`, …) | Entra (Kusto REST) | function contract from 1c | `infra/api_layer.py fleet` (sample client) |

**Verified (dev)**: GraphQL nested query (contract → customer) returns rows; requesting `email` fails schema validation (*field does not exist*); Fleet API functions answer in ~0.6 s warm.
**Why not User Data Functions now**: GraphQL covers the gold read API with no code. Eventhouse isn't a GraphQL source, and its stored functions already *are* an API over REST. UDFs are kept for write or business-logic endpoints (e.g. a "payment promise" mutation) if a later step needs them (D-017).
**Flags**: API for GraphQL is GA and runs on the capacity (CU per request). Callers need read access to the lakehouse (or SQL endpoint).

## Step 2: Real-Time dashboard + Activator ✅ (issue #7)

| Item | What | Tool | Git-tracked? | Capture |
|---|---|---|---|---|
| `rtd_fleet_ops` | Real-Time Dashboard on the Fleet API: KPI cards (online devices, offline sites, faults, open alerts, PV now), PV/load trend, ingestion events + p95 latency, open alerts, offline sites, battery anomalies, site map; auto-refresh 1 min | Fabric REST (KQLDashboard definition, schema 52) → `infra/rti_alerts.py dashboard` (every tile query is smoke-tested first) | ✅ from Fabric; prod rebinding of `database`/`workspace` in `parameter.yml` | Script + Git |
| `act_fleet_device_faults` | Activator: KQL source (5 min, time-axis on alert arrival) → EventTrigger `event_type = raised` AND `severity ≠ Low` → email | Fabric REST (Reflex definition; shapes from **Skills for Fabric** `activator-cli`) → `rti_alerts.py activators` | **No**: holds recipient address (D-018) | Script (`--env dev/prod`) |
| `act_fleet_offline_sites` | Activator: source emits a site **once** when its gateway crosses 20 min without data → email | same | No (D-018) | Script |
| Pipeline-failure alert | Built in step 7, together with the pipeline, failure injection and workspace monitoring (D-019) | n/a | n/a | n/a |
| Live data | `contoso_gen stream --sites 100 --interval-sec 30 --anomaly-boost 40` → `es_fleet` (dev) | local, detached | No | Runbook command |

```powershell
cd data-gen; py -m contoso_gen stream --sites 100 --interval-sec 30 --sink eventhub --anomaly-boost 40 --senders 2 --target 'fleet=conn:ESTREAM_FLEET_CONNECTION_STRING'
py infra/rti_alerts.py all                 # dashboard + activators + status (add --env prod for prod)
py infra/git_sync.py commit --workspace ce-fleet-dev --message "..."   # selective: excludes act_*
```
**Verified (dev)**:
- All 11 tile queries return results.
- Both Activator definitions were accepted with rules *running*, and the same is true in prod.
- The device-faults source returned 22 alert events over the last day.
- No tenant account appears anywhere under `/workspaces`.

**Checklist (portal, human)**:
- Open `rtd_fleet_ops` and confirm the tiles and map render.
- Activator → *History* shows activations, and the alert email arrives.
- Optional: change the action to a Teams message.

**Flags**:
- Activator is GA. Every rule evaluation and KQL source poll consumes CU; a 5-minute poll is the cost/latency trade-off.
- Email/Teams actions need the recipient in the same tenant.
- Real-Time Dashboards are GA.
- All of this runs on a trial capacity.

**Gotcha**: Windows PowerShell 5 `Get-Content`/`Set-Content` re-encode UTF-8 as ANSI and corrupted this runbook once (every emoji and arrow became 2–3 accented Latin characters). It's now blocked by the `mojibake` pre-commit hook; edit UTF-8 files with an editor or Python, not PS5 cmdlets.

## Step 3: Data agents ✅ (issue #8)

| Agent | Workspace | Sources | Notes |
|---|---|---|---|
| `da_fleet_ops` | ce-fleet | `eh_fleet` (KQL): `silver_telemetry`, `silver_alerts`, `dim_*` | Instructions encode "online/offline/open alert" semantics; 4 KQL few-shots |
| `da_finance` | ce-finance | `lh_finance` gold star schema | PII columns (name, email, phone) **deselected**; 3 SQL few-shots |
| `da_contoso_360` | ce-asset | `eh_fleet` + `lh_finance` + `lh_asset` | Cross-domain: joins by `site_id` / `customer_id` in its reasoning |

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Build definitions from live schemas, publish | Fabric REST (DataAgent definition) → `infra/data_agents.py create` | ✅ from Fabric (`git_sync.py`); prod rebinding of `datasource.json` per source (cross-workspace for 360) | Script + Git |
| Ask / regression questions | OpenAI-compatible Assistants endpoint `…/dataagents/{id}/aiassistant/openai` → `data_agents.py ask|test` | Script | Output below |
| Publish to Teams / Microsoft 365 Copilot | Portal: agent → *Publish* → *Microsoft 365 Copilot* / Copilot Studio → Teams | No | Checklist below |

**Verified (dev)**:
- **`da_finance`:** collection rate by product matches the lakehouse (Loan 94.5 %, Lease 98.2 %, PPA 99.0 %). Asked for customer emails, it refused.
- **`da_fleet_ops`:** 384 devices online, 0 sites offline, and the 3 open Critical/High alerts; all identical to `FleetKpis()` / `FleetOpenAlerts()`.
- **`da_contoso_360`:** found the 2 defaulted contracts in finance, then checked their sites' devices in the Eventhouse and explained the join.

**Limitations found (D-020)**:
- The NL-to-KQL tool sees **tables only**. Materialized views (`gold_*`, `*_dedup`) and stored functions (the Fleet API) are rejected as "not a known table", so the fleet agent uses silver tables and few-shots that encode the same logic.
- Data agents run under the **caller's** permissions.

**Checklist (portal)**:
- Publish `da_contoso_360` to Microsoft 365 Copilot (agent store) and to Teams via Copilot Studio.
- Test the same 5 questions there.

**Flags**:
- **Preview:** data agents, Microsoft 365 Copilot publishing.
- **Capacity/tenant:** needs a paid F SKU (F2+) and the *Copilot and Azure OpenAI* tenant setting. Doesn't run on a trial capacity.
- Each question consumes CU (Copilot operations, smoothed as background).
- **Licensing for consumers:** Microsoft 365 Copilot users need an M365 Copilot license; Teams via Copilot Studio needs Copilot Studio capacity/licensing.

## Step 4: Semantic model + Power BI KPIs ✅ (issue #9)

| Item | What | Tool | Git-tracked? |
|---|---|---|---|
| `sm_finance` | **Direct Lake** (on the `lh_finance` SQL analytics endpoint) TMDL model: Date, Customer (no PII columns), Contract, Payment, Contract Month; 5 relationships; 13 DAX measures | `infra/semantic_model.py create` (TMDL generated from the endpoint schema) + refresh (framing) | ✅ from Fabric; prod `Sql.Database(host, id)` rebinding via regex in `parameter.yml` |
| KPIs | Collection Rate, **Delinquency Rate (30+ DPD)** (proposed certified KPI; definition in the measure description), 90+ DPD Rate, On-time Payment Rate, Portfolio Principal, Contracts Billed… | DAX in TMDL | ✅ |
| `rpt_finance_kpis` | PBIR report: 4 KPI cards, collection/delinquency trend, delinquency by product, region matrix | `infra/finance_report.py create` | ✅ (`byPath` to the model) |
| Render check | Export-to-PDF API → `data-gen/out/rpt_finance_kpis-<env>.pdf` (image export stays disabled in the tenant) | `finance_report.py export` | No (output) |
| Certification | Endorse `sm_finance` as **Certified** (certifiers = `ce-fabric-admins`, D-007) | **Portal only** (no public API) | Checklist |

**Verified (dev)**:
- **DAX vs T-SQL** on the same lakehouse match exactly: collection 95.86 %, delinquency (30+ DPD) 6.02 %, 83 billed contracts, on-time 92.33 %, principal $2.47 M.
- **Rendered report:** KPI cards and the region matrix totals agree. Plains is the outlier (85.5 % collection, 15.8 % delinquency).
- **Fixes found while rendering:**
  - Date dimension now starts at the first month start (2 orphan month rows).
  - Count measures `+ 0` so trends draw zero instead of gaps.
  - The table visual was replaced by a matrix (`tableEx` showed only the grouping column).

**Checklist (portal)**:
- `sm_finance` → Settings → Endorsement → **Certified**, plus a description and the KPI owner.
- Optional: a Power BI scorecard goal on *Delinquency Rate (30+ DPD)*.

**Flags**:
- Direct Lake needs a Fabric capacity (works on a trial).
- Report **viewers need Pro/PPU below F64**; free viewers need F64+.
- `ExportToImage` is off in the tenant; PDF export (`ExportToPowerPoint`) is on.

## Step 5: Fabric IQ ontology ✅ (issue #10)

| Item | What | Tool | Git-tracked? |
|---|---|---|---|
| `gold_alert_events` (eh_fleet) | Alert events with a unique key `alert_id:event_type` (update policy from `silver_alerts`) | `infra/kql/eh_fleet/30_gold.kql` → `fleet_medallion.py apply` | ✅ (KQL DB definition) |
| OneLake availability | `silver_telemetry`, `gold_alert_events` mirrored to OneLake as Delta (5 min target latency, backfill) | `infra/ontology.py expose` (KQL mirroring policy) | Script |
| `lh_asset` shortcuts | `fleet_telemetry`, `fleet_alert_events` (→ Eventhouse), `fin_contracts`, `fin_customers` (→ lh_finance gold) | `ontology.py shortcuts` | ✅ (`shortcuts.metadata.json`, prod rebinding in `parameter.yml`) |
| `ont_contoso` (ce-asset) | **Ontology (new TMDL experience)**: Site, Device, Battery, Alert, Contract, Customer; 5 relationships; Device telemetry as 8 **time-series** properties; rule `BatteryWarrantyReview`; customer PII never mapped | `ontology.py create` (TMDL generated from the endpoint schema) | ✅ (Fabric exports an ALM placeholder; `ONT_*` annotations rebound per env) |
| `da_contoso_ontology` (ce-asset) | Data agent with **ontology context** + `lh_asset` as execution source; minimal instructions | `infra/data_agents.py create` | ✅ |
| Ontology agent (Copilot in the ontology item) | Explain / query / improve the ontology | **Portal only** | Checklist |
| Graph materialization | Optional: Manage graph → Materialize | **Portal only** (no API; `RefreshGraph` job is cancelled while the graph schema is empty) | Checklist |

Run: `py infra/ontology.py all` (dev) · prod: `expose --env prod` + `shortcuts --env prod` **before** merging, then fabric-cicd deploys the ontology and agent · `py infra/ontology.py verify [--env prod]` · `py infra/data_agents.py compare`.

**Design (D-023)**:
- An ontology binds every entity through **one** expression, which is one SQL analytics endpoint. So `lh_asset` is the semantic hub: asset tables plus zero-copy shortcuts to Fleet (Eventhouse OneLake availability) and Finance.
- Relationships form a tree (Alert→Device→Site→Customer, Battery→Device, Contract→Site), so there are no ambiguous paths.

**Verified (dev)**:
- `ontology.py verify`: 6/6 entities, 7 tables with source annotations. Keys are unique for every entity except Alert: 1 duplicate from a late re-send (see D-025).
- The ontology agent and the data agent were contrasted on the same questions (`data_agents.py compare`); results are in [ontology-vs-data-agent.md](ontology-vs-data-agent.md).

**Gotchas found (D-024)**:
- **Undocumented `ONT_*` annotations:** REST-created ontologies need eight `ONT_*` partition annotations, or every entity is *Ineligible* for the graph (microsoft/skills-for-fabric#107).
- **Types:** `decimal` columns need `decimal` properties.
- **Time-series tables:** these need `additionalBackingTable` and a table relationship.
- **Auto-created children:** Fabric auto-creates a GraphModel and an Eventhouse per ontology. They're excluded from Git sync and deploy via `common.UNMANAGED_ITEMS`.
- **Orphan cleanup bug:** that same exclusion fixes a bug where fabric-cicd orphan cleanup had deleted the prod Activators.
- **Agent source type:** the agent's `ontology` source type is accepted by the API but undocumented. The ontology alone gives context only, so the agent needs an execution source.

**Checklist (portal)**:
- Open `ont_contoso` → **Ontology agent** → *Explain the ontology*, then ask: "Which sites have the most alert events and who owns them?" and "Which batteries need a warranty review?"
- Optional: **Manage graph** → select all → **Materialize**, then *Explore graph* (Alert→Device→Site traversal). It adds CU/storage; leave it off during the Step 6 load tests.

**Flags**:
- **Preview:** Ontology (Fabric IQ), the ontology agent, ontology as data-agent context, graph, and Eventhouse OneLake availability for shortcuts (GA, but adds OneLake storage).
- **Tenant setting:** *Ontology item (preview)* (OntologyPreview, D-005).
- **Capacity:** paid F SKU; doesn't run on a trial. Ontology adds a managed Eventhouse (no minimum CU). On F2, both Eventhouses (dev and prod) returned HTTP 429 *Request is denied due to throttling* for more than 15 minutes, after the OneLake backfill exports plus the live stream, agents and ontology builds. That is capacity overage, not a single Eventhouse. **Pause → resume** cleared it immediately (the carry-forward is billed). Step 6 reproduces this on purpose.

## Step 6: Capacity monitoring ✅ (issue #11)

Full comparison and load-test results: [monitoring/capacity-comparison.md](../monitoring/capacity-comparison.md).

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| (i) Capacity Metrics app | **Portal only** (AppSource install) | No | Checklist |
| (ii) `cap_monitoring` KQL DB (in `eh_fleet`), `cap_es_capacity` (FabricCapacityOverviewEvents), `cap_rtd_capacity` | `infra/capacity_monitoring.py` | ✅ from ce-fleet-dev; **not deployed to prod** (`DEV_ONLY_ITEMS`, D-027) | Script + Git |
| (ii) `act_capacity_70` (email on upward crossing of 70 %) | `capacity_monitoring.py activator` | No (recipients, D-018) | Script |
| (ii) Workspace monitoring `mon_finance` (Auto) + `mon_fleet`, `mon_asset` (Linked to one Eventhouse) | Fabric REST `monitoringItems` + `enableDataCollection` → `infra/workspace_monitoring.py` | Monitoring items ✅, managed Eventhouse/DBs excluded | Script |
| Tenant settings *Allow monitoring data collection* (+ optional *Users can create Monitoring items*) | `infra/tenant_settings.py --apply` | Script | Script |
| (iii) `cap_lh_capacity` ← Fabric/Admin REST, activity events (hashed users), Cost Management, (ii) aggregates; `cap_sm_capacity` (Direct Lake, 17 measures); `cap_rpt_capacity` | `infra/capacity_report.py all` (collector runs outside Fabric, as user or SP) | ✅ (dev only) | Script + Git |
| Load generator, resize, pause/resume | `monitoring/loadgen.py`, `infra/capacity.py resize|pause|resume` | Script | Results in the comparison doc |
| Surge protection | **Portal only** (Capacity settings) | No | Checklist |

**Verified (dev)**:
- **Option (ii):**
  - Capacity events arrive every 30 s (`verify`: 22 events in 10 min).
  - All 10 dashboard tiles return data.
  - The load test crossed 70 % once (07:42) and `act_capacity_70` fired once, not every 30 s.
  - The *Overloaded / InteractiveDelay* state events were captured.
- **Option (iii):**
  - The model DAX matches the lakehouse: cost $6.96 (Fabric $6.68), 70 items, 8 jobs.
  - The rendered PDF shows utilization, workload, daily cost and a per-workspace chargeback proxy.
- **Load test (F2):**
  - Interactive delay was reached in under 2 minutes (peak 285 %).
  - It throttled throughput, with no errors, for about 18 minutes.
  - Resizing to F4 cleared the delay in about 1 minute and the overage in about 6 minutes.
- **Root cause:** workspace monitoring attributed 17,902 CPU-s to 270 SQL `SELECT`s.

**Checklist (portal)**:
- Install the **Microsoft Fabric Capacity Metrics** app (AppSource) and compare its Compute page with `cap_rtd_capacity` for the load-test window.
- Capacity settings → **Surge protection**: rejection 50 % / recovery 40 %. Then run `nb_finance_medallion` repeatedly and watch `background_rejection_pct`.
- Verify the `act_capacity_70` email (sent at the 07:42 crossing).
- Optional, on a larger SKU: deploy FUAM for per-item CU chargeback.

**Flags**:
- **Preview:** capacity overview events, workspace monitoring, Monitoring items, Activator on KQL.
- **Roles:** Admin APIs and activity events need a Fabric admin; the capacity events source needs a **capacity admin**; cost needs **Cost Management Reader**.
- **Trial:** doesn't run on a trial capacity (needs a paid F SKU to pause/resize); surge protection needs F SKU capacity admin.
- **Cost:** load tests consume real CU. Each pause settles the carry-forward, which is billed.

## Step 7: Observability ✅ (issue #12)

Details and measurements: [monitoring/observability.md](../monitoring/observability.md).

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| `pl_finance_daily` (failure_mode parameter) + `nb_finance_medallion` parameter cell | `infra/observability.py pipeline|notebook` | ✅ (deployed to prod; notebook id rebound in `parameter.yml`) | Script + Git |
| Fabric job events → `cap_es_jobs` → `cap_monitoring.job_events` / `JobFailures()` | `observability.py jobstream` + `infra/kql/cap_monitoring/40_jobs.kql` | ✅ dev only (D-027) | Script + Git |
| Workspace monitoring (from Step 6) | `workspace_monitoring.py` | ✅ | Script |
| `act_job_failures`: routed emails (dataeng / bi) | `observability.py activator` | No (recipients) | Script |
| Failure injection + latency measurement | `observability.py inject --mode ...` | Script | Results doc |
| Monitor hub triage | **Portal only** | No | Checklist |

**Verified (dev)**:
- **`fail_activity`:** the job API returned `CE-OBS-001`. Job events took 3 s and `ItemJobEventLogs` 49 s.
- **`notebook_missing_file`:** a real Spark failure (error 2451). Job events took 4 s, `ItemJobEventLogs` 78 s and activity logs about 90 s (failing activity + `UserError`).
- **Bug caught:** the route filter missed notebooks because they're logged as `SynapseNotebook`. Fixed.

**Checklist (portal)**:
- Monitor hub → filter *Failed* → open the `pl_finance_daily` run → the activity error → the notebook run's Spark logs.
- Confirm the two `act_job_failures` emails arrived (sent about 2–7 min after each failure).
- Optional: add a Teams action in Activator for the dataeng route.

**Flags**:
- **Preview:** workspace monitoring, Fabric job events, Activator.
- **Tenant settings:** *Workspace monitoring* and *Allow monitoring data collection*.
- **Cost:** the monitoring Eventhouse runs on the capacity. Each `notebook_missing_file` run starts Spark (a few CU-minutes on F2).
- **Trial:** not verified on a trial capacity (Activator, workspace monitoring and Real-Time Hub events need a Fabric capacity; F SKU recommended).

## Step 8: Governance ✅ (issue #13)

| Area | What | Tool | Git-tracked? | Capture |
|---|---|---|---|---|
| Domains | Fleet / Asset / Finance, each with a dev + prod workspace (Step 0); `governance.py domains` verifies the assignment | Admin API | Script | Script |
| Workspace roles | Admins = Admin, Engineers = Contributor (dev) / Viewer (prod), Viewers = Viewer, SP = Contributor (prod); `fabric_setup.py` | Fabric REST | Script (`infra/config/lab.json`) | Script |
| OneLake catalog | `governance.py catalog --search <term>` (catalog search API); `hygiene` lists prod items without a description (0) | Fabric REST | Script | Script + portal tour |
| Endorsement | Certify `sm_finance` (Step 4); **Promote** `lh_finance`, `lh_asset`, `gql_finance`, `ont_contoso` | **Portal only** (no public API) | No | Checklist |
| Lineage | (1) Fabric's own: Scanner API (`governance.py scan`), where every item type except mirrored DB/monitoring reports upstream relations; (2) **repo-derived** graph, names only: [lineage.md](lineage.md) | Admin Scanner API; `governance.py lineage` | `plan/lineage.md` ✅ | Script |
| Impact analysis | `governance.py impact --item <name>` (downstream closure); portal *Impact analysis* | Script / portal | Script | Script + checklist |
| **OneLake security (preview)** | `lh_finance` role `FinanceViewerNoPII`: gold tables only, **CLS** on `gold_dim_customer` (no name/email/phone), **RLS** on `gold_dim_contract` (`site_region = 'Coastal'`). Members: Viewers group (+ the deploy SP in dev, as test principal). `DefaultReader` kept for ReadAll holders | Fabric REST `dataAccessRoles` → `governance.py roles` | **No**: Fabric doesn't sync roles (`DataAccessRoles: Disabled`), and members carry tenant IDs | Script (dev + prod) |
| Shortcut security | `lh_asset.fin_customers` / `fin_contracts` read through the source lakehouse's roles | Tested by CI | — | `governance-test.yml` |
| Enforcement test | `.github/workflows/governance-test.yml` (workflow_dispatch, OIDC SP, no secret) → `infra/governance_test.py`: PII/RLS/scope/shortcut checks as the restricted SP | GitHub Actions | ✅ | Job summary |

**Verified (CI, `governance-test`, deploy SP as a restricted Viewer in dev)**:

| Check | Result |
|---|---|
| OneLake: list `silver_customers` (not in role) | **403** (OneLake security enforced) |
| OneLake: list `gold_dim_customer` (CLS) | 0 entries (direct file access blocked for constrained tables) |
| SQL: non-PII customer columns | Allowed |
| SQL: `email` / `SELECT *` on `gold_dim_customer` | **Denied** |
| SQL: `silver_customers` | **Denied** |
| SQL via shortcut: `lh_asset.fin_customers.email` | **Denied** |
| SQL: contract regions (RLS) | **All regions visible** (warning) |

The first CI run exposed **everything** through SQL. The SQL analytics endpoint runs in **delegated identity** mode by default, which ignores OneLake security for table reads, and switching to **user identity** mode is portal-only. The fix (D-032):
- `governance.py sqlsec` mirrors the role with T-SQL `DENY` (PII columns, non-gold tables, shortcut PII), and the test now passes.
- RLS stays OneLake-only: SQL RLS would push Direct Lake into DirectQuery fallback. It's enforced in SQL after the portal switch (checklist).
- Bug found on the way: `fabric_rest.tsql` didn't commit, so the first `DENY`s were silently rolled back.

**Findings**:
- **Circular dependency** `lh_asset ⇄ lh_finance`: `ref_sites` one way, the ontology hub's `fin_*` shortcuts the other. Impact analysis shows an `eh_fleet` change reaching Finance gold, its agents and the report. Accepted for the demo (D-023); in production, put the ontology hub in its own semantic workspace/lakehouse (D-031).
- Workspace **Admins/Members/Contributors bypass** OneLake security. Only Viewers (and item-shared users) are governed by roles, so Engineers (Contributor in dev) still see PII in dev.

**Checklist (portal)**:
- OneLake catalog: browse by domain, then open `lh_finance` and check endorsement, sensitivity and lineage.
- Endorse: Promote `lh_finance`, `lh_asset`, `gql_finance`, `ont_contoso`; Certify `sm_finance` (Step 4).
- `lh_finance` → Lineage view → Impact analysis → notify contacts.
- `lh_finance` → Manage OneLake security: review `FinanceViewerNoPII` (and its preview data).
- **`lh_finance` and `lh_asset` SQL analytics endpoints → Security → switch to *User identity* mode** (enforces OneLake RLS/CLS in SQL; brief endpoint restart), then re-run `governance-test`: the RLS rows should show *Coastal only*.

**Flags**:
- **Preview:** OneLake security (RLS/CLS) and its enforcement in the SQL analytics endpoint (user identity mode).
- **No API:** endorsement, impact-analysis notifications.
- **Admin rights:** Scanner/Admin APIs need a Fabric admin (or an SP with read-only admin API permission).
- **Trial:** OneLake security and catalog work on a trial capacity; not verified there.

## Step 9: Purview ✅ (issue #14), labels and DLP blocked on licensing/publishing

Details and licensing: [governance/purview/README.md](../governance/purview/README.md).

| Capability | Tool | Git-tracked? | Status |
|---|---|---|---|
| Sensitivity labels (12 PII/financing items) | `infra/purview.py labels` (Scanner API) / `apply-labels` (Fabric admin `bulkSetLabels`) | Script | **Blocked**: *Allow users to apply sensitivity labels* is rejected by the API until Purview labels are published to the admin. All 12 items are unlabeled |
| Label tenant settings (apply, downstream inheritance, domain default) | `infra/tenant_settings.py` (optional entries) | Script | Off (same prerequisite) |
| DLP (Fabric/Power BI location: semantic models + lakehouses) | `governance/purview/dlp-fabric.ps1` (Security & Compliance PowerShell) or Purview portal | ✅ template | Not run: interactive compliance sign-in + E5-class licensing |
| Audit | `infra/purview.py audit` (admin activity events; users hashed) | Script | **Working**: data agent queries, OneLake role changes, permission grants, report views |
| Data Map / Unified Catalog | Purview portal (live view of Fabric; classic scans need a Purview account) | No | Checklist; no external sources to scan (D-014) |
| Purview hub in Fabric | Portal | No | Checklist |

**Checklist (portal, Purview/compliance admin)**:
1. Purview portal → Information protection → create or confirm labels (*General*, *Confidential*, *Highly Confidential*) → publish a label policy that includes the admin and the Fabric groups.
2. Fabric Admin portal → Information protection: enable *Allow users to apply sensitivity labels*, *Downstream inheritance* (optional: *From data sources*, *Domain default labels* for Finance = Confidential). Or run `py infra/tenant_settings.py --apply` once labels exist.
3. Put the *Confidential* label GUID in `.env` as `PURVIEW_LABEL_CONFIDENTIAL`, run `py infra/purview.py apply-labels --env dev` and `--env prod`, then `labels` (expect 0 unlabeled).
4. `Connect-IPPSSession`; `./governance/purview/dlp-fabric.ps1 -WorkspaceIds ... -NotifyUser ...`. Then refresh `sm_finance` and wait for the DLP policy tip / incident on the PII semantic model.
5. Purview portal → Audit: search `CreateOrUpdateDataAccessRoles`, `DataAgentFabricQueried` (same records as `purview.py audit`, longer retention).
6. Purview portal → Unified Catalog: confirm the Fabric live view shows the Finance domain items. Fabric → Microsoft Purview hub.

**Flags (licensing)**:
- **Sensitivity labels:** M365 E3/E5 or AIP P1+, plus labels published to users.
- **DLP for Fabric:** M365 E5 / E5 Compliance or the DLP add-on. Lakehouse DLP is **preview**.
- **Audit:** Standard with E3; Premium with E5.
- **Classic Data Map scans:** a Purview account (pay-as-you-go).
- **DSPM for AI:** E5-class.
- **Trial:** none of this depends on the Fabric trial; it depends on the M365/Purview licensing of the tenant.

## Step 10: OneLake diagnostics + access observability ✅ (issue #32), diagnostics delivery blocked

Results: [governance/access-observability.md](../governance/access-observability.md) (raw: [access-observability-results-obs.md](access-observability-results-obs.md)).

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Separate **F64** capacity for observability tests | `infra/capacity.py create --target obs` (`FABRIC_OBS_CAPACITY_*` in `.env`) | Script | Script; pause with `--target obs` |
| Obs lab: `ce-obs-{data,consumer,audit,ops}`, finance copy + OneLake role, cross-workspace shortcuts, data agent, Spark notebook, diagnostics into an **admin-only** audit workspace, workspace monitoring (Auto + Linked), SQL audit | `infra/obs_lab.py workspaces|data|consumer|audit|sqlaudit` | **No** (tenant-bound lab items, D-041) | Script |
| Test identity (cloud-only Viewer, free Fabric license, isolated az profile) | `infra/test_identity.py create|login|whoami` | Script | One device-code sign-in |
| Identity matrix: admin, test user (Viewer + role), deploy SP (Viewer, **no** role; in CI) | `infra/access_matrix.py run --profile obs --as ...`; `governance-test.yml` | Script | Probes in `infra/.state` (gitignored) |
| Correlate probes with OneLake diagnostics, workspace monitoring, activity events (= Purview Audit), SQL audit | `access_matrix.py correlate --profile obs` | Generated results file | Script |
| Dev diagnostics (first attempt) | `infra/onelake_diagnostics.py enable|status|generate|analyze` | Script | Script |

**Verified**:
- **OneLake enforcement:** OneLake security is enforced for OneLake API/Spark access:
  - out-of-role table returns 403;
  - the CLS table lists no files;
  - the SP with no role gets 403 everywhere.
- **SQL bypass:** the **new lakehouse's SQL endpoint is in delegated mode too**. Every SQL path, data agent included, serves PII, out-of-role tables and all regions to **any Viewer** (D-042).
- **Purview Audit / activity events:** show the **human behind a data agent** (`DataAgentFabricQueried`). They show nothing for OneLake reads or denials, and nothing for the SP.
- **Workspace monitoring:** shows SQL callers including the SP for the consumer (shortcut) lakehouse, but recorded **no** SQL executions for the data workspace's endpoint.
- **SQL audit:** enabled via REST (200), but no events within the test window.

**Blocker**: OneLake diagnostics delivered **zero** log files: about 15 h on dev (two destinations) and about 2 h on a fresh, isolated capacity. Settings read *Enabled* and folders are created; there is no known issue listed. Next step: toggle in the portal once, then open a support ticket.

**Gotchas**:
- **Group access delay:** group-based Viewer access for a brand-new user took hours to resolve in Fabric. The script also assigns the test user directly.
- **Stale Graph token:** `az ad` and Graph calls failed with a Continuous Access Evaluation (CAE) re-auth challenge. The obs scripts resolve groups through Fabric role assignments instead.
- **Agent thread reuse:** the data agent endpoint reuses the caller's thread, so take the newest assistant message only.

**Flags**:
- **Preview:** OneLake security; workspace monitoring; data agent; OneLake diagnostics is GA per the docs.
- **Cost:** F64 about $11.5/h; pause it between sessions.

## Step 11: Data agent evaluation 🟨, live run done (dev); CI pending repository variables

Pipeline and grader comparison: [eval/README.md](../eval/README.md). Target `da_contoso_360`, A/B arm `da_contoso_ontology`, 21 golden cases with live SQL/KQL truth (D-035).

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Foundry account (key auth off), project `agent-eval`, `judge-gpt-4-1-mini`, `decision-1` (Microsoft-Decision-1, capacity 100) | `infra/foundry.py create` (az CLI) | Script | Script |
| Golden set + collect (answers, executed SQL/KQL, tokens, latency) | `eval/agent_eval.py collect` → `eval/runs/` (gitignored) | ✅ golden set | Script |
| Graders: rules, Microsoft-Decision-1, Foundry evaluation SDK | `eval/agent_eval.py score` (Foundry run logged to the project) | Script | Script |
| Fabric Data Agent SDK `evaluate_data_agent` | `eval/agent_eval.py fabric` → notebook `lab_agent_eval` (dev only) | ✗ (lab_ item) | Script |
| Regression gate | `.github/workflows/agent-eval.yml` (OIDC SP, `da_contoso_ontology` only, D-036) | ✅ | n/a |

**Results (dev, run live2, F64; report: [eval/reports/live2.md](../eval/reports/live2.md))**: rules accuracy is **`da_contoso_360` 81 %** and **`da_contoso_ontology` 71 %**. Both rise by one case (to 86 % and 76 %) after the `x-default-alerts` wording fix, which run fix1 verified. Neither agent leaked PII. p50 latency was about 14 s for both.

| Grader | Agreement with rules | Kappa | False pass / false fail | p50 latency | Cost (42 answers) |
|---|---|---|---|---|---|
| Microsoft-Decision-1 | 90 % | 0.77 | 0 / 4 | 0.8 s | 9.6K input tokens, ~84 output (decisions only) |
| Foundry SDK (Similarity, gpt-4.1-mini) | 87 % | 0.71 | 0 / 5 (3 errors) | 2.1 s | $0.019 |
| Fabric Data Agent SDK (`evaluate_data_agent`) | 90 %* | 0.72 | 0 / 4 | notebook, ~4 min for 42 | capacity CU only |

\* The SDK re-asks the agent, so it is compared with the rules applied to its own answers. Its answers scored 74 % with the SDK's critic. The SDK output columns include `evaluation_judgement`, `actual_answer`, `thread_url` and the agent version/etag.

**Findings**:
- **Agent errors that only the logged SQL/KQL makes visible.**
  - "Critical alerts raised": both agents counted raised and cleared events instead of distinct raised alerts (4 vs 2, 60 vs 30).
  - The ontology agent summed PV capacity from `fin_contracts` instead of `sites`, and filtered `product = 'loan'` in lowercase.
  - The 360 agent's finance tool rejected a valid query as "irrelevant or harmful".
- **Different grader failure modes.**
  - Foundry Similarity fails correct PII refusals, because they don't resemble the expected refusal text (similarity 1/5). Rules and Decision-1 pass them.
  - Decision-1 failed two correct entity answers that had extra detail ("COMM_LOSS … 3 occurrences" while the truth count was 2).
  - Neither AI grader passed an answer that the rules failed.
- **Capacity.** F2 throttled every KQL query (429, even `print 1`) while streaming, agent calls and truth queries ran. The live run used F64. Scale back with `py infra/capacity.py resize --sku F2` (done).
- **Endpoint.** The endpoint reuses one thread per caller and agent, so questions to the same agent must run one at a time.
- **Fabric SDK.** One run failed on `da_contoso_ontology` with "data source … not found in sandbox stage". The notebook now falls back to the sandbox stage and records per-agent errors. Notebook job runs report only a generic failure, so the notebook writes its traceback to `Files/agent_eval/<run>/fabric_sdk_error.txt`.

### live3: instructions vs ontology, golden set v2, capacity sweep

Report: [eval/reports/live3.md](../eval/reports/live3.md) (F64), [eval/reports/capacity-latency.md](../eval/reports/capacity-latency.md). Design: D-037, D-038.

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| `lab_da_hub_rich` and `lab_da_hub_bare` (ce-asset-dev only) | `infra/data_agents.py create --only lab_da_hub_rich,lab_da_hub_bare` | ✗ (lab_ items; definitions in the script) | Script |
| Golden set v2: 21 original + 27 new questions, `complexity` and `weight` tags | `eval/golden/contoso.jsonl` | ✅ | n/a |
| live3: 48 questions × 4 agents, rules + Decision-1 + Foundry + Fabric SDK | `eval/agent_eval.py` | ✅ report | Script |
| Capacity sweep F32 / F64 / F128 (full golden set each, 2 warm-up calls per agent) | `capacity.py resize` + `agent_eval.py collect/latency` | ✅ report | Script |

**Results (rules, impact-weighted in brackets)**: `lab_da_hub_rich` **94 % (93 %)**, `da_contoso_ontology` 83 % (79 %), `da_contoso_360` 83 % (82 %), `lab_da_hub_bare` 75 % (77 %). No agent disclosed PII. All four were stopped by the service's prompt-injection filter on the "admin mode" question.

| Category | 360 | ontology | hub_bare | hub_rich | Reading |
|---|---|---|---|---|---|
| business_definition | 100 % | 0 % | 0 % | 100 % | "At risk" and "open alert" exist only in instructions; the ontology has no such definitions |
| business_rule | 50 % | 50 % | 50 % | 100 % | The ontology's BatteryWarrantyReview rule didn't change the ontology agent's answer |
| freshness | 0 % | 100 % | 0 % | 100 % | 360/bare counted raised+cleared events; ontology and rich counted distinct raised alerts |
| negation | 0 % | 100 % | 100 % | 100 % | 360 had to anti-join across KQL and SQL |
| impact_analysis | 75 % | 50 % | 75 % | 75 % | Three agents counted only *financed* sites for the inverter recall (23 vs 28); the question allows both readings |
| abstain | 67 % | 67 % | 67 % | 67 % | Agents relabel a financing product as a "tariff" or report 0.00 revenue instead of declining |

- **Instructions vs ontology.** On the same source, extensive instructions beat the ontology by 11 points (14 weighted). The ontology beat bare instructions by 8 points (2 weighted), mainly on freshness and comparison. The ontology encodes entities, keys and relationships; it doesn't carry business definitions, so those questions fail unless they are written down.
- **Complexity.** Multi-hop questions were *not* the hardest: same-source arms scored 83–92 % there. 360 dropped to 67 % because it joins across engines in the AI layer.
- **Graders on 192 answers.** Agreement with rules: Decision-1 93 % (kappa 0.78, 1 false pass), Foundry Similarity 90 % (0.66, 5 false passes, 11 errors), Fabric SDK 90 % (0.68, 9 false passes on its own answers). Decision-1 is the closest to the rules and the cheapest; the LLM judges are lenient on wrong numbers phrased like the expected answer.
- **Capacity latency.** The median answer took 13.2 s at F32, 13.5 s at F64 and 12.4 s at F128 (p95 28–36 s). SQL/KQL inside the agent stayed at ~1 s and direct engine queries at ~0.7 s. Latency is AI orchestration, not capacity; F32 was enough for one evaluation run (no 429s). F2 throttles only because streaming and agent CU share the capacity. F512 was skipped (the sweep was interrupted before it, and the trend was flat).
- **Grader fixes found by live3.** A run that the injection filter stops can still return a refusal: forbidden-data questions are judged on what was returned. Agents write typographic apostrophes ("can’t"); declines are matched after normalizing them. Abstain checks look for presented figures (money, decimals, units), not any integer.

### live4: storage format latency (dedicated capacity)

Report: [eval/reports/live4.md](../eval/reports/live4.md). Decision: D-040. Runs on its **own F128 capacity** (`contosoenergylablive4`) and workspace `ce-live4-dev`, so it never shares CUs with the live2.5 loop on the lab capacity.

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Capacity + workspace (dev roles, Asset domain) | `infra/live4_stores.py capacity` / `workspace` | ✗ | Script |
| Frozen snapshot of 6 hub tables from `lh_asset` (PII columns dropped) | `live4_stores.py snapshot` → `infra/.state/live4/` | ✗ | Script |
| `lab_lh_store` (Delta written with `deltalake`), `lab_sqldb_store`, `lab_eh_live4`/`lab_kql_store` | `live4_stores.py lakehouse` / `sqldb` / `eventhouse` | ✗ (lab_) | Script |
| Private Azure SQL (Entra-only) + VNet, private endpoint, DNS zone group, managed private endpoint load, VNet data gateway, `lab_mir_store` | `live4_stores.py azuresql` (`infra/live4_azuresql.py`) | ✗ | Script |
| Mirror connection `lab-live4-azuresql` (OAuth2 through the VNet gateway) | Portal: Manage connections and gateways | ✗ | Manual |
| Agents `lab_da_live4_{lh,sqldb,mir,kql}`: same instructions, no few-shots | `live4_stores.py agents` | ✗ (lab_) | Script |
| Cross-check (41 truths × 5 engines), engine bench, agent run (3 reps), report | `eval/live4.py crosscheck` / `bench` / `agents` / `report --publish` | ✅ report | Script |
| Pause the live4 capacity | `live4_stores.py pause` | n/a | Script |

**Results**: agent median 10.5–10.8 s and accuracy 91–94 % on every store; in-agent query ~1 s everywhere. Engine-only (net of the round trip): SQL database engine 1 ms, Eventhouse 4 ms, analytics endpoints (lakehouse, SQL database endpoint, mirror) 31–48 ms.

- **The store doesn't move agent latency**; orchestration dominates (as D-038 found for capacity).
- **SQL database sources are queried through the SQL analytics endpoint**, not the database engine, so three of four arms use the same engine. The agent definition type is `sql_database` (not in the documented enum).
- **Lakehouse Load Table API** writes parquet timestamps as `timestamp_ntz`, which the SQL analytics endpoint hides; write Delta with UTC timestamps instead.
- **Private Azure SQL**: Entra-only auth is enforced by policy; the private endpoint needs a DNS zone group or the gateway resolves the public name; the VNet gateway offers Basic/OAuth2/service principal for SQL (no workspace identity), and the first OAuth2 attempt can fail with "invalid token" (a retry worked). West US 2 refused new SQL servers, so the server is in West US 3.

### live2.5: evaluation feedback loop (Eventhouse log, optimizer, guardrails)

Report with the architecture: [eval/reports/live2.5.md](../eval/reports/live2.5.md). Design: D-039.

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Eventhouse log `lab_agent_eval` (in `eh_fleet`): runs, answers, **steps (every tool call: source, type, query, duration)**, scores (+grader tokens), agent versions, improvements (+optimizer tokens), loop events | `infra/agent_eval_kql.py create` | ✗ (lab_ item) | Script |
| Real-Time dashboard `lab_rtd_agent_eval` (Quality / Sources & tokens / Loop) and Activator `act_agent_eval_regression` | `agent_eval_kql.py dashboard / activator` | ✗ (lab_, act_; recipients in `.env`, D-018) | Script |
| Loop arms with ontology + source data: `lab_da_360_onto` (ontology + eh_fleet + lh_finance + lh_asset hub), `lab_da_onto_hub_bare` (ontology + hub, one line) | `data_agents.py create --only ...` | ✗ | Script |
| Optimizer `optimizer-gpt-4-1` (Foundry) | `infra/foundry.py create` | Script | Script |
| Loop: baseline, rounds, final test | `py eval/feedback_loop.py run` (`finalize` resumes the last step) | ✅ report | Script |

**Results (F128, 3 rounds max)**:

| Arm | Baseline (train / validation) | Rounds | Proposals accepted | Final, all 48 questions | Test split (12) |
|---|---|---|---|---|---|
| A `lab_da_360_onto` | 21/22, 12/14 | 2 (stopped after 2 rejections) | 0 | 96 % | 12/12 |
| B `lab_da_onto_hub_bare` | 16/22, 12/14 | 2 | 0 | 77 % | 9/12 |

- **Ontology + all sources + 4 lines of instructions was the strongest agent so far** (96 % on all 48 questions; `lab_da_hub_rich` scored 94 % in live3). It used lh_asset for 50 % of its queries, lh_finance for 27 % and eh_fleet for 23 %, at ~40K prompt tokens per answer (hub-only: ~26K).
- **The gate rejected every proposal, correctly.** Arm B's round-2 candidate dropped validation from 12 to 9 and regressed multi_hop; round 1 gained 2 train questions but lost 1 validation question.
- **The optimizer invents business definitions it can't see.** It inferred "closest to warranty review = earliest warranty end" from an expected battery ID; the rule is the lowest state of health under warranty. Definitions that live only in people's heads can't be learned from failures; they have to be written down (as `lab_da_hub_rich` did).
- **Few-shots filtered on invented values** ('West' region, 'Moderate' severity, 'Li-Ion' chemistry). They executed, so they passed; few-shots must now also return at least one row.
- **Noise is about the size of the acceptance threshold.** The unchanged heads scored 21→20→19 and 16→15→14 train questions across runs. Pairing head and candidate in the same run keeps the comparison fair; a bigger golden set or repetitions are needed to detect gains of 1–2 questions.
- **Runner robustness.** One hung connection stalled a run for 80 minutes (OpenAI client default: 600 s timeout × 3 attempts). Agent calls now use a 60 s timeout and a 420 s cap per question, with one progress line per answer. The ontology never appears as its own tool call; agents route to a lakehouse or KQL source and use the ontology as context.
- **Next phase (planned):** evaluation in prod with a human in the loop: anomaly → diagnosis + suggestion → human approval → CI/CD (fabric-cicd) deploys the agent definition.

## Step 12: Operations agent vs Activator ✅ (issue #36), both measured; agent remediation action not attached

Results: [monitoring/operations-agent.md](../monitoring/operations-agent.md).

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Obs Eventhouse `eh_obs` (`obs_fleet` = eh_fleet KQL, `obs_monitor` = cap_monitoring KQL), `es_obs_fleet`, `es_obs_capacity`, dims, Activator clones | `infra/obs_lab.py ops` | No (D-041) | Script |
| Own simulator stream with timed injections | `contoso_gen stream ... --inject TYPE:SITE@+MIN --target obs=conn:ESTREAM_OBS_FLEET_CONNECTION_STRING --out out_obs_p1` | Generator ✅ | Ground truth in `data-gen/out_obs*` (gitignored) |
| Phase 2: `pl_obs_daily` (failure injection), `pl_obs_remediation`, `es_obs_jobs`, `act_obs_job_autofix` (runs a pipeline) | `obs_lab.py jobs`; failure: `run_job('pl_obs_daily', {'failure_mode': 'fail'})` | No | Script |
| Probe Activator `act_obs_fault_probe` (firing = pipeline run = durable evidence) | inline in the step (see results doc) | No | Script |
| External approver: B2B guest, group `ce-fabric-ops-approvers`, Fabric (Free) license, Contributor on `ce-obs-ops` (group + direct), guest browsing for the group only | `infra/ops_approver.py setup / status / remove` (Graph + Fabric REST + Admin API) | No (identity in `.env`) | Script (D-044) |
| API-authored agents `opa_obs_fleet`, `opa_obs_jobs`, `opa_obs_smoke` (instructions v2, sources, destination) | `infra/operations_agent.py create --phase 0/1/2`, `status` | No | Script; **can't generate a playbook** (D-045) |
| **Portal-authored agents** `opa_obs_portal` (battery_temp_c above 55), `opa_obs_jobs_portal` (pl_obs_daily becomes Failed): create, knowledge, Generate playbook, Save, Start | **Portal only** | No | Checklist |
| Evidence: ground truth, telemetry, alerts, Activator firings, agent rule queries (`EventhouseQueryLogs`) | `operations_agent.py evidence --since <UTC>` | ✅ script | Script |

**Verified (2026-11-14 trial, F64)**:
- **Phase 1 (3 battery-overheat injections):** Activator fired 3/3, 4–5 min after the device alert and about 10 min after the anomaly started. `opa_obs_portal` ran its generated rule query every 5 min (7 runs, about 76 ms each) and sent Teams messages to the creator.
- **Phase 2 (one `pl_obs_daily` failure at 04:14:39 UTC):** the agent's rule query matched at **04:17:17 (2 m 38 s)** and posted to Teams with the job id and an *Investigate further* option. It then ran a 7-day follow-up query (investigation). Activator auto-remediated at **04:21:21 (6 m 42 s)**, logged as `Manual`.
- **Agent remediation:** the message said "I don't have any actions available to recommend", because the Fabric item action wasn't attached to `opa_obs_jobs_portal`. So nothing was approved or run.

**Blockers → solutions (operations agent)**:
- **API-created agents never generate a playbook.** Even a one-line rule fails with "I was not able to process your request". Their source reads back with id `0000…`. A portal-created agent with the same text generated at once, so author agents in the portal (D-045).
- **The definition API doesn't reflect portal agents.** `getDefinition` returns empty instructions, sources and playbook, so the agents can't be versioned or deployed as code.
- **Generation refuses rules it can't express**, and lists what's missing (e.g. how to derive "gateway offline 20 min", region, duration). Rewrite as one property + *is above / becomes*.
- **`FabricJobAction` rejected by the API** (UnknownError); add it in the portal.
- **Guest approver:** the corporate Microsoft account couldn't be invited (home-tenant restriction), so a personal-domain account was used. The recipient setting wasn't found under *Agent behavior*, and the guest hadn't accepted, so the creator stayed the recipient.
- **Graph CAE challenge** again (`TokenCreatedWithOutdatedPolicies`); re-run `az login` before `ops_approver.py status`.

**Checklist (portal)**:
- `opa_obs_jobs_portal` → **Add action** → Fabric item `pl_obs_remediation` (job type Pipeline) → Save → Start; rerun the Phase 2 failure and approve in Teams.
- The guest accepts the invitation and installs *Fabric Operations Agent* in Teams in this organization; set them as recipient when the setting is available.
- Pause the obs capacity when idle: `py infra/capacity.py pause --target obs` (done after the trial).

**Flags**:
- **Preview:** operations agent (Investigator insights preview), Activator on KQL, Real-Time Hub job events.
- **Requirements for the agent:** a paid capacity (no trial), the Copilot + Azure OpenAI tenant settings (cross-geo processing only outside US/EU), Teams with the *Fabric Operations Agent* app.
- **Recipients** must be in the organization and have write on the agent item.

## Step 13: Work IQ - SharePoint and Teams knowledge with a Fabric data agent 🟨 (issue #42), Copilot-licence parts gated

Results and architecture: [work-iq.md](work-iq.md).

| Task | Tool | Git-tracked? | Capture |
|---|---|---|---|
| Lab app `ce-lab-m365` (public client, delegated Graph: Sites, Files, Group, Teams channel) + admin consent | `infra/work_iq.py app` (Graph with the CLI token) | No (client id in `.env`) | Script |
| M365 group `ce-fieldops` -> SharePoint team site + Teams team "Contoso Energy Field Ops" | `work_iq.py site` | No | Script |
| Synthetic content: policy (P1..P7) and per-alert procedures in the General channel files, list "Field Visits" (97, from real alerts), 8 dispatcher posts in the channel | `work_iq.py content` | Generator in the script | Script |
| SharePoint + Teams -> `lh_asset.fieldops_{visits,procedures,policy,channel_messages}` (Delta) | `work_iq.py ingest` (Graph -> deltalake) | No (data) | Script |
| OneLake SharePoint shortcut `lh_asset/Files/fieldops_sharepoint` (preview): workspace identity, `Sites.Selected` + read on one site, connection | `work_iq.py shortcut` | No | Script; **blocked** (Unauthorized), portal fallback |
| Data agent `da_contoso_fieldops` (hub + fieldops tables, vocabulary, 5 few-shots), dev only (`DEV_ONLY_ITEMS` matches `fieldops`) | `infra/data_agents.py create --only da_contoso_fieldops`; test `infra/work_iq_test.py` | ✅ from dev, not deployed | Script + Git |
| Agent answer posted to the Teams channel (licence-free) | `work_iq.py digest` | No | Script |
| Publish `da_contoso_fieldops` to the Agent Store; use it in Teams → Copilot | Portal (**Publish to Agent Store**) + Business Premium on the user (Copilot Chat) | No (server-side flag) | Checklist (D-047) ✅ |
| Work IQ, Copilot Studio connected agent | Portal + **Microsoft 365 Copilot licence** | No | Gated (D-046) |

**Verified**: 4 cross-source questions (SharePoint list + files + Teams messages + fleet + finance): 3 correct, 1 partly (cited labour policy P5 but not P7 for Lease/PPA), PII request refused. Before tuning 0/3: unselected channel table, unmapped vocabulary ("battery overheating" = `BATT_OVERTEMP`), one "irrelevant or harmful" rejection.

**Blockers -> solutions**:
- No Microsoft 365 Copilot licence: Work IQ / Copilot Studio documented as gated. The agent in Teams works with Copilot Chat: the admin had only Teams Exploratory + Fabric Free, so the spare Business Premium seat was assigned.
- Azure CLI token rejected by SharePoint (401) and missing Sites/Files/ChannelMessage scopes: dedicated lab app with admin-consented delegated scopes.
- Graph CAE challenge: `az logout` then `az login`.
- **A plaintext token cache in the OneDrive-synced repo folder was flagged by Purview** (gitignored, never committed): caches moved to `%LOCALAPPDATA%\ce-fabric-lab` and DPAPI-encrypted, the lab app deleted and re-created (invalidates the exposed refresh token), test-user sessions revoked, test-user az profile moved too.
- SharePoint shortcut with workspace identity: still Unauthorized after the documented grants. Fallback: portal shortcut with an organizational-account connection.

**Checklist (portal)**: `lh_asset` -> New shortcut -> SharePoint Folder -> organizational account -> `Shared Documents/General/Field Ops`. Teams: open the Field Ops team, check the channel files, the Field Visits list (as a tab) and the digest post. After any `data_agents.py create` rerun, re-check **Publish to Agent Store** on `da_contoso_fieldops`. With a Microsoft 365 Copilot licence: add it to a Copilot Studio agent with the SharePoint site as knowledge.

**Flags**: preview: data agent, SharePoint shortcuts, Copilot Studio connected agents. Licence: Microsoft 365 Copilot for Work IQ and agent publishing; Teams licence for the team; Fabric paid capacity for the data agent.

---

## Per-step close-out (working rules)
1. Work on `dev` in small commits that reference the issue (`#N`).
2. Verify in the dev workspace, then commit it to Git from Fabric (Source control → Commit).
3. Update this runbook (status) and [decision-log.md](decision-log.md).
4. Open a PR dev → main. Merging deploys prod via fabric-cicd.
5. Stop and summarize.
