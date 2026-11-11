"""Batch mode: master + finance tables, N days of telemetry/alert history, ground truth, summary."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import GenConfig, iso
from .finance import generate_credit_bureau, generate_payments
from .master import generate_master
from .sinks import FileSink, write_csv, write_parquet
from .telemetry import Fleet

TABLES = {"asset": ["sites", "devices", "batteries"], "finance": ["customers", "contracts"]}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def run_batch(cfg: GenConfig, out: Path, fmt: str = "csv", telemetry: bool = True, log=print) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    master = generate_master(cfg)
    payments = generate_payments(cfg, master)  # also finalizes contract status
    writer = write_parquet if fmt == "parquet" else write_csv
    for folder, names in TABLES.items():
        for name in names:
            writer(out / folder / f"{name}.{fmt}", master[name])
    writer(out / "finance" / f"payments.{fmt}", payments)
    for name, rows in generate_credit_bureau(cfg, master, payments).items():
        write_csv(out / "finance" / "credit_bureau" / name, rows)
    log(f"master: {len(master['sites'])} sites, {len(master['devices'])} devices, "
        f"{len(master['contracts'])} contracts, {len(payments)} payments")

    summary: dict = {
        "generated_at": iso(datetime.now(tz=cfg.end.tzinfo)), "seed": cfg.seed, "start": iso(cfg.start),
        "end": iso(cfg.end), "sites": len(master["sites"]), "devices": len(master["devices"]),
        "customers": len(master["customers"]), "contracts": len(master["contracts"]),
        "contract_status": dict(Counter(c["status"] for c in master["contracts"])),
        "payments": len(payments), "payment_status": dict(Counter(p["status"] for p in payments)),
        "payment_duplicates": len(payments) - len({p["payment_id"] for p in payments}),
    }
    if not telemetry:
        (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    fleet = Fleet(cfg, master)
    fleet.plan_batch_anomalies()
    sink = FileSink(out)
    counts: Counter = Counter()
    seen: set[str] = set()
    late = v2 = 0

    def emit(due: list) -> None:
        nonlocal late, v2
        batch = []
        for at, ev in due:
            ev["sent_time"] = iso(at)
            counts[ev["record_type"]] += 1
            if ev["record_type"] == "telemetry":
                if ev["event_id"] in seen:
                    counts["telemetry_duplicates"] += 1
                seen.add(ev["event_id"])
                if (at - _parse(ev["event_time"])).total_seconds() > 300:
                    late += 1
                v2 += ev["schema_version"] == 2
            batch.append(ev)
        sink.send(batch)

    t = cfg.start
    total_ticks = int((cfg.end - cfg.start) / cfg.interval)
    for i in range(total_ticks):
        fleet.tick(t, cfg.interval)
        emit(fleet.delivery.pop_due(t))
        t += cfg.interval
        if i % max(1, total_ticks // 10) == 0:
            log(f"telemetry {i * 100 // total_ticks:3d}%  events so far: {counts['telemetry']:,}")
    emit(fleet.delivery.pop_due(cfg.end))
    undelivered = len(fleet.delivery)
    sink.close()

    write_csv(out / "ground_truth" / "anomalies.csv", fleet.truth_rows())
    write_csv(out / "ground_truth" / "alerts.csv", fleet.alert_truth)
    summary.update({
        "telemetry_events": counts["telemetry"], "telemetry_duplicates": counts["telemetry_duplicates"],
        "telemetry_late_gt_5min": late, "telemetry_schema_v2": v2, "alert_events": counts["alert"],
        "undelivered_at_end": undelivered,
        "anomalies": dict(Counter(a.anomaly_type for a in fleet.anomalies)),
        "skewed_devices": len(fleet.skew),
        "false_positive_alerts": sum(1 for a in fleet.alert_truth if a["false_positive"]),
    })
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
