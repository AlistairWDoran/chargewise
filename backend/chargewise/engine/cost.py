"""Dispatch-aware cost engine for Intelligent Octopus Go (and similar) tariffs.

Pure functions only — given a charge session plus rate history and dispatch
slots, compute the cost. No network, no database. See docs/METHODOLOGY.md for
the reasoning behind each rule.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .models import (
    ChargeSession,
    Dispatch,
    LocationType,
    RatePeriod,
    RateSource,
    SessionCost,
    SlotCost,
)

UK = ZoneInfo("Europe/London")
SLOT = timedelta(minutes=30)

# Intelligent Octopus Go core off-peak window, in local time.
CORE_START = time(23, 30)
CORE_END = time(5, 30)


def half_hour_floor(dt: datetime) -> datetime:
    """Round a datetime down to the start of its half-hour settlement slot."""
    minute = 0 if dt.minute < 30 else 30
    return dt.replace(minute=minute, second=0, microsecond=0)


def split_into_slots(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into half-hour settlement slots, clipped to the session.

    The first and last slots may be partial; their fraction drives energy
    apportionment.
    """
    if end <= start:
        return []
    slots: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        slot_boundary = half_hour_floor(cursor) + SLOT
        slot_end = min(slot_boundary, end)
        slots.append((cursor, slot_end))
        cursor = slot_end
    return slots


def in_core_window(when: datetime) -> bool:
    """True if `when` (any tz) falls inside the 23:30–05:30 local off-peak window."""
    local = when.astimezone(UK).time()
    # Window wraps midnight, so it's "after CORE_START OR before CORE_END".
    return local >= CORE_START or local < CORE_END


def _rate_period_for(when: datetime, rates: list[RatePeriod]) -> RatePeriod:
    for period in rates:
        if period.covers(when):
            return period
    raise ValueError(f"No rate period covers {when.isoformat()}")


def _covered_by_dispatch(when: datetime, dispatches: list[Dispatch]) -> bool:
    return any(d.covers(when) for d in dispatches)


def cost_home_session(
    session: ChargeSession,
    rates: list[RatePeriod],
    dispatches: list[Dispatch],
) -> SessionCost:
    """Cost a home charging session slot-by-slot, dispatch-aware.

    A slot is billed off-peak if it is inside the core window OR covered by a
    smart-charge dispatch; otherwise it is billed at the standard/peak rate.
    """
    slots = split_into_slots(session.start, session.end)
    total_seconds = (session.end - session.start).total_seconds()
    slot_costs: list[SlotCost] = []
    total = 0.0

    for slot_start, slot_end in slots:
        fraction = (slot_end - slot_start).total_seconds() / total_seconds
        energy = session.energy_kwh * fraction
        period = _rate_period_for(slot_start, rates)

        if in_core_window(slot_start):
            rate, source = period.offpeak_inc_vat, RateSource.CORE
        elif _covered_by_dispatch(slot_start, dispatches):
            rate, source = period.offpeak_inc_vat, RateSource.DISPATCH
        else:
            rate, source = period.peak_inc_vat, RateSource.STANDARD

        cost = energy * rate
        total += cost
        slot_costs.append(
            SlotCost(slot_start, slot_end, round(energy, 4), rate, source, round(cost, 4))
        )

    # Keep full precision internally; round only for display/billing reconciliation.
    return SessionCost(session, total, is_estimate=False, slots=slot_costs)


def cost_away_session(session: ChargeSession, away_rate: float) -> SessionCost:
    """Cost an away/public session: use TeslaFi's recorded cost, else estimate."""
    if session.raw_cost is not None and not session.raw_cost_is_estimate:
        return SessionCost(session, round(session.raw_cost, 4), is_estimate=False)
    if session.raw_cost is not None:
        return SessionCost(session, round(session.raw_cost, 4), is_estimate=True)
    return SessionCost(session, round(session.energy_kwh * away_rate, 4), is_estimate=True)


def cost_session(
    session: ChargeSession,
    rates: list[RatePeriod],
    dispatches: list[Dispatch],
    away_rate: float,
) -> SessionCost:
    if session.location_type is LocationType.HOME:
        return cost_home_session(session, rates, dispatches)
    return cost_away_session(session, away_rate)
