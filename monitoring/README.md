# monitoring

Capacity and workspace monitoring (steps 6, 7). Sample, not production guidance.

| File | What |
|---|---|
| [capacity-comparison.md](capacity-comparison.md) | Metrics app gaps, options (i)/(ii)/(iii), F2 load-test results, FUAM assessment, recommendation |
| [loadgen.py](loadgen.py) | Interactive load (SQL endpoint + DAX) to reproduce throttling; `watch`; `recover` (pause/resume) |

Scripts that build the monitoring items live in `infra/`:

| Script | Builds |
|---|---|
| `capacity_monitoring.py` | (ii) `cap_monitoring` KQL DB, `cap_es_capacity`, `cap_rtd_capacity`, `act_capacity_70` |
| `workspace_monitoring.py` | Workspace monitoring `mon_finance` (Auto Eventhouse) + `mon_fleet` / `mon_asset` (Linked) |
| `capacity_report.py` | (iii) `cap_lh_capacity`, `cap_sm_capacity`, `cap_rpt_capacity` |

Run: `py infra/capacity_monitoring.py all` · `py infra/workspace_monitoring.py create` · `py infra/capacity_report.py all`
· `py monitoring/loadgen.py run --minutes 20` (then `watch` / `recover`).
