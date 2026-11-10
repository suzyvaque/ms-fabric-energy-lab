"""Generator configuration and shared helpers."""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def utc_hour_now() -> datetime:
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


@dataclass
class GenConfig:
    sites: int = 200
    days: int = 7
    interval_min: int = 5
    end: datetime = field(default_factory=utc_hour_now)
    seed: int = 42
    # Data-quality injection
    dup_rate: float = 0.005          # share of telemetry events re-sent (same event_id)
    late_rate: float = 0.01          # share of events delivered minutes-hours after event_time
    skew_device_rate: float = 0.003  # share of devices with a wrong clock
    schema_drift_rate: float = 0.10  # share of devices on firmware 2.x (schema v2)
    # Anomalies (ground truth written to ground_truth/anomalies.csv)
    anomaly_rate: float = 0.02       # anomalies per device-day
    false_alert_rate: float = 0.002  # false-positive device alerts per device-day
    alert_dup_rate: float = 0.01
    # Finance
    cash_purchase_rate: float = 0.15
    payment_dup_rate: float = 0.003
    payment_late_arrival_rate: float = 0.01

    @property
    def start(self) -> datetime:
        return self.end - timedelta(days=self.days)

    @property
    def interval(self) -> timedelta:
        return timedelta(minutes=self.interval_min)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def rng_uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))
