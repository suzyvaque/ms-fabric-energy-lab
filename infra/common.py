"""Shared helpers for infra scripts: .env loading, config, and CLI wrappers (fab, az, gh).

Never print secret values: commands are logged by verb only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "infra" / ".state"  # gitignored
CONFIG = json.loads((ROOT / "infra" / "config" / "lab.json").read_text(encoding="utf-8"))


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        raise SystemExit("Missing .env - copy .env.example to .env and fill it in.")
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        raise SystemExit(f"Missing required setting {name} in .env")
    return value


def set_env_value(name: str, value: str) -> None:
    """Write NAME=value into .env (in place) so later scripts can reuse generated IDs."""
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == name:
            lines[i] = f"{name}={value}"
            break
    else:
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[name] = value


def require_tool(name: str, hint: str) -> None:
    if not shutil.which(name):
        raise SystemExit(f"'{name}' not found. {hint}")


def run(cmd: list[str], check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    exe = shutil.which(cmd[0]) or cmd[0]  # resolves az.cmd / gh.exe on Windows
    res = subprocess.run([exe, *cmd[1:]], capture_output=True, text=True, encoding="utf-8", input=stdin)
    if check and res.returncode != 0:
        raise SystemExit(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed:\n{res.stdout}\n{res.stderr}")
    return res


def log(msg: str) -> None:
    print(f"[infra] {msg}", flush=True)


# ---------- Fabric CLI ----------
def fab(*args: str, check: bool = True) -> str:
    return run(["fab", *args], check=check).stdout.strip()


def fab_exists(path: str) -> bool:
    return "true" in fab("exists", path, check=False).lower()


def fab_id(path: str) -> str:
    return fab("get", path, "-q", "id").strip().strip('"')


def fab_api(endpoint: str, method: str = "get", body: dict | None = None,
            audience: str = "fabric", ok: tuple[int, ...] = (200, 201, 202)) -> tuple[int, object]:
    """Call `fab api`; returns (status_code, parsed body). Body goes via a temp file (may hold secrets)."""
    args = ["api", endpoint, "-X", method, "-A", audience]
    tmp = None
    try:
        if body is not None:
            tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(body, tmp)
            tmp.close()
            args += ["-i", tmp.name]
        out = run(["fab", *args], check=False).stdout.strip()
    finally:
        if tmp:
            os.unlink(tmp.name)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return 0, out
    status = int(data.get("status_code", 200)) if isinstance(data, dict) else 200
    payload = data.get("text", data) if isinstance(data, dict) else data
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
    if status not in ok:
        raise SystemExit(f"fab api {method.upper()} {endpoint} -> {status}: {payload}")
    return status, payload


def workspace_name(key: str, environment: str) -> str:
    return f"{env('WORKSPACE_PREFIX')}-{key}-{environment}"


def save_state(name: str, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_state(name: str) -> dict:
    path = STATE_DIR / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
