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
  py eval/agent_eval.py report  --run <id>
  py eval/agent_eval.py gate    --run <id> [--agents ...] [--min-accuracy 0.7]
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

RUNS = ROOT / "eval" / "runs"
GOLDEN = ROOT / "eval" / "golden" / "contoso.jsonl"
NB_SOURCE = ROOT / "eval" / "notebook" / "nb_agent_eval.py"
NOTEBOOK = "lab_agent_eval"  # lab_ = dev-only, not Git-tracked or deployed (common.UNMANAGED_ITEMS)
DEFAULT_AGENTS = ["da_contoso_360", "da_contoso_ontology"]
ALL_GRADERS = ["rules", "decision", "foundry"]
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
def collect(e: str, agents: list[str], case_ids: list[str] | None, run_id: str | None, workers: int = 4) -> str:
    from data_agents import AGENTS, ask_run
    unknown = set(agents) - set(AGENTS)
    if unknown:
        raise SystemExit(f"unknown agent(s): {', '.join(unknown)}")
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cases, truth = load_cases(case_ids), Truth(e)
    log(f"run {run_id}: resolving {len(cases)} truth queries")
    expected = {}
    for c in cases:
        try:
            exp, text = graders.expected_from_truth(c, truth.rows(c))
        except (Exception, SystemExit) as ex:  # noqa: BLE001 - e.g. the CI identity cannot read a truth source
            log(f"truth for {c['id']} unavailable, case skipped: {str(ex)[:160]}")
            continue
        expected[c["id"]] = (exp, text, datetime.now(timezone.utc).isoformat())
    cases = [c for c in cases if c["id"] in expected]
    if not cases:
        raise SystemExit("no case has a resolvable truth")

    def one(job: tuple[str, dict]) -> dict:
        agent, c = job
        exp, text, truth_at = expected[c["id"]]
        base = {"run_id": run_id, "env": e, "agent": agent, "case_id": c["id"], "category": c["category"],
                "question": c["question"], "expected": exp, "expected_answer": text, "check": c.get("check", {}),
                "truth_at": truth_at, "asked_at": datetime.now(timezone.utc).isoformat()}
        try:
            return {**base, **ask_run(e, agent, c["question"], timeout_s=240)}
        except (Exception, SystemExit) as ex:  # noqa: BLE001 - one failed question must not stop the run
            return {**base, "status": "error", "answer": "", "error": f"{type(ex).__name__}: {str(ex)[:300]}"}

    # The endpoint reuses one thread per caller and agent: questions to the same agent must run one at a time
    # ("Can't add messages to thread ... while a run is active"); different agents run in parallel.
    def per_agent(agent: str) -> list[dict]:
        return [one((agent, c)) for c in cases]

    log(f"asking {len(agents) * len(cases)} question(s) ({len(agents)} agent(s) in parallel, sequential per agent)")
    with ThreadPoolExecutor(max(1, min(workers, len(agents)))) as pool:
        records = [r for batch in pool.map(per_agent, agents) for r in batch]
    d = run_dir(run_id)
    write_jsonl(d / "responses.jsonl", records)
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    (d / "meta.json").write_text(json.dumps({"run_id": run_id, "env": e, "agents": agents, "cases": len(cases),
                                             "git": sha, "golden": GOLDEN.name}, indent=2), encoding="utf-8")
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
def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def summarize(run_id: str) -> dict:
    d = run_dir(run_id)
    recs, scores = read_jsonl(d / "responses.jsonl"), read_jsonl(d / "scores.jsonl")
    rules = {(s["agent"], s["case_id"]): s["pass"] for s in scores if s["grader"] == "rules"}
    out: dict = {"agents": {}, "graders": {}, "categories": {}}
    for a in sorted({r["agent"] for r in recs}):
        ar = [r for r in recs if r["agent"] == a]
        verdicts = [rules.get((a, r["case_id"])) for r in ar]
        lat = sorted(r.get("latency_s") or 0 for r in ar if r["status"] == "completed")
        out["agents"][a] = {
            "n": len(ar), "accuracy": sum(bool(v) for v in verdicts) / len(ar) if rules else None,
            "completed": sum(r["status"] == "completed" for r in ar),
            "p50_s": lat[len(lat) // 2] if lat else None, "max_s": lat[-1] if lat else None,
            "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in ar),
            "pii_failures": [r["case_id"] for r, v in zip(ar, verdicts) if r["category"] == "pii" and not v]}
        for cat in sorted({r["category"] for r in ar}):
            cv = [rules.get((a, r["case_id"])) for r in ar if r["category"] == cat]
            out["categories"].setdefault(cat, {})[a] = sum(bool(v) for v in cv) / len(cv)
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


def report(run_id: str) -> Path:
    d, s = run_dir(run_id), summarize(run_id)
    recs, scores = read_jsonl(d / "responses.jsonl"), read_jsonl(d / "scores.jsonl")
    agents = list(s["agents"])
    lines = [f"# Data agent evaluation: run {run_id}", "",
             "Sample, not production guidance. Synthetic data. Generated by `py eval/agent_eval.py report`.", "",
             "## Agents (rules = reference grader)", "",
             "| Agent | Accuracy | Completed | p50 latency | Max latency | Prompt tokens | PII failures |",
             "|---|---|---|---|---|---|---|"]
    for a, v in s["agents"].items():
        lines.append(f"| `{a}` | {_pct(v['accuracy'])} | {v['completed']}/{v['n']} | {v['p50_s']} s | {v['max_s']} s | "
                     f"{v['prompt_tokens']:,} | {', '.join(v['pii_failures']) or 'none'} |")
    lines += ["", "## Accuracy by category", "", "| Category | " + " | ".join(f"`{a}`" for a in agents) + " |",
              "|---|" + "---|" * len(agents)]
    for cat, v in s["categories"].items():
        lines.append(f"| {cat} | " + " | ".join(_pct(v.get(a)) for a in agents) + " |")
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
    if url.exists():
        lines += ["", f"Foundry portal: {url.read_text(encoding='utf-8').strip()}"]
    path = d / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "summary.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    print("\n".join(lines[:4 + 3 + len(agents)]))
    log(f"report -> {path}")
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
    p.add_argument("action", choices=["collect", "score", "fabric", "report", "gate", "all"])
    p.add_argument("--env", choices=["dev", "prod"], default="dev")
    p.add_argument("--agents", default=",".join(DEFAULT_AGENTS))
    p.add_argument("--cases", help="comma-separated golden case ids (default: all)")
    p.add_argument("--run", help="run id (default: new for collect/all, latest otherwise)")
    p.add_argument("--graders", default=",".join(ALL_GRADERS))
    p.add_argument("--no-upload", action="store_true", help="do not log the Foundry evaluation to the project")
    p.add_argument("--min-accuracy", type=float, default=0.7)
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args()
    load_env() if (ROOT / ".env").exists() else None  # CI passes settings as environment variables
    agents = [x for x in a.agents.split(",") if x]
    which = [x for x in a.graders.split(",") if x]
    if a.action in ("collect", "all"):
        run_id = collect(a.env, agents, a.cases.split(",") if a.cases else None, a.run, a.workers)
    else:
        run_id = a.run or latest_run()
    if a.action in ("score", "all"):
        score(run_id, which, upload=not a.no_upload)
    if a.action == "fabric":
        fabric_sdk(a.env, run_id)
    if a.action in ("report", "all"):
        report(run_id)
    if a.action in ("gate", "all"):
        gate(run_id, agents, a.min_accuracy)


if __name__ == "__main__":
    main()
