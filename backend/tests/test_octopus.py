"""Octopus REST + GraphQL adapter tests, using real published tariff shapes.

The account number, meter point and meter serial are placeholders; the unit
rates and the tariff code are Octopus's published ones.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from chargewise.engine import ChargeSession, LocationType, RatePeriod, cost_home_session
from chargewise.engine.models import RateSource
from chargewise.ingest.octopus_graphql import parse_dispatches
from chargewise.ingest.octopus_rest import (
    derive_iog_rate_periods,
    parse_account,
    parse_consumption,
    parse_unit_rates,
)

UK = ZoneInfo("Europe/London")

ACCOUNT = {
    "number": "A-00000000",
    "properties": [{
        "electricity_meter_points": [{
            "mpan": "0000000000000",
            "meters": [{"serial_number": "00X0000000"}],
            "agreements": [
                {"tariff_code": "E-1R-INTELLI-VAR-24-10-29-H",
                 "valid_from": "2025-01-01T00:00:00Z", "valid_to": None},
            ],
        }],
    }],
}

UNIT_RATES = {"results": [
    {"value_exc_vat": 6.5710, "value_inc_vat": 6.9000,
     "valid_from": "2026-06-15T22:30:00Z", "valid_to": None},
    {"value_exc_vat": 28.9251, "value_inc_vat": 30.3714,
     "valid_from": "2026-06-15T04:30:00Z", "valid_to": "2026-06-15T22:30:00Z"},
]}

# Mid-agreement price change: peak rises 30.3714 -> 31.5p at the start of the
# 31 March peak window; the off-peak rate is unchanged.
UNIT_RATES_PEAK_CHANGE = {"results": [
    {"value_exc_vat": 28.9251, "value_inc_vat": 30.3714,
     "valid_from": "2026-03-30T04:30:00Z", "valid_to": "2026-03-30T22:30:00Z"},
    {"value_exc_vat": 6.5710, "value_inc_vat": 6.9000,
     "valid_from": "2026-03-30T22:30:00Z", "valid_to": "2026-03-31T04:30:00Z"},
    {"value_exc_vat": 30.0000, "value_inc_vat": 31.5000,
     "valid_from": "2026-03-31T04:30:00Z", "valid_to": "2026-03-31T22:30:00Z"},
    {"value_exc_vat": 6.5710, "value_inc_vat": 6.9000,
     "valid_from": "2026-03-31T22:30:00Z", "valid_to": None},
]}

# Mid-agreement price change the other way: the OFF-PEAK rate rises
# 6.9 -> 8.5p at the start of the 31 March off-peak window; peak unchanged.
UNIT_RATES_OFFPEAK_RISE = {"results": [
    {"value_exc_vat": 28.9251, "value_inc_vat": 30.3714,
     "valid_from": "2026-03-30T04:30:00Z", "valid_to": "2026-03-30T22:30:00Z"},
    {"value_exc_vat": 6.5710, "value_inc_vat": 6.9000,
     "valid_from": "2026-03-30T22:30:00Z", "valid_to": "2026-03-31T04:30:00Z"},
    {"value_exc_vat": 28.9251, "value_inc_vat": 30.3714,
     "valid_from": "2026-03-31T04:30:00Z", "valid_to": "2026-03-31T22:30:00Z"},
    {"value_exc_vat": 8.0952, "value_inc_vat": 8.5000,
     "valid_from": "2026-03-31T22:30:00Z", "valid_to": None},
]}

# 2022-24 variable-era shape: both rates FALL at a price change effective at
# 00:00, which splits the overnight off-peak record in two. Agreement closed.
UNIT_RATES_VARIABLE_ERA = {"results": [
    {"value_exc_vat": 38.3614, "value_inc_vat": 40.2795,
     "valid_from": "2024-03-30T05:30:00Z", "valid_to": "2024-03-30T23:30:00Z"},
    {"value_exc_vat": 7.1429, "value_inc_vat": 7.5000,
     "valid_from": "2024-03-30T23:30:00Z", "valid_to": "2024-03-31T05:30:00Z"},
    {"value_exc_vat": 38.3614, "value_inc_vat": 40.2795,
     "valid_from": "2024-03-31T05:30:00Z", "valid_to": "2024-03-31T23:30:00Z"},
    {"value_exc_vat": 7.1429, "value_inc_vat": 7.5000,
     "valid_from": "2024-03-31T23:30:00Z", "valid_to": "2024-04-01T00:00:00Z"},
    {"value_exc_vat": 6.5710, "value_inc_vat": 6.9000,
     "valid_from": "2024-04-01T00:00:00Z", "valid_to": "2024-04-01T05:30:00Z"},
    {"value_exc_vat": 28.9251, "value_inc_vat": 30.3714,
     "valid_from": "2024-04-01T05:30:00Z", "valid_to": "2024-04-01T22:30:00Z"},
    {"value_exc_vat": 6.5710, "value_inc_vat": 6.9000,
     "valid_from": "2024-04-01T22:30:00Z", "valid_to": "2024-04-02T05:30:00Z"},
]}

# HA / GraphQL dispatch shape: daytime smart-charge slot at home.
DISPATCHES = [
    {"start": "2026-06-15T13:00:00+01:00", "end": "2026-06-15T13:30:00+01:00",
     "location": "AT_HOME"},
]


def test_parse_account_extracts_meter_and_agreement():
    acct = parse_account(ACCOUNT)
    assert acct["mpan"] == "0000000000000"
    assert acct["serial_number"] == "00X0000000"
    assert acct["agreements"][0].tariff_code == "E-1R-INTELLI-VAR-24-10-29-H"
    assert acct["agreements"][0].valid_to is None


def test_parse_consumption_sorts_half_hours():
    # Consumption endpoint shape (order_by=-period is the API default: newest first).
    payload = {"results": [
        {"consumption": 0.482, "interval_start": "2026-06-15T02:30:00+01:00",
         "interval_end": "2026-06-15T03:00:00+01:00"},
        {"consumption": 1.213, "interval_start": "2026-06-15T02:00:00+01:00",
         "interval_end": "2026-06-15T02:30:00+01:00"},
    ]}
    records = parse_consumption(payload)
    assert [r.kwh for r in records] == [1.213, 0.482]
    assert records[0].interval_start == datetime(2026, 6, 15, 1, 0, tzinfo=timezone.utc)
    assert records[0].interval_end == records[1].interval_start


def test_derive_rate_periods_picks_offpeak_and_peak():
    """Octopus supplies rates in pence; RatePeriods must be in GBP/kWh.

    Regression: rates were passed through in pence, inflating every cost
    by exactly 100x (found when the first real backfill priced lifetime
    home charging at £784k).
    """
    records = parse_unit_rates(UNIT_RATES)
    periods = derive_iog_rate_periods(records)
    assert len(periods) == 1
    assert periods[0].offpeak_inc_vat == pytest.approx(0.069)
    assert periods[0].peak_inc_vat == pytest.approx(0.303714)
    assert periods[0].valid_to is None  # peak rate still active


def test_derive_no_change_is_identical_to_single_era_output():
    """A history with no price change must collapse to exactly one period,
    byte-identical to the previous min/max behaviour (same boundaries, same
    pence -> GBP arithmetic)."""
    periods = derive_iog_rate_periods(parse_unit_rates(UNIT_RATES))
    assert periods == [
        RatePeriod(
            valid_from=datetime(2026, 6, 15, 4, 30, tzinfo=timezone.utc),
            valid_to=None,
            offpeak_inc_vat=6.9 / 100.0,
            peak_inc_vat=30.3714 / 100.0,
        )
    ]


def test_mid_agreement_peak_change_produces_two_eras():
    """One change to the (off-peak, peak) pair -> two adjacent half-open eras."""
    first, second = derive_iog_rate_periods(parse_unit_rates(UNIT_RATES_PEAK_CHANGE))

    assert first.valid_from == datetime(2026, 3, 30, 4, 30, tzinfo=timezone.utc)
    assert first.valid_to == datetime(2026, 3, 31, 4, 30, tzinfo=timezone.utc)
    assert first.offpeak_inc_vat == pytest.approx(0.069)
    assert first.peak_inc_vat == pytest.approx(0.303714)

    assert second.valid_from == first.valid_to
    assert second.valid_to is None  # still active
    assert second.offpeak_inc_vat == pytest.approx(0.069)
    assert second.peak_inc_vat == pytest.approx(0.315)

    # Half-open intervals: the boundary instant belongs to the new era only.
    boundary = datetime(2026, 3, 31, 4, 30, tzinfo=timezone.utc)
    assert not first.covers(boundary)
    assert second.covers(boundary)


def test_mid_agreement_offpeak_rise_updates_offpeak_not_peak():
    """An off-peak RISE (value between the current pair, nearer the off-peak
    member) must replace the off-peak member and leave the peak untouched.

    Guards the nearer-member rule specifically: a naive `value < offpeak`
    classifier would misread the 8.5p rise as a new *peak*, silently billing
    every subsequent peak slot at 8.5p (~3.5x underpriced)."""
    first, second = derive_iog_rate_periods(parse_unit_rates(UNIT_RATES_OFFPEAK_RISE))

    assert first.offpeak_inc_vat == pytest.approx(0.069)
    assert first.peak_inc_vat == pytest.approx(0.303714)
    assert first.valid_to == datetime(2026, 3, 31, 22, 30, tzinfo=timezone.utc)

    assert second.valid_from == datetime(2026, 3, 31, 22, 30, tzinfo=timezone.utc)
    assert second.valid_to is None
    assert second.offpeak_inc_vat == pytest.approx(0.085)   # off-peak rose
    assert second.peak_inc_vat == pytest.approx(0.303714)   # peak unchanged


def test_variable_era_history_yields_one_era_per_change():
    """N pair changes -> N+1 eras; a 00:00 change splitting the off-peak
    record produces a faithful transition sliver (new off-peak, old peak)
    before the peak window re-prices at 05:30. Pence -> GBP conversion is
    applied per era."""
    periods = derive_iog_rate_periods(parse_unit_rates(UNIT_RATES_VARIABLE_ERA))
    assert len(periods) == 3
    era1, era2, era3 = periods

    assert era1.valid_from == datetime(2024, 3, 30, 5, 30, tzinfo=timezone.utc)
    assert era1.valid_to == datetime(2024, 4, 1, 0, 0, tzinfo=timezone.utc)
    assert era1.offpeak_inc_vat == pytest.approx(7.5 / 100.0)
    assert era1.peak_inc_vat == pytest.approx(40.2795 / 100.0)

    # Transition sliver 00:00-05:30: off-peak already cut, peak not yet re-set.
    assert era2.valid_from == era1.valid_to
    assert era2.valid_to == datetime(2024, 4, 1, 5, 30, tzinfo=timezone.utc)
    assert era2.offpeak_inc_vat == pytest.approx(6.9 / 100.0)
    assert era2.peak_inc_vat == pytest.approx(40.2795 / 100.0)

    assert era3.valid_from == era2.valid_to
    # Agreement closed: last era ends at the last record's end.
    assert era3.valid_to == datetime(2024, 4, 2, 5, 30, tzinfo=timezone.utc)
    assert era3.offpeak_inc_vat == pytest.approx(6.9 / 100.0)
    assert era3.peak_inc_vat == pytest.approx(30.3714 / 100.0)


def test_sessions_price_at_the_era_valid_at_slot_time():
    """The engine must bill a peak slot at the rate of the era it falls in,
    not a min/max collapse across the agreement."""
    periods = derive_iog_rate_periods(parse_unit_rates(UNIT_RATES_PEAK_CHANGE))

    def peak_cost_on(day: int) -> float:
        session = ChargeSession(
            datetime(2026, 3, day, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 3, day, 10, 30, tzinfo=timezone.utc),
            4.0, LocationType.HOME,
        )
        result = cost_home_session(session, periods, dispatches=[])
        assert result.slots[0].rate_source is RateSource.STANDARD
        return result.total_cost

    assert peak_cost_on(30) == pytest.approx(4.0 * 0.303714)  # old era
    assert peak_cost_on(31) == pytest.approx(4.0 * 0.315)     # new era


def test_dispatches_feed_into_engine_as_offpeak():
    dispatches = parse_dispatches(DISPATCHES)
    periods = derive_iog_rate_periods(parse_unit_rates(UNIT_RATES))
    session = ChargeSession(
        datetime(2026, 6, 15, 13, 0, tzinfo=UK),
        datetime(2026, 6, 15, 13, 30, tzinfo=UK),
        5.0, LocationType.HOME,
    )
    result = cost_home_session(session, periods, dispatches)
    assert result.slots[0].rate_source is RateSource.DISPATCH
    assert result.total_cost == pytest.approx(5.0 * 0.069)
