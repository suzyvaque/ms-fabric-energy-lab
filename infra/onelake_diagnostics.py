"""Step 10: OneLake diagnostics - "who accessed what data, when, and how" at the storage layer. Idempotent. Dev only.

  py infra/onelake_diagnostics.py enable     # cap_lh_onelake_diag (ce-fleet-dev) + diagnostics ON for the 3 dev workspaces
  py infra/onelake_diagnostics.py status     # OneLake settings per workspace + log files received
  py infra/onelake_diagnostics.py generate   # known access: DFS list/read, SQL via cross-workspace shortcut, report-free
  py infra/onelake_diagnostics.py analyze    # JSON logs -> summary + Delta table onelake_access_events (UPNs hashed)

Events land as JSON under Files/DiagnosticLogs/OneLake/Workspaces/<id>/y=/m=/d=/h=/m=00/ (first events within ~1 h).
The destination lakehouse must be on the same capacity; one destination for all dev workspaces (D-027 rationale).
Logs contain user principal names: they stay in OneLake (never in Git) and are hashed before any table is built.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import time
from collections import Counter

from common import load_env, log
from fabric_rest import FABRIC, _request, fabric, find_item, tsql
from finance_lakehouse import lakehouse, refresh_sql_endpoint, ws
from onelake import ONELAKE, STORAGE, read, upload, walk

DEST = "cap_lh_onelake_diag"
SOURCES = ("fleet", "asset", "finance")
ROOT_DIR = "Files/DiagnosticLogs/OneLake/Workspaces"
COLS = ["access_start", "workspace", "item", "item_type", "operation", "category", "originating_app", "principal_type",
        "principal_hash", "status", "via_shortcut", "path_sample"]


def dest() -> tuple[str, dict]:
    w = ws("fleet", "dev")
    return w, lakehouse(w, DEST, "Step 10: OneLake diagnostics events (JSON) for the dev workspaces")


def settings(w: str) -> dict:
    return fabric("get", f"workspaces/{w}/onelake/settings")


def enable() -> None:
    dw, lh = dest()
    for d in SOURCES:
        w = ws(d, "dev")
        cur = settings(w).get("diagnostics", {})
        target = (cur.get("destination") or {}).get("lakehouse", {}).get("itemId")
        if cur.get("status") == "Enabled" and target == lh["id"]:
            log(f"{d}-dev: already enabled")
            continue
        status, headers, body = _request("post", f"{FABRIC}/v1/workspaces/{w}/onelake/settings/modifyDiagnostics", {
            "status": "Enabled", "destination": {"type": "Lakehouse", "lakehouse": {
                "referenceType": "ById", "itemId": lh["id"], "workspaceId": dw}}}, FABRIC)
        if status not in (200, 202):
            raise SystemExit(f"{d}-dev: modifyDiagnostics {status} {json.dumps(body)[:400]}")
        log(f"{d}-dev: diagnostics enabled -> {DEST}")


def status_() -> None:
    dw, lh = dest()
    for d in SOURCES:
        diag = settings(ws(d, "dev")).get("diagnostics", {})
        print(f"{d}-dev: {diag.get('status')} -> "
              f"{'cap_lh_onelake_diag' if (diag.get('destination') or {}).get('lakehouse', {}).get('itemId') == lh['id'] else diag.get('destination')}")
    files = walk(dw, lh["id"], ROOT_DIR)
    print(f"{len(files)} log file(s), {sum(s for _, s in files):,} bytes")
    for p, s in sorted(files)[-5:]:
        print(f"  {p.split('/Workspaces/')[-1][:8]}.../{p.split('/', 6)[-1][-40:]}  {s:,} B")


def generate() -> None:
    """Known access patterns to look for in the logs (each tagged by time)."""
    fw = ws("finance", "dev")
    fin = find_item(fw, "Lakehouse", "lh_finance")
    # 1) direct ADLS/DFS API: list + read a Delta log file (every operation is logged for this path)
    files = walk(fw, fin["id"], "Tables/gold_dim_customer/_delta_log")
    if files:
        read(fw, fin["id"], files[0][0])
    log(f"DFS: listed + read lh_finance/Tables/gold_dim_customer ({len(files)} files)")
    # 2) SQL endpoint through the cross-workspace shortcut lh_asset.fin_customers (event expected in the SOURCE workspace)
    aw = ws("asset", "dev")
    host = lakehouse(aw, "lh_asset", "")["sql_host"]
    tsql(host, "lh_asset", "SELECT COUNT(*) FROM dbo.fin_customers")
    log("SQL: lh_asset.fin_customers (shortcut -> ce-finance-dev lh_finance)")
    # 3) direct read of the asset lakehouse
    a = find_item(aw, "Lakehouse", "lh_asset")
    log(f"DFS: listed lh_asset/Tables/sites ({len(walk(aw, a['id'], 'Tables/sites'))} files)")


def _events(dw: str, lh_id: str) -> list[dict]:
    out = []
    for path, size in walk(dw, lh_id, ROOT_DIR):
        if not size:
            continue
        raw = read(dw, lh_id, path).decode("utf-8", "replace").strip()
        try:  # file is either a JSON array or JSON lines
            recs = json.loads(raw) if raw.startswith("[") else [json.loads(x) for x in raw.splitlines() if x.strip()]
        except json.JSONDecodeError:
            recs = [json.loads(x) for x in raw.splitlines() if x.strip().startswith("{")]
        out += [r for r in recs if isinstance(r, dict)]
    return out


def _get(r: dict, *keys: str) -> str:
    """Field names vary (top level vs properties{}, camelCase vs PascalCase): take the first present."""
    for src in (r, r.get("properties") or {}, r.get("data") or {}):
        for k in keys:
            for kk in (k, k[0].upper() + k[1:]):
                if src.get(kk) not in (None, ""):
                    return str(src[kk])
    return ""


def analyze() -> None:
    dw, lh = dest()
    evs = _events(dw, lh["id"])
    if not evs:
        log("no diagnostic events yet (first events can take up to 1 hour after enabling)")
        return
    names = {}
    for d in SOURCES:
        w = ws(d, "dev")
        names[w] = f"ce-{d}-dev"
        names.update({i["id"]: i["displayName"] for i in fabric("get", f"workspaces/{w}/items")["value"]})
    print("sample event keys:", sorted(set(evs[0]) | set(evs[0].get("properties") or {}))[:40])
    rows = []
    for r in evs:
        upn = _get(r, "executingUPN", "callerUpn", "identity")
        path = _get(r, "resource", "path", "uri", "requestUri")
        rows.append({
            "access_start": _get(r, "accessStartTime", "time", "timestamp")[:19],
            "workspace": names.get(_get(r, "workspaceId"), _get(r, "workspaceId")[:8]),
            "item": names.get(_get(r, "itemId"), _get(r, "itemId")[:8]),
            "item_type": _get(r, "itemType"),
            "operation": _get(r, "operationName"), "category": _get(r, "operationCategory"),
            "originating_app": _get(r, "originatingApp", "userAgent")[:60],
            "principal_type": _get(r, "executingPrincipalType"),
            "principal_hash": hashlib.sha256(upn.lower().encode()).hexdigest()[:12] if upn else "",
            "status": _get(r, "httpStatusCode", "statusCode", "resultType"),
            "via_shortcut": "shortcut" in json.dumps(r).lower(),
            "path_sample": "/".join(path.split("/")[-3:])[:80] if path else ""})
    print(f"{len(rows)} events")
    for title, key in (("by workspace/item", lambda x: (x["workspace"], x["item"])),
                       ("by operation", lambda x: (x["category"], x["operation"])),
                       ("by originating app", lambda x: (x["originating_app"], x["principal_type"]))):
        print(f"-- {title}")
        for k, n in Counter(map(key, rows)).most_common(12):
            print(f"  {n:6}  {' | '.join(str(v) for v in k)}")
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=COLS, lineterminator="\n")
    wr.writeheader()
    wr.writerows(rows)
    upload(dw, lh["id"], "Files/landing/onelake_access_events.csv", buf.getvalue().encode("utf-8"))
    fabric("post", f"workspaces/{dw}/lakehouses/{lh['id']}/tables/onelake_access_events/load", {
        "relativePath": "Files/landing/onelake_access_events.csv", "pathType": "File", "mode": "Overwrite",
        "recursive": False, "formatOptions": {"format": "Csv", "header": True, "delimiter": ","}})
    refresh_sql_endpoint(dw, DEST)
    log(f"{DEST}.onelake_access_events: {len(rows)} rows (principals hashed)")


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["enable", "status", "generate", "analyze"])
    a = p.parse_args()
    load_env()
    {"enable": enable, "status": status_, "generate": generate, "analyze": analyze}[a.action]()


if __name__ == "__main__":
    main()
