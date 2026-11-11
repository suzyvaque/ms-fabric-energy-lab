"""Step 4b: Power BI report (PBIR) on sm_finance + render check via Export-to-file. Idempotent.

  py infra/finance_report.py create [--env dev]    # rpt_finance_kpis bound to sm_finance (by semantic model id)
  py infra/finance_report.py export [--env dev]    # render to PDF (data-gen/out/rpt_finance_kpis-<env>.pdf) = visual smoke test
                                                   # (PDF, not PNG: tenant setting ExportToImage stays off)
"""
from __future__ import annotations

import argparse
import time
import uuid

from common import ROOT, env, load_env, load_state, log
from fabric_rest import _request, b64, find_item, upsert_item
from rti_lab import platform

REPORT = "rpt_finance_kpis"
PBI = "https://api.powerbi.com/v1.0/myorg"
PBI_RES = "https://analysis.windows.net/powerbi/api"
S = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"


def ws(e: str) -> str:
    return load_state("workspaces")[f"{env('WORKSPACE_PREFIX')}-finance-{e}"]["id"]


def vid(name: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"contoso-energy-lab/rpt/{name}").hex[:20]


def measure(table: str, name: str) -> dict:
    return {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}},
            "queryRef": f"{table}.{name}", "nativeQueryRef": name}


def column(table: str, name: str) -> dict:
    return {"field": {"Column": {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}},
            "queryRef": f"{table}.{name}", "nativeQueryRef": name, "active": True}


def visual(name: str, vtype: str, x: int, y: int, w: int, h: int, state: dict, title: str) -> dict:
    return {"$schema": f"{S}/visualContainer/2.0.0/schema.json", "name": vid(name),
            "position": {"x": x, "y": y, "z": 1000, "height": h, "width": w, "tabOrder": 1000},
            "visual": {"visualType": vtype, "query": {"queryState": state},
                       "visualContainerObjects": {"title": [{"properties": {
                           "show": {"expr": {"Literal": {"Value": "true"}}},
                           "text": {"expr": {"Literal": {"Value": f"'{title}'"}}}}}]},
                       "drillFilterOtherVisuals": True}}


VISUALS = [
    ("card_collection", "card", 20, 20, 300, 130, {"Values": {"projections": [measure("Contract Month", "Collection Rate")]}},
     "Collection rate"),
    ("card_delinq", "card", 335, 20, 300, 130,
     {"Values": {"projections": [measure("Contract Month", "Delinquency Rate (30+ DPD)")]}}, "Delinquency rate (30+ DPD) - certified KPI"),
    ("card_90", "card", 650, 20, 300, 130, {"Values": {"projections": [measure("Contract Month", "90+ DPD Rate")]}}, "90+ DPD rate"),
    ("card_principal", "card", 965, 20, 295, 130, {"Values": {"projections": [measure("Contract", "Portfolio Principal")]}},
     "Portfolio principal"),
    ("line_collection", "lineChart", 20, 165, 620, 260,
     {"Category": {"projections": [column("Date", "year_month")]},
      "Y": {"projections": [measure("Contract Month", "Collection Rate"), measure("Contract Month", "Delinquency Rate (30+ DPD)")]}},
     "Collection and delinquency by month"),
    ("bar_product", "clusteredBarChart", 655, 165, 605, 260,
     {"Category": {"projections": [column("Contract", "product")]},
      "Y": {"projections": [measure("Contract Month", "Delinquency Rate (30+ DPD)")]}}, "Delinquency rate by product"),
    ("table_region", "pivotTable", 20, 440, 1240, 260,
     {"Rows": {"projections": [column("Contract", "site_region")]},
      "Values": {"projections": [measure("Contract", "Contracts"),
                                 measure("Contract", "Portfolio Principal"), measure("Contract Month", "Collection Rate"),
                                 measure("Contract Month", "Delinquency Rate (30+ DPD)"), measure("Payment", "On-time Payment Rate")]}},
     "Portfolio by region"),
]


def create(e: str) -> dict:
    w = ws(e)
    model = find_item(w, "SemanticModel", "sm_finance")
    if not model:
        raise SystemExit("run infra/semantic_model.py create first")
    page = vid("page-kpis")
    parts = {
        "definition.pbir": {"$schema": f"{S}Properties/2.0.0/schema.json".replace("definitionProperties", "definitionProperties"),
                            "version": "4.0", "datasetReference": {"byConnection": {"connectionString": f"semanticmodelid={model['id']}"}}},
        "definition/version.json": {"$schema": f"{S}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"},
        "definition/report.json": {"$schema": f"{S}/report/3.1.0/schema.json",
                                   "themeCollection": {"baseTheme": {"name": "CY25SU12", "type": "SharedResources",
                                                                     "reportVersionAtImport": {"visual": "2.5.0", "report": "3.1.0", "page": "2.3.0"}}},
                                   "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True}},
        "definition/pages/pages.json": {"$schema": f"{S}/pagesMetadata/1.0.0/schema.json", "pageOrder": [page], "activePageName": page},
        f"definition/pages/{page}/page.json": {"$schema": f"{S}/page/2.0.0/schema.json", "name": page,
                                               "displayName": "Finance KPIs", "displayOption": "FitToPage", "height": 720, "width": 1280},
    }
    for v in VISUALS:
        body = visual(*v)
        parts[f"definition/pages/{page}/visuals/{body['name']}/visual.json"] = body
    parts["definition.pbir"]["$schema"] = "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json"
    defn = [{"path": p, "payload": b64(c), "payloadType": "InlineBase64"} for p, c in parts.items()]
    defn.append({"path": ".platform", "payload": b64(platform("Report", REPORT)), "payloadType": "InlineBase64"})
    item = upsert_item(w, "Report", REPORT, defn, "Finance KPIs (Direct Lake)", definition_format="PBIR")
    log(f"{REPORT}: 1 page, {len(VISUALS)} visuals, bound to sm_finance")
    return item


def export(e: str) -> None:
    w = ws(e)
    rpt = find_item(w, "Report", REPORT)
    status, _, body = _request("post", f"{PBI}/groups/{w}/reports/{rpt['id']}/ExportTo", {"format": "PDF"}, PBI_RES)
    if status != 202:
        raise SystemExit(f"export failed to start: {status} {body}")
    exp_id = body["id"]
    for _ in range(60):
        time.sleep(5)
        _, _, st = _request("get", f"{PBI}/groups/{w}/reports/{rpt['id']}/exports/{exp_id}", None, PBI_RES)
        if st.get("status") in ("Succeeded", "Failed"):
            break
    if st.get("status") != "Succeeded":
        raise SystemExit(f"export {st.get('status')}: {st.get('error')}")
    import urllib.request
    from fabric_rest import token
    req = urllib.request.Request(f"{PBI}/groups/{w}/reports/{rpt['id']}/exports/{exp_id}/file",
                                 headers={"Authorization": f"Bearer {token(PBI_RES)}"})
    out = ROOT / "data-gen" / "out" / f"{REPORT}-{e}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(urllib.request.urlopen(req, timeout=120).read())
    log(f"rendered -> {out} ({out.stat().st_size:,} bytes)")


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "export"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    a = p.parse_args()
    load_env()
    create(a.env) if a.action == "create" else export(a.env)


if __name__ == "__main__":
    main()
