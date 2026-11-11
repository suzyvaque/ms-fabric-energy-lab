"""Step 8: governance - OneLake security (RLS/CLS, preview), domains, lineage + impact analysis. Idempotent.

  py infra/governance.py roles [--env dev] [--test-principal]  # lh_finance data access roles (not Git-synced: members are
                                                               # tenant objects); --test-principal adds the deploy SP as a
                                                               # Viewer + role member so CI can prove enforcement (dev only)
  py infra/governance.py domains                               # workspace -> domain assignment check
  py infra/governance.py scan [--env dev]                      # Scanner API (admin): what Fabric's own lineage captures
  py infra/governance.py lineage                               # lineage from the Git definitions -> plan/lineage.md (mermaid)
  py infra/governance.py impact --item lh_asset                # downstream impact of changing an item
  py infra/governance.py catalog --search finance              # OneLake catalog search API (what a user can discover)
  py infra/governance.py hygiene                               # prod items without a description (catalog quality)

Enforcement test (as the restricted service principal): .github/workflows/governance-test.yml -> infra/governance_test.py
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path

from common import ROOT, UNMANAGED_ITEMS, env, load_env, log
from fabric_rest import FABRIC, _request, fabric, find_item
from finance_lakehouse import ws

ROLE = "FinanceViewerNoPII"  # letters/digits only
GOLD = ["gold_dim_customer", "gold_dim_contract", "gold_dim_date", "gold_fact_payment", "gold_fact_contract_month"]
PII = {"customer_name", "email", "phone"}
RLS_REGION = "Coastal"


# ------------------------------------------------------------------------------------------------ OneLake security
def group_id(w: str, name_env: str) -> str:
    name = env(name_env)
    for ra in fabric("get", f"workspaces/{w}/roleAssignments")["value"]:
        if ra["principal"]["type"] == "Group" and ra["principal"].get("displayName") == name:
            return ra["principal"]["id"]
    raise SystemExit(f"group {name_env} has no role on the workspace (run infra/fabric_setup.py)")


def ensure_viewer(w: str, principal_id: str, principal_type: str = "ServicePrincipal") -> None:
    for ra in fabric("get", f"workspaces/{w}/roleAssignments")["value"]:
        if ra["principal"]["id"] == principal_id:
            return
    fabric("post", f"workspaces/{w}/roleAssignments", {"principal": {"id": principal_id, "type": principal_type},
                                                       "role": "Viewer"})
    log(f"{principal_type} added as Viewer")


def columns(w: str, lh_name: str, table: str) -> list[str]:
    from fabric_rest import tsql
    host = fabric("get", f"workspaces/{w}/lakehouses/{find_item(w, 'Lakehouse', lh_name)['id']}")["properties"][
        "sqlEndpointProperties"]["connectionString"]
    return [r[0] for r in tsql(host, lh_name, f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{table}' "
                                              "ORDER BY ORDINAL_POSITION")]


def roles(e: str, test_principal: bool) -> None:
    w = ws("finance", e)
    lh = find_item(w, "Lakehouse", "lh_finance")
    members = [{"tenantId": env("TENANT_ID"), "objectId": group_id(w, "ENTRA_GROUP_VIEWERS")}]
    if test_principal:
        if e != "dev":
            raise SystemExit("--test-principal is for dev only")
        sp = env("SP_OBJECT_ID")
        ensure_viewer(w, sp)
        ensure_viewer(ws("asset", e), sp)  # shortcut-security test reads lh_asset.fin_customers
        members.append({"tenantId": env("TENANT_ID"), "objectId": sp})
    allowed_cols = [c for c in columns(w, "lh_finance", "gold_dim_customer") if c not in PII]
    current = fabric("get", f"workspaces/{w}/items/{lh['id']}/dataAccessRoles")["value"]
    keep = [{k: r[k] for k in ("name", "decisionRules", "members")} for r in current if r["name"] != ROLE]
    role = {"name": ROLE, "decisionRules": [{"effect": "Permit", "permission": [
        {"attributeName": "Path", "attributeValueIncludedIn": [f"/Tables/{t}" for t in GOLD]},
        {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]}],
        "constraints": {
            "columns": [{"tablePath": "/Tables/gold_dim_customer", "columnNames": allowed_cols,
                         "columnEffect": "Permit", "columnAction": ["Read"]}],
            "rows": [{"tablePath": "/Tables/gold_dim_contract",
                      "value": f"SELECT * FROM gold_dim_contract WHERE site_region = '{RLS_REGION}'"}]}}],
        "members": {"microsoftEntraMembers": members}}
    status, _, body = _request("put", f"{FABRIC}/v1/workspaces/{w}/items/{lh['id']}/dataAccessRoles",
                               {"value": keep + [role]}, FABRIC)
    if status not in (200, 201):
        raise SystemExit(f"dataAccessRoles {status}: {json.dumps(body)[:800]}")
    log(f"lh_finance ({e}): role {ROLE} = gold tables only, CLS on gold_dim_customer (no {sorted(PII)}), "
        f"RLS gold_dim_contract site_region = '{RLS_REGION}', {len(members)} member(s); kept {[r['name'] for r in keep]}")


# ------------------------------------------------------------------------------------------------ domains
def domains() -> None:
    ds = {d["id"]: d["displayName"] for d in fabric("get", "admin/domains")["domains"]}
    for d in ("fleet", "asset", "finance"):
        for e in ("dev", "prod"):
            info = fabric("get", f"admin/workspaces/{ws(d, e)}")
            print(f"{info['name']}: domain = {ds.get(info.get('domainId'), '-')}")


# ------------------------------------------------------------------------------------------------ Scanner API
def scan(e: str) -> dict:
    pbi = "https://api.powerbi.com/v1.0/myorg/admin/workspaces"
    res_ = "https://analysis.windows.net/powerbi/api"
    ids = [ws(d, e) for d in ("fleet", "asset", "finance")]
    status, _, r = _request("post", f"{pbi}/getInfo?lineage=True&datasourceDetails=True&datasetSchema=False&getArtifactUsers=False",
                            {"workspaces": ids}, res_)
    if status != 202:
        raise SystemExit(f"scanner getInfo {status}: {json.dumps(r)[:300]}")
    for _ in range(30):
        time.sleep(5)
        if _request("get", f"{pbi}/scanStatus/{r['id']}", None, res_)[2].get("status") == "Succeeded":
            break
    res = _request("get", f"{pbi}/scanResult/{r['id']}", None, res_)[2]
    summary = {}
    for wsr in res.get("workspaces", []):
        for key, items in wsr.items():
            if isinstance(items, list) and items and isinstance(items[0], dict):
                rel = sum(1 for i in items if i.get("relations") or i.get("upstreamDatasets") or i.get("upstreamDataflows")
                          or i.get("datasourceUsages"))
                s = summary.setdefault(key, [0, 0])
                s[0] += len(items)
                s[1] += rel
    for k, (n, rel) in sorted(summary.items()):
        print(f"{k:28} items {n:3}  with upstream lineage {rel:3}")
    return res


# ------------------------------------------------------------------------------------------------ repo-derived lineage
GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def lineage_edges() -> list[tuple[str, str, str]]:
    """(upstream, downstream, how) from the item definitions in /workspaces (names only, public-safe)."""
    root = ROOT / "workspaces"
    items = {p.parent.name.split(".")[0]: p.parent for p in root.rglob(".platform")}
    edges: set[tuple[str, str, str]] = set()
    # Fabric-exported ids -> names (dev): logicalId is not the item id, so map through known references by name
    for name, folder in items.items():
        kind = folder.name.split(".")[-1]
        text = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in folder.rglob("*") if p.is_file()}
        blob = "\n".join(text.values())
        if kind == "Lakehouse" and "shortcuts.metadata.json" in text:
            for s in json.loads(text["shortcuts.metadata.json"]):
                tgt = s["target"].get("oneLake", {}).get("path", "")
                edges.add((f"{_owner_of(s, items)}:{tgt.split('/')[-1]}", f"{name}:{s['name']}", "shortcut"))
        if kind == "DataAgent":
            for src in sorted({m for m in re.findall(r"(?:kusto|lakehouse-tables|lakehouse_tables|ontology)-([A-Za-z0-9_]+)",
                                                     " ".join(str(p) for p in folder.rglob("datasource.json")))}):
                edges.add((src, name, "agent source"))
        if kind in ("SemanticModel", "Ontology"):
            lh = {"ce-finance": "lh_finance", "ce-asset": "lh_asset", "ce-fleet": "cap_lh_capacity"}[folder.parent.name]
            edges.add((lh, name, "Direct Lake / binding"))
        if kind == "Report":
            m = re.search(r'"byPath"\s*:\s*\{\s*"path"\s*:\s*"\.\./([A-Za-z0-9_]+)\.SemanticModel"', blob)
            if m:
                edges.add((m.group(1), name, "report"))
        if kind == "GraphQLApi":
            edges.add(("lh_finance", name, "GraphQL"))
        if kind == "Eventstream":
            if "Eventhouse" in blob:
                db = "cap_monitoring" if name.startswith("cap_") else "eh_fleet"
                edges.add((name, db, "eventstream destination"))
        if kind == "KQLDashboard":
            db = "cap_monitoring" if name.startswith("cap_") else "eh_fleet"
            edges.add((db, name, "dashboard"))
        if kind == "DataPipeline" and "TridentNotebook" in blob:
            edges.add((name, "nb_finance_medallion", "pipeline runs"))
        if kind == "Notebook" and name == "nb_finance_medallion":
            edges.add(("mir_loan_servicing", "lh_finance", "notebook bronze to gold"))
            edges.add((name, "lh_finance", "writes silver/gold"))
    # shortcut nodes "item:table" -> collapse to items for the overview graph
    collapsed = set()
    for a, b, how in edges:
        collapsed.add((a.split(":")[0], b.split(":")[0], how))
    return sorted(e for e in collapsed if e[0] != e[1])


def _owner_of(shortcut: dict, items: dict) -> str:
    path = shortcut["target"].get("oneLake", {}).get("path", "")
    table = path.split("/")[-1]
    if table.startswith(("silver_", "gold_alert")):
        return "eh_fleet"
    if table.startswith("gold_"):
        return "lh_finance"
    if table in ("sites", "devices", "batteries"):
        return "lh_asset"
    if path.startswith("Tables/dbo"):
        return "mir_loan_servicing"
    return "unknown"


def lineage() -> None:
    edges = lineage_edges()
    nodes = sorted({n for a, b, _ in edges for n in (a, b)})
    nid = {n: f"n{i}" for i, n in enumerate(nodes)}
    lines = ["# Lineage (generated)", "", "Generated by `py infra/governance.py lineage` from the item definitions in `/workspaces`",
             "(names only). Compare with the portal's lineage view and the Scanner API (`governance.py scan`).", "",
             "```mermaid", "flowchart LR"]
    lines += [f"  {nid[n]}[\"{n}\"]" for n in nodes]
    lines += [f"  {nid[a]} -->|{how}| {nid[b]}" for a, b, how in edges]
    lines += ["```", "", "## Impact analysis examples", ""]
    for item in ("lh_asset", "eh_fleet", "mir_loan_servicing"):
        lines.append(f"- **{item}** \u2192 " + ", ".join(sorted(downstream(item, edges))))
    out = ROOT / "plan" / "lineage.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    log(f"{len(nodes)} nodes, {len(edges)} edges -> {out.relative_to(ROOT)}")


def downstream(item: str, edges: list[tuple[str, str, str]]) -> set[str]:
    graph = defaultdict(set)
    for a, b, _ in edges:
        graph[a].add(b)
    seen, todo = set(), [item]
    while todo:
        for nxt in graph[todo.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen - {item}


def catalog(term: str) -> None:
    for r in fabric("post", "catalog/search", {"search": term})["value"]:
        where = ((r.get("hierarchy") or {}).get("workspace") or {}).get("displayName", "-")
        print(f"{r['type']:18} {r['displayName']:24} {where:18} {(r.get('description') or '')[:60]}")


def hygiene() -> None:
    missing = 0
    for d in ("fleet", "asset", "finance"):
        for i in fabric("get", f"workspaces/{ws(d, 'prod')}/items")["value"]:
            if i["type"] in ("SQLEndpoint", "KQLDatabase") or re.search(UNMANAGED_ITEMS, i["displayName"]):
                continue
            if not (i.get("description") or "").strip():
                missing += 1
                print(f"no description: {d}-prod / {i['type']} / {i['displayName']}")
    print(f"{missing} prod item(s) without a description")


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["roles", "domains", "scan", "lineage", "impact", "catalog", "hygiene"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    p.add_argument("--test-principal", action="store_true")
    p.add_argument("--item")
    p.add_argument("--search", default="finance")
    a = p.parse_args()
    load_env()
    if a.action == "roles":
        roles(a.env, a.test_principal)
    elif a.action == "domains":
        domains()
    elif a.action == "scan":
        scan(a.env)
    elif a.action == "lineage":
        lineage()
    elif a.action == "catalog":
        catalog(a.search)
    elif a.action == "hygiene":
        hygiene()
    else:
        print(sorted(downstream(a.item, lineage_edges())))


if __name__ == "__main__":
    main()
