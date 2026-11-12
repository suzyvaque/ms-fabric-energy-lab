"""Step 11: data agent evaluation pipeline - one golden set, one run log, four graders, one comparison report.

  collect  ask each agent every golden question; expected values are recomputed live from each case's SQL/KQL
           truth query; logs answer, executed SQL/KQL (run steps), tokens and latency -> eval/runs/<run>/responses.jsonl
  score    graders on the same log -> eval/runs/<run>/scores.jsonl
             rules     deterministic numbers/entities/PII check against the truth values (the reference)
             decision  Microsoft-Decision-1 (Foundry) "System One" judge: typed noul/score decisions with probabilities
             foundry   Foundry evaluation SDK: SimilarityEvaluator (gpt-4.1-mini judge) + rules as a custom evaluator,
                       logged to the Foundry project (compare runs in the portal)
  fabric   Fabric Data Agent SDK evaluate_data_agent in a Fabric notebook (lab_agent_eval, dev only). It re-asks
           the agent itself, so its verdicts are on its own answers; rules are re-applied to those answers too.
  report   per-agent accuracy (A/B), grader agreement with rules (kappa, false pass/fail), latency, tokens, cost
  gate     CI: fail if rules accuracy < --min-accuracy or any PII case fails

  py eval/agent_eval.py all     [--env dev] [--agents da_contoso_360,da_contoso_ontology] [--graders rules,decision,foundry]
  py eval/agent_eval.py collect [--env dev] [--agents ...] [--cases id1,id2] [--run <id>]
  py eval/agent_eval.py score   --run <id> [--graders ...] [--no-upload]
  py eval/agent_eval.py fabric  --run <id> [--env dev]
  py eval/agent_eval.py report  --run <id> [--publish]   # --publish: sanitized copy to Git-tracked eval/reports/
  py eval/agent_eval.py gate    --run <id> [--agents ...] [--min-accuracy 0.7]
  py eval/agent_eval.py latency --runs live3-f32,live3,live3-f128,live3-f512   # capacity sweep -> eval/reports/
  collect --warmup N makes N discarded calls per agent first (use after a capacity resize).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
sys.path.insert(0, str(ROOT / "eval"))

import graders  # noqa: E402
from common import env, load_env, log  # noqa: E402

RUNS = ROOT / "eval" / "runs"          # gitignored: raw answers, generated SQL/KQL, tool output
REPORTS = ROOT / "eval" / "reports"    # Git-tracked: sanitized reports (report --publish)
GOLDEN = ROOT / "eval" / "golden" / "contoso.jsonl"
NB_SOURCE = ROOT / "eval" / "notebook" / "nb_agent_eval.py"
NOTEBOOK = "lab_agent_eval"  # lab_ = dev-only, not Git-tracked or deployed (common.UNMANAGED_ITEMS)
DEFAULT_AGENTS = ["da_contoso_360", "da_contoso_ontology", "lab_da_hub_rich", "lab_da_hub_bare"]
ALL_GRADERS = ["rules", "decision", "foundry"]
WEIGHTS = {"critical": 3, "high": 2, "normal": 1}  # golden-set "weight" -> report impact weighting
# USD per 1M tokens (input, output). List prices at the time of writing; update for your agreement.
# Microsoft-Decision-1: set DECISION_PRICE_PER_M in .env to include it (output tokens are not billed for decisions).
PRICES = {"foundry": (0.40, 1.60)}


# ----------------------------------------------------------------- files
def run_dir(run_id: str) -> Path:
    return RUNS / run_id


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8")


def load_cases(ids: list[str] | None = None) -> list[dict]:
    cases = read_jsonl(GOLDEN)
    if ids:
        unknown = set(ids) - {c["id"] for c in cases}
        if unknown:
            raise SystemExit(f"unknown case id(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c["id"] in ids]
    return cases


def latest_run() -> str:
    runs = sorted(p.name for p in RUNS.glob("*") if (p / "responses.jsonl").exists())
    if not runs:
        raise SystemExit("no runs yet: py eval/agent_eval.py collect")
    return runs[-1]


def _with_deadline(fn, seconds: float):
    """Run fn in a daemon thread; raise TimeoutError if it has not returned in time (a hung call cannot be cancelled,
    but the run moves on and the abandoned thread dies with the process)."""
    import threading
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as ex:  # noqa: BLE001
            box["error"] = ex
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f"no answer within {seconds:.0f} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def capacity_sku() -> str | None:
    """Best effort via the Fabric REST API (CI may not have access)."""
    try:
        from fabric_rest import fabric
        caps = fabric("get", "capacities").get("value", [])
        return next((c["sku"] for c in caps if c["displayName"] == env("FABRIC_CAPACITY_NAME")), None)
    except (Exception, SystemExit):  # noqa: BLE001
        return None


# ----------------------------------------------------------------- truth (live SQL/KQL)
class Truth:
    def __init__(self, e: str):
        self.e, self._hosts, self._kusto = e, {}, None

    def _host(self, source: str) -> str:
        from data_agents import ws
        from fabric_rest import fabric, find_item
        if source not in self._hosts:
            domain = {"lh_finance": "finance", "lh_asset": "asset"}[source]
            w = ws(domain, self.e)
            lh = find_item(w, "Lakehouse", source)
            self._hosts[source] = fabric("get", f"workspaces/{w}/lakehouses/{lh['id']}")["properties"][
                "sqlEndpointProperties"]["connectionString"]
        return self._hosts[source]

    def _kql(self) -> tuple[str, str]:
        from data_agents import ws
        from fabric_rest import fabric, find_item
        if self._kusto is None:
            w = ws("fleet", self.e)
            eh, db = find_item(w, "Eventhouse", "eh_fleet"), find_item(w, "KQLDatabase", "eh_fleet")
            self._kusto = (fabric("get", f"workspaces/{w}/eventhouses/{eh['id']}")["properties"]["queryServiceUri"], db["id"])
        return self._kusto

    def rows(self, case: dict) -> list[list]:
        for attempt in range(6):  # F2 capacity throttles (429) under load; back off and retry
            try:
                return self._rows(case)
            except SystemExit as ex:
                if "429" not in str(ex) or attempt == 5:
                    raise
                time.sleep(10 * (attempt + 1))
        return []

    def _rows(self, case: dict) -> list[list]:
        from fabric_rest import kql, tsql
        t = case["truth"]
        if t["engine"] == "sql":
            return [list(r) for r in tsql(self._host(t["source"]), t["source"], t["query"])]
        uri, db = self._kql()
        return [list(r.values()) for r in kql(uri, db, t["query"])]


# ----------------------------------------------------------------- collect
def collect(e: str, agents: list[str], case_ids: list[str] | None, run_id: str | None, workers: int = 4,
            warmup: int = 0) -> str:
    from data_agents import AGENTS, ask_run
    unknown = {a for a in agents if a not in AGENTS and not a.startswith("lab_")}  # lab_: loop candidates
    if unknown:
        raise SystemExit(f"unknown agent(s): {', '.join(unknown)}")
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cases, truth = load_cases(case_ids), Truth(e)
    log(f"run {run_id}: resolving {len(cases)} truth queries")
    expected = {}
    for c in cases:
        try:
            t0 = time.perf_counter()
            rows = truth.rows(c)
            truth_ms = round((time.perf_counter() - t0) * 1000)  # direct SQL/KQL: engine-only latency control
            exp, text = graders.expected_from_truth(c, rows)
        except (Exception, SystemExit) as ex:  # noqa: BLE001 - e.g. the CI identity cannot read a truth source
            log(f"truth for {c['id']} unavailable, case skipped: {str(ex)[:160]}")
            continue
        expected[c["id"]] = (exp, text, datetime.now(timezone.utc).isoformat(), truth_ms)
    cases = [c for c in cases if c["id"] in expected]
    if not cases:
        raise SystemExit("no case has a resolvable truth")

    def one(job: tuple[str, dict]) -> dict:
        agent, c = job
        exp, text, truth_at, truth_ms = expected[c["id"]]
        base = {"run_id": run_id, "env": e, "agent": agent, "case_id": c["id"], "category": c["category"],
                "complexity": c.get("complexity", "simple"), "weight": WEIGHTS[c.get("weight", "normal")],
                "split": c.get("split", "train"),
                "question": c["question"], "expected": exp, "expected_answer": text, "check": c.get("check", {}),
                "truth_at": truth_at, "truth_ms": truth_ms, "asked_at": datetime.now(timezone.utc).isoformat()}
        try:
            res = _with_deadline(lambda: ask_run(e, agent, c["question"], timeout_s=240), 420)
            out = {**base, **res}
        except (Exception, SystemExit) as ex:  # noqa: BLE001 - one failed question must not stop the run
            out = {**base, "status": "timeout" if isinstance(ex, TimeoutError) else "error", "answer": "",
                   "error": f"{type(ex).__name__}: {str(ex)[:300]}"}
        log(f"  {agent} {c['id']}: {out['status']} {out.get('latency_s', '-')} s")  # progress: a stall shows up in minutes
        return out

    # The endpoint reuses one thread per caller and agent: questions to the same agent must run one at a time
    # ("Can't add messages to thread ... while a run is active"); different agents run in parallel.
    def per_agent(agent: str) -> list[dict]:
        for _ in range(warmup):  # discarded: first calls after a capacity resize/resume are slower
            try:
                ask_run(e, agent, "How many sites are there?", timeout_s=240)
            except (Exception, SystemExit):  # noqa: BLE001
                pass
        return [one((agent, c)) for c in cases]

    log(f"asking {len(agents) * len(cases)} question(s) ({len(agents)} agent(s) in parallel, sequential per agent"
        + (f", {warmup} warm-up call(s) each" if warmup else "") + ")")
    with ThreadPoolExecutor(max(1, min(workers, len(agents)))) as pool:
        records = [r for batch in pool.map(per_agent, agents) for r in batch]
    d = run_dir(run_id)
    write_jsonl(d / "responses.jsonl", records)
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    (d / "meta.json").write_text(json.dumps({"run_id": run_id, "env": e, "agents": agents, "cases": len(cases),
                                             "git": sha, "golden": GOLDEN.name, "capacity_sku": capacity_sku()},
                                            indent=2), encoding="utf-8")
    bad = [r for r in records if r["status"] != "completed"]
    log(f"run {run_id}: {len(records) - len(bad)}/{len(records)} completed -> {d / 'responses.jsonl'}")
    return run_id


# ----------------------------------------------------------------- graders
def grade_rules(recs: list[dict]) -> list[dict]:
    out = []
    for r in recs:
        t0 = time.perf_counter()
        v = graders.rules_grade(r)
        out.append({"grader": "rules", **_key(r), **v, "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
                    "input_tokens": 0, "output_tokens": 0})
    return out


def _key(r: dict) -> dict:
    return {"agent": r["agent"], "case_id": r["case_id"], "category": r["category"]}


def grade_decision(recs: list[dict]) -> list[dict]:
    from fabric_rest import token
    endpoint, model = env("FOUNDRY_ENDPOINT").rstrip("/"), env("FOUNDRY_DECISION_DEPLOYMENT")
    url = f"{endpoint}/providers/microsoft/v1/systemone"

    def one(r: dict) -> dict:
        body = json.dumps(graders.decision_request(r, model)).encode()
        for attempt in range(6):
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Authorization": f"Bearer {token('https://cognitiveservices.azure.com')}",
                "Content-Type": "application/json"})
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read())
                ms = (time.perf_counter() - t0) * 1000
                usage = payload.get("usage", {})
                return {"grader": "decision", **_key(r), **graders.decision_verdict(r, payload),
                        "latency_ms": round(ms, 1), "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0)}
            except urllib.error.HTTPError as ex:
                if ex.code in (429, 500, 502, 503) and attempt < 5:
                    time.sleep(float(ex.headers.get("Retry-After") or 0) or min(60, 5 * 2 ** attempt))
                    continue
                return {"grader": "decision", **_key(r), "pass": None, "score": None,
                        "detail": f"HTTP {ex.code}: {ex.read()[:200]!r}"}
        return {"grader": "decision", **_key(r), "pass": None, "score": None, "detail": "retries exhausted"}

    with ThreadPoolExecutor(4) as pool:
        return list(pool.map(one, recs))


class RulesEvaluator:
    """Custom code evaluator so the deterministic verdict appears next to the LLM judge in the Foundry portal."""

    def __call__(self, *, response: str, expected: str, check: str, category: str, status: str) -> dict:
        v = graders.rules_grade({"answer": response, "expected": json.loads(expected), "check": json.loads(check),
                                 "category": category, "status": status})
        return {"rules_pass": float(v["pass"]), "rules_score": v["score"], "rules_detail": v["detail"]}


def grade_foundry(recs: list[dict], d: Path, upload: bool) -> list[dict]:
    from azure.ai.evaluation import AzureOpenAIModelConfiguration, SimilarityEvaluator, evaluate
    from azure.identity import AzureCliCredential
    data = d / "foundry_input.jsonl"
    write_jsonl(data, [{"agent": r["agent"], "case_id": r["case_id"], "category": r["category"],
                        "status": r["status"], "query": r["question"], "response": r.get("answer") or "",
                        "ground_truth": r["expected_answer"], "expected": json.dumps(r["expected"]),
                        "check": json.dumps(r["check"])} for r in recs])
    model = AzureOpenAIModelConfiguration(azure_endpoint=env("FOUNDRY_ENDPOINT"),
                                          azure_deployment=env("FOUNDRY_JUDGE_DEPLOYMENT"))
    similarity = SimilarityEvaluator(model, credential=AzureCliCredential(), threshold=4)
    kwargs = {"azure_ai_project": env("FOUNDRY_PROJECT_ENDPOINT"), "evaluation_name": f"agent-eval-{d.name}"} if upload else {}
    t0 = time.perf_counter()
    result = evaluate(
        data=str(data),
        evaluators={"similarity": similarity, "rules": RulesEvaluator()},
        evaluator_config={
            "similarity": {"column_mapping": {"query": "${data.query}", "response": "${data.response}",
                                              "ground_truth": "${data.ground_truth}"}},
            "rules": {"column_mapping": {"response": "${data.response}", "expected": "${data.expected}",
                                         "check": "${data.check}", "category": "${data.category}",
                                         "status": "${data.status}"}}},
        output_path=str(d / "foundry_result.json"), **kwargs)
    per_row_ms = (time.perf_counter() - t0) * 1000 / max(len(recs), 1)
    if result.get("studio_url"):
        log(f"Foundry evaluation: {result['studio_url']}")
        (d / "foundry_url.txt").write_text(result["studio_url"], encoding="utf-8")
    out = []
    for row in result["rows"]:
        sim = row.get("outputs.similarity.similarity")
        res = row.get("outputs.similarity.similarity_result")
        r = {"agent": row["inputs.agent"], "case_id": row["inputs.case_id"], "category": row["inputs.category"]}
        props = row.get("outputs.similarity.similarity_properties")
        props = props if isinstance(props, dict) else {}  # NaN when the judge call failed for this row
        if isinstance(sim, float) and math.isnan(sim):
            sim = None
        out.append({"grader": "foundry", **r, "pass": None if sim is None else res == "pass",
                    "score": sim, "detail": f"similarity={sim}",
                    "latency_ms": round(per_row_ms, 1),  # evaluate() runs rows in parallel; this is wall time per row
                    "input_tokens": props.get("prompt_tokens", 0) or 0,
                    "output_tokens": props.get("completion_tokens", 0) or 0})
    return out


def score(run_id: str, which: list[str], upload: bool = True) -> None:
    d = run_dir(run_id)
    recs = read_jsonl(d / "responses.jsonl")
    if not recs:
        raise SystemExit(f"no responses in {d}")
    # Grading settings (tolerances) come from the current golden set, so a rule fix applies on re-score; expected
    # values stay as resolved at collect time.
    golden = {c["id"]: c for c in read_jsonl(GOLDEN)}
    for r in recs:
        if r["case_id"] in golden:
            r["check"] = golden[r["case_id"]].get("check", r.get("check", {}))
    scores = read_jsonl(d / "scores.jsonl")
    for g in which:
        log(f"grading {len(recs)} answer(s) with {g}")
        if g == "rules":
            new = grade_rules(recs)
        elif g == "decision":
            new = grade_decision(recs)
        elif g == "foundry":
            new = grade_foundry(recs, d, upload)
        else:
            raise SystemExit(f"unknown grader {g}")
        scores = [s for s in scores if s["grader"] != g] + new
        write_jsonl(d / "scores.jsonl", scores)  # after each grader: a later failure keeps earlier results
    log(f"scores -> {d / 'scores.jsonl'}")


# ----------------------------------------------------------------- Fabric Data Agent SDK (Fabric notebook)
def notebook_ipynb(ws_id: str, lh: dict) -> dict:
    import re
    chunks = re.split(r"^# %%(.*)$", NB_SOURCE.read_text(encoding="utf-8"), flags=re.M)
    cells = []
    for i, (marker, body) in enumerate([("[markdown]", chunks[0]), *zip(chunks[1::2], chunks[2::2])]):
        body = body.strip("\n")
        if not body.strip():
            continue
        if "[markdown]" in marker:
            text = "\n".join(line[2:] if line.startswith("# ") else line.lstrip("#") for line in body.splitlines())
            cells.append({"cell_type": "markdown", "metadata": {}, "source": text.splitlines(keepends=True)})
        else:
            cells.append({"cell_type": "code", "metadata": {"tags": ["parameters"]} if "[parameters]" in marker else {},
                          "source": body.splitlines(keepends=True), "outputs": [], "execution_count": None})
    return {"nbformat": 4, "nbformat_minor": 5, "cells": cells, "metadata": {
        "kernel_info": {"name": "synapse_pyspark"},
        "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
        "language_info": {"name": "python"},
        "dependencies": {"lakehouse": {"default_lakehouse": lh["id"], "default_lakehouse_name": "lh_asset",
                                       "default_lakehouse_workspace_id": ws_id}}}}


def fabric_sdk(e: str, run_id: str, timeout_min: int = 60) -> None:
    from data_agents import ws
    from fabric_rest import FABRIC, _request, b64, find_item, upsert_item
    from onelake import read, upload
    from rti_lab import platform
    d = run_dir(run_id)
    recs = read_jsonl(d / "responses.jsonl")
    agents = sorted({r["agent"] for r in recs})
    w = ws("asset", e)
    lh = find_item(w, "Lakehouse", "lh_asset")
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["agent", "case_id", "question", "expected_answer"])
    for r in recs:
        wr.writerow([r["agent"], r["case_id"], r["question"], r["expected_answer"]])
    folder = f"Files/agent_eval/{run_id}"
    upload(w, lh["id"], f"{folder}/golden.csv", buf.getvalue().encode("utf-8"))
    nb = upsert_item(w, "Notebook", NOTEBOOK, [
        {"path": "notebook-content.ipynb", "payload": b64(notebook_ipynb(w, lh)), "payloadType": "InlineBase64"},
        {"path": ".platform", "payload": b64(platform("Notebook", NOTEBOOK)), "payloadType": "InlineBase64"}],
        "Data agent evaluation with the Fabric Data Agent SDK (lab, dev only)", definition_format="ipynb")
    body = {"executionData": {"parameters": {"run_id": {"value": run_id, "type": "string"},
                                             "agents": {"value": ",".join(agents), "type": "string"}}}}
    status, headers, payload = _request("post", f"{FABRIC}/v1/workspaces/{w}/items/{nb['id']}/jobs/instances"
                                                "?jobType=RunNotebook", body, FABRIC)
    if status != 202:
        raise SystemExit(f"notebook run failed to start: {status} {payload}")
    job_url, t0 = headers["Location"], time.time()
    log(f"{NOTEBOOK} started for {', '.join(agents)} (Spark start + one agent call per question; several minutes)")
    while time.time() - t0 < timeout_min * 60:
        time.sleep(30)
        _, _, job = _request("get", job_url, None, FABRIC)
        if job.get("status") in ("Completed", "Failed", "Cancelled", "Deduped"):
            break
    else:
        raise SystemExit("notebook job timed out")
    log(f"{NOTEBOOK} {job['status']} after {time.time() - t0:.0f}s")
    if job["status"] != "Completed":
        try:
            tb = read(w, lh["id"], f"{folder}/fabric_sdk_error.txt").decode("utf-8", "replace")
        except SystemExit:
            tb = "(no fabric_sdk_error.txt: the notebook failed before its first guarded statement)"
        raise SystemExit(f"notebook {job['status']}: {json.dumps(job.get('failureReason'))[:400]}\n{tb[-2500:]}")
    rows = json.loads(read(w, lh["id"], f"{folder}/fabric_sdk.json"))
    meta = json.loads(read(w, lh["id"], f"{folder}/fabric_sdk_summary.json"))
    (d / "fabric_sdk.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    (d / "fabric_sdk_summary.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf-8")
    for agent, errs in meta.get("errors", {}).items():
        log(f"fabric_sdk {agent}: {len(errs)} stage attempt(s) failed, last: {errs[-1].strip().splitlines()[-1][:200]}")
    by_case = {(r["agent"], r["case_id"]): r for r in recs}
    kept = [s for s in read_jsonl(d / "scores.jsonl") if s["grader"] not in ("fabric_sdk", "rules@fabric_sdk")]
    new = []
    for row in rows:
        r = by_case.get((row["agent"], row["case_id"]))
        if r is None:
            continue
        verdict = str(row.get("judgement") or "").strip().lower()
        ok = True if verdict in ("true", "yes", "correct", "pass", "passed", "1") else \
            False if verdict in ("false", "no", "incorrect", "fail", "failed", "0") else None
        new.append({"grader": "fabric_sdk", **_key(r), "pass": ok,
                    "score": None, "detail": f"judgement={verdict}", "latency_ms": None,
                    "input_tokens": 0, "output_tokens": 0})
        again = graders.rules_grade({**r, "answer": row.get("actual_answer") or "", "status": "completed"})
        new.append({"grader": "rules@fabric_sdk", **_key(r), **again, "latency_ms": None})
    write_jsonl(d / "scores.jsonl", kept + new)
    log(f"fabric_sdk: {len(rows)} row(s) merged into scores")


# ----------------------------------------------------------------- report / gate
def _u(x, unit: str) -> str:
    return "-" if x is None else f"{x} {unit}"


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def summarize(run_id: str) -> dict:
    d = run_dir(run_id)
    recs, scores = read_jsonl(d / "responses.jsonl"), read_jsonl(d / "scores.jsonl")
    rules = {(s["agent"], s["case_id"]): s["pass"] for s in scores if s["grader"] == "rules"}
    out: dict = {"agents": {}, "graders": {}, "categories": {}, "complexity": {}, "meta": {}}
    if (d / "meta.json").exists():
        out["meta"] = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    for a in sorted({r["agent"] for r in recs}):
        ar = [r for r in recs if r["agent"] == a]
        verdicts = [rules.get((a, r["case_id"])) for r in ar]
        done = [r for r in ar if r["status"] == "completed"]
        lat = [r.get("latency_s") for r in done]
        out["agents"][a] = {
            "n": len(ar), "accuracy": sum(bool(v) for v in verdicts) / len(ar) if rules else None,
            "weighted": graders.weighted_accuracy([(bool(v), r.get("weight", 1)) for r, v in zip(ar, verdicts)])
            if rules else None,
            "completed": len(done), "p50_s": graders.percentile(lat, 0.5), "p95_s": graders.percentile(lat, 0.95),
            "max_s": max(lat) if lat else None,
            "query_p50_s": graders.percentile([r.get("query_s") for r in done if r.get("steps")], 0.5),
            "truth_p50_ms": graders.percentile([r.get("truth_ms") for r in ar], 0.5),
            "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in ar),
            # every case with forbidden values (pii, pii_indirect, injection) must pass: CI gate
            "pii_failures": [r["case_id"] for r, v in zip(ar, verdicts)
                             if ("forbid" in r.get("expected", {}) or r["category"] == "pii") and not v]}
        for key, field in (("categories", "category"), ("complexity", "complexity")):
            for val in sorted({r.get(field, "simple") for r in ar}):
                cv = [rules.get((a, r["case_id"])) for r in ar if r.get(field, "simple") == val]
                out[key].setdefault(val, {})[a] = sum(bool(v) for v in cv) / len(cv)
    price_decision = float(env("DECISION_PRICE_PER_M", required=False) or 0) or None
    # fabric_sdk re-asks the agent, so its reference is the rule verdict on those same answers (rules@fabric_sdk)
    refs = {"rules": rules, "fabric_sdk": {(s["agent"], s["case_id"]): s["pass"] for s in scores
                                           if s["grader"] == "rules@fabric_sdk"}}
    for g in sorted({s["grader"] for s in scores} - {"rules", "rules@fabric_sdk"}):
        refmap = refs.get(g, rules)
        gs = [s for s in scores if s["grader"] == g and s.get("pass") is not None and (s["agent"], s["case_id"]) in refmap]
        ref = [bool(refmap[(s["agent"], s["case_id"])]) for s in gs]
        stats = graders.compare(ref, [bool(s["pass"]) for s in gs])
        tin, tout = sum(s.get("input_tokens") or 0 for s in gs), sum(s.get("output_tokens") or 0 for s in gs)
        price = PRICES.get(g) or ((price_decision, 0.0) if g == "decision" and price_decision else None)
        lat = sorted(s["latency_ms"] for s in gs if s.get("latency_ms") is not None)
        stats.update({"errors": sum(1 for s in scores if s["grader"] == g and s.get("pass") is None),
                      "p50_ms": lat[len(lat) // 2] if lat else None, "input_tokens": tin, "output_tokens": tout,
                      "cost_usd": round((tin * price[0] + tout * price[1]) / 1e6, 4) if price else None})
        out["graders"][g] = stats
    return out


def report(run_id: str, publish: bool = False) -> Path:
    d, s = run_dir(run_id), summarize(run_id)
    recs, scores = read_jsonl(d / "responses.jsonl"), read_jsonl(d / "scores.jsonl")
    agents = list(s["agents"])
    sku = s["meta"].get("capacity_sku")
    lines = [f"# Data agent evaluation: run {run_id}", "",
             "Sample, not production guidance. Synthetic data. Generated by `py eval/agent_eval.py report`."
             + (f" Capacity: {sku}." if sku else ""), "",
             "## Agents (rules = reference grader)", "",
             "Weighted = impact-weighted accuracy (critical 3, high 2, normal 1). Query = SQL/KQL execution inside the "
             "agent (run steps, 1 s resolution); the rest of the latency is AI orchestration.", "",
             "| Agent | Accuracy | Weighted | Completed | p50 latency | p95 latency | p50 query | Prompt tokens | "
             "PII failures |", "|---|---|---|---|---|---|---|---|---|"]
    for a, v in s["agents"].items():
        lines.append(f"| `{a}` | {_pct(v['accuracy'])} | {_pct(v['weighted'])} | {v['completed']}/{v['n']} | "
                     f"{_u(v['p50_s'], 's')} | {_u(v['p95_s'], 's')} | {_u(v['query_p50_s'], 's')} | {v['prompt_tokens']:,} | "
                     f"{', '.join(v['pii_failures']) or 'none'} |")
    for title, key in (("Accuracy by category", "categories"), ("Accuracy by complexity", "complexity")):
        lines += ["", f"## {title}", "", f"| {title.split()[-1].capitalize()} | " + " | ".join(f"`{a}`" for a in agents)
                  + " |", "|---|" + "---|" * len(agents)]
        for val, v in s[key].items():
            lines.append(f"| {val} | " + " | ".join(_pct(v.get(a)) for a in agents) + " |")
    lines += ["", "## Graders vs rules", "",
              "Agreement and Cohen's kappa against the rule verdict on the same answer. False pass = the grader passed "
              "an answer the rules failed. `fabric_sdk` re-asks the agent, so it is compared with the rules applied to "
              "its own answers.", "",
              "| Grader | n | Pass rate | Agreement | Kappa | False pass | False fail | Errors | p50 latency | Tokens in/out | Cost |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for g, v in s["graders"].items():
        cost = "-" if v["cost_usd"] is None else f"${v['cost_usd']:.4f}"
        lat = "-" if v["p50_ms"] is None else f"{v['p50_ms']:.0f} ms"
        lines.append(f"| {g} | {v['n']} | {_pct(v['pass_rate'])} | {_pct(v['agreement'])} | {v['kappa']} | "
                     f"{v['false_pass']} | {v['false_fail']} | {v['errors']} | {lat} | {v['input_tokens']:,}/"
                     f"{v['output_tokens']:,} | {cost} |")
    lines += ["", "## Per question", "", "| Case | Agent | Rules | Decision | Foundry | Fabric SDK | Detail (rules) |",
              "|---|---|---|---|---|---|---|"]
    idx = {(x["grader"], x["agent"], x["case_id"]): x for x in scores}

    def mark(g: str, a: str, c: str) -> str:
        x = idx.get((g, a, c))
        return "-" if x is None or x.get("pass") is None else ("pass" if x["pass"] else "**fail**")

    for r in recs:
        detail = (idx.get(("rules", r["agent"], r["case_id"])) or {}).get("detail", "")
        lines.append(f"| {r['case_id']} | {r['agent']} | {mark('rules', r['agent'], r['case_id'])} | "
                     f"{mark('decision', r['agent'], r['case_id'])} | {mark('foundry', r['agent'], r['case_id'])} | "
                     f"{mark('fabric_sdk', r['agent'], r['case_id'])} | {detail} |")
    url = d / "foundry_url.txt"
    public = list(lines)
    if url.exists():
        lines += ["", f"Foundry portal: {url.read_text(encoding='utf-8').strip()}"]
        # The portal URL carries subscription and tenant IDs (blocked by tools/check_private_terms.py)
        public += ["", f"Foundry portal: project `agent-eval` > Evaluations > `agent-eval-{run_id}`."]
    path = d / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    print("\n".join(lines[:10 + len(agents)]))
    log(f"report -> {path}")
    if publish:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
        public[2] += (f" Environment `{meta.get('env', '?')}`, commit `{meta.get('git', '?')}`, golden set "
                      f"`{meta.get('golden', GOLDEN.name)}`. Raw answers and generated queries stay in the gitignored "
                      "`eval/runs/`.")
        REPORTS.mkdir(parents=True, exist_ok=True)
        out = REPORTS / f"{run_id}.md"
        out.write_text("\n".join(public) + "\n", encoding="utf-8")
        log(f"published (Git-tracked) -> {out}")
    return path


def latency_report(run_ids: list[str]) -> Path:
    """Capacity sweep: same golden set and agents on different SKUs -> Git-tracked eval/reports/capacity-latency.md."""
    lines = ["# Data agent latency by capacity SKU", "",
             "Sample, not production guidance. Synthetic data. Generated by `py eval/agent_eval.py latency`. Each run asks "
             "the full golden set to every agent (agents in parallel, one question at a time per agent). Query = SQL/KQL "
             "execution inside the agent (run steps, 1 s resolution); Truth = the same engines queried directly "
             "(engine-only control). Accuracy is a sanity check: it should not depend on the SKU.", "",
             "| Run | SKU | Agent | n | Completed | p50 latency | p95 latency | p50 query | p50 truth | Accuracy |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    overall = []
    for rid in run_ids:
        s = summarize(rid)
        sku = s["meta"].get("capacity_sku") or "?"
        recs = read_jsonl(run_dir(rid) / "responses.jsonl")
        lat = [r.get("latency_s") for r in recs if r["status"] == "completed"]
        overall.append((rid, sku, len(recs), sum(r["status"] == "completed" for r in recs),
                        graders.percentile(lat, 0.5), graders.percentile(lat, 0.95),
                        graders.percentile([r.get("query_s") for r in recs if r.get("steps")], 0.5),
                        graders.percentile([r.get("truth_ms") for r in recs], 0.5)))
        for a, v in s["agents"].items():
            lines.append(f"| {rid} | {sku} | `{a}` | {v['n']} | {v['completed']} | {_u(v['p50_s'], 's')} | {_u(v['p95_s'], 's')} | "
                         f"{_u(v['query_p50_s'], 's')} | {_u(v['truth_p50_ms'], 'ms')} | {_pct(v['accuracy'])} |")
    lines += ["", "## All agents", "", "| Run | SKU | Answers | Completed | p50 latency | p95 latency | p50 query | "
              "p50 truth |", "|---|---|---|---|---|---|---|---|"]
    for rid, sku, n, done, p50, p95, q50, t50 in overall:
        lines.append(f"| {rid} | {sku} | {n} | {done} | {_u(p50, 's')} | {_u(p95, 's')} | {_u(q50, 's')} | {_u(t50, 'ms')} |")
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / "capacity-latency.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[-len(overall) - 2:]))
    log(f"latency report (Git-tracked) -> {path}")
    return path


def gate(run_id: str, agents: list[str] | None, min_accuracy: float) -> None:
    s = summarize(run_id)
    failures = []
    for a, v in s["agents"].items():
        if agents and a not in agents:
            continue
        if v["accuracy"] is None:
            failures.append(f"{a}: not scored with rules")
        elif v["accuracy"] < min_accuracy:
            failures.append(f"{a}: accuracy {v['accuracy']:.0%} < {min_accuracy:.0%}")
        if v["pii_failures"]:
            failures.append(f"{a}: PII leaked in {', '.join(v['pii_failures'])}")
    if failures:
        raise SystemExit("gate FAILED\n  " + "\n  ".join(failures))
    log(f"gate passed (min accuracy {min_accuracy:.0%}, no PII leaks)")


def main() -> None:
    import warnings
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    warnings.filterwarnings("ignore", category=DeprecationWarning)  # Fabric data agents expose the Assistants protocol
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["collect", "score", "fabric", "report", "gate", "all", "latency"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    p.add_argument("--agents", default=",".join(DEFAULT_AGENTS))
    p.add_argument("--cases", help="comma-separated golden case ids (default: all)")
    p.add_argument("--run", help="run id (default: new for collect/all, latest otherwise)")
    p.add_argument("--graders", default=",".join(ALL_GRADERS))
    p.add_argument("--no-upload", action="store_true", help="do not log the Foundry evaluation to the project")
    p.add_argument("--publish", action="store_true", help="also write a sanitized, Git-tracked eval/reports/<run>.md")
    p.add_argument("--min-accuracy", type=float, default=0.7)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--warmup", type=int, default=0, help="collect: discarded calls per agent first (after a resize)")
    p.add_argument("--runs", help="latency: comma-separated run ids (one per capacity SKU)")
    a = p.parse_args()
    load_env() if (ROOT / ".env").exists() else None  # CI passes settings as environment variables
    if a.action == "latency":
        if not a.runs:
            raise SystemExit("latency requires --runs")
        latency_report([x for x in a.runs.split(",") if x])
        return
    agents = [x for x in a.agents.split(",") if x]
    which = [x for x in a.graders.split(",") if x]
    if a.action in ("collect", "all"):
        run_id = collect(a.env, agents, a.cases.split(",") if a.cases else None, a.run, a.workers, a.warmup)
    else:
        run_id = a.run or latest_run()
    if a.action in ("score", "all"):
        score(run_id, which, upload=not a.no_upload)
    if a.action == "fabric":
        fabric_sdk(a.env, run_id)
    if a.action in ("report", "all"):
        report(run_id, publish=a.publish)
    if a.action in ("gate", "all"):
        gate(run_id, agents, a.min_accuracy)


if __name__ == "__main__":
    main()
