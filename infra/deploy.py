"""Deploy one logical workspace folder with fabric-cicd (used by .github/workflows/fabric-deploy.yml).

Auth: AzureCliCredential, i.e. the OIDC login done by azure/login in the workflow.
Environment-specific values (IDs, connection strings) are swapped via workspaces/<name>/parameter.yml.

  py infra/deploy.py --folder workspaces/ce-fleet --environment prod --workspace-id <id>
"""
import argparse
from pathlib import Path

from azure.identity import AzureCliCredential
from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--folder", required=True)
    p.add_argument("--environment", required=True)
    p.add_argument("--workspace-id", required=True)
    a = p.parse_args()
    folder = ROOT / a.folder
    if not any(folder.rglob(".platform")):
        print(f"No Fabric items in {a.folder} yet - nothing to deploy.")
        return
    ws = FabricWorkspace(
        workspace_id=a.workspace_id,
        environment=a.environment.upper(),
        repository_directory=str(folder),
        token_credential=AzureCliCredential(),
    )
    publish_all_items(ws)
    unpublish_all_orphan_items(ws)


if __name__ == "__main__":
    main()
