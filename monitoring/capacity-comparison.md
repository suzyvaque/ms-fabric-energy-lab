# Capacity monitoring comparison (Step 6)

Sample, not production guidance. Measured on a pay-as-you-go **F2** capacity that also runs the rest of the lab (live stream ~13 events/s,
two Eventhouses, Activators, data agents). Synthetic workload.

## 1. What the Capacity Metrics app cannot do

| Gap | Detail |
|---|---|
| **Retention** | 14 days of compute timepoints (30 days storage). No history beyond that unless you extract it. |
| **Alerting** | None in the app. Thresholds have to be built elsewhere. |
| **Customization / export** | The app's semantic model is locked. Custom visuals or joins need Semantic Link / XMLA extraction (unsupported, can break on app updates). |
| **Cost and owner chargeback** | Shows CU, not money. No owner/department allocation and no Azure invoice data. |
| **Query-level root cause** | Timepoint drill-through shows operation, item, user and CU, but **not the query text** or engine metrics (CPU ms, rows). |
| **Job failures** | Shows throttling/rejection, not *why* a pipeline or notebook failed, and no failure routing. |
| **Real time** | Data appears with minutes of delay. No push/streaming. |

## 2. Options and which gaps they close

| | (i) Capacity Metrics app | (ii) RTI: capacity events + workspace monitoring | (iii) Power BI: APIs + Cost Management (+ Semantic Link) |
|---|---|---|---|
| What we built | Not installed in this lab (AppSource install is portal-only; checklist) | `cap_es_capacity` (Real-Time Hub *Fabric capacity overview events*) → `cap_monitoring` KQL DB (raw → silver update policies → gold functions) → `cap_rtd_capacity` (10 tiles) → `act_capacity_70`; workspace monitoring `mon_finance` (+ `mon_fleet`, `mon_asset` Linked into one Eventhouse) | `infra/capacity_report.py`: Fabric + Admin REST (capacities, workspaces, items, job instances), admin activity events (users hashed), Azure Cost Management query, hourly aggregates from (ii) → `cap_lh_capacity` → `cap_sm_capacity` (Direct Lake, 17 measures) → `cap_rpt_capacity` |
| Freshness | Minutes | **30 s** (Summary events), state changes on transition | Batch (scheduled script; cost data ~1 day lag) |
| Retention | 14 / 30 days | Configurable (raw 30 d, silver 400 d) | Unlimited (Delta) |
| Alerting | ✗ | ✅ Activator on **upward crossings** of 70 % (one email per incident) | Power BI data alerts / Activator on the report (not built) |
| Customization / export | ✗ (locked model) | ✅ KQL | ✅ Full model/report |
| Cost (money) | ✗ | ✗ | ✅ Daily Azure actual cost by resource/meter |
| Owner chargeback | Partial (CU by item/user, manual) | Per **workload** only (no item in the events) | **Proxy** (job hours); true per-item CU needs the Metrics app model → use **FUAM** |
| Query-level root cause | Operation-level | ✅ Workspace monitoring: `WarehouseExecutions` (CPU ms, duration, status), `SemanticModelLogs` (DAX), Eventhouse query/command logs | ✗ |
| Job failures | ✗ | ✅ `ItemJobEventLogs` (Step 7 routes them) | ✅ Job instances API (status, failure reason) |
| Throttling stages | ✅ (Overages page) | ✅ `interactive_delay_pct` / `interactive_rejection_pct` / `background_rejection_pct` + overage CU-s | Via (ii) aggregates |
| Preview? | GA | **Preview** (capacity events, workspace monitoring) | GA APIs; Semantic Link on the app = unsupported |
| Licensing / roles | Capacity admin; Pro to view | Capacity admin (events source), Contributor; tenant settings *Workspace monitoring* + *Allow monitoring data collection* | Fabric admin (Admin APIs, activity events), **Cost Management Reader** on the subscription/RG |
| Own CU cost | None (app runs on shared/Pro) | Eventstream + Eventhouse uptime (co-hosted in `eh_fleet` to avoid another always-on Eventhouse); monitoring Eventhouse | Lakehouse + Direct Lake (small); collector runs outside Fabric |

## 3. Load test on F2 (`monitoring/loadgen.py`)

4 SQL-endpoint self-join workers plus 2 DAX crossjoin workers for 20 minutes. Eventhouse is billed by uptime, so KQL load barely moves CU; SQL and DAX are interactive and smoothed over 5 min.

| Time (UTC) | Billed utilization | Interactive delay stage | Overage | Observation |
|---|---|---|---|---|
| 07:41 | 44 % | 27 % | 0 | Load starts |
| 07:42 | 205 % | **101 %** | 16 CU-s | **Interactive delay** reached in under 2 minutes. State event *Overloaded / InteractiveDelay*. `act_capacity_70` crossing at 07:42 (one email) |
| 07:46 | 285 % | 112 % | 673 CU-s | Peak |
| 07:48–07:54 | ~76–81 % | 100–108 % | ~900 CU-s | Each request now waits 20 s, so throughput (and billed CU) drops: **throttling as back-pressure**. No errors; the rejection stage peaked at 42 % |
| 07:55–08:01 | 108–159 % | 129–137 % | 1,076 CU-s | Workers adapt. Background-rejection stage reaches 24 % |
| 08:03 | — | — | — | **Resize F2 → F4** (`infra/capacity.py resize --sku F4`) |
| 08:04 | 33 % | 56 % | 988 CU-s | Delay cleared within about 1 minute (limits doubled) |
| 08:09 | 12 % | 12 % | **0** | Overage burned down in about 6 minutes. Resized back to F2 |

Earlier in Step 5, an accidental overload (OneLake backfill exports, live stream, agents) got both Eventhouses rejected with HTTP 429 for more than 15 minutes. **Pause → resume** cleared it immediately; pausing settles the carry-forward, which is then billed.

Workspace monitoring attributed the load: 270 SQL `SELECT`s on `lh_asset` with 17,902 CPU-s (avg 17 s), and 231 DAX queries on `sm_finance` with 209 CPU-s. That is the query-level root cause the Metrics app doesn't show.

**Surge protection** (Admin portal → Capacity settings → Surge protection; no public API, so it's a checklist item) only rejects **new background** operations based on the 24 h background %. In this test the overload was interactive, so surge protection wouldn't have helped. It protects interactive users from runaway background jobs (Spark, pipelines, refreshes). To demonstrate it:
1. Set rejection at 50 % and recovery at 40 %.
2. Start `nb_finance_medallion` several times in a row.
3. Watch `background_rejection_pct` and the job error `CapacityLimitExceeded`.

## 4. Existing accelerator: FUAM (microsoft/fabric-toolbox)

FUAM (Fabric Unified Admin Monitoring) collects:
- tenant settings, activities, workspaces/items (Scanner API), refreshables and Git connections;
- **Capacity Metrics app data** (per item/operation CU, the only source for true chargeback), into a lakehouse with Direct Lake reports.

It is deployed by notebook and needs a service principal, a Fabric admin, the Capacity Metrics app (supported versions only) and XMLA read on its workspace. It's a community accelerator, **not a supported product**, and can break on Metrics app updates.

**Decision (D-029)**: don't rebuild FUAM. Options (ii) and (iii) cover real-time alerting, query root cause, cost and job failures. Add FUAM when per-item CU chargeback is required (on a larger SKU; its pipelines add load that F2 can't afford alongside this lab).

## 5. Recommendation

- **Operations (minutes matter):** (ii), capacity events → Eventhouse → RTD + Activator on threshold **crossings**, plus workspace monitoring for "which query/job". This is the only option with real-time alerting and query-level root cause.
- **Finance / FinOps (days matter):** (iii), cost from Azure Cost Management joined with inventory and job data in a lakehouse and Power BI. Use FUAM (Metrics app extraction) for true per-workspace CU chargeback.
- **Keep (i)** for ad-hoc drill-down and Microsoft-supported troubleshooting (support cases reference it).
- **On small SKUs:** host monitoring once per capacity (not per environment). Co-host the Eventhouse database, and use Linked mode for workspace monitoring.
