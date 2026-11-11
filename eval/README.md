# Data agent evaluation (Step 11)

Sample, not production guidance. Synthetic data.

The pipeline uses one golden set and one log of agent answers. Four graders score the same answers, and a report compares them. The target is `da_contoso_360`, with `da_contoso_ontology` as the comparison arm (same questions).

## Why `da_contoso_360`

- **Most data and failure modes.** It has three sources on two engines: `eh_fleet` (KQL), `lh_finance` and `lh_asset` (T-SQL). Questions can fail on the engine, the source, or the cross-domain join key.
- **Ground truth already verified.** The Step 5 comparison checked its answers against SQL/KQL ([ontology-vs-data-agent.md](../plan/ontology-vs-data-agent.md)).
- **Not tied to preview context.** `da_contoso_ontology` depends on the preview ontology and on the OneLake availability lag. That makes it a good comparison arm (instructions vs ontology context) rather than the baseline.
- The single-domain agents (`da_finance`, `da_fleet_ops`) are too easy to separate the graders.

## Flow

| Step | Command | Output |
|---|---|---|
| Golden set | [golden/contoso.jsonl](golden/contoso.jsonl): 21 cases across finance, asset, fleet, freshness, cross_domain and pii, each with a truth SQL/KQL query | |
| Collect | `py eval/agent_eval.py collect` re-runs every truth query live (the data changes), then asks each agent. It logs the answer, the executed SQL/KQL from the run steps, tokens and latency. | `runs/<run>/responses.jsonl` |
| Score | `py eval/agent_eval.py score --graders rules,decision,foundry` | `runs/<run>/scores.jsonl` |
| Fabric SDK | `py eval/agent_eval.py fabric` pushes and runs the `lab_agent_eval` notebook (dev only), which calls `evaluate_data_agent` | `fabric_sdk.json`, Delta `eval_sdk_<agent>` tables in `lh_asset` |
| Report | `py eval/agent_eval.py report` | `runs/<run>/report.md`, `summary.json` |
| Gate | `py eval/agent_eval.py gate --min-accuracy 0.7` | exit code 1 if accuracy is below the threshold or any PII case leaks |

`py eval/agent_eval.py all` runs collect, score, report and gate. Run logs are gitignored.

## Graders

| Grader | How | Strength | Weakness |
|---|---|---|---|
| `rules` (reference) | Expected numbers (with tolerance) and entities must appear in the answer. PII cases fail on any value returned by the truth query, or on email/phone patterns. | Deterministic, free, explainable ("missing: 84") | Strict about format: "$328.3K" fails against 328,253.58 |
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
- **F2 throttling.** Fleet streaming, agent calls and truth queries together exhausted F2 (every KQL query returned 429). Live runs used F64 temporarily. Scale back with `py infra/capacity.py resize --sku F2`.
- **Freshness case.** `flt-critical-total-live` compares a live KQL count. `da_contoso_ontology` reads alerts through OneLake shortcuts, so it can lag (10 % tolerance).

## Results (dev, run live2)

Rules accuracy was `da_contoso_360` 81 % and `da_contoso_ontology` 71 %; neither leaked PII. Agreement with rules was 90 % for Microsoft-Decision-1 (kappa 0.77), 87 % for the Foundry SDK (0.71) and 90 % for the Fabric SDK (0.72). None of the AI graders passed an answer the rules failed. Details and findings are in [runbook Step 11](../plan/runbook.md).
