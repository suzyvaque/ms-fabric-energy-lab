"""Step 1d: Finance lakehouse medallion (+ Asset domain master data as a cross-domain source). Idempotent.

  lh_asset   (ce-asset-<env>)    Tables sites/devices/batteries  <- Load-to-Table API from asset master CSVs
  lh_finance (ce-finance-<env>)
    Tables  bronze_customers/contracts/payments  = OneLake shortcuts -> mir_loan_servicing (open mirroring)
            ref_sites                            = OneLake shortcut  -> lh_asset.sites (cross-domain, zero copy)
    Files   landing/credit_bureau/*.csv          = partner file drop (uploaded here via the OneLake API)
    nb_finance_medallion                         = bronze -> silver -> gold (infra/notebooks/nb_finance_medallion.py)

  py infra/finance_lakehouse.py asset | create | run | verify | all   [--env dev]
"""
from __future__ import annotations

import argparse
import json
import re
import time

from common import ROOT, env, load_env, load_state, log
from fabric_rest import FABRIC, _request, b64, fabric, find_item, tsql, upsert_item
from onelake import upload
from rti_lab import platform

DATA = ROOT / "data-gen" / "out"
NOTEBOOK = "nb_finance_medallion"
NB_SOURCE = ROOT / "infra" / "notebooks" / f"{NOTEBOOK}.py"
ASSET_TABLES = {"sites": "sites.csv", "devices": "devices.csv", "batteries": "batteries.csv"}


def ws(domain: str, e: str) -> str:
    return load_state("workspaces")[f"{env('WORKSPACE_PREFIX')}-{domain}-{e}"]["id"]


def lakehouse(ws_id: str, name: str, description: str) -> dict:
    item = upsert_item(ws_id, "Lakehouse", name, None, description)
    for _ in range(30):  # the SQL analytics endpoint is provisioned asynchronously
        props = fabric("get", f"workspaces/{ws_id}/lakehouses/{item['id']}").get("properties", {})
        if props.get("sqlEndpointProperties", {}).get("provisioningStatus") == "Success":
            return {**item, "sql_host": props["sqlEndpointProperties"]["connectionString"]}
        time.sleep(10)
    raise SystemExit(f"{name}: SQL analytics endpoint not ready")


def asset(e: str) -> dict:
    w = ws("asset", e)
    lh = lakehouse(w, "lh_asset", "Contoso asset master (sites, devices, batteries) - Asset domain data product")
    for table, file in ASSET_TABLES.items():
        upload(w, lh["id"], f"Files/landing/asset_master/{file}", (DATA / "asset" / file).read_bytes())
        fabric("post", f"workspaces/{w}/lakehouses/{lh['id']}/tables/{table}/load", {
            "relativePath": f"Files/landing/asset_master/{file}", "pathType": "File", "mode": "Overwrite",
            "recursive": False, "formatOptions": {"format": "Csv", "header": True, "delimiter": ","}})
        log(f"lh_asset.{table} loaded")
    return lh


def shortcut(ws_id: str, lh_id: str, path: str, name: str, target: dict) -> None:
    status, _, _ = _request("get", f"{FABRIC}/v1/workspaces/{ws_id}/items/{lh_id}/shortcuts/{path}/{name}", None, FABRIC)
    if status == 200:
        return
    fabric("post", f"workspaces/{ws_id}/items/{lh_id}/shortcuts", {"path": path, "name": name, "target": target})
    log(f"shortcut {path}/{name} created")


def notebook_ipynb(ws_id: str, lh: dict) -> dict:
    src = NB_SOURCE.read_text(encoding="utf-8")
    chunks = re.split(r"^# %%(.*)$", src, flags=re.M)
    cells = []

    def add(kind: str, text: str, tags: list[str] | None = None) -> None:
        text = text.strip("\n")
        if not text.strip():
            return
        if kind == "markdown":
            text = "\n".join(line[2:] if line.startswith("# ") else line.lstrip("#") for line in text.splitlines())
        cells.append({"cell_type": kind, "metadata": {"tags": tags} if tags else {}, "source": text.splitlines(keepends=True),
                      **({"outputs": [], "execution_count": None} if kind == "code" else {})})

    add("markdown", chunks[0])  # file header
    for marker, body in zip(chunks[1::2], chunks[2::2]):
        # "# %% [parameters]" = Fabric parameter cell (pipeline Notebook activity base parameters override it)
        add("markdown" if "[markdown]" in marker else "code", body, ["parameters"] if "[parameters]" in marker else None)
    return {"nbformat": 4, "nbformat_minor": 5, "cells": cells, "metadata": {
        "kernel_info": {"name": "synapse_pyspark"},
        "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
        "language_info": {"name": "python"},
        "dependencies": {"lakehouse": {"default_lakehouse": lh["id"], "default_lakehouse_name": "lh_finance",
                                       "default_lakehouse_workspace_id": ws_id}}}}


def spark_pool(e: str) -> None:
    """F2 gives Spark 4 vCores (x3 burst). Starter Pool *Medium* nodes need 16 vCores for driver+executor, so
    jobs queue forever (TooManyRequestsForCapacity). A *Small* node pool (4+4 vCores) fits. Workspace setting,
    not in Git -> scripted here."""
    w = ws("finance", e)
    pools = {p["name"] for p in fabric("get", f"workspaces/{w}/spark/pools").get("value", [])}
    if "pool_small" not in pools:
        fabric("post", f"workspaces/{w}/spark/pools", {
            "name": "pool_small", "nodeFamily": "MemoryOptimized", "nodeSize": "Small",
            "autoScale": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 2},
            "dynamicExecutorAllocation": {"enabled": True, "minExecutors": 1, "maxExecutors": 1}})
    fabric("patch", f"workspaces/{w}/spark/settings", {"pool": {"defaultPool": {"name": "pool_small", "type": "Workspace"}}})
    log(f"finance-{e}: default Spark pool = pool_small (Small nodes)")


def landing(e: str) -> None:
    """Partner file drop (data, not in Git): upload credit bureau CSVs to lh_finance Files/landing."""
    w = ws("finance", e)
    lh = find_item(w, "Lakehouse", "lh_finance")
    for f in sorted((DATA / "finance" / "credit_bureau").glob("*.csv")):
        upload(w, lh["id"], f"Files/landing/credit_bureau/{f.name}", f.read_bytes())
    log(f"finance-{e}: credit bureau partner files uploaded to Files/landing/credit_bureau")


def create(e: str) -> None:
    w = ws("finance", e)
    spark_pool(e)
    lh = lakehouse(w, "lh_finance", "Contoso Energy financing (customers, contracts, payments) - medallion")
    mirror = find_item(w, "MirroredDatabase", "mir_loan_servicing")
    if not mirror:
        raise SystemExit("run infra/loan_servicing.py create + snapshot first")
    for t in ("customers", "contracts", "payments"):
        shortcut(w, lh["id"], "Tables", f"bronze_{t}",
                 {"oneLake": {"workspaceId": w, "itemId": mirror["id"], "path": f"Tables/dbo/{t}"}})
    aw = ws("asset", e)
    asset_lh = find_item(aw, "Lakehouse", "lh_asset")
    if not asset_lh:
        raise SystemExit("run: py infra/finance_lakehouse.py asset")
    shortcut(w, lh["id"], "Tables", "ref_sites", {"oneLake": {"workspaceId": aw, "itemId": asset_lh["id"], "path": "Tables/sites"}})
    for f in sorted((DATA / "finance" / "credit_bureau").glob("*.csv")):
        upload(w, lh["id"], f"Files/landing/credit_bureau/{f.name}", f.read_bytes())
    log("credit bureau partner files uploaded to Files/landing/credit_bureau")
    upsert_item(w, "Notebook", NOTEBOOK, [
        {"path": "notebook-content.ipynb", "payload": b64(notebook_ipynb(w, lh)), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("Notebook", NOTEBOOK)), "payloadType": "InlineBase64"}],
        "Finance medallion: bronze shortcuts -> silver -> gold star schema", definition_format="ipynb")
    log(f"{NOTEBOOK} uploaded (default lakehouse lh_finance)")


def run_notebook(e: str, timeout_min: int = 40) -> None:
    w = ws("finance", e)
    nb = find_item(w, "Notebook", NOTEBOOK)
    status, headers, payload = _request("post", f"{FABRIC}/v1/workspaces/{w}/items/{nb['id']}/jobs/instances?jobType=RunNotebook",
                                        None, FABRIC)
    if status != 202:
        raise SystemExit(f"run failed to start: {status} {payload}")
    job_url, t0 = headers["Location"], time.time()
    log("notebook job started (Spark session start on a small capacity can take a few minutes)")
    while time.time() - t0 < timeout_min * 60:
        time.sleep(20)
        _, _, job = _request("get", job_url, None, FABRIC)
        state = job.get("status")
        if state in ("Completed", "Failed", "Cancelled", "Deduped"):
            log(f"notebook job {state} after {time.time() - t0:.0f}s")
            if state != "Completed":
                raise SystemExit(f"notebook job {state}: {json.dumps(job.get('failureReason'))[:800]}")
            return
    raise SystemExit("notebook job timed out")


def _q(host: str, query: str, tries: int = 15) -> list[tuple]:
    """SQL analytics endpoint metadata sync lags new/overwritten Delta tables by up to a few minutes."""
    for attempt in range(tries):
        try:
            return tsql(host, "lh_finance", query)
        except Exception as ex:  # noqa: BLE001
            if attempt == tries - 1:
                raise
            log(f"waiting for SQL endpoint sync ({type(ex).__name__})")
            time.sleep(20)
    return []


def refresh_sql_endpoint(w: str, name: str = "lh_finance") -> None:
    """Background metadata sync can lag or stall (seen on a fresh prod lakehouse); force it."""
    sep = find_item(w, "SQLEndpoint", name)
    status, headers, _ = _request("post", f"{FABRIC}/v1/workspaces/{w}/sqlEndpoints/{sep['id']}/refreshMetadata", {}, FABRIC)
    if status == 202 and headers.get("Location"):
        for _ in range(30):
            time.sleep(10)
            _, _, r = _request("get", headers["Location"], None, FABRIC)
            if r.get("status") in ("Succeeded", "Failed"):
                break


def verify(e: str) -> None:
    w = ws("finance", e)
    lh = lakehouse(w, "lh_finance", "")
    refresh_sql_endpoint(w)
    rows = _q(lh["sql_host"], "SELECT [check], value, passed FROM dbo.gold_dq_results ORDER BY [check]")
    failed = 0
    for check, value, passed in rows:
        print(f"[{'PASS' if passed else 'FAIL'}] {check}: {value:g}")
        failed += not passed
    kpi = _q(lh["sql_host"], """
        SELECT COUNT(DISTINCT contract_id) contracts,
               SUM(CASE WHEN is_delinquent = 1 THEN 1 ELSE 0 END) delinquent_months,
               CAST(100.0 * SUM(amount_paid) / NULLIF(SUM(amount_due), 0) AS decimal(5,2)) collection_rate_pct
        FROM dbo.gold_fact_contract_month""")[0]
    live = _q(lh["sql_host"], "SELECT COUNT(*) FROM dbo.gold_fact_payment WHERE payment_id LIKE 'PAY-LIVE-%'")[0][0]
    print(f"[INFO] contracts={kpi[0]} delinquent contract-months={kpi[1]} collection rate={kpi[2]}% "
          f"live (mirrored) payments in gold={live}")
    if not rows or failed:
        raise SystemExit("verification failed")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["asset", "spark-pool", "landing", "create", "run", "verify", "all"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    a = p.parse_args()
    load_env()
    if a.action == "spark-pool":
        spark_pool(a.env)
    if a.action == "landing":
        landing(a.env)
    if a.action in ("asset", "all"):
        asset(a.env)
    if a.action in ("create", "all"):
        create(a.env)
    if a.action in ("run", "all"):
        run_notebook(a.env)
    if a.action in ("verify", "all"):
        verify(a.env)


if __name__ == "__main__":
    main()
