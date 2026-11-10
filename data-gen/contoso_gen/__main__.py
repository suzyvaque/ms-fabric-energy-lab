"""CLI: py -m contoso_gen {batch,stream} ...  (run from the data-gen folder)."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import GenConfig, utc_hour_now

HERE = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    env = HERE.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _cfg(a: argparse.Namespace) -> GenConfig:
    end = datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc) if getattr(a, "end", None) else utc_hour_now()
    cfg = GenConfig(sites=a.sites, seed=a.seed, end=end)
    for k in ("days", "interval_min", "dup_rate", "late_rate", "anomaly_rate", "schema_drift_rate"):
        if getattr(a, k, None) is not None:
            setattr(cfg, k, getattr(a, k))
    return cfg


def main() -> None:
    p = argparse.ArgumentParser(prog="contoso_gen", description="Contoso Energy synthetic data (fictional)")
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--sites", type=int, default=200)
    common.add_argument("--seed", type=int, default=42, help="same seed + sites = same master data / IDs")
    common.add_argument("--out", type=Path, default=HERE / "out")
    common.add_argument("--dup-rate", type=float)
    common.add_argument("--late-rate", type=float)
    common.add_argument("--anomaly-rate", type=float, help="anomalies per device-day")
    common.add_argument("--schema-drift-rate", type=float, help="share of sites on firmware 2.x (schema v2)")

    b = sub.add_parser("batch", parents=[common], help="history: master, finance, telemetry, alerts")
    b.add_argument("--days", type=int, default=7)
    b.add_argument("--interval-min", type=int, default=5)
    b.add_argument("--end", help="UTC end hour, e.g. 2026-11-01T00:00 (default: current hour)")
    b.add_argument("--format", choices=["csv", "parquet"], default="csv", help="master/finance tables")
    b.add_argument("--no-telemetry", action="store_true", help="master + finance only")

    s = sub.add_parser("stream", parents=[common], help="live events")
    s.add_argument("--interval-sec", type=float, default=10.0, help="reading interval per device")
    s.add_argument("--duration-min", type=float, help="stop after N minutes (default: until Ctrl+C)")
    s.add_argument("--sink", choices=["stdout", "file", "eventhub"], default="stdout")
    s.add_argument("--connection-env", default="EVENTHUB_CONNECTION_STRING",
                   help="env var (or .env key) holding the Event Hubs / Eventstream connection string")
    s.add_argument("--eventhub-name", help="only if the connection string has no EntityPath")
    s.add_argument("--path-label", help="adds ingest_path=<label> to every event (step 1b comparison)")
    s.add_argument("--anomaly-boost", type=float, default=20.0, help="multiply anomaly rate for live demos")
    s.add_argument("--inject", action="append", default=[], metavar="TYPE:SITE_ID",
                   help="start an anomaly now, e.g. INVERTER_FAULT:SITE-000001 (repeatable)")
    s.add_argument("--inject-minutes", type=float, default=15.0)
    a = p.parse_args()
    cfg = _cfg(a)

    if a.cmd == "batch":
        from .batch import run_batch
        summary = run_batch(cfg, a.out, a.format, telemetry=not a.no_telemetry)
        print(json.dumps(summary, indent=2))
        return

    from .sinks import EventHubSink, FileSink, StdoutSink
    from .stream import run_stream
    if a.sink == "eventhub":
        _load_dotenv()
        conn = os.environ.get(a.connection_env)
        if not conn:
            raise SystemExit(f"Set {a.connection_env} in .env (gitignored) or the environment")
        sink = EventHubSink(conn, a.eventhub_name)
    elif a.sink == "file":
        sink = FileSink(a.out / "stream")
    else:
        sink = StdoutSink()
    log = (lambda m: print(m, file=__import__("sys").stderr, flush=True)) if a.sink == "stdout" else print
    result = run_stream(cfg, sink, a.interval_sec, a.duration_min, a.anomaly_boost, a.inject,
                        a.inject_minutes, a.path_label, a.out / "ground_truth", log=log)
    log(json.dumps(result))


if __name__ == "__main__":
    main()
