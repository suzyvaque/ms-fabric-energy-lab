"""Step 4: Direct Lake semantic model (TMDL) over lh_finance gold + KPI measures. Idempotent.

  sm_finance   Direct Lake (on SQL analytics endpoint) model: Date, Customer, Contract, Payment, Contract Month.
               PII columns are not part of the model. Measures = finance KPIs; "Delinquency Rate (30+ DPD)" is the
               KPI proposed for certification (endorsement has no public API -> portal checklist).

  py infra/semantic_model.py create  [--env dev]   # build TMDL from the SQL endpoint schema, create/update, refresh
  py infra/semantic_model.py verify  [--env dev]   # DAX (executeQueries) vs T-SQL on the same lakehouse
"""
from __future__ import annotations

import argparse
import time
import uuid

from common import env, load_env, load_state, log
from fabric_rest import _request, b64, fabric, find_item, token, tsql, upsert_item
from rti_lab import platform

MODEL = "sm_finance"
PBI = "https://api.powerbi.com/v1.0/myorg"
PII = {"customer_name", "email", "phone"}
TABLES = {  # model table -> (lakehouse table, key columns not to summarize)
    "Date": ("gold_dim_date", {"date_key", "year", "quarter", "month"}),
    "Customer": ("gold_dim_customer", {"customer_id", "bureau_score_current"}),
    "Contract": ("gold_dim_contract", {"contract_id", "customer_id", "site_id", "term_months"}),
    "Payment": ("gold_fact_payment", {"payment_id", "contract_id", "customer_id", "installment_no", "due_date_key",
                                      "paid_date_key", "days_late"}),
    "Contract Month": ("gold_fact_contract_month", {"contract_id", "month_key", "max_days_late", "has_unpaid",
                                                  "days_past_due"}),
}
RELATIONSHIPS = [  # (from table, from col, to table, to col)
    ("Payment", "contract_id", "Contract", "contract_id"),
    ("Payment", "due_date_key", "Date", "date_key"),
    ("Contract Month", "contract_id", "Contract", "contract_id"),
    ("Contract Month", "month_key", "Date", "date_key"),
    ("Contract", "customer_id", "Customer", "customer_id"),
]
MEASURES = {  # table -> [(name, DAX, format, description)]
    "Contract Month": [
        ("Amount Due", "SUM('Contract Month'[amount_due])", "\\$#,0", "Scheduled installments (USD)."),
        ("Amount Paid", "SUM('Contract Month'[amount_paid])", "\\$#,0", "Collected installments (USD)."),
        ("Collection Rate", "DIVIDE([Amount Paid], [Amount Due])", "0.0%", "Amount paid / amount due."),
        ("Contracts Billed", "CALCULATE(DISTINCTCOUNT('Contract Month'[contract_id]), 'Contract Month'[amount_due] > 0)", "#,0",
         "Contracts with an installment due in the period."),
        ("Delinquent Contracts", "CALCULATE(DISTINCTCOUNT('Contract Month'[contract_id]), 'Contract Month'[is_delinquent] = TRUE()) + 0",
         "#,0", "Contracts 30+ days past due at month end."),
        ("Delinquency Rate (30+ DPD)", "DIVIDE([Delinquent Contracts], [Contracts Billed])", "0.0%",
         "CERTIFIED KPI. Share of billed contracts that are 30+ days past due at month end (DPD counted from the first unpaid "
         "installment of the current unpaid streak). Owner: Finance domain. Source: lh_finance.gold_fact_contract_month."),
        ("90+ DPD Contracts", "CALCULATE(DISTINCTCOUNT('Contract Month'[contract_id]), 'Contract Month'[dpd_bucket] = \"90+\") + 0", "#,0",
         "Contracts 90+ days past due at month end."),
        ("90+ DPD Rate", "DIVIDE([90+ DPD Contracts], [Contracts Billed])", "0.0%", "Share of billed contracts 90+ days past due."),
    ],
    "Payment": [
        ("Payments", "COUNTROWS(Payment)", "#,0", "Number of installments (deduplicated)."),
        ("On-time Payment Rate", "DIVIDE(CALCULATE(COUNTROWS(Payment), Payment[status] = \"OnTime\"), [Payments])", "0.0%",
         "Share of installments paid on or before the due date."),
        ("Avg Days Late", "AVERAGEX(FILTER(Payment, Payment[days_late] > 0), Payment[days_late])", "0.0",
         "Average days late for late payments."),
    ],
    "Contract": [
        ("Portfolio Principal", "SUM(Contract[principal])", "\\$#,0", "Financed principal (USD)."),
        ("Contracts", "COUNTROWS(Contract)", "#,0", "Number of financing contracts."),
    ],
}
SQL_TYPES = {"varchar": "string", "nvarchar": "string", "char": "string", "int": "int64", "bigint": "int64",
             "smallint": "int64", "decimal": "decimal", "numeric": "decimal", "float": "double", "real": "double",
             "bit": "boolean", "date": "dateTime", "datetime2": "dateTime", "datetime": "dateTime"}


def ws(e: str) -> str:
    return load_state("workspaces")[f"{env('WORKSPACE_PREFIX')}-finance-{e}"]["id"]


def tag(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "contoso-energy-lab/sm/" + "/".join(parts)))


def q(name: str) -> str:
    return f"'{name}'" if " " in name else name


def tmdl(e: str) -> list[tuple[str, str]]:
    return tmdl_for(ws(e), "lh_finance", TABLES, RELATIONSHIPS, MEASURES, date_table="Date")


def tmdl_for(w: str, lh_name: str, tables: dict, relationships: list, measures: dict,
             date_table: str | None = None, exclude: set[str] = PII) -> list[tuple[str, str]]:
    """Direct Lake (SQL endpoint) TMDL from the live lakehouse schema. Reused by step 6 (capacity model)."""
    lh = find_item(w, "Lakehouse", lh_name)
    sep = fabric("get", f"workspaces/{w}/lakehouses/{lh['id']}")["properties"]["sqlEndpointProperties"]
    cols: dict[str, list[tuple[str, str]]] = {}
    for t, c, typ in tsql(sep["connectionString"], lh_name,
                          "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                          "WHERE TABLE_SCHEMA = 'dbo' ORDER BY TABLE_NAME, ORDINAL_POSITION"):
        cols.setdefault(t, []).append((c, typ))
    files = [
        ("definition/database.tmdl", "database\n\tcompatibilityLevel: 1604\n"),
        ("definition/model.tmdl", "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
                                  "\tdiscourageImplicitMeasures\n\tsourceQueryCulture: en-US\n\n"
         + "".join(f"ref table {q(t)}\n" for t in tables)),
        ("definition/expressions.tmdl",
         "expression DatabaseQuery =\n\t\tlet\n"
         f"\t\t    database = Sql.Database(\"{sep['connectionString']}\", \"{sep['id']}\")\n"
         "\t\tin\n\t\t    database\n"
         f"\tlineageTag: {tag('expr')}\n\n\tannotation PBI_IncludeFutureArtifacts = False\n"),
    ]
    for model_table, (lh_table, keys) in tables.items():
        if lh_table not in cols:
            raise SystemExit(f"{lh_table} missing in the {lh_name} SQL endpoint")
        lines = [f"table {q(model_table)}", f"\tlineageTag: {tag(model_table)}", f"\tsourceLineageTag: [dbo].[{lh_table}]"]
        if model_table == date_table:
            lines.append("\tdataCategory: Time")
        lines.append("")
        for name, dax, fmt, desc in measures.get(model_table, []):
            lines += [f"\t/// {desc}", f"\tmeasure '{name}' = {dax}", f"\t\tformatString: {fmt}",
                      f"\t\tlineageTag: {tag(model_table, 'm', name)}", ""]
        for c, typ in cols[lh_table]:
            if c.startswith("_") or c in exclude:
                continue
            dt = SQL_TYPES.get(typ, "string")
            summarize = "none" if (c in keys or dt in ("string", "dateTime", "boolean")) else "sum"
            lines += [f"\tcolumn {c}", f"\t\tdataType: {dt}"]
            if model_table == date_table and c == "date":
                lines.append("\t\tisKey")
            if dt == "dateTime":
                lines.append("\t\tformatString: yyyy-mm-dd")
            lines += [f"\t\tlineageTag: {tag(model_table, c)}", f"\t\tsourceLineageTag: {c}", f"\t\tsummarizeBy: {summarize}",
                      f"\t\tsourceColumn: {c}", ""]
        lines += [f"\tpartition {q(model_table)} = entity", "\t\tmode: directLake", "\t\tsource",
                  f"\t\t\tentityName: {lh_table}", "\t\t\tschemaName: dbo", "\t\t\texpressionSource: DatabaseQuery", ""]
        files.append((f"definition/tables/{model_table}.tmdl", "\n".join(lines)))
    rel = []
    for ft, fc, tt, tc in relationships:
        rel += [f"relationship {tag('rel', ft, fc)}", f"\tfromColumn: {q(ft)}.{fc}", f"\ttoColumn: {q(tt)}.{tc}", ""]
    files.append(("definition/relationships.tmdl", "\n".join(rel)))
    return files


def pbi(method: str, path: str, body: dict | None = None) -> tuple[int, dict, dict]:
    return _request(method, f"{PBI}/{path}", body, "https://analysis.windows.net/powerbi/api")


def refresh(e: str) -> None:
    """Frame the Direct Lake model (prod after a fabric-cicd deploy, or after gold is rebuilt)."""
    w = ws(e)
    item = find_item(w, "SemanticModel", MODEL)
    status, _, body = pbi("post", f"groups/{w}/datasets/{item['id']}/refreshes", {"type": "full"})
    if status not in (200, 202):
        raise SystemExit(f"refresh failed to start: {status} {body}")
    st, r = None, {}
    for _ in range(40):
        time.sleep(10)
        _, _, r = pbi("get", f"groups/{w}/datasets/{item['id']}/refreshes?$top=1")
        st = r.get("value", [{}])[0].get("status")
        if st in ("Completed", "Failed", "Disabled", "Cancelled"):
            break
    if st != "Completed":
        raise SystemExit(f"refresh {st}: {r.get('value', [{}])[0].get('serviceExceptionJson')}")
    log(f"{MODEL} ({e}): Direct Lake framed")


def create(e: str) -> dict:
    w = ws(e)
    parts = [{"path": p, "payload": b64(content), "payloadType": "InlineBase64"} for p, content in tmdl(e)]
    parts.append({"path": "definition.pbism", "payload": b64({"version": "4.2", "settings": {}}), "payloadType": "InlineBase64"})
    parts.append({"path": ".platform", "payload": b64(platform("SemanticModel", MODEL)), "payloadType": "InlineBase64"})
    item = upsert_item(w, "SemanticModel", MODEL, parts, "Finance KPIs (Direct Lake on lh_finance gold)",
                       definition_format="TMDL")
    refresh(e)
    log(f"{MODEL}: {len(TABLES)} tables, {sum(len(v) for v in MEASURES.values())} measures")
    return item


def dax(e: str, query: str) -> list[dict]:
    w = ws(e)
    item = find_item(w, "SemanticModel", MODEL)
    status, _, body = pbi("post", f"groups/{w}/datasets/{item['id']}/executeQueries",
                          {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}})
    if status != 200:
        raise SystemExit(f"DAX failed {status}: {str(body)[:500]}")
    return body["results"][0]["tables"][0]["rows"]


def verify(e: str) -> None:
    w = ws(e)
    lh = find_item(w, "Lakehouse", "lh_finance")
    host = fabric("get", f"workspaces/{w}/lakehouses/{lh['id']}")["properties"]["sqlEndpointProperties"]["connectionString"]
    m = dax(e, 'EVALUATE ROW("collection", [Collection Rate], "delinq", [Delinquency Rate (30+ DPD)], '
               '"billed", [Contracts Billed], "ontime", [On-time Payment Rate], "principal", [Portfolio Principal])')[0]
    s = tsql(host, "lh_finance", """
        SELECT CAST(SUM(amount_paid) AS float) / SUM(amount_due),
               CAST(COUNT(DISTINCT CASE WHEN is_delinquent = 1 THEN contract_id END) AS float)
                 / COUNT(DISTINCT CASE WHEN amount_due > 0 THEN contract_id END),
               COUNT(DISTINCT CASE WHEN amount_due > 0 THEN contract_id END),
               (SELECT CAST(SUM(CASE WHEN status = 'OnTime' THEN 1 ELSE 0 END) AS float) / COUNT(*) FROM dbo.gold_fact_payment),
               (SELECT CAST(SUM(principal) AS float) FROM dbo.gold_dim_contract)
        FROM dbo.gold_fact_contract_month""")[0]
    names = ["collection", "delinq", "billed", "ontime", "principal"]
    bad = 0
    for i, n in enumerate(names):
        dv, sv = float(m[f"[{n}]"]), float(s[i])
        ok = abs(dv - sv) <= max(1e-6, abs(sv) * 1e-6)
        bad += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {n}: DAX={dv:.6g} SQL={sv:.6g}")
    by_product = dax(e, "EVALUATE SUMMARIZECOLUMNS(Contract[product], \"collection\", [Collection Rate], "
                        "\"delinq\", [Delinquency Rate (30+ DPD)]) ORDER BY Contract[product]")
    for r in by_product:
        print(f"   {r['Contract[product]']:6} collection={r['[collection]']:.4f} delinquency={r['[delinq]'] or 0:.4f}")
    if bad:
        raise SystemExit("semantic model does not match the lakehouse")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "refresh", "verify"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    a = p.parse_args()
    load_env()
    {"create": create, "refresh": refresh, "verify": verify}[a.action](a.env)


if __name__ == "__main__":
    main()
