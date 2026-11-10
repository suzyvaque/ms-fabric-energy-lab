# Step 1b: Ingestion comparison

**Question.** Should Contoso Energy fleet telemetry reach Fabric through **(i) Azure Event Hubs → Eventstream** or **(ii) an Eventstream custom endpoint**?

**Setup (reproducible).**
- **Infrastructure:** `infra/eventhubs.py create` and `infra/rti_lab.py create`.
- **Benchmark:** `data-gen/benchmarks/ingest_compare.py`.
- **Tee sending:** one simulator sends an *identical copy* of every event to both paths (`TeeSink`). Each copy is tagged with `ingest_path`.
- **Landing:** both paths write into the same Eventhouse table (`lab_compare.compare_events`) via *processed ingestion*.
- **Latency:** measured as `ingestion_time() − sent_time`, scored in KQL.

| | (i) Event Hubs → Eventstream | (ii) Eventstream custom endpoint |
|---|---|---|
| Producer auth | Entra ID (`Azure Event Hubs Data Sender`), SAS disabled by policy | Eventstream SAS key (Entra ID available, preview) |
| Hop | simulator → Event Hubs Standard (1 TU, 4 partitions, 24 h) → Eventstream (`AzureEventHubExtended` source, **workspace identity**, preview) → Eventhouse | simulator → Eventstream (custom endpoint) → Eventhouse |
| Eventstream throughput level | Low | Low |

Test client: a laptop on a corporate VPN, AMQP over WebSockets (443), 8 concurrent senders per path, same process for both paths. Absolute latencies include this client's network; compare the paths relative to each other.

## Measured results

| Load (target events/s) | Path | Achieved events/s | Distinct events delivered | Loss | Extra duplicates (beyond the 0.5 % injected) | p50 latency (s) | p95 latency (s) | p99 latency (s) |
|---|---|---|---|---|---|---|---|---|
| 39 | (i) Event Hubs | 37.9 | 9,298 / 9,298 | 0 | 0 | 1.97 | 3.08 | 8.53 |
| 39 | (ii) Custom endpoint | 37.9 | 9,298 / 9,298 | 0 | 0 | 1.64 | 5.47 | 13.22 |
| 390 | (i) Event Hubs | 374 | 91,418 / 91,418 | 0 | 0 | 1.61 | 3.84 | 9.19 |
| 390 | (ii) Custom endpoint | 374 | 91,418 / 91,418 | 0 | 0 | 1.75 | 8.27 | 15.65 |
| 1,170 (client-capped ≈ 900) | (i) Event Hubs | 890 | 217,214 / 222,536 | **2.4 %**: throttled, error `50002` (1 TU) | ~2,900 (SDK retries) | 3.77 | 8.36 | 14.65 |
| 1,170 (client-capped ≈ 900) | (ii) Custom endpoint | 912 | 222,536 / 222,536 | 0 | 0 | **8.66** | 17.79 | 18.93 |

- **Schema v2 drift.** Renamed fields and units in W arrived intact on both paths: `measurements` is kept as `dynamic` and columns are mapped by name.
- **Malformed payloads.** 20 non-JSON payloads were sent per path, and 0 rows landed on either path. They are dropped silently and are visible only in Eventstream runtime logs and data insights.
- **Below the 1-TU ceiling,** the extra Event Hubs hop doesn't add median latency (~2 s p50 on both paths), and its tail was *lower* than the custom endpoint's on the *Low* throughput level.
- **Above ~1,000 events/s,** Event Hubs (1 TU) throttles. You must size TUs or enable auto-inflate, and make producers idempotent: retries create duplicates. The custom endpoint absorbed the load without errors, but its latency rose. Raise the eventstream throughput level to *Medium* before production.

## Dimension-by-dimension

| Dimension | (i) Event Hubs → Eventstream | (ii) Custom endpoint | Edge |
|---|---|---|---|
| **Latency** | p50 ≈ 1.6–2.0 s; tighter p95 at low/medium load | p50 ≈ 1.6–1.8 s; wider tail on *Low* throughput level | Tie (tune ES throughput level) |
| **Throughput** | Bound by TUs: 1 TU = 1 MB/s or 1,000 events/s ingress. Auto-inflate up to 40 TU; Premium/Dedicated beyond that | Bound by eventstream throughput level (Low/Medium/High); no Azure sizing | (ii) for simplicity; (i) for very high, predictable scale |
| **Transformations / routing** | Same Eventstream operators once inside Fabric. **Plus** fan-out to non-Fabric consumers via consumer groups (Functions, Stream Analytics, ADX), and Event Hubs Capture | Eventstream operators, content-based routing to Eventhouse, Lakehouse, Activator and derived streams; consumers must be in Fabric or on Real-Time hub | (i) if consumers outside Fabric |
| **Schema handling** | JSON pass-through; Event Hubs keeps raw bytes for 24 h, so bad batches can be reprocessed | JSON pass-through; malformed events dropped. Event Schema Sets (preview) for validation | (i) slightly (raw replay) |
| **Retention / replay** | 1–7 days (Standard), 90 days (Premium); per-consumer-group offsets; replay from `Earliest` | Eventstream retention 1–90 days (`retentionTimeInDays`, billed as data traffic) | (i) for independent replay per consumer |
| **Networking / private access** | Private endpoints, IP firewall, public access off, Kafka endpoint. Fabric pulling from a private Event Hubs namespace needs Eventstream managed private endpoints / streaming connector VNet (**preview**) | Public Fabric endpoint; inbound restriction via Fabric Private Link (tenant-level; workspace-level **preview**) | (i) for end-to-end private |
| **Auth** | Entra RBAC on Event Hubs. The **classic** Eventstream Event Hubs source accepts **SAS only**. With SAS disabled by policy (as here), only the **Extended source + workspace identity (preview)** works | Fabric-managed SAS key per eventstream, or Entra ID (preview) | (ii) works out of the box; (i) depends on a preview connector when SAS is off |
| **Ops effort** | Extra Azure resource: RG, RBAC, TU sizing/auto-inflate, Azure Monitor alerts, policy compliance, separate cost line | Nothing to run. Rotate the key by regenerating it; monitor in Fabric (data insights, workspace monitoring) | (ii) |
| **Cost** | See model below: Event Hubs TU + ingress, **and** the Eventstream processor is always on (Event Hubs is pulled through the processor) | Eventstream flat + traffic only, if Eventhouse uses **direct ingestion** (no processor) | (ii) |

### Cost model (list prices, US, pay-as-you-go; verify for your region)
Steady **400 events/s × 0.42 KB** ≈ 1.05 B events and ≈ 440 GB per month. Fabric CU-hours are priced at the F-SKU PAYG rate of ≈ $0.18 per CU-hour.

| Meter | (i) Event Hubs → Eventstream | (ii) Custom endpoint → direct ingestion | (ii') Custom endpoint → processed ingestion |
|---|---|---|---|
| Event Hubs Standard, 1 TU ($0.03/TU-h) | ≈ $22 | n/a | n/a |
| Event Hubs ingress ($0.028 per M events) | ≈ $29 | n/a | n/a |
| Eventstream per hour (0.222 CU-h/h) | ≈ $29 | ≈ $29 | ≈ $29 |
| Eventstream processor (≥ 0.778 CU-h/h, autoscales) | ≈ $102 (always: Event Hubs is pulled by the processor) | $0 | ≈ $102 |
| Eventstream data traffic (0.342 CU-h/GB) | ≈ $27 | ≈ $27 | ≈ $27 |
| **Total per month (excluding Eventhouse, the same for both)** | **≈ $209** | **≈ $56** | **≈ $158** |

**Capacity impact on a small SKU.** An eventstream with a processor runs at ≈ 1.0 CU continuously, which is ~50 % of an F2 before Eventhouse. Two running at once (as in this lab) can push an F2 toward throttling. Pause the lab streams when not in use. Measured CU per item gets captured in step 6 (Capacity Metrics app); fill in the checklist below.

## Recommendation (D-009)
**Use (ii) the Eventstream custom endpoint for Contoso fleet telemetry.**
- Land it in the Eventhouse **bronze table with direct ingestion**, and do transformations in the Eventhouse with update policies and materialized views (step 1c) instead of Eventstream operators.
- It has one hop and no Azure resource to run, it doesn't depend on SAS policy or a preview connector, and it costs about a quarter of (i).
- Set the eventstream throughput level to **Medium** once sustained load exceeds ~500 events/s, and switch producers to Entra ID auth when that leaves preview.

**Choose (i) Event Hubs** when any of these apply:
- consumers outside Fabric need the same stream;
- you need replay or retention beyond Eventstream, or Capture;
- you need end-to-end private networking;
- there's an existing Event Hubs/Kafka estate;
- you have very high, predictable throughput that you want to size explicitly.

If you choose (i): plan TUs or auto-inflate for peak load, and make producers idempotent (`event_id`) because throttling retries create duplicates. Deduplicate in silver (step 1c) either way.

## Flags
- **Preview:**
  - the `AzureEventHubExtended` source with workspace identity;
  - custom endpoint Entra ID auth;
  - Event Schema Sets;
  - Eventstream private networking;
  - workspace-level Private Link.
- **Licensing / capacity:**
  - Eventstream and Eventhouse run on paid F SKUs and on the Fabric trial.
  - Event Hubs needs an Azure subscription; it isn't covered by a Fabric trial.
  - Workspace identity needs an F SKU (not a trial).
- **Gotchas hit while building** (now handled in the scripts):
  - Event Hubs SAS forced off by policy, which rules out the classic source.
  - Workspace identity provisioning failed once; deprovision then re-provision fixed it.
  - KQL `DatabaseSchema.kql` accepts only an allow-list of commands.
  - A failed KQL DB creation left an orphan database with the same name. The duplicate name made the Eventstream destination **fail silently**; renaming the item fixed it.
  - AMQP port 5671 is blocked on VPN; use WebSockets (443).

## Checklist (portal, not scriptable)
- [ ] Capacity Metrics app → item `lab_es_eventhubs` vs `lab_es_custom` CU (Eventstream meters) for the benchmark hour; add actual values here.
- [ ] Eventstream → *Data insights* / *Runtime logs*: confirm the 20 malformed events per path appear as deserialization errors.
