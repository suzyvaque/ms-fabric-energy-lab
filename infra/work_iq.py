"""Step 13: "Work IQ"-style grounding without Microsoft 365 Copilot. Idempotent. Dev only.

SharePoint/Teams hold the unstructured field-operations knowledge; Fabric holds the fleet and finance data. A data agent can't
read SharePoint or Teams, so the knowledge is brought into OneLake and joined on site_id / device_id / alert_code:

  py infra/work_iq.py app        # lab app registration + admin consent (Graph delegated: Sites, Files, Teams channel)
  py infra/work_iq.py site       # M365 group "Contoso Energy Field Ops" -> SharePoint team site + Teams team (Graph)
  py infra/work_iq.py content    # synthetic content: policy + per-alert procedures (Markdown in the team's General channel
                                 # files), a "Field Visits" list and dispatcher posts in the channel, built from lh_asset alerts
  py infra/work_iq.py ingest     # read SharePoint back (SharePoint REST) -> Delta tables fieldops_* in lh_asset
  py infra/work_iq.py shortcut   # OneLake shortcut lh_asset/Files/fieldops_sharepoint -> the channel folder (preview)
  py infra/work_iq.py status

Real Work IQ (Microsoft 365 Copilot grounding on Teams chats, meetings, mail, SharePoint) and Copilot Studio agents that
connect a Fabric data agent need a Microsoft 365 Copilot licence; this tenant has none (D-046). Teams *chat messages* are not
in SharePoint: they are read through Graph (ChannelMessage.Read.All) and land as their own table.
All content is synthetic; technicians are codes (TECH-01..), never people.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import ROOT, env, load_env, log, run, save_state, load_state, set_env_value

G = "https://graph.microsoft.com"
NICK = "ce-fieldops"
TITLE = "Contoso Energy Field Ops"
FOLDER = "General/Field Ops"  # the Teams General channel's files live in Shared Documents/General
LIST = "Field Visits"

# ------------------------------------------------------------------------------------------------ synthetic knowledge
POLICY = [
    ("P1", "Scope", "This policy covers residential solar inverters, batteries, gateways and meters installed by Contoso Energy. "
                    "It applies to every site regardless of financing product (Loan, Lease, PPA)."),
    ("P2", "Response SLA", "Critical alerts: technician on site within 24 hours. High alerts: within 72 hours. Low alerts: at the "
                           "next scheduled visit, at most 30 days. The SLA clock starts when the alert is raised."),
    ("P3", "Battery warranty", "A battery is replaced under warranty when its state of health is below 80 % before its "
                               "warranty_end date, or after a confirmed thermal event (BATT_OVERTEMP with a technician finding of "
                               "cell damage). Replacement parts and labour are covered."),
    ("P4", "Inverter warranty", "Inverters are covered for 10 years from install_date. A repeated INV_FAULT (3 or more raised "
                                "alerts in 30 days) qualifies for replacement instead of repair."),
    ("P5", "Labour", "Labour is covered for the first 2 years after install_date. After that, labour is billed to the customer "
                     "unless the visit replaces a part under warranty."),
    ("P6", "Exclusions", "Damage caused by the customer, unauthorized modifications and weather events beyond the design rating "
                         "are excluded. COMM_LOSS caused by the customer's internet outage is not a warranty event."),
    ("P7", "Financing impact", "Sites under a Lease or PPA are Contoso-owned assets: repairs are never billed to the customer. "
                               "For Loan sites, billable visits are added to the next invoice. Field teams must check the "
                               "contract product before billing."),
]

PROCEDURES = {
    "BATT_OVERTEMP": dict(severity="Critical", sla_hours=24, warranty="Yes, if cell damage is confirmed (policy P3)",
        steps="1. Remote: confirm battery_temp_c above 55 C in telemetry and switch the battery to standby. 2. On site: check "
              "ventilation and ambient temperature. 3. Inspect cells for swelling. 4. If damage is found, replace the battery "
              "module and file a warranty claim. 5. Return to service only below 40 C.",
        parts="Battery module; thermal sensor", escalation="Safety officer if temperature exceeds 70 C or smoke is reported."),
    "INV_FAULT": dict(severity="High", sla_hours=72, warranty="Yes, within 10 years of install (policy P4)",
        steps="1. Remote: read the fault code and restart the inverter once. 2. On site: check DC input and isolator. 3. Replace "
              "the fuse or DC connector if damaged. 4. If the fault repeats 3 times in 30 days, replace the inverter.",
        parts="DC fuse; DC connector; inverter (on repeat)", escalation="Engineering if the replacement inverter faults within 7 days."),
    "COMM_LOSS": dict(severity="High", sla_hours=72, warranty="No, if the customer's internet is down (policy P6)",
        steps="1. Remote: check the gateway's last reading and the customer's broadband status. 2. Ask the customer to power-cycle "
              "the gateway. 3. On site: replace the gateway SIM or the gateway.",
        parts="Gateway; SIM", escalation="Network team if more than 5 sites in one region lose communication within 1 hour."),
    "FAN_WARN": dict(severity="Low", sla_hours=720, warranty="Yes",
        steps="1. Clean the inverter fan and air intake at the next visit. 2. Replace the fan if noise or speed alarms persist.",
        parts="Inverter fan", escalation="None."),
    "GRID_VOLTAGE": dict(severity="Low", sla_hours=720, warranty="No (grid-side issue)",
        steps="1. Compare voltage_v with the utility's limits. 2. If the grid is out of range, notify the utility; no site work.",
        parts="None", escalation="Utility liaison if the condition lasts more than 24 hours."),
    "TEMP_SENSOR": dict(severity="Low", sla_hours=720, warranty="Yes",
        steps="1. Compare the sensor with a reference thermometer. 2. Replace the sensor if it is off by more than 3 C.",
        parts="Temperature sensor", escalation="None."),
}

OUTCOMES = {"BATT_OVERTEMP": ["Battery module replaced", "Ventilation cleared, no damage", "Battery module replaced"],
            "INV_FAULT": ["DC fuse replaced", "Inverter restarted, fault cleared", "Inverter replaced"],
            "COMM_LOSS": ["Customer internet outage", "Gateway SIM replaced", "Gateway replaced"],
            "FAN_WARN": ["Fan cleaned"], "GRID_VOLTAGE": ["Utility notified"], "TEMP_SENSOR": ["Sensor replaced"]}


def procedure_md(code: str, p: dict) -> str:
    return (f"# Procedure: {code}\n\nalert_code: {code}\nseverity: {p['severity']}\nsla_hours: {p['sla_hours']}\n"
            f"warranty: {p['warranty']}\nparts: {p['parts']}\nescalation: {p['escalation']}\n\n## Steps\n\n{p['steps']}\n")


def policy_md() -> str:
    return "# Contoso Energy Warranty and Service Policy\n\nSynthetic sample.\n\n" + "\n".join(
        f"## {sid} {title}\n\n{text}\n" for sid, title, text in POLICY)


# ------------------------------------------------------------------------------------------------ auth
# The Azure CLI's SharePoint token is rejected by SharePoint (401 invalid_request) and its Graph token has no Sites/Files/
# ChannelMessage scopes, so the lab uses its own single-tenant public-client app with admin-consented delegated Graph scopes
# (D-046). Tokens are cached in infra/.state (gitignored); the first call opens a browser sign-in.
APP = "ce-lab-m365"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
SCOPES = ["Sites.ReadWrite.All", "Sites.Manage.All", "Sites.FullControl.All", "Files.ReadWrite.All", "Group.ReadWrite.All",
          "Channel.ReadBasic.All", "ChannelMessage.Send", "ChannelMessage.Read.All", "Team.ReadBasic.All"]
def _local_secret_dir() -> Path:
    """Credential caches never live in the repo folder: it can sit in a synced folder (OneDrive) even though .state is
    gitignored. %LOCALAPPDATA% (or ~/.cache) is machine-local and not synced."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "ce-fabric-lab"
    base.mkdir(parents=True, exist_ok=True)
    return base


CACHE = _local_secret_dir() / "m365_token_cache.bin"


def _cli_graph(method: str, path: str, body=None, ok=(200, 201, 202, 204)):
    """Graph with the Azure CLI token (directory work: app registration, consent, group)."""
    tok = run(["az", "account", "get-access-token", "--resource", G, "--query", "accessToken", "-o", "tsv"]).stdout.strip()
    st, data = _http(method, f"{G}/v1.0/{urllib.parse.quote(path, safe='/?&=$,@:()')}", tok, body)
    if st not in ok:
        raise SystemExit(f"Graph {method.upper()} {path.split('?')[0]}: {st} {data}")
    return data


def app() -> str:
    """Create (or update) the lab's public-client app and grant tenant-wide admin consent for SCOPES. Returns its appId."""
    gsp = _cli_graph("get", f"servicePrincipals?$filter=appId eq '{GRAPH_APP_ID}'&$select=id,oauth2PermissionScopes")["value"][0]
    ids = {s["value"]: s["id"] for s in gsp["oauth2PermissionScopes"]}
    access = [{"resourceAppId": GRAPH_APP_ID, "resourceAccess": [{"id": ids[s], "type": "Scope"} for s in SCOPES]}]
    hit = _cli_graph("get", f"applications?$filter=displayName eq '{APP}'&$select=id,appId")["value"]
    if hit:
        a = hit[0]
        _cli_graph("patch", f"applications/{a['id']}", {"requiredResourceAccess": access})
    else:
        a = _cli_graph("post", "applications", {"displayName": APP, "signInAudience": "AzureADMyOrg",
                                                "isFallbackPublicClient": True,
                                                "publicClient": {"redirectUris": ["http://localhost"]},
                                                "notes": "Contoso Energy lab Step 13: SharePoint/Teams content (synthetic)",
                                                "requiredResourceAccess": access})
        log(f"app registration {APP} created")
    sp_hit = _cli_graph("get", f"servicePrincipals?$filter=appId eq '{a['appId']}'&$select=id")["value"]
    spid = sp_hit[0]["id"] if sp_hit else _cli_graph("post", "servicePrincipals", {"appId": a["appId"]})["id"]
    grants = _cli_graph("get", f"oauth2PermissionGrants?$filter=clientId eq '{spid}' and resourceId eq '{gsp['id']}'")["value"]
    scope = " ".join(SCOPES)
    if grants:
        _cli_graph("patch", f"oauth2PermissionGrants/{grants[0]['id']}", {"scope": scope})
    else:
        _cli_graph("post", "oauth2PermissionGrants", {"clientId": spid, "consentType": "AllPrincipals",
                                                      "resourceId": gsp["id"], "scope": scope})
    log(f"admin consent: {scope}")
    set_env_value("M365_APP_CLIENT_ID", a["appId"])
    return a["appId"]


def _mtoken() -> str:
    import msal
    from msal_extensions import PersistedTokenCache, build_encrypted_persistence
    cache = PersistedTokenCache(build_encrypted_persistence(str(CACHE)))  # DPAPI on Windows  # gitleaks:allow (no secret)
    pca = msal.PublicClientApplication(env("M365_APP_CLIENT_ID"), token_cache=cache,
                                       authority=f"https://login.microsoftonline.com/{env('TENANT_ID')}")
    scopes = [f"{G}/{s}" for s in SCOPES]
    acct = pca.get_accounts()
    res = pca.acquire_token_silent(scopes, account=acct[0]) if acct else None
    if not res:
        log("browser sign-in for the lab app (one time; encrypted cache outside the repo)")
        res = pca.acquire_token_interactive(scopes, login_hint=env("ADMIN_UPN"))
    if "access_token" not in res:
        raise SystemExit(f"token: {res.get('error')}: {res.get('error_description', '')[:300]}")
    return res["access_token"]


def _http(method: str, url: str, tok: str, body=None, headers: dict | None = None):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
    if body is not None and not isinstance(body, bytes):
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw and raw[:1] in b"{[" else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600]


def mg(method: str, path: str, body=None, headers: dict | None = None, ok=(200, 201, 202, 204)):
    """Graph as the signed-in admin through the lab app (Sites/Files/Teams scopes)."""
    url = path if path.startswith("http") else f"{G}/v1.0/{urllib.parse.quote(path, safe='/?&=$,@:()!')}"
    for attempt in range(6):
        st, data = _http(method, url, _mtoken(), body, headers)
        if st in ok:
            return data
        if st in (404, 429, 503) and attempt < 5 and method.lower() == "get":
            time.sleep(10 * (attempt + 1))  # new sites/teams replicate for a while
            continue
        raise SystemExit(f"Graph {method.upper()} {url.split('?')[0][-90:]}: {st} {data}")


def pages(path: str) -> list[dict]:
    out, nxt = [], path
    while nxt:
        d = mg("get", nxt)
        out += d.get("value", [])
        nxt = d.get("@odata.nextLink")
    return out


def sp_root() -> str:
    return f"https://{env('TENANT_DOMAIN').split('.')[0]}.sharepoint.com"


def gid() -> str:
    return load_state("work_iq")["group_id"]


def site_id() -> str:
    return mg("get", f"groups/{gid()}/sites/root?$select=id")["id"]


# ------------------------------------------------------------------------------------------------ site + team
def site() -> None:
    app()
    hits = _cli_graph("get", f"groups?$filter=mailNickname eq '{NICK}'&$select=id")["value"]
    if hits:
        g = hits[0]["id"]
        log("group exists")
    else:
        me = _cli_graph("get", "me?$select=id")["id"]
        g = _cli_graph("post", "groups", {"displayName": TITLE, "mailNickname": NICK, "groupTypes": ["Unified"],
                                          "mailEnabled": True, "securityEnabled": False, "visibility": "Private",
                                          "description": "Contoso Energy lab (synthetic): field operations knowledge (Step 13)",
                                          "owners@odata.bind": [f"{G}/v1.0/users/{me}"],  # gitleaks:allow (OData)
                                          "members@odata.bind": [f"{G}/v1.0/users/{me}"]})["id"]  # gitleaks:allow
        log("M365 group created")
    save_state("work_iq", {"group_id": g, "site": NICK})
    for attempt in range(40):  # reading the group's root site through Graph also triggers provisioning
        st, data = _http("get", f"{G}/v1.0/groups/{g}/sites/root?$select=id,webUrl", _mtoken())
        if st == 200:
            break
        time.sleep(15)
    else:
        raise SystemExit(f"SharePoint site not ready: {st} {data}")
    log("SharePoint team site ready")
    st, _ = _http("get", f"{G}/v1.0/groups/{g}/team?$select=id", _mtoken())
    if st == 200:
        log("Teams team exists")
        return
    for attempt in range(12):  # Teams needs the group to replicate first
        st, data = _http("put", f"{G}/v1.0/groups/{g}/team", _mtoken(), {"memberSettings": {"allowCreateUpdateChannels": True}})
        if st in (200, 201, 202):
            log("Teams team created on the group")
            return
        time.sleep(20)
    raise SystemExit(f"team creation failed: {st} {data}")


# ------------------------------------------------------------------------------------------------ content
def _hub():
    from finance_lakehouse import ws
    from fabric_rest import fabric, find_item
    w = ws("asset", "dev")
    lh = find_item(w, "Lakehouse", "lh_asset")
    host = fabric("get", f"workspaces/{w}/lakehouses/{lh['id']}")["properties"]["sqlEndpointProperties"]["connectionString"]
    return w, lh, host


def _alerts() -> list[tuple]:
    from fabric_rest import tsql
    _, _, host = _hub()
    return tsql(host, "lh_asset", "SELECT alert_id, MIN(event_time), MAX(site_id), MAX(device_id), MAX(alert_code), "
                                  "MAX(severity) FROM dbo.fleet_alert_events WHERE event_type = 'raised' "
                                  "GROUP BY alert_id ORDER BY alert_id")


def visits(rows: list[tuple]) -> list[dict]:
    """One field visit for most raised alerts; some alerts deliberately have no visit or a late one."""
    rng = random.Random(13)
    out = []
    for n, (alert_id, raised, site_id_, device_id, code, sev) in enumerate(rows):
        if rng.random() < 0.15:  # no visit yet
            continue
        sla = PROCEDURES.get(code, {}).get("sla_hours", 72)
        late = rng.random() < 0.2
        delay = timedelta(hours=rng.uniform(sla * 1.1, sla * 2.0) if late else rng.uniform(2, sla * 0.9))
        outcome = rng.choice(OUTCOMES.get(code, ["Inspected"]))
        warranty = code in ("BATT_OVERTEMP", "INV_FAULT", "FAN_WARN", "TEMP_SENSOR") and "replaced" in outcome.lower()
        cost = round(rng.uniform(900, 4200) if "replaced" in outcome.lower() else rng.uniform(120, 380), 2)
        out.append({"visit_id": f"VIS-{n:05d}", "alert_id": alert_id, "site_id": site_id_, "device_id": device_id,
                    "alert_code": code, "severity": sev,
                    "visit_time": (raised + delay).replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "technician": f"TECH-{rng.randint(1, 8):02d}", "outcome": outcome,
                    "warranty_claim": "Yes" if warranty else "No", "cost_usd": cost,
                    "notes": f"{outcome}. Followed procedure {code}." + (" Arrived after SLA (parts delay)." if late else "")})
    return out


def channel_posts(rows: list[tuple]) -> list[str]:
    """Synthetic dispatcher updates in the Teams channel: escalations and parts delays that exist nowhere else."""
    rng = random.Random(42)
    crit = [r for r in rows if r[5] == "Critical"]
    posts = []
    for alert_id, raised, site_id_, device_id, code, _ in rng.sample(crit, min(6, len(crit))):
        kind = rng.choice(["parts", "escalation", "customer"])
        text = {"parts": f"Parts delay: replacement battery module for {device_id} at {site_id_} (alert {alert_id}) "
                         f"is back-ordered, ETA 5 days. Battery kept in standby.",
                "escalation": f"Escalation: {site_id_} battery {device_id} reached 71 C (alert {alert_id}). Safety officer "
                              f"informed per BATT_OVERTEMP procedure.",
                "customer": f"Customer at {site_id_} reports a burning smell near battery {device_id} (alert {alert_id}). "
                            f"Visit moved to first slot tomorrow."}[kind]
        posts.append(f"[FIELD-OPS] {text} #{kind}")
    for alert_id, raised, site_id_, device_id, code, _ in rng.sample([r for r in rows if r[4] == "COMM_LOSS"], 2):
        posts.append(f"[FIELD-OPS] {site_id_} COMM_LOSS (alert {alert_id}): customer confirmed home internet outage; "
                     f"not a warranty event (policy P6). #customer")
    return posts


LIST_COLUMNS = [("site_id", {"text": {}}), ("device_id", {"text": {}}), ("alert_id", {"text": {}}),
                ("alert_code", {"text": {}}), ("severity", {"text": {}}), ("visit_time", {"dateTime": {}}),
                ("technician", {"text": {}}), ("outcome", {"text": {}}), ("warranty_claim", {"text": {}}),
                ("cost_usd", {"number": {}}), ("notes", {"text": {"allowMultipleLines": True}})]


def _list_id(sid: str) -> str | None:
    hit = [l for l in pages(f"sites/{sid}/lists?$select=id,displayName") if l["displayName"] == LIST]
    return hit[0]["id"] if hit else None


def _channel() -> str:
    return next(c["id"] for c in pages(f"teams/{gid()}/channels?$select=id,displayName") if c["displayName"] == "General")


def content() -> None:
    sid = site_id()
    up = lambda path, text: mg("put", f"groups/{gid()}/drive/root:/{path}:/content", text.encode("utf-8"),
                               {"Content-Type": "text/markdown"})
    up(f"{FOLDER}/Warranty and Service Policy.md", policy_md())
    for code, p in PROCEDURES.items():
        up(f"{FOLDER}/Procedures/{code}.md", procedure_md(code, p))
    log(f"uploaded policy + {len(PROCEDURES)} procedures to the General channel files ({FOLDER})")
    lid = _list_id(sid)
    if not lid:
        lid = mg("post", f"sites/{sid}/lists", {"displayName": LIST, "description": "Synthetic field visits (Step 13)",
                                                "list": {"template": "genericList"},
                                                "columns": [{"name": n, **t} for n, t in LIST_COLUMNS]})["id"]
        log(f"list '{LIST}' created")
    have = {i["fields"]["Title"] for i in pages(f"sites/{sid}/lists/{lid}/items?$expand=fields($select=Title)&$top=999")}
    rows = _alerts()
    new = [v for v in visits(rows) if v["visit_id"] not in have]
    for v in new:
        mg("post", f"sites/{sid}/lists/{lid}/items", {"fields": {"Title": v["visit_id"], **{k: v[k] for k, _ in LIST_COLUMNS}}})
    log(f"'{LIST}': {len(new)} item(s) added, {len(have)} already present")
    cid = _channel()
    posted = {m.get("body", {}).get("content", "") for m in pages(f"teams/{gid()}/channels/{cid}/messages?$top=50")}
    sent = 0
    for text in channel_posts(rows):
        if not any(text[:60] in p for p in posted):
            mg("post", f"teams/{gid()}/channels/{cid}/messages", {"body": {"contentType": "text", "content": text}})
            sent += 1
    log(f"Teams General channel: {sent} dispatcher update(s) posted")


# ------------------------------------------------------------------------------------------------ OneLake
def _children(path: str) -> list[dict]:
    return pages(f"groups/{gid()}/drive/root:/{path}:/children?$select=name,file,webUrl")


def _read(path: str) -> str:
    return mg("get", f"groups/{gid()}/drive/root:/{path}:/content").decode("utf-8")


def ingest() -> None:
    import re
    import pyarrow as pa
    from deltalake import write_deltalake
    from fabric_rest import token
    from finance_lakehouse import refresh_sql_endpoint
    w, lh, _ = _hub()
    now = datetime.now(timezone.utc)
    procs = []
    for f in _children(f"{FOLDER}/Procedures"):
        head, _, steps = _read(f"{FOLDER}/Procedures/{f['name']}").partition("## Steps")
        kv = dict(line.split(": ", 1) for line in head.splitlines() if ": " in line)
        procs.append({"alert_code": kv["alert_code"], "severity": kv["severity"], "sla_hours": int(kv["sla_hours"]),
                      "warranty": kv["warranty"], "parts": kv["parts"], "escalation": kv["escalation"],
                      "steps": steps.strip(), "source": f"SharePoint: {FOLDER}/Procedures/{f['name']}"})
    policy, cur = [], None
    for line in _read(f"{FOLDER}/Warranty and Service Policy.md").splitlines():
        if line.startswith("## "):
            sid_, _, title = line[3:].partition(" ")
            cur = {"section_id": sid_, "title": title, "text": ""}
            policy.append(cur)
        elif cur and line.strip():
            cur["text"] = (cur["text"] + " " + line.strip()).strip()
    sid = site_id()
    vis = []
    for i in pages(f"sites/{sid}/lists/{_list_id(sid)}/items?$expand=fields&$top=999"):
        f = i["fields"]
        vis.append({"visit_id": f["Title"], **{k: f.get(k) for k, _ in LIST_COLUMNS}})
    for v in vis:
        v["visit_time"] = datetime.fromisoformat(v["visit_time"].replace("Z", "+00:00")) if v["visit_time"] else None
        v["cost_usd"] = float(v["cost_usd"] or 0)
    msgs = []
    for m in pages(f"teams/{gid()}/channels/{_channel()}/messages?$top=50"):
        text = re.sub(r"<[^>]+>", "", (m.get("body") or {}).get("content") or "").strip()
        if not text.startswith("[FIELD-OPS]"):
            continue
        site_m, alert_m, tag = re.search(r"SITE-\d+", text), re.search(r"alert ([\w-]+)", text), re.search(r"#(\w+)", text)
        msgs.append({"message_id": m["id"], "posted_at": datetime.fromisoformat(m["createdDateTime"].replace("Z", "+00:00")),
                     "channel": "Field Ops / General", "author_role": "dispatcher",  # never the person's name
                     "site_id": site_m.group(0) if site_m else None, "alert_id": alert_m.group(1) if alert_m else None,
                     "topic": tag.group(1) if tag else None, "text": text})
    tables = {"fieldops_procedures": procs, "fieldops_policy": policy, "fieldops_visits": vis, "fieldops_channel_messages": msgs}
    storage = {"bearer_token": token("https://storage.azure.com/"), "use_fabric_endpoint": "true"}
    for name, rows in tables.items():
        t = pa.Table.from_pylist([{**r, "_ingested_at": now} for r in rows])
        write_deltalake(f"abfss://{w}@onelake.dfs.fabric.microsoft.com/{lh['id']}/Tables/{name}", t,
                        mode="overwrite", schema_mode="overwrite", storage_options=storage)
        log(f"lh_asset.{name}: {t.num_rows} rows")
    refresh_sql_endpoint(w, "lh_asset")


def grant_site(wi: dict) -> None:
    """Least privilege for the workspace identity: Graph app role Sites.Selected (no tenant-wide access) plus 'read' on this
    one site only. Assigning the app role needs the CLI token (AppRoleAssignment.ReadWrite.All); the site grant needs
    Sites.FullControl.All (lab app, delegated admin)."""
    gsp = _cli_graph("get", f"servicePrincipals?$filter=appId eq '{GRAPH_APP_ID}'&$select=id,appRoles")["value"][0]
    spo = _cli_graph("get", "servicePrincipals?$filter=appId eq '00000003-0000-0ff1-ce00-000000000000'&$select=id,appRoles")["value"][0]
    have = _cli_graph("get", f"servicePrincipals/{wi['servicePrincipalId']}/appRoleAssignments")["value"]
    for res in (gsp, spo):  # Graph (drives) and SharePoint Online (files)
        role = next(r["id"] for r in res["appRoles"] if r["value"] == "Sites.Selected")
        if not any(a["appRoleId"] == role and a["resourceId"] == res["id"] for a in have):
            _cli_graph("post", f"servicePrincipals/{wi['servicePrincipalId']}/appRoleAssignments",
                       {"principalId": wi["servicePrincipalId"], "resourceId": res["id"], "appRoleId": role})
            log("workspace identity: Sites.Selected assigned")
    sid = site_id()
    perms = pages(f"sites/{sid}/permissions")
    if not any(any(i.get("application", {}).get("id") == wi["applicationId"] for i in p.get("grantedToIdentitiesV2") or [])
               for p in perms):
        mg("post", f"sites/{sid}/permissions", {"roles": ["read"], "grantedToIdentities": [
            {"application": {"id": wi["applicationId"], "displayName": "ce-asset-dev workspace identity"}}]})
        log("workspace identity: read on the Field Ops site only")


def shortcut() -> None:
    """SharePoint shortcut (preview) under lh_asset/Files. Needs a Fabric connection to SharePoint: workspace identity is tried
    by API; an organizational-account (OAuth) connection can only be created in the portal."""
    from fabric_rest import _request, fabric, workspace_identity
    F = "https://api.fabric.microsoft.com"
    w, lh, _ = _hub()
    name = "fieldops_sharepoint"
    if any(s["name"] == name for s in fabric("get", f"workspaces/{w}/items/{lh['id']}/shortcuts").get("value", [])):
        log("shortcut exists")
        return
    grant_site(workspace_identity(w))
    conn = next((c for c in fabric("get", "connections").get("value", []) if c.get("displayName") == "ce-fieldops-sharepoint"), None)
    if not conn:
        workspace_identity(w)
        st, _, conn = _request("post", f"{F}/v1/connections", {
            "connectivityType": "ShareableCloud", "displayName": "ce-fieldops-sharepoint", "privacyLevel": "Organizational",
            "connectionDetails": {"type": "SharePoint", "creationMethod": "SharePointList",
                                  "parameters": [{"dataType": "Text", "name": "sharePointSiteUrl",
                                                  "value": f"{sp_root()}/sites/{NICK}"}]},  # the site, not the root:
                                                  # the workspace identity can only read this one site (Sites.Selected)
            "credentialDetails": {"credentials": {"credentialType": "WorkspaceIdentity"}, "singleSignOnType": "None",
                                  "connectionEncryption": "NotEncrypted", "skipTestConnection": False}}, F)
        if st not in (200, 201):
            raise SystemExit(f"connection: {st} {json.dumps(conn)[:400]}\n-> portal: create connection "
                             "'ce-fieldops-sharepoint' (SharePoint, organizational account), then rerun")
        log("connection ce-fieldops-sharepoint created (workspace identity)")
    body = {"path": "Files", "name": name, "target": {"oneDriveSharePoint": {
        "location": f"{sp_root()}/sites/{NICK}", "subpath": f"/Shared Documents/{FOLDER}", "connectionId": conn["id"]}}}
    st, _, res = _request("post", f"{F}/v1/workspaces/{w}/items/{lh['id']}/shortcuts", body, F)
    if st not in (200, 201):
        raise SystemExit(f"shortcut: {st} {json.dumps(res)[:400]}")
    log("shortcut lh_asset/Files/fieldops_sharepoint created")


def status() -> None:
    sid = site_id()
    lid = _list_id(sid)
    print("site: ready; list items:", len(pages(f"sites/{sid}/lists/{lid}/items?$select=id&$top=999")) if lid else 0)
    print("channel files:", [f["name"] for f in _children(FOLDER)])
    print("channel messages:", len(pages(f"teams/{gid()}/channels/{_channel()}/messages?$top=50")))


DIGEST_Q = ("Daily field-ops digest: list Critical alerts raised in the data that have no field visit yet (alert_id, site_id, "
            "alert_code, hours since raised vs SLA, contract product), then any Teams escalations. Max 8 lines.")


def digest() -> None:
    """Licence-free 'agent in Teams': ask the Fabric data agent, post its answer to the Field Ops channel (as the admin).
    The interactive agent in Teams / Microsoft 365 Copilot needs a Microsoft 365 Copilot licence (D-046)."""
    import html
    from data_agents import ask_run
    r = ask_run("dev", "da_contoso_fieldops", DIGEST_Q, 300)
    body = (f"<b>[FIELD-OPS DIGEST]</b> from Fabric data agent <i>da_contoso_fieldops</i> "
            f"({r['latency_s']} s, synthetic data)<br/>" + html.escape(r.get("answer") or r["status"]).replace("\n", "<br/>"))
    mg("post", f"teams/{gid()}/channels/{_channel()}/messages", {"body": {"contentType": "html", "content": body}})
    log(f"digest posted to the Teams channel ({len(r.get('answer') or '')} chars)")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["app", "site", "content", "ingest", "shortcut", "digest", "status"])
    a = p.parse_args()
    load_env()
    {"app": app, "site": site, "content": content, "ingest": ingest, "shortcut": shortcut, "digest": digest,
     "status": status}[a.action]()


if __name__ == "__main__":
    main()
