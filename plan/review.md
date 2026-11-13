# Contoso Energy on Microsoft Fabric: end-to-end review

Sample, not production guidance ([DISCLAIMER](../DISCLAIMER.md)). Synthetic data. This review covers Steps 0–12. For each step it gives the task and actions, blockers and solutions, results, insights, what to check in the UI (Fabric, GitHub, Purview, Foundry portals) and in this repo, and the concepts to understand before demoing it. Detailed procedures are in the [runbook](runbook.md); every decision and its alternatives are in the [decision log](decision-log.md).

Contents: [How the environment was built](#how-the-environment-was-built) · [Tools](#tools) · [Steps 0–12](#step-by-step) · [All blockers](#blockers-at-a-glance) · [Open portal checklist](#open-portal-checklist) · [Top insights](#top-insights) · [Cost and teardown](#cost-and-teardown)

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
- **Task:** a secure, reproducible scaffold. That means identity, leak guards, capacity, tenant settings, a service principal with OIDC, domains, dev/prod workspaces, Entra groups and Git integration.
- **Actions:** the scripts in the table above, run in order (runbook Step 0).
- **Blockers → solutions:**
  - Prod deploy failed with `AADSTS700213`. New repos send an **ID-based** OIDC subject, so both subjects are now registered (D-006).
  - Git connect returned `403 FeatureNotAvailable`. The *GitHub sync* tenant setting was off, and the script matched settings by title instead of `settingName`. Both were fixed.
- **Results:** 6 workspaces in 3 domains on one F2. Dev is Git-synced to `dev`; prod is deploy-only. `main` accepts PRs only.
- **Check in UI:**
  - Admin portal → Domains; workspace settings → Git integration; Admin portal → Tenant settings.
  - GitHub: Settings → Branches / Environments / Secret scanning.
- **Check in repo:** [runbook Step 0](runbook.md#step-0-prereqs-and-platform--issue-1-prs-15-16), [checklists/step-0-portal.md](checklists/step-0-portal.md).
- **Insights:**
  - Script tenant settings by `settingName` and treat propagation (up to 15 min) as part of the run.
  - Register both OIDC subject formats.
  - Keep prod deploy-only from the start; it makes every later step reviewable.
- **Concepts to understand:** Capacity (F SKU, capacity units (CU), pay-as-you-go pause/resume) · scope of tenant vs capacity vs workspace settings · domains · workspace roles (Admin/Member/Contributor/Viewer) · Git integration (workspace ↔ branch + folder, commit/update direction) · fabric-cicd deploys and `parameter.yml` rebinding · OIDC federated credentials (subject claim) vs client secrets · service principal access to Fabric APIs.

### Step 1a: Mock data · issue #2
- **Task:** realistic synthetic telemetry, alerts, assets, customers, contracts and payments, with anomalies, late events and duplicates.
- **Actions:** stdlib Python package `data-gen/contoso_gen` (batch, stream, replay) with ground truth and 9 tests.
- **Blockers:** none.
- **Results:** reproducible from a seed. Anomalies and their ground truth are written to `data-gen/out/`.
- **Check in UI:** none (code-only step).
- **Check in repo:** [data-gen/README.md](../data-gen/README.md) (event contract), `data-gen/tests/`.
- **Insight:** ground truth for injected anomalies is what made recall measurable later (Step 1c).
- **Concepts to understand:** Event time vs ingestion time · late and duplicate events (at-least-once delivery) · seeded, reproducible generators · ground truth as a test oracle for detection.

### Step 1b: Ingestion comparison · issue #3
- **Task:** the same simulator through (i) Event Hubs → Eventstream vs (ii) Eventstream custom endpoint.
- **Actions:** `infra/eventhubs.py`, `infra/rti_lab.py`, and a tee benchmark (`data-gen/benchmarks/ingest_compare.py`).
- **Blockers → solutions:**
  - Tenant governance forces Event Hubs **SAS (local auth) off**. The classic source needs SAS, so I used the *AzureEventHubExtended* source with **workspace identity** (preview).
  - The network blocks AMQP port 5671, so producers use AMQP over WebSockets (443).
- **Results:**
  - Median latency is a tie (~2 s).
  - Event Hubs at 1 TU throttles above ~1,000 events/s.
  - A processor eventstream costs ≈ 1 CU continuously (~50 % of an F2).
  - Recommendation: **custom endpoint → Eventhouse direct ingestion** (D-009).
  - The lab streams are paused.
- **Check in UI:** ce-fleet-dev → `lab_es_*` eventstreams (topology).
- **Check in repo:** [ingestion-comparison.md](ingestion-comparison.md).
- **Insights:**
  - Use Event Hubs only when you need its ecosystem (Kafka clients, capture, geo-DR) or very high predictable scale.
  - On small SKUs, the processor is the real cost; direct ingestion avoids it.
- **Concepts to understand:** Eventstream source / processor / destination · custom endpoint (Event Hubs-compatible) · direct ingestion vs processed ingestion into Eventhouse · Event Hubs throughput units, partitions, consumer groups · workspace identity · AMQP (5671) vs AMQP over WebSockets (443).

### Step 1c: Eventhouse medallion · issue #4
- **Task:** bronze/silver/gold with update policies, materialized views (MVs), Fleet API functions and retention/caching.
- **Actions:**
  - KQL source of truth in `infra/kql/eh_fleet/*.kql`, applied as the KQL DB **definition** (`fleet_medallion.py apply`).
  - `es_fleet` with a custom endpoint feeding direct ingestion.
- **Blockers → solutions:**
  - `DatabaseSchema.kql` accepts only an allow-list of commands, and no MV backfill. Documented a drop/recreate procedure.
  - `avgif` returns NaN for empty groups, so gold stores sums and counts.
  - Orphan pretty-named databases break name-based calls, so everything addresses databases by GUID.
- **Results:**
  - 183k rows; 0.47 % duplicates removed; 1.07 % late events flagged.
  - Recall: overtemp 3/3, inverter faults 4/4, stuck SOC 1/3. The 2 misses are undetectable from the data.
- **Check in UI:** `eh_fleet` → database → Tables / Materialized views / Functions; *Explore your data* with `FleetKpis()`.
- **Check in repo:** [infra/kql/eh_fleet/](../infra/kql/eh_fleet/), `workspaces/ce-fleet/eh_fleet.*`.
- **Insights:**
  - Keep readable KQL in the repo and let Fabric own the serialized definition.
  - Measure detection against ground truth; some "misses" are physics, not bugs.
- **Concepts to understand:** KQL database tables · update policies (bronze → silver on ingest) · materialized views (incremental aggregation) · stored functions as an API · retention vs caching (hot cache) policies · ingestion-time dedup · OneLake availability.

### Step 1d: Finance lakehouse + external source · issue #5
- **Task:** a finance medallion plus an external source (ADLS shortcut and mirrored Azure SQL were requested).
- **Actions:**
  - **Open mirroring** `mir_loan_servicing` (a simulated OLTP writes parquet change files).
  - `lh_asset` (Asset domain), a cross-domain shortcut, and the PySpark notebook `nb_finance_medallion`.
- **Blockers → solutions:**
  - **New Azure SQL servers and storage accounts are forced to public network access Disabled** by tenant governance (not Azure Policy, so no exemption). You chose Fabric-native substitutes (D-014).
  - On F2 the Starter Pool never starts (`TooManyRequestsForCapacity`), so a Small-node pool is used (D-015).
  - Load-to-Table doesn't support schemas, so table names use prefixes (D-016).
  - SQL endpoint metadata lag is handled by forcing `refreshMetadata`.
- **Results:**
  - CDC visible in the SQL endpoint in **38 s**, including a delete for a right-to-erasure request.
  - 11/11 data-quality checks pass.
  - Collection rate 95.86 %. Prod matches dev.
- **Check in UI:** ce-finance-dev → `mir_loan_servicing` (replication status), `lh_finance` (shortcut icons on `bronze_*`, `ref_sites`), notebook run history.
- **Check in repo:** [infra/notebooks/nb_finance_medallion.py](../infra/notebooks/nb_finance_medallion.py), [workspaces/ce-finance/parameter.yml](../workspaces/ce-finance/parameter.yml) (cross-workspace rebinding).
- **Insights:**
  - Open mirroring gives the same CDC demo with no network dependency.
  - Private-networking tenants need a VNet data gateway / private endpoints module before real mirroring and ADLS shortcuts work.
- **Concepts to understand:** Lakehouse Tables vs Files (Delta) · medallion layers · shortcuts (OneLake, ADLS, S3) and whose identity they use · mirroring (open vs managed) · SQL analytics endpoint and its metadata sync · Spark Starter pool vs custom pools.

### Step 1e: API layer · issue #6
- **Task:** an API over gold data.
- **Actions:** **API for GraphQL** `gql_finance` (query-only, PII fields unmapped) plus a Fleet API client over KQL functions.
- **Blockers:** none. User Data Functions were deferred because there was no write use case (D-017).
- **Results:** nested queries work; requesting `email` fails schema validation.
- **Check in UI:** `gql_finance` → schema explorer (no `email`).
- **Check in repo:** `infra/api_layer.py`.
- **Insight:** field mapping is a cheap first privacy layer, but it isn't access control. Steps 8–9 add that.
- **Concepts to understand:** API for GraphQL (data sources, generated schema, field mapping) · User Data Functions (write/logic APIs) · KQL functions as a read API.

### Step 2: Real-Time Dashboard + Activator · issue #7 · PRs #22–#23
- **Task:** an operations dashboard and alerts for device fault, offline device and pipeline failure.
- **Actions:**
  - `rtd_fleet_ops`: 11 tiles, each query smoke-tested before deploy.
  - `act_fleet_device_faults` and `act_fleet_offline_sites` (KQL source → EventTrigger → email). Shapes come from Skills for Fabric.
- **Blockers → solutions:**
  - Activator definitions hold recipient UPNs, so they're kept out of Git and script-managed (D-018).
  - "Offline" is modeled as a one-time transition event so it doesn't re-fire.
  - The pipeline-failure alert moved to Step 7.
  - PowerShell 5 corrupted the runbook encoding; a mojibake hook now blocks this.
- **Results:** both rules are running in dev and prod. They were re-created after a deploy bug deleted them (see Step 5).
- **Check in UI:** `rtd_fleet_ops`; Activator items → *History*.
- **Check in repo:** `infra/rti_alerts.py`.
- **Insight:** poll intervals (5 min) are the Activator cost/latency dial. Alert on **transitions**, not states.
- **Concepts to understand:** Real-Time Dashboard (tiles, parameters, auto-refresh) · Activator objects, properties, rules · events vs state (transitions) · actions (email, Teams, Fabric item, Power Automate) · poll interval as the cost/latency dial.

### Step 3: Data agents · issue #8 · PR #24
- **Task:** Eventhouse, lakehouse and combined agents, published to Teams/M365 Copilot.
- **Actions:**
  - Agents: `da_fleet_ops`, `da_finance` (PII columns deselected), `da_contoso_360`.
  - Tested via the Assistants-compatible endpoint.
- **Blockers → solutions:** the agent's NL-to-KQL **can't see materialized views or functions**, so the fleet agent uses silver tables, with few-shots encoding the gold logic (D-020).
- **Results:**
  - Answers match SQL/KQL ground truth.
  - The PII request was refused.
  - Publishing to Teams/M365 Copilot is a portal step (pending).
- **Check in UI:** each agent → *Test* pane and run steps.
- **Check in repo:** `infra/data_agents.py` (`test`, `compare`).
- **Insight:** design gold as **real tables** if agents must use it. Instructions and few-shots carry the business rules.
- **Concepts to understand:** Data agent sources and selected tables · agent instructions vs data-source instructions · example queries (few-shots) · NL-to-SQL / NL-to-KQL / NL-to-DAX · draft vs published agent · Assistants-compatible endpoint (threads, runs, steps).

### Step 4: Semantic model + Power BI · issue #9 · PR #25
- **Task:** KPIs in a Direct Lake model; certify one gold KPI.
- **Actions:**
  - `sm_finance`: TMDL generated from the live schema, with 13 measures.
  - `rpt_finance_kpis` (PBIR), rendered via Export-to-PDF.
- **Blockers → solutions:**
  - Image export is off in the tenant, so rendering is checked via PDF.
  - Rendering exposed real bugs: the date-dimension start, blank trend months (`+0`), and the table visual, now a matrix.
  - Certification has **no API**, so it's a portal checklist.
- **Results:** DAX = SQL exactly (collection 95.86 %, delinquency 6.02 %), in dev and prod.
- **Check in UI:** `sm_finance` → Settings → Endorsement; `rpt_finance_kpis`.
- **Check in repo:** `infra/semantic_model.py verify`.
- **Insight:** a scripted DAX-vs-SQL parity check plus a render check catches errors no schema validation will.
- **Concepts to understand:** Direct Lake (framing, fallback to DirectQuery) · TMDL and PBIR as code · measures vs columns · endorsement (Promoted, Certified) and who may certify.

### Step 5: Fabric IQ ontology · issue #10 · PR #26
- **Task:** an ontology with Site, Device, Battery, Alert, Contract and Customer, plus an ontology agent, contrasted with the data agent.
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
- **Results:** same answers as `da_contoso_360` in about half the time, with no join instructions. The graph is not materialized (optional portal step).
- **Check in UI:** `ont_contoso` (entity diagram, *Ontology agent* chat, *Manage graph*), `da_contoso_ontology`.
- **Check in repo:** [ontology-vs-data-agent.md](ontology-vs-data-agent.md), `infra/ontology.py`.
- **Insights:**
  - The ontology moves business meaning out of prompts into a governed, reusable artifact.
  - The trade-off is freshness: Eventhouse data arrives through OneLake availability, minutes behind.
- **Concepts to understand:** Ontology entity types, keys, properties, relationships · bindings to one SQL endpoint · time-series properties · rules · graph materialization · ontology agent vs data agent (context vs execution).

### Step 6: Capacity monitoring · issue #11 · PR #27
- **Task:** compare the Metrics app vs RTI vs Power BI/APIs, check FUAM, and reproduce throttling on a small SKU.
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
- **Results:**
  - Under SQL + DAX load, F2 hit interactive delay in under 2 minutes (peak 285 %).
  - Throttling acted as back-pressure, with no errors.
  - Resizing to F4 cleared it in about 1 minute and the overage in about 6.
  - Workspace monitoring attributed 17,902 CPU-s to 270 SQL queries.
- **Check in UI:** `cap_rtd_capacity`, `cap_rpt_capacity`, monitoring KQL DBs (`WarehouseExecutions`, `SemanticModelLogs`), Admin portal → Capacity settings.
- **Check in repo:** [monitoring/capacity-comparison.md](../monitoring/capacity-comparison.md).
- **Insights:**
  - Only the RTI path gives real-time alerts and query-level root cause.
  - Only the Power BI/API path gives money.
  - Per-item CU chargeback needs the Metrics app model (use FUAM).
  - Eventhouse uptime, not query count, drives its CU.
- **Concepts to understand:** CU smoothing (interactive ~5 min, background 24 h) · throttling stages (interactive delay → interactive rejection → background rejection) · overage / carry-forward · surge protection · capacity overview events · workspace monitoring · Capacity Metrics app semantic model · FUAM.

### Step 7: Observability · issue #12 · PR #28
- **Task:** pipeline failure injection → detection → alert routing; Monitor hub; workspace monitoring.
- **Actions:**
  - `pl_finance_daily` with `failure_mode` (Fail activity, or a real Spark missing-file error through a notebook parameter cell).
  - Job events → `cap_monitoring.job_events`.
  - `act_job_failures` routes data-engineering failures and BI refresh failures to separate recipients.
- **Blockers → solutions:**
  - Notebooks log as `SynapseNotebook`, not `Notebook`, so the filter missed them. Fixed.
  - Neither job events nor `ItemJobEventLogs` carry error text; root cause comes from the job API and activity-run logs.
  - The prod deploy hit a transient runner network reset; a rerun fixed it.
- **Results:** detection latency is 3–4 s for job events, 49–78 s for workspace monitoring, and about 2–7 min to email. The prod pipeline runs.
- **Check in UI:** Monitor hub (filter *Failed*), `pl_finance_daily` run → activity error → Spark logs.
- **Check in repo:** [monitoring/observability.md](../monitoring/observability.md).
- **Insights:**
  - Use workspace monitoring for breadth, job events for speed, and the job API for the error text.
  - Build alert filters from **logged** values, not REST type names.
- **Concepts to understand:** Job events in the Real-Time hub · Monitor hub · workspace monitoring tables (`ItemJobEventLogs`, `WarehouseExecutions`) · pipeline activity runs and Spark logs · alert routing by item type.

### Step 8: Governance · issue #13 · PRs #29–#30
- **Task:** catalog, domains, endorsement, lineage/impact, OneLake security (RLS/CLS), shortcut security, roles.
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
- **Results:**
  - OneLake direct access is enforced (403 for out-of-role tables).
  - SQL denies PII and out-of-scope tables, including through the shortcut.
  - RLS in SQL waits on the portal mode switch.
  - Found a circular dependency `lh_asset` ⇄ `lh_finance`.
- **Check in UI:**
  - OneLake catalog (filter by domain); `lh_finance` → *Manage OneLake security*; SQL endpoint → Security (access mode); Lineage view → Impact analysis.
  - GitHub: Actions → `governance-test` job summary.
- **Check in repo:** `infra/governance.py`, `infra/governance_test.py`.
- **Insights:**
  - Always test security **as the restricted identity**; an admin sees everything.
  - Workspace Contributors and above bypass OneLake security entirely.
  - Shortcut access is governed by the **source** item's rules.
- **Concepts to understand:** OneLake catalog · endorsement · lineage and impact analysis · OneLake security roles (RLS/CLS) · **SQL endpoint delegated vs user identity mode** · shortcut security (source rules apply) · workspace roles vs item permissions (Contributor+ bypass roles).

### Step 9: Purview · issue #14 · PR #31
- **Task:** sensitivity labels on PII/financing data, DLP, audit, Data Map; flag licensing.
- **Actions:**
  - `infra/purview.py`: label status via the Scanner API, bulk apply via `bulkSetLabels`, audit via activity events with hashed users.
  - DLP PowerShell template and licensing map.
- **Blockers → solutions:**
  - **The labels tenant setting is rejected until Purview labels are published.**
  - DLP needs an interactive compliance sign-in and E5-class licensing.
  - There are no external sources to scan (D-014).
- **Results:** audit works (agent queries, OneLake role changes, permission grants); 12 items are unlabeled pending Purview setup.
- **Check in UI:**
  - Purview portal: Information protection, DLP, Audit, Unified Catalog.
  - Fabric: Microsoft Purview hub.
- **Check in repo:** [governance/purview/README.md](../governance/purview/README.md).
- **Insight:** Fabric governance is ready before Purview is. Labels and DLP are a tenant-licensing project, so plan them separately from the data build.
- **Concepts to understand:** Sensitivity labels and downstream inheritance · DLP policies for Fabric · Purview Audit (= Fabric activity events) · Data Map scanning · licensing (Purview/E5-class) vs Fabric capacity.

### Step 10: OneLake diagnostics + access observability (added) · issue #32
- **Task:** see who reaches the data (users, service principals, workloads, users behind agents), allowed or denied, and compare the logs with Purview.
- **Actions:**
  - A **separate F64 capacity** and an isolated lab (`infra/obs_lab.py`, D-041): a data workspace with a OneLake role, a consumer workspace with cross-workspace shortcuts, an admin-only audit workspace for the logs, and workspace monitoring.
  - A test Viewer user.
  - Serialized, ID-tagged probes run as admin, the test user (role member) and the deploy SP (Viewer, **not** a role member) in CI.
  - Correlation across OneLake diagnostics, workspace monitoring, activity events (= Purview Audit) and SQL audit.
- **Blockers → solutions:**
  - **OneLake diagnostics delivered zero events.** That was about 15 h on dev and about 2 h on the fresh capacity, though settings read *Enabled* and folders are created. No fix found; a portal toggle, then a support ticket, are next.
  - Group access for the new user took hours to resolve, so the script assigns the user directly.
  - `az ad` / Graph hit a CAE re-auth challenge, so groups are resolved through Fabric.
  - The agent endpoint reuses its thread, so only the newest answer is read.
  - **A shared-folder mistake:** a branch switch briefly removed the other session's files. They were restored within seconds; branch work now uses a separate worktree.
- **Results:**
  - Enforcement and log coverage are measured per identity and channel.
  - **The new lakehouse's SQL endpoint is delegated too.** A Viewer **with no OneLake role** reads PII, out-of-role tables and all regions via SQL and data agents, while OneLake returns 403 (D-042).
  - Purview Audit shows the human behind an agent but not storage reads, denials or SP activity. Workspace monitoring shows SQL callers including the SP (shortcut endpoint only).
- **Check in UI:** `ce-obs-data` → `lh_obs_finance` → Manage OneLake security; SQL endpoint → Security (access mode); workspace settings → OneLake → diagnostics; `ce-obs-audit` monitoring KQL databases.
- **Check in repo:** [governance/access-observability.md](../governance/access-observability.md), [access-observability-results-obs.md](access-observability-results-obs.md).
- **Insights:**
  - OneLake security protects OneLake, not SQL in delegated mode.
  - No single log answers "who touched this data".
  - Agents hide the human at the data layer, but Purview Audit records them at the question layer.
- **Concepts to understand:** OneLake diagnostics (storage-level access events) · activity events vs workspace monitoring vs SQL audit (which layer each sees) · identities: user, service principal, workspace identity, user behind an agent · delegated identity (the item owner's access, not the caller's).

### Step 11: Data agent evaluation (added, parallel workstream) · on `dev`, PR pending
Built and run in a separate session; the files below are on `dev` until its PR merges.
- **Task:** measure data agent accuracy against live truth, compare graders, isolate instructions vs ontology, and test whether capacity, storage format or an optimizer loop changes results.
- **Actions:**
  - Golden set (21 → 48 questions, weighted by impact) with truth recomputed live from SQL/KQL; graders: rules (reference), Microsoft-Decision-1, Foundry Similarity, Fabric Data Agent SDK (`eval/agent_eval.py`).
  - live3: same-source arms `lab_da_hub_rich` vs `lab_da_hub_bare` vs the ontology agent; capacity sweep F32/F64/F128.
  - live4: one frozen snapshot in lakehouse, SQL database, mirrored private Azure SQL and Eventhouse, on its own F128.
  - live2.5: gated feedback loop (Eventhouse log, gpt-4.1 optimizer, guardrails, train/validation/test).
  - CI gate `.github/workflows/agent-eval.yml` as the OIDC SP.
- **Blockers → solutions:**
  - Service principals can't call agents with a KQL source, so CI gates `da_contoso_ontology` only (D-036).
  - F2 returned 429 on every KQL query under shared load, so evaluations run on F32+ (D-038).
  - The private Azure SQL needed Entra-only auth, a private endpoint DNS zone group, a managed private endpoint and a VNet data gateway with OAuth2 (D-040).
  - One hung call stalled a run for 80 min (OpenAI client default retries); calls now time out at 60 s.
  - The Fabric SDK failed on one agent ("not found in sandbox stage"); the notebook falls back and writes its traceback to OneLake.
- **Results:**
  - live3 (rules): `lab_da_hub_rich` 94 %, `da_contoso_ontology` 83 %, `da_contoso_360` 83 %, `lab_da_hub_bare` 75 %. No PII disclosed.
  - Decision-1 agreed best with the rules (93 %, kappa 0.78) and was the cheapest.
  - Latency is AI orchestration: median ~13 s at every SKU, and 10.5–10.8 s on every storage format.
  - live2.5: ontology + all sources + 4 instruction lines reached 96 %; the gate correctly rejected every optimizer proposal.
- **Check in UI:** `ce-asset-dev` → `lab_da_*` agents (*Test* pane, run steps); `eh_fleet` → `lab_agent_eval` database and `lab_rtd_agent_eval` dashboard; Foundry portal → project `agent-eval` → Evaluations; `ce-live4-dev` (capacity paused).
- **Check in repo:** `eval/README.md`, `eval/reports/` (live2, live3, capacity-latency, live4, live2.5), `eval/golden/contoso.jsonl`, `.github/workflows/agent-eval.yml`, decision log D-035..D-040.
- **Insights:**
  - Grade with deterministic rules plus the logged SQL/KQL; LLM judges are lenient on wrong numbers phrased like the expected answer.
  - The ontology carries structure, not business definitions; definitions must be written down (instructions) or agents and optimizers invent them.
  - Neither a bigger SKU nor a different store makes agents faster; fewer orchestration steps do.
- **Concepts to understand:** Golden set with live-computed truth · deterministic rule grading vs LLM judges (agreement, Cohen's kappa) · impact-weighted accuracy · train / validation / test splits · paired head-vs-candidate comparison · guardrails against overfitting and leakage.

### Step 12: Operations agent vs Activator (added) · issue #36
- **Task:** compare the operations agent (preview) with Activator on identical events. Phase 1: alerts. Phase 2: remediation with vs without approval. Phase 3: ontology as a second source.
- **Actions:**
  - In the isolated lab: its own Eventhouse, simulator stream and Activator clones.
  - A generator option for timed injections (`--inject TYPE:SITE@+MIN`).
  - A **probe Activator** whose action runs a pipeline, so every firing is durable evidence.
  - Failure-injection and remediation pipelines with job events.
  - Agents configured by `infra/operations_agent.py`.
- **Blockers → solutions:**
  - The playbook can only be generated in the portal: authored playbooks are rejected, and the agent can't start without one.
  - `FabricJobAction` is rejected by the API.
  - **Portal generation failed** (internal infrastructure error), and the Teams license is expiring.
  - The *Copilot in Fabric* tenant switch was off and is now on; retry pending. Agent phases are documented as blocked.
- **Results:**
  - Activator: **6/6 detected, 0 spurious**, alert to action in 2–7 min.
  - A flapping alert produced 4 firings.
  - Auto-remediation took **5 m 34 s** with no human, logged as `Manual`.
  - The agent is configured but not running.
- **Check in UI:** `ce-obs-ops` → `opa_obs_fleet` / `opa_obs_jobs`, `act_obs_*`.
- **Check in repo:** [monitoring/operations-agent.md](../monitoring/operations-agent.md).
- **Insights:**
  - Activator is scriptable and dependable for deterministic rules and can act alone, but it's noisy on flapping signals.
  - The agent's value (grouping, context, approval) is untested here: its authoring and approval loop depend on portal Copilot and Teams.
- **Concepts to understand:** Operations agent (goals, knowledge sources, playbook, generated rules, actions, approval in Teams) · Activator Fabric item action vs Power Automate action · flapping and hysteresis · human-in-the-loop vs autonomous remediation · invoke type in job history.

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
| 19 | OneLake diagnostics: no events (~15 h dev, ~2 h fresh capacity) | Platform / unknown | Config verified; portal toggle then support ticket | Step 10 |
| 20 | SQL endpoints in delegated mode even when new; Viewers read everything via SQL/agents | Product behavior | DENYs or portal switch to user identity | D-042 |
| 21 | Operations agent playbook: API rejects authored playbooks; portal generation fails | Product (preview) / service | Configure by script, generate in portal; retry after Copilot switch | D-043 |
| 22 | `FabricJobAction` rejected by API (UnknownError) | Product (preview) | Add the action in the portal | Step 12 |
| 23 | Branch switch in a shared working folder removed another session's files | Our mistake | Restored from stash in seconds; use `git worktree` | Step 10 |
| 24 | Graph/`az ad` CAE re-auth challenge | Identity | Resolve groups via Fabric role assignments | Step 10 |
| 25 | Service principals can't call data agents with a KQL source | Product (preview) | CI evaluates the ontology agent only | D-036 |
| 26 | F2: every KQL query 429 while streaming + agent eval share CU | Capacity | Evaluate on F32+; scale back after | D-038 |
| 27 | Private Azure SQL mirror: Entra-only policy, DNS for private endpoint, gateway auth | Tenant governance / network | DNS zone group, managed private endpoint, VNet gateway + OAuth2 | D-040 |
| 28 | Hung agent call stalled an eval run 80 min (client default retries) | Our bug | 60 s timeout, 420 s cap per question | Step 11 |

## Open portal checklist

1. Certify `sm_finance`. Promote `lh_finance`, `lh_asset`, `gql_finance`, `ont_contoso`.
2. Publish `da_contoso_360` / `da_contoso_ontology` to Microsoft 365 Copilot and Teams.
3. Verify Activator emails: fleet faults/offline, capacity at 70 %, job failures.
4. Install the Capacity Metrics app and compare the load-test window; set surge protection (50 % / 40 %).
5. Switch the `lh_finance` and `lh_asset` SQL endpoints to **User identity** mode, then re-run `governance-test`.
6. Purview: publish labels → enable the label tenant settings → run `purview.py apply-labels`; run the DLP template.
7. Optional: materialize the ontology graph; try the ontology agent chat.
8. OneLake diagnostics: toggle it in the portal once (workspace settings → OneLake); if still no files after an hour, open a support ticket (IDs and timestamps in `infra/.state/obs.json`).
9. Switch the obs and dev SQL endpoints to **User identity** mode; re-run `access_matrix.py run --profile obs` for all three identities.
10. Operations agents (`ce-obs-ops`): Generate playbook, Save, Start (add the Fabric item action to `opa_obs_jobs`); then rerun the Step 12 trial.
11. Pause the obs capacity when idle: `py infra/capacity.py pause --target obs`.

## Top insights

1. **Script everything that has an API, checklist the rest, and say which is which.** About 90 % of this lab is re-creatable from `infra/`. The remaining portal steps are where governance (certification, access mode, labels) lives, so they need owners.
2. **Small SKUs teach capacity behavior fast.** Always-on items (Eventhouse, eventstream processors, monitoring) set the floor; interactive SQL/DAX spikes cause throttling. Resizing or pausing is the fix, and it's scriptable.
3. **Test security as the restricted principal, in CI.** It caught a full PII exposure that admin testing would never show.
4. **Real tables beat clever objects for AI.** Agents and ontologies can't use MVs/functions; ontologies need one SQL endpoint, so design a semantic hub.
5. **Deployment tooling can delete things.** Orphan cleanup plus items you deliberately keep out of Git means you must maintain an explicit exclusion list.
6. **Monitoring layers answer different questions:** capacity events (how hot, now), workspace monitoring (which query or job), job API (why it failed), activity events (which Fabric action), OneLake diagnostics (which data, by whom, through which API).

## Cost and teardown

- **Running:** F2 pay-as-you-go (pause when idle), Event Hubs Standard 1 TU (about $22/month), and the local simulator stream.
- **Pause:** `py infra/teardown.py` pauses the capacity. Data and items persist; nothing runs.
- **Remove the lab extras:** `py infra/teardown.py --lab --yes` removes the Step 1b items and Event Hubs.
- **Everything:** `py infra/teardown.py --all --yes`.
