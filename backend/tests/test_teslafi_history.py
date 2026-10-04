"""Tests for the TeslaFi history adapter.

Fixture records mirror the payload shapes of the live API: floats as strings,
empty strings for missing values, ``date`` in UTC, ``totalMinutes`` as the
plugged-in window. Every value is synthetic, and the VINs are placeholders.
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
        "date": "2023-03-15 02:30:45",
        "totalMinutes": 150,
        "minutes": 45,
        "chargerKWH": "20.5000000",
        "totalEnergyAdded": "19.5",
        "energyAdded": "19.5",
        "odometer": "12345.6789",
        "homeChargeFlag": 1,
        "superChargerFlag": 0,
        "travelChargerFlag": 0,
        "superCost": None,
        "travelCost": None,
        "homeCost": 1.5,
        "vin": "TESTVIN0000000001",
        "model": "modely",
    }
    base.update(overrides)
    return base


def test_home_charge_maps_to_utc_session_with_wall_energy() -> None:
    charges = parse_teslafi_charges({"count": 1, "results": [_record()]})
    assert len(charges) == 1
    charge = charges[0]
    session = charge.session
    assert session.start == datetime(2023, 3, 15, 2, 30, 45, tzinfo=timezone.utc)
    assert session.end == session.start + timedelta(minutes=150)
    assert session.energy_kwh == 20.5             # chargerKWH preferred
    assert session.location_type is LocationType.HOME
    assert session.odometer == 12345.6789
    assert session.raw_cost is None               # home is re-costed by the engine
    assert charge.vin == "TESTVIN0000000001"


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
    records = [_record(date="2023-03-20 03:00:00"), _record(date="2023-03-10 04:00:00")]
    charges = parse_teslafi_charges({"results": records})
    assert [c.session.start.day for c in charges] == [10, 20]


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
        _record(date="2024-09-01 10:00:00", vin="TESTVIN0000000002", model="modely"),
    ]
    charges = parse_teslafi_charges({"results": records})
    grouped = group_by_vin(charges)
    assert len(grouped) == 2
    mapping = {"TESTVIN0000000002": "Tesla 2"}
    names = {vehicle_name_for(vin, model, mapping) for vin, model in grouped}
    assert names == {"Modely (000001)", "Tesla 2"}
