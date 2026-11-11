"""Commit a Fabric workspace to Git (from Fabric), excluding unmanaged items (common.UNMANAGED_ITEMS).

Activator (Reflex) items carry alert recipients (tenant accounts), so they are excluded from this public repo
and managed by script instead (D-018); ontology-managed graph/eventhouse children are recreated per workspace.

  py infra/git_sync.py status --workspace ce-fleet-dev
  py infra/git_sync.py commit --workspace ce-fleet-dev --message "Step 2: ..." [--exclude-regex REGEX]
"""
from __future__ import annotations

import argparse
import re

from common import UNMANAGED_ITEMS, load_env, load_state, log
from fabric_rest import fabric


def changes(ws_id: str) -> tuple[list[dict], str]:
    st = fabric("get", f"workspaces/{ws_id}/git/status")
    return st.get("changes", []), st.get("workspaceHead")


def commit(workspace: str, message: str, exclude_regex: str = UNMANAGED_ITEMS) -> list[str]:
    ws_id = load_state("workspaces")[workspace]["id"]
    ch, head = changes(ws_id)
    keep = [c for c in ch if c.get("workspaceChange") and
            not re.search(exclude_regex, c["itemMetadata"]["displayName"])]
    skipped = sorted({c["itemMetadata"]["displayName"] for c in ch} - {c["itemMetadata"]["displayName"] for c in keep})
    if not keep:
        log(f"{workspace}: nothing to commit" + (f" (excluded: {skipped})" if skipped else ""))
        return []
    items = [{"objectId": c["itemMetadata"]["itemIdentifier"].get("objectId"),
              "logicalId": c["itemMetadata"]["itemIdentifier"].get("logicalId")} for c in keep]
    fabric("post", f"workspaces/{ws_id}/git/commitToGit",
           {"mode": "Selective", "workspaceHead": head, "comment": message, "items": items})
    names = [c["itemMetadata"]["displayName"] for c in keep]
    log(f"{workspace}: committed {names}" + (f"; excluded {skipped}" if skipped else ""))
    return names


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["status", "commit"])
    p.add_argument("--workspace", required=True)
    p.add_argument("--message", default="Commit from Fabric")
    p.add_argument("--exclude-regex", default=UNMANAGED_ITEMS)
    a = p.parse_args()
    load_env()
    if a.action == "status":
        ch, _ = changes(load_state("workspaces")[a.workspace]["id"])
        for c in ch:
            print(c["itemMetadata"]["displayName"], c["itemMetadata"]["itemType"], c.get("workspaceChange"), c.get("remoteChange"))
    else:
        commit(a.workspace, a.message, a.exclude_regex)


if __name__ == "__main__":
    main()
