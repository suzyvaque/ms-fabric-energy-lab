"""Step 11 live4: the same frozen hub snapshot in four stores, on a DEDICATED capacity. Dev only, idempotent.

live4 runs on its own capacity (LIVE4_CAPACITY_NAME, default contosoenergylablive4) and workspace (<prefix>-live4-dev),
so it never shares CUs with the evaluation loop on FABRIC_CAPACITY_NAME. All items are lab_ (not Git-tracked/deployed).

  Store                    Item                                   Agent queries
  Lakehouse                lab_lh_store (physical Delta tables)   T-SQL (SQL analytics endpoint)
  SQL database in Fabric   lab_sqldb_store                        T-SQL
  Mirrored Azure SQL       Azure SQL (private only) -> lab_mir_store   T-SQL (mirror's SQL analytics endpoint)
  Eventhouse               lab_eh_live4 / lab_kql_store           KQL

  py infra/live4_stores.py capacity [--sku F128]   # create or resume the live4 capacity
  py infra/live4_stores.py workspace               # <prefix>-live4-dev on it, dev roles, Asset domain
  py infra/live4_stores.py snapshot                # freeze hub tables from lh_asset (PII columns dropped) -> infra/.state/live4
  py infra/live4_stores.py lakehouse|sqldb|eventhouse   # create the store and load the snapshot
  py infra/live4_stores.py azuresql                # VNet + private endpoint + Azure SQL + VNet data gateway + load + mirror
  py infra/live4_stores.py counts                  # row counts per store (must all match the snapshot)
  py infra/live4_stores.py agents                  # lab_da_live4_<store>: same instructions, no few-shots
  py infra/live4_stores.py pause|resume            # the live4 capacity only
Evaluation: eval/live4.py
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import struct
import time
from datetime import date, datetime, timezone
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq

from common import STATE_DIR, env, fab, fab_api, fab_exists, load_env, load_state, log, save_state
from fabric_rest import b64, fabric, find_item, kql, token, tsql, upsert_item
from rti_lab import platform

DEFAULT_CAPACITY = "contosoenergylablive4"
SNAP = STATE_DIR / "live4"
PII = {"customer_name", "email", "phone", "first_name", "last_name", "street_address"}
# The hub tables the golden questions use (fleet_telemetry excluded: freshness questions are out of scope).
TABLES = ["sites", "devices", "batteries", "fin_contracts", "fin_customers", "fleet_alert_events"]
KEYS = {"sites": "site_id", "devices": "device_id", "batteries": "device_id", "fin_contracts": "contract_id",
        "fin_customers": "customer_id", "fleet_alert_events": "alert_event_id"}  # alert_event_id has duplicates in the source
STORES = {"lakehouse": "lab_lh_store", "sqldb": "lab_sqldb_store", "mirror": "lab_mir_store", "eventhouse": "lab_kql_store"}
EVENTHOUSE = "lab_eh_live4"


def cap_name() -> str:
    return env("LIVE4_CAPACITY_NAME", required=False) or DEFAULT_CAPACITY


def ws_name() -> str:
    return f"{env('WORKSPACE_PREFIX')}-live4-dev"


def ws() -> str:
    known = load_state("live4").get("workspace_id")
    if known:
        return known
    match = next((w for w in fabric("get", "workspaces").get("value", []) if w["displayName"] == ws_name()), None)
    if match is None:
        raise SystemExit(f"{ws_name()} not found; run: py infra/live4_stores.py workspace")
    return match["id"]


def update_state(**kv) -> dict:
    s = load_state("live4")
    s.update(kv)
    save_state("live4", s)
    return s


# ----------------------------------------------------------------- capacity + workspace
def capacity(sku: str) -> None:
    cap = f".capacities/{cap_name()}.Capacity"
    if not fab_exists(cap):
        params = ",".join([f"sku={sku}", f"admin={env('ADMIN_UPN')}", f"location={env('AZURE_LOCATION')}",
                           f"resourceGroup={env('AZURE_RESOURCE_GROUP')}", f"subscriptionId={env('AZURE_SUBSCRIPTION_ID')}"])
        fab("create", cap, "-P", params)
        log(f"created {cap} ({sku})")
    else:
        state = fab("get", cap, "-q", "properties.state").strip().strip('"')
        if state != "Active":
            fab("start", cap, "-f")
            log("resumed")
        if fab("get", cap, "-q", "sku.name").strip().strip('"') != sku:
            fab("set", cap, "-q", "sku.name", "-i", sku, "-f")
            log(f"resized to {sku}")
    log(f"{cap_name()}: {fab('get', cap, '-q', '{state: properties.state, sku: sku.name}')}")


def capacity_id() -> str:
    _, data = fab_api("capacities")
    for c in data.get("value", []):
        if c["displayName"] == cap_name():
            if c.get("state") != "Active":
                raise SystemExit(f"{cap_name()} is {c.get('state')}; run: py infra/live4_stores.py resume")
            return c["id"]
    raise SystemExit(f"{cap_name()} not visible; run: py infra/live4_stores.py capacity")


def workspace() -> str:
    from fabric_setup import ensure_domains, ensure_roles
    cid = capacity_id()
    if cid.lower() == next(c["id"] for c in fab_api("capacities")[1]["value"]
                           if c["displayName"] == env("FABRIC_CAPACITY_NAME")).lower():
        raise SystemExit("live4 must not run on the shared lab capacity")
    existing = {w["displayName"]: w for w in fabric("get", "workspaces").get("value", [])}
    if ws_name() in existing:
        wid = existing[ws_name()]["id"]
        if existing[ws_name()].get("capacityId", "").lower() != cid.lower():
            fabric("post", f"workspaces/{wid}/assignToCapacity", {"capacityId": cid})
    else:
        wid = fabric("post", "workspaces", {"displayName": ws_name(), "capacityId": cid,
                                            "description": "Contoso Energy lab - live4 storage latency (dev only, lab_)"})["id"]
        log(f"workspace created: {ws_name()}")
    fab_api(f"admin/domains/{ensure_domains()['Asset']}/assignWorkspaces", "post", {"workspacesIds": [wid]})
    ensure_roles({ws_name(): {"id": wid, "environment": "dev"}})
    update_state(workspace_id=wid, capacity_id=cid)
    log(f"{ws_name()} on {cap_name()}: {wid}")
    return wid


def pause(stop: bool) -> None:
    cap = f".capacities/{cap_name()}.Capacity"
    fab("stop" if stop else "start", cap, "-f")
    log(f"{cap_name()} {'paused' if stop else 'resumed'}")


# ----------------------------------------------------------------- snapshot (T0)
ARROW = {"varchar": pa.string(), "nvarchar": pa.string(), "char": pa.string(), "int": pa.int32(),
         "smallint": pa.int32(), "tinyint": pa.int32(), "bigint": pa.int64(), "float": pa.float64(),
         "real": pa.float64(), "decimal": pa.float64(), "numeric": pa.float64(), "bit": pa.bool_(),
         "date": pa.date32(), "datetime2": pa.timestamp("us"), "datetime": pa.timestamp("us")}


def _py(v):
    return float(v) if isinstance(v, Decimal) else v


def snapshot() -> None:
    """One read of the hub tables from lh_asset (dev) at T0; every store is loaded from these files."""
    from data_agents import ws as domain_ws
    w = domain_ws("asset", "dev")
    lh = find_item(w, "Lakehouse", "lh_asset")
    host = fabric("get", f"workspaces/{w}/lakehouses/{lh['id']}")["properties"]["sqlEndpointProperties"]["connectionString"]
    cols: dict[str, list[tuple[str, str]]] = {}
    for t, c, typ in tsql(host, "lh_asset", "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                                           "WHERE TABLE_SCHEMA = 'dbo' ORDER BY TABLE_NAME, ORDINAL_POSITION"):
        if t in TABLES and c not in PII and not c.startswith("_"):
            cols.setdefault(t, []).append((c, typ))
    SNAP.mkdir(parents=True, exist_ok=True)
    meta = {"taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tables": {}}
    for t in TABLES:
        names = [c for c, _ in cols[t]]
        rows = tsql(host, "lh_asset", f"SELECT {', '.join(f'[{c}]' for c in names)} FROM dbo.[{t}] ORDER BY [{KEYS[t]}]")
        schema = pa.schema([(c, ARROW.get(typ, pa.string())) for c, typ in cols[t]])
        data = {c: [_py(r[i]) for r in rows] for i, c in enumerate(names)}
        for f in schema:  # stringify anything the map didn't type (e.g. uniqueidentifier)
            if pa.types.is_string(f.type):
                data[f.name] = [None if v is None else str(v) for v in data[f.name]]
        table = pa.table(data, schema=schema)
        pq.write_table(table, SNAP / f"{t}.parquet", compression="snappy")
        meta["tables"][t] = {"rows": table.num_rows, "columns": [[f.name, str(f.type)] for f in schema]}
        log(f"snapshot {t}: {table.num_rows} rows, {len(names)} columns")
    (SNAP / "_snapshot.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def snap() -> dict[str, pa.Table]:
    if not (SNAP / "_snapshot.json").exists():
        raise SystemExit("no snapshot; run: py infra/live4_stores.py snapshot")
    return {t: pq.read_table(SNAP / f"{t}.parquet") for t in TABLES}


def snapshot_meta() -> dict:
    return json.loads((SNAP / "_snapshot.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------- DDL / literals
def sql_type(f: pa.Field, col: pa.ChunkedArray) -> str:
    t = f.type
    if pa.types.is_string(t):
        longest = max((len(v) for v in col.to_pylist() if v is not None), default=1)
        return f"varchar({max(32, longest * 2)})" if longest <= 2000 else "varchar(max)"
    return {"int32": "int", "int64": "bigint", "double": "float", "bool": "bit", "date32[day]": "date"}.get(
        str(t), "datetime2(6)" if pa.types.is_timestamp(t) else "varchar(4000)")


def kql_type(f: pa.Field) -> str:
    t = f.type
    if pa.types.is_string(t):
        return "string"
    return {"int32": "int", "int64": "long", "double": "real", "bool": "bool"}.get(
        str(t), "datetime" if pa.types.is_timestamp(t) or pa.types.is_date(t) else "string")


def sql_lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        return f"'{v.isoformat(sep=' ')}'"
    if isinstance(v, date):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("'", "''") + "'"


def sql_ddl(name: str, table: pa.Table) -> str:
    """Same DDL for the SQL database and Azure SQL; the PK is also what Azure SQL mirroring needs.

    fleet_alert_events repeats some alert_event_id values (as in the source), so it gets a surrogate _row_id key;
    agents don't see _ columns."""
    unique = len(set(table.column(KEYS[name]).to_pylist())) == table.num_rows
    cols = ", ".join(f"[{f.name}] {sql_type(f, table.column(f.name))}"
                     + (" NOT NULL" if unique and f.name == KEYS[name] else " NULL") for f in table.schema)
    if unique:
        return f"CREATE TABLE dbo.[{name}] ({cols}, CONSTRAINT [pk_{name}] PRIMARY KEY ([{KEYS[name]}]))"
    return f"CREATE TABLE dbo.[{name}] ([_row_id] bigint IDENTITY(1,1) NOT NULL, {cols}, CONSTRAINT [pk_{name}] PRIMARY KEY ([_row_id]))"


def sql_inserts(name: str, table: pa.Table, batch: int = 500) -> list[str]:
    cols = ", ".join(f"[{c}]" for c in table.column_names)
    rows = table.to_pylist()
    return [f"INSERT INTO dbo.[{name}] ({cols}) VALUES " +
            ", ".join("(" + ", ".join(sql_lit(r[c]) for c in table.column_names) + ")" for r in rows[i:i + batch])
            for i in range(0, len(rows), batch)]


def sql_exec(host: str, database: str, statements: list[str]) -> None:
    import mssql_python
    tok = token("https://database.windows.net/").encode("utf-16-le")
    conn = mssql_python.connect(f"Server=tcp:{host},1433;Database={database};Encrypt=yes;TrustServerCertificate=no",
                                attrs_before={1256: struct.pack(f"<I{len(tok)}s", len(tok), tok)})
    try:
        cur = conn.cursor()
        for s in statements:
            cur.execute(s)
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------- stores
def _onelake_delete(w: str, item_id: str, path: str) -> None:
    import urllib.parse
    from onelake import ONELAKE, _req
    status, resp = _req("DELETE", f"{ONELAKE}/{w}/{item_id}/{urllib.parse.quote(path)}?recursive=true")
    if status not in (200, 202, 404):
        raise SystemExit(f"OneLake delete {path} -> {status}: {resp[:200]!r}")


def lakehouse() -> None:
    from deltalake import write_deltalake
    from finance_lakehouse import lakehouse as make_lakehouse, refresh_sql_endpoint
    w = ws()
    lh = make_lakehouse(w, STORES["lakehouse"], "live4 store: physical Delta copies of the frozen hub snapshot")
    storage = {"bearer_token": token("https://storage.azure.com/"), "use_fabric_endpoint": "true"}
    for name, table in snap().items():
        # The SQL analytics endpoint hides TIMESTAMP_NTZ columns (the Load Table API writes parquet timestamps as NTZ),
        # so the Delta tables are written directly with UTC timestamps.
        for i, f in enumerate(table.schema):
            if pa.types.is_timestamp(f.type) and f.type.tz is None:
                table = table.set_column(i, f.name, table.column(i).cast(pa.timestamp("us", tz="UTC")))
        url = f"abfss://{w}@onelake.dfs.fabric.microsoft.com/{lh['id']}/Tables/{name}"
        _onelake_delete(w, lh["id"], f"Tables/{name}")  # replace tables first created by the Load Table API
        write_deltalake(url, table, mode="overwrite", schema_mode="overwrite", storage_options=storage)
        log(f"{STORES['lakehouse']}.{name}: {table.num_rows} rows written (Delta)")
    refresh_sql_endpoint(w, STORES["lakehouse"])
    update_state(lakehouse={"id": lh["id"], "host": lh["sql_host"], "database": STORES["lakehouse"]})


def sqldb() -> None:
    w = ws()
    item = upsert_item(w, "SQLDatabase", STORES["sqldb"], None, "live4 store: SQL database in Fabric (frozen hub snapshot)")
    for _ in range(30):
        props = fabric("get", f"workspaces/{w}/sqlDatabases/{item['id']}").get("properties", {})
        if props.get("serverFqdn") and props.get("databaseName"):
            break
        time.sleep(10)
    host, db = props["serverFqdn"].split(",")[0], props["databaseName"]
    for name, table in snap().items():
        sql_exec(host, db, [f"DROP TABLE IF EXISTS dbo.[{name}]", sql_ddl(name, table), *sql_inserts(name, table)])
        log(f"{STORES['sqldb']}.{name}: {table.num_rows} rows loaded")
    update_state(sqldb={"id": item["id"], "host": host, "database": db})


def eventhouse() -> None:
    w = ws()
    eh = upsert_item(w, "Eventhouse", EVENTHOUSE, None, "live4 store: Eventhouse (frozen hub snapshot)")
    db = upsert_item(w, "KQLDatabase", STORES["eventhouse"], [
        {"path": "DatabaseProperties.json", "payloadType": "InlineBase64",
         "payload": b64({"databaseType": "ReadWrite", "parentEventhouseItemId": eh["id"],
                         "oneLakeCachingPeriod": "P36500D", "oneLakeStandardStoragePeriod": "P36500D"})},
        {"path": ".platform", "payload": b64(platform("KQLDatabase", STORES["eventhouse"])), "payloadType": "InlineBase64"}],
        "live4 store: KQL database (frozen hub snapshot)", update=False)
    uri = fabric("get", f"workspaces/{w}/eventhouses/{eh['id']}")["properties"]["queryServiceUri"]
    dbname = STORES["eventhouse"]
    for name, table in snap().items():
        cols = ", ".join(f"['{f.name}']:{kql_type(f)}" for f in table.schema)
        kql(uri, dbname, f".drop table ['{name}'] ifexists", mgmt=True)
        kql(uri, dbname, f".create table ['{name}'] ({cols})", mgmt=True)
        rows = table.to_pylist()
        for i in range(0, len(rows), 1000):
            buf = io.StringIO()
            wr = csv.writer(buf, lineterminator="\n")
            for r in rows[i:i + 1000]:
                wr.writerow(["" if r[c] is None else ("true" if r[c] is True else "false" if r[c] is False else
                             r[c].isoformat() if isinstance(r[c], (date, datetime)) else r[c]) for c in table.column_names])
            kql(uri, dbname, f".ingest inline into table ['{name}'] with (format='csv') <|\n{buf.getvalue()}", mgmt=True)
        log(f"{dbname}.{name}: {table.num_rows} rows loaded")
    update_state(eventhouse={"id": eh["id"], "db_id": db["id"], "query_uri": uri, "database": dbname})


# ----------------------------------------------------------------- agents (same instructions, no few-shots)
AGENT = {"lakehouse": "lab_da_live4_lh", "sqldb": "lab_da_live4_sqldb", "mirror": "lab_da_live4_mir",
         "eventhouse": "lab_da_live4_kql"}
# The Step 11 rich hub text (data_agents.HUB_RICH_INSTRUCTIONS) made language-neutral: no SQL wording, no lh_asset,
# no telemetry (not in the snapshot). Same text for all four agents.
INSTRUCTIONS = """You are the Contoso Energy analyst assistant for residential solar + battery sites and their consumer
financing. Data is synthetic and is a frozen snapshot: "now/currently" means the latest state in the data.
All tables are in the single attached data source.

Entities and keys:
- Site (sites.site_id): one home installation; belongs to exactly one customer (sites.customer_id); region, pv_capacity_kw (kW),
  has_battery, install_date, status.
- Device (devices.device_id, devices.site_id): inverter, battery, meter or gateway at a site; model, firmware.
- Battery (batteries.device_id, batteries.site_id): capacity_kwh, chemistry, state_of_health_pct (SoH, %), warranty_end, cycle_count.
- Contract (fin_contracts.contract_id): financing of one site (fin_contracts.site_id, at most one contract per site) for one
  customer (fin_contracts.customer_id); product Loan | Lease | PPA; principal (financed amount, USD); apr_pct (Loans only;
  empty for Lease and PPA); monthly_payment; start_date; status Active | Delinquent | Default | PaidOff.
- Customer (fin_customers.customer_id): segment, region, city, credit bands. Join sites/contracts on customer_id.
- Alert event (fleet_alert_events): one row per event; alert_id identifies an alert; event_type 'raised' or 'cleared';
  severity Critical | High | Low; alert_code; device_id, site_id; event_time (UTC).

Definitions:
- "Alerts raised" = number of distinct alert_id with event_type 'raised'. "Alert events" = rows (raised and cleared).
- Open alert = the latest event of an alert_id is 'raised'.
- Contracts "at risk" = status Delinquent or Default. "In default" = status Default.
- Warranty review rule: a battery with state_of_health_pct < 80 whose warranty_end is after today needs a warranty review.
- Impact questions ("if X fails/defaults, what is affected?"): trace site -> devices/batteries -> contract -> customer and
  report the affected IDs, the financed principal at risk and the customer segment.

Answering:
- Use exact aggregates; round money to 2 decimals and percentages to 2 decimals; state units (USD, kW, kWh, %).
- Always name the IDs you report (SITE-..., CTR-...). Dates are UTC.
- If the data does not contain what is asked (e.g. tariffs, weather, revenue before the data starts), say it is not available;
  never estimate or invent a figure.
- Never reveal customer names, emails or phone numbers; offer customer_id instead."""

SOURCE_INSTRUCTIONS = """Join keys: sites.site_id = devices.site_id = batteries.site_id = fin_contracts.site_id =
fleet_alert_events.site_id; sites.customer_id = fin_contracts.customer_id = fin_customers.customer_id.
Count alerts as distinct alert_id with event_type 'raised'; count alert events as rows.
Product values are capitalized ('Loan', 'Lease', 'PPA'); status values are capitalized ('Active', 'Delinquent', 'Default',
'PaidOff'). Installed PV capacity comes from sites.pv_capacity_kw (not from fin_contracts)."""

# datasource.json "type" and element type prefix per store. The SQL database isn't in the documented enum, but the
# agent accepts "sql_database" (probed: data_warehouse / mirrored_database pointing at it fail to connect).
DS = {"lakehouse": ("lakehouse_tables", "lakehouse_tables"), "mirror": ("mirrored_database", "mirrored_database"),
      "eventhouse": ("kusto", "kusto")}


def _eid(*parts: str) -> str:
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "contoso-energy-lab/live4/" + "/".join(parts)))


def _dtype(arrow: str, kusto: bool) -> str:
    sql = {"string": "varchar", "double": "float", "int32": "int", "int64": "bigint", "bool": "bit", "date32[day]": "date"}
    kq = {"string": "string", "double": "real", "int32": "int", "int64": "long", "bool": "bool"}
    return (kq if kusto else sql).get(arrow, "datetime" if kusto else "datetime2")


def datasource(store: str, sqldb_type: str | None = None) -> tuple[str, dict]:
    s, w = load_state("live4"), ws()
    meta = snapshot_meta()["tables"]
    if store == "eventhouse":
        artifact, name = s["eventhouse"]["db_id"], STORES["eventhouse"]
    else:
        artifact, name = s[store]["id"], STORES[store]
    ds_type, el = DS.get(store) or ((sqldb_type or "sql_database"),) * 2
    if el == "data_warehouse":
        el = "warehouse_tables"
    tables = [{"id": _eid(store, t), "display_name": t, "type": f"{el}.table", "is_selected": True,
               "children": [{"id": _eid(store, t, c), "display_name": c, "type": f"{el}.column",
                             "data_type": _dtype(typ, store == "eventhouse"), "is_selected": True}
                            for c, typ in meta[t]["columns"]]} for t in TABLES]
    elements = tables if store == "eventhouse" else [
        {"id": _eid(store, "dbo"), "display_name": "dbo", "type": f"{el}.schema", "is_selected": True, "children": tables}]
    return f"{ds_type}-{name}", {
        "$schema": "1.0.0", "artifactId": artifact, "workspaceId": w, "displayName": name, "type": ds_type,
        "userDescription": "Contoso Energy hub snapshot: sites, devices, batteries, contracts, customers, alert events",
        "dataSourceInstructions": SOURCE_INSTRUCTIONS, "elements": elements}


def agent(store: str, sqldb_type: str | None = None, name: str | None = None) -> dict:
    name = name or AGENT[store]
    folder, ds = datasource(store, sqldb_type)
    parts = [{"path": "Files/Config/data_agent.json", "payload": b64({"$schema": "2.1.0"}), "payloadType": "InlineBase64"},
             {"path": "Files/Config/publish_info.json", "payloadType": "InlineBase64",
              "payload": b64({"$schema": "1.0.0", "description": f"live4 {store} arm"})}]
    for st in ("draft", "published"):
        parts += [{"path": f"Files/Config/{st}/stage_config.json", "payloadType": "InlineBase64",
                   "payload": b64({"$schema": "1.0.0", "aiInstructions": INSTRUCTIONS})},
                  {"path": f"Files/Config/{st}/{folder}/datasource.json", "payload": b64(ds), "payloadType": "InlineBase64"}]
    parts.append({"path": ".platform", "payload": b64(platform("DataAgent", name)), "payloadType": "InlineBase64"})
    item = upsert_item(ws(), "DataAgent", name, parts, f"live4 {store} arm (same instructions, no few-shots)")
    log(f"{name}: {ds['type']} -> {ds['displayName']}, published")
    return item


def agents(only: list[str] | None = None) -> None:
    s = load_state("live4")
    for store in only or AGENT:
        if store == "eventhouse" or store in s:
            agent(store)
        else:
            log(f"{AGENT[store]}: skipped ({store} store not built yet)")


# ----------------------------------------------------------------- counts
def counts() -> dict:
    s, expected = load_state("live4"), {t: v["rows"] for t, v in snapshot_meta()["tables"].items()}
    out = {"snapshot": expected}
    for store in ("lakehouse", "sqldb", "mirror"):
        if store in s:
            out[store] = {t: tsql(s[store]["host"], s[store]["database"], f"SELECT COUNT(*) FROM dbo.[{t}]")[0][0]
                          for t in TABLES}
    if "eventhouse" in s:
        e = s["eventhouse"]
        out["eventhouse"] = {t: kql(e["query_uri"], e["database"], f"['{t}'] | count")[0]["Count"] for t in TABLES}
    for store, c in out.items():
        print(f"{store:<11} " + "  ".join(f"{t}={c[t]}" for t in TABLES) + ("" if c == expected else "   <-- MISMATCH"))
    return out


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["capacity", "workspace", "snapshot", "lakehouse", "sqldb", "eventhouse",
                                      "azuresql", "counts", "agents", "pause", "resume"])
    p.add_argument("--sku", default="F128")
    p.add_argument("--only", help="agents: comma-separated stores (lakehouse,sqldb,mirror,eventhouse)")
    a = p.parse_args()
    load_env()
    if a.action == "capacity":
        capacity(a.sku)
    elif a.action in ("pause", "resume"):
        pause(a.action == "pause")
    elif a.action == "workspace":
        workspace()
    elif a.action == "snapshot":
        snapshot()
    elif a.action == "lakehouse":
        lakehouse()
    elif a.action == "sqldb":
        sqldb()
    elif a.action == "eventhouse":
        eventhouse()
    elif a.action == "counts":
        counts()
    elif a.action == "azuresql":
        from live4_azuresql import azuresql
        azuresql()
    elif a.action == "agents":
        agents(a.only.split(",") if a.only else None)


if __name__ == "__main__":
    main()
