"""Verify (and optionally enable) the Fabric tenant settings this lab needs.

Settings are matched by settingName (stable), with a title-keyword fallback.
Anything not found is listed so you can set it in the Admin portal (plan/checklists/step-0-portal.md).

  py infra/tenant_settings.py            # report only
  py infra/tenant_settings.py --apply    # enable missing *required* settings (Admin API, preview)
"""
import argparse

from common import env, fab_api, load_env, log, require_tool, run

# (settingName, title keywords fallback, label, scope group env key or None, required)
SETTINGS = [
    ("EnableAOAI", ["copilot", "azure openai"], "Copilot + data agents (Azure OpenAI)", None, True),
    ("AllowSendAOAIDataToOtherRegions", ["azure openai", "processed outside"],
     "Cross-geo AI processing (only if capacity region lacks Azure OpenAI)", None, False),
    ("OntologyPreview", ["ontology"], "Ontology item (preview)", None, True),
    ("GitIntegrationTenantSwitch", ["synchronize workspace items", "git"], "Git integration", None, True),
    ("GitHubTenantSettings", ["github"], "Git sync with GitHub", None, True),
    ("ServicePrincipalAccessPermissionAPIs", ["service principals", "fabric public apis"],
     "Service principals can call Fabric public APIs", "ENTRA_GROUP_AUTOMATION", True),
    ("ServicePrincipalAccessGlobalAPIs", ["service principals", "create workspaces"],
     "SP can create workspaces/connections (not needed: SP only deploys)", "ENTRA_GROUP_AUTOMATION", False),
    ("PlatformMonitoringTenantSetting", ["monitoring", "workspace"], "Workspace monitoring (preview)", None, True),
    ("MonitoringTenant", ["monitoring items"], "Users can create Monitoring items (preview; not needed for admins)",
     "ENTRA_GROUP_ADMINS", False),
    ("AllowMonitoringDataCollection", ["monitoring data collection"], "Allow monitoring data collection (step 6/7)",
     None, True),
    ("CertifyDatasets", ["certification"], "Certification (certifiers = admins group)", "ENTRA_GROUP_ADMINS", True),
    # Step 9: the API rejects these until Purview sensitivity labels are published to the admin (licensing: see runbook)
    ("EimInformationProtectionEdit", ["apply sensitivity labels"],
     "Users can apply sensitivity labels (step 9; needs published Purview labels)", None, False),
    ("EimInformationProtectionDownstreamInheritanceSetting", ["downstream"],
     "Labels flow to downstream content (step 9; needs published Purview labels)", None, False),
    ("EimInformationProtectionDefaultLabelDomainSetting", ["default sensitivity labels", "domains"],
     "Domain admins can set default labels (preview, step 9)", None, False),
]


def group_ref(key: str) -> dict:
    gid = run(["az", "ad", "group", "list", "--display-name", env(key), "--query", "[0].id", "-o", "tsv"]).stdout.strip()
    if not gid:
        raise SystemExit(f"Entra group {env(key)} not found; run infra/entra_setup.py")
    return {"graphId": gid, "name": env(key)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    load_env()
    require_tool("fab", "fab auth login (as Fabric admin)")
    _, data = fab_api("admin/tenantsettings")
    settings = data.get("tenantSettings", data.get("value", []))
    by_name = {s["settingName"]: s for s in settings}
    missing_required = 0
    for name, keywords, label, scope, required in SETTINGS:
        s = by_name.get(name) or next(
            (x for x in settings if all(k in x.get("title", "").lower() for k in keywords)), None)
        tag = "required" if required else "optional"
        if s is None:
            print(f"[ MANUAL ] {label} ({tag}): not found - set in Admin portal")
            missing_required += required
            continue
        on = s.get("enabled")
        groups = ",".join(g.get("name", "") for g in s.get("enabledSecurityGroups") or []) or "everyone"
        print(f"[ {'OK' if on else 'OFF':6} ] {label} ({tag}) -> {s['settingName']}" + (f" [{groups}]" if on else ""))
        if on or not required:
            continue
        if not a.apply:
            missing_required += 1
            continue
        body = {"enabled": True}
        if scope:
            body["enabledSecurityGroups"] = [group_ref(scope)]
        fab_api(f"admin/tenantsettings/{s['settingName']}/update", "post", body)
        log(f"enabled {s['settingName']}" + (f" for {env(scope)}" if scope else ""))
    if missing_required:
        raise SystemExit(f"{missing_required} required setting(s) off - rerun with --apply")


if __name__ == "__main__":
    main()
