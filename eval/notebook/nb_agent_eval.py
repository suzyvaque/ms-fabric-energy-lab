# Data agent evaluation with the Fabric Data Agent SDK (preview). Pushed and run by `py eval/agent_eval.py fabric`
# as lab_agent_eval (dev only, not Git-tracked). Reads the golden questions and live expected answers that
# agent_eval.py collect resolved (Files/agent_eval/<run_id>/golden.csv in lh_asset), runs evaluate_data_agent for each
# agent and writes the per-question verdicts to Files/agent_eval/<run_id>/fabric_sdk.json for the comparison report.
# The SDK also writes Delta tables eval_sdk_<agent> (summary) and eval_sdk_<agent>_steps (reasoning steps).

# %% [parameters]
run_id = ""
agents = "da_contoso_360,da_contoso_ontology"

# %%
import traceback

try:
    get_ipython().run_line_magic("pip", "install -U -q fabric-data-agent-sdk")  # noqa: F821 - notebook builtin
    _pip_error = None
except Exception:  # noqa: BLE001
    _pip_error = traceback.format_exc()

# %%
import json
import os

import pandas as pd

folder = f"/lakehouse/default/Files/agent_eval/{run_id}"


def fail(stage, tb):
    """Job runs only report a generic failure: write the traceback next to the inputs for agent_eval.py to show."""
    os.makedirs(folder, exist_ok=True)
    with open(f"{folder}/fabric_sdk_error.txt", "w", encoding="utf-8") as f:
        f.write(f"{stage}\n{tb}")
    raise RuntimeError(f"{stage} failed; see {folder}/fabric_sdk_error.txt")


if _pip_error:
    fail("pip install fabric-data-agent-sdk", _pip_error)
try:
    from fabric.dataagent.evaluation import evaluate_data_agent, get_evaluation_details, get_evaluation_summary
except Exception:  # noqa: BLE001
    fail("import fabric.dataagent.evaluation", traceback.format_exc())

gold = pd.read_csv(f"{folder}/golden.csv")


def pick(columns, *needles):
    """Column names of the SDK output are not part of its documented contract: match loosely."""
    for n in needles:
        for c in columns:
            if n in c.lower():
                return c
    return None


rows, summaries, errors = [], {}, {}
for agent in [a for a in agents.split(",") if a]:
    g = gold[gold.agent == agent].reset_index(drop=True)
    table = f"eval_sdk_{agent}"
    det = None
    # Published (production) first; the SDK also resolves data sources in the sandbox stage, which fails for some
    # preview sources (seen with the ontology source), so fall back to evaluating the sandbox stage (same content here).
    for stage in ("production", "sandbox"):
        try:
            evaluation_id = evaluate_data_agent(g[["question", "expected_answer"]], agent, table_name=table,
                                                data_agent_stage=stage)
            summaries[agent] = {"stage": stage, "summary": get_evaluation_summary(table).to_dict(orient="records")}
            det = get_evaluation_details(evaluation_id, table, get_all_rows=True, verbose=False)
            break
        except Exception:  # noqa: BLE001
            errors.setdefault(agent, []).append(f"[{stage}] {traceback.format_exc()[-1500:]}")
    if det is None:
        continue
    q_col, j_col = pick(det.columns, "question"), pick(det.columns, "judgement", "judgment", "status")
    a_col = pick(det.columns, "actual")
    by_q = dict(zip(g.question, g.case_id))
    for _, r in det.iterrows():
        rows.append({"agent": agent, "case_id": by_q.get(r[q_col]), "evaluation_id": evaluation_id,
                     "judgement": None if j_col is None else str(r[j_col]),
                     "actual_answer": None if a_col is None else str(r[a_col]),
                     "raw": {c: str(r[c])[:2000] for c in det.columns}})
    print(agent, evaluation_id, summaries[agent])

with open(f"{folder}/fabric_sdk.json", "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False)
with open(f"{folder}/fabric_sdk_summary.json", "w", encoding="utf-8") as f:
    json.dump({"summaries": summaries, "errors": errors}, f, ensure_ascii=False, default=str)
if not rows:
    fail("evaluate_data_agent (all agents)", "\n\n".join(e for es in errors.values() for e in es))
print(f"{len(rows)} verdict(s) written to {folder}/fabric_sdk.json; errors for: {', '.join(errors) or 'none'}")
