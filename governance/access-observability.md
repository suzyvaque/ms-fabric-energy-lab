# Access observability: who reaches the data, and which log sees it (Step 10)

Sample, not production guidance. Synthetic data. All tests ran in an **isolated observability lab** on a separate F64 capacity
(`infra/obs_lab.py`, D-041), so nothing here touched the shared dev/prod capacity or the evaluation sessions.

Raw per-probe results (generated, identities as labels only): [plan/access-observability-results-obs.md](../plan/access-observability-results-obs.md).

## Setup

| Workspace (obs capacity) | Contents | Who has access |
|---|---|---|
| `ce-obs-data` | `lh_obs_finance`: finance gold + `silver_customers` (PII), loaded by Load-to-Table; OneLake role `FinanceViewerNoPII` (gold only, CLS hides name/email/phone, RLS `site_region = 'Coastal'`, **DefaultReader removed**); data agent `da_obs_finance` (PII columns deselected) | Admins (Admin), test user (Viewer **+ role member**), deploy SP (Viewer, **not** a role member = negative control) |
| `ce-obs-consumer` | `lh_obs_hub` with **cross-workspace shortcuts** to `lh_obs_finance`; Spark notebook `nb_obs_reader` | Same as data |
| `ce-obs-audit` | OneLake diagnostics destination `lh_obs_diag`; workspace monitoring Eventhouse (Auto, others Linked) | Admins only (logs contain user identifiers) |

Probes (`infra/access_matrix.py run --profile obs`) are serialized 20 s apart and tagged with a unique ID: DFS list, Blob read,
SQL (allowed table, PII column, out-of-role table, RLS table), SQL through the shortcut, data agent (allowed + PII question),
and a Spark notebook (admin). The deploy SP runs the same probes in GitHub Actions (OIDC, no secret).
`access_matrix.py correlate --profile obs` matches every probe to each log source within -5 s / +15 s of the operation time.

## What each identity could do (enforcement)

| Probe | Admin | Test user (Viewer + role) | Deploy SP (Viewer, no role) |
|---|---|---|---|
| OneLake: list/read gold | allowed | allowed | **403** |
| OneLake: CLS table (`gold_dim_customer`) | allowed | listing shows **0 files** (direct file access blocked) | **403** |
| OneLake: out-of-role table (`silver_customers`) | allowed | **403** | **403** |
| OneLake: through the shortcut | allowed | 0 files (CLS source) | **403** |
| SQL: RLS table | all regions | **all regions** | **all regions** |
| SQL: PII column / out-of-role table | allowed | **allowed** | **allowed** |
| SQL through the shortcut (PII) | allowed | **allowed** | **allowed** |
| Data agent: collection rate | answered | answered | answered |
| Data agent: "list emails" | refused (agent policy) | refused | refused |

**The SQL analytics endpoint of a brand-new lakehouse is also in delegated identity mode.** OneLake security is enforced for
OneLake API/Spark access, but every SQL path (and therefore every data agent, Direct Lake model and GraphQL API on it)
serves the full table, including PII, to **any workspace Viewer**, even one with no OneLake role. Only the agent's own
instructions/column selection kept PII out of its answers. Switching to user identity mode is portal-only (D-032, D-042).

## Which log source saw it (observability)

| Activity | OneLake diagnostics | Workspace monitoring | Activity events = Purview Audit (Fabric) | SQL audit |
|---|---|---|---|---|
| OneLake list/read, **allowed or denied (403)** | *nothing delivered* (see blocker) | - | - | n/a |
| SQL on `lh_obs_finance` (direct) | - | **not captured** (0 `WarehouseExecutions` in the data workspace) | occasional `ConnectWarehouseAndSqlAnalyticsEndpoint`, caller visible | *0 events* (enabled, control event not found) |
| SQL through the shortcut (`lh_obs_hub`) | - | `WarehouseExecutions`, **caller visible** (admin, test user, **SP**) | connect event (users only) | 0 |
| Data agent question | - | - | `DataAgentFabricQueried` / `QueryAISkillDeploymentEndpoint`, **the human behind the agent** (admin, test user) | 0 |
| Spark notebook (workload) | - | `ItemJobEventLogs` `RunNotebook`, caller visible | `RunArtifact` / `ReadArtifact`, caller visible | n/a |
| Anything by the **service principal** | - | shortcut SQL only | **nothing** | 0 |
| Denials | - | - | - | - |

Coverage per source in the probe windows: workspace monitoring 10 events (admin 6, test user 2, SP 2); activity events
184 in the obs workspaces (admin, test user; no SP); OneLake diagnostics 0; SQL audit 0.

## Comparison with Purview

| Question | OneLake diagnostics | Purview Audit (unified audit log) | Workspace monitoring | Purview DSPM for AI |
|---|---|---|---|---|
| Who read which **files/tables** at the storage layer (incl. external tools, ADLS/Blob APIs, shortcuts) | **Designed for this** (every UI/ADLS/Blob operation; Fabric engines log a "temporary access granted" event; shortcut reads logged in the source workspace) | No (item-level operations only) | No | No |
| **Denied** access | Yes (HTTP status per operation), by design | No | No | No |
| User **behind a data agent** | Engine token grant, not the question | **Yes** (`DataAgentFabricQueried`, caller = human) | No (agent SQL not captured here) | Prompts/responses (E5-class licensing) |
| Service principals / workloads | Yes, by design | **Not for the SP in this test** | Yes (SQL through shortcut, job runs) | No |
| Query text / CPU | No | No | Yes (SQL, DAX, KQL) | No |
| Retention / tamper-proofing | You own it (lakehouse JSON; optional immutability/WORM) | 180 days Standard, longer with Audit Premium | Eventhouse retention policy | Purview policy |
| Licensing | Fabric capacity | M365 E3 (Standard) / E5 (Premium) | Fabric capacity | E5-class |

## Blocker: OneLake diagnostics delivered no events

- Enabled via REST on 3 dev workspaces (cross-workspace destination) and again on the fresh obs capacity into an admin-only
  workspace. In both cases settings read back *Enabled*, the per-workspace folders
  `Files/DiagnosticLogs/OneLake/Workspaces/<id>/` were created, and the tenant setting *Include end-user identifiers in OneLake
  diagnostic logs* is on.
- **Zero log files** after ~15 h (dev), ~11 h (dev, same-workspace destination) and ~2 h on the fresh obs capacity, despite DFS
  list/read, Blob read, SQL-through-shortcut and Spark access. The documented delay is up to one hour.
- Not explained by permissions (workspace admin on source, admin of destination), capacity (same capacity, verified),
  networking (no private links) or SKU (F2/F32/F128/F64 all tried). No known issue is listed.
- Next: toggle it in the portal once (rules out an API-only enablement gap); if still empty after an hour, open a support
  ticket with the workspace IDs and enable timestamps (in `infra/.state/obs.json`).

## Takeaways

1. **OneLake security protects OneLake, not SQL in delegated mode.** Until the SQL endpoints are switched to user identity
   mode, a Viewer reads everything through SQL, agents, Direct Lake and GraphQL. Either switch the mode (portal) or keep T-SQL
   DENYs in sync (D-032), and don't give Viewer to people who shouldn't see the data.
2. **No single log answers "who touched this data".** Purview Audit shows the human behind an agent but not storage reads,
   denials or SP activity; workspace monitoring shows SQL callers including SPs but missed direct SQL on the data workspace;
   OneLake diagnostics is the only source designed for storage-level reads and denials, and it didn't deliver here.
3. **Agents launder identity at the data layer.** The question is logged with the human caller (activity events), but the
   SQL runs as the endpoint's delegated identity, so storage-side logs can't attribute it. Keep agent sources on
   user-identity endpoints and correlate by time.
