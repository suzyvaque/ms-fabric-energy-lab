"""Step 11 live2.5: agent evaluation log in Eventhouse (dev only). Idempotent.

  lab_agent_eval    KQL database in the eh_fleet eventhouse (co-hosted, D-027; lab_ = not Git-tracked or deployed)
  lab_rtd_agent_eval Real-Time dashboard: accuracy by agent version over runs, train vs holdout, categories, improvements
  act_agent_eval_regression Activator: e-mail when a run shows a forbidden-data leak or an accuracy drop > 5 points

  py infra/agent_eval_kql.py create      [--env dev]   # database + tables
  py infra/agent_eval_kql.py dashboard   [--env dev]
  py infra/agent_eval_kql.py activator   [--env dev]   # recipient = ALERT_EMAIL or ADMIN_UPN (.env, never in Git, D-018)
  py infra/agent_eval_kql.py status      [--env dev]

Rows are written by eval/agent_eval.py (log, improve) through ingest().
"""
from __future__ import annotations

import argparse
import json
import time

from common import env, load_env, log
from fabric_rest import b64, fabric, find_item, kql, upsert_item
from rti_alerts import activator_entities, gid, ws
from rti_lab import platform

DB = "lab_agent_eval"
DASHBOARD = "lab_rtd_agent_eval"
ACTIVATOR = "act_agent_eval_regression"

SCHEMA = """.create-merge table eval_runs (run_id:string, env:string, capacity_sku:string, git:string, golden:string, agents:dynamic, logged_at:datetime)
.create-merge table eval_answers (run_id:string, agent:string, arm:string, agent_version:string, case_id:string, category:string, complexity:string, weight:int, split:string, question:string, expected_answer:string, answer:string, status:string, latency_s:real, query_s:real, prompt_tokens:long, completion_tokens:long, sources_used:dynamic, steps:dynamic, asked_at:datetime)
.create-merge table eval_steps (run_id:string, agent:string, arm:string, agent_version:string, case_id:string, category:string, seq:int, tool:string, datasource:string, datasource_type:string, nl_query:string, code:string, duration_s:real, output_chars:long, error:string, asked_at:datetime)
.create-merge table eval_scores (run_id:string, agent:string, arm:string, agent_version:string, case_id:string, category:string, split:string, weight:int, grader:string, pass:bool, score:real, detail:string, input_tokens:long, output_tokens:long, logged_at:datetime)
.create-merge table agent_versions (agent:string, arm:string, version:string, parent_version:string, instructions:string, datasource_instructions:dynamic, fewshots:dynamic, source:string, created_at:datetime)
.create-merge table improvements (proposal_id:string, arm:string, round:int, run_id:string, base_agent:string, base_version:string, candidate_agent:string, candidate_version:string, proposal:dynamic, guardrails:dynamic, attempts:int, optimizer_prompt_tokens:long, optimizer_completion_tokens:long, train_base:real, train_cand:real, validation_base:real, validation_cand:real, forbid_fail_cand:int, decision:string, reason:string, decided_at:datetime)
.create-merge table loop_events (loop_id:string, arm:string, round:int, stage:string, status:string, detail:string, agent:string, run_id:string, ts:datetime)
.create-or-alter function with (docstring = "Rules accuracy per run, agent and split") EvalAccuracy() {
    eval_scores | where grader == "rules"
    | summarize n = count(), passed = countif(pass), weighted = todouble(sumif(weight, pass)) / sum(weight),
                forbid_fail = countif(not(pass) and category in ("pii", "pii_indirect", "injection")),
                logged_at = max(logged_at) by run_id, agent, arm, agent_version, split
    | extend accuracy = todouble(passed) / n }"""
TABLES = {"eval_runs", "eval_answers", "eval_steps", "eval_scores", "agent_versions", "improvements", "loop_events"}


def database(e: str) -> tuple[str, dict, str]:
    """(workspace id, KQL database item, query URI) of lab_agent_eval."""
    w = ws(e)
    eh = find_item(w, "Eventhouse", "eh_fleet")
    db = find_item(w, "KQLDatabase", DB)
    if db is None:
        raise SystemExit(f"{DB} missing: py infra/agent_eval_kql.py create --env {e}")
    uri = fabric("get", f"workspaces/{w}/eventhouses/{eh['id']}")["properties"]["queryServiceUri"]
    return w, db, uri


def create(e: str) -> None:
    w = ws(e)
    eh = find_item(w, "Eventhouse", "eh_fleet")
    parts = [
        {"path": "DatabaseProperties.json", "payloadType": "InlineBase64",
         "payload": b64({"databaseType": "ReadWrite", "parentEventhouseItemId": eh["id"],
                         "oneLakeCachingPeriod": "P30D", "oneLakeStandardStoragePeriod": "P365D"})},
        {"path": "DatabaseSchema.kql", "payload": b64(SCHEMA), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("KQLDatabase", DB)), "payloadType": "InlineBase64"}]
    db = upsert_item(w, "KQLDatabase", DB, parts, "Step 11 live2.5: data agent evaluation log (lab, dev only)")
    _, _, uri = database(e)
    for attempt in range(10):  # a new database answers queries a few seconds after creation
        try:
            kql(uri, db["id"], ".show tables | project TableName", mgmt=True)
            break
        except SystemExit:
            time.sleep(10)
    else:
        raise SystemExit(f"{DB} not queryable")
    # Apply the schema directly every time: .create-merge is idempotent and adds new columns to existing tables.
    for cmd in [c for c in SCHEMA.split("\n.") if c.strip()]:
        kql(uri, db["id"], cmd if cmd.startswith(".") else "." + cmd, mgmt=True)
    tables = {r["TableName"] for r in kql(uri, db["id"], ".show tables | project TableName", mgmt=True)}
    if TABLES - tables:
        raise SystemExit(f"missing tables: {TABLES - tables}")
    log(f"{DB}: {len(TABLES)} tables + EvalAccuracy() ok ({db['id']})")


def ingest(e: str, table: str, rows: list[dict], batch: int = 20) -> int:
    """Append rows (dicts keyed by column) with inline multijson ingestion. Lab volumes only (a few hundred rows/run)."""
    if not rows:
        return 0
    _, db, uri = database(e)
    for i in range(0, len(rows), batch):
        body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows[i:i + batch])
        for attempt in range(5):
            try:
                kql(uri, db["id"], f".ingest inline into table {table} with (format='multijson') <|\n{body}", mgmt=True)
                break
            except SystemExit as ex:
                if "429" not in str(ex) or attempt == 4:
                    raise
                time.sleep(10 * (attempt + 1))
    return len(rows)


def query(e: str, q: str) -> list[dict]:
    _, db, uri = database(e)
    return kql(uri, db["id"], q)


# ---------------------------------------------------------------- dashboard
LAST_RUN = "let lastrun = toscalar(eval_scores | summarize arg_max(logged_at, run_id) | project run_id); "
LAST_LOOP = "let lp = toscalar(loop_events | summarize arg_max(ts, loop_id) | project loop_id); "
EXECUTED = "eval_steps | where tool == 'analyze.database.execute' "
PAGES = {"quality": "Quality", "sources": "Sources & tokens", "loop": "Loop"}
TILES = [  # (id, page, title, visual, x, y, w, h, query)
    # ---- Quality
    ("acc_runs", "quality", "Rules accuracy by run and agent", "table", 0, 0, 12, 8,
     "EvalAccuracy() | summarize n = sum(n), passed = sum(passed), forbid_fail = sum(forbid_fail), "
     "logged_at = max(logged_at) by run_id, agent, agent_version "
     "| extend accuracy = round(100.0 * passed / n, 1) | order by logged_at desc, agent asc"),
    ("split", "quality", "Accuracy (%) by agent version and split", "table", 12, 0, 12, 8,
     "EvalAccuracy() | summarize acc = round(100.0 * sum(passed) / sum(n), 1) by agent, agent_version, split "
     "| evaluate pivot(split, take_any(acc)) | order by agent asc"),
    ("trend", "quality", "Accuracy trend per arm (%)", "linechart", 0, 8, 12, 7,
     "EvalAccuracy() | where isnotempty(arm) and split in ('train', 'validation') "
     "| summarize acc = 100.0 * sum(passed) / sum(n), t = max(logged_at) by arm, run_id, agent "
     "| summarize acc = max(acc) by arm, t | order by t asc"),
    ("cats", "quality", "Latest run: accuracy by category (%)", "table", 12, 8, 12, 7,
     LAST_RUN + "eval_scores | where run_id == lastrun and grader == 'rules' "
     "| summarize acc = round(100.0 * countif(pass) / count(), 0) by category, agent "
     "| evaluate pivot(agent, take_any(acc)) | order by category asc"),
    ("fails", "quality", "Latest run: failed questions with the agent's query", "table", 0, 15, 24, 8,
     LAST_RUN + "eval_scores | where run_id == lastrun and grader == 'rules' and not(pass) "
     "| join kind=inner (eval_answers | where run_id == lastrun | project agent, case_id, question, answer, steps) "
     "on agent, case_id | project agent, case_id, category, split, detail, question, "
     "query = tostring(steps[0].code), answer = substring(answer, 0, 300)"),
    # ---- Sources & tokens
    ("src_agent", "sources", "Executed queries by data source and agent (latest run)", "columnchart", 0, 0, 12, 7,
     LAST_RUN + EXECUTED + "and run_id == lastrun | summarize executed = count() by agent, datasource"),
    ("src_cat", "sources", "Data source used per question category (latest run, % of queries)", "table", 12, 0, 12, 7,
     LAST_RUN + EXECUTED + "and run_id == lastrun | summarize n = count() by category, datasource "
     "| join kind=inner (" + EXECUTED + "and run_id == lastrun | summarize total = count() by category) on category "
     "| project category, datasource, pct = round(100.0 * n / total, 0) | evaluate pivot(datasource, take_any(pct))"),
    ("src_q", "sources", "Which source answered which question (latest run)", "table", 0, 7, 24, 8,
     LAST_RUN + EXECUTED + "and run_id == lastrun | summarize sources = make_set(datasource), executed = count(), "
     "query_s = sum(duration_s) by agent, case_id, category "
     "| join kind=leftouter (eval_scores | where run_id == lastrun and grader == 'rules' | project agent, case_id, pass) "
     "on agent, case_id | project agent, case_id, category, sources, executed, query_s, pass | order by case_id asc"),
    ("tok_agent", "sources", "Agent tokens per answer by run", "table", 0, 15, 12, 7,
     "eval_answers | summarize answers = count(), prompt_per_answer = round(avg(prompt_tokens), 0), "
     "completion_per_answer = round(avg(completion_tokens), 1), total_tokens = sum(prompt_tokens + completion_tokens), "
     "t = max(asked_at) by run_id, agent | order by t desc"),
    ("tok_correct", "sources", "Prompt tokens per correct answer (rules)", "table", 12, 15, 12, 7,
     "eval_answers | join kind=inner (eval_scores | where grader == 'rules' | project run_id, agent, case_id, pass) "
     "on run_id, agent, case_id | summarize tokens = sum(prompt_tokens + completion_tokens), correct = countif(pass), "
     "t = max(asked_at) by run_id, agent | extend tokens_per_correct = round(todouble(tokens) / correct, 0) "
     "| order by t desc"),
    ("tok_graders", "sources", "Grader and optimizer tokens", "table", 0, 22, 24, 6,
     "eval_scores | where grader != 'rules' | summarize input_tokens = sum(input_tokens), "
     "output_tokens = sum(output_tokens) by component = strcat('grader: ', grader) "
     "| union (improvements | summarize input_tokens = sum(optimizer_prompt_tokens), "
     "output_tokens = sum(optimizer_completion_tokens) by component = 'optimizer: gpt-4.1')"),
    # ---- Loop
    ("progress", "loop", "Loop progress (latest loop): last stage per arm", "table", 0, 0, 24, 5,
     LAST_LOOP + "loop_events | where loop_id == lp | summarize arg_max(ts, round, stage, status, detail) by arm "
     "| project arm, round, stage, status, detail, ts"),
    ("timeline", "loop", "Loop timeline (latest loop)", "table", 0, 5, 24, 10,
     LAST_LOOP + "loop_events | where loop_id == lp | project ts, arm, round, stage, status, agent, detail "
     "| order by ts desc"),
    ("impr", "loop", "Proposals and decisions", "table", 0, 15, 24, 7,
     "improvements | project decided_at, arm, round, decision, reason, attempts, train_base, train_cand, "
     "validation_base, validation_cand, candidate_agent, candidate_version | order by decided_at desc"),
    ("versions", "loop", "Agent versions", "table", 0, 22, 24, 7,
     "agent_versions | project created_at, arm, agent, version, parent_version, source, "
     "instructions = substring(instructions, 0, 400) | order by created_at desc"),
]


def dashboard(e: str) -> dict:
    w, db, uri = database(e)
    ds_id = gid(f"rtd-eval/ds/{e}")
    page_ids = {k: gid(f"rtd-eval/page/{k}") for k in PAGES}
    queries, tiles = [], []
    for tid, page, title, visual, x, y, wd, h, q in TILES:
        qid = gid(f"rtd-eval/q/{tid}")
        queries.append({"id": qid, "dataSource": {"kind": "inline", "dataSourceId": ds_id}, "text": q, "usedVariables": []})
        tiles.append({"id": gid(f"rtd-eval/t/{tid}"), "title": title, "visualType": visual, "pageId": page_ids[page],
                      "layout": {"x": x, "y": y, "width": wd, "height": h},
                      "queryRef": {"kind": "query", "queryId": qid}, "visualOptions": {}})
    body = {"$schema": "https://dataexplorer.azure.com/static/d/schema/52/dashboard.json", "schema_version": "52",
            "title": "Contoso Energy - Data agent evaluation loop",
            "autoRefresh": {"enabled": True, "defaultInterval": "1m", "minInterval": "30s"},
            "baseQueries": [], "tiles": tiles, "queries": queries, "parameters": [],
            "dataSources": [{"id": ds_id, "name": DB, "scopeId": "kusto-trident", "kind": "kusto-trident",
                             "clusterUri": uri, "database": db["id"], "workspace": w}],
            "pages": [{"name": name, "id": page_ids[k]} for k, name in PAGES.items()]}
    item = upsert_item(w, "KQLDashboard", DASHBOARD, [
        {"path": "RealTimeDashboard.json", "payload": b64(body), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("KQLDashboard", DASHBOARD)), "payloadType": "InlineBase64"}],
        "Data agent evaluation loop: quality, sources and tokens, loop timeline (lab)")
    bad = 0
    for tid, page, title, *_rest, q in TILES:
        try:
            log(f"tile ok   [{PAGES[page]}] {title}: {len(kql(uri, db['id'], q))} rows")
        except SystemExit as ex:
            bad += 1
            log(f"tile FAIL [{PAGES[page]}] {title}: {str(ex)[:200]}")
    if bad:
        raise SystemExit(f"{bad} dashboard tile query(ies) failed")
    return item

# ---------------------------------------------------------------- activator
# One event per (run, agent) that leaked forbidden data or dropped > 5 points vs the same agent's previous run.
REGRESSION_QUERY = (
    "EvalAccuracy() | summarize passed = sum(passed), n = sum(n), forbid_fail = sum(forbid_fail), "
    "logged_at = max(logged_at) by run_id, agent | extend accuracy = 100.0 * passed / n "
    "| order by agent asc, logged_at asc | extend prev = iff(prev(agent) == agent, prev(accuracy), real(null)) "
    "| where logged_at between (startTime .. endTime) and (forbid_fail > 0 or accuracy < prev - 5) "
    "| project logged_at, run_id, agent, accuracy = round(accuracy, 1), prev = round(prev, 1), forbid_fail")


def activator(e: str) -> None:
    w, db, uri = database(e)
    recipient = env("ALERT_EMAIL", required=False) or env("ADMIN_UPN")
    test = REGRESSION_QUERY.replace("startTime", "ago(30d)").replace("endTime", "now()")
    n = len(kql(uri, db["id"], test))
    entities = activator_entities(ACTIVATOR, w, db["id"], REGRESSION_QUERY, "logged_at", [
        {"name": "EventDetectStep", "id": gid("act-eval/detect"),
         "rows": [{"name": "OnEveryValue", "kind": "OnEveryValue", "arguments": []}]}],
        recipient, "Contoso data agent evaluation: regression",
        "An evaluation run leaked forbidden data or dropped more than 5 points. Open lab_rtd_agent_eval.")
    item = find_item(w, "Reflex", ACTIVATOR)
    parts = [{"path": "ReflexEntities.json", "payload": b64(entities), "payloadType": "InlineBase64"}]
    if item:
        fabric("post", f"workspaces/{w}/items/{item['id']}/updateDefinition", {"definition": {"parts": parts}})
    else:
        parts.append({"path": ".platform", "payload": b64(platform("Reflex", ACTIVATOR)), "payloadType": "InlineBase64"})
        upsert_item(w, "Reflex", ACTIVATOR, parts, "Data agent evaluation regression alert (lab)")
    log(f"{ACTIVATOR}: deployed (source query returns {n} rows over 30 days)")


def status(e: str) -> None:
    for t in sorted(TABLES):
        print(t, query(e, f"{t} | count")[0]["Count"])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["create", "dashboard", "activator", "status"])
    p.add_argument("--env", choices=["dev"], default="dev")  # lab: dev only
    a = p.parse_args()
    load_env()
    {"create": create, "dashboard": dashboard, "activator": activator, "status": status}[a.action](a.env)


if __name__ == "__main__":
    main()
