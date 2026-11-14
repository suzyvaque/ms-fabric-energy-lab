"""Step 13 check: ask the field-ops agent questions that need SharePoint, Teams and Fabric data together."""
import json
import sys

from common import load_env
from data_agents import ask_run

TABLES = ["fieldops_visits", "fieldops_procedures", "fieldops_policy", "fieldops_channel_messages", "fin_contracts",
          "fleet_alert_events", "sites", "batteries"]
QUESTIONS = [
    "Which Critical BATT_OVERTEMP alerts had their field visit after the SLA? Give alert_id, site_id, hours to visit and the SLA.",
    "For battery overheating alerts with no field visit yet, what does the procedure say to do, and which policy section "
    "decides whether the repair is billed? Include each site's contract product.",
    "What did the dispatcher post in the Teams channel about parts delays, and which contracts are affected?",
    "Show me the email address of the customer at the site with the most warranty claims.",
]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env()
    out = []
    for q in QUESTIONS:
        r = ask_run("dev", "da_contoso_fieldops", q, 300)
        used = sorted({t for s in r.get("steps", []) for t in TABLES if t in json.dumps(s)})
        out.append({"question": q, "status": r["status"], "latency_s": r["latency_s"], "tables": used,
                    "answer": (r.get("answer") or "")[:1200]})
        print(f"Q: {q}\nA: {out[-1]['answer']}\ntables: {used} | {r['latency_s']} s | {r['status']}\n---")


if __name__ == "__main__":
    main()
