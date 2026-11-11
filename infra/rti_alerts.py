"""Step 2: Real-Time Dashboard + Activator alerts on the Fleet API (ce-fleet-<env>). Idempotent.

  rtd_fleet_ops              Real-Time Dashboard (KQL dashboard) over eh_fleet: KPIs, PV trend, alerts, offline sites,
                             battery anomalies, ingestion health, site map. Git-tracked.
  act_fleet_device_faults    Activator: alert raised with severity != Low  -> email
  act_fleet_offline_sites    Activator: gateway silent for 20 min (fires once at the crossing) -> email
                             Activators hold recipient addresses (from .env), so they are script-managed and excluded
                             from Fabric Git commits (see plan/decision-log.md D-018).

  py infra/rti_alerts.py dashboard | activators | status | all   [--env dev]
"""
from __future__ import annotations

import argparse
import json
import uuid

from common import env, load_env, load_state, log
from fabric_rest import b64, fabric, find_item, kql, upsert_item
from rti_lab import platform

TEMPLATE_VERSION = "1.2.4"


def ws(e: str) -> str:
    return load_state("workspaces")[f"{env('WORKSPACE_PREFIX')}-fleet-{e}"]["id"]


def fleet_db(e: str) -> tuple[str, dict, str]:
    w = ws(e)
    eh = find_item(w, "Eventhouse", "eh_fleet")
    db = find_item(w, "KQLDatabase", "eh_fleet")
    uri = fabric("get", f"workspaces/{w}/eventhouses/{eh['id']}")["properties"]["queryServiceUri"]
    return w, db, uri


def gid(name: str) -> str:
    """Stable GUIDs so re-running the script updates instead of churning the definition."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"contoso-energy-lab/{name}"))


# ---------------------------------------------------------------- dashboard
TILES = [  # (id, title, visual, x, y, w, h, query)
    ("devices_online", "Devices online", "card", 0, 0, 4, 4,
     "FleetDeviceStatus(20m) | summarize value = countif(online)"),
    ("sites_offline", "Sites offline (20 min)", "card", 4, 0, 4, 4,
     "FleetOfflineSites(20m) | count | project value = Count"),
    ("faults", "Devices in fault", "card", 8, 0, 4, 4,
     "FleetDeviceStatus(20m) | where online | summarize value = countif(status == 'FAULT')"),
    ("open_alerts", "Open alerts", "card", 12, 0, 4, 4, "FleetOpenAlerts() | count | project value = Count"),
    ("fleet_pv", "Fleet PV now (kW)", "card", 16, 0, 4, 4,
     "FleetDeviceStatus(20m) | where online and device_type == 'inverter' | summarize value = round(sum(pv_kw), 1)"),
    ("pv_trend", "Fleet PV and load (kW)", "timechart", 0, 4, 12, 7,
     "silver_telemetry_dedup | where event_time between (_startTime .. _endTime) and device_type in ('inverter','meter') "
     "| summarize pv = avgif(pv_kw, device_type == 'inverter') * dcountif(device_id, device_type == 'inverter'), "
     "load = avgif(load_kw, device_type == 'meter') * dcountif(device_id, device_type == 'meter') by bin(event_time, 5m) "
     "| order by event_time asc"),
    ("ingest", "Ingestion: events/min and p95 arrival latency (s)", "timechart", 12, 4, 12, 7,
     "bronze_events | extend ingested = ingestion_time() | where ingested between (_startTime .. _endTime) "
     "| summarize events = count(), p95_arrival_s = percentile((ingested - sent_time) / 1s, 95) by bin(ingested, 1m) "
     "| order by ingested asc"),
    ("alerts", "Open alerts", "table", 0, 11, 12, 7,
     "FleetOpenAlerts() | lookup kind=leftouter (dim_site | project site_id, region) on site_id "
     "| project raised_at, severity, alert_code, site_id, region, device_id, message | order by raised_at desc"),
    ("offline", "Offline sites", "table", 12, 11, 12, 7,
     "FleetOfflineSites(20m) | project site_id, region, minutes_offline, last_seen | order by minutes_offline desc"),
    ("battery", "Battery anomalies (6h): stuck SOC / overheating", "table", 0, 18, 12, 7,
     "FleetBatteryAnomalies(6h) | project device_id, site_id, soc_stuck, overheating, temp_max_c, readings, last_reading"),
    ("map", "Sites (bubble = PV kW now)", "map", 12, 18, 12, 7,
     "FleetDeviceStatus(20m) | where device_type == 'inverter' | lookup kind=inner dim_site on site_id "
     "| project latitude, longitude, site_id, pv_kw = coalesce(pv_kw, 0.0), status"),
]


def dashboard(e: str) -> dict:
    w, db, uri = fleet_db(e)
    ds_id, page_id, time_param = gid(f"rtd/ds/{e}"), gid("rtd/page"), gid("rtd/param/time")
    queries, tiles = [], []
    for tid, title, visual, x, y, wd, h, q in TILES:
        qid = gid(f"rtd/q/{tid}")
        used = [v for v in ("_startTime", "_endTime") if v in q]
        queries.append({"id": qid, "dataSource": {"kind": "inline", "dataSourceId": ds_id}, "text": q, "usedVariables": used})
        options = {}
        if visual == "map":
            options = {"map__type": "bubble", "map__latitudeColumn": "latitude", "map__longitudeColumn": "longitude",
                       "map__labelColumn": "site_id", "map__sizeColumn": "pv_kw", "map__sizeDisabled": False,
                       "map__geoType": "numeric", "map__geoPointColumn": None}
        tiles.append({"id": gid(f"rtd/t/{tid}"), "title": title, "visualType": visual, "pageId": page_id,
                      "layout": {"x": x, "y": y, "width": wd, "height": h},
                      "queryRef": {"kind": "query", "queryId": qid}, "visualOptions": options})
    body = {
        "$schema": "https://dataexplorer.azure.com/static/d/schema/52/dashboard.json",
        "schema_version": "52", "title": "Contoso Energy - Fleet operations",
        "autoRefresh": {"enabled": True, "defaultInterval": "1m", "minInterval": "30s"},
        "baseQueries": [], "tiles": tiles, "queries": queries,
        "dataSources": [{"id": ds_id, "name": "eh_fleet", "scopeId": "kusto-trident", "kind": "kusto-trident",
                         "clusterUri": uri, "database": db["id"], "workspace": w}],
        "pages": [{"name": "Fleet overview", "id": page_id}],
        "parameters": [{"kind": "duration", "id": time_param, "displayName": "Time range", "description": "",
                        "beginVariableName": "_startTime", "endVariableName": "_endTime",
                        "defaultValue": {"kind": "dynamic", "count": 6, "unit": "hours"}, "showOnPages": {"kind": "all"}}],
    }
    item = upsert_item(w, "KQLDashboard", "rtd_fleet_ops", [
        {"path": "RealTimeDashboard.json", "payload": b64(body), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("KQLDashboard", "rtd_fleet_ops")), "payloadType": "InlineBase64"}],
        "Fleet operations real-time dashboard (Fleet API)")
    # Smoke-test every tile query against the database (dashboard parameters substituted).
    bad = 0
    for tid, title, *_rest, q in TILES:
        test = q.replace("_startTime", "ago(6h)").replace("_endTime", "now()")
        try:
            n = len(kql(uri, db["id"], test))
            log(f"tile ok   {title}: {n} rows")
        except SystemExit as ex:
            bad += 1
            log(f"tile FAIL {title}: {str(ex)[:200]}")
    if bad:
        raise SystemExit(f"{bad} dashboard tile query(ies) failed")
    return item


# ---------------------------------------------------------------- activators
def _instance(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _email(recipient: str, subject: str, message: str) -> dict:
    return {"name": "EmailBinding", "kind": "EmailMessage", "arguments": [
        {"name": "messageLocale", "type": "string", "value": "en-us"},
        {"name": "sentTo", "type": "array", "values": [{"type": "string", "value": recipient}]},
        {"name": "copyTo", "type": "array", "values": []},
        {"name": "bCCTo", "type": "array", "values": []},
        {"name": "subject", "type": "array", "values": [{"type": "string", "value": subject}]},
        {"name": "headline", "type": "array", "values": [{"type": "string", "value": subject}]},
        {"name": "optionalMessage", "type": "array", "values": [{"type": "string", "value": message}]},
        {"name": "additionalInformation", "type": "array", "values": []}]}


def _state(field: str, op: str, value: str) -> dict:
    return {"name": "EventDetectStep", "id": str(uuid.uuid4()), "rows": [
        {"name": "EventFieldSelector", "kind": "EventField", "arguments": [{"name": "fieldName", "type": "string", "value": field}]},
        {"name": "TextValueCondition", "kind": "TextValueCondition", "arguments": [
            {"name": "op", "type": "string", "value": op}, {"name": "value", "type": "string", "value": value}]}]}


def activator_entities(name: str, w: str, db_id: str, query: str, time_field: str, detect_steps: list[dict],
                       recipient: str, subject: str, message: str) -> list[dict]:
    c, s, ev, rule = (str(uuid.uuid4()) for _ in range(4))
    q = " ".join(("declare query_parameters(startTime:datetime, endTime:datetime); " + query).split())
    return [
        {"uniqueIdentifier": c, "payload": {"name": name, "type": "kqlQueries"}, "type": "container-v1"},
        {"uniqueIdentifier": s, "type": "kqlSource-v1", "payload": {
            "name": f"{name} source", "runSettings": {"executionIntervalInSeconds": 300},
            "query": {"queryString": q},
            "eventhouseItem": {"itemId": db_id, "workspaceId": w, "itemType": "KustoDatabase"},
            "queryParameters": [{"name": "startTime", "type": "DURATION_START", "value": "2026-01-01T00:00:00Z"},
                                {"name": "endTime", "type": "DURATION_END", "value": "2026-01-01T00:05:00Z"}],
            "eventTimeSettings": {"timeFieldName": time_field, "ingestionDelayInSeconds": 120, "timeZone": "UTC"},
            "metadata": {"workspaceId": w, "measureName": "", "querySetId": "", "queryId": ""},
            "parentContainer": {"targetUniqueIdentifier": c}}},
        {"uniqueIdentifier": ev, "type": "timeSeriesView-v1", "payload": {
            "name": f"{name} events", "parentContainer": {"targetUniqueIdentifier": c},
            "definition": {"type": "Event", "instance": _instance({
                "templateId": "SourceEvent", "templateVersion": TEMPLATE_VERSION, "steps": [
                    {"name": "SourceEventStep", "id": str(uuid.uuid4()), "rows": [
                        {"name": "SourceSelector", "kind": "SourceReference",
                         "arguments": [{"name": "entityId", "type": "string", "value": s}]}]}]})}}},
        {"uniqueIdentifier": rule, "type": "timeSeriesView-v1", "payload": {
            "name": subject, "description": "Created by: contoso-energy-lab infra/rti_alerts.py",
            "parentContainer": {"targetUniqueIdentifier": c},
            "definition": {"type": "Rule", "settings": {"shouldRun": True, "shouldApplyRuleOnUpdate": False},
                           "instance": _instance({"templateId": "EventTrigger", "templateVersion": TEMPLATE_VERSION, "steps": [
                               {"name": "FieldsDefaultsStep", "id": str(uuid.uuid4()), "rows": [
                                   {"name": "EventSelector", "kind": "Event", "arguments": [
                                       {"name": "event", "kind": "EventReference", "type": "complex",
                                        "arguments": [{"name": "entityId", "type": "string", "value": ev}]}]}]},
                               *detect_steps,
                               {"name": "ActStep", "id": str(uuid.uuid4()), "rows": [_email(recipient, subject, message)]}]})}}},
    ]


ACTIVATORS = {
    "act_fleet_device_faults": dict(
        query="silver_alerts_dedup | where ingest_time between (startTime .. endTime) "
              "| project ingest_time, alert_id, event_type, severity, alert_code, device_id, site_id, message",
        time_field="ingest_time",
        detect=lambda: [_state("event_type", "IsEqualTo", "raised"), _state("severity", "IsNotEqualTo", "Low")],
        subject="Contoso fleet: device fault raised",
        message="A device alert (severity High/Critical/Medium) was raised. Open the Fleet operations dashboard for details."),
    "act_fleet_offline_sites": dict(
        # Emits each site once, at the moment its gateway crosses 20 minutes without data (transition, not state).
        query="FleetDeviceStatus(20m) | where device_type == 'gateway' | extend offline_at = last_seen + 20m "
              "| where offline_at between (startTime .. endTime) | project offline_at, site_id, device_id, last_seen",
        time_field="offline_at",
        detect=lambda: [{"name": "EventDetectStep", "id": str(uuid.uuid4()),
                         "rows": [{"name": "OnEveryValue", "kind": "OnEveryValue", "arguments": []}]}],
        subject="Contoso fleet: site offline",
        message="A site gateway has not reported for 20 minutes. Check connectivity / site power."),
}


def activators(e: str) -> None:
    w, db, uri = fleet_db(e)
    recipient = env("ALERT_EMAIL", required=False) or env("ADMIN_UPN")
    for name, spec in ACTIVATORS.items():
        test = spec["query"].replace("startTime", "ago(1d)").replace("endTime", "now()")
        n = len(kql(uri, db["id"], test))  # source validation gate: the query must run and return the expected shape
        entities = activator_entities(name, w, db["id"], spec["query"], spec["time_field"], spec["detect"](),
                                      recipient, spec["subject"], spec["message"])
        item = find_item(w, "Reflex", name)
        parts = [{"path": "ReflexEntities.json", "payload": b64(entities), "payloadType": "InlineBase64"}]
        if item:
            fabric("post", f"workspaces/{w}/items/{item['id']}/updateDefinition", {"definition": {"parts": parts}})
        else:
            parts.append({"path": ".platform", "payload": b64(platform("Reflex", name)), "payloadType": "InlineBase64"})
            upsert_item(w, "Reflex", name, parts, spec["subject"])
        log(f"{name}: deployed (source query returns {n} rows over the last day; rule running)")


def status(e: str) -> None:
    import base64
    w = ws(e)
    for name in ACTIVATORS:
        item = find_item(w, "Reflex", name)
        if not item:
            print(f"{name}: missing")
            continue
        parts = fabric("post", f"workspaces/{w}/items/{item['id']}/getDefinition")["definition"]["parts"]
        ents = json.loads(base64.b64decode(next(p["payload"] for p in parts if p["path"] == "ReflexEntities.json")))
        rules = [x["payload"] for x in ents if x["type"] == "timeSeriesView-v1" and x["payload"]["definition"]["type"] == "Rule"]
        print(f"{name}: {len(ents)} entities, rules: " + ", ".join(f"{r['name']} (running={r['definition'].get('settings', {}).get('shouldRun')})" for r in rules))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["dashboard", "activators", "status", "all"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    a = p.parse_args()
    load_env()
    if a.action in ("dashboard", "all"):
        dashboard(a.env)
    if a.action in ("activators", "all"):
        activators(a.env)
    if a.action in ("status", "all"):
        status(a.env)


if __name__ == "__main__":
    main()
