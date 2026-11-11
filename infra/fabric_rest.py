"""Minimal Fabric REST + Kusto client using Azure CLI tokens (handles long-running operations).

Used where `fab api` is not enough: item create/update with definitions (LRO) and KQL queries.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request

from common import run

FABRIC = "https://api.fabric.microsoft.com"
_tokens: dict[str, tuple[str, float]] = {}


def token(resource: str) -> str:
    cached = _tokens.get(resource)
    if cached and cached[1] > time.time() + 120:
        return cached[0]
    out = json.loads(run(["az", "account", "get-access-token", "--resource", resource, "-o", "json"]).stdout)
    _tokens[resource] = (out["accessToken"], float(out.get("expires_on") or time.time() + 3000))
    return out["accessToken"]


def _request(method: str, url: str, body: dict | None, resource: str) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper(), headers={
        "Authorization": f"Bearer {token(resource)}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw.decode(errors="ignore")}
        return e.code, dict(e.headers), payload


def fabric(method: str, path: str, body: dict | None = None, ok=(200, 201, 202)) -> dict:
    """Call the Fabric REST API; waits for long-running operations and returns the final result."""
    url = path if path.startswith("http") else f"{FABRIC}/v1/{path.lstrip('/')}"
    for attempt in range(6):
        status, headers, payload = _request(method, url, body, FABRIC)
        if status == 429:
            time.sleep(int(headers.get("Retry-After", 10)))
            continue
        break
    if status not in ok:
        raise SystemExit(f"{method.upper()} {path} -> {status}: {json.dumps(payload)[:800]}")
    if status == 202 and headers.get("Location"):
        op = headers["Location"]
        while True:
            time.sleep(int(headers.get("Retry-After", 3)))
            status, headers, state = _request("get", op, None, FABRIC)
            if state.get("status") in ("Succeeded", "Failed", "Undefined") or status not in (200, 202):
                break
        if state.get("status") != "Succeeded":
            raise SystemExit(f"LRO {path} failed: {json.dumps(state)[:800]}")
        status, _, result = _request("get", op.rstrip("/") + "/result", None, FABRIC)
        return result if status == 200 else state
    return payload


def b64(obj) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj, indent=2)
    return base64.b64encode(raw.encode("utf-8")).decode()


def find_item(workspace_id: str, item_type: str, name: str) -> dict | None:
    items = fabric("get", f"workspaces/{workspace_id}/items?type={item_type}").get("value", [])
    return next((i for i in items if i["displayName"] == name), None)


def upsert_item(workspace_id: str, item_type: str, name: str, parts: list[dict] | None,
                description: str = "", creation_payload: dict | None = None, update: bool = True,
                definition_format: str | None = None) -> dict:
    """Create the item (with definition) or, if update=True, update its definition. Returns the item."""
    item = find_item(workspace_id, item_type, name)
    definition = {"parts": parts} if parts else None
    if definition and definition_format:
        definition["format"] = definition_format
    if item is None:
        body = {"displayName": name, "type": item_type, "description": description}
        if definition:
            body["definition"] = definition
        if creation_payload:
            body["creationPayload"] = creation_payload
        fabric("post", f"workspaces/{workspace_id}/items", body)
        item = find_item(workspace_id, item_type, name)
    elif definition and update:
        fabric("post", f"workspaces/{workspace_id}/items/{item['id']}/updateDefinition", {"definition": definition})
    return item


def workspace_identity(workspace_id: str) -> dict:
    """Provision (once) and return the workspace identity {applicationId, servicePrincipalId}.

    Provisioning occasionally fails half-way (InternalError, then WorkspaceIdentityAlreadyExists while
    GET shows none); deprovision + provision recovers it.
    """
    info = fabric("get", f"workspaces/{workspace_id}").get("workspaceIdentity")
    for _ in range(2):
        if info:
            return info
        try:
            fabric("post", f"workspaces/{workspace_id}/provisionIdentity")
        except SystemExit:
            fabric("post", f"workspaces/{workspace_id}/deprovisionIdentity", ok=(200, 202, 400))
            time.sleep(20)
        info = fabric("get", f"workspaces/{workspace_id}").get("workspaceIdentity")
    if not info:
        raise SystemExit("workspace identity could not be provisioned")
    return info


def ensure_connection(name: str, conn_type: str, creation_method: str, params: dict, credentials: dict) -> str:
    """Create a shareable cloud connection if missing (matched by display name). Returns its id."""
    for c in fabric("get", "connections").get("value", []):
        if c.get("displayName") == name:
            return c["id"]
    body = {"connectivityType": "ShareableCloud", "displayName": name, "privacyLevel": "Organizational",
            "connectionDetails": {"type": conn_type, "creationMethod": creation_method,
                                  "parameters": [{"dataType": "Text", "name": k, "value": v} for k, v in params.items()]},
            "credentialDetails": {"credentials": credentials, "singleSignOnType": "None",
                                  "connectionEncryption": "NotEncrypted", "skipTestConnection": False}}
    for attempt in range(10):  # RBAC / database user propagation can take minutes
        try:
            return fabric("post", "connections", body)["id"]
        except SystemExit as e:
            if attempt == 9 or not any(s in str(e) for s in ("IncorrectCredentials", "Unauthorized", "Login failed")):
                raise
            time.sleep(30)
    raise SystemExit("unreachable")


def share_connection(connection_id: str, principal_id: str, principal_type: str = "ServicePrincipal",
                     role: str = "User") -> None:
    current = fabric("get", f"connections/{connection_id}/roleAssignments").get("value", [])
    if not any(r["principal"]["id"] == principal_id for r in current):
        fabric("post", f"connections/{connection_id}/roleAssignments",
               {"principal": {"id": principal_id, "type": principal_type}, "role": role})


def tsql(host: str, database: str, query: str) -> list[tuple]:
    """Query a Fabric SQL analytics endpoint (or Azure SQL) with an Entra token (mssql-python driver)."""
    import struct

    import mssql_python
    tok = token("https://database.windows.net/").encode("utf-16-le")
    conn = mssql_python.connect(f"Server=tcp:{host},1433;Database={database};Encrypt=yes;TrustServerCertificate=no",
                                attrs_before={1256: struct.pack(f"<I{len(tok)}s", len(tok), tok)})
    try:
        cur = conn.cursor()
        cur.execute(query)
        return cur.fetchall()
    finally:
        conn.close()


def kql(query_uri: str, database: str, query: str, mgmt: bool = False) -> list[dict]:
    """Run a KQL query (or management command) against an Eventhouse; returns rows as dicts."""
    url = f"{query_uri.rstrip('/')}/v1/rest/{'mgmt' if mgmt else 'query'}"
    status, _, payload = _request("post", url, {"db": database, "csl": query}, "https://kusto.kusto.windows.net")
    if status != 200:
        raise SystemExit(f"KQL failed ({status}): {json.dumps(payload)[:800]}")
    table = payload["Tables"][0]
    cols = [c["ColumnName"] for c in table["Columns"]]
    return [dict(zip(cols, r)) for r in table["Rows"]]
