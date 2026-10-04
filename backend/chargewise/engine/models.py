"""Core domain models for the ChargeWise cost engine.

These are deliberately plain, immutable dataclasses with no I/O so the engine
stays pure and trivially testable. All datetimes must be timezone-aware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class LocationType(str, Enum):
    HOME = "home"
    AWAY = "away"


class RateSource(str, Enum):
    """Why a given half-hour slot was billed at the rate it was."""

    CORE = "core"          # inside the tariff's core off-peak window
    DISPATCH = "dispatch"  # an Intelligent Octopus Go smart-charge slot
    STANDARD = "standard"  # normal/peak day rate


@dataclass(frozen=True)
class RatePeriod:
    """An off-peak/peak rate pair valid over a date range (handles tariff changes)."""

    valid_from: datetime
    valid_to: datetime | None      # None == still active
    offpeak_inc_vat: float         # GBP/kWh
    peak_inc_vat: float            # GBP/kWh

    def covers(self, when: datetime) -> bool:
        if when < self.valid_from:
            return False
        return self.valid_to is None or when < self.valid_to


@dataclass(frozen=True)
class Dispatch:
    """An Intelligent Octopus Go smart-charge slot (from the Octopus dispatch feed)."""

    start: datetime
    end: datetime
    location: str = "unknown"      # 'AT_HOME' | 'AWAY' | 'unknown'

    def covers(self, when: datetime) -> bool:
        return self.start <= when < self.end


@dataclass(frozen=True)
class ChargeSession:
    """A single charging session as reported by TeslaFi."""

    start: datetime
    end: datetime
    energy_kwh: float
    location_type: LocationType
    odometer: float | None = None
    raw_cost: float | None = None          # TeslaFi/away cost in GBP, if any
    raw_cost_is_estimate: bool = True


@dataclass(frozen=True)
class SlotCost:
    slot_start: datetime
    slot_end: datetime
    energy_kwh: float
    unit_rate: float
    rate_source: RateSource
    cost: float


@dataclass(frozen=True)
class SessionCost:
    session: ChargeSession
    total_cost: float
    is_estimate: bool
    slots: list[SlotCost] = field(default_factory=list)
