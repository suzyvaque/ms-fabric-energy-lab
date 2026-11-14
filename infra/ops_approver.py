"""Step 12: an external (B2B guest) approver for operations agent notifications and approvals. Idempotent.

  py infra/ops_approver.py setup    # invite guest -> security group -> free Fabric license -> Contributor on ce-obs-ops
  py infra/ops_approver.py status   # invitation state, group, license, workspace roles (no identifiers printed)
  py infra/ops_approver.py remove   # take the guest out of the group, drop the direct role, delete the guest

Assignments (least privilege for "receive and approve"):
  - Entra: B2B guest OPS_APPROVER_EMAIL (display name OPS_APPROVER_DISPLAY_NAME), member of ENTRA_GROUP_OPS_APPROVERS.
  - License: Fabric (Free) in this tenant, so Fabric APIs/portal don't return UserNotLicensed (same lesson as the test user).
  - Fabric: Contributor on ce-obs-ops through the group (operations agent recipients need *write* on the agent item;
    Contributor is the lowest role with write). A direct assignment is added too, because group membership took hours
    to reach Fabric for the test user (Step 10). No role on the data/consumer/audit workspaces: approved actions run
    with the agent creator's permissions, so the approver needs no data access.
  - Tenant: guest access to Fabric is already on; "Guest users can browse and access Fabric content" is enabled for the
    group only, so the guest sees the workspace list instead of needing direct links.
Identifiers stay in .env (gitignored); nothing here prints the email or object IDs.
"""
from __future__ import annotations

import argparse
import sys
from urllib.parse import quote

from common import env, fab_api, load_env, log, set_env_value
from fabric_rest import _request, fabric

G = "https://graph.microsoft.com"


def graph(method: str, path: str, body: dict | None = None, ok=(200, 201, 204)) -> dict:
    path = quote(path, safe="/?&=$,'@:()")
    status, _, data = _request(method, f"{G}/v1.0/{path}", body, G)
    if status not in ok:
        raise SystemExit(f"Graph {method.upper()} {path.split('?')[0]}: {status} {str(data)[:300]}")
    return data or {}


def find_guest() -> dict | None:
    email = env("OPS_APPROVER_EMAIL").lower()
    sel = "$select=id,displayName,userType,externalUserState,usageLocation"
    hits = graph("get", f"users?$filter=mail eq '{email}'&{sel}").get("value", [])
    return hits[0] if hits else None


def invite() -> dict:
    user = find_guest()
    if user:
        log(f"guest exists (state: {user.get('externalUserState') or 'n/a'})")
    else:
        body = {"invitedUserEmailAddress": env("OPS_APPROVER_EMAIL"),
                "invitedUserDisplayName": env("OPS_APPROVER_DISPLAY_NAME"),
                "inviteRedirectUrl": f"https://app.fabric.microsoft.com/home?ctid={env('TENANT_ID')}",
                "sendInvitationMessage": True,
                "invitedUserMessageInfo": {"customizedMessageBody":
                    "Contoso Energy Fabric lab (sample, synthetic data): you are the approver for operations agent "
                    "notifications. Accept, then open Fabric and Teams in this organization."}}
        uid = graph("post", "invitations", body)["invitedUser"]["id"]
        log("invitation sent to the approver mailbox")
        user = {"id": uid}
    set_env_value("OPS_APPROVER_OBJECT_ID", user["id"])
    return user


def group() -> str:
    name = env("ENTRA_GROUP_OPS_APPROVERS", required=False) or "ce-fabric-ops-approvers"
    hits = graph("get", f"groups?$filter=displayName eq '{name}'&$select=id").get("value", [])
    if hits:
        return hits[0]["id"]
    gid = graph("post", "groups", {"displayName": name, "mailEnabled": False, "securityEnabled": True,
                                   "mailNickname": name.replace("-", ""),
                                   "description": "Contoso Energy lab: approvers for Fabric operations agent actions "
                                                  "(Contributor on the ops workspace only)"})["id"]
    log(f"group {name} created")
    return gid


def add_member(gid: str, uid: str) -> None:
    status, _, data = _request("post", f"{G}/v1.0/groups/{gid}/members/$ref",
                               {"@odata.id": f"{G}/v1.0/directoryObjects/{uid}"}, G)
    if status in (200, 204):
        log("guest added to the group")
    elif "already exist" in str(data):
        log("guest already in the group")
    else:
        raise SystemExit(f"add member: {status} {str(data)[:300]}")


def license_free(uid: str) -> None:
    graph("patch", f"users/{uid}", {"usageLocation": env("TEST_USER_USAGE_LOCATION", required=False) or "US"})
    skus = graph("get", "subscribedSkus?$select=skuPartNumber,skuId")["value"]
    free = next(s["skuId"] for s in skus if s["skuPartNumber"] == "POWER_BI_STANDARD")
    have = graph("get", f"users/{uid}/licenseDetails?$select=skuId").get("value", [])
    if any(l["skuId"] == free for l in have):
        log("Fabric (Free) license already assigned")
        return
    import time
    for attempt in range(8):
        status, _, data = _request("post", f"{G}/v1.0/users/{uid}/assignLicense",
                                   {"addLicenses": [{"skuId": free}], "removeLicenses": []}, G)
        if status in (200, 201):
            break
        if "invalidUsageLocation" not in str(data) or attempt == 7:
            raise SystemExit(f"assignLicense: {status} {str(data)[:200]}")
        time.sleep(15)  # a new guest's usageLocation takes a moment to replicate
    log("Fabric (Free) license assigned")


def ops_ws() -> str:
    from obs_lab import wid
    return wid("ops")


def workspace_roles(gid: str, uid: str) -> None:
    w = ops_ws()
    current = {r["principal"]["id"]: r["role"] for r in fabric("get", f"workspaces/{w}/roleAssignments")["value"]}
    for pid, ptype in ((gid, "Group"), (uid, "User")):
        if current.get(pid) == "Contributor":
            continue
        if pid in current:
            fabric("patch", f"workspaces/{w}/roleAssignments/{pid}", {"role": "Contributor"})
        else:
            fabric("post", f"workspaces/{w}/roleAssignments", {"principal": {"id": pid, "type": ptype}, "role": "Contributor"})
        log(f"Contributor on ce-obs-ops for the {ptype.lower()}")


def guest_browse(gid: str) -> None:
    name = env("ENTRA_GROUP_OPS_APPROVERS", required=False) or "ce-fabric-ops-approvers"
    _, data = fab_api("admin/tenantsettings")
    s = next(x for x in data.get("tenantSettings", data.get("value", [])) if x["settingName"] == "ElevatedGuestsTenant")
    groups = s.get("enabledSecurityGroups") or []
    if s.get("enabled") and (not groups or any(g.get("graphId") == gid for g in groups)):
        log("guest browsing already enabled for the group")
        return
    keep = [{"graphId": g["graphId"], "name": g.get("name", "")} for g in groups] if s.get("enabled") else []
    fab_api("admin/tenantsettings/ElevatedGuestsTenant/update", "post",
            {"enabled": True, "enabledSecurityGroups": keep + [{"graphId": gid, "name": name}]})
    log(f"ElevatedGuestsTenant enabled for {name}")


def setup() -> None:
    user = invite()
    gid = group()
    add_member(gid, user["id"])
    license_free(user["id"])
    workspace_roles(gid, user["id"])
    guest_browse(gid)
    log("next: the approver accepts the invitation (email), opens Fabric once, and installs the "
        "'Fabric Operations Agent' app in Teams while switched to this organization")


def status() -> None:
    user = find_guest()
    if not user:
        print("guest: not invited")
        return
    gid = group()
    members = {m["id"] for m in graph("get", f"groups/{gid}/members?$select=id").get("value", [])}
    lic = graph("get", f"users/{user['id']}/licenseDetails?$select=skuPartNumber").get("value", [])
    roles = {r["principal"]["id"]: r["role"] for r in fabric("get", f"workspaces/{ops_ws()}/roleAssignments")["value"]}
    print(f"guest: userType={user.get('userType')} invitation={user.get('externalUserState')}")
    print(f"group member: {user['id'] in members}; licenses: {[l['skuPartNumber'] for l in lic]}")
    print(f"ce-obs-ops roles: group={roles.get(gid)}, direct={roles.get(user['id'])}")


def remove() -> None:
    user = find_guest()
    if not user:
        print("nothing to remove")
        return
    gid = group()
    w = ops_ws()
    _request("delete", f"https://api.fabric.microsoft.com/v1/workspaces/{w}/roleAssignments/{user['id']}", None,
             "https://api.fabric.microsoft.com")
    _request("delete", f"{G}/v1.0/groups/{gid}/members/{user['id']}/$ref", None, G)
    graph("delete", f"users/{user['id']}")
    log("guest removed (group and its workspace role kept for reuse)")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["setup", "status", "remove"])
    a = p.parse_args()
    load_env()
    {"setup": setup, "status": status, "remove": remove}[a.action]()


if __name__ == "__main__":
    main()
