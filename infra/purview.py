"""Step 9: Microsoft Purview integration - sensitivity labels, audit, (DLP + Data Map are portal/PowerShell). Idempotent.

  py infra/purview.py labels                # current sensitivity label per finance/asset item (Scanner API)
  py infra/purview.py apply-labels [--env dev|prod]
                                            # label PII/financing items with PURVIEW_LABEL_CONFIDENTIAL (.env, a label GUID)
                                            # via the Fabric admin bulkSetLabels API (needs labels published + tenant setting)
  py infra/purview.py audit [--days 3]      # governance-relevant activity on finance/asset items (users hashed)

DLP for Fabric (lakehouse / semantic model) and Data Map scans are configured in the Purview portal or Security &
Compliance PowerShell: see governance/purview/ (template + checklist) and plan/runbook.md step 9.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import time
from collections import Counter

from common import env, load_env, log
from fabric_rest import FABRIC, _request, fabric, find_item
from finance_lakehouse import ws

PBI = "https://api.powerbi.com/v1.0/myorg"
PBI_RES = "https://analysis.windows.net/powerbi/api"
# Items holding PII or financing data (label: Confidential). Downstream inheritance labels dependants automatically.
SENSITIVE = {
    "finance": [("Lakehouse", "lh_finance"), ("MirroredDatabase", "mir_loan_servicing"), ("SemanticModel", "sm_finance"),
                ("Report", "rpt_finance_kpis"), ("GraphQLApi", "gql_finance"), ("DataAgent", "da_finance"),
                ("Notebook", "nb_finance_medallion")],
    "asset": [("DataAgent", "da_contoso_360"), ("DataAgent", "da_contoso_ontology"), ("Ontology", "ont_contoso")],
}
WATCH = ("share", "export", "permission", "access", "role", "label", "delete", "download", "dataagent", "graphql", "member")


def scan_labels(e: str) -> list[tuple[str, str, str, str]]:
    ids = [ws(d, e) for d in SENSITIVE]
    status, _, r = _request("post", f"{PBI}/admin/workspaces/getInfo?lineage=False&datasourceDetails=False",
                            {"workspaces": ids}, PBI_RES)
    if status != 202:
        raise SystemExit(f"scanner {status}: {r}")
    for _ in range(30):
        time.sleep(5)
        if _request("get", f"{PBI}/admin/workspaces/scanStatus/{r['id']}", None, PBI_RES)[2].get("status") == "Succeeded":
            break
    res = _request("get", f"{PBI}/admin/workspaces/scanResult/{r['id']}", None, PBI_RES)[2]
    out = []
    for w in res.get("workspaces", []):
        for key, items in w.items():
            if isinstance(items, list):
                for i in items:
                    if isinstance(i, dict) and "name" in i:
                        label = (i.get("sensitivityLabel") or {}).get("labelId", "-")
                        out.append((w["name"], key, i["name"], label))
    return out


def labels(e: str) -> None:
    rows = scan_labels(e)
    wanted = {n for v in SENSITIVE.values() for _, n in v}
    for wname, kind, name, label in sorted(rows):
        if name in wanted:
            print(f"{wname:18} {kind:20} {name:24} label={label}")
    unlabeled = [r for r in rows if r[2] in wanted and r[3] == "-"]
    print(f"{len(unlabeled)} sensitive item(s) without a sensitivity label")


def apply_labels(e: str) -> None:
    label = env("PURVIEW_LABEL_CONFIDENTIAL", required=False)
    if not label:
        raise SystemExit("Set PURVIEW_LABEL_CONFIDENTIAL=<label GUID> in .env (Purview portal > Information protection > "
                         "Labels > Confidential > ID), publish the label to the admin, and enable the tenant setting "
                         "'Allow users to apply sensitivity labels for content'.")
    items = []
    for domain, specs in SENSITIVE.items():
        w = ws(domain, e)
        for typ, name in specs:
            it = find_item(w, typ, name)
            if it:
                items.append({"id": it["id"], "type": typ})
    status, _, body = _request("post", f"{FABRIC}/v1/admin/items/bulkSetLabels",
                               {"items": items, "labelId": label, "assignmentMethod": "Standard"}, FABRIC)
    if status not in (200, 202):
        raise SystemExit(f"bulkSetLabels {status}: {str(body)[:500]}")
    log(f"label requested for {len(items)} item(s) ({e}); check with: py infra/purview.py labels")


def audit(days: int) -> None:
    """Admin activity events = the Fabric slice of the unified audit log (Purview Audit holds the same records)."""
    targets = {ws(d, e) for d in SENSITIVE for e in ("dev", "prod")}
    names = {n for v in SENSITIVE.values() for _, n in v}
    acts, watched = Counter(), []
    today = dt.datetime.now(dt.timezone.utc).date()
    for d in range(days):
        day = today - dt.timedelta(days=d)
        url = f"{PBI}/admin/activityevents?startDateTime='{day}T00:00:00.000Z'&endDateTime='{day}T23:59:59.999Z'"
        while url:
            status, _, body = _request("get", url, None, PBI_RES)
            if status != 200:
                break
            for ev in body.get("activityEventEntities", []):
                if (ev.get("WorkspaceId") or "").lower() not in targets:
                    continue
                item = ev.get("ItemName") or ev.get("ArtifactName") or ev.get("DatasetName") or ev.get("ReportName") or ""
                acts[(ev.get("Activity"), item if item in names else "(other)")] += 1
                if any(k in (ev.get("Activity") or "").lower() for k in WATCH):
                    watched.append((ev.get("CreationTime", "")[:19], ev.get("Activity"), item,
                                    hashlib.sha256((ev.get("UserId") or "").lower().encode()).hexdigest()[:10]))
            url = None if body.get("lastResultSet") else body.get("continuationUri")
    print("Activity on sensitive items (last %d days):" % days)
    for (a, item), n in acts.most_common(25):
        if item != "(other)":
            print(f"  {n:5}  {a:40} {item}")
    print(f"Governance-relevant events ({len(watched)}): share/export/permission/role/label/delete/agent/GraphQL")
    for row in sorted(watched)[-15:]:
        print("  " + "  ".join(str(x) for x in row))


def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["labels", "apply-labels", "audit"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    p.add_argument("--days", type=int, default=3)
    a = p.parse_args()
    load_env()
    if a.action == "labels":
        labels(a.env)
    elif a.action == "apply-labels":
        apply_labels(a.env)
    else:
        audit(a.days)


if __name__ == "__main__":
    main()
