# Runbook: Contoso Energy on Microsoft Fabric

Sample, not production guidance (see [DISCLAIMER](../DISCLAIMER.md)). Status legend: ⬜ todo · 🟨 in progress · ✅ verified in dev · 🚀 promoted to prod.

**Tool key**: *Agent* = GitHub Copilot agent mode in VS Code · *Core MCP* = Fabric Core MCP (remote, tenant ops) · *Fabric MCP* = Fabric MCP (local, item definitions/API specs) · *Skills* = Skills for Fabric · *fab* = Fabric CLI · *Portal* = Fabric/Azure/GitHub UI.
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
| 0.5 | Tenant settings (Copilot, data agent, ontology, GitHub sync, SP APIs, workspace monitoring, certification) | fab api → `infra/tenant_settings.py [--apply]`; Core MCP to inspect; Portal for anything unmatched | No (tenant state) | Script + Checklist §B |
| 0.6 | Entra groups, CI/CD SP + OIDC federated credentials (no secret) | az → `infra/entra_setup.py` | Script | Script |
| 0.7 | Domains Fleet/Asset/Finance; 6 workspaces `ce-{fleet,asset,finance}-{dev,prod}`; capacity + domain assignment; workspace roles | fab → `infra/fabric_setup.py` (Core MCP equivalent for ad-hoc checks) | Script + `infra/config/lab.json` | Script |
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
| `eh_fleet` Eventhouse, `lab_compare` KQL DB, workspace identity, Event Hubs connection, 2 eventstreams; custom endpoint key → `.env` | Fabric REST → `infra/rti_lab.py create` (Fabric MCP for definition schemas) | ✅ items committed **from Fabric** to `dev:/workspaces/ce-fleet` | Script + Git |
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
| KQL sources (bronze/silver/gold/API) | Agent; Fabric MCP for KQL DB definition rules | ✅ `infra/kql/eh_fleet/*.kql` (source of truth) | n/a |
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
| `act_fleet_device_faults` | Activator: KQL source (5 min, time-axis on alert arrival) → EventTrigger `event_type = raised` AND `severity ≠ Low` → email | Fabric REST (Reflex definition; shapes from **Skills for Fabric** `activator-cli` + RTI MCP generators) → `rti_alerts.py activators` | **No**: holds recipient address (D-018) | Script (`--env dev/prod`) |
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

## Step 8: Governance ⬜
OneLake catalog, domains, endorsement, lineage/impact, **OneLake security RLS/CLS (preview)**, shortcut security, workspace roles.

## Step 9: Purview ⬜
Sensitivity labels (M365 E3/E5 or equivalent label licensing), DLP for lakehouse/semantic model (E5 / E5 Compliance-class licensing; some Fabric DLP features preview), audit, Data Map for external sources (Purview pay-as-you-go). **Licensing flag.**

---

## Per-step close-out (working rules)
1. Work on `dev` in small commits that reference the issue (`#N`).
2. Verify in the dev workspace, then commit it to Git from Fabric (Source control → Commit).
3. Update this runbook (status) and [decision-log.md](decision-log.md).
4. Open a PR dev → main. Merging deploys prod via fabric-cicd.
5. Stop and summarize.
