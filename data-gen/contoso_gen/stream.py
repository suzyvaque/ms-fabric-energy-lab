"""Live mode: real-time telemetry + alerts to stdout, files, Event Hubs or an Eventstream custom endpoint.

Same event contract as batch. Each event gets sent_time at actual send, so the ingestion
latency is ingestion_time() - sent_time in KQL (used by the step 1b comparison).
"""
from __future__ import annotations

import csv
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import GenConfig, iso
from .master import generate_master
from .telemetry import Fleet


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run_stream(cfg: GenConfig, sink, interval_sec: float, duration_min: float | None, boost: float,
               injects: list[str], inject_minutes: float, path_label: str | None, truth_dir: Path,
               log=print) -> dict:
    master = generate_master(cfg)
    fleet = Fleet(cfg, master, seed_suffix=f"live-{time.time_ns()}", live=True)
    dt = timedelta(seconds=interval_sec)
    start = _now()
    stop = start + timedelta(minutes=duration_min) if duration_min else None
    truth_dir.mkdir(parents=True, exist_ok=True)
    truth_path = truth_dir / f"stream-{start:%Y%m%dT%H%M%S}.csv"
    truth_fh = open(truth_path, "w", encoding="utf-8", newline="")
    truth = None

    def record(anoms) -> None:
        nonlocal truth
        for a in anoms:
            row = a.truth_row()
            if truth is None:
                truth = csv.DictWriter(truth_fh, fieldnames=list(row))
                truth.writeheader()
            truth.writerow(row)
            truth_fh.flush()
            log(f"anomaly {a.anomaly_type} at {a.site_id} until {iso(a.end)}")

    for spec in injects:
        atype, site_id = spec.split(":", 1)
        a = fleet.add_anomaly(atype.upper(), site_id, start, minutes=inject_minutes)
        if a is None:
            raise SystemExit(f"{site_id} has no device for {atype}")
        record([a])

    eps_per_tick = sum(len(s.devices) for s in fleet.sims) / interval_sec
    log(f"streaming ~{eps_per_tick:,.0f} events/s from {len(fleet.sims)} sites; truth -> {truth_path}")
    sent = 0
    errors = 0
    next_tick = start
    last_report, last_sent = time.monotonic(), 0
    try:
        while stop is None or _now() < stop:
            now = _now()
            if now >= next_tick:
                record(fleet.maybe_start_live_anomalies(next_tick, dt, boost))
                fleet.tick(next_tick, dt)
                next_tick += dt
                if _now() - next_tick > dt * 3:
                    log("WARNING: generator falling behind - reduce --sites or raise --interval-sec")
            due = fleet.delivery.pop_due(_now())
            if due:
                stamp = iso(_now())
                batch = []
                for _, ev in due:
                    ev["sent_time"] = stamp
                    if path_label:
                        ev["ingest_path"] = path_label
                    batch.append(ev)
                try:
                    sink.send(batch)
                    sent += len(batch)
                except Exception as e:  # keep streaming; transient endpoint errors are expected in demos
                    errors += 1
                    log(f"send error ({type(e).__name__}): {e}")
            if time.monotonic() - last_report >= 10:
                rate = (sent - last_sent) / (time.monotonic() - last_report)
                extra = ""
                if hasattr(sink, "stats"):
                    extra = " | " + ", ".join(f"{k}: sent {v['sent']:,} err {v['errors']}" for k, v in sink.stats.items())
                log(f"sent {sent:,} events ({rate:,.0f}/s), queued {len(fleet.delivery):,}, errors {errors}{extra}")
                last_report, last_sent = time.monotonic(), sent
            nt = fleet.delivery.next_time()
            waits = [(next_tick - _now()).total_seconds(), 1.0]
            if nt:
                waits.append((nt - _now()).total_seconds())
            wait = min(waits)
            if wait > 0:
                time.sleep(wait)
    except KeyboardInterrupt:
        log("stopped")
    finally:
        sink.close()
        truth_fh.close()
    result = {"sent": sent, "errors": errors, "anomalies": len(fleet.anomalies),
              "start": iso(start), "end": iso(_now())}
    if hasattr(sink, "stats"):
        result["targets"] = sink.stats
    return result
