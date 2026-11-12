"""live4 mirrored Azure SQL arm. Private only: tenant governance forces public network access Disabled (D-014).

  Azure:  VNet vnet-ce-live4 (snet-pe: private endpoint, snet-vnetgw: delegated to Microsoft.PowerPlatform/vnetaccesslinks)
          Azure SQL server (Entra-only auth: MCAPS policy denies SQL logins; SAMI) + serverless GP database live4
          private endpoint + privatelink.database.windows.net zone linked to the VNet
  Fabric: VNet data gateway lab-vnetgw-live4 on the live4 capacity -> connection lab-live4-azuresql (workspace identity)
          managed private endpoint (workspace -> SQL server) used once by lab_nb_live4_load to create the workspace
          identity's database user and load the tables
          lab_mir_store (MirroredDatabase, source AzureSqlDatabase) with the server's SAMI as workspace Contributor
Called by: py infra/live4_stores.py azuresql
"""
from __future__ import annotations

import hashlib
import json
import time

from common import env, log, run
from fabric_rest import FABRIC, _request, b64, fabric, upsert_item
from live4_stores import STORES, capacity_id, snap, sql_ddl, sql_inserts, update_state, ws
from rti_lab import platform

DB = "live4"
VNET, PE_SUBNET, GW_SUBNET = "vnet-ce-live4", "snet-pe", "snet-vnetgw"
GATEWAY, CONNECTION, MPE, NOTEBOOK = "lab-vnetgw-live4", "lab-live4-azuresql", "mpe-live4-sql", "lab_nb_live4_load"


def sub() -> str:
    return env("AZURE_SUBSCRIPTION_ID")


def rg() -> str:
    return env("AZURE_RESOURCE_GROUP")


def server() -> str:
    return f"sql-ce-live4-{hashlib.sha1(sub().encode()).hexdigest()[:6]}-{sql_location()}"


def sql_location() -> str:
    """West US 2 currently refuses new SQL servers (RegionDoesNotAllowProvisioning). The private endpoint, VNet and
    gateway stay in AZURE_LOCATION; agents query the mirror's OneLake copy there, so only replication crosses regions."""
    return env("SQL_LOCATION", required=False) or "westus3"


def az(*args: str, check: bool = True):
    res = run(["az", *args, "--subscription", sub(), "-o", "json"], check=check)
    if not check and (res.returncode != 0 or res.stdout.strip() in ("", "{}")):  # some `show` commands print {} when missing
        return None
    return json.loads(res.stdout) if res.stdout.strip() else {}


# ----------------------------------------------------------------- Azure
def network() -> None:
    state = az("provider", "show", "-n", "Microsoft.PowerPlatform", "--query", "registrationState")
    if state != "Registered":
        az("provider", "register", "-n", "Microsoft.PowerPlatform", "--wait")
        log("Microsoft.PowerPlatform registered")
    if az("network", "vnet", "show", "-g", rg(), "-n", VNET, check=False) is None:
        az("network", "vnet", "create", "-g", rg(), "-n", VNET, "-l", env("AZURE_LOCATION"),
           "--address-prefixes", "10.44.0.0/16", "--subnet-name", PE_SUBNET, "--subnet-prefixes", "10.44.1.0/24")
        log(f"vnet {VNET} created")
    if az("network", "vnet", "subnet", "show", "-g", rg(), "--vnet-name", VNET, "-n", GW_SUBNET, check=False) is None:
        az("network", "vnet", "subnet", "create", "-g", rg(), "--vnet-name", VNET, "-n", GW_SUBNET,
           "--address-prefixes", "10.44.2.0/24", "--delegations", "Microsoft.PowerPlatform/vnetaccesslinks")
        log(f"subnet {GW_SUBNET} (delegated to Microsoft.PowerPlatform/vnetaccesslinks) created")


def sql_server() -> dict:
    """Entra-only authentication: MCAPS policy denies SQL servers without it (no SQL logins, no passwords)."""
    srv = az("sql", "server", "show", "-g", rg(), "-n", server(), check=False)
    if srv is None:
        me = json.loads(run(["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "json"]).stdout)
        srv = az("sql", "server", "create", "-g", rg(), "-n", server(), "-l", sql_location(),
                 "--enable-ad-only-auth", "--enable-public-network", "false",
                 "--assign-identity", "--identity-type", "SystemAssigned",
                 "--external-admin-principal-type", "User", "--external-admin-name", env("ADMIN_UPN"),
                 "--external-admin-sid", me)
        log(f"sql server {server()} created (Entra-only, public network access: {srv.get('publicNetworkAccess')})")
    if az("sql", "db", "show", "-g", rg(), "-s", server(), "-n", DB, check=False) is None:
        az("sql", "db", "create", "-g", rg(), "-s", server(), "-n", DB, "-e", "GeneralPurpose", "-f", "Gen5",
           "-c", "2", "--compute-model", "Serverless", "--min-capacity", "0.5", "--auto-pause-delay", "60",
           "--backup-storage-redundancy", "Local")
        log(f"database {DB} created (serverless GP, auto-pause 60 min)")
    return srv


def private_endpoint(server_id: str) -> None:
    pe = f"pe-{server()}"
    if az("network", "private-endpoint", "show", "-g", rg(), "-n", pe, check=False) is None:
        az("network", "private-endpoint", "create", "-g", rg(), "-n", pe, "-l", env("AZURE_LOCATION"),
           "--vnet-name", VNET, "--subnet", PE_SUBNET, "--private-connection-resource-id", server_id,
           "--group-id", "sqlServer", "--connection-name", f"{pe}-conn")
        log(f"private endpoint {pe} created")
    zone = "privatelink.database.windows.net"
    if az("network", "private-dns", "zone", "show", "-g", rg(), "-n", zone, check=False) is None:
        az("network", "private-dns", "zone", "create", "-g", rg(), "-n", zone)
    if az("network", "private-dns", "link", "vnet", "show", "-g", rg(), "-z", zone, "-n", f"{VNET}-link", check=False) is None:
        az("network", "private-dns", "link", "vnet", "create", "-g", rg(), "-z", zone, "-n", f"{VNET}-link",
           "-v", VNET, "-e", "false")
    if az("network", "private-endpoint", "dns-zone-group", "show", "-g", rg(), "--endpoint-name", pe, "-n", "default",
          check=False) is None:
        az("network", "private-endpoint", "dns-zone-group", "create", "-g", rg(), "--endpoint-name", pe, "-n", "default",
           "--private-dns-zone", zone, "--zone-name", "sql")
    log(f"private DNS {zone} linked to {VNET}")


# ----------------------------------------------------------------- Fabric: gateway + connection
def gateway() -> str:
    for g in fabric("get", "gateways").get("value", []):
        if g.get("displayName") == GATEWAY:
            return g["id"]
    body = {"type": "VirtualNetwork", "displayName": GATEWAY, "capacityId": capacity_id(),
            "virtualNetworkAzureResource": {"subscriptionId": sub(), "resourceGroupName": rg(),
                                            "virtualNetworkName": VNET, "subnetName": GW_SUBNET},
            "inactivityMinutesBeforeSleep": 30, "numberOfMemberGateways": 1}
    for attempt in range(4):  # seen: a transient 500 InternalServerError on the first create
        status, _, g = _request("post", f"{FABRIC}/v1/gateways", body, FABRIC)
        if status in (200, 201):
            log(f"VNet data gateway {GATEWAY} created on the live4 capacity")
            return g["id"]
        if status < 500 or attempt == 3:
            raise SystemExit(f"VNet gateway create -> {status}: {json.dumps(g)[:400]}")
        time.sleep(30)
    raise SystemExit("unreachable")


def supported_credentials(gateway_id: str) -> list[str]:
    for t in fabric("get", f"connections/supportedConnectionTypes?gatewayId={gateway_id}&showAllCreationMethods=true").get("value", []):
        if t.get("type") == "SQL":
            return sorted(set(t.get("supportedCredentialTypes", [])))
    return []


def connection(gateway_id: str) -> str:
    """Workspace identity (no secret); it is added as a database user by the load notebook."""
    for c in fabric("get", "connections").get("value", []):
        if c.get("displayName") == CONNECTION:
            return c["id"]
    supported = supported_credentials(gateway_id)
    log(f"SQL credential types via {GATEWAY}: {', '.join(supported) or 'unknown'}")
    if supported and "WorkspaceIdentity" not in supported:
        raise SystemExit("WorkspaceIdentity is not supported for SQL through the VNet gateway; "
                         f"supported: {supported} (a service principal would be the next option)")
    body = {"connectivityType": "VirtualNetworkGateway", "gatewayId": gateway_id, "displayName": CONNECTION,
            "privacyLevel": "Organizational",
            "connectionDetails": {"type": "SQL", "creationMethod": "SQL", "parameters": [
                {"dataType": "Text", "name": "server", "value": f"{server()}.database.windows.net"},
                {"dataType": "Text", "name": "database", "value": DB}]},
            "credentialDetails": {"credentials": {"credentialType": "WorkspaceIdentity"},
                                  "singleSignOnType": "None", "connectionEncryption": "Encrypted",
                                  "skipTestConnection": False}}
    for attempt in range(10):  # a new VNet gateway takes a few minutes before it can test connections
        try:
            cid = fabric("post", "connections", body)["id"]
            log(f"connection {CONNECTION} created (via {GATEWAY})")
            return cid
        except SystemExit as e:
            if attempt == 9:
                raise
            log(f"connection test not ready yet ({str(e)[:160]}); retrying")
            time.sleep(60)
    raise SystemExit("unreachable")


# ----------------------------------------------------------------- load (managed private endpoint + notebook)
def managed_private_endpoint(w: str, server_id: str) -> None:
    existing = {m["name"]: m for m in fabric("get", f"workspaces/{w}/managedPrivateEndpoints").get("value", [])}
    if MPE not in existing:
        fabric("post", f"workspaces/{w}/managedPrivateEndpoints", {
            "name": MPE, "targetPrivateLinkResourceId": server_id, "targetSubresourceType": "sqlServer",
            "requestMessage": "live4: one-off load of the frozen snapshot"})
        log(f"managed private endpoint {MPE} requested")
    for _ in range(60):
        conns = az("network", "private-endpoint-connection", "list", "--id", server_id) or []
        pending = [c for c in conns if c["properties"]["privateLinkServiceConnectionState"]["status"] == "Pending"]
        for c in pending:
            az("network", "private-endpoint-connection", "approve", "--id", c["id"], "--description", "live4 load")
            log("managed private endpoint approved on the SQL server")
        m = {x["name"]: x for x in fabric("get", f"workspaces/{w}/managedPrivateEndpoints").get("value", [])}.get(MPE, {})
        if m.get("provisioningState") == "Succeeded" and \
                (m.get("connectionState") or {}).get("status") == "Approved":
            return
        time.sleep(20)
    raise SystemExit(f"{MPE} not approved/provisioned in time")


def _sid(app_id: str) -> str:
    import uuid
    return "0x" + uuid.UUID(app_id).bytes_le.hex().upper()


NB_CODE = '''# lab_nb_live4_load: one-off load of the live4 snapshot into the private Azure SQL database (via the workspace's
# managed private endpoint). Generated by infra/live4_azuresql.py; statements are DDL + INSERTs of the frozen snapshot.
import json
SERVER, DB = {server!r}, {db!r}
STATEMENTS = json.loads({stmts!r})
try:
    tok = notebookutils.credentials.getToken("https://database.windows.net/")
except Exception as ex:  # audience not available in this runtime: fall back to the caller's token parameter
    print("getToken(database) unavailable:", type(ex).__name__)
    tok = sql_token
jvm = spark._sc._gateway.jvm
props = jvm.java.util.Properties()
props.setProperty("accessToken", tok)
url = (f"jdbc:sqlserver://{{SERVER}}.database.windows.net:1433;database={{DB}};encrypt=true;"
       "trustServerCertificate=false;hostNameInCertificate=*.database.windows.net;loginTimeout=60")
conn = jvm.java.sql.DriverManager.getConnection(url, props)
st = conn.createStatement()
for s in STATEMENTS:
    st.execute(s)
counts = {{}}
for t in {tables!r}:
    rs = st.executeQuery(f"SELECT COUNT(*) FROM dbo.[{{t}}]")
    rs.next()
    counts[t] = rs.getLong(1)
conn.close()
print(json.dumps(counts))
notebookutils.notebook.exit(json.dumps(counts))
'''


def notebook(w: str, identity_name: str, identity_app_id: str) -> dict:
    # The workspace identity is the mirroring connection's credential: a contained Entra user created by SID
    # (no Graph lookup needed), db_owner covers the mirroring permissions.
    statements = [f"IF USER_ID('{identity_name}') IS NULL CREATE USER [{identity_name}] WITH "
                  f"SID = {_sid(identity_app_id)}, TYPE = E",
                  f"ALTER ROLE db_owner ADD MEMBER [{identity_name}]"]
    for name, table in snap().items():
        statements += [f"DROP TABLE IF EXISTS dbo.[{name}]", sql_ddl(name, table), *sql_inserts(name, table)]
    code = NB_CODE.format(server=server(), db=DB, stmts=json.dumps(statements), tables=list(snap()))
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {
        "kernel_info": {"name": "synapse_pyspark"},
        "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"}, "language_info": {"name": "python"}},
        "cells": [{"cell_type": "code", "metadata": {"tags": ["parameters"]}, "source": ['sql_token = ""\n'],
                   "outputs": [], "execution_count": None},
                  {"cell_type": "code", "metadata": {}, "source": code.splitlines(keepends=True),
                   "outputs": [], "execution_count": None}]}
    return upsert_item(w, "Notebook", NOTEBOOK, [
        {"path": "notebook-content.ipynb", "payload": b64(nb), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("Notebook", NOTEBOOK)), "payloadType": "InlineBase64"}],
        "live4: one-off load of the frozen snapshot into the private Azure SQL database", definition_format="ipynb")


def run_notebook(w: str, nb_id: str, token_param: str | None = None, timeout_min: int = 30) -> dict:
    body = {"executionData": {"parameters": {"sql_token": {"value": token_param, "type": "string"}}}} if token_param else None
    status, headers, payload = _request("post", f"{FABRIC}/v1/workspaces/{w}/items/{nb_id}/jobs/instances?jobType=RunNotebook",
                                        body, FABRIC)
    if status != 202:
        raise SystemExit(f"notebook run failed to start: {status} {payload}")
    url, t0 = headers["Location"], time.time()
    log("load notebook started (Spark in a managed VNet starts in a few minutes)")
    while time.time() - t0 < timeout_min * 60:
        time.sleep(20)
        _, _, job = _request("get", url, None, FABRIC)
        if job.get("status") in ("Completed", "Failed", "Cancelled", "Deduped"):
            log(f"load notebook {job['status']} after {time.time() - t0:.0f}s")
            return job
    raise SystemExit("load notebook timed out")


# ----------------------------------------------------------------- mirror
def grant_sami(w: str, principal_id: str) -> None:
    current = fabric("get", f"workspaces/{w}/roleAssignments").get("value", [])
    if not any(r["principal"]["id"] == principal_id for r in current):
        fabric("post", f"workspaces/{w}/roleAssignments",
               {"principal": {"id": principal_id, "type": "ServicePrincipal"}, "role": "Contributor"})
        log("SQL server managed identity added to the live4 workspace (Contributor: write to the mirror)")


def mirror(w: str, conn_id: str) -> dict:
    definition = {"properties": {
        "source": {"type": "AzureSqlDatabase", "typeProperties": {"connection": conn_id, "database": DB}},
        "target": {"type": "MountedRelationalDatabase", "typeProperties": {"defaultSchema": "dbo", "format": "Delta"}}}}
    item = upsert_item(w, "MirroredDatabase", STORES["mirror"], [
        {"path": "mirroring.json", "payload": b64(definition), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("MirroredDatabase", STORES["mirror"])), "payloadType": "InlineBase64"}],
        "live4 store: mirrored Azure SQL (private, via VNet data gateway)", update=False)
    base = f"workspaces/{w}/mirroredDatabases/{item['id']}"
    t0 = time.time()
    state = fabric("post", f"{base}/getMirroringStatus").get("status")
    for _ in range(40):
        if state != "Initializing":
            break
        time.sleep(15)
        state = fabric("post", f"{base}/getMirroringStatus").get("status")
    if state not in ("Running", "Starting"):
        fabric("post", f"{base}/startMirroring")
        log("mirroring started")
    expected = {t: tb.num_rows for t, tb in snap().items()}
    for _ in range(90):
        tables = fabric("post", f"{base}/getTablesMirroringStatus").get("data", [])
        done = {t["sourceTableName"]: (t.get("metrics") or {}).get("processedRows", 0) for t in tables
                if t.get("status") in ("Replicating", "Running")}
        if all(done.get(t, 0) >= n for t, n in expected.items()):
            break
        time.sleep(20)
    else:
        raise SystemExit(f"mirroring did not catch up: {json.dumps(tables)[:600]}")
    lag = time.time() - t0
    props = fabric("get", base).get("properties", {})
    host = props["sqlEndpointProperties"]["connectionString"]
    log(f"{STORES['mirror']}: all {len(expected)} tables replicated in {lag:.0f}s")
    return update_state(mirror={"id": item["id"], "host": host, "database": STORES["mirror"],
                                "initial_sync_s": round(lag), "path": "Azure SQL (private) via VNet data gateway"})


def azuresql() -> None:
    from fabric_rest import workspace_identity
    w = ws()
    network()
    srv = sql_server()
    if srv.get("publicNetworkAccess") != "Disabled":
        log(f"note: public network access is {srv.get('publicNetworkAccess')}; leaving it (governance controls it)")
    private_endpoint(srv["id"])
    identity = workspace_identity(w)
    managed_private_endpoint(w, srv["id"])
    nb = notebook(w, ws_display_name(w), identity["applicationId"])
    job = run_notebook(w, nb["id"])
    if job.get("status") != "Completed":
        log(f"load with getToken failed ({json.dumps(job.get('failureReason'))[:300]}); retrying with the caller's token")
        from fabric_rest import token
        job = run_notebook(w, nb["id"], token("https://database.windows.net/"))
        if job.get("status") != "Completed":
            raise SystemExit(f"load failed: {json.dumps(job.get('failureReason'))[:800]}")
    gw = gateway()
    conn = connection(gw)
    grant_sami(w, srv["identity"]["principalId"])
    mirror(w, conn)
    update_state(azuresql={"server": server(), "database": DB, "gateway_id": gw, "connection_id": conn})


def ws_display_name(w: str) -> str:
    return fabric("get", f"workspaces/{w}")["displayName"]
