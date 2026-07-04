"""Octopus REST + GraphQL adapter tests, using Alistair's real tariff shapes."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from chargewise.engine import ChargeSession, LocationType, cost_home_session
from chargewise.engine.models import RateSource
from chargewise.ingest.octopus_graphql import parse_dispatches
from chargewise.ingest.octopus_rest import (
    derive_iog_rate_periods,
    parse_account,
    parse_unit_rates,
)

UK = ZoneInfo("Europe/London")

ACCOUNT = {
    "number": "A-A8D3E32A",
    "properties": [{
        "electricity_meter_points": [{
            "mpan": "2000051688869",
            "meters": [{"serial_number": "19L2139904"}],
            "agreements": [
                {"tariff_code": "E-1R-INTELLI-VAR-24-10-29-H",
                 "valid_from": "2024-11-01T00:00:00Z", "valid_to": None},
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

# HA / GraphQL dispatch shape: daytime smart-charge slot at home.
DISPATCHES = [
    {"start": "2026-06-15T13:00:00+01:00", "end": "2026-06-15T13:30:00+01:00",
     "location": "AT_HOME"},
]


def test_parse_account_extracts_meter_and_agreement():
    acct = parse_account(ACCOUNT)
    assert acct["mpan"] == "2000051688869"
    assert acct["serial_number"] == "19L2139904"
    assert acct["agreements"][0].tariff_code == "E-1R-INTELLI-VAR-24-10-29-H"
    assert acct["agreements"][0].valid_to is None


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
