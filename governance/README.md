# governance

Governance and Purview assets (steps 8, 9). Scripts live in `infra/` (`governance.py`, `governance_test.py`, `purview.py`).

| Path | What |
|---|---|
| [purview/README.md](purview/README.md) | Labels, DLP, audit, Data Map: what runs here, what's blocked, licensing |
| [purview/dlp-fabric.ps1](purview/dlp-fabric.ps1) | DLP policy template for Fabric (Security & Compliance PowerShell) |
| [../plan/lineage.md](../plan/lineage.md) | Generated lineage graph + impact analysis |
| [../.github/workflows/governance-test.yml](../.github/workflows/governance-test.yml) | OneLake security enforcement test (restricted SP) |
