# Observability: failure injection -> detection -> alert routing (Step 7)

Sample, not production guidance. Dev workspaces, F2. Scripts: `infra/observability.py`, `infra/workspace_monitoring.py`.

## Failure injection

`pl_finance_daily` (ce-finance) runs `nb_finance_medallion`. Its pipeline parameter `failure_mode` selects the failure:

| Mode | What fails | Realism |
|---|---|---|
| `none` | Nothing (normal daily run) | Baseline |
| `fail_activity` | Fail activity (`CE-OBS-001`, "upstream partner file not delivered") before Spark starts | Fast, no Spark CU; tests routing |
| `notebook_missing_file` | Notebook parameter `inject_failure=missing_file`, so Spark reads a non-existent landing path | A real Spark error (path not found), as with a late partner drop |

Run: `py infra/observability.py inject --mode fail_activity|notebook_missing_file`. The script runs the pipeline, then measures when each detection path first sees the terminal status.

## Detection paths (measured)

| Path | Latency after job end | What you get | Alerting | Notes |
|---|---|---|---|---|
| **Job Scheduler API** (`jobs/instances`, polled; also collected by `capacity_report.py`) | Polling interval | Status plus **failure reason with the message** (e.g. `CE-OBS-001 ... partner file not delivered`; notebook: error 2451 + link to the run logs) | Script only | Richest error text; needs a poller |
| **Real-Time Hub Fabric job events** (`cap_es_jobs` → `cap_monitoring.job_events`, `JobFailures()`) | **3–4 s** | Created / StatusChanged / Failed / Succeeded with item, kind, job type, duration. `errorMessage` was **empty** in both tests | Push: Activator can subscribe to the eventstream or the table | Item-scoped sources (one per pipeline/notebook); preview |
| **Workspace monitoring** `ItemJobEventLogs` | **49 s** (pipeline), **78 s** (notebook) | Status per job, item kind, invoke type, duration. No error text | `act_job_failures` (KQL every 5 min, so an email ~2–7 min after failure) | Covers *every* item in the workspace without per-item setup |
| Workspace monitoring `FabricDataPipelinesActivityRunsLogs` | 75–90 s | **Failing activity**, `ErrorCode` (`ActionFailed`, `2451`), `FailureType` (`UserError`), CPU core ms. `ErrorMessage` empty | Same | Activity-level root cause |
| **Monitor hub** (portal) | Seconds | Full error and Spark/notebook run logs | None | Manual triage |

## Alert routing (`act_job_failures`, script-managed, not in Git)

| Route | Source filter (ItemJobEventLogs, `JobStatus == 'Failed'`) | Recipient |
|---|---|---|
| `dataeng` | `ItemKind in ('Pipeline', 'SynapseNotebook', 'SparkJobDefinition', 'CopyJob', 'Dataflow', ...)` | `ALERT_EMAIL_DATAENG` (default: admin) |
| `bi` | `ItemKind in ('SemanticModel', 'Dataset')` (refresh failures) | `ALERT_EMAIL_BI` (default: admin) |

Item kinds as logged: pipelines are `Pipeline` and notebooks are `SynapseNotebook` (not `DataPipeline` / `Notebook`). A filter built from REST item types silently misses notebooks; this was found during the test.

## Recommendation

- **Breadth:** use **workspace monitoring** + Activator as the default safety net, because it covers every item with zero per-item setup at about 1 minute plus the polling interval.
- **Critical pipelines:** add **job events** (push, seconds) and route them to Activator / Teams.
- **Root cause:** enrich the alert from the **job API** (failure reason) and `FabricDataPipelinesActivityRunsLogs` (failing activity). Neither the events nor `ItemJobEventLogs` carry the error text.
- **Monitor hub** remains the place to read Spark logs.
