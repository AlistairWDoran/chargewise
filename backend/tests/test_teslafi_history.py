"""Tests for the TeslaFi history adapter.

Fixture records mirror the real payload shapes observed against the live API
(July 2026): floats as strings, empty strings for missing values, ``date`` in
UTC, ``totalMinutes`` as the plugged-in window.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from chargewise.engine.models import LocationType
from chargewise.ingest.pipeline import group_by_vin, vehicle_name_for
from chargewise.ingest.teslafi_history import (
    month_ranges,
    parse_teslafi_charges,
)


def _record(**overrides: object) -> dict:
    base = {
        "date": "2023-01-30 02:14:51",
        "totalMinutes": 164,
        "minutes": 44,
        "chargerKWH": "20.6959666",
        "totalEnergyAdded": "19.9",
        "energyAdded": "19.9",
        "odometer": "12345.6789",
        "homeChargeFlag": 1,
        "superChargerFlag": 0,
        "travelChargerFlag": 0,
        "superCost": None,
        "travelCost": None,
        "homeCost": 1.5721,
        "vin": "LRWYHCEK6NC223329",
        "model": "modely",
    }
    base.update(overrides)
    return base


def test_home_charge_maps_to_utc_session_with_wall_energy() -> None:
    charges = parse_teslafi_charges({"count": 1, "results": [_record()]})
    assert len(charges) == 1
    charge = charges[0]
    session = charge.session
    assert session.start == datetime(2023, 1, 30, 2, 14, 51, tzinfo=timezone.utc)
    assert session.end == session.start + timedelta(minutes=164)
    assert session.energy_kwh == 20.6959666       # chargerKWH preferred
    assert session.location_type is LocationType.HOME
    assert session.odometer == 12345.6789
    assert session.raw_cost is None               # home is re-costed by the engine
    assert charge.vin == "LRWYHCEK6NC223329"


def test_supercharge_with_cost_is_away_non_estimate() -> None:
    rec = _record(homeChargeFlag=0, superChargerFlag=1, superCost=12.34)
    (charge,) = parse_teslafi_charges({"results": [rec]})
    assert charge.session.location_type is LocationType.AWAY
    assert charge.session.raw_cost == 12.34
    assert charge.session.raw_cost_is_estimate is False


def test_public_charge_with_travel_cost_is_estimate() -> None:
    rec = _record(homeChargeFlag=0, travelChargerFlag=1, travelCost="4.50")
    (charge,) = parse_teslafi_charges({"results": [rec]})
    assert charge.session.raw_cost == 4.5
    assert charge.session.raw_cost_is_estimate is True


def test_zero_energy_and_zero_duration_records_skipped() -> None:
    noise = [
        _record(chargerKWH="0", totalEnergyAdded="0", energyAdded="0.0"),
        _record(totalMinutes=0, minutes=0),
        _record(date=""),
    ]
    assert parse_teslafi_charges({"results": noise}) == []


def test_empty_string_fields_fall_back_gracefully() -> None:
    rec = _record(chargerKWH="", totalEnergyAdded="", energyAdded="7.5", odometer="")
    (charge,) = parse_teslafi_charges({"results": [rec]})
    assert charge.session.energy_kwh == 7.5
    assert charge.session.odometer is None


def test_results_sorted_by_start() -> None:
    records = [_record(date="2023-01-31 03:29:47"), _record(date="2023-01-26 04:19:48")]
    charges = parse_teslafi_charges({"results": records})
    assert [c.session.start.day for c in charges] == [26, 31]


def test_month_ranges_cover_span_inclusively() -> None:
    ranges = month_ranges(date(2022, 2, 1), date(2022, 4, 15))
    assert ranges == [
        (date(2022, 2, 1), date(2022, 2, 28)),
        (date(2022, 3, 1), date(2022, 3, 31)),
        (date(2022, 4, 1), date(2022, 4, 15)),
    ]
    # December → January rollover
    assert month_ranges(date(2022, 12, 10), date(2023, 1, 5)) == [
        (date(2022, 12, 10), date(2022, 12, 31)),
        (date(2023, 1, 1), date(2023, 1, 5)),
    ]


def test_group_by_vin_and_vehicle_naming() -> None:
    records = [
        _record(),
        _record(date="2024-09-01 10:00:00", vin="XP7YHCEK0RB479220", model="modely"),
    ]
    charges = parse_teslafi_charges({"results": records})
    grouped = group_by_vin(charges)
    assert len(grouped) == 2
    mapping = {"XP7YHCEK0RB479220": "Friday"}
    names = {vehicle_name_for(vin, model, mapping) for vin, model in grouped}
    assert names == {"Modely (223329)", "Friday"}
