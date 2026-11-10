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

## Step 0: Prereqs and platform ⬜ (issue: "Step 0")

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

## Step 1: Data ⬜
- **1a Mock data** (`/data-gen`, Python): telemetry, device alerts, asset master, customers, contracts, payments. Realistic distributions, injected anomalies, late and duplicate events. *Agent*, Git.
- **1b Ingestion comparison**: same simulator → (i) Event Hubs → Eventstream, (ii) Eventstream custom endpoint. Output: comparison table + recommendation in decision log. Event Hubs is billed separately (TU/PU); private access paths differ (flag preview networking features).
- **1c Eventhouse medallion**: update policies, materialized views, "Fleet API" KQL functions, retention/caching. *Fabric MCP + Skills*, Git (KQL DB definition).
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
