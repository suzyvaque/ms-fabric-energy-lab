"""Minimal OneLake (ADLS Gen2 DFS API) client with Entra tokens from Azure CLI.

  upload(ws_id, item_id, "Files/LandingZone/payments/00000000000000000001.parquet", data)
  ls(ws_id, item_id, "Files/LandingZone/payments") -> [names]
  walk(ws_id, item_id, "Files/DiagnosticLogs") -> [(path, size)] (recursive, files only)
  read(ws_id, item_id, "Files/x.json") -> bytes
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from fabric_rest import token

ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
STORAGE = "https://storage.azure.com/"


def _req(method: str, url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    h = {"Authorization": f"Bearer {token(STORAGE)}", "x-ms-version": "2023-11-03"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def upload(ws_id: str, item_id: str, path: str, data: bytes) -> None:
    url = f"{ONELAKE}/{ws_id}/{item_id}/{urllib.parse.quote(path)}"
    for step, (method, q, body, hdr) in enumerate((
            ("PUT", "?resource=file", None, {}),
            ("PATCH", "?action=append&position=0", data, {"Content-Type": "application/octet-stream"}),
            ("PATCH", f"?action=flush&position={len(data)}", None, {}))):
        if step == 1 and not data:
            continue
        if step == 2 and not data:
            break
        status, resp = _req(method, url + q, body, hdr)
        if status not in (200, 201, 202):
            raise SystemExit(f"OneLake {method} {path} -> {status}: {resp[:300]!r}")


def ls(ws_id: str, item_id: str, directory: str) -> list[str]:
    url = (f"{ONELAKE}/{ws_id}?resource=filesystem&recursive=false"
           f"&directory={urllib.parse.quote(f'{item_id}/{directory}')}")
    status, resp = _req("GET", url)
    if status == 404:
        return []
    if status != 200:
        raise SystemExit(f"OneLake list {directory} -> {status}: {resp[:300]!r}")
    return [p["name"].rsplit("/", 1)[-1] for p in json.loads(resp).get("paths", [])]


def walk(ws_id: str, item_id: str, directory: str) -> list[tuple[str, int]]:
    """Recursive file listing (paths relative to the item), following continuation tokens."""
    out, cont = [], ""
    while True:
        url = (f"{ONELAKE}/{ws_id}?resource=filesystem&recursive=true"
               f"&directory={urllib.parse.quote(f'{item_id}/{directory}')}" + (f"&continuation={urllib.parse.quote(cont)}" if cont else ""))
        h = {"Authorization": f"Bearer {token(STORAGE)}", "x-ms-version": "2023-11-03"}
        req = urllib.request.Request(url, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body, cont = json.loads(r.read()), r.headers.get("x-ms-continuation", "")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise SystemExit(f"OneLake walk {directory} -> {e.code}: {e.read()[:300]!r}")
        out += [(p["name"].split("/", 1)[1], int(p.get("contentLength", 0))) for p in body.get("paths", [])
                if p.get("isDirectory") != "true"]
        if not cont:
            return out


def read(ws_id: str, item_id: str, path: str) -> bytes:
    status, resp = _req("GET", f"{ONELAKE}/{ws_id}/{item_id}/{urllib.parse.quote(path)}")
    if status != 200:
        raise SystemExit(f"OneLake read {path} -> {status}: {resp[:300]!r}")
    return resp
