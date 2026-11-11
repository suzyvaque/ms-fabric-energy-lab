"""Step 6 option (iii): capacity reporting on Power BI (APIs -> lakehouse -> Direct Lake model -> report). Dev only (D-027).

  py infra/capacity_report.py collect   # Fabric/Admin REST + activity events + Azure Cost Management + capacity-event
                                        # aggregates (cap_monitoring) -> cap_lh_capacity tables (overwrite, 30 days)
  py infra/capacity_report.py model     # cap_sm_capacity (Direct Lake) + refresh
  py infra/capacity_report.py report    # cap_rpt_capacity (PBIR) + PDF render check
  py infra/capacity_report.py all

Per-item CU (true chargeback) only exists in the Capacity Metrics app model; this collector allocates capacity cost
by job hours as a proxy and leaves Metrics-app extraction to FUAM (see monitoring/capacity-comparison.md).
User ids from activity events are hashed before they land in the lakehouse.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import time

from common import env, load_env, log
from fabric_rest import FABRIC, _request, b64, fabric, find_item, kql, upsert_item
from fabric_setup import capacity_id
from finance_lakehouse import lakehouse, refresh_sql_endpoint, ws
from onelake import upload
from rti_lab import platform

LH = "cap_lh_capacity"
MODEL = "cap_sm_capacity"
REPORT = "cap_rpt_capacity"
PBI = "https://api.powerbi.com/v1.0/myorg"
PBI_RES = "https://analysis.windows.net/powerbi/api"
ARM = "https://management.azure.com"
JOB_TYPES = {"Notebook", "DataPipeline", "SparkJobDefinition", "CopyJob", "Dataflow"}
DAYS = 30


def get_all(path: str, key: str = "value") -> list[dict]:
    out, url = [], path
    while url:
        r = fabric("get", url)
        out += r.get(key, [])
        tok = r.get("continuationToken")
        url = f"{path}{'&' if '?' in path else '?'}continuationToken={tok}" if tok else None
    return out


def to_csv(rows: list[dict], cols: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    return buf.getvalue().encode("utf-8")


# ----------------------------------------------------------------------------------------- collectors
def capacities(cap: str) -> list[dict]:
    return [{"capacity_id": c["id"], "capacity_name": c["displayName"], "sku": c["sku"], "region": c["region"],
             "state": c["state"]} for c in fabric("get", "capacities")["value"] if c["id"] == cap]


def workspaces_items(cap: str) -> tuple[list[dict], list[dict]]:
    wss = [w for w in get_all("admin/workspaces?state=Active", "workspaces") if (w.get("capacityId") or "").lower() == cap.lower()]
    items = []
    for w in wss:
        for i in get_all(f"admin/items?workspaceId={w['id']}", "itemEntities"):
            creator = i.get("creatorPrincipal") or {}
            items.append({"item_id": i["id"], "item_name": i["name"], "item_type": i["type"], "workspace_id": w["id"],
                          "creator_type": creator.get("type"), "creator": creator.get("displayName"),
                          "last_updated": i.get("lastUpdatedDate")})
    return ([{"workspace_id": w["id"], "workspace_name": w["name"], "capacity_id": cap,
              "environment": w["name"].rsplit("-", 1)[-1], "domain": w["name"].split("-")[1] if w["name"].count("-") >= 2 else ""}
             for w in wss], items)


def jobs(items: list[dict]) -> list[dict]:
    out = []
    for i in items:
        if i["item_type"] not in JOB_TYPES:
            continue
        status, _, body = _request("get", f"{FABRIC}/v1/workspaces/{i['workspace_id']}/items/{i['item_id']}/jobs/instances",
                                   None, FABRIC)
        for j in body.get("value", []) if status == 200 else []:
            start, end = j.get("startTimeUtc"), j.get("endTimeUtc")
            dur = None
            if start and end:
                dur = (dt.datetime.fromisoformat(end[:19]) - dt.datetime.fromisoformat(start[:19])).total_seconds()
            out.append({"job_id": j["id"], "item_id": i["item_id"], "workspace_id": i["workspace_id"],
                        "job_type": j.get("jobType"), "invoke_type": j.get("invokeType"), "status": j.get("status"),
                        "start_utc": (start or "")[:19], "end_utc": (end or "")[:19], "duration_s": dur,
                        "failure": ((j.get("failureReason") or {}).get("message") or "")[:300]})
    return out


def activities(days: int = 3) -> list[dict]:
    """Admin activity events (max 1 UTC day per call, 30 days history). User ids are hashed."""
    out = []
    today = dt.datetime.now(dt.timezone.utc).date()
    for d in range(days):
        day = today - dt.timedelta(days=d)
        url = (f"{PBI}/admin/activityevents?startDateTime='{day}T00:00:00.000Z'&endDateTime='"
               f"{day}T23:59:59.999Z'")
        while url:
            status, _, body = _request("get", url, None, PBI_RES)
            if status != 200:
                log(f"activity events {day}: {status} {str(body)[:200]}")
                break
            for e in body.get("activityEventEntities", []):
                out.append({"time_utc": e.get("CreationTime", "")[:19], "activity": e.get("Activity"),
                            "workspace_id": e.get("WorkspaceId"), "item_name": e.get("ItemName") or e.get("ArtifactName"),
                            "item_type": e.get("ItemType") or e.get("ArtifactKind"),
                            "user_hash": hashlib.sha256((e.get("UserId") or "").lower().encode()).hexdigest()[:12],
                            "is_success": e.get("IsSuccess")})
            url = None if body.get("lastResultSet") else body.get("continuationUri")
    return out


def cost(days: int = DAYS) -> list[dict]:
    """Azure Cost Management (ActualCost, daily) for the lab resource group: Fabric capacity + Event Hubs."""
    end = dt.date.today()
    body = {"type": "ActualCost", "timeframe": "Custom",
            "timePeriod": {"from": f"{end - dt.timedelta(days=days)}T00:00:00Z", "to": f"{end}T23:59:59Z"},
            "dataset": {"granularity": "Daily", "aggregation": {"cost": {"name": "Cost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": "ResourceType"},
                                     {"type": "Dimension", "name": "MeterSubCategory"},
                                     {"type": "Dimension", "name": "Meter"}]}}
    scope = f"subscriptions/{env('AZURE_SUBSCRIPTION_ID')}/resourceGroups/{env('AZURE_RESOURCE_GROUP')}"
    for attempt in range(5):  # Cost Management throttles aggressively (429 + Retry-After)
        status, headers, r = _request("post", f"{ARM}/{scope}/providers/Microsoft.CostManagement/query?api-version=2023-11-01",
                                      body, ARM)
        if status == 200:
            break
        log(f"cost query {status}: {str(r)[:200]}")
        time.sleep(int(headers.get("Retry-After", 30)) if status == 429 else 10)
    else:
        return []
    cols = [c["name"] for c in r["properties"]["columns"]]
    out = []
    for row in r["properties"]["rows"]:
        d = dict(zip(cols, row))
        day = str(d.get("UsageDate"))
        out.append({"date": f"{day[:4]}-{day[4:6]}-{day[6:8]}", "resource_type": d.get("ResourceType"),
                    "meter_subcategory": d.get("MeterSubCategory"), "meter": d.get("Meter"),
                    "cost": round(float(d.get("Cost", 0)), 4), "currency": d.get("Currency")})
    return out


def utilization() -> tuple[list[dict], list[dict]]:
    """Hourly aggregates from option (ii) (cap_monitoring) so the Power BI report can show both."""
    w = ws("fleet", "dev")
    eh = find_item(w, "Eventhouse", "eh_fleet")
    uri = fabric("get", f"workspaces/{w}/eventhouses/{eh['id']}")["properties"]["queryServiceUri"]
    db = find_item(w, "KQLDatabase", "cap_monitoring")["id"]
    u = kql(uri, db, f"CapacityUtilization({DAYS}d, 1h) | project hour = format_datetime(window_start, 'yyyy-MM-dd HH:mm:ss'), "
                     "capacity_name, utilization_pct, max_utilization_pct, throttle_pct = max_of(interactive_delay_pct, "
                     "interactive_rejection_pct, background_rejection_pct), overage_cu_s = overage_total_cu_ms / 1000.0")
    wl = kql(uri, db, f"capacity_workload | where window_start > ago({DAYS}d) | summarize cu_s = sum(background_cu_ms + "
                      "interactive_cu_ms) / 1000.0, preview_cu_s = sum(preview_cu_ms) / 1000.0 by hour = "
                      "format_datetime(bin(window_start, 1h), 'yyyy-MM-dd HH:mm:ss'), workload")
    return u, wl


TABLES = {  # lakehouse table -> columns
    "cap_capacities": ["capacity_id", "capacity_name", "sku", "region", "state"],
    "cap_workspaces": ["workspace_id", "workspace_name", "capacity_id", "environment", "domain"],
    "cap_items": ["item_id", "item_name", "item_type", "workspace_id", "creator_type", "creator", "last_updated"],
    "cap_jobs": ["job_id", "item_id", "workspace_id", "job_type", "invoke_type", "status", "start_utc", "end_utc",
                 "duration_s", "failure"],
    "cap_activities": ["time_utc", "activity", "workspace_id", "item_name", "item_type", "user_hash", "is_success"],
    "cap_cost_daily": ["date", "resource_type", "meter_subcategory", "meter", "cost", "currency"],
    "cap_utilization_hourly": ["hour", "capacity_name", "utilization_pct", "max_utilization_pct", "throttle_pct", "overage_cu_s"],
    "cap_workload_hourly": ["hour", "workload", "cu_s", "preview_cu_s"],
}


def collect() -> dict:
    cap = capacity_id()
    w = ws("fleet", "dev")
    lh = lakehouse(w, LH, "Step 6 (iii): capacity inventory, jobs, activity, cost, utilization (collected by script)")
    wss, items = workspaces_items(cap)
    util, wl = utilization()
    data = {"cap_capacities": capacities(cap), "cap_workspaces": wss, "cap_items": items, "cap_jobs": jobs(items),
            "cap_activities": activities(), "cap_cost_daily": cost(), "cap_utilization_hourly": util,
            "cap_workload_hourly": wl}
    for table, rows in data.items():
        path = f"Files/landing/capacity/{table}.csv"
        upload(w, lh["id"], path, to_csv(rows, TABLES[table]))
        if not rows:
            log(f"{table}: 0 rows (header only)")
        fabric("post", f"workspaces/{w}/lakehouses/{lh['id']}/tables/{table}/load", {
            "relativePath": path, "pathType": "File", "mode": "Overwrite", "recursive": False,
            "formatOptions": {"format": "Csv", "header": True, "delimiter": ","}})
        log(f"{LH}.{table}: {len(rows)} rows")
    refresh_sql_endpoint(w, LH)
    return lh


# ----------------------------------------------------------------------------------------- semantic model + report
M_TABLES = {  # model table -> (lakehouse table, columns not to summarize)
    "Utilization": ("cap_utilization_hourly", set()),
    "Workload": ("cap_workload_hourly", set()),
    "Cost": ("cap_cost_daily", set()),
    "Jobs": ("cap_jobs", {"job_id", "item_id", "workspace_id"}),
    "Items": ("cap_items", {"item_id", "workspace_id"}),
    "Workspaces": ("cap_workspaces", {"workspace_id", "capacity_id"}),
    "Activities": ("cap_activities", {"workspace_id"}),
}
M_RELATIONSHIPS = [("Jobs", "item_id", "Items", "item_id"), ("Items", "workspace_id", "Workspaces", "workspace_id"),
                   ("Activities", "workspace_id", "Workspaces", "workspace_id")]
M_MEASURES = {
    "Utilization": [
        ("Avg Utilization", "AVERAGE(Utilization[utilization_pct]) / 100", "0.0%", "Average billed CU utilization (hourly)."),
        ("Peak Utilization", "MAX(Utilization[max_utilization_pct]) / 100", "0.0%", "Highest 30-second window."),
        ("Peak Throttling Stage", "MAX(Utilization[throttle_pct]) / 100", "0.0%",
         "Highest progress toward a throttling stage (100 % = stage reached)."),
        ("Hours Above 70%", "COUNTROWS(FILTER(Utilization, Utilization[max_utilization_pct] >= 70)) + 0", "#,0",
         "Hours with at least one window at/above the 70 % alert threshold."),
    ],
    "Workload": [
        ("Billed CU Hours", "SUM(Workload[cu_s]) / 3600", "#,0.00", "Billed capacity-unit hours."),
        ("Preview CU Hours", "SUM(Workload[preview_cu_s]) / 3600", "#,0.00", "CU hours of preview workloads (not billed)."),
    ],
    "Cost": [
        ("Total Cost", "SUM(Cost[cost])", "\\$#,0.00", "Azure actual cost (Cost Management) in the billing currency."),
        ("Fabric Capacity Cost", "CALCULATE([Total Cost], Cost[resource_type] = \"microsoft.fabric/capacities\")", "\\$#,0.00",
         "Cost of the Fabric capacity resource (pay-as-you-go hours incl. paused time = 0)."),
        ("Avg Daily Cost", "AVERAGEX(VALUES(Cost[date]), [Total Cost])", "\\$#,0.00", "Average cost per day."),
    ],
    "Jobs": [
        ("Job Runs", "COUNTROWS(Jobs) + 0", "#,0", "Job instances (notebooks, pipelines, ...)."),
        ("Failed Jobs", "CALCULATE(COUNTROWS(Jobs), Jobs[status] = \"Failed\") + 0", "#,0", "Job instances with status Failed."),
        ("Job Failure Rate", "DIVIDE([Failed Jobs], [Job Runs])", "0.0%", "Failed / all job instances."),
        ("Job Hours", "SUM(Jobs[duration_s]) / 3600", "#,0.00", "Run time of job instances."),
        ("Allocated Capacity Cost (proxy)",
         "DIVIDE([Job Hours], CALCULATE([Job Hours], REMOVEFILTERS(Workspaces), REMOVEFILTERS(Items))) "
         "* CALCULATE([Fabric Capacity Cost], REMOVEFILTERS())", "\\$#,0.00",
         "Chargeback PROXY: capacity cost split by job hours. True CU chargeback needs the Capacity Metrics app (FUAM)."),
    ],
    "Items": [("Item Count", "COUNTROWS(Items) + 0", "#,0", "Fabric items on the capacity.")],
    "Activities": [
        ("Activity Events", "COUNTROWS(Activities) + 0", "#,0", "Admin activity events (last 3 days)."),
        ("Active Users", "DISTINCTCOUNT(Activities[user_hash])", "#,0", "Distinct (hashed) users."),
    ],
}


def model() -> dict:
    from semantic_model import pbi, tmdl_for
    w = ws("fleet", "dev")
    parts = [{"path": p, "payload": b64(c), "payloadType": "InlineBase64"}
             for p, c in tmdl_for(w, LH, M_TABLES, M_RELATIONSHIPS, M_MEASURES, exclude={"creator"})]
    parts.append({"path": "definition.pbism", "payload": b64({"version": "4.2", "settings": {}}), "payloadType": "InlineBase64"})
    parts.append({"path": ".platform", "payload": b64(platform("SemanticModel", MODEL)), "payloadType": "InlineBase64"})
    item = upsert_item(w, "SemanticModel", MODEL, parts, "Step 6 (iii): capacity utilization, cost, jobs (Direct Lake)",
                       definition_format="TMDL")
    status, _, body = pbi("post", f"groups/{w}/datasets/{item['id']}/refreshes", {"type": "full"})
    for _ in range(40):
        time.sleep(10)
        _, _, r = pbi("get", f"groups/{w}/datasets/{item['id']}/refreshes?$top=1")
        st = r.get("value", [{}])[0].get("status")
        if st in ("Completed", "Failed", "Disabled", "Cancelled"):
            break
    if st != "Completed":
        raise SystemExit(f"refresh {st}: {r.get('value', [{}])[0].get('serviceExceptionJson')}")
    _, _, res = pbi("post", f"groups/{w}/datasets/{item['id']}/executeQueries", {"queries": [{"query":
        'EVALUATE ROW("avg_util", [Avg Utilization], "peak", [Peak Utilization], "cost", [Total Cost], "fabric", [Fabric Capacity Cost], '
        '"jobs", [Job Runs], "failed", [Failed Jobs], "alloc", [Allocated Capacity Cost (proxy)], "items", [Item Count])'}]})
    log(f"{MODEL}: framed; {res['results'][0]['tables'][0]['rows'][0]}")
    return item


def report() -> None:
    from finance_report import S, column, measure, vid, visual
    w = ws("fleet", "dev")
    m = find_item(w, "SemanticModel", MODEL)
    visuals = [
        ("cap_card_avg", "card", 20, 20, 300, 120, {"Values": {"projections": [measure("Utilization", "Avg Utilization")]}},
         "Avg utilization"),
        ("cap_card_peak", "card", 335, 20, 300, 120, {"Values": {"projections": [measure("Utilization", "Peak Utilization")]}},
         "Peak utilization (30 s)"),
        ("cap_card_cost", "card", 650, 20, 300, 120, {"Values": {"projections": [measure("Cost", "Fabric Capacity Cost")]}},
         "Fabric capacity cost (30 days)"),
        ("cap_card_failed", "card", 965, 20, 295, 120, {"Values": {"projections": [measure("Jobs", "Failed Jobs")]}},
         "Failed jobs"),
        ("cap_line_util", "lineChart", 20, 155, 620, 250, {"Category": {"projections": [column("Utilization", "hour")]},
         "Y": {"projections": [measure("Utilization", "Avg Utilization"), measure("Utilization", "Peak Utilization"),
                               measure("Utilization", "Peak Throttling Stage")]}}, "Utilization and throttling by hour"),
        ("cap_bar_workload", "clusteredBarChart", 655, 155, 605, 250, {"Category": {"projections": [column("Workload", "workload")]},
         "Y": {"projections": [measure("Workload", "Billed CU Hours")]}}, "Billed CU hours by workload"),
        ("cap_col_cost", "clusteredColumnChart", 20, 420, 620, 280, {"Category": {"projections": [column("Cost", "date")]},
         "Y": {"projections": [measure("Cost", "Total Cost")]}}, "Daily Azure cost (capacity + Event Hubs)"),
        ("cap_tbl_ws", "pivotTable", 655, 420, 605, 280, {"Rows": {"projections": [column("Workspaces", "workspace_name")]},
         "Values": {"projections": [measure("Items", "Item Count"), measure("Jobs", "Job Runs"), measure("Jobs", "Failed Jobs"),
                                    measure("Jobs", "Job Hours"), measure("Jobs", "Allocated Capacity Cost (proxy)"),
                                    measure("Activities", "Activity Events")]}}, "Workspaces: inventory, jobs, chargeback proxy"),
    ]
    page = vid("cap-page")
    parts = {
        "definition.pbir": {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
                            "version": "4.0", "datasetReference": {"byConnection": {"connectionString": f"semanticmodelid={m['id']}"}}},
        "definition/version.json": {"$schema": f"{S}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"},
        "definition/report.json": {"$schema": f"{S}/report/3.1.0/schema.json",
                                   "themeCollection": {"baseTheme": {"name": "CY25SU12", "type": "SharedResources",
                                                                     "reportVersionAtImport": {"visual": "2.5.0", "report": "3.1.0", "page": "2.3.0"}}},
                                   "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True}},
        "definition/pages/pages.json": {"$schema": f"{S}/pagesMetadata/1.0.0/schema.json", "pageOrder": [page], "activePageName": page},
        f"definition/pages/{page}/page.json": {"$schema": f"{S}/page/2.0.0/schema.json", "name": page,
                                               "displayName": "Capacity", "displayOption": "FitToPage", "height": 720, "width": 1280},
    }
    for v in visuals:
        body = visual(*v)
        parts[f"definition/pages/{page}/visuals/{body['name']}/visual.json"] = body
    defn = [{"path": p, "payload": b64(c), "payloadType": "InlineBase64"} for p, c in parts.items()]
    defn.append({"path": ".platform", "payload": b64(platform("Report", REPORT)), "payloadType": "InlineBase64"})
    rpt = upsert_item(w, "Report", REPORT, defn, "Step 6 (iii): capacity report (Power BI)", definition_format="PBIR")
    log(f"{REPORT}: {len(visuals)} visuals")
    # Render check (PDF; image export is off in the tenant)
    status, _, body = _request("post", f"{PBI}/groups/{w}/reports/{rpt['id']}/ExportTo", {"format": "PDF"}, PBI_RES)
    if status != 202:
        raise SystemExit(f"export failed to start: {status} {body}")
    for _ in range(60):
        time.sleep(5)
        _, _, st = _request("get", f"{PBI}/groups/{w}/reports/{rpt['id']}/exports/{body['id']}", None, PBI_RES)
        if st.get("status") in ("Succeeded", "Failed"):
            break
    if st.get("status") != "Succeeded":
        raise SystemExit(f"export {st.get('status')}: {st.get('error')}")
    import urllib.request
    from common import ROOT
    from fabric_rest import token
    req = urllib.request.Request(f"{PBI}/groups/{w}/reports/{rpt['id']}/exports/{body['id']}/file",
                                 headers={"Authorization": f"Bearer {token(PBI_RES)}"})
    out = ROOT / "data-gen" / "out" / f"{REPORT}.pdf"
    out.write_bytes(urllib.request.urlopen(req, timeout=120).read())
    log(f"rendered -> {out} ({out.stat().st_size:,} bytes)")


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["collect", "model", "report", "all"])
    a = p.parse_args()
    load_env()
    if a.action in ("collect", "all"):
        collect()
    if a.action in ("model", "all"):
        model()
    if a.action in ("report", "all"):
        report()


if __name__ == "__main__":
    main()
