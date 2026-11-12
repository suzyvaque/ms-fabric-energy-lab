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

### Operations agent: blocked

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

So the agent never ran, and Phases 1 and 3 have no agent results. Both agents stay configured (`infra/operations_agent.py
create --phase 1|2`) so a retry only needs the portal "Generate playbook", then Save and Start.

### Phase 1: Activator baseline (controlled trial)

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
| Result | Failure 04:55:55 → remediation created **05:01:29**, succeeded 05:01:37 (**5 m 34 s**, no human) | Blocked (no playbook) |
| Evidence | `job_events`: the remediation run is logged with `invokeType = Manual`, indistinguishable from a person | n/a |

## Effort and cost

| | Activator | Operations agent |
|---|---|---|
| Authoring | Fully scriptable (`ReflexEntities.json`, Skills for Fabric shapes) incl. Fabric item actions | Instructions/sources scriptable; **playbook only via portal Copilot**; Fabric job action not via API |
| Logic | Explicit KQL + conditions (reviewable, deterministic) | LLM-generated rules from instructions (reviewable in the playbook) |
| Notification | Email / Teams / Fabric item | Teams (with approval loop) / Power Automate / Fabric job |
| Licensing | Fabric capacity | Fabric capacity (no trial) + Copilot/Azure OpenAI tenant settings + Teams |
| CU in this test (F64) | Trial window (45 min): **Reflex 6.6 CU-s** for 5 Activators polling every 5 min; capacity avg 1.9 % | Never ran. The window's largest workload was **ML 2,101 CU-s**, most likely the portal Copilot/playbook-generation attempts, so even failed generation costs CU |

## Takeaways

1. **Activator is production-ready for deterministic rules.** Scriptable end to end, 100 % precision on this trial, and it can
   remediate on its own. It is noisy on flapping signals and its runs look manual in the logs: tag remediation runs with a
   parameter (e.g. `reason = activator`) when auditability matters.
2. **The operations agent's value is judgment + human approval** (grouping, context, "don't act if it already recovered").
   In this tenant it couldn't be evaluated: playbook generation fails service-side and the action/approval channel (Teams)
   is unavailable. Re-test when generation works; the trial harness (`--inject TYPE:SITE@+MIN`, probe Activator, job events)
   is ready for a side-by-side run on identical events.
3. **Neither replaces ground truth.** Alert-based detection only sees what devices report; keep a telemetry-based check
   (e.g. `FleetBatteryAnomalies()`) for silent failures.
