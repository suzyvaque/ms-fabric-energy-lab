"""Step 6 option (ii): capacity monitoring on Real-Time Intelligence. Idempotent. Dev only (D-027).

  py infra/capacity_monitoring.py db           # KQL database cap_monitoring in the eh_fleet eventhouse (infra/kql/cap_monitoring)
  py infra/capacity_monitoring.py eventstream  # cap_es_capacity: Fabric capacity overview events -> capacity_events_raw
  py infra/capacity_monitoring.py peek         # latest raw events (schema discovery)
  py infra/capacity_monitoring.py dashboard    # cap_rtd_capacity (Real-Time Dashboard)
  py infra/capacity_monitoring.py activator    # act_capacity_70 (email when the capacity crosses 70 %; not in Git, D-018)
  py infra/capacity_monitoring.py verify

One capacity serves dev and prod, so its telemetry is collected once (cap_* items are Git-tracked from ce-fleet-dev
but excluded from the prod deploy). It shares the eh_fleet eventhouse to avoid another always-on eventhouse on F2.
"""
import argparse
import json
import time

from common import ROOT, load_env, log
from fabric_rest import b64, fabric, find_item, kql, upsert_item
from common import env
from rti_alerts import activator_entities, gid
from fabric_setup import capacity_id
from rti_lab import platform, ws_id

DB = "cap_monitoring"
ES = "cap_es_capacity"
KQL_DIR = ROOT / "infra" / "kql" / DB


def script() -> str:
    return "\n\n".join(p.read_text(encoding="utf-8").strip() for p in sorted(KQL_DIR.glob("*.kql"))) + "\n"


def eventhouse(ws: str) -> tuple[dict, str]:
    eh = find_item(ws, "Eventhouse", "eh_fleet")
    return eh, fabric("get", f"workspaces/{ws}/eventhouses/{eh['id']}")["properties"]["queryServiceUri"]


def db() -> dict:
    ws = ws_id()
    eh, _ = eventhouse(ws)
    parts = [{"path": "DatabaseProperties.json", "payloadType": "InlineBase64",
              "payload": b64({"databaseType": "ReadWrite", "parentEventhouseItemId": eh["id"],
                              "oneLakeCachingPeriod": "P7D", "oneLakeStandardStoragePeriod": "P30D"})},
             {"path": "DatabaseSchema.kql", "payload": b64(script()), "payloadType": "InlineBase64"},
             {"path": ".platform", "payload": b64(platform("KQLDatabase", DB)), "payloadType": "InlineBase64"}]
    item = upsert_item(ws, "KQLDatabase", DB, parts, "Step 6: Fabric capacity telemetry (capacity events, load tests)")
    log(f"{DB}: {item['id']} ({len(list(KQL_DIR.glob('*.kql')))} KQL files)")
    return item


def eventstream(force: bool = False) -> None:
    ws = ws_id()
    database = find_item(ws, "KQLDatabase", DB)
    stream = f"{ES}-stream"
    topology = {
        "sources": [{"name": "capacity", "type": "FabricCapacityOverviewEvents", "properties": {
            "eventScope": "Capacity", "capacityId": capacity_id(),
            "includedEventTypes": ["Microsoft.Fabric.Capacity.State", "Microsoft.Fabric.Capacity.Summary"],
            "filters": []}}],
        "destinations": [{"name": "raw", "type": "Eventhouse", "properties": {
            "dataIngestionMode": "DirectIngestion", "workspaceId": ws, "itemId": database["id"],
            "tableName": "capacity_events_raw", "connectionName": "cap_es_capacity_raw",
            "mappingRuleName": "capacity_events_raw_json"},
            "inputNodes": [{"name": stream}]}],
        "streams": [{"name": stream, "type": "DefaultStream", "properties": {}, "inputNodes": [{"name": "capacity"}]}],
        "operators": [], "compatibilityLevel": "1.0"}
    parts = [{"path": "eventstream.json", "payload": b64(topology), "payloadType": "InlineBase64"},
             {"path": "eventstreamProperties.json", "payloadType": "InlineBase64",
              "payload": b64({"retentionTimeInDays": 1, "eventThroughputLevel": "Low"})},
             {"path": ".platform", "payload": b64(platform("Eventstream", ES)), "payloadType": "InlineBase64"}]
    es = upsert_item(ws, "Eventstream", ES, parts, "Step 6: Fabric capacity overview events -> cap_monitoring",
                     update=force)
    for _ in range(40):
        topo = fabric("get", f"workspaces/{ws}/eventstreams/{es['id']}/topology")
        status = [n.get("status") for n in topo["sources"] + topo["destinations"]]
        if all(s == "Running" for s in status) or "Failed" in status:
            break
        time.sleep(15)
    log(f"{ES} source/destination status: {status}")
    if "Failed" in status:
        raise SystemExit(json.dumps(topo)[:1500])


def query(q: str, mgmt: bool = False) -> list[dict]:
    ws = ws_id()
    _, uri = eventhouse(ws)
    return kql(uri, find_item(ws, "KQLDatabase", DB)["id"], q, mgmt=mgmt)


def peek() -> None:
    rows = query("capacity_events_raw | extend t = tostring(event.type) | summarize n = count(), last = take_any(event) by t")
    for r in rows:
        print(r["t"], r["n"], json.dumps(r["last"])[:2500])


TILES = [  # (id, title, visual, x, y, w, h, query)
    ("util_now", "Billed utilization now (%)", "card", 0, 0, 6, 4,
     "capacity_utilization | top 1 by window_start | project value = utilization_pct"),
    ("throttle_now", "Throttling stage now (% of limit)", "card", 6, 0, 6, 4,
     "capacity_utilization | top 1 by window_start "
     "| project value = round(max_of(interactive_delay_pct, interactive_rejection_pct, background_rejection_pct), 1)"),
    ("overage_now", "Carry-forward overage (CU-s)", "card", 12, 0, 6, 4,
     "capacity_utilization | top 1 by window_start | project value = round(overage_total_cu_ms / 1000.0, 1)"),
    ("sku", "Capacity / SKU", "card", 18, 0, 6, 4,
     "capacity_utilization | top 1 by window_start | project value = strcat(capacity_name, ' / ', sku)"),
    ("util_trend", "Utilization vs 70 % alert line (5 min)", "timechart", 0, 4, 12, 7,
     "CapacityUtilization(30d, 5m) | where window_start between (_startTime .. _endTime) "
     "| project window_start, utilization_pct, max_utilization_pct, alert_line = 70.0 | order by window_start asc"),
    ("throttle_trend", "Throttling stages (% of each limit)", "timechart", 12, 4, 12, 7,
     "CapacityUtilization(30d, 5m) | where window_start between (_startTime .. _endTime) "
     "| project window_start, interactive_delay_pct, interactive_rejection_pct, background_rejection_pct "
     "| order by window_start asc"),
    ("by_workload", "Billed CU-seconds by workload", "bar", 0, 11, 12, 7,
     "capacity_workload | where window_start between (_startTime .. _endTime) "
     "| summarize cu_s = round(sum(background_cu_ms + interactive_cu_ms) / 1000.0, 1) by workload | order by cu_s desc"),
    ("workload_trend", "Workload CU-seconds per 5 min", "timechart", 12, 11, 12, 7,
     "capacity_workload | where window_start between (_startTime .. _endTime) "
     "| summarize cu_s = sum(background_cu_ms + interactive_cu_ms) / 1000.0 by workload, bin(window_start, 5m) "
     "| order by window_start asc"),
    ("crossings", "70 % crossings (alerts sent)", "table", 0, 18, 12, 6,
     "CapacityAlert70Crossings(70.0, 30d) | where window_start between (_startTime .. _endTime) | order by window_start desc"),
    ("state", "Capacity state events", "table", 12, 18, 12, 6,
     "capacity_state | where event_time between (_startTime .. _endTime) | project event_time, capacity_name, data "
     "| order by event_time desc"),
]


def dashboard() -> dict:
    ws = ws_id()
    _, uri = eventhouse(ws)
    database = find_item(ws, "KQLDatabase", DB)
    ds_id, page_id = gid("cap/rtd/ds"), gid("cap/rtd/page")
    queries, tiles = [], []
    for tid, title, visual, x, y, wd, h, q in TILES:
        qid = gid(f"cap/rtd/q/{tid}")
        used = [v for v in ("_startTime", "_endTime") if v in q]
        queries.append({"id": qid, "dataSource": {"kind": "inline", "dataSourceId": ds_id}, "text": q, "usedVariables": used})
        tiles.append({"id": gid(f"cap/rtd/t/{tid}"), "title": title, "visualType": visual, "pageId": page_id,
                      "layout": {"x": x, "y": y, "width": wd, "height": h},
                      "queryRef": {"kind": "query", "queryId": qid}, "visualOptions": {}})
    body = {
        "$schema": "https://dataexplorer.azure.com/static/d/schema/52/dashboard.json",
        "schema_version": "52", "title": "Contoso Energy - Capacity monitoring",
        "autoRefresh": {"enabled": True, "defaultInterval": "1m", "minInterval": "30s"},
        "baseQueries": [], "tiles": tiles, "queries": queries,
        "dataSources": [{"id": ds_id, "name": DB, "scopeId": "kusto-trident", "kind": "kusto-trident",
                         "clusterUri": uri, "database": database["id"], "workspace": ws}],
        "pages": [{"name": "Capacity", "id": page_id}],
        "parameters": [{"kind": "duration", "id": gid("cap/rtd/param/time"), "displayName": "Time range", "description": "",
                        "beginVariableName": "_startTime", "endVariableName": "_endTime",
                        "defaultValue": {"kind": "dynamic", "count": 24, "unit": "hours"}, "showOnPages": {"kind": "all"}}],
    }
    item = upsert_item(ws, "KQLDashboard", "cap_rtd_capacity", [
        {"path": "RealTimeDashboard.json", "payload": b64(body), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("KQLDashboard", "cap_rtd_capacity")), "payloadType": "InlineBase64"}],
        "Step 6: capacity utilization, throttling stages, workloads (capacity overview events)")
    bad = 0
    for tid, title, *_rest, q in TILES:
        try:
            n = len(kql(uri, database["id"], q.replace("_startTime", "ago(24h)").replace("_endTime", "now()")))
            log(f"tile ok   {title}: {n} rows")
        except SystemExit as ex:
            bad += 1
            log(f"tile FAIL {title}: {str(ex)[:200]}")
    if bad:
        raise SystemExit(f"{bad} tile query(ies) failed")
    return item


ACTIVATOR = "act_capacity_70"


def activator(threshold: float = 70.0) -> None:
    ws = ws_id()
    database = find_item(ws, "KQLDatabase", DB)
    q = (f"CapacityAlert70Crossings({threshold}, 2h) | where window_start between (startTime .. endTime) "
         "| project window_start, capacity_name, sku, level, utilization_pct, throttle_pct")
    query(q.replace("startTime", "ago(1d)").replace("endTime", "now()"))  # validation gate
    detect = [{"name": "EventDetectStep", "id": "00000000-0000-0000-0000-000000000070",
               "rows": [{"name": "OnEveryValue", "kind": "OnEveryValue", "arguments": []}]}]
    subject = f"Contoso capacity above {threshold:g} %"
    entities = activator_entities(ACTIVATOR, ws, database["id"], q, "window_start", detect,
                                  env("ALERT_EMAIL", required=False) or env("ADMIN_UPN"), subject,
                                  "Billed utilization or a throttling stage crossed the threshold. Open cap_rtd_capacity; "
                                  "consider pausing load, surge protection or a temporary resize.")
    parts = [{"path": "ReflexEntities.json", "payload": b64(entities), "payloadType": "InlineBase64"}]
    item = find_item(ws, "Reflex", ACTIVATOR)
    if item:
        fabric("post", f"workspaces/{ws}/items/{item['id']}/updateDefinition", {"definition": {"parts": parts}})
    else:
        parts.append({"path": ".platform", "payload": b64(platform("Reflex", ACTIVATOR)), "payloadType": "InlineBase64"})
        upsert_item(ws, "Reflex", ACTIVATOR, parts, subject)
    log(f"{ACTIVATOR}: deployed (fires once per upward crossing of {threshold:g} %)")


def verify() -> None:
    n = query("capacity_events_raw | where ingestion_time() > ago(10m) | count")[0]["Count"]
    u = query("capacity_utilization | top 1 by window_start")
    print(f"[{'PASS' if n >= 10 else 'FAIL'}] capacity events in last 10 min: {n} (expected ~20)")
    print(f"[{'PASS' if u else 'FAIL'}] latest window: {u[0]['window_start'] if u else '-'} "
          f"utilization {u[0]['utilization_pct'] if u else '-'} %")
    if n < 10 or not u:
        raise SystemExit("verification failed")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["db", "eventstream", "peek", "dashboard", "activator", "verify", "all"])
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    load_env()
    if a.action in ("db", "all"):
        db()
    if a.action in ("eventstream", "all"):
        eventstream(a.force)
    if a.action == "peek":
        peek()
    if a.action in ("dashboard", "all"):
        dashboard()
    if a.action in ("activator", "all"):
        activator()
    if a.action in ("verify", "all"):
        verify()


if __name__ == "__main__":
    main()
