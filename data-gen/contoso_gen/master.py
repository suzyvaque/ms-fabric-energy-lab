"""Asset master + customer + contract generation (deterministic for a given seed and site count)."""
from __future__ import annotations

import random
from datetime import date, timedelta

from .config import GenConfig
from .reference import (CREDIT_BANDS, DEVICE_MODELS, FIRMWARE_V1, FIRMWARE_V2, FIRST_NAMES, LAST_NAMES,
                        REGION_WEIGHTS, REGIONS, STREET_TYPES, STREETS)

# Fixed anchor keeps master data identical across runs/days, so batch history and live streams share IDs.
MASTER_ANCHOR = date(2026, 1, 1)


def _weighted(rng: random.Random, weights: dict):
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _amortized_payment(principal: float, apr_pct: float, months: int) -> float:
    r = apr_pct / 100 / 12
    return principal / months if r == 0 else principal * r / (1 - (1 + r) ** -months)


def generate_master(cfg: GenConfig) -> dict[str, list[dict]]:
    rng = random.Random(f"{cfg.seed}-master")
    customers: list[dict] = []
    sites: list[dict] = []
    devices: list[dict] = []
    batteries: list[dict] = []
    contracts: list[dict] = []

    def new_customer(region: str, install: date) -> dict:
        i = len(customers)
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        band = _weighted(rng, {k: v[0] for k, v in CREDIT_BANDS.items()})
        lo, hi = CREDIT_BANDS[band][1]
        c = {
            "customer_id": f"CUST-{i:06d}",
            "first_name": first,
            "last_name": last,
            "email": f"{first}.{last}.{i}@example.com".lower(),
            "phone": f"+1-555-01{rng.randint(0, 99):02d}",
            "street_address": f"{rng.randint(10, 9999)} {rng.choice(STREETS)} {rng.choice(STREET_TYPES)}",
            "city": rng.choice(REGIONS[region]["cities"]),
            "region": region,
            "postal_code": f"{rng.randint(10000, 99999)}",
            "credit_band": band,
            "credit_score": rng.randint(lo, hi),
            "signup_date": (install - timedelta(days=rng.randint(20, 120))).isoformat(),
            "segment": "Residential",
            "marketing_opt_in": rng.random() < 0.4,
        }
        customers.append(c)
        return c

    for s in range(cfg.sites):
        region = _weighted(rng, REGION_WEIGHTS)
        reg = REGIONS[region]
        install = MASTER_ANCHOR - timedelta(days=rng.randint(30, 6 * 365))
        # ~3% of sites belong to a customer who already has a site (multi-site owners)
        same_region = [c for c in customers if c["region"] == region]
        cust = rng.choice(same_region) if same_region and rng.random() < 0.03 else new_customer(region, install)
        pv_kw = round(min(15.0, max(3.0, rng.lognormvariate(2.0, 0.3))), 1)
        has_battery = rng.random() < 0.85
        site_id = f"SITE-{s:06d}"
        sites.append({
            "site_id": site_id, "customer_id": cust["customer_id"], "region": region,
            "latitude": round(rng.uniform(*reg["lat"]), 4), "longitude": round(rng.uniform(*reg["lon"]), 4),
            "timezone": reg["tz"], "install_date": install.isoformat(), "pv_capacity_kw": pv_kw,
            "panel_count": int(pv_kw * 1000 / 400), "tilt_deg": rng.choice([15, 20, 25, 30]),
            "azimuth_deg": rng.choice([150, 165, 180, 195, 210]), "has_battery": has_battery,
            "ev_charger": rng.random() < 0.2, "status": "Active",
        })
        drift = rng.random() < cfg.schema_drift_rate  # firmware 2.x site -> schema v2 events
        device_types = ["gateway", "inverter", "meter"] + (["battery"] if has_battery else [])
        for dtype in device_types:
            prefix = {"gateway": "GW", "inverter": "INV", "meter": "MTR", "battery": "BAT"}[dtype]
            dev_id = f"{prefix}-{s:06d}"
            devices.append({
                "device_id": dev_id, "site_id": site_id, "device_type": dtype,
                "model": rng.choice(DEVICE_MODELS[dtype]),
                "serial_number": f"CE{rng.getrandbits(40):010X}",
                "firmware": rng.choice(FIRMWARE_V2 if drift else FIRMWARE_V1),
                "install_date": install.isoformat(), "status": "Active",
            })
            if dtype == "battery":
                cap = rng.choice([10.0, 13.5, 13.5, 20.0])
                age_years = (MASTER_ANCHOR - install).days / 365
                batteries.append({
                    "device_id": dev_id, "site_id": site_id, "capacity_kwh": cap,
                    "max_power_kw": 5.0 if cap <= 10 else 7.0,
                    "chemistry": "LFP" if rng.random() < 0.7 else "NMC",
                    "warranty_end": (install + timedelta(days=3650)).isoformat(),
                    "cycle_count": int(age_years * rng.uniform(250, 330)),
                    "state_of_health_pct": round(max(70.0, 100 - age_years * rng.uniform(1.2, 2.8)), 1),
                })

        if rng.random() >= cfg.cash_purchase_rate:
            band = cust["credit_band"]
            product = _weighted(rng, {"Loan": 0.55, "Lease": 0.25, "PPA": 0.20})
            term = rng.choice([120, 180, 240, 300] if product == "Loan" else [240, 300])
            system_cost = round(pv_kw * rng.uniform(2600, 3200) + (rng.uniform(8500, 12500) if has_battery else 0), 2)
            apr = round(CREDIT_BANDS[band][2] + rng.uniform(-0.5, 1.0), 2) if product == "Loan" else None
            if product == "Loan":
                principal = round(system_cost * rng.uniform(0.85, 1.0), 2)
                monthly = _amortized_payment(principal, apr, term)
            elif product == "Lease":
                principal = system_cost
                monthly = system_cost / term * rng.uniform(1.2, 1.35)
            else:  # PPA: priced per kWh, billed on an estimated flat monthly amount
                principal = system_cost
                monthly = pv_kw * 125 * rng.uniform(0.12, 0.18)
            contracts.append({
                "contract_id": f"CTR-{s:06d}", "customer_id": cust["customer_id"], "site_id": site_id,
                "product": product, "start_date": install.isoformat(), "term_months": term,
                "system_cost": system_cost, "principal": round(principal, 2), "apr_pct": apr,
                "ppa_rate_per_kwh": round(monthly / (pv_kw * 125), 4) if product == "PPA" else None,
                "escalator_pct": 2.9 if product == "PPA" else None,
                "monthly_payment": round(monthly, 2), "currency": "USD",
                "status": "Active",  # finalized by finance.generate_payments
            })
    return {"customers": customers, "sites": sites, "devices": devices, "batteries": batteries,
            "contracts": contracts}
