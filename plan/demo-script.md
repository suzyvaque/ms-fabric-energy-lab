# Demo script (generic)

Customer-specific talk tracks are kept outside this repo.

| Min | Scene | Shows | Step |
|---|---|---|---|
| 0–3 | "Contoso Energy" story: solar + battery fleet, consumer financing | Domains, workspaces, OneLake catalog | 0, 8 |
| 3–10 | Live telemetry → Eventhouse medallion → Real-Time dashboard; trigger a device fault | Eventstream, KQL, Activator | 1, 2 |
| 10–15 | Ask the data agent / ontology agent about a failing site and its financing contract | Data agent vs ontology | 3, 5 |
| 15–20 | KPI report on certified gold model | Direct Lake, endorsement, lineage | 4, 8 |
| 20–25 | Push the capacity to throttling; show the alert at 70 % and the root cause | Capacity monitoring options | 6, 7 |
| 25–30 | Sensitivity labels, DLP, PR → prod deploy | Purview, CI/CD | 9, 0 |
