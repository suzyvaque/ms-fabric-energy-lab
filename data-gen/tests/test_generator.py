"""Contract tests for the generator. Run: cd data-gen; py -m pytest -q"""
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contoso_gen.batch import run_batch
from contoso_gen.config import GenConfig
from contoso_gen.finance import generate_payments
from contoso_gen.master import generate_master
from contoso_gen.telemetry import ANOMALY_TYPES

END = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    cfg = GenConfig(sites=40, days=2, end=END, seed=7, dup_rate=0.01, late_rate=0.02)
    summary = run_batch(cfg, out, log=lambda *_: None)
    events = [json.loads(line) for f in (out / "telemetry").rglob("*.jsonl") for line in f.open(encoding="utf-8")]
    alerts = [json.loads(line) for f in (out / "alerts").rglob("*.jsonl") for line in f.open(encoding="utf-8")]
    return out, summary, events, alerts


def _csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_master_is_deterministic():
    a = generate_master(GenConfig(sites=30, seed=1, end=END))
    b = generate_master(GenConfig(sites=30, seed=1, end=END + (END - END.replace(month=1))))
    assert a == b, "master data must not depend on run date (shared IDs for batch + stream)"


def test_referential_integrity(batch):
    out = batch[0]
    customers = {r["customer_id"] for r in _csv(out / "finance" / "customers.csv")}
    sites = {r["site_id"]: r for r in _csv(out / "asset" / "sites.csv")}
    devices = _csv(out / "asset" / "devices.csv")
    assert all(s["customer_id"] in customers for s in sites.values())
    assert all(d["site_id"] in sites for d in devices)
    contracts = _csv(out / "finance" / "contracts.csv")
    assert all(c["site_id"] in sites and c["customer_id"] in customers for c in contracts)
    contract_ids = {c["contract_id"] for c in contracts}
    assert all(p["contract_id"] in contract_ids for p in _csv(out / "finance" / "payments.csv"))
    device_ids = {d["device_id"] for d in devices}
    assert {e["device_id"] for e in batch[2]} <= device_ids


def test_no_real_pii(batch):
    for c in _csv(batch[0] / "finance" / "customers.csv"):
        assert c["email"].endswith("@example.com")
        assert c["phone"].startswith("+1-555-01")


def test_event_contract(batch):
    required = {"record_type", "schema_version", "event_id", "device_id", "site_id", "device_type",
                "event_time", "sent_time", "seq", "firmware", "status", "fault_code", "measurements"}
    for e in batch[2][:5000]:
        assert required <= e.keys()
        assert e["sent_time"] >= e["event_time"] or e["device_id"] in {
            r["device_id"] for r in _csv(batch[0] / "ground_truth" / "anomalies.csv")
            if r["anomaly_type"] == "CLOCK_SKEW"}


def test_physics_bounds(batch):
    events = batch[2]
    socs = [e["measurements"]["soc_pct"] for e in events if e["device_type"] == "battery"]
    assert 0 <= min(socs) and max(socs) <= 100
    night = [e["measurements"].get("pv_kw", 0) for e in events
             if e["device_type"] == "inverter" and e["schema_version"] == 1 and e["event_time"][11:13] in ("10", "11")]
    assert max(night) == 0  # 10-11 UTC is night across the fleet's longitudes


def test_quality_injection(batch):
    s, events = batch[1], batch[2]
    ids = Counter(e["event_id"] for e in events)
    assert sum(v - 1 for v in ids.values()) == s["telemetry_duplicates"] > 0
    assert s["telemetry_late_gt_5min"] > 0
    assert s["telemetry_schema_v2"] > 0
    v2 = next(e for e in events if e["schema_version"] == 2 and e["device_type"] == "inverter")
    assert "dc_power_w" in v2["measurements"] and "pv_kw" not in v2["measurements"]


def test_anomalies_have_ground_truth_and_signatures(batch):
    out, s, events, alerts = batch
    truth = _csv(out / "ground_truth" / "anomalies.csv")
    assert set(ANOMALY_TYPES) <= {t["anomaly_type"] for t in truth}
    fault = next(t for t in truth if t["anomaly_type"] == "INVERTER_FAULT")
    faulty = [e for e in events if e["device_id"] == fault["device_id"]
              and fault["start_time"] <= e["event_time"] < fault["end_time"]]
    assert faulty and all(e["status"] == "FAULT" and e["measurements"]["pv_kw" if e["schema_version"] == 1
                          else "dc_power_w"] == 0 for e in faulty)
    assert {a["event_type"] for a in alerts} == {"raised", "cleared"}


def test_payments_behaviour():
    cfg = GenConfig(sites=400, seed=3, end=END)
    master = generate_master(cfg)
    payments = generate_payments(cfg, master)
    status = Counter(p["status"] for p in payments)
    assert status["OnTime"] / len(payments) > 0.8
    assert status["Late"] > 0 and status["Missed"] > 0
    assert len(payments) > len({p["payment_id"] for p in payments})  # duplicates injected
    assert {"Active", "Default"} <= {c["status"] for c in master["contracts"]}


def test_credit_bureau_files():
    from contoso_gen.finance import generate_credit_bureau
    cfg = GenConfig(sites=200, seed=3, end=END)
    master = generate_master(cfg)
    payments = generate_payments(cfg, master)
    files = generate_credit_bureau(cfg, master, payments, months=6)
    monthly = [n for n in files if not n.endswith("_v2.csv")]
    assert len(monthly) == 6 and any(n.endswith("_v2.csv") for n in files)
    customers = {c["customer_id"] for c in master["customers"]}
    assert all({r["customer_id"] for r in files[n]} == customers for n in monthly)
    assert all(300 <= r["bureau_score"] <= 850 for rows in files.values() for r in rows)
    defaulted = {c["customer_id"] for c in master["contracts"] if c["status"] == "Default"}
    first, last = files[monthly[0]], files[monthly[-1]]
    drop = [next(r["bureau_score"] for r in first if r["customer_id"] == c) -
            next(r["bureau_score"] for r in last if r["customer_id"] == c) for c in defaulted]
    assert drop and sum(drop) / len(drop) > 20  # defaulted customers deteriorate


def test_stream_delayed_injection(tmp_path):
    """TYPE:SITE@+MIN starts the anomaly MIN minutes after the stream start (controlled trials, step 12)."""
    from contoso_gen.stream import run_stream

    class Sink:
        def send(self, batch):
            pass

        def close(self):
            pass

    cfg = GenConfig(sites=3, seed=7)
    out = run_stream(cfg, Sink(), interval_sec=60, duration_min=0.02, boost=0,
                     injects=["INVERTER_FAULT:SITE-000001@+30"], inject_minutes=10, path_label=None,
                     truth_dir=tmp_path, log=lambda *_: None)
    import csv
    rows = list(csv.DictReader(next(tmp_path.glob("stream-*.csv")).open(encoding="utf-8")))
    from datetime import datetime
    start = datetime.fromisoformat(out["start"].replace("Z", "+00:00"))
    began = datetime.fromisoformat(rows[0]["start_time"].replace("Z", "+00:00"))
    assert rows[0]["anomaly_type"] == "INVERTER_FAULT"
    assert 29.9 <= (began - start).total_seconds() / 60 <= 30.1
