# Contoso Energy: Microsoft Fabric lab

A public, reusable, hands-on Microsoft Fabric demo for **Contoso Energy**, a fictional company with a residential solar and battery fleet and consumer financing. Everything uses **synthetic data**.

> ⚠️ Sample, not production guidance. See [DISCLAIMER.md](DISCLAIMER.md). Several features are in preview. Running this creates billable Azure/Fabric resources.

## What it shows
Real-time telemetry (Eventstream → Eventhouse medallion → Real-Time dashboard + Activator), a finance lakehouse medallion, an API layer, data agents vs a Fabric IQ ontology, Power BI KPIs, capacity monitoring options, observability, governance and Purview. The full plan, preview flags and licensing notes are in [plan/runbook.md](plan/runbook.md).

## Layout
| Path | Contents |
|---|---|
| [plan/](plan/) | Runbook, demo script, decision log, manual checklists, issue backlog |
| [infra/](infra/) | Idempotent scripts (Fabric CLI, az, gh): capacity, Entra/SP (OIDC), tenant settings, domains, workspaces, roles, Git connect, teardown |
| [workspaces/](workspaces/) | Fabric Git-synced items, one folder per logical workspace (`ce-fleet`, `ce-asset`, `ce-finance`) |
| [data-gen/](data-gen/) | Simulators and load generator |
| [monitoring/](monitoring/) | Capacity / workspace monitoring assets |
| [governance/](governance/) | Governance + Purview assets |
| [.github/workflows/](.github/workflows/) | gitleaks; fabric-cicd (dev → validate, main → prod) |

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
