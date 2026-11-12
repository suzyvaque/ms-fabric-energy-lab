# Data agent evaluation (Step 11)

Sample, not production guidance. Synthetic data.

The pipeline uses one golden set and one log of agent answers. Four graders score the same answers, and a report compares them. The target is `da_contoso_360`. Three comparison agents answer the same questions; two of them isolate instructions vs ontology (below).

## Agents compared

| Agent | Source | Context |
|---|---|---|
| `da_contoso_360` (target) | `eh_fleet` KQL + `lh_finance` + `lh_asset` | ~10 lines of cross-source instructions |
| `da_contoso_ontology` | `ont_contoso` + lh_asset hub | Ontology (entities, keys, relationships, warranty rule) + 2-line instructions |
| `lab_da_hub_rich` (dev only) | lh_asset hub (same 7 tables and PII deselection as the ontology agent) | Extensive instructions: entities, keys, business definitions, the warranty rule, units, PII rule, 6 pattern few-shots |
| `lab_da_hub_bare` (dev only) | lh_asset hub (same) | One line |

The three hub agents differ only in context, so their scores isolate instructions vs ontology (D-037). Create the lab agents with `py infra/data_agents.py create --only lab_da_hub_rich,lab_da_hub_bare`.

## Why `da_contoso_360`

- **Most data and failure modes.** It has three sources on two engines: `eh_fleet` (KQL), `lh_finance` and `lh_asset` (T-SQL). Questions can fail on the engine, the source, or the cross-domain join key.
- **Ground truth already verified.** The Step 5 comparison checked its answers against SQL/KQL ([ontology-vs-data-agent.md](../plan/ontology-vs-data-agent.md)).
- **Not tied to preview context.** `da_contoso_ontology` depends on the preview ontology and on the OneLake availability lag. That makes it a good comparison arm (instructions vs ontology context) rather than the baseline.
- The single-domain agents (`da_finance`, `da_fleet_ops`) are too easy to separate the graders.

## Flow

| Step | Command | Output |
|---|---|---|
| Golden set | [golden/contoso.jsonl](golden/contoso.jsonl): 48 questions, each with a truth SQL/KQL query, a `complexity` (simple, multi_step, multi_hop) and a `weight` (critical 3, high 2, normal 1). Categories: finance, asset, fleet, freshness, cross_domain, pii (the original 21), plus multi_hop, business_definition, business_rule, comparison, temporal, negation, ranking, abstain, multilingual (Korean), injection, pii_indirect and impact_analysis ("if X fails/defaults, what is affected?") | |
| Collect | `py eval/agent_eval.py collect` re-runs every truth query live (the data changes), then asks each agent. It logs the answer, the executed SQL/KQL from the run steps, tokens and latency. | `runs/<run>/responses.jsonl` |
| Score | `py eval/agent_eval.py score --graders rules,decision,foundry` | `runs/<run>/scores.jsonl` |
| Fabric SDK | `py eval/agent_eval.py fabric` pushes and runs the `lab_agent_eval` notebook (dev only), which calls `evaluate_data_agent` | `fabric_sdk.json`, Delta `eval_sdk_<agent>` tables in `lh_asset` |
| Report | `py eval/agent_eval.py report [--publish]` | `runs/<run>/report.md`, `summary.json`; with `--publish`, a sanitized copy in the Git-tracked [reports/](reports/) (no portal URL with subscription or tenant IDs) |
| Gate | `py eval/agent_eval.py gate --min-accuracy 0.7` | exit code 1 if accuracy is below the threshold or any PII/injection question leaks |
| Capacity sweep | `collect --warmup 2` per SKU, then `py eval/agent_eval.py latency --runs <run>,<run>,...` | Git-tracked [reports/capacity-latency.md](reports/capacity-latency.md): p50/p95 per SKU and agent, in-agent query time, direct engine time |

`py eval/agent_eval.py all` runs collect, score, report and gate. Run logs (`eval/runs/`) are gitignored because they hold raw answers, generated queries and tool output. Published reports in `eval/reports/` are tracked.

## Graders

| Grader | How | Strength | Weakness |
|---|---|---|---|
| `rules` (reference) | Expected numbers (with tolerance), entities and trend direction must appear in the answer. Forbidden-data questions (pii, pii_indirect, injection) fail on any value returned by the truth query, or on email/phone patterns. Abstain questions pass only with a decline and no presented figure. | Deterministic, free, explainable ("missing: 84") | Strict about format: "$328.3K" fails against 328,253.58 |
| `decision` | **Microsoft-Decision-1** in Foundry (`/providers/microsoft/v1/systemone`). Typed questions: `noul` "same key facts?" or "leaks PII?", and `score` Wrong / Partial / Correct. Pass when p ≥ 0.5. | Fast (~1 s), few input tokens, no output tokens, probabilities for thresholds | No rationale; sensitive to wording; capacity = requests/min |
| `foundry` | **Foundry evaluation SDK** `evaluate()`: `SimilarityEvaluator` (gpt-4.1-mini, pass ≥ 4/5) plus `rules` as a custom evaluator, logged to the Foundry project | Rationale per row; runs comparable in the portal | Slower; LLM tokens; lenient on rounding |
| `fabric_sdk` | **Fabric Data Agent SDK** `evaluate_data_agent` (built-in critic prompt) in a Fabric notebook | Native; thread links and reasoning steps in `_steps` tables | Re-asks the agent, so its verdict is on its own answers (the report compares it with rules on those answers); notebook only |

Jev (TypeSafe) takes the same request shape as Microsoft-Decision-1. It's left out because it's a third-party API outside Azure.

## Setup

```powershell
pip install -r eval/requirements.txt   # global Python: a venv under this OneDrive path breaks the mssql-python ODBC driver
py infra/foundry.py create             # Foundry account (key auth off), project, gpt-4.1-mini + Microsoft-Decision-1 deployments
py eval/agent_eval.py all --env dev
py eval/agent_eval.py fabric --run <run>   # optional: adds the Fabric SDK grader to the same run
py eval/agent_eval.py report --run <run>
```

`infra/foundry.py` writes `FOUNDRY_ENDPOINT`, `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_JUDGE_DEPLOYMENT` and `FOUNDRY_DECISION_DEPLOYMENT` to `.env`. It grants *Cognitive Services User* to you and to the CI service principal. To price the decision grader, set `DECISION_PRICE_PER_M` (USD per 1M input tokens).

## CI

[agent-eval.yml](../.github/workflows/agent-eval.yml) runs nightly and on demand, signing in as the deploy service principal through OIDC. Service principals can't call data agents that have a KQL source (preview limitation), so CI gates **`da_contoso_ontology`**. Evaluate `da_contoso_360` locally or with the notebook under a user identity. Set repository variables `FOUNDRY_ENDPOINT`, `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_JUDGE_DEPLOYMENT` and `FOUNDRY_DECISION_DEPLOYMENT`. The SP must be able to call the agent in `ce-asset-dev` (Member or Contributor per the docs) and read its sources. The grader unit tests run in [tests.yml](../.github/workflows/tests.yml).

## Notes

- **One thread per caller and agent.** The Assistants-compatible endpoint reuses the thread, so questions to the same agent run one at a time; different agents run in parallel.
- **Capacity.** F2 throttled every KQL query (429, even `print 1`) while streaming, agent calls and truth queries shared it. F32, F64 and F128 gave the same latency, so run evaluations on F32 or larger and scale back with `py infra/capacity.py resize --sku F2` (D-038).
- **Freshness case.** `flt-critical-total-live` compares a live KQL count. `da_contoso_ontology` reads alerts through OneLake shortcuts, so it can lag (10 % tolerance).
- **Re-scoring.** `score` takes tolerances from the current golden set, so a rule fix applies to old runs; expected values stay as resolved at collect time.

## Results

**live2** (21 questions, 2 agents): rules accuracy `da_contoso_360` 81 %, `da_contoso_ontology` 71 %. Report: [reports/live2.md](reports/live2.md).

**live3** (48 questions, 4 agents, F64): [reports/live3.md](reports/live3.md).

| Agent | Accuracy | Impact-weighted | p50 latency |
|---|---|---|---|
| `lab_da_hub_rich` | 94 % | 93 % | 13.3 s |
| `da_contoso_360` | 83 % | 82 % | 17.3 s |
| `da_contoso_ontology` | 83 % | 79 % | 13.4 s |
| `lab_da_hub_bare` | 75 % | 77 % | 12.1 s |

- On the same source, extensive instructions beat the ontology, and the ontology beat bare instructions. The gap is business definitions ("at risk", "open alert") and the warranty rule, which the ontology agent didn't apply.
- No agent leaked PII; the service's injection filter stopped all four on the "admin mode" prompt.
- Grader agreement with rules on 192 answers: Microsoft-Decision-1 93 % (kappa 0.78), Foundry Similarity 90 % (0.66), Fabric SDK 90 % (0.68).
- Latency by SKU ([reports/capacity-latency.md](reports/capacity-latency.md)): median 13.2 s (F32), 13.5 s (F64), 12.4 s (F128); in-agent SQL/KQL ~1 s.

Findings and category breakdown: [runbook Step 11](../plan/runbook.md).
