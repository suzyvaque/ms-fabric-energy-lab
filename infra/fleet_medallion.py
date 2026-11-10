"""Step 1c: Eventhouse medallion for the fleet (eh_fleet database in ce-fleet-dev). Idempotent.

  py infra/fleet_medallion.py apply          # infra/kql/eh_fleet/*.kql -> eh_fleet DatabaseSchema.kql (item definition)
  py infra/fleet_medallion.py eventstream    # es_fleet: custom endpoint -> bronze_events (direct ingestion)
  py infra/fleet_medallion.py load-dims      # dim_site/dim_device/dim_battery from data-gen batch output
  py infra/fleet_medallion.py verify [--truth data-gen/out/ground_truth/anomalies.csv]
  py infra/fleet_medallion.py all

Source of truth for KQL = infra/kql/eh_fleet/*.kql (readable, ordered). After `apply`, commit the
workspace from Fabric so workspaces/ce-fleet/... holds the deployable definition (fabric-cicd -> prod).
"""
import argparse
import csv
import json
import time
from pathlib import Path

from common import ROOT, load_env, load_state, log, save_state, set_env_value
from fabric_rest import b64, fabric, find_item, kql, upsert_item
from rti_lab import JSON_SER, custom_endpoint_connection_string, platform, ws_id

DB = "eh_fleet"
ES = "es_fleet"
KQL_DIR = ROOT / "infra" / "kql" / DB
DATA = ROOT / "data-gen" / "out"


def script() -> str:
    return "\n\n".join(p.read_text(encoding="utf-8").strip() for p in sorted(KQL_DIR.glob("*.kql"))) + "\n"


def db_item(ws: str) -> dict:
    item = find_item(ws, "KQLDatabase", DB)
    if not item:
        raise SystemExit("eh_fleet database missing - run infra/rti_lab.py create (creates the eventhouse)")
    return item


def apply() -> None:
    ws = ws_id()
    db = db_item(ws)
    current = fabric("post", f"workspaces/{ws}/items/{db['id']}/getDefinition")["definition"]["parts"]
    parts = [p for p in current if p["path"] not in ("DatabaseSchema.kql", ".platform")]
    parts.append({"path": "DatabaseSchema.kql", "payload": b64(script()), "payloadType": "InlineBase64"})
    fabric("post", f"workspaces/{ws}/items/{db['id']}/updateDefinition", {"definition": {"parts": parts}})
    log(f"applied {len(list(KQL_DIR.glob('*.kql')))} KQL files to {DB}")


def eventstream(force: bool = False) -> None:
    ws = ws_id()
    db = db_item(ws)
    stream = f"{ES}-stream"
    topology = {
        "sources": [{"name": "devices", "type": "CustomEndpoint", "properties": {}}],
        "destinations": [{"name": "bronze", "type": "Eventhouse", "properties": {
            "dataIngestionMode": "DirectIngestion", "workspaceId": ws, "itemId": db["id"],
            "tableName": "bronze_events", "connectionName": "es_fleet_bronze", "mappingRuleName": "bronze_events_json"},
            "inputNodes": [{"name": stream}]}],
        "streams": [{"name": stream, "type": "DefaultStream", "properties": {}, "inputNodes": [{"name": "devices"}]}],
        "operators": [], "compatibilityLevel": "1.0"}
    parts = [{"path": "eventstream.json", "payload": b64(topology), "payloadType": "InlineBase64"},
             {"path": "eventstreamProperties.json", "payloadType": "InlineBase64",
              "payload": b64({"retentionTimeInDays": 1, "eventThroughputLevel": "Low"})},
             {"path": ".platform", "payload": b64(platform("Eventstream", ES)), "payloadType": "InlineBase64"}]
    es = upsert_item(ws, "Eventstream", ES, parts, "Contoso fleet telemetry ingestion (custom endpoint, D-009)",
                     update=force)
    for _ in range(40):
        topo = fabric("get", f"workspaces/{ws}/eventstreams/{es['id']}/topology")
        status = [n.get("status") for n in topo["destinations"]]
        if all(s == "Running" for s in status) or "Failed" in status:
            break
        time.sleep(15)
    log(f"{ES} destination status: {status}")
    if "Failed" in status:
        raise SystemExit(f"{ES} destination failed: {json.dumps(topo['destinations'])[:600]}")
    set_env_value("ESTREAM_FLEET_CONNECTION_STRING", custom_endpoint_connection_string(ws, es["id"]))
    state = load_state("rti")
    state["es_fleet_id"] = es["id"]
    state["fleet_db_id"] = db["id"]
    save_state("rti", state)
    log("ESTREAM_FLEET_CONNECTION_STRING written to .env")


def _csv_rows(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[1:]


def load_dims() -> None:
    rti = load_state("rti")
    asset = DATA / "asset"
    if not (asset / "sites.csv").exists():
        raise SystemExit("run: cd data-gen; py -m contoso_gen batch ... (needs out/asset/*.csv)")
    for table, name in (("dim_site", "sites"), ("dim_device", "devices"), ("dim_battery", "batteries")):
        rows = _csv_rows(asset / f"{name}.csv")
        kql(rti["query_uri"], rti["fleet_db_id"], f".clear table {table} data", mgmt=True)
        for i in range(0, len(rows), 500):
            body = "\n".join(rows[i:i + 500])
            kql(rti["query_uri"], rti["fleet_db_id"],
                f".ingest inline into table {table} with (format='csv') <|\n{body}", mgmt=True)
        n = kql(rti["query_uri"], rti["fleet_db_id"], f"{table} | count")[0]["Count"]
        log(f"{table}: {n} rows")


CHECKS = {
    "bronze has telemetry and alerts": "bronze_events | summarize t=countif(record_type=='telemetry'), a=countif(record_type=='alert') | project ok = t > 0 and a > 0, detail = strcat('telemetry=', t, ' alerts=', a)",
    "silver = bronze telemetry (update policy)": "let b = toscalar(bronze_events | where record_type=='telemetry' | count); let s = toscalar(silver_telemetry | count); print ok = b == s, detail = strcat('bronze=', b, ' silver=', s)",
    "dedup removes duplicates": "let s = toscalar(silver_telemetry | count); let d = toscalar(silver_telemetry_dedup | count); let u = toscalar(silver_telemetry | summarize dcount(event_id)); print ok = d < s and abs(d - u) <= u * 0.01, detail = strcat('silver=', s, ' dedup=', d, ' distinct~', u)",
    "schema v2 normalized to kW": "silver_telemetry | where schema_version == 2 and device_type == 'inverter' | summarize n = count(), nulls = countif(isnull(pv_kw)), mx = max(pv_kw) | project ok = n > 0 and nulls == 0 and mx < 20, detail = strcat('v2 inverter rows=', n, ' null pv=', nulls, ' max pv_kw=', mx)",
    "late + outliers flagged": "silver_telemetry | summarize late = countif(is_late), outliers = countif(is_outlier) | project ok = late > 0 and outliers > 0, detail = strcat('late=', late, ' outliers=', outliers)",
    "gold_device_latest covers devices": "let d = toscalar(dim_device | count); let g = toscalar(gold_device_latest | count); print ok = g >= d * 0.95, detail = strcat('devices=', d, ' latest=', g)",
    "gold_site_15m populated": "GoldSiteProfile(30d) | summarize n = count(), sites = dcount(site_id), pv = max(pv_kw_avg) | project ok = n > 0 and pv > 0, detail = strcat('rows=', n, ' sites=', sites, ' max pv_kw_avg=', round(pv, 2))",
    "FleetKpis returns one row": "FleetKpis() | project ok = devices > 0, detail = strcat('devices=', devices, ' online=', devices_online, ' faults=', devices_in_fault, ' open_alerts=', open_alerts, ' pv_kw=', fleet_pv_kw)",
    "FleetSiteEnergy returns kWh": "FleetSiteEnergy(30d) | summarize n = count(), y = avg(specific_yield_kwh_per_kw) | project ok = n > 0 and y between (0.5 .. 9), detail = strcat('site-days=', n, ' avg yield kWh/kWp=', round(y, 2))",
    "FleetDataQuality": "FleetDataQuality(1d) | project ok = events > 0, detail = strcat('events=', events, ' dup%=', dup_pct, ' late%=', late_pct, ' v2%=', schema_v2_pct)",
}


def verify(truth: Path | None) -> None:
    rti = load_state("rti")
    db = rti["fleet_db_id"]
    failed = 0
    for name, q in CHECKS.items():
        try:
            r = kql(rti["query_uri"], db, q)[0]
            ok = bool(r["ok"])
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {r['detail']}")
        except SystemExit as e:
            ok = False
            print(f"[FAIL] {name}: {str(e)[:300]}")
        failed += not ok
    if truth and truth.exists():
        # Detection recall vs ground truth (informational). Anomalies may run past the end of the
        # generated history, so evaluate at min(anomaly end, data end).
        summary = json.loads((truth.parent.parent / "summary.json").read_text(encoding="utf-8"))
        data_end = summary["end"]
        rows = list(csv.DictReader(truth.open(encoding="utf-8")))
        for atype, fn, window in (("SOC_STUCK", "FleetBatteryAnomalies", "6h"),
                                  ("BATTERY_OVERTEMP", "FleetBatteryAnomalies", "6h"),
                                  ("INVERTER_FAULT", "FleetInverterFaults", "1h")):
            cases = [r for r in rows if r["anomaly_type"] == atype and r["start_time"] < data_end]
            hits = 0
            for t in cases:
                as_of = min(t["end_time"], data_end)
                # Evaluate like a scheduled detector: any hourly run during the anomaly counts as detected.
                q = (f"range as_of from datetime({t['start_time']}) + {window} to datetime({as_of}) + 1m step 1h "
                     f"| project as_of")
                points = [r["as_of"] for r in kql(rti["query_uri"], db, q)] or [as_of]
                found = False
                for pt in points:
                    c = f"{fn}({window}, datetime({pt})) | where device_id == '{t['device_id']}' | count"
                    if kql(rti["query_uri"], db, c)[0]["Count"] > 0:
                        found = True
                        break
                hits += found
                print(f"   {'hit ' if found else 'miss'} {atype} {t['device_id']} ({t['start_time'][:16]} -> {as_of[:16]})")
            if cases:
                print(f"[INFO] {atype} recall via {fn}: {hits}/{len(cases)}")
    if failed:
        raise SystemExit(f"{failed} check(s) failed")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["apply", "eventstream", "load-dims", "verify", "all"])
    p.add_argument("--force", action="store_true", help="re-apply the eventstream definition")
    p.add_argument("--truth", type=Path, default=DATA / "ground_truth" / "anomalies.csv")
    a = p.parse_args()
    load_env()
    if a.action in ("apply", "all"):
        apply()
    if a.action in ("eventstream", "all"):
        eventstream(a.force)
    if a.action in ("load-dims", "all"):
        load_dims()
    if a.action in ("verify", "all"):
        verify(a.truth)


if __name__ == "__main__":
    main()
