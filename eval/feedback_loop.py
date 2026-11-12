"""Step 11 live2.5: evaluation feedback loop. Log to Eventhouse -> diagnose -> propose -> guardrails -> candidate -> accept.

Arms (dev only, ontology AND source data): A = lab_da_360_onto, B = lab_da_onto_hub_bare. v0 stays frozen; each round
publishes lab_<v0>_c<round> and evaluates head and candidate in the SAME run (paired). Only lab_ heads advance.
Golden split: train (proposals come from here), validation (accept/reject), test (scored once, at the end).

  py eval/feedback_loop.py run      [--rounds 3] [--arms A,B]   # r0 baseline, rounds, final test; logs everything
  py eval/feedback_loop.py log      --run <id> [--arm-map agent=arm,...]   # (re)log a local run to Eventhouse
  py eval/feedback_loop.py report   [--loop <id>]               # eval/reports/live2.5.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra"))
sys.path.insert(0, str(ROOT / "eval"))

import agent_eval as ae  # noqa: E402
import graders  # noqa: E402
import optimizer as opt  # noqa: E402
from common import env, load_env, log  # noqa: E402

ARMS = {"A": "lab_da_360_onto", "B": "lab_da_onto_hub_bare"}
GRADERS = ["rules", "decision"]
STATE = ae.RUNS / "loop_state.json"
FORBID_CATS = ("pii", "pii_indirect", "injection")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def kq():
    import agent_eval_kql
    return agent_eval_kql


# ----------------------------------------------------------------- loop events (visible timeline)
class Events:
    def __init__(self, e: str, loop_id: str):
        self.e, self.loop_id = e, loop_id

    def __call__(self, arm: str, rnd: int, stage: str, status: str, detail: str = "", agent: str = "", run_id: str = ""):
        log(f"[loop {arm} r{rnd}] {stage}: {status} {detail[:160]}")
        try:
            kq().ingest(self.e, "loop_events", [{"loop_id": self.loop_id, "arm": arm, "round": rnd, "stage": stage,
                                                 "status": status, "detail": detail[:4000], "agent": agent,
                                                 "run_id": run_id, "ts": now()}])
        except (Exception, SystemExit) as ex:  # noqa: BLE001 - the timeline must not stop the loop
            log(f"loop_events ingest failed: {str(ex)[:120]}")


# ----------------------------------------------------------------- logging a run to Eventhouse
def register_version(e: str, agent: str, arm: str, parent: str, source: str) -> tuple[str, dict, dict]:
    from data_agents import get_definition, published_view, version_of
    defn = get_definition(e, agent)
    view = published_view(defn)
    version = version_of(view)
    kq().ingest(e, "agent_versions", [{
        "agent": agent, "arm": arm, "version": version, "parent_version": parent, "instructions": view["instructions"],
        "datasource_instructions": {k: v["instructions"] for k, v in view["sources"].items()},
        "fewshots": {k: v["fewshots"] for k, v in view["sources"].items()}, "source": source, "created_at": now()}])
    return version, defn, view


def log_run(e: str, run_id: str, arms: dict[str, str], versions: dict[str, str]) -> dict:
    """Ingest one local run (responses + scores) into lab_agent_eval. arms/versions: agent -> arm / version."""
    d = ae.run_dir(run_id)
    recs, scores = ae.read_jsonl(d / "responses.jsonl"), ae.read_jsonl(d / "scores.jsonl")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").exists() else {}
    golden = {c["id"]: c for c in ae.load_cases()}
    split = lambda r: r.get("split") or golden.get(r["case_id"], {}).get("split", "")  # noqa: E731 (older runs)
    answers, steps = [], []
    for r in recs:
        calls = r.get("tool_calls") or [{"seq": i, "tool": "analyze.database.execute", "datasource": s.get("datasource"),
                                         "type": s.get("type"), "code": s.get("code"), "duration_s": s.get("duration_s")}
                                        for i, s in enumerate(r.get("steps", [])) if "error" not in s]
        used = sorted({c["datasource"] for c in calls if c.get("datasource") and c["tool"] in
                       ("analyze.database.execute", "analyze.database.nl2code")})
        a, v = r["agent"], versions.get(r["agent"], "")
        answers.append({"run_id": run_id, "agent": a, "arm": arms.get(a, ""), "agent_version": v, "case_id": r["case_id"],
                        "category": r["category"], "complexity": r.get("complexity", ""), "weight": r.get("weight", 1),
                        "split": split(r), "question": r["question"], "expected_answer": r.get("expected_answer", ""),
                        "answer": (r.get("answer") or "")[:8000], "status": r["status"], "latency_s": r.get("latency_s"),
                        "query_s": r.get("query_s"), "prompt_tokens": r.get("prompt_tokens"),
                        "completion_tokens": r.get("completion_tokens"), "sources_used": used,
                        "steps": [{k: (s.get(k) or "")[:2000] if k in ("code", "output") else s.get(k)
                                   for k in ("datasource", "type", "code", "output", "duration_s")}
                                  for s in r.get("steps", [])], "asked_at": r.get("asked_at")})
        steps += [{"run_id": run_id, "agent": a, "arm": arms.get(a, ""), "agent_version": v, "case_id": r["case_id"],
                   "category": r["category"], "seq": c.get("seq", 0), "tool": c.get("tool"), "datasource": c.get("datasource"),
                   "datasource_type": c.get("type"), "nl_query": (c.get("nl_query") or "")[:2000],
                   "code": (c.get("code") or "")[:4000], "duration_s": c.get("duration_s"),
                   "output_chars": c.get("output_chars"), "error": c.get("error"), "asked_at": r.get("asked_at")}
                  for c in calls]
    by_key = {(r["agent"], r["case_id"]): r for r in recs}
    rows = []
    for s in scores:
        r = by_key.get((s["agent"], s["case_id"]), {})
        rows.append({"run_id": run_id, "agent": s["agent"], "arm": arms.get(s["agent"], ""),
                     "agent_version": versions.get(s["agent"], ""), "case_id": s["case_id"], "category": s["category"],
                     "split": split(r) if r else "", "weight": r.get("weight", 1), "grader": s["grader"],
                     "pass": s.get("pass"), "score": s.get("score"), "detail": (s.get("detail") or "")[:1000],
                     "input_tokens": s.get("input_tokens") or 0, "output_tokens": s.get("output_tokens") or 0,
                     "logged_at": now()})
    k = kq()
    k.ingest(e, "eval_runs", [{"run_id": run_id, "env": meta.get("env", e), "capacity_sku": meta.get("capacity_sku"),
                               "git": meta.get("git"), "golden": meta.get("golden"), "agents": meta.get("agents"),
                               "logged_at": now()}])
    counts = {"answers": k.ingest(e, "eval_answers", answers, batch=10), "steps": k.ingest(e, "eval_steps", steps, batch=50),
              "scores": k.ingest(e, "eval_scores", rows, batch=50)}
    log(f"logged {run_id}: {counts}")
    return counts


# ----------------------------------------------------------------- diagnose / propose / candidate
def train_failures(e: str, run_id: str, agent: str) -> list[dict]:
    """From Eventhouse: the agent's failed TRAIN questions with its generated query and the grader detail."""
    q = (f"eval_scores | where run_id == '{run_id}' and agent == '{agent}' and grader == 'rules' and split == 'train' "
         "and not(pass) | project case_id, detail "
         f"| join kind=inner (eval_answers | where run_id == '{run_id}' and agent == '{agent}' "
         "| project case_id, question, answer, expected_answer, steps) on case_id "
         "| project case_id, question, answer, expected_answer, detail, query = tostring(steps[0].code)")
    return kq().query(e, q)


def executor(e: str, view: dict):
    truth = ae.Truth(e)

    def run(source: str, query: str) -> int:
        """Execute a few-shot query; return its row count."""
        from fabric_rest import kql, tsql
        typ = view["sources"][source]["type"]
        if typ == "kusto":
            uri, db = truth._kql()
            return len(kql(uri, db, query))
        if typ == "lakehouse_tables":
            return len(tsql(truth._host(source), source, query))
        raise ValueError(f"few-shots are not supported on {typ} sources")
    return run


def optimizer_client():
    from azure.identity import AzureCliCredential, get_bearer_token_provider
    from openai import AzureOpenAI
    tp = get_bearer_token_provider(AzureCliCredential(), "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(azure_endpoint=env("FOUNDRY_ENDPOINT"), azure_ad_token_provider=tp, api_version="2024-10-21")


def improve(e: str, ev: Events, arm: str, rnd: int, head: str, defn: dict, view: dict, failures: list[dict],
            golden: list[dict]) -> dict:
    """Propose with guardrails (up to 3 attempts). Returns {"ok", "proposal", "problems", "attempts", "tokens", ...}."""
    sources = [{"name": n, "type": s["type"], "instructions": s["instructions"], "schema": "; ".join(s["tables"])[:3000]}
               for n, s in view["sources"].items()]
    prompt = opt.build_prompt(view["instructions"], sources, failures)
    client, dep = optimizer_client(), env("FOUNDRY_OPTIMIZER_DEPLOYMENT")
    feedback, tokens = None, [0, 0]
    src_instr = {n: s["instructions"] for n, s in view["sources"].items()}
    for attempt in range(1, 4):
        p = opt.propose(client, dep, prompt, feedback)
        tokens = [tokens[0] + p["usage"]["prompt_tokens"], tokens[1] + p["usage"]["completion_tokens"]]
        new_i, new_s = opt.apply(view["instructions"], src_instr, p)
        problems = opt.check(p, view["instructions"], new_i, new_s, golden, set(view["sources"]), executor(e, view))
        ev(arm, rnd, "guardrails", "pass" if not problems else "reject",
           f"attempt {attempt}: " + ("; ".join(problems) if problems else
                                     f"{len(p['instructions_add'])} instruction(s), {len(p['fewshots'])} few-shot(s)"), head)
        if not problems:
            return {"ok": True, "proposal": p, "problems": [], "attempts": attempt, "tokens": tokens,
                    "instructions": new_i, "source_instructions": new_s}
        feedback = problems
    return {"ok": False, "proposal": p, "problems": problems, "attempts": 3, "tokens": tokens}


def split_stats(run_id: str, agent: str) -> dict:
    recs = {r["case_id"]: r for r in ae.read_jsonl(ae.run_dir(run_id) / "responses.jsonl") if r["agent"] == agent}
    rules = {s["case_id"]: bool(s["pass"]) for s in ae.read_jsonl(ae.run_dir(run_id) / "scores.jsonl")
             if s["agent"] == agent and s["grader"] == "rules"}
    out = {"train": [0, 0], "validation": [0, 0], "test": [0, 0], "forbid_fail": 0, "categories": {}}
    for cid, r in recs.items():
        sp, ok = r.get("split", "train"), rules.get(cid, False)
        out[sp][0] += ok
        out[sp][1] += 1
        if sp != "test":
            out["categories"][r["category"]] = out["categories"].get(r["category"], 0) + ok
        if r["category"] in FORBID_CATS and not ok:
            out["forbid_fail"] += 1
    for k in ("train", "validation", "test"):
        out[k] = tuple(out[k])
    return out


LIMIT = 0  # smoke tests: first N questions per split (0 = all)


def collect_and_grade(e: str, run_id: str, agents: list[str], splits: tuple[str, ...]) -> None:
    cases = ae.load_cases()
    ids = [c["id"] for sp in splits for c in [x for x in cases if x.get("split") == sp][:LIMIT or None]]
    ae.collect(e, agents, ids, run_id, workers=len(agents))
    ae.score(run_id, GRADERS, upload=False)


# ----------------------------------------------------------------- the loop
def run_loop(e: str, rounds: int, arm_ids: list[str]) -> str:
    from data_agents import delete_agent, publish_candidate
    loop_id = "live2.5-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ev = Events(e, loop_id)
    golden = [{"question": c["question"], "expected_answer": c.get("answer", "")} for c in ae.load_cases()]
    truth = ae.Truth(e)  # expected answers with real values for the leakage check
    for c, g in zip(ae.load_cases(), golden):
        try:
            g["expected_answer"] = graders.expected_from_truth(c, truth.rows(c))[1]
        except (Exception, SystemExit):  # noqa: BLE001
            pass
    state = {"loop_id": loop_id, "sku": ae.capacity_sku(), "arms": {}, "rounds": []}
    versions, arm_of = {}, {}
    for a in arm_ids:
        v0 = ARMS[a]
        ver, _, _ = register_version(e, v0, a, "", "v0")
        state["arms"][a] = {"v0": v0, "v0_version": ver, "head": v0, "head_version": ver, "rejections": 0, "done": False,
                            "history": []}
        versions[v0], arm_of[v0] = ver, a
    ev("all", 0, "loop", "start", f"arms {', '.join(ARMS[a] for a in arm_ids)}; {rounds} round(s); sku {state['sku']}")

    run_id = f"{loop_id}-r0"
    ev("all", 0, "collect", "start", "baseline on train + validation", run_id=run_id)
    collect_and_grade(e, run_id, [ARMS[a] for a in arm_ids], ("train", "validation"))
    log_run(e, run_id, arm_of, versions)
    for a in arm_ids:
        s = split_stats(run_id, ARMS[a])
        state["arms"][a]["last_run"] = run_id
        state["arms"][a]["history"].append({"round": 0, "run_id": run_id, "head": ARMS[a], "stats": s})
        ev(a, 0, "baseline", "done", f"train {s['train'][0]}/{s['train'][1]}, validation {s['validation'][0]}/"
                                    f"{s['validation'][1]}", ARMS[a], run_id)
    _save(state)

    for rnd in range(1, rounds + 1):
        candidates = {}
        for a in arm_ids:
            st = state["arms"][a]
            if st["done"]:
                continue
            head = st["head"]
            fails = train_failures(e, st["last_run"], head)
            ev(a, rnd, "diagnose", "done", f"{len(fails)} train failure(s): " + ", ".join(f["case_id"] for f in fails),
               head, st["last_run"])
            if not fails:
                st["done"] = True
                ev(a, rnd, "stop", "no train failures", agent=head)
                continue
            from data_agents import get_definition, published_view
            defn = get_definition(e, head)
            view = published_view(defn)
            res = improve(e, ev, a, rnd, head, defn, view, fails, golden)
            pid = str(uuid.uuid4())
            if not res["ok"]:
                st["rejections"] += 1
                _improvement(e, pid, a, rnd, st["last_run"], head, st["head_version"], "", "", res, None, None,
                             "guardrail_rejected", "; ".join(res["problems"])[:2000])
                ev(a, rnd, "decision", "guardrail_rejected", "; ".join(res["problems"]), head)
                st["done"] = st["rejections"] >= 2
                continue
            cand = f"{st['v0']}_c{rnd}"
            publish_candidate(e, defn, cand, res["instructions"], res["source_instructions"], res["proposal"]["fewshots"],
                              f"live2.5 candidate {a} round {rnd} (from {head})")
            ver, _, _ = register_version(e, cand, a, st["head_version"], f"optimizer round {rnd}")
            versions[cand], arm_of[cand] = ver, a
            candidates[a] = (cand, ver, pid, res)
            ev(a, rnd, "candidate", "published", f"{cand} version {ver}", cand)
        if not candidates:
            ev("all", rnd, "loop", "stop", "no candidates this round")
            break
        run_id = f"{loop_id}-r{rnd}"
        agents = sorted({x for a, c in candidates.items() for x in (state["arms"][a]["head"], c[0])})
        ev("all", rnd, "collect", "start", f"paired: {', '.join(agents)}", run_id=run_id)
        collect_and_grade(e, run_id, agents, ("train", "validation"))
        log_run(e, run_id, arm_of, versions)
        for a in arm_ids:
            st = state["arms"][a]
            if a not in candidates:
                continue
            cand, ver, pid, res = candidates[a]
            base_s, cand_s = split_stats(run_id, st["head"]), split_stats(run_id, cand)
            ok, why = opt.accept({**base_s}, {**cand_s})
            _improvement(e, pid, a, rnd, run_id, st["head"], st["head_version"], cand, ver, res, base_s, cand_s,
                         "accepted" if ok else "rejected", why)
            st["history"].append({"round": rnd, "run_id": run_id, "head": st["head"], "candidate": cand,
                                  "base": base_s, "cand": cand_s, "accepted": ok, "reason": why,
                                  "proposal": res["proposal"], "optimizer_tokens": res["tokens"]})
            if ok:
                st.update(head=cand, head_version=ver, rejections=0, last_run=run_id)
                ev(a, rnd, "decision", "accepted", why, cand, run_id)
            else:
                st["rejections"] += 1
                st["last_run"] = run_id
                delete_agent(e, cand)
                ev(a, rnd, "decision", "rejected", why + "; candidate deleted", cand, run_id)
                st["done"] = st["rejections"] >= 2
        state["rounds"].append(run_id)
        _save(state)
        if all(state["arms"][a]["done"] for a in arm_ids):
            break

    finalize(e, state, ev, arm_of, versions, arm_ids)
    return loop_id


def finalize(e: str, state: dict, ev: Events, arm_of: dict, versions: dict, arm_ids: list[str]) -> None:
    """Final step: v0 and final head of each arm on ALL splits; the test split is scored only here."""
    run_id = f"{state['loop_id']}-final"
    finals = sorted({state["arms"][a][k] for a in arm_ids for k in ("v0", "head")})
    ev("all", 99, "collect", "start", f"final on all splits incl. test: {', '.join(finals)}", run_id=run_id)
    collect_and_grade(e, run_id, finals, ("train", "validation", "test"))
    log_run(e, run_id, arm_of, versions)
    state["final_run"] = run_id
    for a in arm_ids:
        st = state["arms"][a]
        st["final"] = {"v0": split_stats(run_id, st["v0"]), "head": split_stats(run_id, st["head"])}
        f = st["final"]
        ev(a, 99, "final", "done", f"test v0 {f['v0']['test'][0]}/{f['v0']['test'][1]} -> head "
                                   f"{f['head']['test'][0]}/{f['head']['test'][1]} ({st['head']})", st["head"], run_id)
    _save(state)
    ev("all", 99, "loop", "end", state["loop_id"])


def resume_final(e: str) -> None:
    """Re-run only the final step of the saved loop (e.g. after the runner was interrupted)."""
    state = json.loads(STATE.read_text(encoding="utf-8"))
    arm_ids = list(state["arms"])
    arm_of, versions = {}, {}
    for a, st in state["arms"].items():
        arm_of[st["v0"]], versions[st["v0"]] = a, st["v0_version"]
        arm_of[st["head"]], versions[st["head"]] = a, st["head_version"]
    ev = Events(e, state["loop_id"])
    ev("all", 99, "loop", "resume", "final step re-run after the runner was interrupted")
    finalize(e, state, ev, arm_of, versions, arm_ids)


def _improvement(e, pid, arm, rnd, run_id, base, base_ver, cand, cand_ver, res, base_s, cand_s, decision, reason):
    frac = lambda s, k: round(s[k][0] / s[k][1], 3) if s and s[k][1] else None  # noqa: E731
    kq().ingest(e, "improvements", [{
        "proposal_id": pid, "arm": arm, "round": rnd, "run_id": run_id, "base_agent": base, "base_version": base_ver,
        "candidate_agent": cand, "candidate_version": cand_ver, "proposal": res["proposal"],
        "guardrails": res["problems"], "attempts": res["attempts"], "optimizer_prompt_tokens": res["tokens"][0],
        "optimizer_completion_tokens": res["tokens"][1], "train_base": frac(base_s, "train"),
        "train_cand": frac(cand_s, "train"), "validation_base": frac(base_s, "validation"),
        "validation_cand": frac(cand_s, "validation"), "forbid_fail_cand": cand_s["forbid_fail"] if cand_s else None,
        "decision": decision, "reason": reason, "decided_at": now()}])


def _save(state: dict) -> None:
    ae.RUNS.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")


def main() -> None:
    import warnings
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["run", "log", "report", "finalize"])
    p.add_argument("--env", choices=["dev"], default="dev")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--arms", default="A,B")
    p.add_argument("--run")
    p.add_argument("--arm-map", default="", help="log: agent=arm,... (default: none)")
    p.add_argument("--limit", type=int, default=0, help="smoke test: first N questions per split")
    a = p.parse_args()
    load_env()
    global LIMIT
    LIMIT = a.limit
    if a.action == "run":
        print(run_loop(a.env, a.rounds, [x for x in a.arms.split(",") if x]))
    elif a.action == "finalize":
        resume_final(a.env)
    elif a.action == "log":
        arms = dict(x.split("=", 1) for x in a.arm_map.split(",") if "=" in x)
        log_run(a.env, a.run, arms, {})
    else:
        import loop_report
        loop_report.write(json.loads(STATE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    main()
