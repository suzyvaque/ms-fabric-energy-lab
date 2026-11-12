"""Fabric capacity lifecycle (Fabric CLI over ARM). Idempotent.

  py infra/capacity.py create      # resource group + F SKU capacity (skips if it exists)
  py infra/capacity.py status
  py infra/capacity.py pause       # stop billing when idle
  py infra/capacity.py resume
  py infra/capacity.py resize --sku F4
  py infra/capacity.py <action> --target obs   # second, isolated capacity for observability tests (steps 10/12):
                                               # FABRIC_OBS_CAPACITY_NAME / FABRIC_OBS_CAPACITY_SKU (default F64)
"""
import argparse

from common import env, fab, fab_exists, load_env, log, require_tool, run


def capacity_name(target: str) -> tuple[str, str]:
    if target == "obs":
        return (env("FABRIC_OBS_CAPACITY_NAME", required=False) or f"{env('FABRIC_CAPACITY_NAME')}obs",
                env("FABRIC_OBS_CAPACITY_SKU", required=False) or "F64")
    return env("FABRIC_CAPACITY_NAME"), env("FABRIC_CAPACITY_SKU")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "status", "pause", "resume", "resize"])
    p.add_argument("--sku")
    p.add_argument("--target", choices=["main", "obs"], default="main")
    a = p.parse_args()
    load_env()
    require_tool("fab", "py -m pip install -r infra/requirements.txt; fab auth login")
    name, sku = capacity_name(a.target)
    cap = f".capacities/{name}.Capacity"

    if a.action == "create":
        if fab_exists(cap):
            log(f"{a.target} capacity exists")
        else:
            require_tool("az", "Install Azure CLI and run az login")
            run(["az", "group", "create", "-n", env("AZURE_RESOURCE_GROUP"), "-l", env("AZURE_LOCATION"),
                 "--subscription", env("AZURE_SUBSCRIPTION_ID"), "-o", "none"])
            params = ",".join([
                f"sku={sku}", f"admin={env('ADMIN_UPN')}",
                f"location={env('AZURE_LOCATION')}", f"resourceGroup={env('AZURE_RESOURCE_GROUP')}",
                f"subscriptionId={env('AZURE_SUBSCRIPTION_ID')}"])
            fab("create", cap, "-P", params)
            log(f"created {a.target} capacity ({sku})")
    elif a.action == "status":
        print(fab("get", cap, "-q", "{state: properties.state, sku: sku.name}"))
    elif a.action == "pause":
        fab("stop", cap, "-f")
        log("paused")
    elif a.action == "resume":
        fab("start", cap, "-f")
        log("resumed")
    elif a.action == "resize":
        if not a.sku:
            raise SystemExit("--sku required")
        fab("set", cap, "-q", "sku.name", "-i", a.sku, "-f")
        log(f"resized to {a.sku}")


if __name__ == "__main__":
    main()
