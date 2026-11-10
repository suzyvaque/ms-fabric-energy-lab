"""Entra setup (Azure CLI). Idempotent.

Creates: security groups, the CI/CD app registration + service principal, GitHub OIDC
federated credentials (one per GitHub environment, no client secret), and adds the SP to the
automation group used to scope Fabric tenant settings. Writes SP_CLIENT_ID / SP_OBJECT_ID to .env.

  az login --tenant <tenant>   (as a user who can create apps and groups)
  py infra/entra_setup.py
"""
import json
import os
import tempfile

from common import CONFIG, env, load_env, log, require_tool, run, set_env_value

GROUP_KEYS = ["ENTRA_GROUP_ADMINS", "ENTRA_GROUP_ENGINEERS", "ENTRA_GROUP_VIEWERS", "ENTRA_GROUP_AUTOMATION"]


def az_json(*args: str):
    out = run(["az", *args, "-o", "json"]).stdout
    return json.loads(out) if out.strip() else None


def ensure_group(name: str) -> str:
    found = az_json("ad", "group", "list", "--display-name", name, "--query", "[].id")
    if found:
        return found[0]
    log(f"creating group {name}")
    return az_json("ad", "group", "create", "--display-name", name, "--mail-nickname", name)["id"]


def ensure_member(group_id: str, member_id: str) -> None:
    if not az_json("ad", "group", "member", "check", "--group", group_id, "--member-id", member_id)["value"]:
        run(["az", "ad", "group", "member", "add", "--group", group_id, "--member-id", member_id])


def oidc_subjects() -> list[tuple[str, str]]:
    """Name-based and ID-based (immutable) GitHub OIDC subjects for each environment.

    Newer repos send `repo:<owner>@<ownerId>/<repo>@<repoId>:environment:<env>`; older ones
    `repo:<owner>/<repo>:environment:<env>`. Registering both keeps either format working.
    """
    owner, repo = env("GITHUB_OWNER"), env("GITHUB_REPO")
    subjects = []
    ids = run(["gh", "api", f"repos/{owner}/{repo}", "--jq", "[.owner.id, .id] | @tsv"], check=False).stdout.split()
    for environment in CONFIG["environments"]:
        subjects.append((f"github-{environment}", f"repo:{owner}/{repo}:environment:{environment}"))
        if len(ids) == 2:
            subjects.append((f"github-{environment}-id",
                             f"repo:{owner}@{ids[0]}/{repo}@{ids[1]}:environment:{environment}"))
        else:
            log("gh not available/authenticated - skipped ID-based OIDC subjects")
    return subjects


def main() -> None:
    load_env()
    require_tool("az", "Install Azure CLI and run az login --tenant <tenant>")
    groups = {k: ensure_group(env(k)) for k in GROUP_KEYS}
    me = az_json("ad", "signed-in-user", "show", "--query", "id")
    ensure_member(groups["ENTRA_GROUP_ADMINS"], me)

    app_name = env("SP_APP_NAME")
    apps = az_json("ad", "app", "list", "--display-name", app_name, "--query", "[].{appId:appId,id:id}")
    if apps:
        app = apps[0]
    else:
        log(f"creating app registration {app_name}")
        app = az_json("ad", "app", "create", "--display-name", app_name, "--sign-in-audience", "AzureADMyOrg",
                      "--query", "{appId:appId,id:id}")
    sps = az_json("ad", "sp", "list", "--filter", f"appId eq '{app['appId']}'", "--query", "[].id")
    sp_id = sps[0] if sps else az_json("ad", "sp", "create", "--id", app["appId"], "--query", "id")
    ensure_member(groups["ENTRA_GROUP_AUTOMATION"], sp_id)

    existing = {c["name"] for c in az_json("ad", "app", "federated-credential", "list", "--id", app["id"]) or []}
    for name, subject in oidc_subjects():
        if name in existing:
            continue
        params = {"name": name, "issuer": "https://token.actions.githubusercontent.com",
                  "subject": subject, "audiences": ["api://AzureADTokenExchange"],
                  "description": "GitHub Actions OIDC (see plan/decision-log.md D-003)"}
        # Pass JSON via file: az.cmd on Windows mangles inline JSON quoting.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(params, f)
        try:
            run(["az", "ad", "app", "federated-credential", "create", "--id", app["id"],
                 "--parameters", f"@{f.name}"])
        finally:
            os.unlink(f.name)
        log(f"federated credential {name} created")

    set_env_value("SP_CLIENT_ID", app["appId"])
    set_env_value("SP_OBJECT_ID", sp_id)
    log("done - SP_CLIENT_ID/SP_OBJECT_ID written to .env (no client secret created)")


if __name__ == "__main__":
    main()
