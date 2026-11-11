"""Step 1e: API layer over gold data. Idempotent.

  gql_finance (ce-finance)  API for GraphQL over lh_finance SQL analytics endpoint (gold star schema, read-only,
                            SSO). PII columns (name, email, phone) are deliberately NOT exposed.
  Fleet API (ce-fleet)      the KQL stored functions in eh_fleet, called over the Eventhouse REST endpoint.

  py infra/api_layer.py graphql [--env dev]      # create/update gql_finance from the endpoint's columns
  py infra/api_layer.py test-graphql [--env dev] # run sample queries through the GraphQL endpoint
  py infra/api_layer.py fleet [--env dev]        # call the Fleet API functions (KPIs, offline sites, open alerts)
"""
from __future__ import annotations

import argparse
import json
import time

from common import env, load_env, load_state, log
from fabric_rest import FABRIC, _request, b64, fabric, find_item, kql, tsql, upsert_item
from rti_lab import platform

API = "gql_finance"
PII = {"customer_name", "email", "phone"}
# graphqlType -> (table, primary key)
OBJECTS = {
    "customer": ("gold_dim_customer", "customer_id"),
    "contract": ("gold_dim_contract", "contract_id"),
    "payment": ("gold_fact_payment", "payment_id"),
    "contract_month": ("gold_fact_contract_month", None),
    "calendar_date": ("gold_dim_date", "date_key"),
}
RELATIONSHIPS = {  # source type -> [(field, cardinality, target type, source field, target field)]
    "contract": [("customer", "ManyToOne", "customer", "customer_id", "customer_id"),
                 ("payments", "OneToMany", "payment", "contract_id", "contract_id"),
                 ("months", "OneToMany", "contract_month", "contract_id", "contract_id")],
    "customer": [("contracts", "OneToMany", "contract", "customer_id", "customer_id")],
    "payment": [("contract", "ManyToOne", "contract", "contract_id", "contract_id")],
    "contract_month": [("contract", "ManyToOne", "contract", "contract_id", "contract_id")],
}


def ws(domain: str, e: str) -> str:
    return load_state("workspaces")[f"{env('WORKSPACE_PREFIX')}-{domain}-{e}"]["id"]


def graphql(e: str) -> dict:
    w = ws("finance", e)
    lh = find_item(w, "Lakehouse", "lh_finance")
    props = fabric("get", f"workspaces/{w}/lakehouses/{lh['id']}")["properties"]["sqlEndpointProperties"]
    cols: dict[str, list[str]] = {}
    for table, column in tsql(props["connectionString"], "lh_finance",
                              "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                              "WHERE TABLE_NAME LIKE 'gold[_]%' ORDER BY TABLE_NAME, ORDINAL_POSITION"):
        if not column.startswith("_") and column not in PII:
            cols.setdefault(table, []).append(column)
    objects = []
    for gql_type, (table, pk) in OBJECTS.items():
        if table not in cols:
            raise SystemExit(f"{table} not visible in the SQL endpoint; run finance_lakehouse.py verify first")
        objects.append({
            "graphqlType": gql_type, "sourceObject": f"dbo.{table}", "sourceObjectType": "Table",
            "actions": {"Query": "Enabled", **({"Query_by_pk": "Enabled"} if pk else {})},
            "fieldMappings": {c: c for c in cols[table]},
            "relationships": [{"field": f, "cardinality": card, "targetObject": f"dbo.{OBJECTS[t][0]}",
                               "sourceFields": [sf], "targetFields": [tf]}
                              for f, card, t, sf, tf in RELATIONSHIPS.get(gql_type, [])]})
    definition = {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/graphqlApi/definition/1.0.0/schema.json",
                  "datasources": [{"sourceItemId": props["id"], "sourceWorkspaceId": w,
                                   "sourceType": "SqlAnalyticsEndpoint", "objects": objects}]}
    item = upsert_item(w, "GraphQLApi", API, [
        {"path": "graphql-definition.json", "payload": b64(definition), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("GraphQLApi", API)), "payloadType": "InlineBase64"}],
        "Read-only GraphQL API over finance gold (no PII)")
    log(f"{API}: {len(objects)} types, PII excluded: {sorted(PII)}")
    return item


SAMPLES = {
    "delinquent contracts with customer segment": """
      { contracts(filter: { status: { eq: "Default" } }, first: 5) {
          items { contract_id product monthly_payment site_region customer { region bureau_band_current } } } }""",
    "latest months for one contract": """
      { contract_months(filter: { contract_id: { eq: "CTR-000045" } }, orderBy: { month: DESC }, first: 3) {
          items { month amount_due amount_paid days_past_due dpd_bucket } } }""",
    "PII is not exposed (expect an error)": """{ customers(first: 1) { items { customer_id email } } }""",
}


def test_graphql(e: str) -> None:
    w = ws("finance", e)
    api = find_item(w, "GraphQLApi", API)
    url = f"{FABRIC}/v1/workspaces/{w}/graphqlapis/{api['id']}/graphql"
    for name, q in SAMPLES.items():
        t0 = time.time()
        for _ in range(6):  # the API schema is generated asynchronously after an update
            status, _, body = _request("post", url, {"query": q}, FABRIC)
            if status != 404:
                break
            time.sleep(20)
        errors = body.get("errors")
        data = body.get("data") or {}
        rows = next(iter(data.values()), {}).get("items", []) if data else []
        print(f"[{status}] {name}: {len(rows)} rows in {time.time() - t0:.1f}s"
              + (f" | errors: {errors[0].get('message', '')[:120]}" if errors else ""))
        if rows:
            print("      " + json.dumps(rows[0])[:200])


FLEET_CALLS = {
    "FleetKpis()": "FleetKpis()",
    "FleetOfflineSites(20m)": "FleetOfflineSites(20m) | take 5",
    "FleetOpenAlerts()": "FleetOpenAlerts() | take 5",
    "FleetSiteEnergy(7d) top sites": "FleetSiteEnergy(7d) | summarize kwh = sum(pv_kwh) by site_id, region | top 3 by kwh",
}


def fleet(e: str) -> None:
    w = ws("fleet", e)
    eh = find_item(w, "Eventhouse", "eh_fleet")
    db = find_item(w, "KQLDatabase", "eh_fleet")
    uri = fabric("get", f"workspaces/{w}/eventhouses/{eh['id']}")["properties"]["queryServiceUri"]
    for name, q in FLEET_CALLS.items():
        t0 = time.time()
        rows = kql(uri, db["id"], q)
        print(f"{name}: {len(rows)} rows in {time.time() - t0:.2f}s" + (f" | {json.dumps(rows[0], default=str)[:180]}" if rows else ""))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["graphql", "test-graphql", "fleet"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    a = p.parse_args()
    load_env()
    {"graphql": graphql, "test-graphql": test_graphql, "fleet": fleet}[a.action](a.env)


if __name__ == "__main__":
    main()
