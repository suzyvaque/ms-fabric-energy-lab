"""Correlate access probes (infra/.state/access_probes.jsonl) with every log source. Used by access_matrix.py correlate.

Sources: OneLake diagnostics (JSON in the diagnostics lakehouses), workspace monitoring (WarehouseExecutions,
SemanticModelLogs, ItemJobEventLogs), admin activity events (= Purview Audit's Fabric records), SQL audit files.
Identities are classified in memory as admin / test-user / deploy-sp / other; only labels and hashes are written.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import Counter, defaultdict

from common import ROOT, env, log
from fabric_rest import FABRIC, _request, fabric, find_item, kql, tsql
from finance_lakehouse import lakehouse, ws
from onelake import read, walk

PROBES = ROOT / "infra" / ".state" / "access_probes.jsonl"
OUT = ROOT / "plan" / "access-observability-results.md"
PBI = "https://api.powerbi.com/v1.0/myorg"
PBI_RES = "https://analysis.windows.net/powerbi/api"
DIAG_DESTS = [("fleet", "cap_lh_onelake_diag"), ("finance", "cap_lh_onelake_diag_fin")]


def ts(s: str) -> dt.datetime | None:
    if not s:
        return None
    s = s.replace("Z", "").replace("T", " ")[:19]
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def known_identities() -> dict[str, str]:
    """value fragment (lowercase) -> label. Values stay in memory only."""
    ids = {env("ADMIN_UPN").lower(): "admin", env("TEST_USER_UPN", required=False).lower(): "test-user",
           env("TEST_USER_OBJECT_ID", required=False).lower(): "test-user",
           env("SP_CLIENT_ID", required=False).lower(): "deploy-sp", env("SP_OBJECT_ID", required=False).lower(): "deploy-sp"}
    me = _request("get", "https://graph.microsoft.com/v1.0/me?$select=id", None, "https://graph.microsoft.com")[2].get("id", "")
    ids[me.lower()] = "admin"
    return {k: v for k, v in ids.items() if k}


def label(raw: str, ids: dict[str, str]) -> str:
    low = (raw or "").lower()
    if not low:
        return "(none)"
    for frag, lab in ids.items():
        if frag in low:
            return lab
    return "other:" + hashlib.sha256(low.encode()).hexdigest()[:8]


def field(r: dict, *keys: str) -> str:
    for src in (r, r.get("properties") or {}, r.get("data") or {}):
        for k in keys:
            for kk in (k, k[0].upper() + k[1:]):
                v = src.get(kk)
                if v not in (None, ""):
                    return v if isinstance(v, str) else json.dumps(v)
    return ""


# ------------------------------------------------------------------------------------------- sources
def diag_events(ids: dict[str, str]) -> tuple[list[dict], list[str]]:
    rows, keys = [], set()
    names = {}
    for d in ("fleet", "asset", "finance"):
        w = ws(d, "dev")
        names[w] = f"ce-{d}-dev"
        names.update({i["id"]: i["displayName"] for i in fabric("get", f"workspaces/{w}/items")["value"]})
    for domain, lh_name in DIAG_DESTS:
        w = ws(domain, "dev")
        lh = find_item(w, "Lakehouse", lh_name)
        if not lh:
            continue
        for path, size in walk(w, lh["id"], "Files/DiagnosticLogs"):
            if not size:
                continue
            raw = read(w, lh["id"], path).decode("utf-8", "replace").strip()
            recs = json.loads(raw) if raw.startswith("[") else [json.loads(x) for x in raw.splitlines() if x.strip().startswith("{")]
            for r in recs:
                if not isinstance(r, dict):
                    continue
                keys |= set(r) | set(r.get("properties") or {})
                who = " ".join(field(r, k) for k in ("executingUPN", "executingPrincipalId", "callerIpAddress") if k != "callerIpAddress")
                rows.append({"time": ts(field(r, "accessStartTime", "time", "timestamp")), "dest": lh_name,
                             "workspace": names.get(field(r, "workspaceId"), "?"), "item": names.get(field(r, "itemId"), "?"),
                             "operation": field(r, "operationName"), "category": field(r, "operationCategory"),
                             "app": field(r, "originatingApp", "userAgent")[:50], "ptype": field(r, "executingPrincipalType"),
                             "who": label(who, ids), "status": field(r, "httpStatusCode", "statusCode", "resultType", "status"),
                             "resource": "/".join(field(r, "resource", "path", "uri", "objectPath").split("/")[-3:])[:60]})
    return rows, sorted(keys)


def monitoring_rows(ids: dict[str, str], since: str) -> list[dict]:
    from workspace_monitoring import eventhouse
    out = []
    queries = {
        "WarehouseExecutions": "WarehouseExecutions | where Timestamp > datetime({s}) | project t = Timestamp, item = ItemName, "
                               "op = OperationName, status = Status, who = tostring(Identity), cpu = CpuTimeMs",
        "SemanticModelLogs": "SemanticModelLogs | where Timestamp > datetime({s}) and OperationName in ('QueryEnd', 'Error') "
                             "| project t = Timestamp, item = ItemName, op = OperationName, status = tostring(Status), "
                             "who = strcat(tostring(column_ifexists('ExecutingUser', '')), ' ', tostring(column_ifexists('Identity', ''))), cpu = CpuTimeMs",
        "ItemJobEventLogs": "ItemJobEventLogs | where Timestamp > datetime({s}) | project t = Timestamp, item = ItemName, "
                            "op = JobType, status = JobStatus, who = strcat(ExecutingPrincipalType, ' ', ExecutingPrincipalId), cpu = DurationMs",
    }
    for domain in ("finance", "asset", "fleet"):
        uri, db = eventhouse(domain)
        for table, q in queries.items():
            try:
                for r in kql(uri, db, q.format(s=since)):
                    out.append({"source": f"WSM.{table}", "time": ts(r["t"]), "item": r["item"], "op": r["op"],
                                "status": str(r["status"]), "who": label(r["who"], ids), "domain": domain})
            except SystemExit as ex:
                log(f"monitoring {domain}.{table}: {str(ex)[:80]}")
    return out


def activity_rows(ids: dict[str, str], start: dt.datetime, end: dt.datetime) -> list[dict]:
    out = []
    day = start.date()
    while day <= end.date():
        s = max(start, dt.datetime.combine(day, dt.time.min)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        e = min(end, dt.datetime.combine(day, dt.time(23, 59, 59))).strftime("%Y-%m-%dT%H:%M:%S.999Z")
        url = f"{PBI}/admin/activityevents?startDateTime='{s}'&endDateTime='{e}'"
        while url:
            status, _, body = _request("get", url, None, PBI_RES)
            if status != 200:
                log(f"activity events {status}")
                break
            for ev in body.get("activityEventEntities", []):
                out.append({"source": "ActivityEvents", "time": ts(ev.get("CreationTime", "")), "op": ev.get("Activity"),
                            "item": ev.get("ItemName") or ev.get("ArtifactName") or ev.get("DatasetName") or "",
                            "status": "success" if ev.get("IsSuccess", True) else "failed",
                            "who": label(f"{ev.get('UserId', '')} {ev.get('UserKey', '')}", ids)})
            url = None if body.get("lastResultSet") else body.get("continuationUri")
        day += dt.timedelta(days=1)
    return out


def sql_audit_rows(ids: dict[str, str], since: str) -> list[dict]:
    out = []
    for domain, name in (("finance", "lh_finance"), ("asset", "lh_asset")):
        w = ws(domain, "dev")
        lh = lakehouse(w, name, "")
        sep = find_item(w, "SQLEndpoint", name)
        path = f"https://onelake.blob.fabric.microsoft.com/{w}/{sep['id']}/Audit/sqldbauditlogs/"
        try:
            rows = tsql(lh["sql_host"], name, f"SELECT event_time, action_id, succeeded, server_principal_name, "
                                             f"LEFT(statement, 80) FROM sys.fn_get_audit_file_v2('{path}', default, default, "
                                             f"'{since}', default)")
        except Exception as ex:  # noqa: BLE001
            log(f"sql audit {name}: {str(ex).splitlines()[0][:120]}")
            continue
        for t, action, ok, who, stmt in rows:
            out.append({"source": "SQLAudit", "time": ts(str(t)), "item": name, "op": f"{action} {stmt or ''}"[:60],
                        "status": "success" if ok else "failed", "who": label(who or "", ids)})
    return out


# ------------------------------------------------------------------------------------------- correlate
def correlate() -> None:
    probes = [json.loads(x) for x in PROBES.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not probes:
        raise SystemExit("no probes yet (run access_matrix.py run)")
    ids = known_identities()
    start = min(ts(p["start"]) for p in probes) - dt.timedelta(minutes=5)
    end = max(ts(p["end"]) for p in probes) + dt.timedelta(minutes=30)
    since = start.strftime("%Y-%m-%dT%H:%M:%S")
    diag, keys = diag_events(ids)
    sources = {"OneLake diagnostics": [dict(r, source="OneLakeDiag") for r in diag],
               "Workspace monitoring": monitoring_rows(ids, since),
               "Activity events / Purview Audit": activity_rows(ids, start, end),
               "SQL audit": sql_audit_rows(ids, since)}

    def seen(src: list[dict], p: dict) -> str:
        a, b = ts(p["start"]) - dt.timedelta(seconds=90), ts(p["end"]) + dt.timedelta(seconds=180)
        hits = [r for r in src if r.get("time") and a <= r["time"] <= b]
        if not hits:
            return "-"
        whos = Counter(r["who"] for r in hits)
        mine = whos.get(p["identity"], 0)
        failed = sum(1 for r in hits if any(k in str(r.get("status", "")).lower() for k in ("fail", "403", "401", "denied", "false")))
        top = ", ".join(f"{w}x{n}" for w, n in whos.most_common(3))
        return f"{len(hits)} ({'as caller' if mine else 'NOT as caller'}; {top}{'; failed ' + str(failed) if failed else ''})"

    lines = ["# Access observability results (Step 10)", "",
             "Generated by `py infra/access_matrix.py correlate`. Identities are labels (admin / test-user / deploy-sp) or",
             "`other:<hash>`; no account names or IDs. Each cell: events in the probe window (-90 s / +180 s), whether any",
             "carried the **caller's** identity, and the top identities seen.", "",
             f"Window: {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC. Probes: {len(probes)}. "
             + ", ".join(f"{k}: {len(v)} events" for k, v in sources.items()) + ".", ""]
    lines += ["| Identity | Channel | Target | Outcome | " + " | ".join(sources) + " |",
              "|---|---|---|---|" + "---|" * len(sources)]
    for p in probes:
        lines.append(f"| {p['identity']} | {p['channel']} | {p['target']} | {p['outcome']} | "
                     + " | ".join(seen(v, p) for v in sources.values()) + " |")
    lines += ["", "## OneLake diagnostics: event shape", "",
              f"Fields observed: `{', '.join(keys[:40]) or 'none (no events)'}`", ""]
    if diag:
        lines += ["| Identity | Principal type | Originating app | Category / operation | Status | Events |", "|---|---|---|---|---|---|"]
        for (who, pt, app, cat, op, st), n in Counter((r["who"], r["ptype"], r["app"], r["category"], r["operation"], r["status"])
                                                     for r in diag).most_common(25):
            lines.append(f"| {who} | {pt} | {app} | {cat} / {op} | {st} | {n} |")
    lines += ["", "## Per-source identity coverage", "", "| Source | Events | Identities seen |", "|---|---|---|"]
    for k, v in sources.items():
        lines.append(f"| {k} | {len(v)} | {', '.join(f'{w} x{n}' for w, n in Counter(r['who'] for r in v).most_common(6)) or '-'} |")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    log(f"{len(probes)} probes correlated -> {OUT.relative_to(ROOT)}")
