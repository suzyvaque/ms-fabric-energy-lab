"""Output sinks: JSONL files (landing zone layout), stdout, Azure Event Hubs / Eventstream custom endpoint."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


class FileSink:
    """telemetry/date=YYYY-MM-DD/hour=HH.jsonl and alerts/date=YYYY-MM-DD.jsonl, partitioned by sent_time."""

    def __init__(self, out: Path):
        self.out = out
        self._handles: dict[Path, object] = {}

    def _path(self, ev: dict) -> Path:
        st = ev["sent_time"]
        if ev["record_type"] == "telemetry":
            return self.out / "telemetry" / f"date={st[:10]}" / f"hour={st[11:13]}.jsonl"
        return self.out / "alerts" / f"date={st[:10]}.jsonl"

    def send(self, events: list[dict]) -> None:
        for ev in events:
            p = self._path(ev)
            fh = self._handles.get(p)
            if fh is None:
                if len(self._handles) > 48:
                    self.close()
                p.parent.mkdir(parents=True, exist_ok=True)
                fh = self._handles[p] = open(p, "a", encoding="utf-8", newline="\n")
            fh.write(json.dumps(ev, separators=(",", ":")) + "\n")

    def close(self) -> None:
        for fh in self._handles.values():
            fh.close()
        self._handles.clear()


class StdoutSink:
    def send(self, events: list[dict]) -> None:
        for ev in events:
            sys.stdout.write(json.dumps(ev, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def close(self) -> None:
        pass


class EventHubSink:
    """Works for both Azure Event Hubs and a Fabric Eventstream custom endpoint (Event Hub-compatible
    connection string). Batches events; sets record_type as an application property for routing."""

    def __init__(self, connection_string: str, eventhub_name: str | None = None):
        try:
            from azure.eventhub import EventData, EventHubProducerClient
        except ImportError as e:
            raise SystemExit("pip install -r data-gen/requirements.txt (azure-eventhub)") from e
        self._EventData = EventData
        kwargs = {"eventhub_name": eventhub_name} if eventhub_name else {}
        self.client = EventHubProducerClient.from_connection_string(connection_string, **kwargs)
        self.sent = 0

    def send(self, events: list[dict]) -> None:
        if not events:
            return
        batch = self.client.create_batch()
        for ev in events:
            data = self._EventData(json.dumps(ev, separators=(",", ":")))
            data.properties = {"record_type": ev["record_type"]}
            data.content_type = "application/json"
            try:
                batch.add(data)
            except ValueError:  # batch full
                self.client.send_batch(batch)
                batch = self.client.create_batch()
                batch.add(data)
        self.client.send_batch(batch)
        self.sent += len(events)

    def close(self) -> None:
        self.client.close()


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def write_parquet(path: Path, rows: list[dict]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as e:
        raise SystemExit("pip install pyarrow for --format parquet") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)
