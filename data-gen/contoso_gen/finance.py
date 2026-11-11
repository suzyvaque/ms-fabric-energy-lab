"""Payment history per financing contract, with lateness by credit band, defaults, partials,
duplicates (same payment_id re-sent) and late-arriving records (recorded_at >> paid_date)."""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone

from .config import GenConfig, iso
from .reference import CREDIT_BANDS, DEFAULT_PROB


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    days = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, days))


def _ts(d: date, rng: random.Random) -> datetime:
    return datetime.combine(d, time(rng.randint(6, 22), rng.randint(0, 59), rng.randint(0, 59)), timezone.utc)


def generate_payments(cfg: GenConfig, master: dict) -> list[dict]:
    """Returns payment records; mutates contract status (Active/Delinquent/Default/PaidOff)."""
    rng = random.Random(f"{cfg.seed}-payments")
    as_of = cfg.end.date()
    band_of = {c["customer_id"]: c["credit_band"] for c in master["customers"]}
    payments: list[dict] = []
    for ctr in master["contracts"]:
        band = band_of[ctr["customer_id"]]
        p_late = CREDIT_BANDS[band][3]
        start = date.fromisoformat(ctr["start_date"])
        n_due = 0
        while _add_months(start, n_due + 1) <= as_of and n_due < ctr["term_months"]:
            n_due += 1
        default_at = rng.randint(6, max(7, n_due)) if n_due and rng.random() < DEFAULT_PROB[band] else None
        method = rng.choices(["AutoPay", "ACH", "Card", "Check"], weights=[60, 20, 15, 5])[0]
        consecutive_missed = 0
        for k in range(1, n_due + 1):
            due = _add_months(start, k)
            amount_due = ctr["monthly_payment"]
            if ctr["escalator_pct"]:
                amount_due = round(amount_due * (1 + ctr["escalator_pct"] / 100) ** ((k - 1) // 12), 2)
            rec = {"payment_id": f"PAY-{ctr['contract_id'][4:]}-{k:03d}", "contract_id": ctr["contract_id"],
                   "customer_id": ctr["customer_id"], "installment_no": k, "due_date": due.isoformat(),
                   "amount_due": amount_due, "method": method}
            if default_at and k >= default_at:
                rec.update(paid_date=None, amount_paid=0.0, status="Missed", days_late=None)
                consecutive_missed += 1
            else:
                late = rng.random() < (p_late * (0.3 if method == "AutoPay" else 1.0))
                if late:
                    days_late = min(90, max(1, int(rng.lognormvariate(2.0, 0.8))))
                    paid = due + timedelta(days=days_late)
                    status = "Late"
                else:
                    days_late = 0
                    paid = due - timedelta(days=rng.randint(0, 5) if method != "AutoPay" else 0)
                    status = "OnTime"
                amount_paid = amount_due
                if rng.random() < 0.02:
                    amount_paid = round(amount_due * rng.uniform(0.5, 0.9), 2)
                    status = "Partial"
                if paid > as_of:  # not yet paid as of the snapshot date
                    rec.update(paid_date=None, amount_paid=0.0, status="Overdue", days_late=(as_of - due).days)
                    consecutive_missed += 1
                else:
                    rec.update(paid_date=paid.isoformat(), amount_paid=amount_paid, status=status,
                               days_late=days_late)
                    consecutive_missed = 0
            event_day = date.fromisoformat(rec["paid_date"]) if rec["paid_date"] else due
            recorded = _ts(event_day, rng) + timedelta(days=rng.randint(0, 2))
            if rng.random() < cfg.payment_late_arrival_rate:
                recorded += timedelta(days=rng.randint(10, 40))
            rec["recorded_at"] = iso(min(recorded, cfg.end))
            payments.append(rec)
            if rng.random() < cfg.payment_dup_rate:
                dup = dict(rec)
                dup["recorded_at"] = iso(min(recorded + timedelta(hours=rng.randint(1, 72)), cfg.end))
                payments.append(dup)
        if n_due >= ctr["term_months"]:
            ctr["status"] = "PaidOff"
        elif consecutive_missed >= 3:
            ctr["status"] = "Default"
        elif consecutive_missed >= 1:
            ctr["status"] = "Delinquent"
    return payments


def _band(score: int) -> str:
    return "A" if score >= 780 else "B" if score >= 720 else "C" if score >= 660 else "D" if score >= 600 else "E"


def generate_credit_bureau(cfg: GenConfig, master: dict, payments: list[dict], months: int = 6) -> dict[str, list[dict]]:
    """Monthly credit-bureau score refresh files (external partner drop, step 1d ADLS shortcut).

    Returns {file_name: rows}. Scores random-walk from the onboarding score; customers with
    defaulted/delinquent contracts deteriorate. The latest month is re-issued as a `_v2` correction
    file for ~3% of customers (silver keeps the latest version per customer and month)."""
    rng = random.Random(f"{cfg.seed}-bureau")
    status_by_customer: dict[str, str] = {}
    for c in master["contracts"]:
        if c["status"] in ("Default", "Delinquent"):
            status_by_customer[c["customer_id"]] = c["status"]
    late_counts: dict[str, int] = {}
    for p in payments:
        if p["status"] in ("Missed", "Overdue") or (p["days_late"] or 0) > 30:
            late_counts[p["customer_id"]] = late_counts.get(p["customer_id"], 0) + 1
    month_ends = []
    d = cfg.end.date().replace(day=1)
    for _ in range(months):
        d = d - timedelta(days=1)
        month_ends.append(d)
        d = d.replace(day=1)
    month_ends.reverse()
    scores = {c["customer_id"]: c["credit_score"] for c in master["customers"]}
    files: dict[str, list[dict]] = {}
    for i, as_of in enumerate(month_ends):
        rows = []
        for c in master["customers"]:
            cid = c["customer_id"]
            drift = rng.gauss(0, 6)
            if cid in status_by_customer:
                drift -= rng.uniform(5, 25) if status_by_customer[cid] == "Default" else rng.uniform(0, 10)
            scores[cid] = int(min(850, max(300, scores[cid] + drift)))
            rows.append({"as_of_date": as_of.isoformat(), "customer_id": cid, "bureau_score": scores[cid],
                         "score_band": _band(scores[cid]), "inquiries_6m": min(6, int(rng.expovariate(1.2))),
                         "delinquencies_24m": late_counts.get(cid, 0), "bankruptcy_flag": rng.random() < 0.002,
                         "file_version": 1})
        files[f"score_refresh_{as_of:%Y%m}.csv"] = rows
        if i == len(month_ends) - 1:
            fixes = [dict(r, bureau_score=min(850, r["bureau_score"] + rng.randint(-40, 40)), file_version=2)
                     for r in rows if rng.random() < 0.03]
            for f in fixes:
                f["score_band"] = _band(f["bureau_score"])
            files[f"score_refresh_{as_of:%Y%m}_v2.csv"] = fixes
    return files
