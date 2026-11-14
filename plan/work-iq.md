# Work IQ: SharePoint and Teams knowledge with a Fabric data agent (Step 13)

Sample, not production guidance. Synthetic data; technicians are codes, not people.

## What "Work IQ" means here, and what the licence gates

| Layer | What it grounds on | How a Fabric data agent relates | Licence |
|---|---|---|---|
| **Work IQ** | Microsoft 365 signals: Teams chats, meetings, mail, SharePoint, OneDrive | Not a data agent source. Combined with Fabric only inside Microsoft 365 Copilot / Copilot Studio | **Microsoft 365 Copilot** |
| **Copilot Studio agent + Fabric data agent** (connected agent, preview) | SharePoint knowledge + the published Fabric data agent | The documented way to answer across SharePoint and Fabric in Teams | Microsoft 365 Copilot for the maker; Copilot Studio |
| **Fabric data agent in Microsoft 365 Copilot / Teams** | The agent's Fabric sources | Publish target of the data agent | Microsoft 365 Copilot |
| **This lab (no Copilot licence)** | SharePoint files + list + Teams channel messages brought into OneLake | The data agent queries them as lakehouse tables next to fleet and finance | Fabric capacity only |

The tenant has Business Premium, Teams, Entra P2 and Fabric (Free) licences, but **no Microsoft 365 Copilot licence**, so Work IQ,
Copilot Studio connected agents and publishing the agent to Teams / Microsoft 365 Copilot are recorded as licence-gated (D-046).

## Architecture

```
Teams team + SharePoint site "Contoso Energy Field Ops" (M365 group ce-fieldops)
 ├─ General channel files  = Shared Documents/General/Field Ops
 │    ├─ Warranty and Service Policy.md   (sections P1..P7)
 │    └─ Procedures/<ALERT_CODE>.md        (severity, SLA hours, warranty, parts, escalation, steps)
 ├─ List "Field Visits"                    (97 visits for real lh_asset alerts; some late, some missing)
 └─ General channel messages               (8 dispatcher posts: parts delays, escalations, customer reports)
        │ Graph (lab app ce-lab-m365, delegated admin)            │ OneLake shortcut (preview, workspace identity)
        ▼                                                         ▼
 lh_asset (ce-asset-dev): fieldops_visits · fieldops_procedures · fieldops_policy · fieldops_channel_messages   Files/fieldops_sharepoint
        ▼  joined on alert_id / site_id / alert_code with fleet_alert_events · sites · batteries · fin_contracts
 da_contoso_fieldops (data agent) ──► infra/work_iq.py digest ──► post to the Teams channel (licence-free "agent in Teams")
```

## Results

| Question | Sources used | Result |
|---|---|---|
| Critical BATT_OVERTEMP alerts visited after the SLA | visits (SharePoint list) + procedures (SharePoint files) + alerts (Eventhouse via hub) | **Correct**: the 4 late visits, hours vs the 24 h SLA |
| Overheating alerts with no visit: procedure, billing policy, contract product | procedures + policy + visits + alerts + contracts | Steps and SLA correct, sites with products listed; cited P5 (labour) but missed that P7 makes Lease/PPA repairs never billable |
| Dispatcher posts about parts delays and affected contracts | **Teams channel messages** + contracts | **Correct**: both posts quoted, contracts joined (one site has none) |
| Customer email at the site with most warranty claims | visits + sites | **Refused**, offered customer_id |

First attempt (before tuning): 0/3 useful answers. The channel table wasn't selected, "battery overheating" wasn't mapped to
`BATT_OVERTEMP`, and one question was stopped by the service's "irrelevant or harmful" filter. Mapping the vocabulary in the
instructions and adding few-shots fixed all three: **unstructured knowledge needs the same semantic work as tables**.

## Blockers and solutions

| Blocker | Solution |
|---|---|
| No Microsoft 365 Copilot licence | Work IQ / Copilot Studio / publish-to-Teams documented as gated; knowledge brought into OneLake instead (D-046) |
| Azure CLI token rejected by SharePoint (401 `invalid_request`) and no Sites/Files/ChannelMessage scopes on its Graph token | Lab app `ce-lab-m365` (single-tenant public client) with admin-consented delegated Graph scopes, created by script |
| Graph CAE challenge (`TokenCreatedWithOutdatedPolicies`) | `az logout` + `az login` (a plain `az login` reused the revoked cached token) |
| **Plaintext token cache written into the OneDrive-synced repo folder** (gitignored, never committed) was flagged by Purview | Caches moved to `%LOCALAPPDATA%` and encrypted (DPAPI); the exposed refresh token invalidated by deleting and re-creating the app; test-user sessions revoked |
| SharePoint connection: `SharePointList` creation method, no skip-test | Fixed in script |
| Workspace identity can't read SharePoint | Documented grants applied by script: `Sites.Selected` (SharePoint + Graph) on the workspace identity and **read on this one site**; connection on the site URL. Shortcut creation still returns "Unauthorized exception, failed to retrieve SharePoint drives" (possibly propagation). Fallback: create the shortcut in the portal with an **organizational account** connection |
| Data agents read tables, not files | Files and list are parsed into Delta tables; the shortcut exposes the raw files for Spark/other engines |

## Teams: what works without Copilot

- **Works:** the team and its channel files (the SharePoint library); posting and reading channel messages through Graph; the
  agent's answer as a channel post (`work_iq.py digest`); Activator / operations agent messages (Step 12).
- **Gated:** chatting with the data agent in Teams or Microsoft 365 Copilot, Copilot Studio agents, Work IQ grounding on chats and meetings.

## Effort and cost

- Ingestion is a script run (seconds); refresh it on a schedule or when the list changes. The shortcut avoids copying files.
- Agent answers took 15–40 s; the CU is the Fabric data agent's (AI) plus SQL endpoint queries.
