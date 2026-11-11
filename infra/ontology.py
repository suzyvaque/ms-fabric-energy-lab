"""Step 5: Fabric IQ ontology `ont_contoso` (ce-asset) over one SQL endpoint (lh_asset). Idempotent.

  py infra/ontology.py expose    [--env dev|prod]  # eh_fleet tables -> OneLake (mirroring policy + backfill)
  py infra/ontology.py shortcuts [--env dev|prod]  # lh_asset shortcuts: fleet telemetry/alerts, finance contracts/customers
  py infra/ontology.py create    [--env dev]       # ontology definition (TMDL) - prod comes from fabric-cicd
  py infra/ontology.py verify    [--env dev|prod]  # definition, bound data, graph status
  py infra/ontology.py all       [--env dev]

An ontology binds every entity through ONE expression (a SQL analytics endpoint), so lh_asset is the
"semantic hub": its own asset tables + zero-copy OneLake shortcuts to the Fleet (Eventhouse) and
Finance (lakehouse) domains. PII columns of the customer table are never mapped to ontology properties.
"""
import argparse
import json
import time
import uuid

from common import load_env, log
from fabric_rest import FABRIC, _request, b64, fabric, find_item, kql, tsql, upsert_item
from finance_lakehouse import lakehouse, refresh_sql_endpoint, shortcut, ws
from fleet_medallion import script as fleet_script
from rti_alerts import fleet_db
from semantic_model import SQL_TYPES

NAME = "ont_contoso"
NS = "contoso"
PINNED_AT = "2026-01-01T00:00:00Z"  # fixed so regenerating the definition does not churn Git
MIRRORED = ("silver_telemetry", "gold_alert_events")

# shortcut name in lh_asset -> (domain, item type, item name, source table)
SHORTCUTS = {
    "fleet_telemetry": ("fleet", "KQLDatabase", "eh_fleet", "silver_telemetry"),
    "fleet_alert_events": ("fleet", "KQLDatabase", "eh_fleet", "gold_alert_events"),
    "fin_contracts": ("finance", "Lakehouse", "lh_finance", "gold_dim_contract"),
    "fin_customers": ("finance", "Lakehouse", "lh_finance", "gold_dim_customer"),
}

# entity -> (table, key, description, {column: description})
ENTITIES = {
    "Site": ("sites", "site_id", "A residential solar installation (home) in the Contoso Energy fleet.", {
        "site_id": "Unique site identifier.", "customer_id": "Customer who owns the site.",
        "region": "Sales/service region.", "timezone": "IANA time zone of the site.",
        "install_date": "Commissioning date.", "pv_capacity_kw": "Installed PV capacity (kWp).",
        "panel_count": "Number of PV panels.", "has_battery": "True when a home battery is installed.",
        "ev_charger": "True when an EV charger is installed.", "status": "Site lifecycle status."}),
    "Device": ("devices", "device_id", "A connected device at a site (inverter, battery, meter or gateway).", {
        "device_id": "Unique device identifier.", "site_id": "Site where the device is installed.",
        "device_type": "inverter, battery, meter or gateway.", "model": "Device model.",
        "firmware": "Installed firmware version.", "install_date": "Installation date.",
        "status": "Device lifecycle status."}),
    "Battery": ("batteries", "device_id", "Home battery asset details (one per battery device).", {
        "device_id": "Battery device identifier.", "site_id": "Site where the battery is installed.",
        "capacity_kwh": "Usable capacity (kWh).", "max_power_kw": "Maximum charge/discharge power (kW).",
        "chemistry": "Cell chemistry.", "warranty_end": "Warranty end date.",
        "cycle_count": "Full equivalent cycles to date.", "state_of_health_pct": "State of health (%)."}),
    "Alert": ("fleet_alert_events", "alert_event_id", "A device alert event (raised or cleared) from the fleet.", {
        "alert_event_id": "Unique alert event id (alert_id:event_type).", "alert_id": "Alert identifier.",
        "event_type": "raised or cleared.", "device_id": "Device that raised the alert.",
        "alert_code": "Alert code, e.g. BATT_OVERTEMP or COMM_LOSS.", "severity": "Low, Medium, High or Critical.",
        "source": "device or cloud.", "message": "Alert text.", "event_time": "Time the event occurred (UTC)."}),
    "Contract": ("fin_contracts", "contract_id", "A consumer financing contract (loan or lease) for a site.", {
        "contract_id": "Unique contract identifier.", "site_id": "Financed site.",
        "product": "Financing product.", "start_date": "Contract start date.", "term_end_date": "Contract end date.",
        "term_months": "Term in months.", "principal": "Financed principal (USD).", "apr_pct": "APR (%).",
        "monthly_payment": "Scheduled monthly payment (USD).", "status": "Contract status."}),
    "Customer": ("fin_customers", "customer_id", "A Contoso Energy customer (non-PII attributes only).", {
        "customer_id": "Unique customer identifier.", "region": "Customer region.", "segment": "Customer segment.",
        "signup_date": "Signup date.", "bureau_band_current": "Current credit bureau band."}),
}

# Device time series from the Eventhouse telemetry shortcut
TELEMETRY = ("fleet_telemetry", "device_id", "event_time", {
    "pv_kw": "PV power (kW).", "ac_kw": "Inverter AC output (kW).", "inverter_temp_c": "Inverter temperature (C).",
    "soc_pct": "Battery state of charge (%).", "battery_kw": "Battery power (kW, + discharge).",
    "battery_temp_c": "Battery temperature (C).", "grid_kw": "Grid power (kW, + import).",
    "load_kw": "Home load (kW)."})

# (name, from entity, from column, to entity, to column) - a tree, so there are no ambiguous paths
RELATIONSHIPS = [
    ("Device Installed At Site", "Device", "site_id", "Site", "site_id"),
    ("Battery Is Device", "Battery", "device_id", "Device", "device_id"),
    ("Alert Raised By Device", "Alert", "device_id", "Device", "device_id"),
    ("Site Owned By Customer", "Site", "customer_id", "Customer", "customer_id"),
    ("Contract Finances Site", "Contract", "site_id", "Site", "site_id"),
]

RULES = {
    "BatteryWarrantyReview": ("A battery whose state of health is below 80 percent before its warranty end date needs "
                              "a warranty review.", "Battery", ["state_of_health_pct", "warranty_end"]),
}


def tag(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "contoso-energy-lab/ontology/" + "/".join(parts)))


def ent(name: str) -> str:
    return f"{NS}#{name}"


def q(name: str) -> str:
    return f"'{name}'" if " " in name else name


# --- OneLake exposure + shortcuts -------------------------------------------------------------------

def expose(e: str) -> None:
    w, db, uri = fleet_db(e)
    # Same script as the deployed definition (idempotent) so prod tables exist before shortcuts point at them.
    kql(uri, db["id"], ".execute database script with (ContinueOnErrors=false) <|\n" + fleet_script(), mgmt=True)
    kql(uri, db["id"], ".set-or-replace gold_alert_events <| GoldAlertEvents()", mgmt=True)  # backfill + dedup (small)
    for t in MIRRORED:
        pol = kql(uri, db["id"], f".show table {t} policy mirroring", mgmt=True)
        if pol and pol[0].get("Policy") and '"IsEnabled": true' in pol[0]["Policy"]:
            continue
        kql(uri, db["id"], f".alter-merge table {t} policy mirroring dataformat=parquet "
                           "with (IsEnabled=true, TargetLatencyInMinutes=5, Backfill=true)", mgmt=True)
        log(f"{t}: OneLake availability enabled (backfill)")


def shortcuts(e: str) -> dict:
    aw = ws("asset", e)
    lh = lakehouse(aw, "lh_asset", "")
    for name, (domain, typ, item, table) in SHORTCUTS.items():
        sw = ws(domain, e)
        src = find_item(sw, typ, item)
        shortcut(aw, lh["id"], "Tables", name, {"oneLake": {"workspaceId": sw, "itemId": src["id"],
                                                             "path": f"Tables/{table}"}})
    return lh


# --- ontology definition ----------------------------------------------------------------------------

def columns(lh: dict) -> dict[str, dict[str, str]]:
    tables = {t for t, *_ in ENTITIES.values()} | {TELEMETRY[0]}
    for attempt in range(20):  # SQL endpoint discovers new shortcuts asynchronously
        rows = tsql(lh["sql_host"], "lh_asset", "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE "
                    "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = 'dbo'")
        cols: dict[str, dict[str, str]] = {}
        for t, c, typ in rows:
            cols.setdefault(t, {})[c] = SQL_TYPES.get(typ, "string")
        missing = tables - cols.keys()
        if not missing:
            return cols
        log(f"refreshing SQL endpoint metadata; missing {sorted(missing)}")
        refresh_sql_endpoint(lh["workspaceId"], "lh_asset")
        time.sleep(15)
    raise SystemExit(f"tables not visible in lh_asset SQL endpoint: {sorted(missing)}")


def table_tmdl(table: str, cols: dict[str, str], used: list[str], src: dict) -> str:
    out = [f"table {table}", f"\tlineageTag: {tag('table', table)}", ""]
    for c in used:
        out += [f"\tcolumn {c}", f"\t\tdataType: {cols[c]}", f"\t\tlineageTag: {tag('col', table, c)}",
                f"\t\tsourceColumn: {c}", ""]
    out += [f"\tpartition {table} = entity", "\t\tmode: directLake", "\t\tsource", f"\t\t\tentityName: {table}",
            "\t\t\tschemaName: dbo", "\t\t\texpressionSource: DatabaseQuery", ""]
    # Undocumented annotations the portal writes; without them the graph marks every entity "Ineligible"
    # (microsoft/skills-for-fabric#107). Prod values are rebound by workspaces/ce-asset/parameter.yml.
    out += [f"\t\tannotation {k} = {v}" for k, v in src.items()] + [""]
    return "\n".join(out)


def prop_type(t: str) -> str:
    return t


def entity_tmdl(name: str, table: str, key: str, desc: str, props: dict[str, str], cols: dict[str, str],
                series: tuple | None) -> str:
    out = [f"/// {desc}", f"entity {ent(name)}", f"\tlineageTag: {tag('entity', name)}",
           f"\tbackingTable: {table}", f"\tkeyProperty: {key}", ""]
    for c, d in props.items():
        out += [f"\t/// {d}", f"\tproperty {c}", f"\t\tdataType: {prop_type(cols[c])}",
                f"\t\tlineageTag: {tag('prop', name, c)}", "", "\t\tbackingConfiguration",
                f"\t\t\tvalueColumn: {table}.{c}", ""]
    if series:
        ts_table, ts_key, order, measures = series
        for c, d in measures.items():
            out += [f"\t/// {d}", f"\tproperty {c}", "\t\tdataType: TimeSeries<double>",
                    f"\t\tlineageTag: {tag('prop', name, c)}", "", "\t\tbackingConfiguration", "\t\t\ttype: timeSeries",
                    f"\t\t\tvalueColumn: {ts_table}.{c}", f"\t\t\torderingColumn: {ts_table}.{order}", ""]
        out += ["\tadditionalBackingTable", f"\t\ttable: {ts_table}", f"\t\trelationship: rel_{ts_table}_{ts_key}", ""]
    return "\n".join(out)


def parts(lh: dict, sql_endpoint_id: str, workspace_name: str) -> list[dict]:
    cols = columns(lh)
    used: dict[str, list[str]] = {}
    for table, key, _, props in ENTITIES.values():
        used.setdefault(table, [])
        used[table] += [c for c in props if c not in used[table]]
    ts_table, ts_key, ts_order, ts_measures = TELEMETRY
    used[ts_table] = [ts_key, ts_order, *ts_measures]
    for _, fe, fc, te, tc in RELATIONSHIPS:  # relationship columns must exist in the model tables
        for t, c in ((ENTITIES[fe][0], fc), (ENTITIES[te][0], tc)):
            if c not in used[t]:
                used[t].append(c)

    files: dict[str, str] = {
        "database.tmdl": "database\n\tcompatibilityLevel: 1000000\n",
        "expressions.tmdl": (f"expression DatabaseQuery =\n\t\tlet\n\t\t    database = Sql.Database(\"{lh['sql_host']}\", "
                             f"\"{sql_endpoint_id}\")\n\t\tin\n\t\t    database\n\tlineageTag: {tag('expr')}\n"),
        f"namespaces/{NS}.tmdl": f"namespace {NS}\n\tlineageTag: {tag('ns', NS)}\n",
    }
    src = {"ONT_WorkspaceId": lh["workspaceId"], "ONT_ItemId": lh["id"], "ONT_ItemKind": "Lakehouse",
           "ONT_ItemName": lh["displayName"], "ONT_WorkspaceName": workspace_name,
           "ONT_SqlEndpoint": lh["sql_host"], "ONT_SqlDatabase": lh["displayName"],
           "ONT_PinnedAtUtc": PINNED_AT}
    for t, cs in used.items():
        files[f"tables/{t}.tmdl"] = table_tmdl(t, cols[t], cs, src)
    for name, (table, key, desc, props) in ENTITIES.items():
        files[f"entities/{ent(name)}.tmdl"] = entity_tmdl(name, table, key, desc, props, cols[table],
                                                          TELEMETRY if name == "Device" else None)

    rel = [f"relationship rel_{ts_table}_{ts_key}", f"\tfromColumn: {ts_table}.{ts_key}",
           f"\ttoColumn: {ENTITIES['Device'][0]}.device_id", ""]
    erel = []
    for name, fe, fc, te, tc in RELATIONSHIPS:
        ft, tt = ENTITIES[fe][0], ENTITIES[te][0]
        rid = f"rel_{ft}_{fc}"
        rel += [f"relationship {rid}", f"\tfromColumn: {ft}.{fc}", f"\ttoColumn: {tt}.{tc}", ""]
        erel += [f"entityRelationship {q(name)}", f"\tlineageTag: {tag('erel', name)}", f"\tfromEntity: {ent(fe)}",
                 f"\ttoEntity: {ent(te)}", "\tbackingConfiguration", f"\t\trelationship: {rid}", ""]
    files["relationships.tmdl"] = "\n".join(rel)
    files["entityRelationships.tmdl"] = "\n".join(erel)

    for rname, (stmt, e_name, props) in RULES.items():
        files[f"rules/{rname}.tmdl"] = "\n".join([
            f"rule {rname}", f"\tstatement: {stmt}", f"\tlineageTag: {tag('rule', rname)}",
            f"\truleReferencedEntity {ent(e_name)}", "\t\tpropertyScope: specific",
            *[f"\t\truleReferencedProperty {p}" for p in props], ""])

    model = ["model Model", *[f"ref table {t}" for t in used], *[f"ref entity {ent(n)}" for n in ENTITIES],
             f"ref namespace {NS}", *[f"ref rule {r}" for r in RULES], ""]
    files["model.tmdl"] = "\n".join(model)
    return [{"path": p, "payload": b64(t), "payloadType": "InlineBase64"} for p, t in files.items()]


def create(e: str) -> dict:
    aw = ws("asset", e)
    lh = lakehouse(aw, "lh_asset", "")
    sep = find_item(aw, "SQLEndpoint", "lh_asset")
    wname = fabric("get", f"workspaces/{aw}")["displayName"]
    item = upsert_item(aw, "Ontology", NAME, parts(lh, sep["id"], wname),
                       "Contoso Energy ontology: Site, Device, Battery, Alert, Contract, Customer (Fabric IQ)")
    log(f"{NAME} upserted ({item['id']})")
    return item


def graph_model(e: str) -> tuple[str, dict]:
    aw = ws("asset", e)
    o = find_item(aw, "Ontology", NAME)
    gs = [i for i in fabric("get", f"workspaces/{aw}/items")["value"]
          if i["type"] == "GraphModel" and i["displayName"].endswith(o["id"].replace("-", ""))]
    if not gs:
        raise SystemExit(f"{NAME}: managed graph not found (create the ontology first)")
    return aw, gs[0]


def verify(e: str) -> None:
    """Definition round-trip + bound data per entity + graph status (materialization is an optional portal step)."""
    import base64
    aw = ws("asset", e)
    o = find_item(aw, "Ontology", NAME)
    got = fabric("post", f"workspaces/{aw}/items/{o['id']}/getDefinition")["definition"]["parts"]
    paths = {p["path"] for p in got}
    missing = [n for n in ENTITIES if f"entities/{ent(n)}.tmdl" not in paths]
    ann = sum(base64.b64decode(p["payload"]).decode("utf-8").count("ONT_ItemKind") for p in got if p["path"].startswith("tables/"))
    print(f"[{'PASS' if not missing else 'FAIL'}] entities in definition: {len(ENTITIES) - len(missing)}/{len(ENTITIES)}")
    print(f"[{'PASS' if ann == len(ENTITIES) + 1 else 'FAIL'}] tables with ONT_* source annotations: {ann}")
    lh = lakehouse(aw, "lh_asset", "")
    for name, (table, key, *_) in ENTITIES.items():
        n, k = tsql(lh["sql_host"], "lh_asset", f"SELECT COUNT(*), COUNT(DISTINCT {key}) FROM dbo.{table}")[0]
        print(f"[{'PASS' if n and n == k else 'WARN'}] {name}: {n} rows, {k} distinct {key}")
    aw, g = graph_model(e)
    status, _, body = _request("post", f"{FABRIC}/v1/workspaces/{aw}/graphModels/{g['id']}/executeQuery?preview=true",
                               {"query": "MATCH (n) RETURN count(n) AS nodes"}, FABRIC)
    print(f"[INFO] graph: {'materialized ' + json.dumps(body)[:200] if status == 200 else 'not materialized (optional portal step)'}")
    if missing:
        raise SystemExit("verification failed")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["expose", "shortcuts", "create", "verify", "all"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    a = p.parse_args()
    load_env()
    if a.action in ("expose", "all"):
        expose(a.env)
    if a.action in ("shortcuts", "all"):
        shortcuts(a.env)
    if a.action in ("create", "all"):
        create(a.env)
    if a.action in ("verify", "all"):
        verify(a.env)


if __name__ == "__main__":
    main()
