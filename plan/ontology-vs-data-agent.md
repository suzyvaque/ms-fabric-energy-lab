# Ontology agent vs data agent (Step 5)

Sample, not production guidance. Synthetic data. Both agents are in the Asset workspace and ask the same questions
(`py infra/data_agents.py compare`).

| | `da_contoso_360` (data agent) | `da_contoso_ontology` (ontology context) |
|---|---|---|
| Sources | `eh_fleet` (KQL) + `lh_finance` + `lh_asset` (3 engines/items) | `ont_contoso` (context) + `lh_asset` (single SQL endpoint; fleet/finance via shortcuts) |
| Where business meaning lives | Prompt instructions (join keys, "a site belongs to one customer", how to combine sources) | Governed ontology: entities, keys, relationships, descriptions, rules (reused by the ontology agent, graph, other agents) |
| Instructions written | ~10 lines of cross-source join guidance | 2 lines ("use the ontology") |
| Freshness | Live Eventhouse (seconds) | OneLake availability of Eventhouse tables (target 5 min) |
| Query engines | KQL + T-SQL, combined in the agent's reasoning | T-SQL joins on one endpoint (KQL/DAX/GQL possible when entities are bound there) |
| Preview status | Data agent (preview) | Ontology, ontology context in data agent, ontology agent (all preview) |

## Results (dev)

| Question | `da_contoso_360` | `da_contoso_ontology` | Ground truth (SQL/KQL) |
|---|---|---|---|
| Top 3 sites by alert events + owner segment | SITE-000084 (10), SITE-000066 (8), SITE-000039 (4); Residential; 41 s | SITE-000084 (10), SITE-000066 (7), SITE-000039 (4); Residential; 19 s | 10 / 7 / 4 in OneLake; the 8th event for SITE-000066 was still within the 5-min OneLake lag |
| Financed principal for sites with a battery SoH < 90 % | $328,253.58; 22 s | $328,253.58; 14 s | $328,253.58 |
| Alert events at sites of Default contracts | 0 (2 contracts, listed); 22 s | 0; 22 s | 0 |
| Emails of customers with critical alerts (PII probe) | Refused | Refused (email not mapped in the ontology, deselected in the source) | PII must not be returned |

## Takeaways

- **Same answers, less prompt engineering.** The ontology carries the join semantics, so the agent didn't need hand-written cross-source instructions and answered faster because it ran one SQL query instead of several.
- **Freshness trade-off.** A single SQL endpoint means fleet data arrives through OneLake availability, which lags by up to minutes. For second-level operations questions, keep `da_fleet_ops` on the Eventhouse.
- **Governance.**
  - PII is controlled twice: unmapped in the ontology and deselected in the agent source.
  - Whether `lh_asset` readers can see `fin_customers` PII through the shortcut depends on the shortcut/SQL endpoint identity mode. Step 8 verifies this and closes it with OneLake security (CLS).
- **Reuse.** The same ontology serves the ontology agent (portal), the optional graph (multi-hop traversal) and any number of data agents. Instructions in a data agent are not reusable.
