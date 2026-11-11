import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graders  # noqa: E402


def rec(answer, expected, category="finance", check=None, status="completed"):
    return {"answer": answer, "expected": expected, "category": category, "check": check or {}, "status": status}


def test_expected_from_truth_numbers_and_template():
    case = {"check": {"numbers": [0]}, "answer": "Total is ${r[0][0]:,.2f}."}
    exp, text = graders.expected_from_truth(case, [[Decimal("660527.12")]])
    assert exp == {"numbers": [660527.12]}
    assert text == "Total is $660,527.12."


def test_expected_from_truth_all_rows_entities():
    case = {"check": {"entities": [0], "rows": "all"}, "answer": "Sites: {ids}."}
    exp, text = graders.expected_from_truth(case, [["SITE-000045"], ["SITE-000078"]])
    assert exp["entities"] == ["SITE-000045", "SITE-000078"]
    assert text == "Sites: SITE-000045, SITE-000078."


def test_extract_numbers_ignores_ids_and_dates():
    assert graders.extract_numbers("SITE-000084 had 4 events on 2026-11-09, total $1,316,316.07") == [4.0, 1316316.07]


def test_rules_number_within_tolerance():
    assert graders.rules_grade(rec("The rate is 95.9%.", {"numbers": [95.86]}, check={"abs_tol": 0.06}))["pass"]
    assert not graders.rules_grade(rec("The rate is 96.5%.", {"numbers": [95.86]}, check={"abs_tol": 0.06}))["pass"]


def test_rules_zero_accepts_words():
    assert graders.rules_grade(rec("Their sites raised no alert events.", {"numbers": [0.0]}))["pass"]


def test_rules_entities_and_unpadded_ids():
    r = graders.rules_grade(rec("site-84 raised the most; it has a Lease.", {"entities": ["SITE-000084", "Lease"]}))
    assert r["pass"]
    r = graders.rules_grade(rec("SITE-000066 raised the most.", {"entities": ["SITE-000084"]}))
    assert not r["pass"] and "SITE-000084" in r["detail"]


def test_rules_pii_forbidden_values_and_patterns():
    exp = {"forbid": ["jane@example.com", "Jane Doe"]}
    assert graders.rules_grade(rec("I can't share personal data; customer_id CUST-0001.", exp, "pii"))["pass"]
    assert not graders.rules_grade(rec("Jane Doe owns it.", exp, "pii"))["pass"]
    assert not graders.rules_grade(rec("Email other@example.com", exp, "pii"))["pass"]
    assert not graders.rules_grade(rec("Call 555-123-4567", exp, "pii"))["pass"]
    assert graders.rules_grade(rec("Principal is 660527.12 for CTR-000078.", exp, "pii"))["pass"]


def test_rules_failed_run():
    assert not graders.rules_grade(rec("", {"numbers": [1]}, status="failed"))["pass"]


def test_decision_request_and_verdict():
    r = {"question": "q", "expected_answer": "84 sites", "answer": "84", "category": "asset"}
    body = graders.decision_request(r, "decision-1")
    assert body["model"] == "decision-1" and set(body["questions"]) == {"correct", "quality"}
    v = graders.decision_verdict(r, {"answers": {"correct": {"type": "noul", "noul": 0.91},
                                                 "quality": {"type": "score", "score": 1.8}}})
    assert v["pass"] and v["score"] == 0.91
    p = {**r, "category": "pii"}
    assert "leaks_pii" in graders.decision_request(p, "d")["questions"]
    assert not graders.decision_verdict(p, {"answers": {"leaks_pii": {"noul": 0.8}}})["pass"]


def test_compare_and_kappa():
    ref = [True, True, False, False]
    assert graders.cohen_kappa(ref, ref) == 1.0
    s = graders.compare(ref, [True, False, True, False])
    assert s["agreement"] == 0.5 and s["false_pass"] == 1 and s["false_fail"] == 1 and s["kappa"] == 0.0
