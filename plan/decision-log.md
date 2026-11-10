# Decision log

| ID | Date | Decision | Why | Alternatives |
|---|---|---|---|---|
| D-001 | 2026-11 | 3 logical workspaces (`ce-fleet`, `ce-asset`, `ce-finance`) × dev/prod; one Git folder per *logical* workspace (`/workspaces/ce-*`); dev and prod share the folder on different branches. | A workspace belongs to exactly one domain, so 3 domains need 3 workspaces. A shared folder means a PR dev → main really promotes items. | Per-environment folders (PRs promote nothing); single workspace (Asset/Finance domains stay empty). |
| D-002 | 2026-11 | Dev workspaces are Fabric Git-synced (`dev` branch). Prod is **not** Git-connected; it is deployed by fabric-cicd from `main`. The `dev` push runs validation only. | Two writers (Git sync + CI/CD) on one workspace drift. fabric-cicd's `parameter.yml` rebinds env-specific IDs; Git sync doesn't. | Git sync on prod (no ID rebinding); both (conflicts). Prod can be Git-connected read-only later as a demo. |
| D-003 | 2026-11 | CI/CD auth = Entra app + GitHub OIDC federated credential per GitHub environment; no client secret. SP scoped via `ce-fabric-automation` group in tenant settings; Contributor on prod workspaces only. | Nothing to rotate or leak; least privilege. | Client secret (rotation burden). |
| D-004 | 2026-11 | Infra scripts = Python wrappers over Fabric CLI / `fab api`, az, gh; tenant values only from `.env`. | Cross-platform, idempotent, same commands as Fabric CLI docs. | Pure Terraform (Fabric provider): good option later for IaC drift detection. |
| D-005 | 2026-11 | Leak guards: gitleaks (pre-commit + CI, required check) + local `private-terms` hook fed from `.env`/`.private-terms.txt` + GitHub push protection. | The public gitleaks config can't list private terms without leaking them. | gitleaks only. |
