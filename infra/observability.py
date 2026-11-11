"""Step 7: observability - failure injection -> detection -> alert routing. Idempotent.

  py infra/observability.py notebook   [--env dev]   # push nb_finance_medallion (parameter cell for failure injection)
  py infra/observability.py pipeline   [--env dev]   # pl_finance_daily (failure_mode: none | fail_activity | notebook_missing_file)
  py infra/observability.py jobstream                # cap_es_jobs: Real-Time Hub Fabric job events -> cap_monitoring.job_events_raw
  py infra/observability.py activator                # act_job_failures: workspace monitoring -> routed emails (not in Git, D-018)
  py infra/observability.py inject --mode fail_activity|notebook_missing_file|none   # run + measure detection latency
  py infra/observability.py all

Detection paths compared (dev): job API (ground truth) vs Real-Time Hub job events (push) vs workspace monitoring
ItemJobEventLogs (+ Activator polling every 5 min). Routing: pipeline/notebook failures -> data engineering on-call,
semantic model refresh failures -> BI owner (ALERT_EMAIL_DATAENG / ALERT_EMAIL_BI, default ADMIN_UPN).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time

from capacity_monitoring import eventhouse as fleet_eventhouse
from common import env, load_env, log
from fabric_rest import FABRIC, _request, b64, fabric, find_item, kql, upsert_item
from finance_lakehouse import NOTEBOOK, lakehouse, notebook_ipynb, ws
from rti_alerts import activator_entities
from rti_lab import platform, ws_id
from workspace_monitoring import eventhouse as monitoring_eventhouse

PIPELINE = "pl_finance_daily"
JOB_ES = "cap_es_jobs"
ACTIVATOR = "act_job_failures"
MODES = ("none", "fail_activity", "notebook_missing_file")


def notebook(e: str) -> None:
    w = ws("finance", e)
    lh = lakehouse(w, "lh_finance", "")
    upsert_item(w, "Notebook", NOTEBOOK, [
        {"path": "notebook-content.ipynb", "payload": b64(notebook_ipynb(w, lh)), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("Notebook", NOTEBOOK)), "payloadType": "InlineBase64"}],
        "Finance medallion: bronze shortcuts -> silver -> gold star schema", definition_format="ipynb")
    log(f"{NOTEBOOK} updated (parameter cell inject_failure)")


def pipeline(e: str) -> dict:
    w = ws("finance", e)
    nb = find_item(w, "Notebook", NOTEBOOK)
    policy = {"timeout": "0.01:00:00", "retry": 0, "retryIntervalInSeconds": 30, "secureOutput": False, "secureInput": False}
    content = {"properties": {
        "description": "Daily finance refresh: medallion notebook. failure_mode injects failures for step 7 tests.",
        "parameters": {"failure_mode": {"type": "string", "defaultValue": "none"}},
        "activities": [
            {"name": "Inject pipeline failure", "type": "IfCondition", "dependsOn": [],
             "typeProperties": {
                 "expression": {"value": "@equals(pipeline().parameters.failure_mode, 'fail_activity')", "type": "Expression"},
                 "ifTrueActivities": [{"name": "Fail - injected", "type": "Fail", "dependsOn": [], "typeProperties": {
                     "message": "Injected failure (step 7 observability test): upstream partner file not delivered",
                     "errorCode": "CE-OBS-001"}}],
                 "ifFalseActivities": []}},
            {"name": "Run finance medallion", "type": "TridentNotebook",
             "dependsOn": [{"activity": "Inject pipeline failure", "dependencyConditions": ["Succeeded"]}],
             "policy": policy,
             "typeProperties": {"notebookId": nb["id"], "workspaceId": w, "parameters": {"inject_failure": {
                 "value": {"value": "@if(equals(pipeline().parameters.failure_mode, 'notebook_missing_file'), "
                                    "'missing_file', '')", "type": "Expression"}, "type": "string"}}}},
        ]}}
    item = upsert_item(w, "DataPipeline", PIPELINE, [
        {"path": "pipeline-content.json", "payload": b64(content), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("DataPipeline", PIPELINE)), "payloadType": "InlineBase64"}],
        "Daily finance refresh (step 7 failure injection)")
    log(f"{PIPELINE}: {item['id']}")
    return item


# ---------------------------------------------------------------- detection path B: Real-Time Hub job events
def jobstream() -> None:
    fw = ws_id()  # ce-fleet-dev (cap_monitoring lives in eh_fleet, D-027)
    db = find_item(fw, "KQLDatabase", "cap_monitoring")
    finance = ws("finance", "dev")
    sources = []
    for name, typ in ((PIPELINE, "DataPipeline"), (NOTEBOOK, "Notebook")):
        item = find_item(finance, typ, name)
        sources.append({"name": f"jobs-{name}".replace("_", "-"), "type": "FabricJobEvents", "properties": {
            "eventScope": "Item", "workspaceId": finance, "itemId": item["id"],
            "includedEventTypes": ["Microsoft.Fabric.JobEvents.ItemJobCreated", "Microsoft.Fabric.JobEvents.ItemJobStatusChanged",
                                   "Microsoft.Fabric.JobEvents.ItemJobSucceeded", "Microsoft.Fabric.JobEvents.ItemJobFailed"],
            "filters": []}})
    stream = f"{JOB_ES}-stream"
    topology = {
        "sources": sources,
        "destinations": [{"name": "raw", "type": "Eventhouse", "properties": {
            "dataIngestionMode": "DirectIngestion", "workspaceId": fw, "itemId": db["id"], "tableName": "job_events_raw",
            "connectionName": "cap_es_jobs_raw", "mappingRuleName": "job_events_raw_json"}, "inputNodes": [{"name": stream}]}],
        "streams": [{"name": stream, "type": "DefaultStream", "properties": {},
                     "inputNodes": [{"name": s["name"]} for s in sources]}],
        "operators": [], "compatibilityLevel": "1.0"}
    es = upsert_item(fw, "Eventstream", JOB_ES, [
        {"path": "eventstream.json", "payload": b64(topology), "payloadType": "InlineBase64"},
        {"path": "eventstreamProperties.json", "payloadType": "InlineBase64",
         "payload": b64({"retentionTimeInDays": 1, "eventThroughputLevel": "Low"})},
        {"path": ".platform", "payload": b64(platform("Eventstream", JOB_ES)), "payloadType": "InlineBase64"}],
        "Step 7: Fabric job events (pipeline + notebook) -> cap_monitoring")
    for _ in range(40):
        topo = fabric("get", f"workspaces/{fw}/eventstreams/{es['id']}/topology")
        status = [n.get("status") for n in topo["sources"] + topo["destinations"]]
        if all(s == "Running" for s in status) or "Failed" in status:
            break
        time.sleep(15)
    log(f"{JOB_ES} status: {status}")
    if "Failed" in status:
        raise SystemExit(json.dumps(topo)[:1500])


# ---------------------------------------------------------------- detection path C: workspace monitoring + Activator
ROUTES = [  # (route, ItemKind filter, recipient env key, subject)
    # ItemKind values as logged (pipelines = 'Pipeline', notebooks = 'SynapseNotebook')
    ("dataeng", "('Pipeline', 'DataPipeline', 'SynapseNotebook', 'Notebook', 'SparkJobDefinition', 'CopyJob', 'Dataflow')",
     "ALERT_EMAIL_DATAENG",
     "Contoso data platform: pipeline/notebook job failed"),
    ("bi", "('SemanticModel', 'Dataset')", "ALERT_EMAIL_BI", "Contoso BI: semantic model refresh failed"),
]


def activator() -> None:
    fw = ws("finance", "dev")
    uri, db = monitoring_eventhouse("finance")
    entities = []
    for route, kinds, key, subject in ROUTES:
        q = (f"ItemJobEventLogs | where Timestamp between (startTime .. endTime) and JobStatus == 'Failed' "
             f"and ItemKind in~ {kinds} | project Timestamp, WorkspaceName, ItemKind, ItemName, JobType, JobInvokeType, "
             "JobInstanceId, DurationMs")
        kql(uri, db, q.replace("startTime", "ago(1d)").replace("endTime", "now()"))  # validation gate
        detect = [{"name": "EventDetectStep", "id": f"00000000-0000-0000-0000-0000000007{len(entities):02d}",
                   "rows": [{"name": "OnEveryValue", "kind": "OnEveryValue", "arguments": []}]}]
        entities += activator_entities(f"{ACTIVATOR}_{route}", fw, db, q, "Timestamp", detect,
                                       env(key, required=False) or env("ALERT_EMAIL", required=False) or env("ADMIN_UPN"),
                                       subject, "A Fabric job failed. Open the Monitor hub (filter: Failed) or query "
                                                "ItemJobEventLogs in the workspace monitoring database for details.")
    parts = [{"path": "ReflexEntities.json", "payload": b64(entities), "payloadType": "InlineBase64"}]
    item = find_item(fw, "Reflex", ACTIVATOR)
    if item:
        fabric("post", f"workspaces/{fw}/items/{item['id']}/updateDefinition", {"definition": {"parts": parts}})
    else:
        parts.append({"path": ".platform", "payload": b64(platform("Reflex", ACTIVATOR)), "payloadType": "InlineBase64"})
        upsert_item(fw, "Reflex", ACTIVATOR, parts, "Step 7: routed job-failure alerts (workspace monitoring)")
    log(f"{ACTIVATOR}: {len(ROUTES)} routes (dataeng, bi), polling ItemJobEventLogs every 5 min")


# ---------------------------------------------------------------- injection + detection latency
def inject(mode: str, timeout_min: int = 45) -> dict:
    w = ws("finance", "dev")
    pl = find_item(w, "DataPipeline", PIPELINE)
    status, headers, body = _request("post", f"{FABRIC}/v1/workspaces/{w}/items/{pl['id']}/jobs/instances?jobType=Pipeline",
                                     {"executionData": {"parameters": {"failure_mode": mode}}}, FABRIC)
    if status != 202:
        raise SystemExit(f"pipeline start failed: {status} {body}")
    job_url = headers["Location"]
    job_id = job_url.rstrip("/").split("/")[-1]
    log(f"{PIPELINE} started (failure_mode={mode}, job {job_id})")
    t0 = time.time()
    while time.time() - t0 < timeout_min * 60:
        time.sleep(15)
        _, _, job = _request("get", job_url, None, FABRIC)
        if job.get("status") in ("Completed", "Failed", "Cancelled", "Deduped"):
            break
    end = job.get("endTimeUtc")
    log(f"job API: {job.get('status')} end={end} reason={json.dumps(job.get('failureReason'))[:300]}")
    expected = "Failed" if mode != "none" else "Completed"

    # B: Real-Time Hub job events (push)
    fw = ws_id()
    _, curi = fleet_eventhouse(fw)
    cdb = find_item(fw, "KQLDatabase", "cap_monitoring")["id"]
    # C: workspace monitoring
    muri, mdb = monitoring_eventhouse("finance")
    seen = {"job_events": None, "workspace_monitoring": None}
    t1 = time.time()
    while time.time() - t1 < 30 * 60 and None in seen.values():
        if seen["job_events"] is None:
            r = kql(curi, cdb, f"job_events_raw | where tostring(event.data.jobInstanceId) =~ '{job_id}' "
                               "and tostring(event.type) has_any ('ItemJobFailed', 'ItemJobSucceeded') "
                               "| summarize first_seen = min(ingestion_time())")
            if r and r[0]["first_seen"]:
                seen["job_events"] = r[0]
        if seen["workspace_monitoring"] is None:
            r = kql(muri, mdb, f"ItemJobEventLogs | where JobInstanceId =~ '{job_id}' and JobStatus in ('Failed', 'Completed') "
                               "| summarize first_seen = min(ingestion_time()), status = take_any(JobStatus)")
            if r and r[0]["first_seen"]:
                seen["workspace_monitoring"] = r[0]
        time.sleep(30)
    result = {"mode": mode, "job_status": job.get("status"), "expected": expected, "job_end_utc": end}
    for k, v in seen.items():
        if v and end:
            lag = (dt.datetime.fromisoformat(v["first_seen"].replace("Z", "+00:00")) -
                   dt.datetime.fromisoformat(end[:19] + "+00:00")).total_seconds()
            result[f"{k}_lag_s"] = round(lag)
        else:
            result[f"{k}_lag_s"] = None
    log(f"detection: {json.dumps(result)}")
    if job.get("status") != expected:
        raise SystemExit(f"unexpected job status {job.get('status')} (expected {expected})")
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["notebook", "pipeline", "jobstream", "activator", "inject", "all"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    p.add_argument("--mode", choices=MODES, default="fail_activity")
    a = p.parse_args()
    load_env()
    if a.action in ("notebook", "all"):
        notebook(a.env)
    if a.action in ("pipeline", "all"):
        pipeline(a.env)
    if a.action in ("jobstream", "all"):
        jobstream()
    if a.action in ("activator", "all"):
        activator()
    if a.action == "inject":
        inject(a.mode)


if __name__ == "__main__":
    main()
