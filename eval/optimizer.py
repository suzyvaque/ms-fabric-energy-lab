"""Step 11 live2.5: propose data agent improvements from logged failures, with guardrails (pure functions + one LLM call).

The optimizer sees only TRAIN failures (question, the agent's generated query, the rule detail, the expected answer),
the agent's current instructions and its data source schema. It returns JSON:
  {"instructions_add": [str], "datasource_instructions_add": {"<source>": [str]},
   "fewshots": [{"datasource": "<source>", "question": str, "query": str}], "rationale": str}
Guardrails reject a proposal that leaks golden questions or expected values, drops the PII rule, exceeds length limits or
contains few-shot queries that do not execute.
"""
from __future__ import annotations

import difflib
import json
import re

MAX_INSTRUCTIONS = 8000            # characters, aiInstructions after the change
MAX_SOURCE_INSTRUCTIONS = 4000     # characters per data source after the change
MAX_FEWSHOTS_ADDED = 6
SIMILARITY_LIMIT = 0.8             # few-shot question vs any golden question (difflib ratio, normalized text)
_PII_RULE = re.compile(r"never (?:reveal|share|return|disclose|show)[^.]{0,80}(?:name|e-?mail|phone)", re.I)
_PII_WORDS = re.compile(r"customer_name|e-?mail|phone", re.I)
_NEGATION = re.compile(r"\b(?:never|not|no|don't|do not|must not|refuse|withhold)\b", re.I)
_ID = re.compile(r"\b(?:SITE|CTR|BAT|CUST|DEV|INV|MTR|GW)-\d+\b", re.I)

SYSTEM = """You improve a Microsoft Fabric data agent (natural language -> SQL/KQL over its data sources).
You get the agent's current instructions, its data sources with schema, and questions it answered wrong, each with the
query it generated and why the answer was graded wrong.
Propose GENERAL fixes that would help any similar question, not answers to these specific questions:
- business definitions, metric formulas, join keys, value casing, which table/column holds a fact,
- answering policy (e.g. decline when the data does not contain what is asked; never estimate),
- few-shot examples as reusable SQL/KQL PATTERNS on different wording/entities than the failed questions.
Be concrete: name the table, column, filter value or formula. Generic advice ("use correct join keys", "use consistent
casing") does not help. Only use filter values and columns that appear in the schema or in the generated queries shown.
The agent answers in a single turn: never tell it to ask the user for clarification.
If a question needs data the agent's sources do not contain, the fix is to decline clearly, not to approximate.
SQL sources are Fabric SQL analytics endpoints (T-SQL): use CAST(GETUTCDATE() AS date), not CURRENT_DATE; prefix tables
with dbo. KQL sources use Kusto syntax.
Few-shot questions must be about DIFFERENT metrics or entities than the failed questions (the same pattern applied to
another table, column or filter), so they teach the method without giving away the answer.
Rules: never include the failed questions or their expected numbers/IDs; never weaken privacy rules (customer names,
emails and phone numbers must never be revealed); keep additions short and specific.
Return only JSON: {"instructions_add": [string], "datasource_instructions_add": {"<data source name>": [string]},
"fewshots": [{"datasource": "<data source name>", "question": string, "query": string}], "rationale": string}"""


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def build_prompt(instructions: str, sources: list[dict], failures: list[dict]) -> str:
    """sources: [{name, type, instructions, schema}]; failures: [{question, query, detail, expected_answer, answer}]"""
    lines = ["## Current agent instructions", instructions or "(none)", "", "## Data sources"]
    for s in sources:
        lines += [f"### {s['name']} ({s['type']})", f"Instructions: {s.get('instructions') or '(none)'}",
                  f"Schema: {s.get('schema') or '(see ontology)'}"]
    lines += ["", f"## Wrong answers ({len(failures)})"]
    for i, f in enumerate(failures, 1):
        lines += [f"{i}. Question: {f['question']}", f"   Generated query: {(f.get('query') or '(none)')[:800]}",
                  f"   Agent answer: {(f.get('answer') or '')[:400]}", f"   Grader: {f['detail']}",
                  f"   Expected: {f['expected_answer']}"]
    return "\n".join(lines)


def parse_proposal(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("optimizer returned no JSON object")
    p = json.loads(m.group(0))
    out = {"instructions_add": [str(x).strip() for x in p.get("instructions_add") or [] if str(x).strip()],
           "datasource_instructions_add": {str(k): [str(x).strip() for x in v if str(x).strip()]
                                           for k, v in (p.get("datasource_instructions_add") or {}).items()},
           "fewshots": [{"datasource": str(f.get("datasource", "")), "question": str(f.get("question", "")).strip(),
                         "query": str(f.get("query", "")).strip()} for f in p.get("fewshots") or []
                        if f.get("question") and f.get("query")],
           "rationale": str(p.get("rationale", ""))[:2000]}
    return out


def apply(instructions: str, source_instructions: dict[str, str], proposal: dict) -> tuple[str, dict[str, str]]:
    new_instr = instructions.rstrip()
    if proposal["instructions_add"]:
        new_instr += "\n\nLearned from evaluation:\n" + "\n".join(f"- {x}" for x in proposal["instructions_add"])
    new_src = dict(source_instructions)
    for name, adds in proposal["datasource_instructions_add"].items():
        if name in new_src and adds:
            new_src[name] = (new_src[name].rstrip() + "\n" if new_src[name] else "") + "\n".join(adds)
    return new_instr.strip(), new_src


def _leaked_values(golden: list[dict]) -> tuple[set[str], set[str]]:
    """IDs and distinctive numbers from expected answers (not small integers that any pattern may contain)."""
    ids, nums = set(), set()
    for c in golden:
        text = c.get("expected_answer", "")
        ids |= {x.upper() for x in _ID.findall(text)}
        for n in re.findall(r"\d[\d,]*\.\d+|\d{1,3}(?:,\d{3})+|\d{3,}", text):
            v = n.replace(",", "")
            if not re.fullmatch(r"(?:19|20)\d\d", v):  # years are fine
                nums.add(v)
    return ids, nums


def check(proposal: dict, base_instructions: str, new_instructions: str, new_sources: dict[str, str],
          golden: list[dict], source_names: set[str], execute=None) -> list[str]:
    """Return guardrail violations (empty = pass). golden: [{question, expected_answer}] for ALL splits.
    execute(source_name, query) -> None or raises; called for each few-shot when provided."""
    problems: list[str] = []
    if not (proposal["instructions_add"] or proposal["datasource_instructions_add"] or proposal["fewshots"]):
        problems.append("empty proposal")
    added = proposal["instructions_add"] + [x for v in proposal["datasource_instructions_add"].values() for x in v]
    added_text = "\n".join(added + [f["question"] + "\n" + f["query"] for f in proposal["fewshots"]])
    # 1. golden leakage: questions (verbatim or close paraphrase) and expected IDs/values
    gq = [_norm(c["question"]) for c in golden]
    for f in proposal["fewshots"]:
        best = max((difflib.SequenceMatcher(None, _norm(f["question"]), q).ratio() for q in gq), default=0.0)
        if best >= SIMILARITY_LIMIT:
            problems.append(f"few-shot too close to a golden question ({best:.2f}): {f['question'][:80]}")
    for line in added:
        if any(q and q in _norm(line) for q in gq):
            problems.append(f"instruction contains a golden question: {line[:80]}")
    ids, nums = _leaked_values(golden)
    found_ids = {x.upper() for x in _ID.findall(added_text)} & ids
    if found_ids:
        problems.append(f"expected IDs leaked: {', '.join(sorted(found_ids)[:5])}")
    found_nums = {n.replace(",", "") for n in re.findall(r"\d[\d,]*\.\d+|\d{1,3}(?:,\d{3})+|\d{3,}", added_text)} & nums
    if found_nums:
        problems.append(f"expected values leaked: {', '.join(sorted(found_nums)[:5])}")
    # 2. privacy: keep an existing PII rule; additions may only mention PII to forbid it
    if _PII_RULE.search(base_instructions) and not _PII_RULE.search(new_instructions):
        problems.append("PII rule removed")
    for line in added:
        if _PII_WORDS.search(line) and not _NEGATION.search(line):
            problems.append(f"addition mentions PII without forbidding it: {line[:80]}")
    for f in proposal["fewshots"]:
        if _PII_WORDS.search(f["query"]):
            problems.append(f"few-shot selects PII columns: {f['question'][:60]}")
    for line in added:
        if re.search(r"\b(?:ask|request)\b[^.]{0,40}\bclarif", line, re.I):
            problems.append(f"agents answer in one turn; no clarification requests: {line[:80]}")
    # 3. size
    if len(new_instructions) > MAX_INSTRUCTIONS:
        problems.append(f"instructions too long ({len(new_instructions)} > {MAX_INSTRUCTIONS})")
    for name, text in new_sources.items():
        if len(text) > MAX_SOURCE_INSTRUCTIONS:
            problems.append(f"{name} instructions too long ({len(text)} > {MAX_SOURCE_INSTRUCTIONS})")
    if len(proposal["fewshots"]) > MAX_FEWSHOTS_ADDED:
        problems.append(f"too many few-shots ({len(proposal['fewshots'])} > {MAX_FEWSHOTS_ADDED})")
    # 4. few-shots must target a queryable source and execute
    for name in list(proposal["datasource_instructions_add"]) + [f["datasource"] for f in proposal["fewshots"]]:
        if name not in source_names:
            problems.append(f"unknown data source: {name}")
    if execute is not None:
        for f in proposal["fewshots"]:
            if f["datasource"] in source_names:
                try:
                    n = execute(f["datasource"], f["query"])
                    if n == 0:  # a pattern that matches nothing usually filters on an invented value ('West', 'Li-Ion')
                        problems.append(f"few-shot returns no rows (invented filter value?) ({f['datasource']}): "
                                        f"{f['question'][:80]}")
                except (Exception, SystemExit) as ex:  # noqa: BLE001
                    problems.append(f"few-shot does not execute ({f['datasource']}): {str(ex)[:120]}")
    return problems


def propose(client, deployment: str, prompt: str, feedback: list[str] | None = None) -> dict:
    """One optimizer call (AzureOpenAI client). feedback = guardrail violations of the previous attempt."""
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    if feedback:
        hint = ""
        if any("too close to a golden question" in f for f in feedback):
            hint = ("\nFew-shot questions must not reuse the failed questions or their wording: pick a different metric, "
                    "table or filter that exercises the same pattern, or drop the few-shot and use an instruction.")
        messages.append({"role": "user", "content": "Your previous proposal was rejected:\n- " + "\n- ".join(feedback)
                         + hint + "\nReturn a corrected proposal (JSON only)."})
    resp = client.chat.completions.create(model=deployment, messages=messages, temperature=0.2,
                                          response_format={"type": "json_object"}, max_tokens=4000)
    p = parse_proposal(resp.choices[0].message.content)
    p["usage"] = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
    return p


def accept(base: dict, cand: dict, min_train_gain: int = 2) -> tuple[bool, str]:
    """base/cand: {"train": (passed, n), "validation": (passed, n), "forbid_fail": int, "categories": {cat: passed}}.
    Paired comparison from the SAME run, so capacity and data drift affect both equally. The required train gain is
    capped by the head's train failures (a head with 1 failure can only gain 1)."""
    reasons = []
    if cand["forbid_fail"] > 0:
        reasons.append(f"forbidden-data failures: {cand['forbid_fail']}")
    gain = cand["train"][0] - base["train"][0]
    need = max(1, min(min_train_gain, base["train"][1] - base["train"][0]))
    if gain < need:
        reasons.append(f"train gain {gain:+d} < {need}")
    if cand["validation"][0] < base["validation"][0]:
        reasons.append(f"validation {cand['validation'][0]} < {base['validation'][0]}")
    drops = [c for c, v in base["categories"].items() if cand["categories"].get(c, 0) < v - 1]
    if drops:
        reasons.append(f"category drop > 1 question: {', '.join(drops)}")
    if reasons:
        return False, "; ".join(reasons)
    return True, (f"train {base['train'][0]}->{cand['train'][0]}/{cand['train'][1]}, "
                  f"validation {base['validation'][0]}->{cand['validation'][0]}/{cand['validation'][1]}")
