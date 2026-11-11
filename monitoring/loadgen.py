"""Step 6: capacity load generator - reproduce utilization spikes and throttling on a small SKU (F2). Dev only.

  py monitoring/loadgen.py run   --minutes 15 --sql-workers 4 --dax-workers 2   # interactive load (SQL endpoint + DAX)
  py monitoring/loadgen.py watch --minutes 20                                    # capacity events, 1 line per minute
  py monitoring/loadgen.py recover                                               # pause -> resume (clears carry-forward; billed)

Why SQL + DAX: Eventhouse is billed by uptime (not per query), so heavy KQL barely moves CU. SQL analytics endpoint
queries and Power BI (Direct Lake) DAX queries are interactive operations, smoothed over 5 minutes, so they reach
the throttling stages quickly. Background jobs are smoothed over 24 h and hardly move the needle in a short test.

Throttling stages (learn.microsoft.com/fabric/enterprise/throttling): <=10 min future use = overage protection;
10-60 min = interactive delay (20 s); 60 min-24 h = interactive rejection; >24 h = background rejection.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))

from capacity_monitoring import query as capq  # noqa: E402
from common import load_env, log  # noqa: E402
from fabric_rest import find_item, tsql  # noqa: E402
from finance_lakehouse import lakehouse, ws  # noqa: E402

SQL = ("SELECT COUNT_BIG(*), AVG(CAST(a.pv_kw AS float) * b.load_kw) FROM dbo.fleet_telemetry a "
       "JOIN dbo.fleet_telemetry b ON a.site_id = b.site_id AND a.device_type = b.device_type")
DAX = ('EVALUATE ROW("x", COUNTROWS(CROSSJOIN(SELECTCOLUMNS(Payment, "a", Payment[payment_id]), '
       'SELECTCOLUMNS(Payment, "b", Payment[amount_due]))))')

stats = {"sql_ok": 0, "sql_err": 0, "dax_ok": 0, "dax_err": 0, "errors": {}}
lock = threading.Lock()


def record(kind: str, ok: bool, err: str = "") -> None:
    with lock:
        stats[f"{kind}_{'ok' if ok else 'err'}"] += 1
        if err:
            key = err[:90]
            stats["errors"][key] = stats["errors"].get(key, 0) + 1


def sql_worker(host: str, until: float) -> None:
    while time.time() < until:
        try:
            tsql(host, "lh_asset", SQL)
            record("sql", True)
        except Exception as ex:  # noqa: BLE001 - throttling surfaces as driver errors; keep going and count them
            record("sql", False, str(ex))
            time.sleep(5)


def dax_worker(until: float) -> None:
    from semantic_model import pbi
    w = ws("finance", "dev")
    model = find_item(w, "SemanticModel", "sm_finance")
    while time.time() < until:
        status, _, body = pbi("post", f"groups/{w}/datasets/{model['id']}/executeQueries", {"queries": [{"query": DAX}]})
        record("dax", status == 200, "" if status == 200 else f"{status} {str(body)[:200]}")
        if status != 200:
            time.sleep(5)


def watch_line() -> str:
    r = capq("capacity_utilization | top 1 by window_start | project window_start, utilization_pct, "
             "interactive_delay_pct, interactive_rejection_pct, background_rejection_pct, overage_total_cu_ms")
    if not r:
        return "no capacity events yet"
    x = r[0]
    return (f"{x['window_start'][11:19]} util {x['utilization_pct']:6.1f} %  delay-stage {x['interactive_delay_pct']:6.1f} %  "
            f"reject-stage {x['interactive_rejection_pct']:6.1f} %  bg-reject {x['background_rejection_pct']:5.1f} %  "
            f"overage {x['overage_total_cu_ms'] / 1000:8.0f} CU-s")


def run(minutes: float, sql_workers: int, dax_workers: int) -> None:
    host = lakehouse(ws("asset", "dev"), "lh_asset", "")["sql_host"]
    until = time.time() + minutes * 60
    threads = [threading.Thread(target=sql_worker, args=(host, until), daemon=True) for _ in range(sql_workers)]
    threads += [threading.Thread(target=dax_worker, args=(until,), daemon=True) for _ in range(dax_workers)]
    for t in threads:
        t.start()
    log(f"load: {sql_workers} SQL + {dax_workers} DAX workers for {minutes:g} min")
    while any(t.is_alive() for t in threads):
        time.sleep(60)
        try:
            line = watch_line()
        except SystemExit as ex:  # the monitoring query itself can be throttled
            line = f"monitoring query failed: {str(ex)[:80]}"
        with lock:
            log(f"{line} | sql {stats['sql_ok']}/{stats['sql_err']} dax {stats['dax_ok']}/{stats['dax_err']} (ok/err)")
    for k, v in sorted(stats["errors"].items(), key=lambda kv: -kv[1])[:5]:
        log(f"error x{v}: {k}")


def watch(minutes: float) -> None:
    until = time.time() + minutes * 60
    while time.time() < until:
        try:
            log(watch_line())
        except SystemExit as ex:
            log(f"monitoring query failed: {str(ex)[:80]}")
        time.sleep(60)


def recover() -> None:
    import subprocess
    infra = Path(__file__).resolve().parents[1] / "infra"
    for action in ("pause", "resume"):
        subprocess.run([sys.executable, str(infra / "capacity.py"), action], check=True, cwd=infra)
    log("capacity paused and resumed: carry-forward overage settled (billed), throttling cleared")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["run", "watch", "recover"])
    p.add_argument("--minutes", type=float, default=15)
    p.add_argument("--sql-workers", type=int, default=4)
    p.add_argument("--dax-workers", type=int, default=2)
    a = p.parse_args()
    load_env()
    if a.action == "run":
        run(a.minutes, a.sql_workers, a.dax_workers)
    elif a.action == "watch":
        watch(a.minutes)
    else:
        recover()


if __name__ == "__main__":
    main()
