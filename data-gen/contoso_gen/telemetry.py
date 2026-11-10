"""Fleet telemetry + device alerts simulator.

Physics-lite model per site: PV output (solar geometry, season, weather), household load (+EV),
battery self-consumption (SOC, temperature) and grid exchange. On top of that:
  - anomalies with ground truth (inverter fault, battery overtemp, SOC stuck, offline gateway,
    meter spike, PV degradation, clock skew)
  - delivery effects: network latency, late events, duplicates (retries), store-and-forward backfill
  - schema drift: firmware 2.x sites emit schema v2 (renamed fields, W instead of kW)
Used by both batch (history) and stream (live) modes so the event contract is identical.
"""
from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import GenConfig, iso, rng_uuid
from .reference import REGIONS

# type: (device_type, weight, duration minutes range, device alert (code, severity, source) or None)
ANOMALY_TYPES: dict[str, tuple] = {
    "INVERTER_FAULT":   ("inverter", 0.25, (60, 720), ("INV_FAULT", "High", "device")),
    "BATTERY_OVERTEMP": ("battery", 0.15, (30, 180), ("BATT_OVERTEMP", "Critical", "device")),
    "SOC_STUCK":        ("battery", 0.15, (360, 2880), None),
    "DEVICE_OFFLINE":   ("gateway", 0.25, (30, 1440), ("COMM_LOSS", "High", "cloud")),
    "METER_SPIKE":      ("meter", 0.15, (0, 0), None),
    "PV_DEGRADATION":   ("inverter", 0.05, (2880, 10080), None),
}
FALSE_ALERTS = [("meter", "GRID_VOLTAGE", "Low"), ("inverter", "FAN_WARN", "Low"),
                ("battery", "TEMP_SENSOR", "Low")]
ALERT_MESSAGES = {
    "INV_FAULT": "Inverter fault E-301: DC input lost", "BATT_OVERTEMP": "Battery temperature above limit",
    "COMM_LOSS": "No heartbeat from gateway", "GRID_VOLTAGE": "Grid voltage out of range",
    "FAN_WARN": "Inverter fan speed low", "TEMP_SENSOR": "Battery temperature sensor noisy",
}


@dataclass
class Anomaly:
    anomaly_id: str
    anomaly_type: str
    device_id: str
    site_id: str
    start: datetime
    end: datetime
    backfill: bool = False
    state: dict = field(default_factory=dict)

    def active(self, t: datetime) -> bool:
        return self.start <= t < self.end

    def progress(self, t: datetime) -> float:
        span = (self.end - self.start).total_seconds() or 1.0
        return min(1.0, max(0.0, (t - self.start).total_seconds() / span))

    def truth_row(self) -> dict:
        alert = ANOMALY_TYPES.get(self.anomaly_type, (None, 0, 0, None))[3]
        return {"anomaly_id": self.anomaly_id, "anomaly_type": self.anomaly_type, "device_id": self.device_id,
                "site_id": self.site_id, "start_time": iso(self.start), "end_time": iso(self.end),
                "expected_alert_code": alert[0] if alert else "", "backfilled": self.backfill, "skew_minutes": ""}


class Delivery:
    """Time-ordered outbox: (deliver_at, event)."""

    def __init__(self) -> None:
        self._heap: list = []
        self._n = 0

    def push(self, at: datetime, event: dict) -> None:
        heapq.heappush(self._heap, (at, self._n, event))
        self._n += 1

    def pop_due(self, now: datetime) -> list[tuple[datetime, dict]]:
        out = []
        while self._heap and self._heap[0][0] <= now:
            at, _, ev = heapq.heappop(self._heap)
            out.append((at, ev))
        return out

    def next_time(self) -> datetime | None:
        return self._heap[0][0] if self._heap else None

    def __len__(self) -> int:
        return len(self._heap)


class SiteSim:
    def __init__(self, site: dict, devices: list[dict], battery: dict | None, rng: random.Random):
        self.site = site
        self.rng = rng
        self.region = REGIONS[site["region"]]
        self.devices = {d["device_type"]: d for d in devices}
        self.v2 = devices[0]["firmware"].startswith("2.")
        self.battery = battery
        self.soc = rng.uniform(30, 90)
        self.cloud = 1.0
        self.weather_day = None
        self.weather_base, self.weather_var = 1.0, 0.05
        self.base_load = rng.uniform(0.35, 1.1)
        self.efficiency = rng.uniform(0.955, 0.975)
        self.rssi = rng.uniform(-82, -58)
        self.uptime_s = rng.randint(3600, 90 * 86400)

    def _local_hour(self, t: datetime) -> float:
        return (t.hour + t.minute / 60 + t.second / 3600 + self.site["longitude"] / 15) % 24

    def _weather(self, t: datetime) -> None:
        day = (t + timedelta(hours=self.site["longitude"] / 15)).date()
        if day != self.weather_day:
            self.weather_day = day
            r = self.rng.random()
            clear = self.region["clear"]
            if r < clear:
                self.weather_base, self.weather_var = self.rng.uniform(0.9, 1.0), 0.04
            elif r < clear + (1 - clear) * 0.6:
                self.weather_base, self.weather_var = self.rng.uniform(0.55, 0.8), 0.25
            else:
                self.weather_base, self.weather_var = self.rng.uniform(0.15, 0.4), 0.12
        target = self.weather_base + self.rng.gauss(0, self.weather_var)
        self.cloud = min(1.0, max(0.05, 0.7 * self.cloud + 0.3 * target))

    def step(self, t: datetime, dt_h: float, anomalies: list[Anomaly]) -> dict:
        """Advance physical state; returns per-device readings (before schema mapping)."""
        rng = self.rng
        self._weather(t)
        lh = self._local_hour(t)
        doy = t.timetuple().tm_yday
        season = math.cos(2 * math.pi * (doy - 172) / 365)
        lat = self.site["latitude"]
        daylen = 12 + 3.2 * season * (lat / 35)
        x = (lh - (12 - daylen / 2)) / daylen
        irr = math.sin(math.pi * x) ** 1.3 if 0 < x < 1 else 0.0
        cap = self.site["pv_capacity_kw"]
        ambient = self.region["temp"] + 8 * season + 6 * math.sin(2 * math.pi * (lh - 9) / 24) + rng.gauss(0, 0.8)

        act = {a.anomaly_type: a for a in anomalies if a.active(t)}
        degrade = 1.0
        if "PV_DEGRADATION" in act:
            degrade = 1 - 0.4 * act["PV_DEGRADATION"].progress(t)
        pv = cap * irr * (0.78 + 0.22 * season) * self.cloud * self.region["irr"] * 0.85 * degrade
        pv = max(0.0, min(cap * 0.95, pv + rng.gauss(0, 0.01 * cap) * (pv > 0)))
        inv_fault = "INVERTER_FAULT" in act
        if inv_fault:
            pv = 0.0
        ac = pv * self.efficiency

        load = (self.base_load + 1.2 * math.exp(-((lh - 7.5) / 1.2) ** 2)
                + 2.2 * math.exp(-((lh - 19.0) / 1.8) ** 2) + abs(rng.gauss(0, 0.15)))
        if self.site["ev_charger"] and (lh >= 22 or lh < 1.5) and rng.random() < 0.8:
            load += 7.2

        batt = None
        batt_kw = 0.0
        if self.battery:
            bcap, pmax = self.battery["capacity_kwh"], self.battery["max_power_kw"]
            surplus = ac - load
            if surplus > 0 and self.soc < 99:
                batt_kw = min(surplus, pmax, (100 - self.soc) / 100 * bcap / dt_h)
            elif surplus < 0 and self.soc > 10:
                batt_kw = -min(-surplus, pmax, (self.soc - 10) / 100 * bcap / dt_h)
            temp = ambient * 0.6 + 10 + 2.2 * abs(batt_kw) + rng.gauss(0, 0.5)
            status, fault = "OK", None
            if "BATTERY_OVERTEMP" in act:
                p = act["BATTERY_OVERTEMP"].progress(t)
                peak = act["BATTERY_OVERTEMP"].state.setdefault("peak", rng.uniform(58, 72))
                temp = temp + (peak - temp) * (p / 0.7 if p < 0.7 else (1 - p) / 0.3)
                if temp > 55:
                    batt_kw = 0.0  # thermal derate
                status, fault = ("FAULT", "B-210") if temp > 60 else (("WARN", None) if temp > 50 else ("OK", None))
            self.soc = min(100.0, max(5.0, self.soc + batt_kw * dt_h / bcap * 100 * (0.95 if batt_kw > 0 else 1 / 0.95)))
            soc_reported = self.soc
            if "SOC_STUCK" in act:
                soc_reported = act["SOC_STUCK"].state.setdefault("value", round(self.soc, 1))
            batt = {"soc_pct": soc_reported, "batt_kw": batt_kw, "temp_c": temp,
                    "soh_pct": self.battery["state_of_health_pct"], "status": status, "fault_code": fault}

        grid = load - ac + batt_kw
        if "METER_SPIKE" in act:
            grid = grid * rng.choice([12, -12]) + rng.choice([45.0, -45.0])
        self.uptime_s += int(dt_h * 3600)
        return {
            "inverter": {"pv_kw": pv, "ac_kw": ac, "inv_temp_c": ambient + 18 * pv / cap + rng.gauss(0, 0.6),
                         "efficiency": self.efficiency if pv > 0.05 else 0.0,
                         "status": "FAULT" if inv_fault else "OK", "fault_code": "E-301" if inv_fault else None},
            "battery": batt,
            "meter": {"grid_kw": grid, "load_kw": load, "voltage_v": 240 + rng.gauss(0, 1.5),
                      "freq_hz": 60 + rng.gauss(0, 0.015), "status": "OK", "fault_code": None},
            "gateway": {"rssi_dbm": self.rssi + rng.gauss(0, 2), "uptime_s": self.uptime_s,
                        "cpu_pct": rng.uniform(4, 25), "status": "OK", "fault_code": None},
        }


def _measurements(dtype: str, r: dict, v2: bool) -> dict:
    if dtype == "inverter":
        if v2:
            return {"dc_power_w": round(r["pv_kw"] * 1000, 1), "ac_power_w": round(r["ac_kw"] * 1000, 1),
                    "inverter_temp_c": round(r["inv_temp_c"], 2), "efficiency": round(r["efficiency"], 4)}
        return {"pv_kw": round(r["pv_kw"], 3), "ac_kw": round(r["ac_kw"], 3),
                "inv_temp_c": round(r["inv_temp_c"], 2), "efficiency": round(r["efficiency"], 4)}
    if dtype == "battery":
        if v2:
            return {"soc_pct": round(r["soc_pct"], 1), "battery_power_w": round(r["batt_kw"] * 1000, 1),
                    "temperature_c": round(r["temp_c"], 2), "soh_pct": r["soh_pct"]}
        return {"soc_pct": round(r["soc_pct"], 1), "batt_kw": round(r["batt_kw"], 3),
                "temp_c": round(r["temp_c"], 2), "soh_pct": r["soh_pct"]}
    if dtype == "meter":
        return {"grid_kw": round(r["grid_kw"], 3), "load_kw": round(r["load_kw"], 3),
                "voltage_v": round(r["voltage_v"], 1), "freq_hz": round(r["freq_hz"], 3)}
    return {"rssi_dbm": round(r["rssi_dbm"], 1), "uptime_s": r["uptime_s"], "cpu_pct": round(r["cpu_pct"], 1)}


class Fleet:
    def __init__(self, cfg: GenConfig, master: dict, seed_suffix: str = "telemetry", live: bool = False):
        self.cfg = cfg
        self.live = live
        self.rng = random.Random(f"{cfg.seed}-{seed_suffix}")
        self.qrng = random.Random(f"{cfg.seed}-{seed_suffix}-quality")
        self.arng = random.Random(f"{cfg.seed}-{seed_suffix}-alerts")
        devs_by_site: dict[str, list] = {}
        for d in master["devices"]:
            devs_by_site.setdefault(d["site_id"], []).append(d)
        batt = {b["site_id"]: b for b in master["batteries"]}
        self.sims = [SiteSim(s, devs_by_site[s["site_id"]], batt.get(s["site_id"]), self.rng)
                     for s in master["sites"]]
        self.sim_by_site = {s.site["site_id"]: s for s in self.sims}
        self.anomalies: list[Anomaly] = []
        self.by_site: dict[str, list[Anomaly]] = {}
        self.seq: dict[str, int] = {}
        self.skew: dict[str, timedelta] = {}
        srng = random.Random(f"{cfg.seed}-skew")
        for d in master["devices"]:
            if srng.random() < cfg.skew_device_rate:
                self.skew[d["device_id"]] = timedelta(minutes=srng.choice([-1, 1]) * srng.randint(2, 90))
        self.delivery = Delivery()
        self.alert_truth: list[dict] = []

    # ---------- anomalies ----------
    def add_anomaly(self, atype: str, site_id: str, start: datetime, minutes: float | None = None) -> Anomaly | None:
        dtype, _, (lo, hi), _ = ANOMALY_TYPES[atype]
        sim = self.sim_by_site[site_id]
        dev = sim.devices.get(dtype)
        if dev is None:
            return None
        if minutes is None:
            minutes = self.rng.uniform(lo, hi) if hi else self.cfg.interval_min
        if atype == "METER_SPIKE":
            minutes = max(minutes, 0.01)
        a = Anomaly(rng_uuid(self.rng), atype, dev["device_id"], site_id, start, start + timedelta(minutes=minutes),
                    backfill=atype == "DEVICE_OFFLINE" and self.rng.random() < 0.5)
        self.anomalies.append(a)
        self.by_site.setdefault(site_id, []).append(a)
        self._schedule_alerts(a)
        return a

    def plan_batch_anomalies(self) -> None:
        cfg = self.cfg
        n_dev = sum(len(s.devices) for s in self.sims)
        n = round(cfg.anomaly_rate * n_dev * cfg.days)
        types = list(ANOMALY_TYPES)
        weights = [ANOMALY_TYPES[t][1] for t in types]
        planned = (types if len(self.sims) >= 10 else []) + self.rng.choices(types, weights, k=max(0, n - len(types)))
        span_s = (cfg.end - cfg.start).total_seconds()
        for atype in planned:
            dtype = ANOMALY_TYPES[atype][0]
            candidates = [s for s in self.sims if dtype in s.devices]
            if not candidates:
                continue
            site = self.rng.choice(candidates).site["site_id"]
            start = cfg.start + timedelta(seconds=self.rng.uniform(0, span_s * 0.9))
            start = start.replace(second=0, microsecond=0)
            start -= timedelta(minutes=start.minute % cfg.interval_min)
            self.add_anomaly(atype, site, start)
        n_false = round(cfg.false_alert_rate * n_dev * cfg.days)
        for _ in range(n_false):
            self._false_alert(cfg.start + timedelta(seconds=self.rng.uniform(0, span_s)))

    def maybe_start_live_anomalies(self, t: datetime, dt: timedelta, boost: float) -> list[Anomaly]:
        started = []
        p = self.cfg.anomaly_rate * boost * dt.total_seconds() / 86400
        for sim in self.sims:
            for _ in sim.devices:
                if self.rng.random() < p:
                    types = list(ANOMALY_TYPES)
                    atype = self.rng.choices(types, [ANOMALY_TYPES[x][1] for x in types])[0]
                    a = self.add_anomaly(atype, sim.site["site_id"], t,
                                         minutes=self.rng.uniform(5, 30) if atype != "METER_SPIKE" else 0.01)
                    if a:
                        started.append(a)
        if self.rng.random() < self.cfg.false_alert_rate * boost * len(self.sims) * dt.total_seconds() / 86400:
            self._false_alert(t)
        return started

    # ---------- alerts ----------
    def _alert_event(self, alert_id: str, etype: str, device_id: str, site_id: str, code: str, sev: str,
                     source: str, t: datetime) -> dict:
        return {"record_type": "alert", "schema_version": 1, "alert_id": alert_id, "event_type": etype,
                "device_id": device_id, "site_id": site_id, "alert_code": code, "severity": sev,
                "source": source, "message": ALERT_MESSAGES.get(code, code), "event_time": iso(t),
                "sent_time": None}

    def _push_alert(self, ev: dict, t: datetime) -> None:
        at = t + timedelta(seconds=self.arng.uniform(0.5, 5))
        self.delivery.push(at, ev)
        if self.arng.random() < self.cfg.alert_dup_rate:
            self.delivery.push(at + timedelta(seconds=self.arng.uniform(1, 60)), dict(ev))

    def _schedule_alerts(self, a: Anomaly) -> None:
        spec = ANOMALY_TYPES[a.anomaly_type][3]
        if not spec:
            return
        code, sev, source = spec
        r = self.arng
        if source == "cloud":  # raised by the platform after 15 min without heartbeat
            if (a.end - a.start) <= timedelta(minutes=15):
                return
            raise_t = a.start + timedelta(minutes=15)
        else:
            if r.random() > 0.9:  # device misses 10% of real faults
                return
            raise_t = a.start + timedelta(minutes=r.uniform(1, 10))
        windows = [(raise_t, a.end + timedelta(minutes=r.uniform(1, 5)))]
        if a.anomaly_type == "BATTERY_OVERTEMP" and r.random() < 0.2:  # flapping alert
            windows = []
            t0 = raise_t
            for _ in range(r.randint(3, 5)):
                t1 = t0 + timedelta(minutes=r.uniform(2, 6))
                windows.append((t0, t1))
                t0 = t1 + timedelta(minutes=r.uniform(1, 4))
        dev_site = a.device_id if source == "device" else self.sim_by_site[a.site_id].devices["gateway"]["device_id"]
        for rt, ct in windows:
            aid = rng_uuid(r)
            self._push_alert(self._alert_event(aid, "raised", dev_site, a.site_id, code, sev, source, rt), rt)
            self._push_alert(self._alert_event(aid, "cleared", dev_site, a.site_id, code, sev, source, ct), ct)
            self.alert_truth.append({"alert_id": aid, "anomaly_id": a.anomaly_id, "false_positive": False})

    def _false_alert(self, t: datetime) -> None:
        r = self.arng
        dtype, code, sev = r.choice(FALSE_ALERTS)
        candidates = [s for s in self.sims if dtype in s.devices]
        sim = r.choice(candidates)
        dev = sim.devices[dtype]["device_id"]
        aid = rng_uuid(r)
        ct = t + timedelta(minutes=r.uniform(2, 20))
        self._push_alert(self._alert_event(aid, "raised", dev, sim.site["site_id"], code, sev, "device", t), t)
        self._push_alert(self._alert_event(aid, "cleared", dev, sim.site["site_id"], code, sev, "device", ct), ct)
        self.alert_truth.append({"alert_id": aid, "anomaly_id": "", "false_positive": True})

    # ---------- telemetry ----------
    def tick(self, t: datetime, dt: timedelta) -> int:
        """Generate one reading per device at time t and push it to the delivery outbox. Returns count."""
        dt_h = dt.total_seconds() / 3600
        heartbeat = self.live or t.minute % 15 == 0
        q, cfg = self.qrng, self.cfg
        n = 0
        for sim in self.sims:
            site_id = sim.site["site_id"]
            anoms = self.by_site.get(site_id, [])
            readings = sim.step(t, dt_h, anoms)
            offline = next((a for a in anoms if a.anomaly_type == "DEVICE_OFFLINE" and a.active(t)), None)
            for dtype, dev in sim.devices.items():
                if dtype == "gateway" and not heartbeat:
                    continue
                r = readings[dtype]
                dev_id = dev["device_id"]
                self.seq[dev_id] = self.seq.get(dev_id, 0) + 1
                ev = {"record_type": "telemetry", "schema_version": 2 if sim.v2 else 1,
                      "event_id": rng_uuid(q), "device_id": dev_id, "site_id": site_id, "device_type": dtype,
                      "event_time": iso(t + self.skew.get(dev_id, timedelta(0))), "sent_time": None,
                      "seq": self.seq[dev_id], "firmware": dev["firmware"], "status": r["status"],
                      "fault_code": r["fault_code"], "measurements": _measurements(dtype, r, sim.v2)}
                if offline:
                    if not offline.backfill:
                        continue  # data lost
                    at = offline.end + timedelta(seconds=q.uniform(5, 300))  # store-and-forward flush
                elif q.random() < cfg.late_rate:
                    at = t + timedelta(minutes=min(720.0, q.lognormvariate(2.7, 1.0)))
                else:
                    at = t + timedelta(seconds=q.uniform(0.2, 3.0))
                self.delivery.push(at, ev)
                if q.random() < cfg.dup_rate:
                    self.delivery.push(at + timedelta(seconds=q.uniform(1, 120)), dict(ev))
                n += 1
        return n

    def truth_rows(self) -> list[dict]:
        rows = [a.truth_row() for a in self.anomalies]
        for dev_id, skew in self.skew.items():
            site = next(s.site["site_id"] for s in self.sims if any(d["device_id"] == dev_id for d in s.devices.values()))
            rows.append({"anomaly_id": f"skew-{dev_id}", "anomaly_type": "CLOCK_SKEW", "device_id": dev_id,
                         "site_id": site, "start_time": iso(self.cfg.start), "end_time": iso(self.cfg.end),
                         "expected_alert_code": "", "backfilled": False,
                         "skew_minutes": int(skew.total_seconds() // 60)})
        return rows
