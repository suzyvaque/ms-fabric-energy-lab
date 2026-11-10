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

## Step 1: Data 🟨

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
- **1d Finance lakehouse medallion** + 1 external source (ADLS/S3 shortcut or mirrored DB). Mirroring sources vary in preview/GA; check before you choose.
- **1e API layer**: GraphQL or User Data Functions over gold.

## Step 2: Real-Time dashboard + Activator ⬜
Device fault, offline device, pipeline failure. Activator runs consume CU.

## Step 3: Data agents ⬜
Eventhouse, lakehouse, combined; publish to Teams/M365 Copilot. **Preview; paid F SKU + Copilot tenant settings; not on trial.** M365 Copilot publishing needs M365 Copilot licenses for consumers.

## Step 4: Semantic model + Power BI KPIs ⬜
Direct Lake model, KPIs (DAX), certify one gold KPI (certification tenant setting + certifier group). Consumers need Pro below F64.

## Step 5: Fabric IQ ontology ⬜
Site, Device, Battery, Alert, Contract, Customer + ontology agent; contrast with data agent. **Preview; tenant setting.**

## Step 6: Capacity monitoring ⬜
Gaps of the Capacity Metrics app first; then (ii) RTI capacity events + workspace monitoring → Eventhouse → RTD + Activator @70 %; (iii) REST/Admin APIs + Semantic Link + Cost Management export → lakehouse → report. Check FUAM (fabric-toolbox, community, unsupported) first. Load generator on a small SKU: throttling, surge protection, pause/resize. Capacity events and parts of workspace monitoring are **preview**. Cost export needs Azure Cost Management Reader.

## Step 7: Observability ⬜
Failure injection → detection → alert routing; Monitor hub; workspace monitoring (uses CU and Eventhouse storage).

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
