"""CLI: py -m contoso_gen {batch,stream} ...  (run from the data-gen folder)."""
from __future__ import annotations

import argparse
import json
import os
import sys
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
    for stream in (sys.stdout, sys.stderr):  # paths may contain non-ASCII characters (e.g. localized folders)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
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
    s.add_argument("--target", action="append", default=[], metavar="LABEL=KIND:VALUE",
                   help="eventhub sink targets; repeat to tee identical events to several endpoints. "
                        "KIND conn:ENV_VAR (connection string, e.g. Eventstream custom endpoint) or "
                        "entra:NAMESPACE_OR_$ENV/HUB (Entra ID via az login). Each copy gets ingest_path=LABEL")
    s.add_argument("--connection-env", default="EVENTHUB_CONNECTION_STRING",
                   help="single target: env var (or .env key) holding a connection string")
    s.add_argument("--senders", type=int, default=4, help="concurrent producer clients per target")
    s.add_argument("--transport", choices=["websocket", "amqp"], default="websocket",
                   help="websocket = AMQP over 443 (works behind proxies/VPN); amqp = port 5671")
    s.add_argument("--eventhub-name", help="only if the connection string has no EntityPath")
    s.add_argument("--path-label", help="adds ingest_path=<label> to every event (step 1b comparison)")
    s.add_argument("--anomaly-boost", type=float, default=20.0, help="multiply anomaly rate for live demos")
    s.add_argument("--inject", action="append", default=[], metavar="TYPE:SITE_ID",
                   help="start an anomaly now, e.g. INVERTER_FAULT:SITE-000001 (repeatable)")
    s.add_argument("--inject-minutes", type=float, default=15.0)

    r = sub.add_parser("replay", help="send batch history (out/telemetry + out/alerts) to an endpoint; keeps original times")
    r.add_argument("--in", dest="inp", type=Path, default=HERE / "out", help="batch output folder")
    r.add_argument("--target", action="append", required=True, metavar="LABEL=KIND:VALUE")
    r.add_argument("--senders", type=int, default=8)
    r.add_argument("--transport", choices=["websocket", "amqp"], default="websocket")
    r.add_argument("--chunk", type=int, default=2000)
    a = p.parse_args()

    if a.cmd == "replay":
        from .sinks import TeeSink, build_target
        _load_dotenv()
        files = sorted((a.inp / "telemetry").rglob("*.jsonl")) + sorted((a.inp / "alerts").rglob("*.jsonl"))
        if not files:
            raise SystemExit(f"no batch output under {a.inp} - run `batch` first")
        sink = TeeSink(dict(build_target(t, a.senders, a.transport) for t in a.target))
        # Keep the producer's ingest_path label out of the payload for single-target replays.
        single = len(a.target) == 1
        total = 0
        for f in files:
            events = [json.loads(line) for line in f.open(encoding="utf-8")]
            for i in range(0, len(events), a.chunk):
                chunk = events[i:i + a.chunk]
                if single:
                    label, target = next(iter(sink.targets.items()))
                    try:
                        target.send(chunk)
                        sink.stats[label]["sent"] += len(chunk)
                    except Exception as e:  # noqa: BLE001
                        sink.stats[label]["errors"] += 1
                        sink.stats[label]["failed_events"] += getattr(e, "failed", len(chunk))
                else:
                    sink.send(chunk)
                total += len(chunk)
            print(f"{f.relative_to(a.inp)}: {len(events):,} (total {total:,})", flush=True)
        sink.close()
        print(json.dumps(sink.stats))
        return
    cfg = _cfg(a)

    if a.cmd == "batch":
        from .batch import run_batch
        summary = run_batch(cfg, a.out, a.format, telemetry=not a.no_telemetry)
        print(json.dumps(summary, indent=2))
        return

    from .sinks import EventHubSink, FileSink, StdoutSink, TeeSink, build_target
    from .stream import run_stream
    if a.sink == "eventhub":
        _load_dotenv()
        if a.target:
            sink = TeeSink(dict(build_target(t, a.senders, a.transport) for t in a.target))
        else:
            conn = os.environ.get(a.connection_env)
            if not conn:
                raise SystemExit(f"Set {a.connection_env} in .env (gitignored) or use --target")
            sink = EventHubSink(connection_string=conn, eventhub_name=a.eventhub_name, senders=a.senders,
                                transport=a.transport)
    elif a.sink == "file":
        sink = FileSink(a.out / "stream")
    else:
        sink = StdoutSink()
    log = (lambda m: print(m, file=sys.stderr, flush=True)) if a.sink == "stdout" else print
    result = run_stream(cfg, sink, a.interval_sec, a.duration_min, a.anomaly_boost, a.inject,
                        a.inject_minutes, a.path_label, a.out / "ground_truth", log=log)
    log(json.dumps(result))


if __name__ == "__main__":
    main()
