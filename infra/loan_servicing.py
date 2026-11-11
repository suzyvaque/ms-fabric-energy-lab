"""Step 1d source system: Contoso "loan servicing" -> Fabric OPEN MIRRORING (mir_loan_servicing). Idempotent.

Simulates an external OLTP system publishing change files into the mirrored database landing zone:
  Files/LandingZone/{customers,contracts,payments}/_metadata.json   (keyColumns)
  Files/LandingZone/<table>/00000000000000000001.parquet            (initial snapshot, no __rowMarker__)
  Files/LandingZone/<table>/0000000000000000000N.parquet            (changes; __rowMarker__ last: 0 ins, 1 upd, 2 del)

  py infra/loan_servicing.py create            # mirrored DB item (GenericMirror) + start mirroring
  py infra/loan_servicing.py snapshot          # initial load from data-gen/out/finance/*.csv
  py infra/loan_servicing.py changes --payments 20 --status-updates 3 --delete-customer
  py infra/loan_servicing.py status            # per-table mirroring status
  py infra/loan_servicing.py latency           # publish 1 change, time until visible in the SQL analytics endpoint
  (all accept --env dev|prod)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import random
import time
from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from common import ROOT, env, load_env, load_state, log
from fabric_rest import b64, fabric, find_item, tsql, upsert_item
from onelake import ls, upload
from rti_lab import platform

MIRROR = "mir_loan_servicing"
DATA = ROOT / "data-gen" / "out" / "finance"
KEYS = {"customers": ["customer_id"], "contracts": ["contract_id"], "payments": ["payment_txn_id"]}

SCHEMAS = {
    "customers": pa.schema([("customer_id", pa.string()), ("first_name", pa.string()), ("last_name", pa.string()),
                            ("email", pa.string()), ("phone", pa.string()), ("street_address", pa.string()),
                            ("city", pa.string()), ("region", pa.string()), ("postal_code", pa.string()),
                            ("credit_band", pa.string()), ("credit_score", pa.int32()), ("signup_date", pa.date32()),
                            ("segment", pa.string()), ("marketing_opt_in", pa.bool_()),
                            ("updated_at", pa.timestamp("ms", tz="UTC"))]),
    "contracts": pa.schema([("contract_id", pa.string()), ("customer_id", pa.string()), ("site_id", pa.string()),
                            ("product", pa.string()), ("start_date", pa.date32()), ("term_months", pa.int32()),
                            ("system_cost", pa.float64()), ("principal", pa.float64()), ("apr_pct", pa.float64()),
                            ("ppa_rate_per_kwh", pa.float64()), ("escalator_pct", pa.float64()),
                            ("monthly_payment", pa.float64()), ("currency", pa.string()), ("status", pa.string()),
                            ("updated_at", pa.timestamp("ms", tz="UTC"))]),
    # payment_id is NOT unique on purpose: the servicing feed re-posts some payments (silver dedups them).
    "payments": pa.schema([("payment_txn_id", pa.int64()), ("payment_id", pa.string()), ("contract_id", pa.string()),
                           ("customer_id", pa.string()), ("installment_no", pa.int32()), ("due_date", pa.date32()),
                           ("amount_due", pa.float64()), ("method", pa.string()), ("paid_date", pa.date32()),
                           ("amount_paid", pa.float64()), ("status", pa.string()), ("days_late", pa.int32()),
                           ("recorded_at", pa.timestamp("ms", tz="UTC"))]),
}


def ws_id(e: str) -> str:
    return load_state("workspaces")[f"{env('WORKSPACE_PREFIX')}-finance-{e}"]["id"]


def mirror_id(e: str) -> str:
    item = find_item(ws_id(e), "MirroredDatabase", MIRROR)
    if not item:
        raise SystemExit(f"{MIRROR} not found in finance-{e}; run: py infra/loan_servicing.py create --env {e}")
    return item["id"]


def _cast(value: str, typ: pa.DataType):
    if value in ("", "None", None):
        return None
    if pa.types.is_boolean(typ):
        return value in ("True", "true", "1")
    if pa.types.is_integer(typ):
        return int(float(value))
    if pa.types.is_floating(typ):
        return float(value)
    if pa.types.is_date(typ):
        return date.fromisoformat(value[:10])
    if pa.types.is_timestamp(typ):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def to_parquet(table: str, rows: list[dict], marker: list[int] | None = None) -> bytes:
    schema = SCHEMAS[table]
    cols = {f.name: [_cast(r.get(f.name), f.type) if isinstance(r.get(f.name), str) or r.get(f.name) is None
                     else r.get(f.name) for r in rows] for f in schema}
    t = pa.table(cols, schema=schema)
    if marker is not None:  # must be the LAST column
        t = t.append_column("__rowMarker__", pa.array(marker, pa.int32()))
    buf = io.BytesIO()
    pq.write_table(t, buf, compression="snappy")
    return buf.getvalue()


def next_file(ws: str, mid: str, table: str) -> str:
    nums = [int(n.split(".")[0]) for n in ls(ws, mid, f"Files/LandingZone/{table}") if n[:20].isdigit()]
    return f"{(max(nums) + 1) if nums else 1:020d}.parquet"


def publish(e: str, table: str, rows: list[dict], marker: list[int] | None = None) -> str:
    ws, mid = ws_id(e), mirror_id(e)
    name = next_file(ws, mid, table)
    upload(ws, mid, f"Files/LandingZone/{table}/{name}", to_parquet(table, rows, marker))
    log(f"{table}: published {len(rows)} rows as {name}" + (" (changes)" if marker else " (snapshot)"))
    return name


def create(e: str) -> None:
    ws = ws_id(e)
    definition = {"properties": {
        "source": {"type": "GenericMirror", "typeProperties": {}},
        "target": {"type": "MountedRelationalDatabase",
                   "typeProperties": {"defaultSchema": "dbo", "format": "Delta", "retentionInDays": 7}}}}
    item = upsert_item(ws, "MirroredDatabase", MIRROR, [
        {"path": "mirroring.json", "payload": b64(definition), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("MirroredDatabase", MIRROR)), "payloadType": "InlineBase64"}],
        "Contoso loan servicing system (open mirroring)", update=False)
    status_url = f"workspaces/{ws}/mirroredDatabases/{item['id']}/getMirroringStatus"
    state = fabric("post", status_url).get("status")
    for _ in range(40):  # a new mirror reports Initializing for a short while; start is only allowed after
        if state != "Initializing":
            break
        time.sleep(15)
        state = fabric("post", status_url).get("status")
    if state not in ("Running", "Starting"):
        fabric("post", f"workspaces/{ws}/mirroredDatabases/{item['id']}/startMirroring")
    upload(ws, item["id"], "Files/LandingZone/_partnerEvents.json",
           json.dumps({"partnerName": "ContosoEnergyLab", "sourceInfo": {"sourceType": "LoanServicing-OLTP",
                                                                         "sourceVersion": "1.0"}}).encode())
    for table, keys in KEYS.items():
        if "_metadata.json" not in ls(ws, item["id"], f"Files/LandingZone/{table}"):
            upload(ws, item["id"], f"Files/LandingZone/{table}/_metadata.json", json.dumps({"keyColumns": keys}).encode())
    log(f"{MIRROR} ready in finance-{e} (mirroring {state or 'started'})")


def _rows(name: str) -> list[dict]:
    with (DATA / f"{name}.csv").open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def snapshot(e: str) -> None:
    ws, mid = ws_id(e), mirror_id(e)
    now = datetime.now(timezone.utc)
    for table in KEYS:
        if any(n[:20].isdigit() for n in ls(ws, mid, f"Files/LandingZone/{table}")):
            log(f"{table}: snapshot already published")
            continue
        rows = _rows(table)
        if table == "payments":
            for i, r in enumerate(rows, 1):
                r["payment_txn_id"] = i
        else:
            for r in rows:
                r["updated_at"] = now
        publish(e, table, rows)


def _sql_endpoint(e: str) -> tuple[str, str]:
    props = fabric("get", f"workspaces/{ws_id(e)}/mirroredDatabases/{mirror_id(e)}")["properties"]
    ep = props["sqlEndpointProperties"]
    return ep["connectionString"], MIRROR


def sql(e: str, query: str) -> list[tuple]:
    host, db = _sql_endpoint(e)
    return tsql(host, db, query)


def changes(e: str, n_payments: int, n_status: int, delete_customer: bool) -> str:
    """Incremental changes: new payments (insert), contract status changes (update), optional customer delete."""
    rng = random.Random()
    contracts = _rows("contracts")
    active = [c for c in contracts if c["status"] == "Active"]
    now = datetime.now(timezone.utc)
    marker = f"PAY-LIVE-{now:%Y%m%d%H%M%S}"
    base = int(now.timestamp() * 1000)  # unique, increasing surrogate keys for new payment transactions
    pays = []
    for i, c in enumerate(rng.sample(active, min(n_payments, len(active)))):
        pays.append({"payment_txn_id": base + i, "payment_id": f"{marker}-{i:03d}", "contract_id": c["contract_id"],
                     "customer_id": c["customer_id"], "installment_no": 0, "due_date": now.date(),
                     "amount_due": float(c["monthly_payment"]), "method": "ACH", "paid_date": now.date(),
                     "amount_paid": float(c["monthly_payment"]), "status": "OnTime", "days_late": 0, "recorded_at": now})
    if pays:
        publish(e, "payments", pays, [0] * len(pays))
    if n_status:
        upd = [dict(c, status=rng.choice(["Delinquent", "Active"]), updated_at=now) for c in rng.sample(active, n_status)]
        publish(e, "contracts", upd, [1] * len(upd))
    if delete_customer:  # e.g. a right-to-erasure request processed in the source system
        cust = rng.choice(_rows("customers"))
        publish(e, "customers", [{"customer_id": cust["customer_id"]}], [2])
        log(f"customers: delete published for {cust['customer_id']}")
    return marker


def status(e: str) -> None:
    ws, mid = ws_id(e), mirror_id(e)
    print("mirroring:", fabric("post", f"workspaces/{ws}/mirroredDatabases/{mid}/getMirroringStatus").get("status"))
    for t in fabric("post", f"workspaces/{ws}/mirroredDatabases/{mid}/getTablesMirroringStatus").get("data", []):
        m = t.get("metrics", {})
        print(f"  {t.get('sourceTableName'):10} {t.get('status'):12} rows={m.get('processedRows')} "
              f"last_sync={m.get('lastSyncDateTime')}")


def latency(e: str, timeout_s: int = 900) -> float:
    marker = changes(e, 1, 0, False)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            n = sql(e, f"SELECT COUNT(*) FROM dbo.payments WHERE payment_id LIKE '{marker}%'")[0][0]
        except Exception as ex:  # noqa: BLE001 - endpoint metadata sync may lag on first use
            n = 0
            log(f"query retry: {type(ex).__name__}")
        if n:
            secs = time.time() - t0
            log(f"change visible in SQL analytics endpoint after {secs:.0f}s")
            return secs
        time.sleep(10)
    raise SystemExit("change not visible within timeout")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "snapshot", "changes", "status", "latency"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    p.add_argument("--payments", type=int, default=20)
    p.add_argument("--status-updates", type=int, default=3)
    p.add_argument("--delete-customer", action="store_true")
    a = p.parse_args()
    load_env()
    {"create": lambda: create(a.env), "snapshot": lambda: snapshot(a.env),
     "changes": lambda: changes(a.env, a.payments, a.status_updates, a.delete_customer),
     "status": lambda: status(a.env), "latency": lambda: latency(a.env)}[a.action]()


if __name__ == "__main__":
    main()
