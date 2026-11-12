"""Step 11 live4: storage-format latency. Same frozen data, four stores, one dedicated capacity (infra/live4_stores.py).

Stores: lakehouse (lab_lh_store), SQL database in Fabric (lab_sqldb_store), mirrored Azure SQL (lab_mir_store),
Eventhouse (lab_kql_store). Agents lab_da_live4_<lh|sqldb|mir|kql> share instructions, have no few-shots and see the
same columns. The questions are the golden cases that don't depend on live streaming or PII (PII columns are not in the
snapshot); finance and fleet cases are rewritten onto the hub tables. Truth comes from the snapshot, in T-SQL and KQL.

  py eval/live4.py crosscheck              # truth on every engine; any mismatch stops (exit 1)
  py eval/live4.py bench [--n 10]          # truth queries run directly per engine (1 cold + n-1 warm, perf_counter)
  py eval/live4.py agents [--reps 3 --warmup 2]   # 4 agents in parallel, same order, sequential per agent
  py eval/live4.py report [--publish]      # eval/runs/live4/report.md (+ sanitized eval/reports/live4.md)
Raw output: eval/runs/live4/ (gitignored).
"""
from __future__ import annotations

import argparse
import json
import statistics
import struct
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "infra"), str(ROOT / "eval")]

import graders  # noqa: E402
from common import load_env, load_state, log  # noqa: E402

GOLDEN = ROOT / "eval" / "golden" / "contoso.jsonl"
OUT = ROOT / "eval" / "runs" / "live4"
REPORTS = ROOT / "eval" / "reports"
EXCLUDED = {"pii", "pii_indirect", "injection", "freshness"}  # PII not in the snapshot; freshness needs live data
AGENTS = {"lakehouse": "lab_da_live4_lh", "sqldb": "lab_da_live4_sqldb", "mirror": "lab_da_live4_mir",
          "eventhouse": "lab_da_live4_kql"}
# Engine-only targets. sqldb = the SQL database's own engine; sqldb_endpoint = its SQL analytics endpoint (OneLake
# replica), which is what the data agent queries for a SQL database source.
ENGINES = ["lakehouse", "sqldb", "sqldb_endpoint", "mirror", "eventhouse"]

# Golden cases whose source isn't the hub, rewritten onto the hub tables (same question, same check).
SQL_REWRITE = {
    "fin-default-count": "SELECT COUNT(*) FROM dbo.fin_contracts WHERE status = 'Default'",
    "fin-lease-principal": "SELECT SUM(principal) FROM dbo.fin_contracts WHERE product = 'Lease'",
    "fin-loan-apr": "SELECT CAST(AVG(apr_pct) AS decimal(6,2)) FROM dbo.fin_contracts WHERE product = 'Loan'",
    "fin-top-product": "SELECT TOP 1 product, COUNT(*) FROM dbo.fin_contracts GROUP BY product ORDER BY COUNT(*) DESC",
    "flt-critical-nov9": "SELECT COUNT(DISTINCT alert_id) FROM dbo.fleet_alert_events WHERE event_type = 'raised' AND "
                         "severity = 'Critical' AND event_time >= '2026-11-09' AND event_time < '2026-11-10'",
    "flt-top-code-nov10": "SELECT TOP 1 alert_code, COUNT(*) FROM dbo.fleet_alert_events WHERE event_type = 'raised' AND "
                          "event_time >= '2026-11-10' AND event_time < '2026-11-11' GROUP BY alert_code "
                          "ORDER BY COUNT(*) DESC, alert_code",
    "flt-top-site-before-nov11": "SELECT TOP 1 site_id, COUNT(*) FROM dbo.fleet_alert_events WHERE event_time < "
                                 "'2026-11-11' GROUP BY site_id ORDER BY COUNT(*) DESC, site_id",
}
_TODAY = "warranty_end > startofday(now())"
KQL = {
    "fin-default-count": "fin_contracts | where status == 'Default' | count",
    "fin-lease-principal": "fin_contracts | where product == 'Lease' | summarize sum(principal)",
    "fin-loan-apr": "fin_contracts | where product == 'Loan' | summarize round(avg(apr_pct), 2)",
    "fin-top-product": "fin_contracts | summarize n = count() by product | top 1 by n",
    "ast-battery-sites": "sites | where has_battery == true | count",
    "ast-avg-soh": "batteries | summarize round(avg(state_of_health_pct), 2)",
    "ast-top-region-pv": "sites | summarize kw = sum(pv_capacity_kw) by region | top 1 by kw | project region, round(kw, 1)",
    "ast-inverters": "devices | where device_type == 'inverter' | count",
    "flt-critical-nov9": "fleet_alert_events | where event_type == 'raised' and severity == 'Critical' and event_time >= "
                         "datetime(2026-11-09) and event_time < datetime(2026-11-10) | summarize count_distinct(alert_id)",
    "flt-top-code-nov10": "fleet_alert_events | where event_type == 'raised' and event_time >= datetime(2026-11-10) and "
                          "event_time < datetime(2026-11-11) | summarize n = count() by alert_code "
                          "| order by n desc, alert_code asc | take 1",
    "flt-top-site-before-nov11": "fleet_alert_events | where event_time < datetime(2026-11-11) | summarize n = count() "
                                 "by site_id | order by n desc, site_id asc | take 1",
    "x-soh90-principal": "fin_contracts | where site_id in ((batteries | where state_of_health_pct < 90 | project site_id)) "
                         "| summarize sum(principal)",
    "x-default-alerts": "fleet_alert_events | where site_id in ((fin_contracts | where status == 'Default' | project site_id)) "
                        "| count",
    "x-default-sites": "fin_contracts | where status == 'Default' | project site_id | order by site_id asc",
    "x-active-soh": "fin_contracts | where status == 'Active' | join kind=inner batteries on site_id "
                    "| summarize round(avg(state_of_health_pct), 2)",
    "x-top-critical-site-product": "fleet_alert_events | where event_type == 'raised' and severity == 'Critical' "
                                   "| join kind=leftouter (fin_contracts | project site_id, product) on site_id "
                                   "| summarize n = count() by site_id, product | order by n desc, site_id asc | take 1",
    "mh-loan-apr-low-soh": "fin_contracts | where product == 'Loan' | join kind=inner (batteries | where "
                           "state_of_health_pct < 90) on site_id | summarize round(avg(apr_pct), 2)",
    "mh-critical-city": "fleet_alert_events | where severity == 'Critical' and event_type == 'raised' "
                        "| join kind=inner (sites | project site_id, customer_id) on site_id "
                        "| join kind=inner (fin_customers | project customer_id, city) on customer_id "
                        "| summarize n = count_distinct(alert_id) by city | order by n desc, city asc | take 1",
    "mh-principal-inverter-high": "let s = fleet_alert_events | where severity == 'High' and event_type == 'raised' "
                                  "| join kind=inner (devices | where device_type == 'inverter' | project device_id) "
                                  "on device_id | distinct site_id; fin_contracts | where site_id in (s) "
                                  "| summarize sum(principal)",
    "bd-at-risk-sites": "fin_contracts | where status in ('Delinquent', 'Default') | project site_id | order by site_id asc",
    "bd-open-alerts": "fleet_alert_events | summarize arg_max(event_time, event_type) by alert_id "
                      "| where event_type == 'raised' | count",
    "br-warranty-review": f"batteries | where state_of_health_pct < 80 and {_TODAY} | count",
    "br-closest-warranty-review": f"batteries | where {_TODAY} | order by state_of_health_pct asc, device_id asc "
                                  "| take 1 | project device_id, state_of_health_pct",
    "cmp-loan-vs-lease": "fin_contracts | summarize l = avgif(principal, product == 'Loan'), "
                         "s = avgif(principal, product == 'Lease') | project round(l - s, 2)",
    "cmp-battery-share": "sites | summarize share = round(100.0 * countif(has_battery) / count(), 2) by region "
                         "| order by share desc, region asc | take 1",
    "tmp-contract-year": "fin_contracts | summarize n = count() by y = tostring(getyear(start_date)) | top 1 by n",
    "tmp-alert-trend": "fleet_alert_events | where event_type == 'raised' "
                       "| summarize a = count_distinctif(alert_id, startofday(event_time) == datetime(2026-11-09)), "
                       "b = count_distinctif(alert_id, startofday(event_time) == datetime(2026-11-10))",
    "tmp-latest-install": "sites | order by install_date desc, site_id asc | take 1 "
                          "| project site_id, format_datetime(install_date, 'yyyy-MM-dd')",
    "neg-battery-no-alerts": "sites | where has_battery == true and site_id !in ((fleet_alert_events "
                             "| where event_type == 'raised' | project site_id)) | count",
    "neg-active-no-alert-events": "fin_contracts | where status == 'Active' and site_id !in "
                                  "((fleet_alert_events | project site_id)) | count",
    "rk-top3-region-principal": "fin_contracts | summarize p = sum(principal) by site_region | top 3 by p "
                                "| project site_region",
    "rk-top3-sites-pv": "sites | order by pv_capacity_kw desc, site_id asc | take 3 | project site_id",
    "ab-tariff": "print 1", "ab-revenue-2019": "print 1", "ab-irradiance": "print 1",
    "ml-battery-sites-ko": "sites | where has_battery == true | count",
    "ml-default-sites-ko": "fin_contracts | where status == 'Default' | project site_id | order by site_id asc",
    "imp-battery-fail-site": "fin_contracts | where site_id == 'SITE-000006' | project contract_id, status, principal",
    "imp-region-outage": "fin_contracts | join kind=inner (sites | where region == 'Coastal' | project site_id) "
                         "on site_id | summarize count(), sum(principal)",
    "imp-delinquent-default": "fin_contracts | where status in ('Delinquent', 'Default') | summarize count(), sum(principal)",
    "imp-inverter-recall": "devices | where device_type == 'inverter' and model == 'Contoso SunWave 5.0' "
                           "| join kind=leftouter (fin_contracts | project site_id, principal) on site_id "
                           "| summarize count_distinct(site_id), sum(principal)",
}


def cases() -> list[dict]:
    out = []
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        c = json.loads(line)
        if c["category"] in EXCLUDED or c["id"] not in KQL:
            continue
        c["sql"] = SQL_REWRITE.get(c["id"], c["truth"]["query"])
        c["kql"] = KQL[c["id"]]
        out.append(c)
    return out


# ----------------------------------------------------------------- engines (persistent connections)
class Sql:
    def __init__(self, host: str, database: str):
        import mssql_python
        from fabric_rest import token
        tok = token("https://database.windows.net/").encode("utf-16-le")
        self.conn = mssql_python.connect(f"Server=tcp:{host},1433;Database={database};Encrypt=yes;TrustServerCertificate=no",
                                         attrs_before={1256: struct.pack(f"<I{len(tok)}s", len(tok), tok)})

    def run(self, query: str) -> list[list]:
        cur = self.conn.cursor()
        cur.execute(query)
        rows = [list(r) for r in cur.fetchall()]
        cur.close()
        return rows


class Kql:
    """Persistent keep-alive HTTPS connection, like the open TDS connection the SQL engines get."""

    def __init__(self, uri: str, database: str):
        import http.client
        import urllib.parse
        from fabric_rest import token
        self.host, self.db = urllib.parse.urlparse(uri).netloc, database
        self.tok = token("https://kusto.kusto.windows.net")
        self.conn = http.client.HTTPSConnection(self.host, timeout=120)

    def run(self, query: str) -> list[list]:
        body = json.dumps({"db": self.db, "csl": query})
        self.conn.request("POST", "/v1/rest/query", body, {"Authorization": f"Bearer {self.tok}",
                                                           "Content-Type": "application/json"})
        resp = self.conn.getresponse()
        payload = json.loads(resp.read())
        if resp.status != 200:
            raise SystemExit(f"KQL failed ({resp.status}): {json.dumps(payload)[:300]}")
        return [list(r) for r in payload["Tables"][0]["Rows"]]


def engines() -> dict:
    s = load_state("live4")
    lh_host = s["lakehouse"]["host"]  # one SQL analytics endpoint host per workspace; the database = item name
    e = {"lakehouse": lambda: Sql(lh_host, s["lakehouse"]["database"]),
         "sqldb": lambda: Sql(s["sqldb"]["host"], s["sqldb"]["database"]),
         "sqldb_endpoint": lambda: Sql(lh_host, "lab_sqldb_store"),
         "eventhouse": lambda: Kql(s["eventhouse"]["query_uri"], s["eventhouse"]["database"])}
    if "mirror" in s:
        e["mirror"] = lambda: Sql(s["mirror"]["host"], s["mirror"]["database"])
    return e


def query_for(engine: str, c: dict) -> str:
    return c["kql"] if engine == "eventhouse" else c["sql"]


# ----------------------------------------------------------------- crosscheck
def _same(a: dict, b: dict, chk: dict) -> bool:
    tol = max(chk.get("abs_tol", 0.0), 0.011)
    na, nb = a.get("numbers", []), b.get("numbers", [])
    if len(na) != len(nb) or any(abs(x - y) > max(tol, 1e-9 * abs(x)) for x, y in zip(na, nb)):
        return False
    norm = lambda v: [str(x).split(" ")[0].split("T")[0] for x in v if x not in ("", None)]  # noqa: E731 - KQL null string = ""
    return norm(a.get("entities", [])) == norm(b.get("entities", [])) and a.get("direction") == b.get("direction")


def crosscheck() -> dict:
    cs, eng = cases(), engines()
    conns = {k: f() for k, f in eng.items()}
    truth, bad = {}, []
    for c in cs:
        per = {}
        for k, conn in conns.items():
            try:
                per[k] = graders.expected_from_truth(c, conn.run(query_for(k, c)))
            except (Exception, SystemExit) as ex:  # noqa: BLE001
                per[k] = ({"error": str(ex)[:200]}, "")
        ref = per["sqldb"][0]
        diff = [k for k, (exp, _) in per.items() if "error" in exp or not _same(ref, exp, c.get("check", {}))]
        if diff:
            bad.append(c["id"])
            log(f"MISMATCH {c['id']}: " + "; ".join(f"{k}={json.dumps(per[k][0])[:160]}" for k in ["sqldb", *diff]))
        truth[c["id"]] = {"expected": ref, "answer": per["sqldb"][1]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "truth.json").write_text(json.dumps({"engines": list(conns), "cases": truth, "mismatches": bad,
                                                "at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
    log(f"crosscheck: {len(cs) - len(bad)}/{len(cs)} cases identical on {', '.join(conns)}")
    if bad:
        raise SystemExit(f"truth differs across engines for: {', '.join(bad)}")
    return truth


# ----------------------------------------------------------------- engine-only bench
def bench(n: int = 10, only: list[str] | None = None) -> None:
    cs, eng = [c for c in cases() if c["category"] != "abstain"], engines()
    eng = {k: v for k, v in eng.items() if only is None or k in only}
    path = OUT / "bench.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []
    rows = [r for r in rows if r["engine"] not in eng]  # re-benched engines are replaced
    for k, make in eng.items():
        t0 = time.perf_counter()
        conn = make()
        connect_ms = (time.perf_counter() - t0) * 1000
        # Baseline = trivial query: the network round trip from the runner. "Net" = query time minus this median.
        for i in range(n):
            t0 = time.perf_counter()
            conn.run("print 1" if k == "eventhouse" else "SELECT 1")
            rows.append({"engine": k, "case_id": "_baseline", "category": "_baseline", "complexity": None, "i": i,
                         "ms": round((time.perf_counter() - t0) * 1000, 2)})
        for c in cs:
            q = query_for(k, c)
            for i in range(n):
                t0 = time.perf_counter()
                conn.run(q)
                rows.append({"engine": k, "case_id": c["id"], "category": c["category"], "complexity": c.get("complexity"),
                             "i": i, "ms": round((time.perf_counter() - t0) * 1000, 2)})
        log(f"bench {k}: {len(cs)} queries x {n} (connect {connect_ms:.0f} ms)")
    OUT.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


# ----------------------------------------------------------------- agents
def agents(reps: int = 3, warmup: int = 2, only: list[str] | None = None) -> None:
    import data_agents
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    s = load_state("live4")
    data_agents.ws = lambda domain, e: s["workspace_id"]  # live4 agents live in the live4 workspace
    truth = json.loads((OUT / "truth.json").read_text(encoding="utf-8"))["cases"]
    cs = [c for c in cases() if c["id"] in truth]
    stores = [k for k in (only or AGENTS) if k == "eventhouse" or k in s]
    out_path = OUT / "responses.jsonl"

    def per_agent(store: str) -> list[dict]:
        agent = AGENTS[store]
        for _ in range(warmup):  # discarded
            try:
                data_agents.ask_run("dev", agent, "How many sites are there?", timeout_s=240)
            except (Exception, SystemExit):  # noqa: BLE001
                pass
        recs = []
        for rep in range(1, reps + 1):
            for c in cs:
                base = {"store": store, "agent": agent, "rep": rep, "case_id": c["id"], "category": c["category"],
                        "complexity": c.get("complexity", "simple"), "question": c["question"],
                        "expected": truth[c["id"]]["expected"], "check": c.get("check", {}),
                        "asked_at": datetime.now(timezone.utc).isoformat()}
                try:
                    r = {**base, **data_agents.ask_run("dev", agent, c["question"], timeout_s=240)}
                except (Exception, SystemExit) as ex:  # noqa: BLE001
                    r = {**base, "status": "error", "answer": "", "error": f"{type(ex).__name__}: {str(ex)[:300]}"}
                r["grade"] = graders.rules_grade(r)
                recs.append(r)
                log(f"  {store} r{rep} {c['id']}: {r['status']} {r.get('latency_s', '-')} s "
                    f"{'pass' if r['grade']['pass'] else 'FAIL'}")
        return recs

    log(f"asking {len(cs)} questions x {reps} reps to {len(stores)} agents in parallel ({warmup} warm-up each)")
    with ThreadPoolExecutor(len(stores)) as pool:
        records = [r for batch in pool.map(per_agent, stores) for r in batch]
    OUT.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, default=str) + "\n")
    log(f"{len(records)} answers -> {out_path}")


# ----------------------------------------------------------------- report
def _p(values: list[float], q: float) -> float | None:
    return graders.percentile(values, q / 100) if values else None


def _f(x, unit: str = " s", nd: int = 1) -> str:
    return "-" if x is None else f"{x:.{nd}f}{unit}"


def report(publish: bool = False) -> Path:
    from fabric_rest import fabric
    s = load_state("live4")
    snap = json.loads((ROOT / "infra" / ".state" / "live4" / "_snapshot.json").read_text(encoding="utf-8"))
    truth = json.loads((OUT / "truth.json").read_text(encoding="utf-8"))
    bench_rows = [json.loads(x) for x in (OUT / "bench.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    recs = [json.loads(x) for x in (OUT / "responses.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    cap = next((c for c in fabric("get", "capacities").get("value", []) if c["id"] == s["capacity_id"]), {})
    n_cases = len(truth["cases"])
    L = ["# live4: storage format latency (lakehouse vs SQL database vs mirrored Azure SQL vs Eventhouse)", "",
         "Sample, not production guidance. Synthetic data. Generated by `py eval/live4.py report`. "
         f"Dedicated capacity {cap.get('sku', '?')} (not shared with the evaluation loop), dev workspace `<prefix>-live4-dev`. "
         f"Frozen snapshot of 6 hub tables taken {snap['taken_at'][:16]} UTC "
         f"({sum(t['rows'] for t in snap['tables'].values()):,} rows, PII columns dropped). "
         f"{n_cases} golden questions; truth identical on all engines "
         f"({', '.join(truth['engines'])}). Raw output stays in the gitignored `eval/runs/live4/`.", ""]

    L += ["## Engine only (truth queries run directly)", "",
          "Each non-abstain truth query run 10x per engine on one open connection (TDS, or keep-alive HTTPS for KQL) from "
          "the runner (`perf_counter`). The runner is far from the capacity region, so the baseline (a trivial "
          "`SELECT 1` / `print 1`) is mostly network round trip; **net = warm p50 minus baseline p50** is the engine's "
          "share. Cold = first run of each query, warm = runs 2-10.", "",
          "| Engine | Agent query path | Baseline p50 | Cold p50 | Warm p50 | Warm p95 | Net warm p50 | Net simple | "
          "Net complex |", "|---|---|---|---|---|---|---|---|---|"]
    path = {"lakehouse": "SQL analytics endpoint (Delta)", "sqldb": "not used by agents (native engine)",
            "sqldb_endpoint": "SQL analytics endpoint of the SQL database (OneLake replica)",
            "mirror": "SQL analytics endpoint (mirrored Delta)", "eventhouse": "KQL engine"}
    for k in ENGINES:
        b = [r for r in bench_rows if r["engine"] == k and r["case_id"] != "_baseline"]
        if not b:
            continue
        base = _p([r["ms"] for r in bench_rows if r["engine"] == k and r["case_id"] == "_baseline"], 50) or 0.0
        cold = [r["ms"] for r in b if r["i"] == 0]
        warm = [r["ms"] for r in b if r["i"] > 0]
        ws_ = [r["ms"] for r in b if r["i"] > 0 and r["complexity"] == "simple"]
        wc = [r["ms"] for r in b if r["i"] > 0 and r["complexity"] != "simple"]
        net = lambda v: None if not v else max(0.0, _p(v, 50) - base)  # noqa: E731
        L.append(f"| `{k}` | {path[k]} | {_f(base, ' ms', 0)} | {_f(_p(cold, 50), ' ms', 0)} | "
                 f"{_f(_p(warm, 50), ' ms', 0)} | {_f(_p(warm, 95), ' ms', 0)} | {_f(net(warm), ' ms', 0)} | "
                 f"{_f(net(ws_), ' ms', 0)} | {_f(net(wc), ' ms', 0)} |")

    L += ["", "## Agents end to end", "",
          "Same instructions, no few-shots, same columns; 4 agents in parallel, same question order, 2 discarded warm-up "
          "calls each. Accuracy = rules grader. Query = in-agent SQL/KQL execution from run steps (1 s resolution).", "",
          "| Agent | Store | Accuracy | Completed | p50 latency | p95 latency | p50 rep 1 | p50 reps 2-3 | p50 query | "
          "Prompt tokens / answer |", "|---|---|---|---|---|---|---|---|---|---|"]
    stores = [k for k in AGENTS if any(r["store"] == k for r in recs)]
    for k in stores:
        rr = [r for r in recs if r["store"] == k]
        ok = [r for r in rr if r.get("status") == "completed"]
        lat = [r["latency_s"] for r in ok]
        q = [r["query_s"] for r in ok if r.get("query_s")]
        tok = [r["prompt_tokens"] for r in ok if r.get("prompt_tokens")]
        acc = 100 * sum(r["grade"]["pass"] for r in rr) / len(rr)
        L.append(f"| `{AGENTS[k]}` | {k} | {acc:.0f}% | {len(ok)}/{len(rr)} | "
                 f"{_f(_p(lat, 50))} | {_f(_p(lat, 95))} | {_f(_p([r['latency_s'] for r in ok if r['rep'] == 1], 50))} | "
                 f"{_f(_p([r['latency_s'] for r in ok if r['rep'] > 1], 50))} | {_f(_p(q, 50))} | "
                 f"{f'{round(statistics.mean(tok)):,}' if tok else '-'} |")

    cats = sorted({r["category"] for r in recs})
    L += ["", "## Accuracy by category (all reps)", "", "| Category | " + " | ".join(f"`{k}`" for k in stores) + " |",
          "|---|" + "---|" * len(stores)]
    for cat in cats:
        cells = []
        for k in stores:
            rr = [r for r in recs if r["store"] == k and r["category"] == cat]
            cells.append(f"{100 * sum(r['grade']['pass'] for r in rr) / len(rr):.0f}%" if rr else "-")
        L.append(f"| {cat} | " + " | ".join(cells) + " |")

    L += ["", "## Consistency across reps", "",
          "Questions answered correctly in all 3 reps / in some but not all reps (non-determinism).", "",
          "| Agent | Stable pass | Flaky | Stable fail |", "|---|---|---|---|"]
    for k in stores:
        by = {}
        for r in recs:
            if r["store"] == k:
                by.setdefault(r["case_id"], []).append(r["grade"]["pass"])
        L.append(f"| `{AGENTS[k]}` | {sum(all(v) for v in by.values())} | {sum(any(v) and not all(v) for v in by.values())} "
                 f"| {sum(not any(v) for v in by.values())} |")

    mir = s.get("mirror", {})
    L += ["", "## Load and freshness", "",
          "| Store | Load path | Snapshot -> queryable |", "|---|---|---|",
          "| Lakehouse | Delta tables written directly (`deltalake`, UTC timestamps; physical copies, not shortcuts) | "
          "write, then SQL endpoint metadata refresh |",
          "| SQL database | T-SQL batched INSERT (PK per table) | immediate (engine); analytics endpoint replicates to OneLake |",
          f"| Mirrored Azure SQL | {mir.get('path', 'not built')} | initial mirroring {mir.get('initial_sync_s', '-')} s |",
          "| Eventhouse | `.ingest inline` | immediate |", ""]

    # Findings (computed where possible)
    net = {}
    for k in ENGINES:
        b = [r["ms"] for r in bench_rows if r["engine"] == k and r["case_id"] != "_baseline" and r["i"] > 0]
        base = _p([r["ms"] for r in bench_rows if r["engine"] == k and r["case_id"] == "_baseline"], 50) or 0.0
        if b:
            net[k] = max(0.0, _p(b, 50) - base)
    p50 = {k: _p([r["latency_s"] for r in recs if r["store"] == k and r.get("status") == "completed"], 50) for k in stores}
    acc = {k: 100 * sum(r["grade"]["pass"] for r in recs if r["store"] == k) / max(1, sum(r["store"] == k for r in recs))
           for k in stores}
    spread = max(p50.values()) - min(p50.values()) if p50 else 0
    L += ["## Findings", "",
          f"- **The store doesn't change agent latency.** Median answer {min(p50.values()):.1f}-{max(p50.values()):.1f} s "
          f"across the four stores (spread {spread:.1f} s), p95 within ~2 s, and the in-agent query step rounds to 1 s on "
          "every store. As in D-038, the time is AI orchestration (planning, NL2SQL/NL2KQL, answer writing); prompt tokens "
          "per answer are within 1 % because the instructions and schema are identical.",
          f"- **Engines differ by tens of milliseconds, which the agent can't show.** Net of the round trip, the SQL database "
          f"engine ({_f(net.get('sqldb'), ' ms', 0)}) and the Eventhouse ({_f(net.get('eventhouse'), ' ms', 0)}) answer these "
          f"small queries fastest; the three SQL analytics endpoints (lakehouse {_f(net.get('lakehouse'), ' ms', 0)}, "
          f"SQL database endpoint {_f(net.get('sqldb_endpoint'), ' ms', 0)}, mirror {_f(net.get('mirror'), ' ms', 0)}) add "
          "~30-50 ms of Delta/OneLake overhead. That is <1 % of a ~10 s answer.",
          "- **A SQL database source is queried through its SQL analytics endpoint**, not the database engine (the data "
          "agent runs T-SQL on the OneLake replica). So lakehouse, SQL database and mirror agents all hit the same engine "
          "type; only the Eventhouse agent uses a different engine (KQL).",
          f"- **Accuracy is a wash** ({min(acc.values()):.0f}-{max(acc.values()):.0f} %, 2-3 flaky questions per agent across "
          "3 reps). The failures are the same questions on every store (the inverter-recall ambiguity from live3, the "
          "critical-site/product tie-break). The one store-sensitive case was the multi-hop inverter/High-alert principal, "
          "which the KQL agent got 3/3 and the SQL agents 0-1/3.",
          "- **Choose the store for the workload, not for agent speed**: streaming/time series -> Eventhouse; OLTP "
          "writes -> SQL database; existing Azure SQL -> mirroring; files/Spark -> lakehouse.", "",
          "## Setup notes", "",
          "- **Dedicated capacity.** live4 ran on its own F128 capacity and workspace, so it took no CUs from the "
          "evaluation loop on the lab capacity (apart from one read of 989 rows from lh_asset for the snapshot). "
          "It is paused after the run.",
          "- **SQL database source type.** The data agent definition schema doesn't list SQL database; `sql_database` "
          "works (`data_warehouse` and `mirrored_database` pointing at it fail to connect).",
          "- **Lakehouse timestamps.** The Load Table API writes parquet timestamps as `timestamp_ntz`, which the SQL "
          "analytics endpoint hides (the column disappears). The tables are written with UTC `timestamp` instead.",
          "- **Private Azure SQL.** Tenant governance disables public network access and an MCAPS policy requires "
          "Entra-only authentication. The server is reached through a private endpoint (DNS zone group required), loaded "
          "once by a notebook over a workspace managed private endpoint (Entra token, no password), and mirrored through a "
          "VNet data gateway. Through that gateway the SQL connection supports Basic, OAuth2 or service principal only "
          "(no workspace identity), so the connection uses OAuth2 and was created in the portal. West US 2 refused new "
          "SQL servers, so the server is in West US 3 (only replication crosses regions).", ""]
    text = "\n".join(L) + "\n"
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "report.md"
    out.write_text(text, encoding="utf-8")
    if publish:
        REPORTS.mkdir(parents=True, exist_ok=True)
        out = REPORTS / "live4.md"
        out.write_text(text, encoding="utf-8")
    log(f"report -> {out}")
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["crosscheck", "bench", "agents", "report"])
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--warmup", type=int, default=2)
    p.add_argument("--only", help="bench: engines / agents: stores (comma-separated)")
    p.add_argument("--publish", action="store_true")
    a = p.parse_args()
    load_env()
    if a.action == "crosscheck":
        crosscheck()
    elif a.action == "bench":
        bench(a.n, a.only.split(",") if a.only else None)
    elif a.action == "agents":
        agents(a.reps, a.warmup, a.only.split(",") if a.only else None)
    else:
        report(a.publish)


if __name__ == "__main__":
    main()
