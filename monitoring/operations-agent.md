# Operations agent vs Activator (Step 12)

Sample, not production guidance. Synthetic data. Everything ran in the isolated observability lab on a separate F64
capacity (`ce-obs-ops`, `infra/obs_lab.py`, D-041) with its **own** simulator stream, so injected anomalies never reached the
shared dev stream, its Activators or the evaluation sessions (plan review #1).

## Plan (as reviewed)

| Phase | Operations agent | Activator counterpart | Measures |
|---|---|---|---|
| 1 | `opa_obs_fleet` on `obs_fleet` (natural-language device-fault + site-offline rules, "notify once, group per site") | `act_obs_device_faults`, `act_obs_offline_sites` (clones of the dev rules) + `act_obs_fault_probe` (same rule, runs a pipeline = durable firing evidence) | Detection latency, precision/recall vs ground truth, noise, effort, CU |
| 2 | `opa_obs_jobs` on `obs_monitor.job_events`, recommends "Run remediation pipeline" and waits for **approval in Teams** | `act_obs_job_autofix`: runs `pl_obs_remediation` **automatically** | Time to remediation, human in the loop, evidence |
| 3 | `opa_obs_fleet` + an ontology source + a cross-domain priority rule | n/a (Activator has no ontology context) | What the ontology adds, freshness |

## What happened

### Trial 2 (2026-11-14): portal-authored agents vs Activator

**Root cause of the earlier block.** The agents created through the REST definition API could never generate a playbook, even a
one-line `opa_obs_smoke` ("alert me when battery_temp_c is above 55"): "No playbook generated: I was not able to process your
request". Their knowledge source reads back with id `00000000-…`. A **portal-created** agent with the same sentence
(`opa_obs_portal`) generated a playbook immediately. `getDefinition` returns empty instructions, sources and playbook for
portal agents, so the API and the portal don't share state yet (D-045).

**Copilot as a reviewer.** For the first (v1) instructions, generation answered with a list of what it couldn't derive: how to
compute "gateway offline 20 min", the site's region, the outage duration, and whether to use `ingest_time` or `event_time`. Rules
must be one property per object with a documented condition (*is above*, *crosses above*, *becomes*). Absence of data and
"once per id" aren't expressible.

**What the agent runs.** Every step is visible in workspace monitoring (`EventhouseQueryLogs`, creator's identity, OBO):
- **Generation:** `getschema` sampling, then "entity materialization" queries Copilot drafted (e.g. *DeviceTelemetry per-event
  telemetry from silver_telemetry for battery overheat alert rule*).
- **Running:** the rule query every 5 minutes (7 runs in 30 min, about 76 ms and negligible CPU each).
- **After a match:** a 7-day follow-up query on the same table (Investigator insights).

#### Phase 1: battery overheating (3 injections, 100 sites, `--anomaly-rate 0`)

| Site | Anomaly start | Device alert raised | Telemetry > 55 °C | Activator probe fired | Agent rule |
|---|---|---|---|---|---|
| SITE-000001 | 03:39:17 | 03:44:32 | 03:46:38 | 03:49:30 (alert + 4 m 58 s) | battery_temp_c above 55, evaluated 03:50:09 |
| SITE-000011 | 03:44:17 | 03:50:13 | 03:51:11 | 03:54:26 (alert + 4 m 13 s) | evaluated 03:55:09 |
| SITE-000012 | 03:49:17 | 03:55:24 | 03:56:58 | 03:59:27 (alert + 4 m 03 s) | evaluated 04:00:09 |

- **Activator:** 3/3 detected, 0 spurious, about 10 minutes from anomaly to action (5-minute poll + 2-minute ingestion delay).
- **Agent:** sent Teams messages to the creator for the overheating batteries. Message count and grouping per site weren't
  captured, because the Teams chat has no export or API. The rule (`is above`, a *state* condition) is evaluated on the same 5-minute
  cadence, and the first match is possible 3–4 minutes after the threshold crossing.
- The two detect **different signals**: the device's own alert (lower threshold, earlier) vs telemetry above 55 °C (later, but
  independent of the device's alerting logic).

#### Phase 2: failed job, auto vs approved remediation (one `pl_obs_daily` failure at 04:14:39)

| | Activator `act_obs_job_autofix` | Operations agent `opa_obs_jobs_portal` |
|---|---|---|
| Detected | Poll at 04:21:17 (missed 04:16:17 because of ingestion delay) | Rule query at **04:17:17 (2 m 38 s)** |
| Output | Ran `pl_obs_remediation` at **04:21:21 (6 m 42 s)**, succeeded 04:21:29 | Teams message with job id, status, rule text, data source, **Investigate further** / **Ask a question** |
| Remediation | Automatic, logged as `invokeType = Manual` | "I don't have any actions available to recommend": the Fabric item action wasn't attached, so nothing was approved or run |
| Human in the loop | None | Yes (message), but no action to approve |

### First attempt (2026-11-12): API-authored agents, blocked

| Step | Result |
|---|---|
| Create item (REST, item type `OperationsAgent`) | OK (empty item) |
| Configure instructions, KQL data source, message destination (REST `updateDefinition`, `shouldRun: false`, **no** `playbook`) | Accepted (200) |
| Any authored `playbook` (including `{}` and rule-shaped objects) | **Rejected**: "No rule definitions available in the playbook." The schema is documented as reserved |
| `shouldRun: true` without a generated playbook | **Rejected** (404 EntityNotFound) |
| `FabricJobAction` (documented) | **Rejected** (UnknownError) in every variant; `PowerAutomateAction` is accepted |
| Readback (`getDefinition`) | Data source `id` returned as all zeros; `messageDestination` omitted |
| **Portal "Generate playbook"** (user) | **Failed**: "rule generation for GenerateEntityAttributeRules failed with an internal infrastructure error" (also after Copilot edited the instructions) |
| Teams (where agent notifications and approvals arrive) | The tenant's Teams license is expiring |
| Tenant setting *Users can access Microsoft Copilot in Power BI and Microsoft Fabric* (`CatalystTenantAdminSwitch`) | Was **off**; enabled by `tenant_settings.py` afterwards. A likely prerequisite for playbook generation. Retry pending |

So the API-authored agents never ran (see Trial 2 for the cause). Both stay configured (`infra/operations_agent.py create --phase 0|1|2`) as evidence.

### Approver identity (Trial 2)

`infra/ops_approver.py` invites an external approver as a B2B guest, adds them to `ce-fabric-ops-approvers`, assigns the free
Fabric license and gives **Contributor on `ce-obs-ops` only** (recipients need write on the agent item; approved actions run with the
creator's permissions, so no data access is needed). Guest browsing is enabled for that group only (D-044). Findings:
- A corporate Microsoft account couldn't be invited (its home tenant restricts B2B), so a personal-domain account was used.
- The guest hadn't accepted before the trial, and *Agent behavior → recipient* wasn't found in the portal, so the creator received
  the messages.

### Phase 1: Activator baseline (first controlled trial, 2026-11-12)

The obs simulator (100 sites, `--anomaly-rate 0`) ran four injected anomalies 3, 5, 9 and 13 minutes after start (ground truth in
`data-gen/out_obs/ground_truth`, gitignored). The probe Activator runs `pl_obs_remediation` whenever the device-fault rule fires,
so `job_events` timestamps every firing.

| Injected (start) | Alert in `obs_fleet` (ingested) | Activator fired (pipeline created) | Alert → action | Anomaly → action |
|---|---|---|---|---|
| BATTERY_OVERTEMP site 1 (05:11:16) | raised 05:15:45 | 05:19:25 | 3 m 40 s | 8 m 09 s |
| DEVICE_OFFLINE site 20 (05:07:16) | COMM_LOSS raised 05:22:19 (cloud detects after 15 min) | 05:24:27 | 2 m 08 s | 17 m 11 s |
| BATTERY_OVERTEMP re-raise | 05:22:53 | 05:29:26 | 6 m 33 s | (repeat) |
| INVERTER_FAULT site 30 (05:15:16) | raised 05:24:32 | 05:29:26 | 4 m 54 s | 14 m 10 s |
| BATTERY_OVERTEMP re-raise | 05:29:47 | 05:34:26 | 4 m 39 s | (repeat) |
| BATTERY_OVERTEMP re-raise | 05:38:29 | 05:44:26 | 5 m 57 s | (repeat) |
| INVERTER_FAULT site 3 (05:05:16) | **no alert in the data** | none | n/a | n/a |

- **Precision 6/6, recall 6/6 of the raised alerts.** No firing for cleared events or empty windows.
- **Latency is set by the design, not the engine:** the 5-minute poll plus a 120 s ingestion-delay setting gives 2–7 minutes
  from alert to action. The device's own alert delay (and 15 minutes for cloud-side offline detection) dominates end to end.
- **Noise:** one flapping battery (raised/cleared 4×) produced **4 separate firings**. Activator rules fire per event and don't
  group or suppress repeats. That is the gap the operations agent's "notify once per alert, group per site" instruction was
  meant to close; it remains untested.
- One injected inverter fault never produced an alert in the data (the device didn't raise one), so no detector could see it:
  ground truth catches this, alert-based rules can't.
- The offline rule (`act_obs_offline_sites`) only notifies by email; its firing could not be verified without the mailbox.

### Phase 2: auto-remediation (Activator) vs approved remediation (agent)

| | Activator `act_obs_job_autofix` | Operations agent `opa_obs_jobs` |
|---|---|---|
| Trigger | `ItemJobFailed` for `pl_obs_daily` in `job_events` (Real-Time Hub job events, ~4 s after failure) | Same table |
| Action | Runs `pl_obs_remediation` **without approval** | Recommends it in Teams; runs it after **approval** |
| Result | Failure 04:55:55 → remediation created **05:01:29**, succeeded 05:01:37 (**5 m 34 s**, no human) | Blocked in the first attempt (no playbook); see Trial 2 |
| Evidence | `job_events`: the remediation run is logged with `invokeType = Manual`, indistinguishable from a person | n/a |

## Effort and cost

| | Activator | Operations agent |
|---|---|---|
| Authoring | Fully scriptable (`ReflexEntities.json`, Skills for Fabric shapes) incl. Fabric item actions | **Portal only in practice**: API-created agents can't generate a playbook, and portal agents can't be read back by the API; Fabric job action not via API |
| Logic | Explicit KQL + conditions (reviewable, deterministic) | LLM-generated rules from instructions (reviewable in the playbook) |
| Notification | Email / Teams / Fabric item | Teams (with approval loop) / Power Automate / Fabric job |
| Licensing | Fabric capacity | Fabric capacity (no trial) + Copilot/Azure OpenAI tenant settings + Teams |
| CU in this test (F64) | Trial window (45 min): **Reflex 6.6 CU-s** for 5 Activators polling every 5 min; capacity avg 1.9 % | Trial 2: rule queries every 5 min at ~76 ms each (negligible Kusto CPU). Generation and investigation run Copilot (AI CU); in the first attempt the window's largest workload was **ML 2,101 CU-s**, so even failed generations cost CU |

## Takeaways

1. **Activator is production-ready for deterministic rules.** Scriptable end to end, 9/9 correct firings across both trials,
   and it can remediate on its own. It is noisy on flapping signals, and its runs look manual in the logs: tag remediation runs
   with a parameter (e.g. `reason = activator`) when auditability matters.
2. **The operations agent detected faster here and explains more**: 2 m 38 s vs 6 m 42 s for the failed job (its rule
   uses a time window and isn't caught by the ingestion-delay gap). It also gives a Teams message with context, investigation and Q&A.
   But it's **portal-only**: API-created agents can't generate a playbook and the definition API can't read portal agents back.
   So no Git, no CI/CD and no fabric-cicd deploy yet (D-045).
3. **Approval needs an action.** Without a Fabric item action attached, the agent only notifies. With one, the approver
   (Contributor, in the organization, Teams app installed) approves and the action runs as the **creator** (D-044).
4. **Write rules the condition model can express.** One property, one condition. Copilot generation doubles as a spec review:
   it names the definitions your instructions are missing.
5. **Neither replaces ground truth.** Alert-based detection only sees what devices report; keep a telemetry-based check
   (e.g. `FleetBatteryAnomalies()`) for silent failures. The overheating trial showed that the device alert and the telemetry
   threshold fire minutes apart.

## Open

- Attach the Fabric item action (`pl_obs_remediation`) to `opa_obs_jobs_portal` and rerun Phase 2 to time *message → approval →
  run*.
- The guest accepts, installs the Teams app, and is set as recipient: verify an external approver can approve.
- Phase 3 (ontology as a second source) on a portal-authored agent.
