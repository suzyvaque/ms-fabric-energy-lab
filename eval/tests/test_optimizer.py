import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import optimizer as opt  # noqa: E402

GOLDEN = [{"question": "Which sites have a financing contract that is at risk?",
           "expected_answer": "At-risk contracts are at: SITE-000045, SITE-000063."},
          {"question": "What is the total financed principal of all Lease contracts, in USD?",
           "expected_answer": "Total Lease principal is $660,527.12."}]
BASE = "You are an assistant. Never reveal customer names, emails or phone numbers."
SOURCES = {"lh_asset"}


def prop(**kw):
    p = {"instructions_add": [], "datasource_instructions_add": {}, "fewshots": [], "rationale": ""}
    p.update(kw)
    return p


def run(p, base=BASE, execute=None):
    new_i, new_s = opt.apply(base, {"lh_asset": ""}, p)
    return opt.check(p, base, new_i, new_s, GOLDEN, SOURCES, execute)


def test_parse_and_apply():
    p = opt.parse_proposal('noise {"instructions_add": ["At risk = Delinquent or Default."], '
                           '"datasource_instructions_add": {"lh_asset": ["Status values are capitalized."]}, '
                           '"fewshots": [{"datasource": "lh_asset", "question": "q", "query": "SELECT 1"}]} tail')
    new_i, new_s = opt.apply(BASE, {"lh_asset": "Join on site_id."}, p)
    assert "Learned from evaluation:\n- At risk = Delinquent or Default." in new_i and new_i.startswith(BASE)
    assert new_s["lh_asset"] == "Join on site_id.\nStatus values are capitalized."


def test_clean_proposal_passes():
    p = prop(instructions_add=["'At risk' means status Delinquent or Default."],
             fewshots=[{"datasource": "lh_asset", "question": "Contracts by status per product",
                        "query": "SELECT product, status, COUNT(*) FROM dbo.fin_contracts GROUP BY product, status"}])
    assert run(p, execute=lambda s, q: None) == []


def test_leakage_rejected():
    assert any("too close" in x for x in run(prop(fewshots=[{"datasource": "lh_asset",
               "question": "Which sites have a financing contract at risk?", "query": "SELECT 1"}])))
    assert any("IDs leaked" in x for x in run(prop(instructions_add=["SITE-000045 is in default."])))
    assert any("values leaked" in x for x in run(prop(instructions_add=["Lease principal totals 660,527.12."])))
    assert run(prop(instructions_add=["Contracts started between 2020 and 2025."])) == []  # years are fine


def test_privacy_and_size_and_execution():
    assert any("PII rule removed" in x for x in opt.check(prop(instructions_add=["x"]), BASE, "You are an assistant.",
                                                          {}, GOLDEN, SOURCES))
    assert any("without forbidding" in x for x in run(prop(instructions_add=["Include the customer email."])))
    assert run(prop(instructions_add=["Never include customer email or phone."])) == []
    assert any("selects PII" in x for x in run(prop(fewshots=[{"datasource": "lh_asset", "question": "contacts",
                                                               "query": "SELECT email FROM dbo.fin_customers"}])))
    assert any("too long" in x for x in run(prop(instructions_add=["a" * 9000])))
    assert any("unknown data source" in x for x in run(prop(datasource_instructions_add={"nope": ["x"]})))

    def boom(source, query):
        raise RuntimeError("Invalid column name 'x'")
    assert any("does not execute" in x for x in run(prop(fewshots=[{"datasource": "lh_asset", "question": "n",
                                                                    "query": "SELECT x"}]), execute=boom))
    assert any("no rows" in x for x in run(prop(fewshots=[{"datasource": "lh_asset", "question": "West principal",
                                                          "query": "SELECT 1 WHERE 1 = 0"}]), execute=lambda s, q: 0))
    assert run(prop()) == ["empty proposal"]
    assert any("clarification" in x for x in run(prop(instructions_add=["If ambiguous, request clarification."])))


def test_accept():
    base = {"train": (20, 27), "validation": (15, 21), "forbid_fail": 0, "categories": {"a": 3, "b": 2}}
    ok, why = opt.accept(base, {"train": (23, 27), "validation": (15, 21), "forbid_fail": 0, "categories": {"a": 3, "b": 2}})
    assert ok and "20->23" in why
    assert not opt.accept(base, {"train": (21, 27), "validation": (16, 21), "forbid_fail": 0, "categories": {"a": 3, "b": 2}})[0]
    assert not opt.accept(base, {"train": (24, 27), "validation": (14, 21), "forbid_fail": 0, "categories": {"a": 3, "b": 2}})[0]
    assert not opt.accept(base, {"train": (24, 27), "validation": (16, 21), "forbid_fail": 1, "categories": {"a": 3, "b": 2}})[0]
    assert not opt.accept(base, {"train": (24, 27), "validation": (16, 21), "forbid_fail": 0, "categories": {"a": 1, "b": 2}})[0]
    one_left = {"train": (21, 22), "validation": (12, 14), "forbid_fail": 0, "categories": {"a": 3}}
    assert opt.accept(one_left, {"train": (22, 22), "validation": (12, 14), "forbid_fail": 0, "categories": {"a": 3}})[0]
    assert not opt.accept(one_left, {"train": (21, 22), "validation": (12, 14), "forbid_fail": 0, "categories": {"a": 3}})[0]
