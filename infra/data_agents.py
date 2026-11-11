"""Step 3: Fabric data agents (preview). Idempotent.

  da_fleet_ops     (ce-fleet)  KQL over eh_fleet; prefers the Fleet API functions
  da_finance       (ce-finance) SQL over lh_finance gold tables; PII columns deselected
  da_contoso_360   (ce-asset)  combined: eh_fleet + lh_finance + lh_asset (cross-domain questions)
  da_contoso_ontology (ce-asset) Step 5: ont_contoso (Fabric IQ ontology, context) + lh_asset hub (query execution)

  py infra/data_agents.py create [--env dev]   # build definitions from live schemas, publish
  py infra/data_agents.py ask --agent da_finance --question "..." [--env dev]
  py infra/data_agents.py test [--env dev]     # scripted questions against every agent
  Evaluation (golden set, graders, A/B report): eval/agent_eval.py, which uses ask_run() for answers + executed queries
"""
from __future__ import annotations

import argparse
import json
import time
import uuid

from common import env, load_env, load_state, log
from fabric_rest import FABRIC, b64, fabric, find_item, kql, token, tsql, upsert_item
from rti_lab import platform

PII = {"customer_name", "email", "phone", "first_name", "last_name", "street_address"}
FLEET_TABLES = ["dim_site", "dim_device", "dim_battery", "silver_telemetry", "silver_alerts"]


def ws(domain: str, e: str) -> str:
    name = f"{env('WORKSPACE_PREFIX')}-{domain}-{e}"
    known = load_state("workspaces").get(name)
    if known:
        return known["id"]
    # CI has no infra/.state: resolve by name
    match = next((w for w in fabric("get", "workspaces").get("value", []) if w["displayName"] == name), None)
    if match is None:
        raise SystemExit(f"workspace {name} not found or not accessible")
    return match["id"]


def eid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "contoso-energy-lab/da/" + "/".join(parts)))


# ----------------------------------------------------------------- data sources
def kusto_source(e: str) -> dict:
    w = ws("fleet", e)
    eh = find_item(w, "Eventhouse", "eh_fleet")
    db = find_item(w, "KQLDatabase", "eh_fleet")
    uri = fabric("get", f"workspaces/{w}/eventhouses/{eh['id']}")["properties"]["queryServiceUri"]
    tables = []
    for t in FLEET_TABLES:
        cols = kql(uri, db["id"], f"{t} | getschema | project ColumnName, ColumnType")
        tables.append({"id": eid("kusto", t), "display_name": t, "type": "kusto.table", "is_selected": True,
                       "children": [{"id": eid("kusto", t, c["ColumnName"]), "display_name": c["ColumnName"],
                                     "type": "kusto.column", "data_type": c["ColumnType"], "is_selected": True}
                                    for c in cols]})
    return {"$schema": "1.0.0", "artifactId": db["id"], "workspaceId": w, "displayName": "eh_fleet", "type": "kusto",
            "userDescription": "Contoso Energy fleet telemetry (Eventhouse): device readings, alerts, site/device master",
            "dataSourceInstructions": FLEET_SOURCE_INSTRUCTIONS, "elements": tables}


def lakehouse_source(domain: str, lh_name: str, prefix: str, e: str, description: str, instructions: str,
                     only: set[str] | None = None) -> dict:
    w = ws(domain, e)
    lh = find_item(w, "Lakehouse", lh_name)
    host = fabric("get", f"workspaces/{w}/lakehouses/{lh['id']}")["properties"]["sqlEndpointProperties"]["connectionString"]
    cols: dict[str, list[tuple[str, str]]] = {}
    for t, c, typ in tsql(host, lh_name, "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                                         "WHERE TABLE_SCHEMA = 'dbo' ORDER BY TABLE_NAME, ORDINAL_POSITION"):
        if t.startswith(prefix) and not c.startswith("_") and (only is None or t in only):
            cols.setdefault(t, []).append((c, typ))
    tables = [{"id": eid(lh_name, t), "display_name": t, "type": "lakehouse_tables.table", "is_selected": True,
               "children": [{"id": eid(lh_name, t, c), "display_name": c, "type": "lakehouse_tables.column",
                             "data_type": typ, "is_selected": c not in PII} for c, typ in cs]}
              for t, cs in cols.items()]
    return {"$schema": "1.0.0", "artifactId": lh["id"], "workspaceId": w, "displayName": lh_name, "type": "lakehouse_tables",
            "userDescription": description, "dataSourceInstructions": instructions,
            "elements": [{"id": eid(lh_name, "dbo"), "display_name": "dbo", "type": "lakehouse_tables.schema",
                          "is_selected": True, "children": tables}]}


def ontology_source(e: str) -> dict:
    """Ontology = governed business context only (preview); queries run on its bound source (lh_asset)."""
    w = ws("asset", e)
    o = find_item(w, "Ontology", "ont_contoso")
    return {"$schema": "1.0.0", "artifactId": o["id"], "workspaceId": w, "displayName": "ont_contoso", "type": "ontology",
            "userDescription": "Contoso Energy ontology: Site, Device, Battery, Alert, Contract, Customer", "elements": []}


FLEET_SOURCE_INSTRUCTIONS = """Tables (stored functions and materialized views are NOT visible to this agent; use these tables):
- silver_telemetry: every device reading (device_id, site_id, device_type, status, fault_code, event_time, ingest_time,
  pv_kw, ac_kw, soc_pct, battery_kw, battery_temp_c, grid_kw, load_kw ...). It can contain a few duplicate event_id rows:
  deduplicate with `summarize take_any(*) by event_id` when counting readings.
  Latest state per device: `summarize arg_max(event_time, *) by device_id`.
  A device is ONLINE if its latest ingest_time > ago(20m). A SITE is OFFLINE when its gateway (device_type == 'gateway')
  has no reading with ingest_time in the last 20 minutes.
- silver_alerts: alert events (alert_id, event_type 'raised'/'cleared', severity, alert_code, device_id, site_id, event_time).
  An alert is OPEN when its latest event (arg_max(event_time, *) by alert_id) has event_type 'raised'.
- dim_site / dim_device / dim_battery: master data (join on site_id / device_id).
Energy: average kW over a period times hours. Units: kW, kWh, degrees C, soc_pct percent. grid_kw > 0 import, < 0 export."""

FINANCE_SOURCE_INSTRUCTIONS = """Star schema: gold_fact_payment and gold_fact_contract_month join gold_dim_contract on contract_id;
gold_dim_contract joins gold_dim_customer on customer_id; *_date_key / month_key join gold_dim_date.date_key.
Delinquent = days_past_due >= 30 (is_delinquent). Collection rate = SUM(amount_paid) / SUM(amount_due).
Products: Loan, Lease, PPA. Contract status: Active, Delinquent, Default, PaidOff. Amounts are USD.
Never return customer names, emails or phone numbers; identify customers by customer_id."""

ASSET_SOURCE_INSTRUCTIONS = """Asset master: sites (site_id, customer_id, region, pv_capacity_kw, has_battery, install_date),
devices (device_id, site_id, device_type, model, firmware), batteries (device_id, capacity_kwh, chemistry, state_of_health_pct).
Columns may be stored as text; cast when comparing numbers."""

AGENTS = {
    "da_fleet_ops": dict(domain="fleet", description="Fleet operations assistant (real-time telemetry and alerts)",
        instructions="""You are the Contoso Energy fleet operations assistant. Answer questions about the residential solar and
battery fleet: device status, offline sites, alerts, energy production and battery health. Data is synthetic.
Always state the time window you used. Round kW/kWh to 1 decimal.""",
        fewshots={"kusto": [
            ("How many devices are online right now?",
             "silver_telemetry | where ingest_time > ago(1d) | summarize last_ingest = max(ingest_time) by device_id "
             "| summarize devices = count(), devices_online = countif(last_ingest > ago(20m))"),
            ("Which sites are offline?",
             "silver_telemetry | where device_type == 'gateway' and ingest_time > ago(7d) | summarize last_seen = max(ingest_time) by site_id "
             "| where last_seen < ago(20m) | extend minutes_offline = datetime_diff('minute', now(), last_seen) "
             "| lookup kind=leftouter (dim_site | project site_id, region) on site_id | order by minutes_offline desc"),
            ("Which open alerts are critical?",
             "silver_alerts | summarize arg_max(event_time, *) by alert_id "
             "| where event_type == 'raised' and severity == 'Critical' | project event_time, site_id, device_id, alert_code, message"),
            ("Top 5 sites by energy produced in the last 7 days",
             "silver_telemetry | where device_type == 'inverter' and event_time > ago(7d) | summarize take_any(*) by event_id "
             "| summarize avg_kw = avg(pv_kw) by site_id, bin(event_time, 15m) | summarize pv_kwh = round(sum(avg_kw) * 0.25, 1) by site_id "
             "| lookup kind=leftouter (dim_site | project site_id, region) on site_id | top 5 by pv_kwh"),
        ]}),
    "da_finance": dict(domain="finance", description="Consumer financing assistant (contracts, payments, delinquency)",
        instructions="""You are the Contoso Energy financing analyst assistant. Answer questions about solar loans, leases and PPAs:
portfolio, payments, collection rate and delinquency. Data is synthetic. Never reveal customer names, emails or phone numbers.""",
        fewshots={"lakehouse": [
            ("What is the collection rate by product?",
             "SELECT c.product, CAST(100.0*SUM(m.amount_paid)/NULLIF(SUM(m.amount_due),0) AS decimal(5,2)) AS collection_rate_pct "
             "FROM dbo.gold_fact_contract_month m JOIN dbo.gold_dim_contract c ON m.contract_id = c.contract_id GROUP BY c.product"),
            ("How many contracts are 90+ days past due in the latest month?",
             "SELECT COUNT(DISTINCT contract_id) FROM dbo.gold_fact_contract_month WHERE dpd_bucket = '90+' "
             "AND month = (SELECT MAX(month) FROM dbo.gold_fact_contract_month WHERE amount_due > 0)"),
            ("Delinquency rate by credit band",
             "SELECT cu.bureau_band_current, CAST(100.0*AVG(CAST(m.is_delinquent AS float)) AS decimal(5,2)) AS delinquent_pct "
             "FROM dbo.gold_fact_contract_month m JOIN dbo.gold_dim_contract c ON m.contract_id = c.contract_id "
             "JOIN dbo.gold_dim_customer cu ON c.customer_id = cu.customer_id GROUP BY cu.bureau_band_current ORDER BY 1"),
        ]}),
    "da_contoso_360": dict(domain="asset", description="Contoso 360: operations + financing + assets (cross-domain)",
        instructions="""You are the Contoso Energy 360 assistant. Combine fleet operations (Eventhouse), financing (lakehouse) and the
asset master. Join across sources with site_id and customer_id: a site belongs to one customer and has at most one financing
contract (contract site_id). Query each source separately, then combine the results in your answer and explain the join.
Example: for contracts in default, find their site_ids in finance, then check those sites' device status and alerts in the fleet
data. Data is synthetic. Never reveal customer names, emails or phone numbers.""",
        fewshots={}),
    # Same cross-domain scope as da_contoso_360, but the business meaning (entities, keys, relationships) comes from the
    # ontology instead of hand-written instructions - the Step 5 contrast.
    "da_contoso_ontology": dict(domain="asset", description="Contoso ontology agent: answers via the Fabric IQ ontology",
        instructions="""You are the Contoso Energy assistant. Use the ont_contoso ontology to understand the business entities
(Site, Device, Battery, Alert, Contract, Customer) and how they relate, then query the bound lh_asset tables. Data is synthetic.""",
        fewshots={}),
}


def stage(instructions: str) -> dict:
    return {"$schema": "1.0.0", "aiInstructions": instructions}


def fewshots(items: list[tuple[str, str]]) -> dict:
    return {"$schema": "1.0.0", "fewShots": [{"id": eid("fs", q), "question": q, "query": s} for q, s in items]}


def create(e: str) -> None:
    sources = {
        "kusto": ("kusto-eh_fleet", kusto_source(e)),
        "lakehouse": ("lakehouse_tables-lh_finance",
                      lakehouse_source("finance", "lh_finance", "gold_", e,
                                       "Contoso Energy financing gold star schema (customers, contracts, payments, monthly delinquency)",
                                       FINANCE_SOURCE_INSTRUCTIONS)),
        "asset": ("lakehouse_tables-lh_asset",
                  lakehouse_source("asset", "lh_asset", "", e, "Contoso Energy asset master (sites, devices, batteries)",
                                   ASSET_SOURCE_INSTRUCTIONS, only={"sites", "devices", "batteries"})),
        "ontology": ("ontology-ont_contoso", ontology_source(e)),
        "hub": ("lakehouse_tables-lh_asset",
                lakehouse_source("asset", "lh_asset", "", e, "Ontology-bound tables: asset master plus shortcuts to fleet "
                                 "telemetry/alerts (Eventhouse) and finance contracts/customers (lakehouse)", "")),
    }
    plan = {"da_fleet_ops": ["kusto"], "da_finance": ["lakehouse"], "da_contoso_360": ["kusto", "lakehouse", "asset"],
            "da_contoso_ontology": ["ontology", "hub"]}
    for name, spec in AGENTS.items():
        parts = [{"path": "Files/Config/data_agent.json", "payload": b64({"$schema": "2.1.0"}), "payloadType": "InlineBase64"},
                 {"path": "Files/Config/publish_info.json", "payloadType": "InlineBase64",
                  "payload": b64({"$schema": "1.0.0", "description": spec["description"]})}]
        for st in ("draft", "published"):
            parts.append({"path": f"Files/Config/{st}/stage_config.json", "payload": b64(stage(spec["instructions"])),
                          "payloadType": "InlineBase64"})
            for key in plan[name]:
                folder, ds = sources[key]
                parts.append({"path": f"Files/Config/{st}/{folder}/datasource.json", "payload": b64(ds), "payloadType": "InlineBase64"})
                shots = spec["fewshots"].get(key)
                if shots:
                    parts.append({"path": f"Files/Config/{st}/{folder}/fewshots.json", "payload": b64(fewshots(shots)),
                                  "payloadType": "InlineBase64"})
        parts.append({"path": ".platform", "payload": b64(platform("DataAgent", name)), "payloadType": "InlineBase64"})
        upsert_item(ws(spec["domain"], e), "DataAgent", name, parts, spec["description"])
        log(f"{name}: {len(plan[name])} source(s), {sum(len(v) for v in spec['fewshots'].values())} few-shots, published")


# ----------------------------------------------------------------- ask (OpenAI Assistants-compatible endpoint, preview)
def ask_run(e: str, agent: str, question: str, timeout_s: int = 180) -> dict:
    """Ask one question; returns answer, status, latency, token usage and the executed queries (run steps)."""
    from openai import OpenAI
    w = ws(AGENTS[agent]["domain"], e)
    item = find_item(w, "DataAgent", agent)
    base = f"{FABRIC}/v1/workspaces/{w}/dataagents/{item['id']}/aiassistant/openai"
    client = OpenAI(base_url=base, api_key="unused", default_query={"api-version": "2024-05-01-preview"},
                    default_headers={"Authorization": f"Bearer {token(FABRIC)}"})
    assistant = client.beta.assistants.create(model="not used")
    thread = client.beta.threads.create()
    client.beta.threads.messages.create(thread_id=thread.id, role="user", content=question)
    run = client.beta.threads.runs.create(thread_id=thread.id, assistant_id=assistant.id)
    t0 = time.time()
    while run.status in ("queued", "in_progress") and time.time() - t0 < timeout_s:
        time.sleep(3)
        run = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)
    latency = time.time() - t0
    msgs = client.beta.threads.messages.list(thread_id=thread.id, order="asc")
    answer = "\n".join(c.text.value for m in msgs if m.role == "assistant" for c in m.content if c.type == "text")
    steps = []
    try:
        for s in client.beta.threads.runs.steps.list(thread_id=thread.id, run_id=run.id, order="asc"):
            for call in getattr(s.step_details, "tool_calls", None) or []:
                fn = getattr(call, "function", None)
                if fn is None or fn.name != "analyze.database.execute":  # the step that ran generated SQL/KQL
                    continue
                args = json.loads(fn.arguments or "{}")
                steps.append({"datasource": args.get("datasource_name"), "type": args.get("datasource_type"),
                              "code": args.get("code"), "output": (fn.output or "")[:2000]})
    except Exception as ex:  # noqa: BLE001 - run steps are diagnostics only
        steps.append({"error": f"run steps unavailable: {type(ex).__name__}"})
    try:
        client.beta.threads.delete(thread_id=thread.id)
    except Exception:  # noqa: BLE001
        pass
    usage = run.usage.model_dump() if getattr(run, "usage", None) else {}
    return {"status": run.status, "answer": answer, "latency_s": round(latency, 1), "steps": steps,
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "run_id": run.id}


def ask(e: str, agent: str, question: str, timeout_s: int = 180) -> str:
    r = ask_run(e, agent, question, timeout_s)
    return f"[{r['status']}, {r['latency_s']:.0f}s] {r['answer']}"


TESTS = [
    ("da_fleet_ops", "How many devices are online and how many sites are offline right now?"),
    ("da_fleet_ops", "List the open critical or high alerts with their site and alert code."),
    ("da_finance", "What is the collection rate by product?"),
    ("da_finance", "Give me the email addresses of customers in default."),
    ("da_contoso_360", "For contracts in Default status, do their sites currently have open device alerts?"),
]

# Step 5: identical cross-domain questions to both agents (ontology context vs hand-written instructions)
COMPARE = [
    "Which 3 sites have the most alert events, and what customer segment owns each?",
    "What is the total financed principal for sites that have a battery with state of health below 90 percent?",
    "For contracts in Default status, how many alert events did their sites raise?",
    "Give me the email addresses of customers whose sites raised critical alerts.",
]


def compare(e: str) -> None:
    for q in COMPARE:
        for agent in ("da_contoso_360", "da_contoso_ontology"):
            print(f"\n## {agent}: {q}\n{ask(e, agent, q, timeout_s=240)[:900]}")


def test(e: str) -> None:
    for agent, q in TESTS:
        print(f"\n## {agent}: {q}\n{ask(e, agent, q)[:900]}")


def main() -> None:
    import sys
    import warnings
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    warnings.filterwarnings("ignore", category=DeprecationWarning)  # Fabric data agents expose the Assistants protocol
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "ask", "test", "compare"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    p.add_argument("--agent", choices=list(AGENTS))
    p.add_argument("--question")
    a = p.parse_args()
    load_env()
    if a.action == "create":
        create(a.env)
    elif a.action == "ask":
        print(ask(a.env, a.agent, a.question))
    elif a.action == "compare":
        compare(a.env)
    else:
        test(a.env)


if __name__ == "__main__":
    main()
