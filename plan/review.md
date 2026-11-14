# Contoso Energy on Microsoft Fabric: end-to-end review

Sample, not production guidance ([DISCLAIMER](../DISCLAIMER.md)). Synthetic data. This review covers Steps 0–13. For each step it gives the task and actions, blockers and solutions, results, insights, and two review tracks: **Click and see** (portal actions: where to go, what to look at, and the concept it shows) and **Read and done** (repo documents: what they answer and the concept behind it). Detailed procedures are in the [runbook](runbook.md); every decision and its alternatives are in the [decision log](decision-log.md). A formatted version of this page is [review.html](review.html): download or clone and open it in a browser. Regenerate it with `py tools/review_html.py` after editing this file.

Contents: [How the environment was built](#how-the-environment-was-built) · [Tools](#tools) · [Steps 0–13](#step-by-step) · [All blockers](#blockers-at-a-glance) · [Open portal checklist](#open-portal-checklist) · [Top insights](#top-insights) · [Cost and teardown](#cost-and-teardown). The HTML version also gathers every *Click and see* item into one portal walkthrough (with progress) and every *Read and done* item into one reading list.

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
- **Click and see:**
  - **Admin portal → Domains.** See: Fleet, Asset and Finance, each with its two workspaces assigned. Why: *Domains* group workspaces by business area for the OneLake catalog filter and domain admins. They organize; they don't grant access.
  - **Admin portal → Tenant settings (search "service principals", "GitHub").** See: Which switches are on and which security group each is scoped to. Why: *Tenant settings* are the outermost gate: if one is off, the API returns 403 `FeatureNotAvailable` whatever your workspace role (the Git-connect blocker).
  - **ce-finance-dev → Workspace settings → Git integration, then *Source control*.** See: Repo, branch `dev`, folder `workspaces/ce-finance`; items marked changed vs committed. Why: *Git integration* syncs item **definitions**, never data. Dev is authored and committed here; prod is never Git-connected.
  - **ce-finance-prod → item list.** See: The same items as dev, no Git panel, last modified by the deploy service principal. Why: *Deploy-only prod*: fabric-cicd publishes from `main`, and `parameter.yml` swaps dev IDs (lakehouses, connections) for prod IDs.
  - **GitHub → Settings → Branches, Environments, Code security.** See: `main` requires a pull request; environment `prod` accepts only `main`; secret scanning and push protection on. Why: *Guardrails before content*: a public repo can't receive a secret or an unreviewed change.
  - **Azure portal → Entra ID → App registrations → CI/CD app → Certificates & secrets → Federated credentials.** See: Two credentials per environment (name- and ID-based subjects), no client secret. Why: *OIDC federation*: GitHub presents a short-lived token whose subject must match exactly; new repos send an ID-based subject (`AADSTS700213`).
- **Read and done:**
  - **[runbook.md](runbook.md), Step 0.** See: The task / tool / Git-tracked / capture table. Why: *Scripted vs checklist*: the pattern every later step follows.
  - **[checklists/step-0-portal.md](checklists/step-0-portal.md).** See: The steps that have no API. Why: What a rebuild can't automate, and who must click it.
  - **[decision-log.md](decision-log.md), D-004 and D-006.** See: Why the Fabric CLI + REST, and why two OIDC subjects. Why: *Tooling choice* and *OIDC subject claims*.
- **Insights:**
  - Script tenant settings by `settingName` and treat propagation (up to 15 min) as part of the run.
  - Register both OIDC subject formats.
  - Keep prod deploy-only from the start; it makes every later step reviewable.

### Step 1a: Mock data · issue #2
- **Task:** realistic synthetic telemetry, alerts, assets, customers, contracts and payments, with anomalies, late events and duplicates.
- **Actions:** stdlib Python package `data-gen/contoso_gen` (batch, stream, replay) with ground truth and 9 tests.
- **Blockers:** none.
- **Results:** reproducible from a seed. Anomalies and their ground truth are written to `data-gen/out/`.
- **Before you click:** Nothing to click: the generator runs locally.
- **Click and see:** nothing; this step is local code.
- **Read and done:**
  - **[data-gen/README.md](../data-gen/README.md).** See: The event contract: fields, `event_time` vs ingestion time, duplicate and late rates, anomaly types. Why: *Event time vs ingestion time* and *at-least-once delivery*: the dedup and late-event logic in Step 1c depends on this contract.
  - **[data-gen/tests/](../data-gen/tests/).** See: Tests that the seed reproduces the same data and that every injected anomaly is in the ground truth. Why: *Ground truth as a test oracle*: detection recall becomes measurable instead of anecdotal.
- **Insight:** ground truth for injected anomalies is what made recall measurable later (Step 1c).

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
- **Click and see:**
  - **ce-fleet-dev → `lab_es_eventhubs` and `lab_es_custom` (open each canvas).** See: Source → processor → destination on one, source → direct ingestion on the other; the Event Hubs source authenticates with workspace identity. Why: *Eventstream processor vs direct ingestion*: a processor node runs continuously (~1 CU, half an F2); direct ingestion has no processor.
  - **ce-fleet-dev → `es_fleet` → custom endpoint source → *Details*.** See: An Event Hubs-compatible endpoint and a *Direct ingestion* destination into `eh_fleet`. Why: *Custom endpoint*: any Event Hubs SDK producer can send without an Event Hubs namespace. This is the recommended path (D-009).
  - **Azure portal → Event Hubs namespace → Overview metrics, Scale.** See: 1 throughput unit, throttled requests during the benchmark, local (SAS) auth disabled. Why: *Throughput units* cap ingress at 1 MB/s or 1,000 events/s each; that ceiling, not Fabric, limited path (i).
- **Read and done:**
  - **[ingestion-comparison.md](ingestion-comparison.md).** See: The 8-criteria table (latency, throughput, transforms, schema, replay, networking, ops, cost) and the recommendation. Why: The decision itself; no need to rerun the benchmark.
  - **[ingest_compare.py](../data-gen/benchmarks/ingest_compare.py).** See: How one generator tees identical events to both paths. Why: *Fair comparison*: same events, same time, so latency differences belong to the path.
- **Insights:**
  - Use Event Hubs only when you need its ecosystem (Kafka clients, capture, geo-DR) or very high predictable scale.
  - On small SKUs, the processor is the real cost; direct ingestion avoids it.

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
- **Click and see:**
  - **ce-fleet-dev → `eh_fleet` → KQL database → Tables → a `bronze_*` table → *Update policy*.** See: The policy that writes the silver table on every ingestion. Why: *Update policies* transform at ingestion time, so silver exists the moment bronze does; no scheduled job.
  - **Same database → Materialized views → `gold_device_latest`, `gold_site_15m`.** See: The definitions (`arg_max` per device, sums and counts per 15 min) and their health. Why: *Materialized views* aggregate incrementally; a query returns the materialized part plus the not-yet-processed delta, so it's always fresh.
  - **Same database → *Explore your data*: run `FleetKpis()` and `FleetOpenAlerts()`.** See: One KPI row and the open alerts. Why: *Stored functions as an API*: consumers call a stable contract and never touch table names.
  - **Database → *Data policies*.** See: Retention (silver 90 d, gold 365 d) and hot cache (31 d). Why: *Retention vs caching*: retention deletes data; caching only decides what sits on fast local SSD, which drives query speed and cost.
- **Read and done:**
  - **[infra/kql/eh_fleet/](../infra/kql/eh_fleet/).** See: Numbered scripts: bronze, silver (update policies, dedup), gold (MVs), functions, policies. Why: *Readable source of truth*: Fabric owns the serialized definition; the repo keeps what humans review.
  - **[runbook.md](runbook.md), Step 1c results.** See: Duplicates removed (0.47 %), late events flagged (1.07 %), recall per anomaly type. Why: *Recall against ground truth*: two "misses" are physically undetectable, not bugs.
- **Insights:**
  - Keep readable KQL in the repo and let Fabric own the serialized definition.
  - Measure detection against ground truth; some "misses" are physics, not bugs.

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
- **Click and see:**
  - **ce-finance-dev → `mir_loan_servicing` → *Monitor replication*.** See: Replicated tables, row counts, last sync time. Why: *Open mirroring*: the writer drops parquet change files in a landing zone and Fabric merges them into Delta, deletes included (CDC).
  - **ce-finance-dev → `lh_finance` → Tables.** See: Shortcut icons on `bronze_*` and `ref_sites`; `bronze_` / `silver_` / `gold_` prefixes. Why: *Shortcuts* reference data in another item without copying it; access is checked against the **source**.
  - **`lh_finance` → SQL analytics endpoint → new query `SELECT TOP 10 * FROM gold_fact_payments`.** See: The same Delta tables answered by T-SQL. Why: *SQL analytics endpoint*: an auto-generated read-only SQL view over Delta; its metadata sync can lag, which the scripts force.
  - **`nb_finance_medallion` → *Recent runs* → a run.** See: The custom Small-node pool, duration, the data-quality output. Why: *Spark pools*: the Starter Pool never started on F2, so a custom pool is used (D-015).
- **Read and done:**
  - **[nb_finance_medallion.py](../infra/notebooks/nb_finance_medallion.py).** See: Bronze → silver → gold transforms and the 11 data-quality checks. Why: *Medallion layers* on a lakehouse.
  - **[parameter.yml](../workspaces/ce-finance/parameter.yml).** See: How shortcut and connection IDs are rewritten for prod. Why: *Deployment rebinding*: the same definition, environment-specific references.
  - **[decision-log.md](decision-log.md), D-014 to D-016.** See: Why open mirroring replaced Azure SQL / ADLS, the custom pool, and table prefixes. Why: *Tenant governance forcing design*: public network access disabled.
- **Insights:**
  - Open mirroring gives the same CDC demo with no network dependency.
  - Private-networking tenants need a VNet data gateway / private endpoints module before real mirroring and ADLS shortcuts work.

### Step 1e: API layer · issue #6
- **Task:** an API over gold data.
- **Actions:** **API for GraphQL** `gql_finance` (query-only, PII fields unmapped) plus a Fleet API client over KQL functions.
- **Blockers:** none. User Data Functions were deferred because there was no write use case (D-017).
- **Results:** nested queries work; requesting `email` fails schema validation.
- **Click and see:**
  - **ce-finance-dev → `gql_finance` → Query editor, schema explorer.** See: Types and relationships with no `email`; run a nested query (contract → customer → payments). Why: *API for GraphQL* generates its schema from the mapped data; unmapped fields don't exist. That limits exposure, but it isn't access control.
- **Read and done:**
  - **[api_layer.py](../infra/api_layer.py).** See: Sample GraphQL queries and the Fleet API client calling KQL functions. Why: *Two read APIs*: GraphQL over lakehouse gold, KQL functions over Eventhouse.
  - **[decision-log.md](decision-log.md), D-017.** See: Why User Data Functions were deferred. Why: *UDFs* are for write or logic APIs; there was no write use case.
- **Insight:** field mapping is a cheap first privacy layer, but it isn't access control. Steps 8–9 add that.

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
- **Click and see:**
  - **ce-fleet-dev → `rtd_fleet_ops`.** See: 11 tiles, the time-range parameter, auto-refresh; *Edit* a tile to see its KQL. Why: *Real-Time Dashboard*: each tile is a live KQL query against Eventhouse at refresh; no import, no semantic model.
  - **ce-fleet-dev → `act_fleet_device_faults` → rule → *Definition* and *History*.** See: KQL source, 5-minute poll interval, condition, email action, past firings. Why: *Activator* evaluates on a schedule; the poll interval is the cost/latency dial.
  - **ce-fleet-dev → `act_fleet_offline_sites` → rule.** See: The condition fires on an *offline transition* event, not on "is offline". Why: *Events vs state*: alerting on a state re-fires every poll; alerting on a transition fires once.
- **Read and done:**
  - **[rti_alerts.py](../infra/rti_alerts.py).** See: Activator definitions as code: entity, attributes, rule, action. Why: *Activator object model*.
  - **[decision-log.md](decision-log.md), D-018.** See: Why Activators stay out of Git. Why: Their definitions contain recipient accounts, which can't go into a public repo.
- **Insight:** poll intervals (5 min) are the Activator cost/latency dial. Alert on **transitions**, not states.

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
- **Click and see:**
  - **ce-fleet-dev → `da_fleet_ops` → *Test*: "How many devices are online?", then expand the run steps.** See: The generated KQL and the tables it used. Why: *NL-to-KQL*: the agent writes queries only against selected **tables**; it can't see MVs or functions, so instructions and few-shots carry the gold logic.
  - **ce-finance-dev → `da_finance` → data source → selected tables and columns; then ask for a customer's email.** See: PII columns deselected; the agent declines. Why: *Source scoping*: what the agent can't select, it can't leak. It's the agent's first privacy layer.
  - **ce-asset-dev → `da_contoso_360` → agent instructions, data source instructions, example queries.** See: Business rules in the agent instructions; per-source hints and few-shots. Why: *Instructions vs examples*: instructions route and define terms; examples teach query shape.
  - **Any agent → *Publish*.** See: Draft vs published version; the Microsoft 365 Copilot / Teams option. Why: *Draft vs published*: consumers only ever see the published version.
- **Read and done:**
  - **[data_agents.py](../infra/data_agents.py).** See: Agent definitions as code, plus the `test` and `compare` commands. Why: *Agents as code*: reproducible and reviewable.
  - **[decision-log.md](decision-log.md), D-020.** See: Why agents use silver tables plus few-shots. Why: The NL-to-KQL visibility limit.
- **Insight:** design gold as **real tables** if agents must use it. Instructions and few-shots carry the business rules.

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
- **Click and see:**
  - **ce-finance-dev → `sm_finance` → *Open data model*.** See: Direct Lake tables, 13 measures, relationships. Why: *Direct Lake* reads Delta from OneLake without import; *framing* pins a Delta version at refresh; unsupported queries fall back to DirectQuery.
  - **ce-finance-dev → `rpt_finance_kpis`.** See: Collection rate 95.86 %, delinquency 6.02 %, trend, matrix. Why: *Parity*: these exact numbers were checked against SQL.
  - **`sm_finance` → Settings → Endorsement.** See: Promoted vs Certified (Certified needs a tenant-approved certifier). Why: *Endorsement* is the trust signal in the catalog; certification is governed, so it has no API.
- **Read and done:**
  - **[semantic_model.py](../infra/semantic_model.py).** See: TMDL generated from the live schema; `verify` compares DAX with SQL. Why: *Parity checks* catch logic errors that no schema validation will.
  - **[sm_finance.SemanticModel](../workspaces/ce-finance/sm_finance.SemanticModel/).** See: Measures and tables in TMDL. Why: *TMDL / PBIR as code*: model and report changes become reviewable diffs.
- **Insight:** a scripted DAX-vs-SQL parity check plus a render check catches errors no schema validation will.

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
- **Click and see:**
  - **ce-asset-dev → `ont_contoso` → entity diagram.** See: 6 entity types with keys, 5 relationships, Device telemetry as a time series, 1 rule, no PII. Why: *Ontology*: the business vocabulary bound to data, so agents get structure without join instructions.
  - **`ont_contoso` → an entity → *Bindings*.** See: Every entity bound to tables in `lh_asset`. Why: *One SQL endpoint per ontology*: hence a hub lakehouse with shortcuts.
  - **ce-asset-dev → `lh_asset` → Tables.** See: Shortcuts to `eh_fleet` tables (OneLake availability) and to finance gold. Why: *OneLake availability* exposes Eventhouse tables as Delta, minutes behind real time.
  - **ce-asset-dev → `da_contoso_ontology` and `da_contoso_360` → *Test*: ask both the same question.** See: Similar answers; the ontology agent needs no join instructions. Why: *Ontology agent vs data agent*: context from a governed artifact vs from prose.
- **Read and done:**
  - **[ontology-vs-data-agent.md](ontology-vs-data-agent.md).** See: Side-by-side answers, latency, what each needs. Why: When an ontology pays off.
  - **[ontology.py](../infra/ontology.py).** See: The `ONT_*` annotations and `additionalBackingTable`. Why: *Undocumented requirements*: without them every entity is *Ineligible*.
- **Insights:**
  - The ontology moves business meaning out of prompts into a governed, reusable artifact.
  - The trade-off is freshness: Eventhouse data arrives through OneLake availability, minutes behind.

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
- **Click and see:**
  - **ce-fleet-dev → `cap_rtd_capacity`.** See: CU % over time, throttling indicators, top items during the load test. Why: *Capacity overview events* arrive every ~30 s: a real-time view the Metrics app doesn't give.
  - **ce-fleet-dev → `act_capacity_70` → *History*.** See: One firing at the 70 % upward crossing. Why: *Alert on the crossing*, not on every sample above the threshold.
  - **ce-fleet-dev → `mon_fleet` monitoring database → query `WarehouseExecutions` and `SemanticModelLogs`.** See: CPU, duration and caller per SQL query and DAX query. Why: *Workspace monitoring* gives query-level root cause.
  - **ce-fleet-dev → `cap_rpt_capacity`.** See: Cost from Cost Management, items, job outcomes. Why: *Chargeback*: only the API path has money.
  - **Fabric Capacity Metrics app (once installed) → Compute, around the load test window.** See: Interactive delay and rejection, smoothing, overage burn-down. Why: *Smoothing and throttling stages*: interactive work is smoothed over ~5 min, background over 24 h; throttling escalates from delay to rejection.
  - **Admin portal → Capacity settings → capacity → Surge protection.** See: The background-rejection thresholds. Why: *Surge protection* sheds background jobs to keep interactive users working.
- **Read and done:**
  - **[capacity-comparison.md](../monitoring/capacity-comparison.md).** See: What the Metrics app can't show, which option closes each gap, FUAM assessment, load-test timeline. Why: *Three monitoring paths*: Metrics app, RTI, Power BI + APIs.
  - **[loadgen.py](../monitoring/loadgen.py).** See: How SQL and DAX load reproduced throttling on F2. Why: *Interactive vs background CU*.
- **Insights:**
  - Only the RTI path gives real-time alerts and query-level root cause.
  - Only the Power BI/API path gives money.
  - Per-item CU chargeback needs the Metrics app model (use FUAM).
  - Eventhouse uptime, not query count, drives its CU.

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
- **Click and see:**
  - **Monitor hub → filter Status = Failed.** See: Failed `pl_finance_daily` runs with item type, start time, duration. Why: *Monitor hub*: one job list across the workspaces you can access, with short history.
  - **`pl_finance_daily` → a failed run → failed activity → error; for the notebook, the Spark logs.** See: The actual error text (missing file). Why: *Error text lives only in job and activity logs*, not in job events or workspace monitoring.
  - **ce-fleet-dev → `eh_fleet` → `cap_monitoring` → query `job_events`.** See: Failed events within 3–4 s, logged item type `SynapseNotebook`. Why: *Job events* are the fastest signal; logged type names differ from REST names.
  - **ce-fleet-dev → `act_job_failures` → rules.** See: Two rules routed by item type to different recipients. Why: *Alert routing*: data engineering vs BI owners.
- **Read and done:**
  - **[observability.md](../monitoring/observability.md).** See: Detection latency per source (3–4 s, 49–78 s, 2–7 min to email) and where root cause comes from. Why: *Breadth vs speed vs detail*.
  - **[observability.py](../infra/observability.py).** See: The `failure_mode` injection and the routed Activator. Why: *Failure injection*: test the alert before you need it.
- **Insights:**
  - Use workspace monitoring for breadth, job events for speed, and the job API for the error text.
  - Build alert filters from **logged** values, not REST type names.

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
- **Click and see:**
  - **OneLake catalog → filter Domain = Finance.** See: Items, endorsement badges, descriptions. Why: *Catalog*: discovery, where descriptions and endorsement drive trust.
  - **ce-finance-dev → `lh_finance` → *Manage OneLake security* → `FinanceViewerNoPII`.** See: Gold tables only, hidden PII columns (CLS), the Coastal row filter (RLS), members. Why: *OneLake security roles* restrict Viewers / ReadAll readers; Contributors and above bypass them.
  - **`lh_finance` → SQL analytics endpoint → Security / settings → access mode.** See: *Delegated identity*. Why: *Delegated mode*: SQL runs as the item owner, so OneLake security isn't applied; T-SQL DENYs fill the gap until the switch to user identity.
  - **ce-finance-dev → Lineage view → `lh_finance` → *Impact analysis*.** See: Downstream items and the `lh_asset` ⇄ `lh_finance` cycle. Why: *Impact analysis*: check who breaks before you change a table.
  - **GitHub → Actions → `governance-test` → run summary.** See: Allowed and denied results per channel, as the restricted service principal. Why: *Test as the restricted identity*: an admin sees everything.
- **Read and done:**
  - **[lineage.md](lineage.md).** See: The generated lineage graph and impact lists. Why: *Lineage as code*.
  - **[governance_test.py](../infra/governance_test.py).** See: Every probe and its expected outcome. Why: *Security as a CI test*.
  - **[decision-log.md](decision-log.md), D-032.** See: Delegated vs user identity mode, and why DENYs. Why: *Defense in depth* until a portal-only switch is made.
- **Insights:**
  - Always test security **as the restricted identity**; an admin sees everything.
  - Workspace Contributors and above bypass OneLake security entirely.
  - Shortcut access is governed by the **source** item's rules.

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
- **Click and see:**
  - **Fabric → Microsoft Purview hub.** See: Label coverage and sensitive items. Why: *Purview hub*: Fabric's protection summary.
  - **Purview portal → Audit → search (e.g. activity `DataAgentFabricQueried`).** See: Fabric activities with the user, including who asked an agent. Why: *Purview Audit = Fabric activity events*: the same records.
  - **Purview portal → Information protection → Labels.** See: Whether labels exist and are published. Why: *Labels must be published* before Fabric can apply them; labels then inherit downstream.
  - **Purview portal → Data loss prevention → Policies.** See: No Fabric policy yet. Why: *DLP for Fabric* needs E5-class licensing and a compliance admin.
- **Read and done:**
  - **[governance/purview/README.md](../governance/purview/README.md).** See: The licensing map and the order of enablement. Why: *Licensing vs capacity*: Purview features are licensed per user, not by Fabric SKU.
  - **[purview.py](../infra/purview.py).** See: Label status, bulk apply, audit export with hashed users. Why: *Bulk labeling* once labels exist.
- **Insight:** Fabric governance is ready before Purview is. Labels and DLP are a tenant-licensing project, so plan them separately from the data build.

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
- **Before you click:** Before clicking: resume the obs capacity (`py infra/capacity.py resume --target obs`); items on a paused capacity won't open.
- **Click and see:**
  - **ce-obs-data → `lh_obs_finance` → *Manage OneLake security*.** See: The role, its tables, the test user as member; the deploy SP is not a member. Why: *Role membership* decides OneLake reads for Viewers.
  - **`lh_obs_finance` → SQL analytics endpoint → access mode.** See: *Delegated identity*, even on a new lakehouse. Why: *Delegated mode* is why a Viewer with no role read PII through SQL and agents while OneLake returned 403.
  - **ce-obs-data → Workspace settings → OneLake → Diagnostics, then the destination lakehouse's Files.** See: *Enabled* with a destination, but empty folders. Why: *OneLake diagnostics* would log storage-level reads per identity, the only source for denials; it isn't delivering yet.
  - **ce-obs-audit → monitoring KQL database → query `WarehouseExecutions`.** See: SQL from the test user and the SP through the shortcut endpoint. Why: *Workspace monitoring* sees engine-level callers, including service principals.
  - **Purview portal → Audit → `DataAgentFabricQueried`.** See: The human who asked the agent. Why: *Question layer vs data layer*: Audit names the person; the data layer sees only the agent.
- **Read and done:**
  - **[access-observability.md](../governance/access-observability.md).** See: The enforcement matrix (identity × channel) and which log saw what. Why: *No single log answers "who touched this data"*.
  - **[access-observability-results-obs.md](access-observability-results-obs.md).** See: Raw correlated probe results. Why: Evidence behind the matrix.
  - **[decision-log.md](decision-log.md), D-041 and D-042.** See: Why an isolated capacity, and delegated SQL. Why: *Isolation for clean evidence*.
- **Insights:**
  - OneLake security protects OneLake, not SQL in delegated mode.
  - No single log answers "who touched this data".
  - Agents hide the human at the data layer, but Purview Audit records them at the question layer.

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
- **Before you click:** The live4 capacity is paused; resume it only to rerun live4.
- **Click and see:**
  - **ce-asset-dev → `lab_da_hub_rich` vs `lab_da_hub_bare` → instructions.** See: Identical sources, long vs one-line instructions. Why: *Controlled comparison*: change one variable to isolate the effect of context.
  - **ce-fleet-dev → `eh_fleet` → `lab_agent_eval` database and `lab_rtd_agent_eval`.** See: Scores per question, every tool call with its source, tokens, loop decisions. Why: *Observable evaluation*: failures are queryable, not buried in files.
  - **Foundry portal → project `agent-eval` → Evaluation.** See: Similarity scores per answer. Why: *LLM judge vs rules*: compare with the rule grades in the reports.
- **Read and done:**
  - **[eval/README.md](../eval/README.md).** See: The pipeline: golden set, collect, graders, feedback loop. Why: *Golden set with live truth*.
  - **[eval/reports/live3.md](../eval/reports/live3.md).** See: Instructions vs ontology by category; grader agreement and kappa. Why: *Rules as the reference grader*; *Cohen's kappa* for agreement beyond chance.
  - **[eval/reports/live2.5.md](../eval/reports/live2.5.md).** See: The gated optimizer loop and why every proposal was rejected. Why: *Train / validation / test splits* and *guardrails against overfitting*.
  - **[eval/reports/live4.md](../eval/reports/live4.md) and [capacity-latency.md](../eval/reports/capacity-latency.md).** See: Latency flat across stores and SKUs. Why: *Orchestration dominates agent latency*.
- **Insights:**
  - Grade with deterministic rules plus the logged SQL/KQL; LLM judges are lenient on wrong numbers phrased like the expected answer.
  - The ontology carries structure, not business definitions; definitions must be written down (instructions) or agents and optimizers invent them.
  - Neither a bigger SKU nor a different store makes agents faster; fewer orchestration steps do.

### Step 12: Operations agent vs Activator (added) · issue #36
- **Task:** compare the operations agent (preview) with Activator on identical events. Phase 1: alerts. Phase 2: remediation with vs without approval. Phase 3: ontology as a second source.
- **Actions:**
  - In the isolated lab: its own Eventhouse, simulator stream and Activator clones.
  - A generator option for timed injections (`--inject TYPE:SITE@+MIN`).
  - A **probe Activator** whose action runs a pipeline, so every firing is durable evidence.
  - Failure-injection and remediation pipelines with job events.
  - An external approver: B2B guest in `ce-fabric-ops-approvers` with Contributor on `ce-obs-ops` only (`infra/ops_approver.py`, D-044).
  - API-authored agents (`infra/operations_agent.py`), then portal-authored agents `opa_obs_portal` and `opa_obs_jobs_portal` (D-045).
  - Evidence from ground truth, job events and `EventhouseQueryLogs` (`operations_agent.py evidence`).
- **Blockers → solutions:**
  - **API-created agents never generate a playbook**, even a one-line rule; their source reads back as id `0000…`. The same text in a portal-created agent worked, so agents are authored in the portal (D-045).
  - The definition API returns empty configuration for portal agents, so agents can't go through Git or fabric-cicd yet.
  - Copilot rejected rules it can't express (absence of data, once per id). Rewritten as one property + *is above / becomes*.
  - `FabricJobAction` is rejected by the API, and the action wasn't attached in the portal, so the agent had nothing to recommend.
  - A corporate account couldn't be invited as a guest; a personal-domain guest was invited but hadn't accepted, so the creator received messages.
- **Results:**
  - Phase 1 (3 battery overheats): Activator **3/3**, about 10 min from anomaly to action. The agent's rule ran every 5 min (about 76 ms) and messaged Teams.
  - Phase 2 (failed job): the agent detected it in **2 m 38 s** with context and *Investigate further*. Activator auto-remediated in **6 m 42 s**, logged as `Manual`. No approval happened (no action attached).
  - Earlier trial: Activator 6/6, a flapping alert fired 4 times, auto-remediation 5 m 34 s.
- **Before you click:** Resume the obs capacity first (`py infra/capacity.py resume --target obs`).
- **Click and see:**
  - **ce-obs-ops → `opa_obs_portal` → Agent playbook.** See: The generated property (battery_temp_c), the condition *is above 55* and the rule's KQL query. Why: *Playbook*: Copilot turns instructions into explicit, inspectable rules evaluated every 5 minutes.
  - **ce-obs-ops → `opa_obs_smoke` vs `opa_obs_portal`.** See: The same one-line instruction; the API-created agent has no playbook, the portal-created one does. Why: *Portal-only authoring*: the definition API and the portal don't share agent state yet (D-045).
  - **ce-obs-ops → `opa_obs_jobs_portal` → View activity, and the Teams chat *Fabric Operations Agent*.** See: The failed-job message with job id, rule text, *Investigate further*, and "no actions available". Why: *Human in the loop*: approval only exists when an action is attached; it runs as the creator.
  - **ce-obs-ops → Manage access.** See: `ce-fabric-ops-approvers` and the guest as Contributor; no role on the data workspaces. Why: *Least privilege for approvers*: recipients need write on the agent, not data access.
  - **ce-obs-ops → `act_obs_job_autofix` → *History*, then `pl_obs_remediation` → runs.** See: The failure triggering the remediation pipeline, invoke type *Manual*. Why: *Autonomous remediation*: fast and scriptable, but the run doesn't reveal it was automated.
  - **ce-obs-audit → `ce-obs-ops_mon_obs_ops_Database` → query `EventhouseQueryLogs`.** See: The agent's generation queries (schema sampling, entity materialization), its rule every 5 min and the 7-day investigation query. Why: *Agent observability*: every agent step is a KQL query under the creator's identity (OBO).
- **Read and done:**
  - **[operations-agent.md](../monitoring/operations-agent.md).** See: Both trials' timelines, the root cause of the blocked attempt, approver identity, CU. Why: *Rules vs agent*.
  - **[ops_approver.py](../infra/ops_approver.py).** See: The guest, group, license and role assignments, and what each is for. Why: *External approvers*.
  - **[operations_agent.py](../infra/operations_agent.py).** See: v1 vs v2 instructions, what the API accepts, the `evidence` collector. Why: *Writing rules the condition model can express*.
  - **[decision-log.md](decision-log.md), D-043 to D-045.** See: When to use Activator vs the agent, approver permissions, portal authoring. Why: *Determinism vs judgment*.
- **Insights:**
  - Activator is scriptable and dependable for deterministic rules and can act alone, but it's noisy on flapping signals and its runs look manual.
  - The agent detected faster and explained more, but it's portal-authored only: no Git, CI/CD or deploy yet.
  - Approval needs an attached action; approvers need Contributor on the agent's workspace only, because actions run as the creator.
  - Copilot's generation feedback is a free spec review: it lists the definitions your instructions are missing.


### Step 13: Work IQ: SharePoint and Teams with a Fabric data agent (added) · issue #42
- **Task:** deploy a data agent to Teams and ground it on Teams/SharePoint content together with Fabric data; create SharePoint content if needed. Without a Microsoft 365 Copilot licence (owner decision), Work IQ itself is recorded as licence-gated; the agent reaches Teams through Copilot Chat.
- **Actions:**
  - A lab app `ce-lab-m365` with admin-consented delegated Graph scopes (`infra/work_iq.py app`).
  - An M365 group giving a SharePoint team site and a Teams team, "Contoso Energy Field Ops".
  - Synthetic content: a warranty/service policy and per-alert procedures in the General channel files, a "Field Visits" list built from real alerts, and dispatcher posts in the channel.
  - Ingestion through Graph into `lh_asset.fieldops_{visits,procedures,policy,channel_messages}`, plus a SharePoint shortcut attempt.
  - Data agent `da_contoso_fieldops` (hub + field-ops tables) and a licence-free Teams post of its answer (`work_iq.py digest`).
- **Blockers → solutions:**
  - No Microsoft 365 Copilot licence: Work IQ and Copilot Studio connected agents are documented as gated (D-046). Publishing to Teams only needed Copilot Chat: the spare Business Premium seat was assigned to the admin (D-047).
  - The Azure CLI token is rejected by SharePoint and lacks Sites/Files/Teams scopes: dedicated lab app.
  - **A plaintext token cache in the OneDrive-synced repo folder was flagged by Purview** (gitignored, never committed). Caches moved to `%LOCALAPPDATA%` and encrypted, the app re-created to invalidate the token, test-user sessions revoked.
  - SharePoint shortcut with workspace identity stays Unauthorized despite `Sites.Selected` + a site grant; portal fallback with an organizational account.
  - First answers failed (unselected channel table, unmapped vocabulary); fixed with instructions and few-shots.
- **Results:**
  - 4 cross-source questions: 3 correct (late Critical visits = ground truth 4; Teams parts-delay posts joined to contracts), 1 partly (missed policy P7), PII request refused.
  - Answers in 15–40 s; the digest is posted in the Teams channel.
  - **Published to the Agent Store and used in Teams → Copilot** (Copilot Chat with Business Premium, D-047): the SLA question answered correctly. The PII question returned a customer_id from a 4-way tie without saying so.
- **Before you click:** The shared capacity must be running (`py infra/capacity.py resume`).
- **Click and see:**
  - **Teams → team *Contoso Energy Field Ops* → General → Posts and Files.** See: The dispatcher posts, the digest from the data agent, and the *Field Ops* folder with the policy and procedures. Why: *Teams channel files are a SharePoint library*: the same files the ingestion and the shortcut read.
  - **SharePoint → site *ce-fieldops* → list *Field Visits*.** See: 97 visits with alert_id, site_id, technician code, outcome, warranty claim, cost. Why: *Lists are structured knowledge*: they map to a table with keys shared with Fabric.
  - **ce-asset-dev → `lh_asset` → Tables `fieldops_*`.** See: Four tables with an `_ingested_at` column next to `sites`, `fin_contracts` and the fleet shortcuts. Why: *Bring knowledge to the data*: a data agent can only query tables, so files, lists and messages become Delta tables.
  - **ce-asset-dev → `da_contoso_fieldops` → Test: "Which Critical BATT_OVERTEMP alerts had their field visit after the SLA?".** See: SQL that joins visits (SharePoint), procedures (SharePoint) and alerts (Eventhouse). Why: *Cross-source grounding*: the answer needs the knowledge and the operational data together.
  - **Teams → Copilot → Agents → *da_contoso_fieldops*.** See: The Fabric data agent chatting inside Teams; ask the SLA question, then a PII question. Why: *Agent Store publishing*: a published data agent becomes a Copilot agent for licensed users, querying Fabric under their own identity.
  - **Entra admin center → Enterprise applications → `ce-lab-m365` → Permissions.** See: Delegated Graph scopes with admin consent. Why: *Delegated vs application permissions*: the lab acts as the signed-in admin, nothing tenant-wide for an unattended app.
- **Read and done:**
  - **[work-iq.md](work-iq.md).** See: Work IQ vs Fabric IQ vs this lab, the licence matrix, architecture, results, blockers. Why: *What is licence-gated*.
  - **[work_iq.py](../infra/work_iq.py).** See: App consent, site/team, content, ingestion, shortcut grants, digest. Why: *Microsoft 365 content into OneLake*.
  - **[decision-log.md](decision-log.md), D-046.** See: Why knowledge goes into OneLake and how credentials are kept out of synced folders. Why: *Licence-free design*.
- **Insights:**
  - Without Microsoft 365 Copilot, Work IQ isn't available, but bringing SharePoint and Teams knowledge into OneLake answers the same cross-source questions with governance and SQL joins.
  - Unstructured knowledge needs the same semantic work as tables: vocabulary mapping and few-shots took the agent from 0/3 to 3/3.
  - Credential caches belong outside synced folders, even when gitignored: OneDrive is scanned, and rotation is the only fix once a token is exposed.
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
| 21 | Operations agent playbook: authored playbooks rejected; API-created agents never generate one | Product (preview) | Author agents in the portal; one property + *is above / becomes* per rule | D-045 |
| 22 | `FabricJobAction` rejected by API (UnknownError) | Product (preview) | Add the action in the portal | Step 12 |
| 23 | Branch switch in a shared working folder removed another session's files | Our mistake | Restored from stash in seconds; use `git worktree` | Step 10 |
| 24 | Graph/`az ad` CAE re-auth challenge | Identity | Resolve groups via Fabric role assignments | Step 10 |
| 25 | Service principals can't call data agents with a KQL source | Product (preview) | CI evaluates the ontology agent only | D-036 |
| 26 | F2: every KQL query 429 while streaming + agent eval share CU | Capacity | Evaluate on F32+; scale back after | D-038 |
| 27 | Private Azure SQL mirror: Entra-only policy, DNS for private endpoint, gateway auth | Tenant governance / network | DNS zone group, managed private endpoint, VNet gateway + OAuth2 | D-040 |
| 28 | Hung agent call stalled an eval run 80 min (client default retries) | Our bug | 60 s timeout, 420 s cap per question | Step 11 |
| 29 | Definition API returns empty configuration for portal-created agents (no Git/CI/deploy) | Product (preview) | Portal checklist; scripts only for plumbing and evidence | D-045 |
| 30 | Corporate account can't be invited as a B2B guest (home-tenant restriction) | Identity | Personal-domain guest in a dedicated approver group | D-044 |
| 31 | No Microsoft 365 Copilot licence: Work IQ, Copilot Studio connected agents | Licensing | Knowledge into OneLake; the agent in Teams via Copilot Chat (Business Premium) | D-046, D-047 |
| 32 | Azure CLI token rejected by SharePoint (401) and missing Sites/Files/Teams scopes | Identity | Dedicated lab app with admin-consented delegated scopes | D-046 |
| 33 | Plaintext token cache in the OneDrive-synced repo folder flagged by Purview | Our mistake | Cache in %LOCALAPPDATA%, encrypted; app re-created; sessions revoked | Step 13 |
| 34 | SharePoint shortcut with workspace identity Unauthorized despite Sites.Selected + site grant | Product (preview) | Portal shortcut with organizational account | Step 13 |

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
10. Operations agents (`ce-obs-ops`): attach the Fabric item action `pl_obs_remediation` to `opa_obs_jobs_portal`, rerun the Phase 2 failure and approve in Teams; the guest accepts, installs *Fabric Operations Agent* and becomes recipient; Phase 3 (ontology source) on a portal agent.
11. Pause the obs capacity when idle: `py infra/capacity.py pause --target obs`.
12. Step 13: create the SharePoint shortcut in the portal (organizational account) if the workspace-identity one stays Unauthorized; re-check **Publish to Agent Store** after any agent re-create; with a Microsoft 365 Copilot licence, add it to a Copilot Studio agent.

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
