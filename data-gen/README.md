# data-gen: Contoso Energy synthetic data

Deterministic, dependency-free (stdlib) generator for the fictional Contoso Energy fleet. **Synthetic data only.** Emails use `example.com`, phones use the fictional `555-01xx` range, and regions are generic.

```powershell
cd data-gen
py -m pip install -r requirements.txt          # only needed for Event Hubs sink / parquet / tests
py -m contoso_gen batch --sites 200 --days 7    # history -> data-gen/out (gitignored)
py -m contoso_gen stream --sites 200 --interval-sec 10                       # live to stdout
# Event Hubs with Entra ID (az login, "Azure Event Hubs Data Sender"); Eventstream custom endpoint key from .env
py -m contoso_gen stream --sink eventhub --target 'eventhubs=entra:$EVENTHUB_NAMESPACE/fleet-telemetry'
py -m contoso_gen stream --sink eventhub --target 'es-custom=conn:ESTREAM_CONNECTION_STRING'
# tee identical events to both (step 1b); --senders N concurrent clients; --transport websocket (443, default) | amqp
py -m contoso_gen stream --sink eventhub --target 'eventhubs=entra:$EVENTHUB_NAMESPACE/fleet-telemetry' --target 'es-custom=conn:ESTREAM_CONNECTION_STRING'
py benchmarks/ingest_compare.py                                                  # staged A/B benchmark + KQL scoring
py -m contoso_gen stream --inject INVERTER_FAULT:SITE-000001 --inject-minutes 10    # demo trigger
py -m pytest -q
```
The same `--seed` and `--sites` always produce the same master data and IDs (anchored to a fixed date). Batch history, live streams and lakehouse tables therefore join cleanly.
Throughput ≈ devices / `--interval-sec` (≈3.9 devices per site): `--sites 2000 --interval-sec 5` ≈ 1,500 events/s. From a laptop, sending is round-trip-time-bound, so use `--senders 8`. Behind a VPN, plain AMQP (5671) may be blocked; WebSockets (default) works.

## Outputs (`batch`)
| Path | Domain | Rows (200 sites, 7 days) | Notes |
|---|---|---|---|
| `asset/sites.csv` | Asset | 200 | region, lat/lon, PV kW, battery/EV flags, install date |
| `asset/devices.csv` | Asset | ~780 | gateway, inverter, meter, battery; firmware 1.x / 2.x |
| `asset/batteries.csv` | Asset | ~170 | capacity, chemistry, SoH, warranty |
| `finance/customers.csv` | Finance | ~195 | **PII** (name, email, phone, address), credit band |
| `finance/contracts.csv` | Finance | ~170 | Loan / Lease / PPA, APR, term, status |
| `finance/payments.csv` | Finance | ~7k | OnTime / Late / Partial / Missed / Overdue; duplicates; late-arriving `recorded_at` |
| `telemetry/date=…/hour=….jsonl` | Fleet | ~1.5M | partitioned by `sent_time` (arrival), like a landing zone |
| `alerts/date=….jsonl` | Fleet | ~300 | raised / cleared events |
| `ground_truth/anomalies.csv`, `ground_truth/alerts.csv` | n/a | | what was injected. Use it to score detections (steps 2, 7); never load it into the demo |
| `summary.json` | n/a | | counts, duplicates, late events, schema v2 share, anomalies |

## Event contract (Fleet stream)
One JSON object per event. `record_type` is also set as an Event Hubs application property for routing.

**Telemetry** (`record_type = "telemetry"`)
| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | 1, or 2 for firmware 2.x sites (~10 %) |
| `event_id` | guid | duplicates re-use it (retries) |
| `device_id`, `site_id`, `device_type` | string | `gateway`/`inverter`/`meter`/`battery` |
| `event_time` | ISO-8601 UTC | device clock; a few devices are skewed by ±2–90 min |
| `sent_time` | ISO-8601 UTC | when the event left the device or gateway |
| `seq` | int | per-device counter; gaps mean lost data (offline without backfill) |
| `firmware`, `status` (`OK`/`WARN`/`FAULT`), `fault_code` | string | |
| `measurements` | object | see below |
| `ingest_path` | string | optional, set by `--path-label` (step 1b) |

| device_type | schema v1 | schema v2 (drift) |
|---|---|---|
| inverter | `pv_kw`, `ac_kw`, `inv_temp_c`, `efficiency` | `dc_power_w`, `ac_power_w`, `inverter_temp_c`, `efficiency` |
| battery | `soc_pct`, `batt_kw` (+charge/−discharge), `temp_c`, `soh_pct` | `soc_pct`, `battery_power_w`, `temperature_c`, `soh_pct` |
| meter | `grid_kw` (+import/−export), `load_kw`, `voltage_v`, `freq_hz` | same |
| gateway | `rssi_dbm`, `uptime_s`, `cpu_pct` (every 15 min in batch, every tick live) | same |

**Alert** (`record_type = "alert"`): `alert_id`, `event_type` (`raised`/`cleared`), `device_id`, `site_id`, `alert_code`, `severity`, `source` (`device`/`cloud`), `message`, `event_time`, `sent_time`.

## Injected behaviour
| Kind | Signature in data | Device alert? |
|---|---|---|
| `INVERTER_FAULT` | inverter `status=FAULT`, `fault_code=E-301`, PV = 0 in daylight | `INV_FAULT` (90 % of faults) |
| `BATTERY_OVERTEMP` | temperature ramps to 58–72 °C, WARN > 50, FAULT > 60, power derated | `BATT_OVERTEMP`, 20 % flapping |
| `SOC_STUCK` | `soc_pct` constant while battery power ≠ 0 | none: detect with analytics |
| `DEVICE_OFFLINE` | whole site silent; 50 % backfilled late at reconnect (store-and-forward) | `COMM_LOSS` from cloud after 15 min |
| `METER_SPIKE` | single `grid_kw` outlier (±12× + 45 kW) | none |
| `PV_DEGRADATION` | PV output falls by up to 40 % over days | none |
| `CLOCK_SKEW` | `event_time` offset ±2–90 min for a device | none |
| Late events | `sent_time − event_time` minutes to hours (~1 %) | n/a |
| Duplicates | same `event_id` re-sent within 2 min (~0.5 %) | alerts ~1 % |
| False alerts | `GRID_VOLTAGE`, `FAN_WARN`, `TEMP_SENSOR` with no anomaly | yes |
