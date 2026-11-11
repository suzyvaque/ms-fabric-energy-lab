"""Step 6/7: workspace monitoring (preview). Idempotent. Dev workspaces only (D-027).

  py infra/workspace_monitoring.py create   # mon_finance (owns the monitoring Eventhouse) + mon_fleet / mon_asset (Linked)
  py infra/workspace_monitoring.py status
  py infra/workspace_monitoring.py query --kql "ItemJobEventLogs | take 10"

One Eventhouse for all three workspaces (Linked mode) keeps CU and storage down on a small SKU.
Requires the tenant settings PlatformMonitoringTenantSetting + AllowMonitoringDataCollection (infra/tenant_settings.py).
"""
import argparse
import json
import time

from common import load_env, log
from fabric_rest import FABRIC, _request, b64, fabric, kql
from finance_lakehouse import ws

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/monitoring/definition/monitoringProperties/1.0.0/schema.json"
OWNER = ("finance", "mon_finance")
LINKED = [("fleet", "mon_fleet"), ("asset", "mon_asset")]


def monitoring_item(w: str, name: str) -> dict:
    for i in fabric("get", f"workspaces/{w}/monitoringItems").get("value", []):
        return i  # one Monitoring item per workspace
    fabric("post", f"workspaces/{w}/monitoringItems", {"displayName": name,
                                                       "description": "Workspace monitoring (job, query, refresh logs)"})
    log(f"{name}: created")
    return fabric("get", f"workspaces/{w}/monitoringItems")["value"][0]


def configure(w: str, item: dict, destination: dict) -> None:
    props = {"$schema": SCHEMA, "dataCollectionSettings": [
        {"sources": {"workspaceDiagnostics": {"mode": "All"}}, "destinations": {"eventhouse": destination}}]}
    status, _, body = _request("post", f"{FABRIC}/v1/workspaces/{w}/items/{item['id']}/updateDefinition",
                               {"definition": {"format": "MonitoringV1", "parts": [
                                   {"path": "MonitoringProperties.json", "payload": b64(props), "payloadType": "InlineBase64"}]}},
                               FABRIC)
    # The Eventhouse mode can't change once set, so a re-run that sends the same routing may be rejected: ignore that.
    if status not in (200, 202) and "mode" not in json.dumps(body).lower():
        raise SystemExit(f"{item['displayName']}: updateDefinition {status} {json.dumps(body)[:400]}")


def enable(w: str, item: dict) -> str:
    for attempt in range(20):
        state = fabric("get", f"workspaces/{w}/monitoringItems/{item['id']}")["properties"]["dataCollectionState"]
        if state == "Enabled":
            return state
        status, _, body = _request("post", f"{FABRIC}/v1/workspaces/{w}/monitoringItems/{item['id']}/enableDataCollection",
                                   {}, FABRIC)
        if status not in (200, 202):
            log(f"{item['displayName']}: enable {status} {body.get('errorCode')} (tenant setting propagation?)")
        time.sleep(30)
    raise SystemExit(f"{item['displayName']}: data collection not enabled")


def create() -> None:
    ow = ws(OWNER[0], "dev")
    owner = monitoring_item(ow, OWNER[1])
    configure(ow, owner, {"mode": "Auto"})
    log(f"{owner['displayName']}: {enable(ow, owner)} (Auto eventhouse)")
    for domain, name in LINKED:
        w = ws(domain, "dev")
        item = monitoring_item(w, name)
        configure(w, item, {"mode": "Linked", "parentMonitoringItem": {"itemId": owner["id"], "workspaceId": ow}})
        log(f"{item['displayName']}: {enable(w, item)} (linked to {OWNER[1]})")


def eventhouse(domain: str = "finance") -> tuple[str, str]:
    """Shared monitoring eventhouse (owned by mon_finance); one KQL database per linked workspace."""
    ow = ws(OWNER[0], "dev")
    items = fabric("get", f"workspaces/{ow}/items")["value"]
    eh = next((i for i in items if i["type"] == "Eventhouse" and i["displayName"] == f"{OWNER[1]}_Eventhouse"), None)
    if not eh:
        raise SystemExit("monitoring eventhouse not found (run create)")
    uri = fabric("get", f"workspaces/{ow}/eventhouses/{eh['id']}")["properties"]["queryServiceUri"]
    name = dict([OWNER, *LINKED])[domain]
    db = next(i for i in items if i["type"] == "KQLDatabase" and i["displayName"].endswith(f"-dev_{name}_Database"))
    return uri, db["id"]


def status() -> None:
    for domain, name in [OWNER, *LINKED]:
        w = ws(domain, "dev")
        items = fabric("get", f"workspaces/{w}/monitoringItems").get("value", [])
        print(domain, [(i["displayName"], i.get("properties", {}).get("dataCollectionState")) for i in items])
    for domain, _ in [OWNER, *LINKED]:
        uri, db = eventhouse(domain)
        rows = kql(uri, db, ".show tables details | where TotalRowCount > 0 | project TableName, TotalRowCount", mgmt=True)
        print(f"  {domain}: " + (", ".join(f"{r['TableName']}={r['TotalRowCount']}" for r in rows) or "no rows yet"))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "status", "query"])
    p.add_argument("--kql")
    p.add_argument("--domain", default="finance", choices=["finance", "fleet", "asset"])
    a = p.parse_args()
    load_env()
    if a.action == "create":
        create()
    elif a.action == "status":
        status()
    else:
        uri, db = eventhouse(a.domain)
        for r in kql(uri, db, a.kql):
            print(json.dumps(r, default=str)[:600])


if __name__ == "__main__":
    main()
