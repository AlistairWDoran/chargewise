"""Cost-engine unit tests, grounded on Alistair's real Intelligent Octopus Go rates.

Off-peak 6.9p, peak 30.3714p inc VAT (region H, tariff E-1R-INTELLI-VAR-24-10-29-H).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from chargewise.engine import (
    ChargeSession,
    Dispatch,
    LocationType,
    RatePeriod,
    cost_away_session,
    cost_home_session,
    in_core_window,
    split_into_slots,
)
from chargewise.engine.models import RateSource

UK = ZoneInfo("Europe/London")
UTC = ZoneInfo("UTC")

OFFPEAK = 0.069
PEAK = 0.303714

RATES = [
    RatePeriod(
        valid_from=datetime(2024, 10, 29, tzinfo=UTC),
        valid_to=None,
        offpeak_inc_vat=OFFPEAK,
        peak_inc_vat=PEAK,
    )
]


def local(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UK)


# --- slot splitting -------------------------------------------------------

def test_split_aligns_to_half_hours_with_partial_ends():
    slots = split_into_slots(local(2026, 1, 10, 23, 15), local(2026, 1, 11, 0, 45))
    assert slots == [
        (local(2026, 1, 10, 23, 15), local(2026, 1, 10, 23, 30)),
        (local(2026, 1, 10, 23, 30), local(2026, 1, 11, 0, 0)),
        (local(2026, 1, 11, 0, 0), local(2026, 1, 11, 0, 30)),
        (local(2026, 1, 11, 0, 30), local(2026, 1, 11, 0, 45)),
    ]


def test_split_empty_when_end_not_after_start():
    assert split_into_slots(local(2026, 1, 10, 1, 0), local(2026, 1, 10, 1, 0)) == []


# --- core-window detection ------------------------------------------------

@pytest.mark.parametrize(
    "dt,expected",
    [
        (local(2026, 6, 15, 0, 0), True),
        (local(2026, 6, 15, 5, 0), True),
        (local(2026, 6, 15, 5, 30), False),
        (local(2026, 6, 15, 12, 0), False),
        (local(2026, 6, 15, 23, 0), False),
        (local(2026, 6, 15, 23, 30), True),
    ],
)
def test_core_window(dt, expected):
    assert in_core_window(dt) is expected


# --- home session costing -------------------------------------------------

def test_home_session_entirely_offpeak():
    s = ChargeSession(local(2026, 1, 10, 0, 0), local(2026, 1, 10, 2, 0), 10.0, LocationType.HOME)
    result = cost_home_session(s, RATES, [])
    assert result.total_cost == pytest.approx(10.0 * OFFPEAK)  # £0.69
    assert {sc.rate_source for sc in result.slots} == {RateSource.CORE}


def test_home_session_crossing_offpeak_boundary():
    # 23:00–00:00: first half peak, second half off-peak.
    s = ChargeSession(local(2026, 1, 10, 23, 0), local(2026, 1, 11, 0, 0), 4.0, LocationType.HOME)
    result = cost_home_session(s, RATES, [])
    expected = 2.0 * PEAK + 2.0 * OFFPEAK
    assert result.total_cost == pytest.approx(expected)
    assert [sc.rate_source for sc in result.slots] == [RateSource.STANDARD, RateSource.CORE]


def test_dispatch_makes_daytime_slot_offpeak():
    s = ChargeSession(local(2026, 6, 15, 13, 0), local(2026, 6, 15, 13, 30), 5.0, LocationType.HOME)
    dispatch = Dispatch(local(2026, 6, 15, 13, 0), local(2026, 6, 15, 13, 30), "AT_HOME")
    result = cost_home_session(s, RATES, [dispatch])
    assert result.total_cost == pytest.approx(5.0 * OFFPEAK)
    assert result.slots[0].rate_source is RateSource.DISPATCH


def test_daytime_slot_without_dispatch_is_peak():
    s = ChargeSession(local(2026, 6, 15, 13, 0), local(2026, 6, 15, 13, 30), 5.0, LocationType.HOME)
    result = cost_home_session(s, RATES, [])
    assert result.total_cost == pytest.approx(5.0 * PEAK)
    assert result.slots[0].rate_source is RateSource.STANDARD


# --- away session costing -------------------------------------------------

def test_away_uses_actual_cost_when_not_estimate():
    s = ChargeSession(
        local(2026, 6, 14, 2, 0), local(2026, 6, 14, 2, 30), 20.0,
        LocationType.AWAY, raw_cost=9.50, raw_cost_is_estimate=False,
    )
    result = cost_away_session(s, away_rate=0.50)
    assert result.total_cost == pytest.approx(9.50)
    assert result.is_estimate is False


def test_away_falls_back_to_estimate_rate():
    s = ChargeSession(
        local(2026, 6, 14, 2, 0), local(2026, 6, 14, 2, 30), 20.0,
        LocationType.AWAY, raw_cost=None,
    )
    result = cost_away_session(s, away_rate=0.50)
    assert result.total_cost == pytest.approx(10.0)
    assert result.is_estimate is True
