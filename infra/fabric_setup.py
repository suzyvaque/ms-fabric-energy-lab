"""Fabric setup: domains, workspaces (dev+prod), capacity + domain assignment, roles, Git connect.

Idempotent; rerun safely. Run as a Fabric admin (domains need the admin API):
  fab auth login            # interactive user
  az login                  # used only to resolve Entra group IDs
  py infra/fabric_setup.py                     # everything
  py infra/fabric_setup.py --only git          # just Git connect (dev workspaces)
"""
import argparse
import json

from common import (CONFIG, env, fab, fab_api, load_env, load_state, log, require_tool, run,
                    save_state, workspace_name)


def capacity_id() -> str:
    _, data = fab_api("capacities")
    name = env("FABRIC_CAPACITY_NAME")
    for c in data.get("value", []):
        if c["displayName"] == name:
            if c.get("state") != "Active":
                raise SystemExit(f"capacity {name} is {c.get('state')}; run: py infra/capacity.py resume")
            return c["id"]
    raise SystemExit(f"capacity {name} not visible to you; run infra/capacity.py create")


def ensure_domains() -> dict:
    ids = {}
    _, data = fab_api("admin/domains")
    existing = {d["displayName"]: d["id"] for d in data.get("domains", data.get("value", []))}
    for d in CONFIG["domains"]:
        if d["name"] not in existing:
            fab("create", f".domains/{d['name']}.Domain", "-P", f"description={d['description']}")
            log(f"domain created: {d['name']}")
            _, data = fab_api("admin/domains")
            existing = {x["displayName"]: x["id"] for x in data.get("domains", data.get("value", []))}
        ids[d["name"]] = existing[d["name"]]
    return ids


def ensure_workspaces(cap_id: str, domain_ids: dict) -> dict:
    _, data = fab_api("workspaces")
    existing = {w["displayName"]: w for w in data.get("value", [])}
    state = {}
    for ws in CONFIG["workspaces"]:
        for environment in CONFIG["environments"]:
            name = workspace_name(ws["key"], environment)
            if name not in existing:
                _, created = fab_api("workspaces", "post", {
                    "displayName": name, "capacityId": cap_id,
                    "description": f"Contoso Energy lab - {ws['key']} ({environment})"})
                ws_id = created["id"]
                log(f"workspace created: {name}")
            else:
                ws_id = existing[name]["id"]
                if existing[name].get("capacityId", "").lower() != cap_id.lower():
                    fab_api(f"workspaces/{ws_id}/assignToCapacity", "post", {"capacityId": cap_id})
            fab_api(f"admin/domains/{domain_ids[ws['domain']]}/assignWorkspaces", "post",
                    {"workspacesIds": [ws_id]})
            state[name] = {"id": ws_id, "key": ws["key"], "environment": environment, "folder": ws["folder"]}
    return state


def group_id(name: str) -> str:
    out = run(["az", "ad", "group", "list", "--display-name", name, "--query", "[0].id", "-o", "tsv"]).stdout.strip()
    if not out:
        raise SystemExit(f"Entra group {name} not found; run infra/entra_setup.py")
    return out


def ensure_roles(state: dict) -> None:
    principals = {}
    for environment, mapping in CONFIG["roles"].items():
        for key in mapping:
            if key == "SP":
                principals[key] = (env("SP_OBJECT_ID"), "ServicePrincipal")
            elif key not in principals:
                principals[key] = (group_id(env(key)), "Group")
    for name, ws in state.items():
        _, data = fab_api(f"workspaces/{ws['id']}/roleAssignments")
        current = {r["principal"]["id"]: r for r in data.get("value", [])}
        for key, role in CONFIG["roles"][ws["environment"]].items():
            pid, ptype = principals[key]
            role = role.capitalize()
            if pid not in current:
                fab_api(f"workspaces/{ws['id']}/roleAssignments", "post",
                        {"principal": {"id": pid, "type": ptype}, "role": role})
            elif current[pid]["role"] != role:
                fab_api(f"workspaces/{ws['id']}/roleAssignments/{current[pid]['id']}", "patch", {"role": role})
        log(f"roles ok: {name}")


def ensure_git_connection() -> str:
    name = env("FABRIC_GIT_CONNECTION_NAME")
    _, data = fab_api("connections")
    for c in data.get("value", []):
        if c.get("displayName") == name:
            return c["id"]
    url = f"https://github.com/{env('GITHUB_OWNER')}/{env('GITHUB_REPO')}"
    _, created = fab_api("connections", "post", {
        "connectivityType": "ShareableCloud",
        "displayName": name,
        "connectionDetails": {
            "type": "GitHubSourceControl",
            "creationMethod": "GitHubSourceControl.Contents",
            "parameters": [{"dataType": "Text", "name": "url", "value": url}],
        },
        "credentialDetails": {
            "credentials": {"credentialType": "Key", "key": env("GITHUB_FABRIC_PAT")},
            "singleSignOnType": "None", "connectionEncryption": "NotEncrypted", "skipTestConnection": False,
        },
    })
    log(f"Fabric GitHub connection created: {name}")
    return created["id"]


def ensure_git(state: dict) -> None:
    conn_id = None
    for name, ws in state.items():
        git_cfg = CONFIG["git"][ws["environment"]]
        if not git_cfg["connect"]:
            continue
        conn_id = conn_id or ensure_git_connection()
        _, status = fab_api(f"workspaces/{ws['id']}/git/connection")
        if status.get("gitConnectionState") == "NotConnected":
            fab_api(f"workspaces/{ws['id']}/git/connect", "post", {
                "gitProviderDetails": {
                    "gitProviderType": "GitHub", "ownerName": env("GITHUB_OWNER"),
                    "repositoryName": env("GITHUB_REPO"), "branchName": git_cfg["branch"],
                    "directoryName": ws["folder"]},
                "myGitCredentials": {"source": "ConfiguredConnection", "connectionId": conn_id}})
            log(f"git connected: {name} -> {git_cfg['branch']}:{ws['folder']}")
        _, status = fab_api(f"workspaces/{ws['id']}/git/connection")
        if status.get("gitConnectionState") == "Connected":
            _, init = fab_api(f"workspaces/{ws['id']}/git/initializeConnection", "post",
                              {"initializationStrategy": "PreferRemote"})
            action = init.get("requiredAction", "None")
            if action == "UpdateFromGit":
                fab_api(f"workspaces/{ws['id']}/git/updateFromGit", "post", {
                    "remoteCommitHash": init["remoteCommitHash"], "workspaceHead": init.get("workspaceHead"),
                    "conflictResolution": {"conflictResolutionType": "Workspace",
                                           "conflictResolutionPolicy": "PreferRemote"},
                    "options": {"allowOverrideItems": True}})
            elif action == "CommitToGit":
                fab_api(f"workspaces/{ws['id']}/git/commitToGit", "post", {
                    "mode": "All", "workspaceHead": init.get("workspaceHead"),
                    "comment": "Initial commit from Fabric"})
            log(f"git initialized: {name} (action: {action})")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", choices=["domains", "workspaces", "roles", "git"])
    a = p.parse_args()
    load_env()
    require_tool("fab", "py -m pip install -r infra/requirements.txt; fab auth login")
    if a.only in (None, "roles"):
        require_tool("az", "Install Azure CLI and run az login")

    if a.only == "domains":
        ensure_domains()
        return
    state = load_state("workspaces")
    if a.only in (None, "workspaces") or not state:
        state = ensure_workspaces(capacity_id(), ensure_domains())
        save_state("workspaces", state)
    if a.only in (None, "roles"):
        ensure_roles(state)
    if a.only in (None, "git"):
        ensure_git(state)
    print(json.dumps({k: v["environment"] for k, v in state.items()}, indent=2))


if __name__ == "__main__":
    main()
