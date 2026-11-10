"""GitHub repo setup via gh CLI (gh auth login with your *personal* account). Idempotent.

  py infra/github_setup.py repo       # dev branch, secret scanning + push protection, main protection
  py infra/github_setup.py envs       # GitHub environments dev/prod (prod: main only)
  py infra/github_setup.py secrets    # AZURE_* + per-environment workspace IDs (after fabric_setup)
  py infra/github_setup.py issues     # labels + one issue per step from plan/backlog.json
"""
import argparse
import json

from common import CONFIG, ROOT, env, load_env, load_state, log, require_tool, run


def gh_api(path: str, method: str = "GET", body: dict | None = None, check: bool = True):
    args = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
    if body is not None:
        args += ["--input", "-"]
    res = run(args, check=check, stdin=json.dumps(body) if body is not None else None)
    return json.loads(res.stdout) if res.returncode == 0 and res.stdout.strip() else None


def repo(r: str) -> None:
    main_sha = gh_api(f"repos/{r}/git/ref/heads/main")["object"]["sha"]
    if gh_api(f"repos/{r}/git/ref/heads/dev", check=False) is None:
        gh_api(f"repos/{r}/git/refs", "POST", {"ref": "refs/heads/dev", "sha": main_sha})
        log("dev branch created")
    gh_api(f"repos/{r}", "PATCH", {
        "delete_branch_on_merge": False, "allow_merge_commit": True, "allow_squash_merge": True,
        "security_and_analysis": {"secret_scanning": {"status": "enabled"},
                                  "secret_scanning_push_protection": {"status": "enabled"}}})
    log("secret scanning + push protection enabled")
    gh_api(f"repos/{r}/branches/main/protection", "PUT", {
        "required_status_checks": {"strict": False, "contexts": ["gitleaks"]},
        "enforce_admins": True,
        "required_pull_request_reviews": {"required_approving_review_count": 0},
        "restrictions": None,
        "allow_force_pushes": False, "allow_deletions": False,
        "required_conversation_resolution": True})
    log("main protected: PR required (admins included), gitleaks check required")


def envs(r: str) -> None:
    gh_api(f"repos/{r}/environments/dev", "PUT", {})
    gh_api(f"repos/{r}/environments/prod", "PUT",
           {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
    policies = gh_api(f"repos/{r}/environments/prod/deployment-branch-policies") or {}
    if not any(p["name"] == "main" for p in policies.get("branch_policies", [])):
        gh_api(f"repos/{r}/environments/prod/deployment-branch-policies", "POST", {"name": "main", "type": "branch"})
    log("environments dev/prod ready (prod deploys from main only)")


def set_secret(r: str, name: str, value: str, environment: str | None = None) -> None:
    args = ["gh", "secret", "set", name, "--repo", r]
    if environment:
        args += ["--env", environment]
    run(args, stdin=value)
    log(f"secret {name}{' (' + environment + ')' if environment else ''} set")


def secrets(r: str) -> None:
    set_secret(r, "AZURE_TENANT_ID", env("TENANT_ID"))
    set_secret(r, "AZURE_CLIENT_ID", env("SP_CLIENT_ID"))
    state = load_state("workspaces")
    if not state:
        raise SystemExit("No workspace state - run infra/fabric_setup.py first")
    for ws in state.values():
        set_secret(r, f"FABRIC_WS_{ws['key'].upper()}", ws["id"], ws["environment"])


def issues(r: str) -> None:
    backlog = json.loads((ROOT / "plan" / "backlog.json").read_text(encoding="utf-8"))
    for label in backlog["labels"]:
        run(["gh", "label", "create", label["name"], "--repo", r, "--color", label["color"], "--force"])
    existing = {i["title"] for i in json.loads(run(
        ["gh", "issue", "list", "--repo", r, "--state", "all", "--limit", "200", "--json", "title"]).stdout)}
    for issue in backlog["issues"]:
        if issue["title"] in existing:
            continue
        args = ["gh", "issue", "create", "--repo", r, "--title", issue["title"], "--body", issue["body"]]
        for label in issue.get("labels", []):
            args += ["--label", label]
        run(args)
        log(f"issue created: {issue['title']}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["repo", "envs", "secrets", "issues", "all"])
    a = p.parse_args()
    load_env()
    require_tool("gh", "winget install GitHub.cli; gh auth login (personal account)")
    r = f"{env('GITHUB_OWNER')}/{env('GITHUB_REPO')}"
    steps = {"repo": repo, "envs": envs, "secrets": secrets, "issues": issues}
    for name in (["repo", "envs", "issues", "secrets"] if a.action == "all" else [a.action]):
        steps[name](r)


if __name__ == "__main__":
    main()
