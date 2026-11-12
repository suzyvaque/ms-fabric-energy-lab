# Contoso Energy: Microsoft Fabric lab

A public, reusable, hands-on Microsoft Fabric demo for **Contoso Energy**, a fictional company with a residential solar and battery fleet and consumer financing. Everything uses **synthetic data**.

> ⚠️ Sample, not production guidance. See [DISCLAIMER.md](DISCLAIMER.md). Several features are in preview. Running this creates billable Azure/Fabric resources.

## What it shows
Real-time telemetry (Eventstream → Eventhouse medallion → Real-Time dashboard + Activator), a finance lakehouse medallion, an API layer, data agents vs a Fabric IQ ontology, Power BI KPIs, capacity monitoring options, observability, governance and Purview. The full plan, preview flags and licensing notes are in [plan/runbook.md](plan/runbook.md).

| Step | Highlights | Key write-ups |
|---|---|---|
| 1 Data | Synthetic generator; Event Hubs vs Eventstream custom endpoint; Eventhouse medallion + Fleet API; finance lakehouse (open mirroring, cross-domain shortcut); GraphQL | [ingestion-comparison.md](plan/ingestion-comparison.md) |
| 2 Real-time | Real-Time Dashboard; Activator (device fault, offline site) | [runbook](plan/runbook.md) |
| 3–5 AI | Data agents per domain + cross-domain; Fabric IQ ontology + ontology-context agent | [ontology-vs-data-agent.md](plan/ontology-vs-data-agent.md) |
| 4 BI | Direct Lake model, PBIR report, certified KPI checklist | [runbook](plan/runbook.md) |
| 6 Capacity | Metrics app gaps; RTI capacity events + Activator at 70 %; Power BI cost/inventory report; F2 throttling load test | [capacity-comparison.md](monitoring/capacity-comparison.md) |
| 7 Observability | Failure injection; job events vs workspace monitoring latency; routed alerts | [observability.md](monitoring/observability.md) |
| 8 Governance | OneLake security RLS/CLS proven in CI by a restricted SP; lineage + impact analysis | [lineage.md](plan/lineage.md) |
| 9 Purview | Label status/bulk apply, audit, DLP template, licensing map | [purview/README.md](governance/purview/README.md) |
| 10 Access observability | Isolated F64 lab: users vs SP vs agents, allowed vs denied; OneLake diagnostics vs Purview Audit vs workspace monitoring | [access-observability.md](governance/access-observability.md) |
| 12 Operations agent | Agent vs Activator on identical seeded events; auto vs approved remediation | [operations-agent.md](monitoring/operations-agent.md) |

**Start here:** [plan/review.md](plan/review.md) covers each step's goal, actions, blockers, end state, where to look in the portal and repo, and the insights to carry. Every decision is logged with its alternatives in [plan/decision-log.md](plan/decision-log.md).

## Layout
| Path | Contents |
|---|---|
| [plan/](plan/) | Runbook, demo script, decision log, manual checklists, issue backlog |
| [infra/](infra/) | Idempotent scripts (Fabric CLI, az, gh): capacity, Entra/SP (OIDC), tenant settings, domains, workspaces, roles, Git connect, teardown |
| [workspaces/](workspaces/) | Fabric Git-synced items, one folder per logical workspace (`ce-fleet`, `ce-asset`, `ce-finance`) |
| [data-gen/](data-gen/) | Simulators and load generator |
| [monitoring/](monitoring/) | Capacity / workspace monitoring assets |
| [governance/](governance/) | Governance + Purview assets |
| [.github/workflows/](.github/workflows/) | gitleaks; tests; fabric-cicd (dev → validate, main → prod); governance-test (OneLake security as a restricted SP) |

## Environments
```
dev branch  ──Fabric Git sync──▶  ce-*-dev workspaces   (authoring)
main branch ──fabric-cicd (OIDC)──▶ ce-*-prod workspaces (deploy only)
```
Promote by pull request dev → main. `main` is protected (PR only, gitleaks check required).

## Quick start
1. Paid Fabric F SKU (trial capacities don't support Copilot/data agents), Fabric admin, Azure subscription owner.
2. `Copy-Item .env.example .env` and fill it in (gitignored).
3. `pip install -r infra/requirements.txt; pre-commit install`
4. Follow [plan/runbook.md](plan/runbook.md) step 0.

## Contributing / safety
- Commit with a GitHub no-reply email (enforced by a pre-commit hook).
- Never commit tenant names, IDs, accounts, customer names or capacity details. gitleaks and a local private-terms hook block them.
- Teardown: `py infra/teardown.py` pauses the capacity. Use `--all --yes` to delete everything.

## License
[MIT](LICENSE)
