"""Step 10 A1-A3: access observability matrix - who reaches finance data, through which channel, allowed or denied,
and which log source sees it (OneLake diagnostics, workspace monitoring, activity events/Purview, SQL audit).

  py infra/access_matrix.py run --as admin              # your default az profile
  $env:AZURE_CONFIG_DIR="infra/.state/az-test-user"; py infra/access_matrix.py run --as test-user   # restricted Viewer
  (the deploy SP runs the same probes in CI: .github/workflows/governance-test.yml -> governance_test.py --tag sp)
  py infra/access_matrix.py sqlaudit                    # enable SQL audit on lh_finance / lh_asset SQL endpoints
  py infra/access_matrix.py correlate                   # join probes with every log source -> plan/access-observability-results.md

Probes are written to infra/.state/access_probes.jsonl (gitignored) with UTC timestamps and an outcome. Correlation
matches by time window and identity type; identities are hashed in all outputs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from common import ROOT, load_env, log
from fabric_rest import FABRIC, _request, fabric, find_item, token, tsql

PROBES = ROOT / "infra" / ".state" / "access_probes.jsonl"
PBI_RES = "https://analysis.windows.net/powerbi/api"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
BLOB = "https://onelake.blob.fabric.microsoft.com"
STORAGE = "https://storage.azure.com/"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def ws_by_name(name: str) -> str:
    return next(w["id"] for w in fabric("get", "workspaces")["value"] if w["displayName"] == name)


PROBE_ID = {"id": ""}
GAP_S = 20  # serialize probes so asynchronous log sources can't cross-attribute (plan review #10)


def record(identity: str, channel: str, target: str, ok: bool, detail: str, t0: str) -> None:
    PROBES.parent.mkdir(parents=True, exist_ok=True)
    detail = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "<id>", detail)
    detail = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<upn>", detail)  # no tenant IDs/accounts in outputs (public repo)
    row = {"probe_id": PROBE_ID["id"], "start": t0, "end": now(), "identity": identity, "channel": channel, "target": target,
           "outcome": "allowed" if ok else "denied/error", "detail": detail[:200]}
    with PROBES.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    print(f"{row['outcome']:13} {channel:22} {target:44} {detail[:90]}", flush=True)


def probe(identity: str, channel: str, target: str, fn) -> None:
    import uuid
    PROBE_ID["id"] = f"probe-{uuid.uuid4().hex[:12]}"
    time.sleep(GAP_S)
    t0 = now()
    try:
        out = fn()
        record(identity, channel, target, True, str(out), t0)
    except BaseException as ex:  # noqa: BLE001 - denials surface as many exception types (incl. SystemExit)
        if isinstance(ex, KeyboardInterrupt):
            raise
        record(identity, channel, target, False, str(ex).splitlines()[0] if str(ex) else type(ex).__name__, t0)


def _h() -> dict:
    return {"Authorization": f"Bearer {token(STORAGE)}", "x-ms-version": "2023-11-03",
            "x-ms-client-request-id": PROBE_ID["id"]}


def dfs_list(w: str, item: str, path: str) -> str:
    req = urllib.request.Request(f"{ONELAKE}/{w}?resource=filesystem&recursive=false&directory={item}/{path}", headers=_h())
    with urllib.request.urlopen(req, timeout=60) as r:
        return f"HTTP {r.status} {len(json.loads(r.read()).get('paths', []))} entries"


def blob_read(w: str, item: str, path: str) -> str:
    listing = urllib.request.Request(f"{ONELAKE}/{w}?resource=filesystem&recursive=false&directory={item}/{path}", headers=_h())
    with urllib.request.urlopen(listing, timeout=60) as r:
        first = next(p["name"] for p in json.loads(r.read())["paths"] if p.get("isDirectory") != "true")
    req = urllib.request.Request(f"{BLOB}/{w}/{first}", headers=_h())
    with urllib.request.urlopen(req, timeout=60) as r:
        return f"HTTP {r.status} {len(r.read())} bytes"


def sql(host: str, db: str, q: str) -> str:
    rows = tsql(host, db, f"/* {PROBE_ID['id']} */ {q}")
    return f"{len(rows)} row(s)"


def gql(w: str, api_id: str, query: str) -> str:
    status, _, body = _request("post", f"{FABRIC}/v1/workspaces/{w}/graphqlapis/{api_id}/graphql", {"query": query}, FABRIC)
    if status != 200 or body.get("errors"):
        raise RuntimeError(f"HTTP {status} {json.dumps(body.get('errors') or body)[:150]}")
    return f"HTTP {status} ok"


def dax(w: str, model_id: str, q: str) -> str:
    status, _, body = _request("post", f"https://api.powerbi.com/v1.0/myorg/groups/{w}/datasets/{model_id}/executeQueries",
                               {"queries": [{"query": q}]}, PBI_RES)
    if status != 200:
        raise RuntimeError(f"HTTP {status} {str(body)[:150]}")
    return f"HTTP {status} {body['results'][0]['tables'][0]['rows'][0]}"


def agent(domain_ws: str, name: str, question: str) -> str:
    import warnings
    warnings.filterwarnings("ignore")
    from openai import OpenAI
    item = next(i for i in fabric("get", f"workspaces/{domain_ws}/items?type=DataAgent")["value"] if i["displayName"] == name)
    client = OpenAI(base_url=f"{FABRIC}/v1/workspaces/{domain_ws}/dataagents/{item['id']}/aiassistant/openai", api_key="unused",
                    default_query={"api-version": "2024-05-01-preview"},
                    default_headers={"Authorization": f"Bearer {token(FABRIC)}"})
    a = client.beta.assistants.create(model="not used")
    th = client.beta.threads.create()
    client.beta.threads.messages.create(thread_id=th.id, role="user", content=question)
    run = client.beta.threads.runs.create(thread_id=th.id, assistant_id=a.id)
    t0 = time.time()
    while run.status in ("queued", "in_progress") and time.time() - t0 < 240:
        time.sleep(3)
        run = client.beta.threads.runs.retrieve(thread_id=th.id, run_id=run.id)
    msgs = client.beta.threads.messages.list(thread_id=th.id, order="desc")  # endpoint may reuse a per-user thread:
    last = next((m for m in msgs if m.role == "assistant" and getattr(m, "run_id", run.id) == run.id), None)  # newest only
    ans = " ".join(c.text.value for c in (last.content if last else []) if c.type == "text")
    if run.status != "completed":
        raise RuntimeError(f"run {run.status}: {ans[:120]}")
    return f"{run.status}: {ans[:150]}"


def run_probes(identity: str, prefix: str = "ce") -> None:
    fin, asset = ws_by_name(f"{prefix}-finance-dev"), ws_by_name(f"{prefix}-asset-dev")
    lh = next(i for i in fabric("get", f"workspaces/{fin}/items?type=Lakehouse")["value"] if i["displayName"] == "lh_finance")
    alh = next(i for i in fabric("get", f"workspaces/{asset}/items?type=Lakehouse")["value"] if i["displayName"] == "lh_asset")
    host = fabric("get", f"workspaces/{fin}/lakehouses/{lh['id']}")["properties"]["sqlEndpointProperties"]["connectionString"]
    ahost = fabric("get", f"workspaces/{asset}/lakehouses/{alh['id']}")["properties"]["sqlEndpointProperties"]["connectionString"]
    items = {i["displayName"]: i for i in fabric("get", f"workspaces/{fin}/items")["value"]}
    log(f"probing as {identity}")
    p = lambda ch, tgt, fn: probe(identity, ch, tgt, fn)  # noqa: E731
    p("onelake-dfs-list", "lh_finance/gold_dim_date", lambda: dfs_list(fin, lh["id"], "Tables/gold_dim_date"))
    p("onelake-dfs-list", "lh_finance/gold_dim_customer (CLS)", lambda: dfs_list(fin, lh["id"], "Tables/gold_dim_customer"))
    p("onelake-dfs-list", "lh_finance/silver_customers (out of role)", lambda: dfs_list(fin, lh["id"], "Tables/silver_customers"))
    p("onelake-blob-read", "lh_finance/gold_dim_date file", lambda: blob_read(fin, lh["id"], "Tables/gold_dim_date"))
    p("onelake-blob-read", "lh_finance/silver_customers file", lambda: blob_read(fin, lh["id"], "Tables/silver_customers"))
    p("onelake-dfs-list", "lh_asset/fin_customers (shortcut)", lambda: dfs_list(asset, alh["id"], "Tables/fin_customers"))
    p("sql", "lh_finance gold_dim_contract", lambda: sql(host, "lh_finance", "SELECT COUNT(*) FROM dbo.gold_dim_contract"))
    p("sql", "lh_finance gold_dim_customer.email (PII)", lambda: sql(host, "lh_finance", "SELECT TOP 1 email FROM dbo.gold_dim_customer"))
    p("sql", "lh_finance silver_customers", lambda: sql(host, "lh_finance", "SELECT TOP 1 * FROM dbo.silver_customers"))
    p("sql-via-shortcut", "lh_asset fin_customers.email (PII)", lambda: sql(ahost, "lh_asset", "SELECT TOP 1 email FROM dbo.fin_customers"))
    if "gql_finance" in items:
        p("graphql", "gql_finance contracts", lambda: gql(fin, items["gql_finance"]["id"],
                                                          "{ contracts(first: 3) { items { contract_id product } } }"))
    if "sm_finance" in items:
        p("dax", "sm_finance Collection Rate", lambda: dax(fin, items["sm_finance"]["id"], 'EVALUATE ROW("c", [Collection Rate])'))
    p("data-agent", "da_finance (collection rate)", lambda: agent(fin, "da_finance", "What is the collection rate by product?"))
    p("data-agent", "da_finance (PII ask)", lambda: agent(fin, "da_finance", "List three customer email addresses."))
    p("data-agent", "da_contoso_360 (cross-domain)", lambda: agent(asset, "da_contoso_360",
                                                                     "How many contracts are in Default status and do their sites have alerts?"))
    if identity == "admin":
        p("pipeline (workload)", "pl_finance_daily normal run", lambda: run_pipeline(fin))


def run_obs(identity: str, prefix: str) -> None:
    """Isolated obs lab (separate capacity): data = lh_obs_finance (role FinanceViewerNoPII, no SQL DENYs),
    consumer = lh_obs_hub (cross-workspace shortcuts), agent da_obs_finance, Spark notebook nb_obs_reader."""
    data, cons = ws_by_name(f"{prefix}-obs-data"), ws_by_name(f"{prefix}-obs-consumer")
    lh = next(i for i in fabric("get", f"workspaces/{data}/items?type=Lakehouse")["value"] if i["displayName"] == "lh_obs_finance")
    hub = next(i for i in fabric("get", f"workspaces/{cons}/items?type=Lakehouse")["value"] if i["displayName"] == "lh_obs_hub")
    host = fabric("get", f"workspaces/{data}/lakehouses/{lh['id']}")["properties"]["sqlEndpointProperties"]["connectionString"]
    hhost = fabric("get", f"workspaces/{cons}/lakehouses/{hub['id']}")["properties"]["sqlEndpointProperties"]["connectionString"]
    log(f"probing obs lab as {identity}")
    p = lambda ch, tgt, fn: probe(identity, ch, tgt, fn)  # noqa: E731
    p("onelake-dfs-list", "obs_finance/gold_dim_date", lambda: dfs_list(data, lh["id"], "Tables/gold_dim_date"))
    p("onelake-dfs-list", "obs_finance/gold_dim_customer (CLS)", lambda: dfs_list(data, lh["id"], "Tables/gold_dim_customer"))
    p("onelake-dfs-list", "obs_finance/silver_customers (out of role)", lambda: dfs_list(data, lh["id"], "Tables/silver_customers"))
    p("onelake-blob-read", "obs_finance/gold_dim_date file", lambda: blob_read(data, lh["id"], "Tables/gold_dim_date"))
    p("onelake-blob-read", "obs_finance/silver_customers file", lambda: blob_read(data, lh["id"], "Tables/silver_customers"))
    p("onelake-dfs-list", "obs_hub/fin_customers (shortcut)", lambda: dfs_list(cons, hub["id"], "Tables/fin_customers"))
    p("sql", "obs_finance gold_dim_contract regions (RLS)", lambda: sql_rows(host, "lh_obs_finance",
        "SELECT site_region, COUNT(*) FROM dbo.gold_dim_contract GROUP BY site_region"))
    p("sql", "obs_finance gold_dim_customer.segment (allowed col)", lambda: sql(host, "lh_obs_finance",
        "SELECT TOP 3 segment FROM dbo.gold_dim_customer"))
    p("sql", "obs_finance gold_dim_customer.email (PII, CLS)", lambda: sql(host, "lh_obs_finance",
        "SELECT TOP 1 email FROM dbo.gold_dim_customer"))
    p("sql", "obs_finance silver_customers (out of role)", lambda: sql(host, "lh_obs_finance", "SELECT TOP 1 * FROM dbo.silver_customers"))
    p("sql-via-shortcut", "obs_hub fin_customers.email (PII)", lambda: sql(hhost, "lh_obs_hub", "SELECT TOP 1 email FROM dbo.fin_customers"))
    p("sql-via-shortcut", "obs_hub fin_contracts regions (RLS)", lambda: sql_rows(hhost, "lh_obs_hub",
        "SELECT site_region, COUNT(*) FROM dbo.fin_contracts GROUP BY site_region"))
    p("data-agent", "da_obs_finance (collection rate)", lambda: agent(data, "da_obs_finance", "What is the collection rate by product?"))
    p("data-agent", "da_obs_finance (PII ask)", lambda: agent(data, "da_obs_finance", "List three customer email addresses."))
    if identity == "admin":
        p("spark-notebook (workload)", "nb_obs_reader (shortcut + direct + out-of-role)", lambda: run_notebook(cons))


def sql_rows(host: str, db: str, q: str) -> str:
    rows = tsql(host, db, f"/* {PROBE_ID['id']} */ {q}")
    return json.dumps([list(r) for r in rows], default=str)


def run_notebook(cons: str) -> str:
    nb = find_item(cons, "Notebook", "nb_obs_reader")
    status, headers, body = _request("post", f"{FABRIC}/v1/workspaces/{cons}/items/{nb['id']}/jobs/instances?jobType=RunNotebook",
                                     None, FABRIC)
    for _ in range(90):
        time.sleep(20)
        _, _, j = _request("get", headers["Location"], None, FABRIC)
        if j.get("status") in ("Completed", "Failed", "Cancelled"):
            if j["status"] != "Completed":
                raise RuntimeError(f"{j['status']} {json.dumps(j.get('failureReason'))[:150]}")
            return f"Completed {j.get('startTimeUtc')} -> {j.get('endTimeUtc')}"
    raise RuntimeError("timeout")


def run_pipeline(fin: str) -> str:
    pl = find_item(fin, "DataPipeline", "pl_finance_daily")
    status, headers, body = _request("post", f"{FABRIC}/v1/workspaces/{fin}/items/{pl['id']}/jobs/instances?jobType=Pipeline",
                                     {"executionData": {"parameters": {"failure_mode": "none"}}}, FABRIC)
    for _ in range(90):
        time.sleep(20)
        _, _, j = _request("get", headers["Location"], None, FABRIC)
        if j.get("status") in ("Completed", "Failed", "Cancelled"):
            if j["status"] != "Completed":
                raise RuntimeError(j["status"])
            return f"Completed {j.get('startTimeUtc')} -> {j.get('endTimeUtc')}"
    raise RuntimeError("timeout")


# ------------------------------------------------------------------------------ SQL audit (SQL analytics endpoints)
def sqlaudit() -> None:
    from finance_lakehouse import ws
    for domain, name in (("finance", "lh_finance"), ("asset", "lh_asset")):
        w = ws(domain, "dev")
        sep = find_item(w, "SQLEndpoint", name)
        body = {"state": "Enabled", "retentionDays": 30,
                "auditActionsAndGroups": ["SUCCESSFUL_DATABASE_AUTHENTICATION_GROUP", "FAILED_DATABASE_AUTHENTICATION_GROUP",
                                          "BATCH_COMPLETED_GROUP", "SCHEMA_OBJECT_ACCESS_GROUP"]}
        for kind in ("sqlEndpoints", "warehouses"):
            status, _, r = _request("patch", f"{FABRIC}/v1/workspaces/{w}/{kind}/{sep['id']}/settings/sqlAudit", body, FABRIC)
            log(f"{name}: sqlAudit via {kind} -> {status} {json.dumps(r)[:200]}")
            if status in (200, 202):
                break


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["run", "sqlaudit", "correlate"])
    p.add_argument("--as", dest="identity", default="admin")
    p.add_argument("--profile", choices=["dev", "obs"], default="dev")
    a = p.parse_args()
    if os.path.exists(ROOT / ".env"):
        load_env()
    if a.action == "run":
        prefix = os.environ.get("WORKSPACE_PREFIX", "ce")
        (run_obs if a.profile == "obs" else run_probes)(a.identity, prefix)
    elif a.action == "sqlaudit":
        sqlaudit()
    else:
        from access_correlate import correlate
        correlate(a.profile)


if __name__ == "__main__":
    main()
