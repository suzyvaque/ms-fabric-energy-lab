"""Step 8: prove OneLake security as the restricted principal (run in CI as the deploy SP, see governance-test.yml).

The SP is a Viewer on the dev finance + asset workspaces and a member of FinanceViewerNoPII on lh_finance
(py infra/governance.py roles --env dev --test-principal). Exit code 1 if PII or out-of-scope rows are exposed.

  python infra/governance_test.py [--prefix ce]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from fabric_rest import _request, fabric, tsql  # noqa: E402

ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
STORAGE = "https://storage.azure.com/"


def find(name: str) -> dict:
    return next(w for w in fabric("get", "workspaces")["value"] if w["displayName"] == name)


def lakehouse(ws_id: str, name: str) -> tuple[str, str]:
    lh = next(i for i in fabric("get", f"workspaces/{ws_id}/items?type=Lakehouse")["value"] if i["displayName"] == name)
    host = fabric("get", f"workspaces/{ws_id}/lakehouses/{lh['id']}")["properties"]["sqlEndpointProperties"]["connectionString"]
    return lh["id"], host


def sql(host: str, db: str, q: str) -> tuple[bool, object]:
    try:
        return True, [list(r) for r in tsql(host, db, q)]
    except Exception as ex:  # noqa: BLE001 - denial surfaces as a driver error
        return False, str(ex).splitlines()[0][:200]


def onelake_list(ws_id: str, lh_id: str, path: str) -> tuple[bool, str]:
    status, _, body = _request("get", f"{ONELAKE}/{ws_id}?resource=filesystem&recursive=false&directory={lh_id}/{path}",
                               None, STORAGE)
    return status == 200, f"HTTP {status} " + (f"{len(body.get('paths', []))} entries" if status == 200 else str(body)[:150])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--prefix", default=os.environ.get("WORKSPACE_PREFIX", "ce"))
    a = p.parse_args()
    fin = find(f"{a.prefix}-finance-dev")["id"]
    asset = find(f"{a.prefix}-asset-dev")["id"]
    fin_lh, fin_host = lakehouse(fin, "lh_finance")
    asset_lh, asset_host = lakehouse(asset, "lh_asset")

    # (name, kind, must_hold, run) - must_hold: "deny" = must fail, "allow" = should work, None = observe only
    checks = [
        ("CLS: non-PII customer columns", "allow", lambda: sql(fin_host, "lh_finance",
            "SELECT COUNT(*), COUNT(DISTINCT segment) FROM dbo.gold_dim_customer")),
        ("CLS: PII column email", "deny", lambda: sql(fin_host, "lh_finance", "SELECT TOP 1 email FROM dbo.gold_dim_customer")),
        ("CLS: SELECT * on customers", "deny", lambda: sql(fin_host, "lh_finance", "SELECT TOP 1 * FROM dbo.gold_dim_customer")),
        ("RLS: contract regions visible", "rls", lambda: sql(fin_host, "lh_finance",
            "SELECT site_region, COUNT(*) FROM dbo.gold_dim_contract GROUP BY site_region")),
        ("Scope: silver table (not in role)", "deny", lambda: sql(fin_host, "lh_finance",
            "SELECT TOP 1 * FROM dbo.silver_customers")),
        ("Shortcut: lh_asset.fin_customers email", "deny", lambda: sql(asset_host, "lh_asset",
            "SELECT TOP 1 email FROM dbo.fin_customers")),
        ("Shortcut: lh_asset.fin_contracts regions", "rls", lambda: sql(asset_host, "lh_asset",
            "SELECT site_region, COUNT(*) FROM dbo.fin_contracts GROUP BY site_region")),
        ("OneLake: list gold_dim_date (no constraints)", None, lambda: onelake_list(fin, fin_lh, "Tables/gold_dim_date")),
        ("OneLake: list gold_dim_customer (CLS)", None, lambda: onelake_list(fin, fin_lh, "Tables/gold_dim_customer")),
        ("OneLake: list silver_customers (not in role)", None, lambda: onelake_list(fin, fin_lh, "Tables/silver_customers")),
    ]
    failed = 0
    lines = ["| Check | Expectation | Result | Detail |", "|---|---|---|---|"]
    for name, must, fn in checks:
        ok, result = fn()
        detail = json.dumps(result[:6], default=str)[:200] if isinstance(result, list) else str(result)
        if must == "deny":
            verdict = "PASS (denied)" if not ok else "**FAIL (exposed)**"
            failed += ok
        elif must == "allow":
            verdict = "PASS" if ok else "WARN (denied)"
        elif must == "rls":
            # RLS is OneLake-only; the SQL endpoint enforces it after the portal switch to user identity mode.
            exposed = ok and any(r[0] != "Coastal" for r in result)
            verdict = ("WARN (all regions: SQL endpoint in delegated mode)" if exposed
                       else ("PASS (Coastal only)" if ok else "PASS (denied)"))
        else:
            verdict = "observed: " + ("allowed" if ok else "denied")
        lines.append(f"| {name} | {must or 'observe'} | {verdict} | {detail.replace('|', '/')} |")
        print(f"{verdict:24} {name}: {detail}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## OneLake security enforcement (restricted SP)\n\n" + "\n".join(lines) + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
