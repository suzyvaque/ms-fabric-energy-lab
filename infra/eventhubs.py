"""Azure Event Hubs for the step 1b ingestion comparison (Azure CLI). Idempotent. Secretless (Entra ID).

Creates a Standard namespace (1 TU, TLS 1.2, local/SAS auth disabled), hub `fleet-telemetry`
(4 partitions, 24 h retention) and consumer group `fabric`. Grants:
  - the signed-in user          -> Azure Event Hubs Data Sender   (simulator, DefaultAzureCredential)
  - ce-fleet-dev workspace identity -> Azure Event Hubs Data Receiver (Eventstream source; see rti_lab.py)
Writes EVENTHUB_NAMESPACE to .env.

  py infra/eventhubs.py create
  py infra/eventhubs.py grant-receiver --principal-id <workspace identity SP object id>
  py infra/eventhubs.py status
  py infra/eventhubs.py delete --yes
"""
import argparse
import json
import secrets

from common import env, load_env, log, require_tool, run, set_env_value

HUB = "fleet-telemetry"
CONSUMER_GROUP = "fabric"


def az(*args: str, check: bool = True):
    res = run(["az", *args, "--subscription", env("AZURE_SUBSCRIPTION_ID"), "-o", "json"], check=check)
    if res.returncode != 0:
        return None
    return json.loads(res.stdout) if res.stdout.strip() else {}


def namespace() -> str:
    ns = env("EVENTHUB_NAMESPACE", required=False)
    if not ns:
        ns = f"ce-energylab-{secrets.token_hex(3)}"
        set_env_value("EVENTHUB_NAMESPACE", ns)
    return ns


def hub_scope() -> str:
    return (f"/subscriptions/{env('AZURE_SUBSCRIPTION_ID')}/resourceGroups/{env('AZURE_RESOURCE_GROUP')}"
            f"/providers/Microsoft.EventHub/namespaces/{env('EVENTHUB_NAMESPACE')}/eventhubs/{HUB}")


def grant(principal_id: str, principal_type: str, role: str) -> None:
    existing = az("role", "assignment", "list", "--assignee", principal_id, "--scope", hub_scope(),
                  "--role", role, check=False) or []
    if not existing:
        az("role", "assignment", "create", "--assignee-object-id", principal_id,
           "--assignee-principal-type", principal_type, "--role", role, "--scope", hub_scope())
        log(f"granted {role} on {HUB} to {principal_type}")


def create() -> None:
    rg, ns = env("AZURE_RESOURCE_GROUP"), namespace()
    state = run(["az", "provider", "show", "-n", "Microsoft.EventHub", "--subscription", env("AZURE_SUBSCRIPTION_ID"),
                 "--query", "registrationState", "-o", "tsv"]).stdout.strip()
    if state != "Registered":
        log("registering resource provider Microsoft.EventHub (one-time, ~1-2 min)")
        run(["az", "provider", "register", "-n", "Microsoft.EventHub", "--subscription",
             env("AZURE_SUBSCRIPTION_ID"), "--wait"])
    base = ["-g", rg, "--namespace-name", ns]
    if az("eventhubs", "namespace", "show", "-g", rg, "-n", ns, check=False) is None:
        log(f"creating namespace {ns} (Standard, 1 TU, SAS disabled)")
        az("eventhubs", "namespace", "create", "-g", rg, "-n", ns, "-l", env("AZURE_LOCATION"), "--sku", "Standard",
           "--capacity", "1", "--minimum-tls-version", "1.2", "--disable-local-auth", "true",
           "--tags", "project=contoso-energy-lab", "step=1b")
    if az("eventhubs", "eventhub", "show", *base, "-n", HUB, check=False) is None:
        az("eventhubs", "eventhub", "create", *base, "-n", HUB, "--partition-count", "4",
           "--cleanup-policy", "Delete", "--retention-time-in-hours", "24")
        log(f"hub {HUB} created")
    if az("eventhubs", "eventhub", "consumer-group", "show", *base, "--eventhub-name", HUB, "-n", CONSUMER_GROUP,
          check=False) is None:
        az("eventhubs", "eventhub", "consumer-group", "create", *base, "--eventhub-name", HUB, "-n", CONSUMER_GROUP)
    me = run(["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"]).stdout.strip()
    grant(me, "User", "Azure Event Hubs Data Sender")


def status() -> None:
    ns = env("EVENTHUB_NAMESPACE")
    n = az("eventhubs", "namespace", "show", "-g", env("AZURE_RESOURCE_GROUP"), "-n", ns)
    h = az("eventhubs", "eventhub", "show", "-g", env("AZURE_RESOURCE_GROUP"), "--namespace-name", ns, "-n", HUB)
    print(json.dumps({"sku": n["sku"], "status": n.get("status"), "disableLocalAuth": n.get("disableLocalAuth"),
                      "hub": h["name"], "partitions": h["partitionCount"],
                      "retention": h.get("retentionDescription")}, indent=2))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "grant-receiver", "status", "delete"])
    p.add_argument("--principal-id")
    p.add_argument("--yes", action="store_true")
    a = p.parse_args()
    load_env()
    require_tool("az", "Install Azure CLI and run az login")
    if a.action == "create":
        create()
    elif a.action == "grant-receiver":
        grant(a.principal_id, "ServicePrincipal", "Azure Event Hubs Data Receiver")
    elif a.action == "status":
        status()
    else:
        if not a.yes:
            raise SystemExit("delete requires --yes")
        az("eventhubs", "namespace", "delete", "-g", env("AZURE_RESOURCE_GROUP"), "-n", env("EVENTHUB_NAMESPACE"))
        log("namespace deleted")


if __name__ == "__main__":
    main()
