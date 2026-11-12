"""Step 11: Microsoft Foundry resources for data agent evaluation (Azure CLI). Idempotent. Secretless (Entra ID).

Creates an AIServices account (local/key auth disabled) with a Foundry project and two deployments:
  - FOUNDRY_JUDGE_DEPLOYMENT     gpt-4.1-mini          LLM judge for the Foundry evaluation SDK (SimilarityEvaluator)
  - FOUNDRY_DECISION_DEPLOYMENT  Microsoft-Decision-1  "System One" judge: typed noul/choice/score decisions
  - FOUNDRY_OPTIMIZER_DEPLOYMENT gpt-4.1               live2.5 optimizer: proposes agent improvements from failures
Grants "Cognitive Services User" on the account to the signed-in user and, if SP_OBJECT_ID is set, the CI service principal.
Writes FOUNDRY_* settings to .env.

  py infra/foundry.py create
  py infra/foundry.py status
  py infra/foundry.py delete --yes
"""
from __future__ import annotations

import argparse
import json
import secrets

from common import env, load_env, log, require_tool, run, set_env_value

PROJECT = "agent-eval"
# Both models must be offered in the region; westus2 (the lab's default) has Microsoft-Decision-1 but not gpt-4.1-mini.
DEFAULT_LOCATION = "westus3"
DEPLOYMENTS = {
    "FOUNDRY_JUDGE_DEPLOYMENT": ("judge-gpt-4-1-mini", "gpt-4.1-mini", "OpenAI", "2025-04-14", 50),
    # capacity = requests/min (and x1K tokens/min) for Microsoft-Decision-1; a full run is ~42 decisions
    "FOUNDRY_DECISION_DEPLOYMENT": ("decision-1", "Microsoft-Decision-1", "Microsoft", "1", 100),
    # Step 11 live2.5: proposes instruction / few-shot improvements from logged failures (a few calls per round)
    "FOUNDRY_OPTIMIZER_DEPLOYMENT": ("optimizer-gpt-4-1", "gpt-4.1", "OpenAI", "2025-04-14", 50),
}
ROLE = "Cognitive Services User"  # all Microsoft.CognitiveServices/* data actions ("Azure AI User" is not in every tenant)


def az(*args: str, check: bool = True):
    res = run(["az", *args, "--subscription", env("AZURE_SUBSCRIPTION_ID"), "-o", "json"], check=check)
    if res.returncode != 0:
        return None
    return json.loads(res.stdout) if res.stdout.strip() else {}


def account() -> str:
    name = env("FOUNDRY_ACCOUNT", required=False)
    if not name:
        name = f"ce-agent-eval-{secrets.token_hex(3)}"
        set_env_value("FOUNDRY_ACCOUNT", name)
    return name


def grant(principal_id: str, principal_type: str, scope: str) -> None:
    existing = az("role", "assignment", "list", "--assignee", principal_id, "--scope", scope, "--role", ROLE,
                  check=False) or []
    if not existing:
        az("role", "assignment", "create", "--assignee-object-id", principal_id, "--assignee-principal-type",
           principal_type, "--role", ROLE, "--scope", scope)
        log(f"granted {ROLE} to {principal_type}")


def create() -> None:
    rg, name = env("AZURE_RESOURCE_GROUP"), account()
    loc = env("FOUNDRY_LOCATION", required=False) or DEFAULT_LOCATION
    state = run(["az", "provider", "show", "-n", "Microsoft.CognitiveServices", "--subscription",
                 env("AZURE_SUBSCRIPTION_ID"), "--query", "registrationState", "-o", "tsv"]).stdout.strip()
    if state != "Registered":
        log("registering resource provider Microsoft.CognitiveServices (one-time, ~1-2 min)")
        run(["az", "provider", "register", "-n", "Microsoft.CognitiveServices", "--subscription",
             env("AZURE_SUBSCRIPTION_ID"), "--wait"])
    acct =  az("cognitiveservices", "account", "show", "-g", rg, "-n", name, check=False)
    if acct is None:
        log(f"creating Foundry account {name} in {loc} (AIServices S0)")
        acct = az("cognitiveservices", "account", "create", "-g", rg, "-n", name, "-l", loc, "--kind", "AIServices",
                  "--sku", "S0", "--custom-domain", name, "--allow-project-management", "--assign-identity", "--yes",
                  "--tags", "project=contoso-energy-lab", "step=11")
    if not acct["properties"].get("disableLocalAuth"):
        az("resource", "update", "--ids", acct["id"], "--set", "properties.disableLocalAuth=true")
        log("key auth disabled (Entra ID only)")
    if az("cognitiveservices", "account", "project", "show", "-g", rg, "-n", name, "--project-name", PROJECT,
          check=False) is None:
        az("cognitiveservices", "account", "project", "create", "-g", rg, "-n", name, "--project-name", PROJECT, "-l", loc)
        log(f"project {PROJECT} created")
    for key, (dep, model, fmt, version, capacity) in DEPLOYMENTS.items():
        cur = az("cognitiveservices", "account", "deployment", "show", "-g", rg, "-n", name, "--deployment-name", dep,
                 check=False)
        if cur is None or cur.get("sku", {}).get("capacity") != capacity:  # create is a PUT: also updates capacity
            az("cognitiveservices", "account", "deployment", "create", "-g", rg, "-n", name, "--deployment-name", dep,
               "--model-name", model, "--model-format", fmt, "--model-version", version,
               "--sku-name", "GlobalStandard", "--sku-capacity", str(capacity))
            log(f"deployment {dep} ({model}, capacity {capacity}) {'created' if cur is None else 'updated'}")
        set_env_value(key, dep)
    set_env_value("FOUNDRY_ENDPOINT", f"https://{name}.services.ai.azure.com")
    set_env_value("FOUNDRY_PROJECT_ENDPOINT", f"https://{name}.services.ai.azure.com/api/projects/{PROJECT}")
    me = run(["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"]).stdout.strip()
    grant(me, "User", acct["id"])
    if env("SP_OBJECT_ID", required=False):
        grant(env("SP_OBJECT_ID"), "ServicePrincipal", acct["id"])


def status() -> None:
    rg, name = env("AZURE_RESOURCE_GROUP"), env("FOUNDRY_ACCOUNT")
    acct = az("cognitiveservices", "account", "show", "-g", rg, "-n", name)
    deps = az("cognitiveservices", "account", "deployment", "list", "-g", rg, "-n", name)
    print(json.dumps({"location": acct["location"], "disableLocalAuth": acct["properties"].get("disableLocalAuth"),
                      "endpoint": env("FOUNDRY_ENDPOINT", required=False),
                      "deployments": {d["name"]: f"{d['properties']['model']['name']} ({d['sku']['name']})" for d in deps}},
                     indent=2))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "status", "delete"])
    p.add_argument("--yes", action="store_true")
    a = p.parse_args()
    load_env()
    require_tool("az", "Install Azure CLI and run az login")
    if a.action == "create":
        create()
    elif a.action == "status":
        status()
    else:
        if not a.yes:
            raise SystemExit("delete requires --yes")
        az("cognitiveservices", "account", "delete", "-g", env("AZURE_RESOURCE_GROUP"), "-n", env("FOUNDRY_ACCOUNT"))
        log("Foundry account deleted (soft-deleted; purge with az cognitiveservices account purge)")


if __name__ == "__main__":
    main()
