"""Isolated observability lab on a separate capacity (steps 10 + 12). Idempotent. Never touches dev/prod items.

  py infra/obs_lab.py workspaces   # ce-obs-{data,consumer,audit,ops} on the obs capacity (capacity.py --target obs) + roles
  py infra/obs_lab.py data         # lh_obs_finance: finance gold + silver_customers (Load-to-Table from a one-time CSV export)
                                   # + OneLake role FinanceViewerNoPII (no SQL DENYs: endpoint behaviour is observed as-is)
  py infra/obs_lab.py consumer     # lh_obs_hub (cross-workspace shortcuts) + da_obs_finance + nb_obs_reader
  py infra/obs_lab.py audit        # lh_obs_diag + OneLake diagnostics (data, consumer, ops) + workspace monitoring (Auto+Linked)
  py infra/obs_lab.py ops          # eh_obs: obs_fleet (eh_fleet KQL) + obs_monitor (cap_monitoring KQL), es_obs_fleet,
                                   # es_obs_capacity, dims, Activator clones act_obs_* (script-managed)
  py infra/obs_lab.py sqlaudit     # capability test: SQL audit on the lh_obs_finance SQL endpoint (control event)
  py infra/obs_lab.py status

Roles (paired identities, plan review #8/#9):
  data/consumer: admins = Admin; Viewers group (test user) = Viewer AND FinanceViewerNoPII member;
                 deploy SP = Viewer but NOT a role member (negative control)
  audit/ops:     admins only (diagnostic logs contain user identifiers)
Nothing here is Git-synced or deployed: tenant-bound lab items, re-created by this script (D-041).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import decimal
import io
import json
import time
import uuid

from common import ROOT, env, load_env, load_state, log, run, save_state
from fabric_rest import FABRIC, _request, b64, fabric, find_item, kql, tsql, upsert_item
from finance_lakehouse import lakehouse, refresh_sql_endpoint, shortcut, ws as main_ws
from onelake import upload
from rti_lab import custom_endpoint_connection_string, platform

KINDS = ("data", "consumer", "audit", "ops")
DATA = ROOT / "data-gen" / "out"
EXPORT = ["gold_dim_customer", "gold_dim_contract", "gold_fact_contract_month", "gold_dim_date", "silver_customers"]
GOLD = [t for t in EXPORT if t.startswith("gold_")]
PII = {"customer_name", "email", "phone", "first_name", "last_name", "street_address"}
ROLE = "FinanceViewerNoPII"


def name(kind: str) -> str:
    return f"{env('WORKSPACE_PREFIX')}-obs-{kind}"


def state() -> dict:
    return load_state("obs")


def wid(kind: str) -> str:
    return state()["workspaces"][kind]


def obs_capacity_id() -> str:
    from capacity import capacity_name
    cap_name, _ = capacity_name("obs")
    for c in fabric("get", "capacities")["value"]:
        if c["displayName"] == cap_name:
            if c["state"] != "Active":
                raise SystemExit("obs capacity is paused: py infra/capacity.py resume --target obs")
            return c["id"]
    raise SystemExit("obs capacity not found: py infra/capacity.py create --target obs")


def group_object_id(env_key: str) -> str:
    """Resolve via Fabric (dev workspace role assignments) - avoids az ad / Graph CAE re-auth prompts."""
    target = env(env_key)
    for ra in fabric("get", f"workspaces/{main_ws('finance', 'dev')}/roleAssignments")["value"]:
        if ra["principal"]["type"] == "Group" and ra["principal"].get("displayName") == target:
            return ra["principal"]["id"]
    raise SystemExit(f"group {env_key} has no role on ce-finance-dev")


# ------------------------------------------------------------------------------------------------ workspaces
def workspaces() -> None:
    cap = obs_capacity_id()
    existing = {w["displayName"]: w for w in fabric("get", "workspaces")["value"]}
    ids = {}
    for k in KINDS:
        n = name(k)
        if n in existing:
            ids[k] = existing[n]["id"]
            if (existing[n].get("capacityId") or "").lower() != cap.lower():
                fabric("post", f"workspaces/{ids[k]}/assignToCapacity", {"capacityId": cap})
        else:
            ids[k] = fabric("post", "workspaces", {"displayName": n, "capacityId": cap,
                                                   "description": f"Contoso Energy observability lab ({k}), isolated capacity"})["id"]
            log(f"workspace created: {n}")
    save_state("obs", {**state(), "workspaces": ids})
    admins, viewers = group_object_id("ENTRA_GROUP_ADMINS"), group_object_id("ENTRA_GROUP_VIEWERS")
    sp = env("SP_OBJECT_ID")
    wanted = {k: [(admins, "Group", "Admin")] for k in KINDS}
    tu = env("TEST_USER_OBJECT_ID", required=False)
    for k in ("data", "consumer"):
        wanted[k] += [(viewers, "Group", "Viewer"), (sp, "ServicePrincipal", "Viewer")]
        if tu:  # direct as well: group-based access for the new test user never resolved in Fabric (finding, step 10)
            wanted[k].append((tu, "User", "Viewer"))
    for k, roles in wanted.items():
        current = {r["principal"]["id"]: r for r in fabric("get", f"workspaces/{ids[k]}/roleAssignments")["value"]}
        for pid, ptype, role in roles:
            if pid not in current:
                fabric("post", f"workspaces/{ids[k]}/roleAssignments", {"principal": {"id": pid, "type": ptype}, "role": role})
        log(f"{name(k)}: roles ok ({', '.join(r for _, _, r in roles)})")


# ------------------------------------------------------------------------------------------------ data
def _csv(rows: list[tuple], cols: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow(["" if v is None else (str(v) if not isinstance(v, (dt.date, decimal.Decimal)) else str(v)) for v in r])
    return buf.getvalue().encode("utf-8")


def data() -> None:
    dw = wid("data")
    lh = lakehouse(dw, "lh_obs_finance", "Obs lab: finance gold + PII silver (copy of dev, synthetic)")
    src_ws = main_ws("finance", "dev")
    src = lakehouse(src_ws, "lh_finance", "")
    for t in EXPORT:  # one-time small read on the shared capacity; everything after runs on the obs capacity
        cols = [r[0] for r in tsql(src["sql_host"], "lh_finance", f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                                                                  f"WHERE TABLE_NAME = '{t}' ORDER BY ORDINAL_POSITION")
                if not r[0].startswith("_")]
        rows = tsql(src["sql_host"], "lh_finance", f"SELECT {', '.join(f'[{c}]' for c in cols)} FROM dbo.{t}")
        upload(dw, lh["id"], f"Files/landing/{t}.csv", _csv(rows, cols))
        fabric("post", f"workspaces/{dw}/lakehouses/{lh['id']}/tables/{t}/load", {
            "relativePath": f"Files/landing/{t}.csv", "pathType": "File", "mode": "Overwrite", "recursive": False,
            "formatOptions": {"format": "Csv", "header": True, "delimiter": ","}})
        log(f"lh_obs_finance.{t}: {len(rows)} rows")
    refresh_sql_endpoint(dw, "lh_obs_finance")
    got = {r[0]: r[1] for r in tsql(lh["sql_host"], "lh_obs_finance",
                                    "SELECT t.name, SUM(p.rows) FROM sys.tables t JOIN sys.partitions p ON p.object_id = t.object_id "
                                    "AND p.index_id IN (0,1) GROUP BY t.name")}
    missing = [t for t in EXPORT if t not in got]
    log(f"SQL endpoint tables: {got}" + (f" MISSING {missing}" if missing else ""))
    if missing:
        raise SystemExit("tables not registered in the SQL endpoint")
    role(dw, lh)


def role(dw: str, lh: dict) -> None:
    cols = [r[0] for r in tsql(lh["sql_host"], "lh_obs_finance", "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                                                                  "WHERE TABLE_NAME = 'gold_dim_customer'")]
    members = [{"tenantId": env("TENANT_ID"), "objectId": group_object_id("ENTRA_GROUP_VIEWERS")}]
    if env("TEST_USER_OBJECT_ID", required=False):
        members.append({"tenantId": env("TENANT_ID"), "objectId": env("TEST_USER_OBJECT_ID")})
    body = {"value": [{"name": ROLE, "decisionRules": [{"effect": "Permit", "permission": [
        {"attributeName": "Path", "attributeValueIncludedIn": [f"/Tables/{t}" for t in GOLD]},
        {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]}],
        "constraints": {
            "columns": [{"tablePath": "/Tables/gold_dim_customer", "columnNames": [c for c in cols if c not in PII],
                         "columnEffect": "Permit", "columnAction": ["Read"]}],
            "rows": [{"tablePath": "/Tables/gold_dim_contract",
                      "value": "SELECT * FROM gold_dim_contract WHERE site_region = 'Coastal'"}]}}],
        "members": {"microsoftEntraMembers": members}}]}
    # DefaultReader is intentionally NOT kept: only role members (and workspace Contributor+) can read (negative control)
    status, _, r = _request("put", f"{FABRIC}/v1/workspaces/{dw}/items/{lh['id']}/dataAccessRoles", body, FABRIC)
    if status not in (200, 201):
        raise SystemExit(f"dataAccessRoles {status}: {json.dumps(r)[:500]}")
    log(f"lh_obs_finance: {ROLE} (gold only, CLS no PII, RLS Coastal; members = Viewers group); DefaultReader removed")


# ------------------------------------------------------------------------------------------------ consumer
NB = "nb_obs_reader"
NB_SOURCE = """# Obs lab workload probe: Spark reads through a cross-workspace shortcut and directly from the source lakehouse.
# Runs as the identity that starts the job (step 10 identity matrix).
import time
src = "abfss://{data_ws}@onelake.dfs.fabric.microsoft.com/{data_lh}/Tables"
res = {{}}
for label, path in [("shortcut fin_contracts", "Tables/fin_contracts"), ("direct gold_dim_contract", f"{{src}}/gold_dim_contract"),
                    ("direct silver_customers (out of role)", f"{{src}}/silver_customers")]:
    try:
        res[label] = spark.read.format("delta").load(path).count()
    except Exception as e:
        res[label] = "ERROR " + str(e).splitlines()[0][:150]
print(res)
mssparkutils.notebook.exit(str(res))
"""


def consumer() -> None:
    cw, dw = wid("consumer"), wid("data")
    hub = lakehouse(cw, "lh_obs_hub", "Obs lab: consumer lakehouse with cross-workspace shortcuts to lh_obs_finance")
    src = find_item(dw, "Lakehouse", "lh_obs_finance")
    for sc, table in (("fin_customers", "gold_dim_customer"), ("fin_contracts", "gold_dim_contract"),
                      ("fin_silver_customers", "silver_customers")):
        shortcut(cw, hub["id"], "Tables", sc, {"oneLake": {"workspaceId": dw, "itemId": src["id"], "path": f"Tables/{table}"}})
    refresh_sql_endpoint(cw, "lh_obs_hub")
    agent(dw, src)
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {
        "kernel_info": {"name": "synapse_pyspark"}, "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
        "language_info": {"name": "python"},
        "dependencies": {"lakehouse": {"default_lakehouse": hub["id"], "default_lakehouse_name": "lh_obs_hub",
                                       "default_lakehouse_workspace_id": cw}}},
        "cells": [{"cell_type": "code", "metadata": {}, "outputs": [], "execution_count": None,
                   "source": NB_SOURCE.format(data_ws=dw, data_lh=src["id"]).splitlines(keepends=True)}]}
    upsert_item(cw, "Notebook", NB, [{"path": "notebook-content.ipynb", "payload": b64(nb), "payloadType": "InlineBase64"},
                                     {"path": ".platform", "payload": b64(platform("Notebook", NB)), "payloadType": "InlineBase64"}],
                "Obs lab: Spark workload probe", definition_format="ipynb")
    log(f"{NB}: ready")


def agent(dw: str, lh: dict) -> None:
    """da_obs_finance lives in the DATA workspace next to its source (callers need item access to query it)."""
    from data_agents import eid, stage
    host = fabric("get", f"workspaces/{dw}/lakehouses/{lh['id']}")["properties"]["sqlEndpointProperties"]["connectionString"]
    cols: dict[str, list[tuple[str, str]]] = {}
    for t, c, typ in tsql(host, "lh_obs_finance", "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                                                  "WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME LIKE 'gold%' ORDER BY TABLE_NAME, ORDINAL_POSITION"):
        cols.setdefault(t, []).append((c, typ))
    tables = [{"id": eid("obs", t), "display_name": t, "type": "lakehouse_tables.table", "is_selected": True,
               "children": [{"id": eid("obs", t, c), "display_name": c, "type": "lakehouse_tables.column", "data_type": typ,
                             "is_selected": c not in PII} for c, typ in cs]} for t, cs in cols.items()]
    ds = {"$schema": "1.0.0", "artifactId": lh["id"], "workspaceId": dw, "displayName": "lh_obs_finance", "type": "lakehouse_tables",
          "userDescription": "Contoso financing gold (obs lab copy)", "dataSourceInstructions":
          "Collection rate = SUM(amount_paid)/SUM(amount_due) from gold_fact_contract_month joined to gold_dim_contract on contract_id.",
          "elements": [{"id": eid("obs", "dbo"), "display_name": "dbo", "type": "lakehouse_tables.schema", "is_selected": True,
                        "children": tables}]}
    instr = "You are the Contoso financing assistant (synthetic data). Never reveal customer names, emails or phone numbers."
    parts = [{"path": "Files/Config/data_agent.json", "payload": b64({"$schema": "2.1.0"}), "payloadType": "InlineBase64"},
             {"path": "Files/Config/publish_info.json", "payload": b64({"$schema": "1.0.0", "description": "obs lab"}),
              "payloadType": "InlineBase64"}]
    for st in ("draft", "published"):
        parts += [{"path": f"Files/Config/{st}/stage_config.json", "payload": b64(stage(instr)), "payloadType": "InlineBase64"},
                  {"path": f"Files/Config/{st}/lakehouse_tables-lh_obs_finance/datasource.json", "payload": b64(ds),
                   "payloadType": "InlineBase64"}]
    parts.append({"path": ".platform", "payload": b64(platform("DataAgent", "da_obs_finance")), "payloadType": "InlineBase64"})
    upsert_item(dw, "DataAgent", "da_obs_finance", parts, "Obs lab finance agent")
    log("da_obs_finance: published (PII columns deselected)")


# ------------------------------------------------------------------------------------------------ audit
def audit() -> None:
    aw = wid("audit")
    diag = lakehouse(aw, "lh_obs_diag", "Obs lab: OneLake diagnostics destination (admin-only workspace)")
    for k in ("data", "consumer", "ops"):
        w = wid(k)
        cur = fabric("get", f"workspaces/{w}/onelake/settings").get("diagnostics", {})
        if cur.get("status") == "Enabled" and (cur.get("destination") or {}).get("lakehouse", {}).get("itemId") == diag["id"]:
            continue
        status, _, r = _request("post", f"{FABRIC}/v1/workspaces/{w}/onelake/settings/modifyDiagnostics", {
            "status": "Enabled", "destination": {"type": "Lakehouse", "lakehouse": {
                "referenceType": "ById", "itemId": diag["id"], "workspaceId": aw}}}, FABRIC)
        if status not in (200, 202):
            raise SystemExit(f"{name(k)}: modifyDiagnostics {status} {json.dumps(r)[:300]}")
        log(f"{name(k)}: OneLake diagnostics -> lh_obs_diag")
    save_state("obs", {**state(), "diag_enabled_utc": state().get("diag_enabled_utc") or
                       dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    from workspace_monitoring import SCHEMA, enable
    owner = _monitoring(aw, "mon_obs_audit", {"mode": "Auto"}, SCHEMA)
    log(f"mon_obs_audit: {enable(aw, owner)} (Auto)")
    for k in ("data", "consumer", "ops"):
        item = _monitoring(wid(k), f"mon_obs_{k}", {"mode": "Linked", "parentMonitoringItem": {"itemId": owner["id"],
                                                                                              "workspaceId": aw}}, SCHEMA)
        log(f"mon_obs_{k}: {enable(wid(k), item)} (Linked)")


def _monitoring(w: str, n: str, destination: dict, schema: str) -> dict:
    items = fabric("get", f"workspaces/{w}/monitoringItems").get("value", [])
    item = items[0] if items else fabric("post", f"workspaces/{w}/monitoringItems", {"displayName": n})
    if not items:
        item = fabric("get", f"workspaces/{w}/monitoringItems")["value"][0]
    props = {"$schema": schema, "dataCollectionSettings": [{"sources": {"workspaceDiagnostics": {"mode": "All"}},
                                                            "destinations": {"eventhouse": destination}}]}
    _request("post", f"{FABRIC}/v1/workspaces/{w}/items/{item['id']}/updateDefinition",
             {"definition": {"format": "MonitoringV1", "parts": [{"path": "MonitoringProperties.json", "payload": b64(props),
                                                                  "payloadType": "InlineBase64"}]}}, FABRIC)
    return item


# ------------------------------------------------------------------------------------------------ SQL audit capability
def sqlaudit() -> None:
    dw = wid("data")
    lh = lakehouse(dw, "lh_obs_finance", "")
    sep = find_item(dw, "SQLEndpoint", "lh_obs_finance")
    body = {"state": "Enabled", "retentionDays": 30,
            "auditActionsAndGroups": ["SUCCESSFUL_DATABASE_AUTHENTICATION_GROUP", "FAILED_DATABASE_AUTHENTICATION_GROUP",
                                      "BATCH_COMPLETED_GROUP"]}
    result = {}
    for path in (f"sqlEndpoints/{sep['id']}/settings/sqlAudit", f"warehouses/{sep['id']}/settings/sqlAudit"):
        status, _, r = _request("patch", f"{FABRIC}/v1/workspaces/{dw}/{path}", body, FABRIC)
        result[path.split("/")[0]] = f"{status} {json.dumps(r)[:160]}"
        if status in (200, 202):
            break
    log(f"sqlAudit enable: {result}")
    marker = f"obs-audit-control-{uuid.uuid4().hex[:8]}"
    tsql(lh["sql_host"], "lh_obs_finance", f"SELECT '{marker}' AS control")
    save_state("obs", {**state(), "sqlaudit": {"enable": result, "control": marker,
                                               "at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}})
    log(f"control query sent ({marker}); read back later with `obs_lab.py sqlaudit-read`")


def sqlaudit_read() -> None:
    dw = wid("data")
    lh = lakehouse(dw, "lh_obs_finance", "")
    s = state().get("sqlaudit", {})
    try:
        rows = tsql(lh["sql_host"], "lh_obs_finance", "SELECT TOP 20 event_time, action_id, succeeded, LEFT(statement, 80) "
                                                      "FROM sys.fn_get_audit_file_v2('https://onelake.blob.fabric.microsoft.com/"
                                                      f"{dw}/{find_item(dw, 'SQLEndpoint', 'lh_obs_finance')['id']}/Audit/sqldbauditlogs/',"
                                                      " default, default, default, default) WHERE statement LIKE '%obs-audit-control%'")
        found = any(s.get("control", "?") in str(r[3]) for r in rows)
        log(f"SQL audit: {len(rows)} control-like rows; control {'FOUND' if found else 'not found'}")
    except Exception as ex:  # noqa: BLE001
        log(f"SQL audit read failed: {str(ex).splitlines()[0][:200]}")


# ------------------------------------------------------------------------------------------------ ops (plan B base)
def kql_db(w: str, eh_id: str, db_name: str, script: str) -> dict:
    parts = [{"path": "DatabaseProperties.json", "payloadType": "InlineBase64",
              "payload": b64({"databaseType": "ReadWrite", "parentEventhouseItemId": eh_id,
                              "oneLakeCachingPeriod": "P7D", "oneLakeStandardStoragePeriod": "P30D"})},
             {"path": "DatabaseSchema.kql", "payload": b64(script), "payloadType": "InlineBase64"},
             {"path": ".platform", "payload": b64(platform("KQLDatabase", db_name)), "payloadType": "InlineBase64"}]
    return upsert_item(w, "KQLDatabase", db_name, parts, f"Obs lab: {db_name}")


def eventstream(w: str, es_name: str, source: dict, db: dict, table: str, mapping: str, update: bool = False) -> dict:
    stream = f"{es_name}-stream"
    topo = {"sources": [source],
            "destinations": [{"name": "raw", "type": "Eventhouse", "properties": {
                "dataIngestionMode": "DirectIngestion", "workspaceId": w, "itemId": db["id"], "tableName": table,
                "connectionName": f"{es_name}_raw", "mappingRuleName": mapping}, "inputNodes": [{"name": stream}]}],
            "streams": [{"name": stream, "type": "DefaultStream", "properties": {}, "inputNodes": [{"name": source["name"]}]}],
            "operators": [], "compatibilityLevel": "1.0"}
    es = upsert_item(w, "Eventstream", es_name, [
        {"path": "eventstream.json", "payload": b64(topo), "payloadType": "InlineBase64"},
        {"path": "eventstreamProperties.json", "payloadType": "InlineBase64",
         "payload": b64({"retentionTimeInDays": 1, "eventThroughputLevel": "Low"})},
        {"path": ".platform", "payload": b64(platform("Eventstream", es_name)), "payloadType": "InlineBase64"}],
        f"Obs lab: {es_name}", update=update)
    for _ in range(40):
        t = fabric("get", f"workspaces/{w}/eventstreams/{es['id']}/topology")
        st = [n.get("status") for n in t["sources"] + t["destinations"]]
        if all(s == "Running" for s in st) or "Failed" in st:
            break
        time.sleep(15)
    log(f"{es_name}: {st}")
    if "Failed" in st:
        raise SystemExit(json.dumps(t)[:1200])
    return es


def ops() -> None:
    from capacity_monitoring import script as cap_script
    from fleet_medallion import script as fleet_script
    from fabric_setup import capacity_id  # noqa: F401  (main capacity helper; obs uses obs_capacity_id)
    ow = wid("ops")
    eh = upsert_item(ow, "Eventhouse", "eh_obs", None, "Obs lab eventhouse (isolated capacity)")
    uri = fabric("get", f"workspaces/{ow}/eventhouses/{eh['id']}")["properties"]["queryServiceUri"]
    fleet = kql_db(ow, eh["id"], "obs_fleet", fleet_script())
    mon = kql_db(ow, eh["id"], "obs_monitor", cap_script())
    # dims (asset master from the local generator output)
    for table, f in (("dim_site", "sites"), ("dim_device", "devices"), ("dim_battery", "batteries")):
        rows = (DATA / "asset" / f"{f}.csv").read_text(encoding="utf-8").splitlines()[1:]
        kql(uri, fleet["id"], f".clear table {table} data", mgmt=True)
        for i in range(0, len(rows), 500):
            kql(uri, fleet["id"], f".ingest inline into table {table} with (format='csv') <|\n" + "\n".join(rows[i:i + 500]), mgmt=True)
    log("obs_fleet: dims loaded")
    es = eventstream(ow, "es_obs_fleet", {"name": "devices", "type": "CustomEndpoint", "properties": {}}, fleet,
                     "bronze_events", "bronze_events_json")
    from common import set_env_value
    set_env_value("ESTREAM_OBS_FLEET_CONNECTION_STRING", custom_endpoint_connection_string(ow, es["id"]))
    eventstream(ow, "es_obs_capacity", {"name": "capacity", "type": "FabricCapacityOverviewEvents", "properties": {
        "eventScope": "Capacity", "capacityId": obs_capacity_id(),
        "includedEventTypes": ["Microsoft.Fabric.Capacity.State", "Microsoft.Fabric.Capacity.Summary"], "filters": []}},
        mon, "capacity_events_raw", "capacity_events_raw_json")
    save_state("obs", {**state(), "eh_obs": eh["id"], "query_uri": uri, "obs_fleet": fleet["id"], "obs_monitor": mon["id"]})
    activators()


def activators() -> None:
    """Clones of the dev fleet Activators on obs_fleet (same queries/rules) for the Step 12 comparison."""
    from rti_alerts import ACTIVATORS, activator_entities
    ow, s = wid("ops"), state()
    for dev_name, spec in ACTIVATORS.items():
        n = dev_name.replace("act_fleet_", "act_obs_")
        kql(s["query_uri"], s["obs_fleet"], spec["query"].replace("startTime", "ago(1d)").replace("endTime", "now()"))
        ents = activator_entities(n, ow, s["obs_fleet"], spec["query"], spec["time_field"], spec["detect"](),
                                  env("ALERT_EMAIL", required=False) or env("ADMIN_UPN"), "[obs] " + spec["subject"],
                                  spec["message"])
        parts = [{"path": "ReflexEntities.json", "payload": b64(ents), "payloadType": "InlineBase64"}]
        item = find_item(ow, "Reflex", n)
        if item:
            fabric("post", f"workspaces/{ow}/items/{item['id']}/updateDefinition", {"definition": {"parts": parts}})
        else:
            parts.append({"path": ".platform", "payload": b64(platform("Reflex", n)), "payloadType": "InlineBase64"})
            upsert_item(ow, "Reflex", n, parts, "[obs] " + spec["subject"])
        log(f"{n}: deployed")


# ------------------------------------------------------------------------------------------------ phase 2 (jobs)
def jobs() -> None:
    """pl_obs_daily (failure injection) + pl_obs_remediation, job events -> obs_monitor, and act_obs_job_autofix:
    an Activator that re-runs the remediation pipeline AUTOMATICALLY (contrast: operations agent asks for approval)."""
    ow, s = wid("ops"), state()
    daily = {"properties": {"description": "Obs lab daily job (step 12): failure_mode = none | fail",
                            "parameters": {"failure_mode": {"type": "string", "defaultValue": "none"}},
                            "activities": [{"name": "Inject failure", "type": "IfCondition", "dependsOn": [], "typeProperties": {
                                "expression": {"value": "@equals(pipeline().parameters.failure_mode, 'fail')", "type": "Expression"},
                                "ifTrueActivities": [{"name": "Fail - injected", "type": "Fail", "dependsOn": [], "typeProperties": {
                                    "message": "Injected failure (step 12): upstream file missing", "errorCode": "CE-OBS-012"}}],
                                "ifFalseActivities": [{"name": "Work", "type": "Wait", "dependsOn": [],
                                                       "typeProperties": {"waitTimeInSeconds": 5}}]}}]}}
    remediation = {"properties": {"description": "Obs lab remediation (step 12): stands in for a re-run/backfill",
                                  "parameters": {"reason": {"type": "string", "defaultValue": "manual"}},
                                  "activities": [{"name": "Remediate", "type": "Wait", "dependsOn": [],
                                                  "typeProperties": {"waitTimeInSeconds": 5}}]}}
    items = {}
    for n, content in (("pl_obs_daily", daily), ("pl_obs_remediation", remediation)):
        items[n] = upsert_item(ow, "DataPipeline", n, [
            {"path": "pipeline-content.json", "payload": b64(content), "payloadType": "InlineBase64"},
            {"path": ".platform", "payload": b64(platform("DataPipeline", n)), "payloadType": "InlineBase64"}], f"Obs lab: {n}")
    mon = find_item(ow, "KQLDatabase", "obs_monitor")
    events = ["Microsoft.Fabric.JobEvents.ItemJobCreated", "Microsoft.Fabric.JobEvents.ItemJobStatusChanged",
              "Microsoft.Fabric.JobEvents.ItemJobSucceeded", "Microsoft.Fabric.JobEvents.ItemJobFailed"]
    sources = [{"name": f"jobs-{n.replace('_', '-')}", "type": "FabricJobEvents", "properties": {
        "eventScope": "Item", "workspaceId": ow, "itemId": it["id"], "includedEventTypes": events, "filters": []}}
        for n, it in items.items()]
    stream = "es_obs_jobs-stream"
    topo = {"sources": sources,
            "destinations": [{"name": "raw", "type": "Eventhouse", "properties": {
                "dataIngestionMode": "DirectIngestion", "workspaceId": ow, "itemId": mon["id"], "tableName": "job_events_raw",
                "connectionName": "es_obs_jobs_raw", "mappingRuleName": "job_events_raw_json"}, "inputNodes": [{"name": stream}]}],
            "streams": [{"name": stream, "type": "DefaultStream", "properties": {}, "inputNodes": [{"name": x["name"]} for x in sources]}],
            "operators": [], "compatibilityLevel": "1.0"}
    es = upsert_item(ow, "Eventstream", "es_obs_jobs", [
        {"path": "eventstream.json", "payload": b64(topo), "payloadType": "InlineBase64"},
        {"path": "eventstreamProperties.json", "payloadType": "InlineBase64",
         "payload": b64({"retentionTimeInDays": 1, "eventThroughputLevel": "Low"})},
        {"path": ".platform", "payload": b64(platform("Eventstream", "es_obs_jobs")), "payloadType": "InlineBase64"}],
        "Obs lab: job events (pl_obs_daily, pl_obs_remediation)")
    for _ in range(40):
        tp = fabric("get", f"workspaces/{ow}/eventstreams/{es['id']}/topology")
        st = [n.get("status") for n in tp["sources"] + tp["destinations"]]
        if all(x == "Running" for x in st) or "Failed" in st:
            break
        time.sleep(15)
    log(f"es_obs_jobs: {st}")
    autofix(ow, mon, items["pl_obs_remediation"])
    save_state("obs", {**state(), "pl_obs_daily": items["pl_obs_daily"]["id"],
                       "pl_obs_remediation": items["pl_obs_remediation"]["id"]})


def autofix(ow: str, mon: dict, target: dict) -> None:
    from rti_alerts import activator_entities
    n = "act_obs_job_autofix"
    q = ("job_events | where ingestion_time() between (startTime .. endTime) and event_type == 'ItemJobFailed' "
         "and item_name == 'pl_obs_daily' | project event_time, item_name, job_instance_id, job_status, duration_ms")
    detect = [{"name": "EventDetectStep", "id": str(uuid.uuid4()),
               "rows": [{"name": "OnEveryValue", "kind": "OnEveryValue", "arguments": []}]}]
    ents = activator_entities(n, ow, mon["id"], q, "event_time", detect, env("ADMIN_UPN"), "[obs] auto-remediation", "")
    container = next(e for e in ents if e["type"] == "container-v1")["uniqueIdentifier"]
    action_id = str(uuid.uuid4())
    ents.append({"uniqueIdentifier": action_id, "type": "fabricItemAction-v1", "payload": {
        "name": "Run pl_obs_remediation", "fabricItem": {"itemId": target["id"], "workspaceId": ow, "itemType": "Pipeline"},
        "jobType": "Pipeline", "parentContainer": {"targetUniqueIdentifier": container}}})
    rule = next(e for e in ents if e["type"] == "timeSeriesView-v1" and e["payload"]["definition"]["type"] == "Rule")
    inst = json.loads(rule["payload"]["definition"]["instance"])
    act = next(s for s in inst["steps"] if s["name"] == "ActStep")
    act["rows"] = [{"name": "FabricItemBinding", "kind": "FabricItemInvocation", "arguments": [
        {"name": "workspaceId", "type": "string", "value": ow},
        {"name": "itemId", "type": "string", "value": target["id"]},
        {"name": "itemType", "type": "string", "value": "Pipeline"},
        {"name": "jobType", "type": "string", "value": "Pipeline"},
        {"name": "fabricJobConnectionDocumentId", "type": "string", "value": action_id},
        {"name": "additionalInformation", "type": "array", "values": []},
        {"name": "parameters", "type": "array", "values": []}]}]
    rule["payload"]["definition"]["instance"] = json.dumps(inst, separators=(",", ":"))
    parts = [{"path": "ReflexEntities.json", "payload": b64(ents), "payloadType": "InlineBase64"}]
    item = find_item(ow, "Reflex", n)
    if item:
        fabric("post", f"workspaces/{ow}/items/{item['id']}/updateDefinition", {"definition": {"parts": parts}})
    else:
        parts.append({"path": ".platform", "payload": b64(platform("Reflex", n)), "payloadType": "InlineBase64"})
        upsert_item(ow, "Reflex", n, parts, "[obs] auto-remediation: failed pl_obs_daily -> run pl_obs_remediation")
    log(f"{n}: deployed (ItemJobFailed on pl_obs_daily -> run pl_obs_remediation, no approval)")


def run_job(n: str, params: dict | None = None) -> str:
    ow = wid("ops")
    it = find_item(ow, "DataPipeline", n)
    status, headers, body = _request("post", f"{FABRIC}/v1/workspaces/{ow}/items/{it['id']}/jobs/instances?jobType=Pipeline",
                                     {"executionData": {"parameters": params or {}}}, FABRIC)
    if status != 202:
        raise SystemExit(f"{n}: {status} {body}")
    return headers["Location"].rstrip("/").split("/")[-1]


def status() -> None:
    s = state()
    for k in KINDS:
        items = fabric("get", f"workspaces/{s['workspaces'][k]}/items")["value"]
        print(f"{name(k)}: {sorted((i['type'], i['displayName']) for i in items)}")


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["workspaces", "data", "consumer", "audit", "ops", "activators", "jobs", "sqlaudit",
                                      "sqlaudit-read", "status", "all"])
    a = p.parse_args()
    load_env()
    steps = {"workspaces": workspaces, "data": data, "consumer": consumer, "audit": audit, "ops": ops,
             "activators": activators, "jobs": jobs, "sqlaudit": sqlaudit, "sqlaudit-read": sqlaudit_read, "status": status}
    if a.action == "all":
        for k in ("workspaces", "data", "consumer", "audit", "sqlaudit", "ops"):
            steps[k]()
    else:
        steps[a.action]()


if __name__ == "__main__":
    main()
