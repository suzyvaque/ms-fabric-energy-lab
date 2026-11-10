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


class PartialSendError(Exception):
    def __init__(self, failed: int, total: int, cause: Exception):
        super().__init__(f"{failed}/{total} events failed: {type(cause).__name__}: {cause}")
        self.failed = failed


class EventHubSink:
    """Works for both Azure Event Hubs and a Fabric Eventstream custom endpoint (Event Hub-compatible).

    Auth: a connection string (SAS; e.g. the Eventstream custom endpoint key) or Entra ID
    (`fully_qualified_namespace` + `eventhub_name`, DefaultAzureCredential, e.g. your az login),
    which is required when the Event Hubs namespace has local/SAS auth disabled.
    Transport: AMQP over WebSockets (443) by default - works through proxies/VPNs that block 5671.
    Sends batches over `senders` concurrent producer clients (throughput is usually RTT-bound).
    Sets record_type as an application property for routing."""

    def __init__(self, connection_string: str | None = None, eventhub_name: str | None = None,
                 fully_qualified_namespace: str | None = None, senders: int = 4, transport: str = "websocket"):
        try:
            from azure.eventhub import EventData, EventHubProducerClient, TransportType
        except ImportError as e:
            raise SystemExit("pip install -r data-gen/requirements.txt (azure-eventhub)") from e
        from concurrent.futures import ThreadPoolExecutor
        self._EventData = EventData
        tt = TransportType.AmqpOverWebsocket if transport == "websocket" else TransportType.Amqp
        if connection_string:
            kwargs = {"eventhub_name": eventhub_name} if eventhub_name else {}
            make = lambda: EventHubProducerClient.from_connection_string(connection_string, transport_type=tt, **kwargs)
        else:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as e:
                raise SystemExit("pip install -r data-gen/requirements.txt (azure-identity)") from e
            fqns = fully_qualified_namespace
            if fqns and "." not in fqns:
                fqns = f"{fqns}.servicebus.windows.net"
            cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
            make = lambda: EventHubProducerClient(fully_qualified_namespace=fqns, eventhub_name=eventhub_name,
                                                  credential=cred, transport_type=tt)
        self.clients = [make() for _ in range(max(1, senders))]
        self.pool = ThreadPoolExecutor(max_workers=len(self.clients))
        self.sent = 0

    def _send_chunk(self, client, events: list[dict]) -> int:
        batch = client.create_batch()
        for ev in events:
            data = self._EventData(json.dumps(ev, separators=(",", ":")))
            data.properties = {"record_type": ev["record_type"]}
            data.content_type = "application/json"
            try:
                batch.add(data)
            except ValueError:  # batch full
                client.send_batch(batch)
                batch = client.create_batch()
                batch.add(data)
        client.send_batch(batch)
        return len(events)

    def send(self, events: list[dict]) -> None:
        if not events:
            return
        n = len(self.clients)
        chunks = [events[i::n] for i in range(n)]
        futures = [(self.pool.submit(self._send_chunk, c, ch), len(ch)) for c, ch in zip(self.clients, chunks) if ch]
        failed, cause = 0, None
        for fut, size in futures:
            try:
                self.sent += fut.result()
            except Exception as e:  # noqa: BLE001 - counted and re-raised below
                failed += size
                cause = e
        if cause is not None:
            raise PartialSendError(failed, len(events), cause)

    def close(self) -> None:
        self.pool.shutdown(wait=True)
        for c in self.clients:
            c.close()

class TeeSink:
    """Sends an identical copy of every event to several labelled sinks (fair A/B ingestion tests).
    Each copy gets ingest_path=<label>. A failing target never blocks the others."""

    def __init__(self, targets: dict[str, object]):
        self.targets = targets
        self.stats = {label: {"sent": 0, "errors": 0, "failed_events": 0, "last_error": ""} for label in targets}

    def send(self, events: list[dict]) -> None:
        for label, sink in self.targets.items():
            copies = [dict(ev, ingest_path=label) for ev in events]
            try:
                sink.send(copies)
                self.stats[label]["sent"] += len(copies)
            except Exception as e:  # e.g. Event Hubs throttling (ServerBusy) when TUs are exhausted
                st = self.stats[label]
                failed = getattr(e, "failed", len(copies))
                st["errors"] += 1
                st["failed_events"] += failed
                st["sent"] += len(copies) - failed
                st["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"

    def close(self) -> None:
        for sink in self.targets.values():
            sink.close()


def build_target(spec: str, senders: int = 4, transport: str = "websocket"):
    """LABEL=conn:ENV_VAR  |  LABEL=entra:NAMESPACE_OR_$ENV_VAR/HUB"""
    import os
    label, _, target = spec.partition("=")
    kind, _, value = target.partition(":")
    if kind == "conn":
        cs = os.environ.get(value)
        if not cs:
            raise SystemExit(f"{value} is not set (.env or environment)")
        return label, EventHubSink(connection_string=cs, senders=senders, transport=transport)
    if kind == "entra":
        ns, _, hub = value.partition("/")
        if ns.startswith("$"):
            ns = os.environ.get(ns[1:], "")
        if not ns or not hub:
            raise SystemExit(f"bad target {spec}: use LABEL=entra:<namespace or $ENV>/<hub>")
        return label, EventHubSink(fully_qualified_namespace=ns, eventhub_name=hub, senders=senders,
                                   transport=transport)
    raise SystemExit(f"bad target {spec}: kind must be conn or entra")


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
