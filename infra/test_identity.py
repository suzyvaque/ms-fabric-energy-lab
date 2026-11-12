"""Test identity for access-observability tests (Step 10 A1): a cloud-only Viewer user. Idempotent.

  py infra/test_identity.py create   # user TEST_USER_UPN (default ce-test-analyst@<TENANT_DOMAIN>) -> Viewers group;
                                     # random password written to .env only (never printed)
  py infra/test_identity.py login    # one-time interactive sign-in (device code) into an ISOLATED az profile
  py infra/test_identity.py whoami   # confirm the isolated profile signs in as the test user

Other scripts run as the test user when AZURE_CONFIG_DIR points at the isolated profile, e.g.
  $env:AZURE_CONFIG_DIR = "infra/.state/az-test-user"; py infra/access_matrix.py run --as test-user
The isolated profile keeps your own `az` login untouched. infra/.state is gitignored.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import subprocess

from common import ROOT, env, load_env, log, run, set_env_value

PROFILE = ROOT / "infra" / ".state" / "az-test-user"


def az_json(*args: str):
    return json.loads(run(["az", *args, "-o", "json"]).stdout or "null")


def upn() -> str:
    return env("TEST_USER_UPN", required=False) or f"ce-test-analyst@{env('TENANT_DOMAIN')}"


def create() -> None:
    name = upn()
    found = az_json("ad", "user", "list", "--filter", f"userPrincipalName eq '{name}'", "--query", "[].id")
    if found:
        uid = found[0]
        log(f"test user exists ({name.split('@')[0]})")
    else:
        alphabet = string.ascii_letters + string.digits
        pwd = "".join(secrets.choice(alphabet) for _ in range(20)) + "!7a"
        set_env_value("TEST_USER_PASSWORD", pwd)  # .env only
        uid = az_json("ad", "user", "create", "--display-name", "CE Test Analyst", "--user-principal-name", name,
                      "--password", pwd, "--force-change-password-next-sign-in", "false")["id"]
        log(f"test user created ({name.split('@')[0]}); password stored in .env as TEST_USER_PASSWORD")
    # Fabric needs at least the free license (POWER_BI_STANDARD) or every API call returns UserNotLicensed.
    from fabric_rest import _request
    g = "https://graph.microsoft.com"
    _request("patch", f"{g}/v1.0/users/{uid}", {"usageLocation": env("TEST_USER_USAGE_LOCATION", required=False) or "US"}, g)
    skus = _request("get", f"{g}/v1.0/subscribedSkus?$select=skuPartNumber,skuId", None, g)[2]["value"]
    free = next(s["skuId"] for s in skus if s["skuPartNumber"] == "POWER_BI_STANDARD")
    status, _, body = _request("post", f"{g}/v1.0/users/{uid}/assignLicense", {"addLicenses": [{"skuId": free}], "removeLicenses": []}, g)
    if status not in (200, 201):
        raise SystemExit(f"assignLicense {status}: {str(body)[:300]}")
    log("Fabric (free) license assigned")
    set_env_value("TEST_USER_UPN", name)
    set_env_value("TEST_USER_OBJECT_ID", uid)
    group = az_json("ad", "group", "list", "--display-name", env("ENTRA_GROUP_VIEWERS"), "--query", "[].id")[0]
    if not az_json("ad", "group", "member", "check", "--group", group, "--member-id", uid)["value"]:
        run(["az", "ad", "group", "member", "add", "--group", group, "--member-id", uid])
    log("test user is a member of the Viewers group (Viewer on all workspaces, FinanceViewerNoPII, SQL DENYs)")


def _isolated() -> dict:
    PROFILE.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "AZURE_CONFIG_DIR": str(PROFILE)}


def login() -> None:
    log(f"sign in as {upn().split('@')[0]}@<tenant> (password: TEST_USER_PASSWORD in .env); MFA registration may be asked")
    subprocess.run(["az", "login", "--tenant", env("TENANT_ID"), "--allow-no-subscriptions", "--use-device-code",
                    "-o", "none"], env=_isolated(), shell=os.name == "nt", check=True)


def whoami() -> None:
    res = subprocess.run(["az", "account", "show", "--query", "user.name", "-o", "tsv"], env=_isolated(),
                         capture_output=True, text=True, shell=os.name == "nt")
    ok = res.returncode == 0 and res.stdout.strip().lower() == upn().lower()
    print("isolated profile:", "signed in as the test user" if ok else f"not signed in ({res.stderr.strip()[:120]})")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "login", "whoami"])
    a = p.parse_args()
    load_env()
    {"create": create, "login": login, "whoami": whoami}[a.action]()


if __name__ == "__main__":
    main()
