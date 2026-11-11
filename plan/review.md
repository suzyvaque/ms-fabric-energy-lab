# Contoso Energy on Microsoft Fabric: end-to-end review

Sample, not production guidance ([DISCLAIMER](../DISCLAIMER.md)). Synthetic data. This review covers Steps 0–10. For each step it gives the goal, actions, blockers and solutions, the end state, where to look (Fabric portal and this repo), and the insights worth carrying forward. Detailed procedures are in the [runbook](runbook.md); every decision and its alternatives are in the [decision log](decision-log.md).

Contents: [How the environment was built](#how-the-environment-was-built) · [Tools](#tools) · [Steps 0–10](#step-by-step) · [All blockers](#blockers-at-a-glance) · [Open portal checklist](#open-portal-checklist) · [Top insights](#top-insights) · [Cost and teardown](#cost-and-teardown)

---

## How the environment was built

```
GitHub repo (public)                 Microsoft Entra ID                  Azure subscription
 main ── PR only, gitleaks ──┐        groups: admins / engineers /        resource group
 dev  ── Fabric Git sync     │        viewers / automation                 ├─ Fabric capacity F2 (pay-as-you-go, pausable)
 Actions: fabric-cicd (OIDC) │        CI/CD service principal              └─ Event Hubs Standard 1 TU (Step 1b only)
          governance-test    │        (federated credentials, no secret)
                             ▼
Fabric tenant (tenant settings scripted)
 Domains: Fleet · Asset · Finance
 Workspaces (all on the one F2):  ce-fleet-dev / -prod · ce-asset-dev / -prod · ce-finance-dev / -prod
   dev  = authoring, Git-connected to dev:/workspaces/ce-*   (commit from Fabric → PR → main)
   prod = deploy-only, published by fabric-cicd from main with parameter.yml ID rebinding
```

| Layer | How it was created | Where it lives |
|---|---|---|
| Local Git identity | Per-repo `user.name` / GitHub no-reply email; SSH host alias for the personal account; pre-commit hook rejects other emails | `tools/check_git_identity.py`, `.pre-commit-config.yaml` |
| Leak guards | gitleaks (pre-commit + CI), a private-terms hook fed from `.env`, a mojibake hook, GitHub secret scanning + push protection, `main` protection (PR only) | `.gitleaks.toml`, `tools/`, `.github/workflows/gitleaks.yml`, `infra/github_setup.py` |
| Capacity | ARM through the Fabric CLI; pause/resume/resize scripted | `infra/capacity.py` |
| Entra | Groups + CI/CD service principal + **two** OIDC federated credentials per environment (name- and ID-based subjects) | `infra/entra_setup.py` |
| Tenant settings | Admin API, matched by `settingName`; required vs optional settings | `infra/tenant_settings.py` |
| Domains, workspaces, roles, Git connect | Fabric CLI (`fab api`) + REST | `infra/fabric_setup.py`, `infra/config/lab.json` |
| GitHub | Repo settings, environments, secrets, issues | `infra/github_setup.py` |
| Items | Scripts build item **definitions** via REST in dev → *Commit to Git from Fabric* (`infra/git_sync.py`, selective) → PR → `fabric-deploy` publishes to prod | `infra/*.py`, `workspaces/ce-*/` |
| Secrets / tenant facts | `.env` (gitignored) and GitHub secrets only | `.env.example` lists the keys |

**Item lifecycle rules** ([common.py](../infra/common.py)):
- `UNMANAGED_ITEMS` are never Git-synced or deployed: `lab_*` experiments, `act_*` Activators (recipients are tenant accounts), and children Fabric auto-creates for ontologies and monitoring.
- `DEV_ONLY_ITEMS` (`cap_*`, `mon_*`) are Git-tracked from dev but not deployed: one capacity means one monitoring instance (D-027).
- Data never goes to Git. It's re-created by the scripts from the seeded generator.

## Tools

| Tool | Used for | Notes |
|---|---|---|
| **GitHub Copilot agent mode (VS Code)** | Wrote and ran every script, KQL, PySpark, DAX/TMDL, PBIR, docs; drove the step loop | This review was produced the same way |
| **Fabric REST API** (`infra/fabric_rest.py`) | Item create/update with definitions (LRO), jobs, Git commit, data access roles, monitoring items, OneLake settings, catalog search, admin APIs | Retries for DNS/VPN blips (D-022) |
| **Fabric CLI** (`fab`, `fab api`) | Capacity (ARM), workspaces, domains, roles, tenant settings, Git connect | D-004 |
| **Kusto REST** | KQL management/queries against Eventhouse (by database GUID) | `kql()` in `fabric_rest.py` |
| **T-SQL** (`mssql-python` + Entra token) | SQL analytics endpoint checks, DENYs | Now commits (DCL was silently rolled back before) |
| **OneLake DFS API** (`infra/onelake.py`) | Open-mirroring landing zone, partner files, diagnostics logs | |
| **Power BI REST** | Refresh/frame Direct Lake, `executeQueries` (DAX), Export-to-PDF render checks, Scanner API, activity events | |
| **Assistants-compatible data agent endpoint** (OpenAI SDK) | Automated agent tests and comparisons | Preview |
| **fabric-cicd** | Prod deploy (main → prod) with `parameter.yml` rebinding; orphan cleanup with exclusions | `infra/deploy.py`, `.github/workflows/fabric-deploy.yml` |
| **Azure CLI** | Tokens, Entra, Event Hubs, Cost Management | |
| **GitHub CLI** | Issues, PRs, checks, workflow dispatch, merges | |
| **Skills for Fabric** (microsoft/skills-for-fabric) | Activator definition shapes (`activator-cli`); its issue #107 solved the ontology graph blocker | Read from GitHub, not installed as an agent skill |
| **Microsoft Learn item-definition articles** | Definition formats (ontology TMDL, data agent, monitoring, eventstream template) | The decoded samples were the most reliable source |
| **Fabric Core MCP / Fabric MCP** | **Not used.** They were planned in the brief but not connected to the agent session that built the lab; REST + Learn definition docs covered their role | Connect them for interactive tenant exploration (listing items, reading API specs) next time |
| **Portal (manual)** | Only where no API exists; each case is captured as a checklist | See [open checklist](#open-portal-checklist) |

---

## Step by step

### Step 0: Platform and guardrails · issue #1 · PRs #15–#16
- **Goal:** a secure, reproducible scaffold. That means identity, leak guards, capacity, tenant settings, a service principal with OIDC, domains, dev/prod workspaces, Entra groups and Git integration.
- **Actions:** the scripts in the table above, run in order (runbook Step 0).
- **Blockers → solutions:**
  - Prod deploy failed with `AADSTS700213`. New repos send an **ID-based** OIDC subject, so both subjects are now registered (D-006).
  - Git connect returned `403 FeatureNotAvailable`. The *GitHub sync* tenant setting was off, and the script matched settings by title instead of `settingName`. Both were fixed.
- **End state:** 6 workspaces in 3 domains on one F2. Dev is Git-synced to `dev`; prod is deploy-only. `main` accepts PRs only.
- **Look at:**
  - **Portal:** Admin portal → Domains; workspace settings → Git integration; Admin portal → Tenant settings.
  - **GitHub:** Settings → Branches / Environments / Secret scanning.
  - **Repo:** [runbook Step 0](runbook.md#step-0-prereqs-and-platform--issue-1-prs-15-16), [checklists/step-0-portal.md](checklists/step-0-portal.md).
- **Insights:**
  - Script tenant settings by `settingName` and treat propagation (up to 15 min) as part of the run.
  - Register both OIDC subject formats.
  - Keep prod deploy-only from the start; it makes every later step reviewable.

### Step 1a: Mock data · issue #2
- **Goal:** realistic synthetic telemetry, alerts, assets, customers, contracts and payments, with anomalies, late events and duplicates.
- **Actions:** stdlib Python package `data-gen/contoso_gen` (batch, stream, replay) with ground truth and 9 tests.
- **Blockers:** none.
- **End state:** reproducible from a seed. Anomalies and their ground truth are written to `data-gen/out/`.
- **Look at:** [data-gen/README.md](../data-gen/README.md) (event contract), `data-gen/tests/`.
- **Insight:** ground truth for injected anomalies is what made recall measurable later (Step 1c).

### Step 1b: Ingestion comparison · issue #3
- **Goal:** the same simulator through (i) Event Hubs → Eventstream vs (ii) Eventstream custom endpoint.
- **Actions:** `infra/eventhubs.py`, `infra/rti_lab.py`, and a tee benchmark (`data-gen/benchmarks/ingest_compare.py`).
- **Blockers → solutions:**
  - Tenant governance forces Event Hubs **SAS (local auth) off**. The classic source needs SAS, so I used the *AzureEventHubExtended* source with **workspace identity** (preview).
  - The network blocks AMQP port 5671, so producers use AMQP over WebSockets (443).
- **End state:**
  - Median latency is a tie (~2 s).
  - Event Hubs at 1 TU throttles above ~1,000 events/s.
  - A processor eventstream costs ≈ 1 CU continuously (~50 % of an F2).
  - Recommendation: **custom endpoint → Eventhouse direct ingestion** (D-009).
  - The lab streams are paused.
- **Look at:**
  - **Portal:** ce-fleet-dev → `lab_es_*` eventstreams (topology).
  - **Repo:** [ingestion-comparison.md](ingestion-comparison.md).
- **Insights:**
  - Use Event Hubs only when you need its ecosystem (Kafka clients, capture, geo-DR) or very high predictable scale.
  - On small SKUs, the processor is the real cost; direct ingestion avoids it.

### Step 1c: Eventhouse medallion · issue #4
- **Goal:** bronze/silver/gold with update policies, materialized views (MVs), Fleet API functions and retention/caching.
- **Actions:**
  - KQL source of truth in `infra/kql/eh_fleet/*.kql`, applied as the KQL DB **definition** (`fleet_medallion.py apply`).
  - `es_fleet` with a custom endpoint feeding direct ingestion.
- **Blockers → solutions:**
  - `DatabaseSchema.kql` accepts only an allow-list of commands, and no MV backfill. Documented a drop/recreate procedure.
  - `avgif` returns NaN for empty groups, so gold stores sums and counts.
  - Orphan pretty-named databases break name-based calls, so everything addresses databases by GUID.
- **End state:**
  - 183k rows; 0.47 % duplicates removed; 1.07 % late events flagged.
  - Recall: overtemp 3/3, inverter faults 4/4, stuck SOC 1/3. The 2 misses are undetectable from the data.
- **Look at:**
  - **Portal:** `eh_fleet` → database → Tables / Materialized views / Functions; *Explore your data* with `FleetKpis()`.
  - **Repo:** [infra/kql/eh_fleet/](../infra/kql/eh_fleet/), `workspaces/ce-fleet/eh_fleet.*`.
- **Insights:**
  - Keep readable KQL in the repo and let Fabric own the serialized definition.
  - Measure detection against ground truth; some "misses" are physics, not bugs.

### Step 1d: Finance lakehouse + external source · issue #5
- **Goal:** a finance medallion plus an external source (ADLS shortcut and mirrored Azure SQL were requested).
- **Actions:**
  - **Open mirroring** `mir_loan_servicing` (a simulated OLTP writes parquet change files).
  - `lh_asset` (Asset domain), a cross-domain shortcut, and the PySpark notebook `nb_finance_medallion`.
- **Blockers → solutions:**
  - **New Azure SQL servers and storage accounts are forced to public network access Disabled** by tenant governance (not Azure Policy, so no exemption). You chose Fabric-native substitutes (D-014).
  - On F2 the Starter Pool never starts (`TooManyRequestsForCapacity`), so a Small-node pool is used (D-015).
  - Load-to-Table doesn't support schemas, so table names use prefixes (D-016).
  - SQL endpoint metadata lag is handled by forcing `refreshMetadata`.
- **End state:**
  - CDC visible in the SQL endpoint in **38 s**, including a delete for a right-to-erasure request.
  - 11/11 data-quality checks pass.
  - Collection rate 95.86 %. Prod matches dev.
- **Look at:**
  - **Portal:** ce-finance-dev → `mir_loan_servicing` (replication status), `lh_finance` (shortcut icons on `bronze_*`, `ref_sites`), notebook run history.
  - **Repo:** [infra/notebooks/nb_finance_medallion.py](../infra/notebooks/nb_finance_medallion.py), [workspaces/ce-finance/parameter.yml](../workspaces/ce-finance/parameter.yml) (cross-workspace rebinding).
- **Insights:**
  - Open mirroring gives the same CDC demo with no network dependency.
  - Private-networking tenants need a VNet data gateway / private endpoints module before real mirroring and ADLS shortcuts work.

### Step 1e: API layer · issue #6
- **Goal:** an API over gold data.
- **Actions:** **API for GraphQL** `gql_finance` (query-only, PII fields unmapped) plus a Fleet API client over KQL functions.
- **Blockers:** none. User Data Functions were deferred because there was no write use case (D-017).
- **End state:** nested queries work; requesting `email` fails schema validation.
- **Look at:**
  - **Portal:** `gql_finance` → schema explorer (no `email`).
  - **Repo:** `infra/api_layer.py`.
- **Insight:** field mapping is a cheap first privacy layer, but it isn't access control. Steps 8–9 add that.

### Step 2: Real-Time Dashboard + Activator · issue #7 · PRs #22–#23
- **Goal:** an operations dashboard and alerts for device fault, offline device and pipeline failure.
- **Actions:**
  - `rtd_fleet_ops`: 11 tiles, each query smoke-tested before deploy.
  - `act_fleet_device_faults` and `act_fleet_offline_sites` (KQL source → EventTrigger → email). Shapes come from Skills for Fabric.
- **Blockers → solutions:**
  - Activator definitions hold recipient UPNs, so they're kept out of Git and script-managed (D-018).
  - "Offline" is modeled as a one-time transition event so it doesn't re-fire.
  - The pipeline-failure alert moved to Step 7.
  - PowerShell 5 corrupted the runbook encoding; a mojibake hook now blocks this.
- **End state:** both rules are running in dev and prod. They were re-created after a deploy bug deleted them (see Step 5).
- **Look at:**
  - **Portal:** `rtd_fleet_ops`; Activator items → *History*.
  - **Repo:** `infra/rti_alerts.py`.
- **Insight:** poll intervals (5 min) are the Activator cost/latency dial. Alert on **transitions**, not states.

### Step 3: Data agents · issue #8 · PR #24
- **Goal:** Eventhouse, lakehouse and combined agents, published to Teams/M365 Copilot.
- **Actions:**
  - Agents: `da_fleet_ops`, `da_finance` (PII columns deselected), `da_contoso_360`.
  - Tested via the Assistants-compatible endpoint.
- **Blockers → solutions:** the agent's NL-to-KQL **can't see materialized views or functions**, so the fleet agent uses silver tables, with few-shots encoding the gold logic (D-020).
- **End state:**
  - Answers match SQL/KQL ground truth.
  - The PII request was refused.
  - Publishing to Teams/M365 Copilot is a portal step (pending).
- **Look at:**
  - **Portal:** each agent → *Test* pane and run steps.
  - **Repo:** `infra/data_agents.py` (`test`, `compare`).
- **Insight:** design gold as **real tables** if agents must use it. Instructions and few-shots carry the business rules.

### Step 4: Semantic model + Power BI · issue #9 · PR #25
- **Goal:** KPIs in a Direct Lake model; certify one gold KPI.
- **Actions:**
  - `sm_finance`: TMDL generated from the live schema, with 13 measures.
  - `rpt_finance_kpis` (PBIR), rendered via Export-to-PDF.
- **Blockers → solutions:**
  - Image export is off in the tenant, so rendering is checked via PDF.
  - Rendering exposed real bugs: the date-dimension start, blank trend months (`+0`), and the table visual, now a matrix.
  - Certification has **no API**, so it's a portal checklist.
- **End state:** DAX = SQL exactly (collection 95.86 %, delinquency 6.02 %), in dev and prod.
- **Look at:**
  - **Portal:** `sm_finance` → Settings → Endorsement; `rpt_finance_kpis`.
  - **Repo:** `infra/semantic_model.py verify`.
- **Insight:** a scripted DAX-vs-SQL parity check plus a render check catches errors no schema validation will.

### Step 5: Fabric IQ ontology · issue #10 · PR #26
- **Goal:** an ontology with Site, Device, Battery, Alert, Contract and Customer, plus an ontology agent, contrasted with the data agent.
- **Actions:**
  - `ont_contoso` (TMDL): 6 entities, 5 relationships, Device telemetry as time series, 1 rule, no PII.
  - Bound to `lh_asset` as a hub, with shortcuts to Eventhouse (OneLake availability) and finance gold.
  - New agent `da_contoso_ontology`: ontology for context, `lh_asset` for execution.
- **Blockers → solutions:**
  - **Undocumented `ONT_*` partition annotations** are required, or the graph marks every entity *Ineligible* (skills-for-fabric#107).
  - `decimal` columns need `decimal` properties, and time-series tables need `additionalBackingTable`.
  - An ontology binds through **one** SQL endpoint, hence the hub design.
  - MVs can't be mirrored to OneLake, so I added `gold_alert_events` (a real table).
  - The agent's `ontology` source type works but is undocumented.
  - **Regression found:** fabric-cicd orphan cleanup had deleted the prod Activators. Fixed with `UNMANAGED_ITEMS`.
  - F2 Kusto 429s lasted more than 15 minutes; pause/resume cleared them.
- **End state:** same answers as `da_contoso_360` in about half the time, with no join instructions. The graph is not materialized (optional portal step).
- **Look at:**
  - **Portal:** `ont_contoso` (entity diagram, *Ontology agent* chat, *Manage graph*), `da_contoso_ontology`.
  - **Repo:** [ontology-vs-data-agent.md](ontology-vs-data-agent.md), `infra/ontology.py`.
- **Insights:**
  - The ontology moves business meaning out of prompts into a governed, reusable artifact.
  - The trade-off is freshness: Eventhouse data arrives through OneLake availability, minutes behind.

### Step 6: Capacity monitoring · issue #11 · PR #27
- **Goal:** compare the Metrics app vs RTI vs Power BI/APIs, check FUAM, and reproduce throttling on a small SKU.
- **Actions:**
  - Capacity overview events → `cap_monitoring` → `cap_rtd_capacity` + `act_capacity_70`.
  - Workspace monitoring (`mon_*`, Linked to one Eventhouse).
  - Collector over REST, activity events, Cost Management and the RTI aggregates → `cap_lh_capacity` → `cap_sm_capacity` → `cap_rpt_capacity`.
  - Load generator `monitoring/loadgen.py`.
- **Blockers → solutions:**
  - *Allow monitoring data collection* tenant setting was off; scripted on.
  - KQL reserved word `time` → bracket notation.
  - Measure name clashed with a column (names are case-insensitive).
  - The Metrics app install has no API, so it's a checklist.
- **End state:**
  - Under SQL + DAX load, F2 hit interactive delay in under 2 minutes (peak 285 %).
  - Throttling acted as back-pressure, with no errors.
  - Resizing to F4 cleared it in about 1 minute and the overage in about 6.
  - Workspace monitoring attributed 17,902 CPU-s to 270 SQL queries.
- **Look at:**
  - **Portal:** `cap_rtd_capacity`, `cap_rpt_capacity`, monitoring KQL DBs (`WarehouseExecutions`, `SemanticModelLogs`), Admin portal → Capacity settings.
  - **Repo:** [monitoring/capacity-comparison.md](../monitoring/capacity-comparison.md).
- **Insights:**
  - Only the RTI path gives real-time alerts and query-level root cause.
  - Only the Power BI/API path gives money.
  - Per-item CU chargeback needs the Metrics app model (use FUAM).
  - Eventhouse uptime, not query count, drives its CU.

### Step 7: Observability · issue #12 · PR #28
- **Goal:** pipeline failure injection → detection → alert routing; Monitor hub; workspace monitoring.
- **Actions:**
  - `pl_finance_daily` with `failure_mode` (Fail activity, or a real Spark missing-file error through a notebook parameter cell).
  - Job events → `cap_monitoring.job_events`.
  - `act_job_failures` routes data-engineering failures and BI refresh failures to separate recipients.
- **Blockers → solutions:**
  - Notebooks log as `SynapseNotebook`, not `Notebook`, so the filter missed them. Fixed.
  - Neither job events nor `ItemJobEventLogs` carry error text; root cause comes from the job API and activity-run logs.
  - The prod deploy hit a transient runner network reset; a rerun fixed it.
- **End state:** detection latency is 3–4 s for job events, 49–78 s for workspace monitoring, and about 2–7 min to email. The prod pipeline runs.
- **Look at:**
  - **Portal:** Monitor hub (filter *Failed*), `pl_finance_daily` run → activity error → Spark logs.
  - **Repo:** [monitoring/observability.md](../monitoring/observability.md).
- **Insights:**
  - Use workspace monitoring for breadth, job events for speed, and the job API for the error text.
  - Build alert filters from **logged** values, not REST type names.

### Step 8: Governance · issue #13 · PRs #29–#30
- **Goal:** catalog, domains, endorsement, lineage/impact, OneLake security (RLS/CLS), shortcut security, roles.
- **Actions:**
  - OneLake role `FinanceViewerNoPII` (gold only, CLS, RLS on Coastal), script-managed in dev and prod.
  - CI workflow `governance-test` proves enforcement as the restricted SP.
  - Catalog search, description hygiene, Scanner API, and a generated [lineage graph](lineage.md) with impact analysis.
- **Blockers → solutions:**
  - **The SQL endpoint in delegated identity mode ignores OneLake security.** The first CI run exposed PII, silver tables and shortcut data.
  - Switching to user identity mode is portal-only, so `governance.py sqlsec` adds T-SQL DENYs (D-032).
  - `tsql()` never committed, so the DENYs were silently rolled back. Fixed.
  - Role names must be alphanumeric.
  - The `prod` environment only allows `main`, so the test runs from `main`.
  - Endorsement has no API.
- **End state:**
  - OneLake direct access is enforced (403 for out-of-role tables).
  - SQL denies PII and out-of-scope tables, including through the shortcut.
  - RLS in SQL waits on the portal mode switch.
  - Found a circular dependency `lh_asset` ⇄ `lh_finance`.
- **Look at:**
  - **Portal:** OneLake catalog (filter by domain); `lh_finance` → *Manage OneLake security*; SQL endpoint → Security (access mode); Lineage view → Impact analysis.
  - **GitHub:** Actions → `governance-test` job summary.
  - **Repo:** `infra/governance.py`, `infra/governance_test.py`.
- **Insights:**
  - Always test security **as the restricted identity**; an admin sees everything.
  - Workspace Contributors and above bypass OneLake security entirely.
  - Shortcut access is governed by the **source** item's rules.

### Step 9: Purview · issue #14 · PR #31
- **Goal:** sensitivity labels on PII/financing data, DLP, audit, Data Map; flag licensing.
- **Actions:**
  - `infra/purview.py`: label status via the Scanner API, bulk apply via `bulkSetLabels`, audit via activity events with hashed users.
  - DLP PowerShell template and licensing map.
- **Blockers → solutions:**
  - **The labels tenant setting is rejected until Purview labels are published.**
  - DLP needs an interactive compliance sign-in and E5-class licensing.
  - There are no external sources to scan (D-014).
- **End state:** audit works (agent queries, OneLake role changes, permission grants); 12 items are unlabeled pending Purview setup.
- **Look at:**
  - **Purview portal:** Information protection, DLP, Audit, Unified Catalog.
  - **Fabric:** Microsoft Purview hub.
  - **Repo:** [governance/purview/README.md](../governance/purview/README.md).
- **Insight:** Fabric governance is ready before Purview is. Labels and DLP are a tenant-licensing project, so plan them separately from the data build.

### Step 10: OneLake diagnostics (added) · issue #32
<!-- STEP10 -->

---

## Blockers at a glance

| # | Blocker | Kind | Resolution | Ref |
|---|---|---|---|---|
| 1 | OIDC `AADSTS700213` (ID-based subject) | Identity | Register both subjects | D-006 |
| 2 | Git connect 403 (GitHub sync off) | Tenant setting | Scripted settings by `settingName` | Step 0 |
| 3 | Event Hubs SAS forced off | Tenant governance | Extended source + workspace identity (preview) | D-009 |
| 4 | AMQP 5671 blocked | Network | AMQP over WebSockets | Step 1b |
| 5 | Azure SQL / storage public access forced Disabled | Tenant governance | Open mirroring + cross-domain shortcut (your decision) | D-014 |
| 6 | Starter Pool never starts on F2 | Capacity | Small-node custom pool | D-015 |
| 7 | MVs/functions invisible to data agents | Product limit | Agents on real tables + few-shots | D-020 |
| 8 | Image export disabled | Tenant setting | Render checks via PDF | D-021 |
| 9 | Certification, endorsement, Metrics app, surge protection, SQL access mode, graph materialization: no API | Product limit | Portal checklists | Steps 4–8 |
| 10 | Ontology graph *Ineligible* | Undocumented | `ONT_*` annotations (skills-for-fabric#107) | D-024 |
| 11 | fabric-cicd orphan cleanup deleted prod Activators | Our bug | `UNMANAGED_ITEMS` exclusion | D-024 |
| 12 | F2 Kusto 429 for >15 min | Capacity | Pause/resume; monitoring co-hosted | Step 5 |
| 13 | Monitoring data collection off | Tenant setting | Scripted on | Step 6 |
| 14 | Notebooks logged as `SynapseNotebook` | Undocumented | Route filter fixed | Step 7 |
| 15 | SQL endpoint delegated mode ignores OneLake security | Product behavior | T-SQL DENYs; portal switch to user identity | D-032 |
| 16 | `tsql()` didn't commit (DENYs rolled back) | Our bug | Commit after execute | Step 8 |
| 17 | Labels setting rejected until labels are published | Licensing/Purview | Checklist; `apply-labels` ready | D-033 |
| 18 | VPN/DNS blips, runner network reset | Environment | Retries; rerun failed job | D-022 |

## Open portal checklist

1. Certify `sm_finance`. Promote `lh_finance`, `lh_asset`, `gql_finance`, `ont_contoso`.
2. Publish `da_contoso_360` / `da_contoso_ontology` to Microsoft 365 Copilot and Teams.
3. Verify Activator emails: fleet faults/offline, capacity at 70 %, job failures.
4. Install the Capacity Metrics app and compare the load-test window; set surge protection (50 % / 40 %).
5. Switch the `lh_finance` and `lh_asset` SQL endpoints to **User identity** mode, then re-run `governance-test`.
6. Purview: publish labels → enable the label tenant settings → run `purview.py apply-labels`; run the DLP template.
7. Optional: materialize the ontology graph; try the ontology agent chat.

## Top insights

1. **Script everything that has an API, checklist the rest, and say which is which.** About 90 % of this lab is re-creatable from `infra/`. The remaining portal steps are where governance (certification, access mode, labels) lives, so they need owners.
2. **Small SKUs teach capacity behavior fast.** Always-on items (Eventhouse, eventstream processors, monitoring) set the floor; interactive SQL/DAX spikes cause throttling. Resizing or pausing is the fix, and it's scriptable.
3. **Test security as the restricted principal, in CI.** It caught a full PII exposure that admin testing would never show.
4. **Real tables beat clever objects for AI.** Agents and ontologies can't use MVs/functions; ontologies need one SQL endpoint, so design a semantic hub.
5. **Deployment tooling can delete things.** Orphan cleanup plus items you deliberately keep out of Git means you must maintain an explicit exclusion list.
6. **Monitoring layers answer different questions:** capacity events (how hot, now), workspace monitoring (which query or job), job API (why it failed), activity events / OneLake diagnostics (who touched what).

## Cost and teardown

- **Running:** F2 pay-as-you-go (pause when idle), Event Hubs Standard 1 TU (about $22/month), and the local simulator stream.
- **Pause:** `py infra/teardown.py` pauses the capacity. Data and items persist; nothing runs.
- **Remove the lab extras:** `py infra/teardown.py --lab --yes` removes the Step 1b items and Event Hubs.
- **Everything:** `py infra/teardown.py --all --yes`.
