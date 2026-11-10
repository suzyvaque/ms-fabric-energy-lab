"""Step 1b benchmark: identical events -> (i) Azure Event Hubs -> Eventstream and (ii) Eventstream custom
endpoint, both landing in the same Eventhouse table (lab_compare.compare_events). Scores latency,
throughput, loss and schema handling per stage with KQL.

  cd data-gen
  py benchmarks/ingest_compare.py                                   # default stages
  py benchmarks/ingest_compare.py --stages 50:5:4,500:5:4,1500:5:4 --senders 8

Stage = sites:interval_sec:minutes (events/s per path ~= sites * 3.9 / interval_sec).
Requires: infra/eventhubs.py create + infra/rti_lab.py create; az login (Entra sender role).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HERE), str(HERE.parent / "infra")]

from contoso_gen.__main__ import _load_dotenv  # noqa: E402
from contoso_gen.config import GenConfig, iso  # noqa: E402
from contoso_gen.sinks import TeeSink, build_target  # noqa: E402
from contoso_gen.stream import run_stream  # noqa: E402

TARGETS = ["eventhubs=entra:$EVENTHUB_NAMESPACE/fleet-telemetry", "es-custom=conn:ESTREAM_CONNECTION_STRING"]

STAGE_QUERY = """compare_events
| where sent_time between (datetime({start}) .. datetime({end})) and record_type in ('telemetry','alert')
| extend ingest = ingestion_time()
| extend lat = (ingest - sent_time) / 1s, enq = (EventEnqueuedUtcTime - sent_time) / 1s,
         proc = (EventProcessedUtcTime - sent_time) / 1s
| summarize rows = count(), p50 = percentile(lat, 50), p95 = percentile(lat, 95), p99 = percentile(lat, 99),
            max_lat = max(lat), enq_p50 = percentile(enq, 50), proc_p50 = percentile(proc, 50),
            v2 = countif(schema_version == 2), alerts = countif(record_type == 'alert') by ingest_path"""
PEAK_QUERY = """compare_events
| where sent_time between (datetime({start}) .. datetime({end}))
| summarize n = count() by ingest_path, bin(ingestion_time(), 1m)
| summarize peak_per_min = max(n) by ingest_path"""
MALFORMED_QUERY = """compare_events | where ingestion_time() > datetime({start}) | where isempty(record_type)
| summarize rows = count() by ingest_path"""


def send_malformed(sink: TeeSink, n: int = 20) -> None:
    from azure.eventhub import EventData
    for target in sink.targets.values():
        client = target.clients[0]
        batch = client.create_batch()
        for i in range(n):
            batch.add(EventData("{not-json " + str(i)))
        client.send_batch(batch)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("--stages", default="50:5:4,500:5:4,1500:5:4")
    p.add_argument("--senders", type=int, default=8)
    p.add_argument("--settle-min", type=float, default=3.0)
    p.add_argument("--out", type=Path, default=HERE / "out" / "benchmarks")
    a = p.parse_args()
    _load_dotenv()
    from common import load_env, load_state
    from fabric_rest import kql
    load_env()
    rti = load_state("rti")
    stages = [tuple(float(x) for x in s.split(":")) for s in a.stages.split(",")]
    results = {"started": iso(datetime.now(timezone.utc)), "senders": a.senders, "stages": []}

    for sites, interval, minutes in stages:
        sink = TeeSink(dict(build_target(t, a.senders) for t in TARGETS))
        print(f"\n== stage {int(sites)} sites / {interval}s for {minutes} min "
              f"(~{sites * 3.9 / interval:,.0f} events/s per path)", flush=True)
        r = run_stream(GenConfig(sites=int(sites), seed=42), sink, interval, minutes, 1.0, [], 0,
                       None, HERE / "out" / "ground_truth", log=lambda m: print(m, flush=True))
        r.update(sites=int(sites), interval_sec=interval, minutes=minutes,
                 target_eps=round(sites * 3.9 / interval))
        results["stages"].append(r)

    mal_start = iso(datetime.now(timezone.utc))
    sink = TeeSink(dict(build_target(t, 1) for t in TARGETS))
    send_malformed(sink)
    sink.close()
    print(f"\nwaiting {a.settle_min} min for ingestion to settle...", flush=True)
    time.sleep(a.settle_min * 60)

    for st in results["stages"]:
        rows = kql(rti["query_uri"], rti["lab_db_id"], STAGE_QUERY.format(start=st["start"], end=st["end"]))
        peaks = {r["ingest_path"]: r["peak_per_min"] for r in
                 kql(rti["query_uri"], rti["lab_db_id"], PEAK_QUERY.format(start=st["start"], end=st["end"]))}
        duration = (datetime.fromisoformat(st["end"][:-1]) - datetime.fromisoformat(st["start"][:-1])).total_seconds()
        st["kql"] = {}
        for row in rows:
            path = row["ingest_path"]
            sent = st["targets"][path]["sent"]
            k = {key: (round(v, 2) if isinstance(v, float) else v) for key, v in row.items() if key != "ingest_path"}
            k.update(sent=sent, failed_send=st["targets"][path]["failed_events"],
                     lost_after_accept=sent - row["rows"], peak_per_min=peaks.get(path),
                     achieved_eps=round(sent / duration, 1), last_error=st["targets"][path]["last_error"][:160])
            st["kql"][path] = k
    results["malformed"] = {"sent_per_path": 20, "landed": kql(rti["query_uri"], rti["lab_db_id"],
                                                               MALFORMED_QUERY.format(start=mal_start))}
    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / f"ingest-compare-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print("\n| target ev/s | path | achieved ev/s | sent | send failures | lost after accept | p50 s | p95 s | "
          "p99 s | peak rows/min | schema v2 rows |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for st in results["stages"]:
        for pth, k in sorted(st.get("kql", {}).items()):
            print(f"| {st['target_eps']} | {pth} | {k['achieved_eps']} | {k['sent']:,} | {k['failed_send']:,} | "
                  f"{k['lost_after_accept']:,} | {k['p50']} | {k['p95']} | {k['p99']} | {k['peak_per_min']:,} | "
                  f"{k['v2']:,} |")
    print(f"\nmalformed: {results['malformed']}\nresults -> {path}")


if __name__ == "__main__":
    main()
