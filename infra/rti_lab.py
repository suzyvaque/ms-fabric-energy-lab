"""Step 1b lab items in the ce-fleet *dev* workspace (Fabric REST). Idempotent.

  eh_fleet            Eventhouse (its default DB eh_fleet is used from step 1c)
  lab_compare         KQL database: table compare_events (both paths land here)
  lab_es_eventhubs    Eventstream: Azure Event Hubs source  -> Eventhouse (processed ingestion)
  lab_es_custom       Eventstream: Custom endpoint source   -> Eventhouse (processed ingestion)
  connection          Fabric cloud connection to the hub (workspace identity, no secrets)

Writes ESTREAM_CONNECTION_STRING (custom endpoint) to .env. `lab_` items are excluded from prod
deploys (infra/deploy.py). Run after infra/eventhubs.py create.

  py infra/rti_lab.py create
  py infra/rti_lab.py info          # query URI, item IDs
  py infra/rti_lab.py pause|resume  # stop/start the lab eventstreams (CU cost)
  py infra/rti_lab.py delete-lab    # remove lab_ items + connection (keeps eh_fleet)
"""
import argparse
import json
import time

from common import env, load_env, load_state, log, require_tool, save_state, set_env_value
from eventhubs import grant
from fabric_rest import b64, fabric, find_item, upsert_item
from fabric_rest import workspace_identity as ws_identity

EVENTHOUSE = "eh_fleet"
LAB_DB = "lab_compare"
TABLE = "compare_events"
HUB = "fleet-telemetry"
CONSUMER_GROUP = "fabric"
CONNECTION = "lab-eventhubs-fleet-telemetry"

LAB_SCHEMA = f"""// Step 1b ingestion comparison. Both eventstreams write here; ingest_path tells them apart.
.create-merge table {TABLE} (record_type:string, schema_version:int, event_id:string, device_id:string, site_id:string, device_type:string, event_time:datetime, sent_time:datetime, seq:long, firmware:string, status:string, fault_code:string, measurements:dynamic, alert_id:string, event_type:string, alert_code:string, severity:string, source:string, message:string, ingest_path:string, EventProcessedUtcTime:datetime, PartitionId:long, EventEnqueuedUtcTime:datetime)
.alter table {TABLE} policy retention "{{\\"SoftDeletePeriod\\": \\"14.00:00:00\\", \\"Recoverability\\": \\"Disabled\\"}}"
.alter table {TABLE} policy caching hot = 3d
"""
JSON_SER = {"type": "Json", "properties": {"encoding": "UTF8"}}


def platform(item_type: str, name: str) -> dict:
    return {"$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": item_type, "displayName": name},
            "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"}}


def ws_id() -> str:
    state = load_state("workspaces")
    name = f"{env('WORKSPACE_PREFIX')}-fleet-dev"
    if name not in state:
        raise SystemExit("run infra/fabric_setup.py first")
    return state[name]["id"]


def workspace_identity(ws: str) -> str:
    """Workspace identity service principal object id (see fabric_rest.workspace_identity)."""
    return ws_identity(ws)["servicePrincipalId"]


def ensure_connection(ws: str) -> str:
    for c in fabric("get", "connections").get("value", []):
        if c.get("displayName") == CONNECTION:
            return c["id"]
    # Secretless: the namespace has local (SAS) auth disabled; Eventstream reads as the workspace identity.
    grant(workspace_identity(ws), "ServicePrincipal", "Azure Event Hubs Data Receiver")
    ns = env("EVENTHUB_NAMESPACE")
    body = {
        "connectivityType": "ShareableCloud", "displayName": CONNECTION,
        "connectionDetails": {"type": "EventHub", "creationMethod": "EventHub.Contents", "parameters": [
            {"dataType": "Text", "name": "endpoint", "value": f"{ns}.servicebus.windows.net"},
            {"dataType": "Text", "name": "entityPath", "value": HUB}]},
        "privacyLevel": "Organizational",
        "credentialDetails": {"credentials": {"credentialType": "WorkspaceIdentity"},
                              "singleSignOnType": "None", "connectionEncryption": "NotEncrypted",
                              "skipTestConnection": False}}
    for attempt in range(10):  # RBAC propagation can take a few minutes
        try:
            created = fabric("post", "connections", body)
            break
        except SystemExit as e:
            if "IncorrectCredentials" not in str(e) or attempt == 9:
                raise
            log("waiting for role assignment to propagate...")
            time.sleep(30)
    log(f"connection {CONNECTION} created (workspace identity)")
    return created["id"]


def eventstream_parts(name: str, source: dict, ws: str, db_id: str) -> list[dict]:
    stream = f"{name}-stream"
    topology = {
        "sources": [source],
        "destinations": [{"name": "compare-events", "type": "Eventhouse", "properties": {
            "dataIngestionMode": "ProcessedIngestion", "workspaceId": ws, "itemId": db_id,
            "databaseName": LAB_DB, "tableName": TABLE, "inputSerialization": JSON_SER},
            "inputNodes": [{"name": stream}]}],
        "streams": [{"name": stream, "type": "DefaultStream", "properties": {},
                     "inputNodes": [{"name": source["name"]}]}],
        "operators": [], "compatibilityLevel": "1.0"}
    return [{"path": "eventstream.json", "payload": b64(topology), "payloadType": "InlineBase64"},
            {"path": "eventstreamProperties.json", "payloadType": "InlineBase64",
             "payload": b64({"retentionTimeInDays": 1, "eventThroughputLevel": "Low"})},
            {"path": ".platform", "payload": b64(platform("Eventstream", name)), "payloadType": "InlineBase64"}]


def custom_endpoint_connection_string(ws: str, es_id: str) -> str:
    # After a create/update the endpoint is re-provisioned; its key can take a minute to appear.
    for attempt in range(12):
        topo = fabric("get", f"workspaces/{ws}/eventstreams/{es_id}/topology")
        src = next(s for s in topo["sources"] if s["type"] == "CustomEndpoint")
        try:
            conn = fabric("get", f"workspaces/{ws}/eventstreams/{es_id}/sources/{src['id']}/connection")
            break
        except SystemExit as e:
            if "AuthorizationRuleNotFound" not in str(e) or attempt == 11:
                raise
            time.sleep(15)
    cs = conn["accessKeys"]["primaryConnectionString"]
    if "EntityPath=" not in cs:
        cs = f"{cs.rstrip(';')};EntityPath={conn['eventHubName']}"
    return cs


def create(force: bool = False) -> None:
    ws = ws_id()
    eh = upsert_item(ws, "Eventhouse", EVENTHOUSE, None, "Contoso Energy fleet telemetry (RTI)")
    log(f"eventhouse {EVENTHOUSE}: {eh['id']}")
    db_parts = [
        {"path": "DatabaseProperties.json", "payloadType": "InlineBase64",
         "payload": b64({"databaseType": "ReadWrite", "parentEventhouseItemId": eh["id"],
                         "oneLakeCachingPeriod": "P7D", "oneLakeStandardStoragePeriod": "P14D"})},
        {"path": "DatabaseSchema.kql", "payload": b64(LAB_SCHEMA), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("KQLDatabase", LAB_DB)), "payloadType": "InlineBase64"}]
    db = upsert_item(ws, "KQLDatabase", LAB_DB, db_parts, "Step 1b ingestion comparison (lab, not deployed to prod)")
    log(f"kql database {LAB_DB}: {db['id']}")

    conn_id = ensure_connection(ws)
    sources = {
        # AzureEventHubExtended = newer connector; supports workspace identity (preview). The classic
        # AzureEventHub source only accepts Basic (SAS), which is unavailable when local auth is disabled.
        "lab_es_eventhubs": {"name": "azure-event-hubs", "type": "AzureEventHubExtended", "properties": {
            "dataConnectionId": conn_id, "consumerGroupName": CONSUMER_GROUP, "startPosition": "Latest"}},
        "lab_es_custom": {"name": "simulator", "type": "CustomEndpoint", "properties": {}},
    }
    ids = {}
    for name, source in sources.items():
        # Updating an eventstream re-provisions it (new custom endpoint key), so only with --force.
        es = upsert_item(ws, "Eventstream", name, eventstream_parts(name, source, ws, db["id"]),
                         "Step 1b ingestion comparison (lab)", update=force)
        ids[name] = es["id"]
        log(f"eventstream {name}: {es['id']}")
    set_env_value("ESTREAM_CONNECTION_STRING", custom_endpoint_connection_string(ws, ids["lab_es_custom"]))
    log("ESTREAM_CONNECTION_STRING written to .env")
    props = fabric("get", f"workspaces/{ws}/eventhouses/{eh['id']}")["properties"]
    save_state("rti", {"workspace_id": ws, "eventhouse_id": eh["id"], "query_uri": props["queryServiceUri"],
                       "lab_db_id": db["id"], "eventstreams": ids, "connection_id": conn_id})


def delete_lab() -> None:
    ws = ws_id()
    for item_type, name in (("Eventstream", "lab_es_eventhubs"), ("Eventstream", "lab_es_custom"),
                            ("KQLDatabase", LAB_DB)):
        item = find_item(ws, item_type, name)
        if item:
            fabric("delete", f"workspaces/{ws}/items/{item['id']}")
            log(f"deleted {name}")
    for c in fabric("get", "connections").get("value", []):
        if c.get("displayName") == CONNECTION:
            fabric("delete", f"connections/{c['id']}")
            log(f"deleted connection {CONNECTION}")
    set_env_value("ESTREAM_CONNECTION_STRING", "")


def set_running(running: bool) -> None:
    """Pause/resume the lab eventstreams (a running processor costs ~1 CU each, ~50 % of an F2)."""
    s = load_state("rti")
    for name, es in s.get("eventstreams", {}).items():
        fabric("post", f"workspaces/{s['workspace_id']}/eventstreams/{es}/{'resume' if running else 'pause'}")
        log(f"{'resumed' if running else 'paused'} {name}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "info", "pause", "resume", "delete-lab"])
    p.add_argument("--force", action="store_true", help="re-apply eventstream definitions")
    a = p.parse_args()
    load_env()
    require_tool("az", "az login (same user as fab auth login)")
    if a.action == "create":
        create(a.force)
    elif a.action == "delete-lab":
        delete_lab()
    elif a.action in ("pause", "resume"):
        set_running(a.action == "resume")
    print(json.dumps(load_state("rti"), indent=2))


if __name__ == "__main__":
    main()
