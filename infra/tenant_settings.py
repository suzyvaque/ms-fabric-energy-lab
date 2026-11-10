"""Verify (and optionally enable) the Fabric tenant settings this lab needs.

Settings are matched by title keywords at runtime because internal names and titles change.
Anything not matched is listed so you can set it in the Admin portal (plan/checklists/step-0-portal.md).

  py infra/tenant_settings.py            # report only
  py infra/tenant_settings.py --apply    # enable missing ones (Admin API "update tenant setting")
"""
import argparse

from common import env, fab_api, load_env, log, require_tool, run

# (label, all keywords that must appear in the title, scope to automation group?)
REQUIRED = [
    ("Copilot / Azure OpenAI", ["copilot", "azure openai"], False),
    ("Cross-geo processing for AI (only if your capacity region lacks Azure OpenAI)", ["processed outside", "geographic"], False),
    ("Data agent (preview)", ["data agent"], False),
    ("Ontology item (preview)", ["ontology"], False),
    ("Git sync with GitHub", ["github"], False),
    ("Service principals can call Fabric public APIs", ["service principals", "fabric public apis"], True),
    ("Service principals can create workspaces/connections/pipelines", ["service principals", "create workspaces"], True),
    ("Workspace monitoring", ["monitoring", "workspace"], False),
    ("Certification", ["certification"], False),
]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    load_env()
    require_tool("fab", "fab auth login (as Fabric admin)")
    _, data = fab_api("admin/tenantsettings")
    settings = data.get("tenantSettings", data.get("value", []))
    group = None
    for label, keywords, scoped in REQUIRED:
        matches = [s for s in settings if all(k in s.get("title", "").lower() for k in keywords)]
        if not matches:
            print(f"[ MANUAL ] {label}: not found by keyword - set in Admin portal")
            continue
        for s in matches:
            mark = "OK" if s.get("enabled") else "OFF"
            print(f"[ {mark:6} ] {label} -> {s['title']} ({s['settingName']})")
            if a.apply and not s.get("enabled"):
                body = {"enabled": True}
                if scoped:
                    if group is None:
                        gid = run(["az", "ad", "group", "list", "--display-name", env("ENTRA_GROUP_AUTOMATION"),
                                   "--query", "[0].id", "-o", "tsv"]).stdout.strip()
                        group = {"graphId": gid, "name": env("ENTRA_GROUP_AUTOMATION")}
                    body["enabledSecurityGroups"] = [group]
                fab_api(f"admin/tenantsettings/{s['settingName']}/update", "post", body)
                log(f"enabled {s['settingName']}")


if __name__ == "__main__":
    main()
