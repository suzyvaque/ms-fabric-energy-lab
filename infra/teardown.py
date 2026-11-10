"""Teardown. Default = pause capacity only (stops compute billing, keeps everything).

  py infra/teardown.py                         # pause capacity
  py infra/teardown.py --workspaces --yes      # delete the 6 lab workspaces (items are lost; Git keeps definitions)
  py infra/teardown.py --domains --yes
  py infra/teardown.py --capacity --yes        # delete the capacity (ARM)
  py infra/teardown.py --entra --yes           # delete lab groups + app registration
  py infra/teardown.py --lab --yes             # step 1b: lab_ items + Event Hubs namespace (~$22/month at 1 TU)
  py infra/teardown.py --all --yes
"""
import argparse

from common import CONFIG, env, fab, fab_api, fab_exists, load_env, log, run, workspace_name


def main() -> None:
    p = argparse.ArgumentParser()
    for f in ("workspaces", "domains", "capacity", "entra", "lab", "all", "yes"):
        p.add_argument(f"--{f}", action="store_true")
    a = p.parse_args()
    load_env()
    destructive = a.workspaces or a.domains or a.capacity or a.entra or a.lab or a.all
    if destructive and not a.yes:
        raise SystemExit("Destructive teardown requires --yes")
    cap = f".capacities/{env('FABRIC_CAPACITY_NAME')}.Capacity"

    if (a.lab or a.all) and env("EVENTHUB_NAMESPACE", required=False):
        if not (a.workspaces or a.all):
            from rti_lab import delete_lab
            delete_lab()
        run(["az", "eventhubs", "namespace", "delete", "--subscription", env("AZURE_SUBSCRIPTION_ID"),
             "-g", env("AZURE_RESOURCE_GROUP"), "-n", env("EVENTHUB_NAMESPACE")], check=False)
        log("lab items + Event Hubs namespace deleted")

    if a.workspaces or a.all:
        for ws in CONFIG["workspaces"]:
            for environment in CONFIG["environments"]:
                path = f"{workspace_name(ws['key'], environment)}.Workspace"
                if fab_exists(path):
                    fab("rm", path, "-f")
                    log(f"deleted {path}")
    if a.domains or a.all:
        _, data = fab_api("admin/domains")
        existing = {d["displayName"] for d in data.get("domains", data.get("value", []))}
        for d in CONFIG["domains"]:
            if d["name"] in existing:
                fab("rm", f".domains/{d['name']}.Domain", "-f")
                log(f"deleted domain {d['name']}")
    if a.capacity or a.all:
        if fab_exists(cap):
            fab("rm", cap, "-f")
            log("capacity deleted")
    elif fab_exists(cap):
        fab("stop", cap, "-f", check=False)
        log("capacity paused")
    if a.entra or a.all:
        for key in ("ENTRA_GROUP_ADMINS", "ENTRA_GROUP_ENGINEERS", "ENTRA_GROUP_VIEWERS", "ENTRA_GROUP_AUTOMATION"):
            gid = run(["az", "ad", "group", "list", "--display-name", env(key), "--query", "[0].id", "-o", "tsv"]).stdout.strip()
            if gid:
                run(["az", "ad", "group", "delete", "--group", gid])
        if env("SP_CLIENT_ID", required=False):
            run(["az", "ad", "app", "delete", "--id", env("SP_CLIENT_ID")], check=False)
        log("entra objects deleted")


if __name__ == "__main__":
    main()
