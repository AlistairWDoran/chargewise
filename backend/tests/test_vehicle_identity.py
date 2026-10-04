"""A vehicle is identified by its VIN; its name is only a label.

``get_or_create_vehicle`` looks a vehicle up by VIN before it looks by name, so
a run whose VEHICLE_MAP is missing, incomplete or spelt differently finds the
cars already stored instead of starting new ones and storing their charges a
second time. Every VIN here is a placeholder.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from chargewise.config import Settings, parse_vehicle_map
from chargewise.engine.models import RatePeriod
from chargewise.ingest import pipeline
from chargewise.ingest.teslafi_history import parse_teslafi_charges
from chargewise.store import repositories as repo
from chargewise.store.db import init_db, make_engine, make_session_factory
from chargewise.store.models import Vehicle

VIN1 = "TESTVIN0000000001"
VIN2 = "TESTVIN0000000002"
VIN3 = "TESTVIN0000000003"


def fresh_session():
    engine = make_engine("sqlite://")
    init_db(engine)
    return make_session_factory(engine)()


def vehicles(db) -> list[tuple[str, str | None]]:
    """(name, VIN) of every stored vehicle, oldest first."""
    return [(v.name, v.vin) for v in db.scalars(select(Vehicle).order_by(Vehicle.id))]


# --------------------------------------------------------------------------- #
# get_or_create_vehicle.
# --------------------------------------------------------------------------- #

def test_found_by_vin_keeps_its_stored_name() -> None:
    db = fresh_session()
    stored = repo.get_or_create_vehicle(db, "Tesla 1", vin=VIN1)

    # No map: the caller only has the default name for this VIN.
    found = repo.get_or_create_vehicle(db, "Modely (000001)", vin=VIN1)

    assert found.id == stored.id
    assert vehicles(db) == [("Tesla 1", VIN1)]


def test_explicit_name_for_the_vin_renames_the_vehicle() -> None:
    db = fresh_session()
    stored = repo.get_or_create_vehicle(db, "Modely (000001)", vin=VIN1)

    found = repo.get_or_create_vehicle(db, "Tesla 1", vin=VIN1, rename=True)

    assert found.id == stored.id
    assert vehicles(db) == [("Tesla 1", VIN1)]


def test_rename_is_off_unless_asked_for() -> None:
    db = fresh_session()
    repo.get_or_create_vehicle(db, "Tesla 1", vin=VIN1)
    repo.get_or_create_vehicle(db, "Another name", vin=VIN1)
    assert vehicles(db) == [("Tesla 1", VIN1)]


def test_without_a_vin_the_vehicle_is_found_by_name() -> None:
    db = fresh_session()
    first = repo.get_or_create_vehicle(db, "Tesla 2")
    again = repo.get_or_create_vehicle(db, "Tesla 2")
    assert again.id == first.id
    assert vehicles(db) == [("Tesla 2", None)]

    # A name lookup does not care what VIN the stored vehicle has.
    with_vin = repo.get_or_create_vehicle(db, "Tesla 1", vin=VIN1)
    assert repo.get_or_create_vehicle(db, "Tesla 1").id == with_vin.id
    assert len(vehicles(db)) == 2


@pytest.mark.parametrize("stored_vin", [None, ""])
def test_stored_vehicle_with_no_vin_adopts_it(stored_vin: str | None) -> None:
    db = fresh_session()
    db.add(Vehicle(name="Tesla 2", vin=stored_vin))
    db.commit()

    found = repo.get_or_create_vehicle(db, "Tesla 2", vin=VIN2)

    assert vehicles(db) == [("Tesla 2", VIN2)]
    # From now on it is found by VIN, whatever name comes with it.
    assert repo.get_or_create_vehicle(db, "Modely (000002)", vin=VIN2).id == found.id
    assert vehicles(db) == [("Tesla 2", VIN2)]


def test_same_name_with_a_different_vin_is_a_different_car() -> None:
    db = fresh_session()
    first = repo.get_or_create_vehicle(db, "Tesla", vin=VIN1)

    second = repo.get_or_create_vehicle(db, "Tesla", vin=VIN2)

    assert second.id != first.id
    assert vehicles(db) == [("Tesla", VIN1), ("Tesla", VIN2)]
    # Each is then found by its own VIN.
    assert repo.get_or_create_vehicle(db, "Tesla", vin=VIN1).id == first.id
    assert repo.get_or_create_vehicle(db, "Tesla", vin=VIN2).id == second.id
    assert len(vehicles(db)) == 2


def test_adoption_skips_a_namesake_that_has_another_vin() -> None:
    db = fresh_session()
    repo.get_or_create_vehicle(db, "Tesla", vin=VIN1)
    db.add(Vehicle(name="Tesla", vin=None))
    db.commit()

    repo.get_or_create_vehicle(db, "Tesla", vin=VIN2)

    assert vehicles(db) == [("Tesla", VIN1), ("Tesla", VIN2)]


def test_vin_is_matched_without_regard_to_case() -> None:
    db = fresh_session()
    stored = repo.get_or_create_vehicle(db, "Tesla 1", vin=VIN1.lower())
    assert repo.get_or_create_vehicle(db, "Modely (000001)", vin=VIN1).id == stored.id
    assert repo.get_or_create_vehicle(db, "Modely (000001)", vin=VIN1.title()).id == stored.id
    assert len(vehicles(db)) == 1


def test_vin_is_stored_trimmed_and_matched_trimmed() -> None:
    db = fresh_session()
    created = repo.get_or_create_vehicle(db, "Tesla 1", vin=f"  {VIN1} ")
    assert vehicles(db) == [("Tesla 1", VIN1)]            # stored without the padding
    assert repo.get_or_create_vehicle(db, "Modely (000001)", vin=f"{VIN1}\n").id == created.id

    # A VIN that was stored padded (by an earlier version, or by hand) still matches.
    db.add(Vehicle(name="Tesla 2", vin=f" {VIN2.lower()}  "))
    db.commit()
    padded = db.scalar(select(Vehicle).where(Vehicle.name == "Tesla 2"))
    assert repo.get_or_create_vehicle(db, "Modely (000002)", vin=VIN2).id == padded.id
    assert len(vehicles(db)) == 2


@pytest.mark.parametrize("padding", ["\t", "\r\n", "\n", " \t "])
def test_vin_stored_with_tabs_or_line_ends_still_matches(padding: str) -> None:
    db = fresh_session()
    db.add(Vehicle(name="Tesla 1", vin=f"{padding}{VIN1}{padding}"))
    db.commit()
    stored = db.scalar(select(Vehicle).where(Vehicle.name == "Tesla 1"))
    assert repo.get_or_create_vehicle(db, "Modely (000001)", vin=VIN1).id == stored.id
    assert len(vehicles(db)) == 1


def test_blank_vin_is_treated_as_no_vin() -> None:
    db = fresh_session()
    first = repo.get_or_create_vehicle(db, "Tesla", vin="   ")
    assert vehicles(db) == [("Tesla", "")]
    assert repo.get_or_create_vehicle(db, "Tesla", vin="").id == first.id
    # ...and such a vehicle adopts a VIN when one arrives under the same name.
    assert repo.get_or_create_vehicle(db, "Tesla", vin=VIN1).id == first.id
    assert vehicles(db) == [("Tesla", VIN1)]


def test_oldest_vehicle_wins_when_a_vin_is_stored_twice() -> None:
    db = fresh_session()
    db.add_all([Vehicle(name="Tesla 1", vin=VIN1), Vehicle(name="Modely (000001)", vin=VIN1)])
    db.commit()
    assert repo.get_or_create_vehicle(db, "Modely (000001)", vin=VIN1).name == "Tesla 1"
    assert len(vehicles(db)) == 2


def test_unknown_vin_and_name_creates_the_vehicle() -> None:
    db = fresh_session()
    repo.get_or_create_vehicle(db, "Tesla 1", vin=VIN1)
    repo.get_or_create_vehicle(db, "Modely (000003)", vin=VIN3)
    assert vehicles(db) == [("Tesla 1", VIN1), ("Modely (000003)", VIN3)]


# --------------------------------------------------------------------------- #
# The map is consulted without regard to case.
# --------------------------------------------------------------------------- #

def test_lower_case_map_key_matches() -> None:
    # --vehicle-map flags are taken as typed; the VEHICLE_MAP setting is upper-cased.
    typed = {VIN1.lower(): "Tesla 1", f" {VIN2.title()} ": "Tesla 2"}
    assert pipeline.mapped_name(VIN1, typed) == "Tesla 1"
    assert pipeline.vehicle_name_for(VIN2, "modely", typed) == "Tesla 2"
    assert pipeline.vehicle_name_for(VIN1.lower(), "modely", {VIN1: "Tesla 1"}) == "Tesla 1"

    parsed = parse_vehicle_map(f"{VIN1.lower()}=Tesla 1")
    assert pipeline.vehicle_name_for(VIN1, "modely", parsed) == "Tesla 1"


def test_a_vin_the_map_does_not_name_has_no_explicit_name() -> None:
    assert pipeline.mapped_name(VIN3, {VIN1: "Tesla 1"}) is None
    assert pipeline.mapped_name(VIN1, None) is None
    assert pipeline.mapped_name(VIN1, {}) is None
    assert pipeline.mapped_name("", {"": "Nameless"}) is None
    assert pipeline.vehicle_name_for(VIN3, "modely", {VIN1: "Tesla 1"}) == "Modely (000003)"


# --------------------------------------------------------------------------- #
# End to end: runs of the pipeline over a database that already holds the cars.
# --------------------------------------------------------------------------- #

FUEL_CSV = (
    "Date,ULSP Pump price in pence/litre,ULSD Pump price in pence/litre,"
    "ULSP Duty,ULSD Duty,ULSP VAT,ULSD VAT\n"
    "09/06/2026,140.00,150.00,52.95,52.95,20,20\n"
)
RATE_PERIODS = [RatePeriod(datetime(2026, 1, 1, tzinfo=timezone.utc), None, 0.07, 0.30)]
MAP = {VIN1: "Tesla 1", VIN2: "Tesla 2"}


def record(vin: str, day: int, odometer: float) -> dict:
    return {
        "date": f"2026-06-{day:02d} 23:30:00", "totalMinutes": 60, "minutes": 60,
        "chargerKWH": "10.0", "energyAdded": "9.5", "odometer": str(odometer),
        "homeChargeFlag": 1, "superChargerFlag": 0, "travelChargerFlag": 0,
        "vin": vin, "model": "modely",
    }


WINDOW = [
    record(VIN1, 10, 1000.0), record(VIN1, 12, 1040.0),
    record(VIN2, 11, 500.0), record(VIN2, 13, 560.0),
]


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Run the TeslaFi stage against a file database with every network call faked."""
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'cw.sqlite'}",
        octopus_api_key="dummy", octopus_account_number="A-00000000",
        teslafi_token="dummy",
    )
    served = {"records": WINDOW}

    async def fake_fetch_fuel_csv(url):
        return FUEL_CSV

    async def fake_octopus_inputs(rest, gql, account):
        return RATE_PERIODS, []

    class FakeTeslaFi:
        def __init__(self, token):
            pass

        async def backfill(self, start, end):
            return parse_teslafi_charges({"results": served["records"]})

    monkeypatch.setattr(pipeline, "fetch_fuel_csv", fake_fetch_fuel_csv)
    monkeypatch.setattr(pipeline, "fetch_octopus_inputs", fake_octopus_inputs)
    monkeypatch.setattr(pipeline, "TeslaFiHistoryClient", FakeTeslaFi)

    def run_once(vehicle_map: dict[str, str] | None) -> dict:
        return asyncio.run(pipeline.run_pipeline(
            settings, teslafi=True, vehicle_map=vehicle_map,
            fuel_url="https://example/fuel.csv",
        ))

    def state() -> dict:
        engine = make_engine(settings.database_url)
        db = make_session_factory(engine)()
        try:
            return {
                "vehicles": vehicles(db),
                "sessions": len(repo.list_charge_sessions(db)),
                "summary": repo.lifetime_summary(db, settings.petrol_mpg, settings.fuel_type),
            }
        finally:
            db.close()
            engine.dispose()

    run_once.state = state
    run_once.served = served
    return run_once


def test_run_without_a_map_adds_nothing_to_a_database_that_holds_the_cars(run) -> None:
    first = run(MAP)
    assert first["teslafi:Tesla 1"] == {"processed": 2, "inserted": 2}
    assert first["teslafi:Tesla 2"] == {"processed": 2, "inserted": 2}
    before = run.state()
    assert before["vehicles"] == [("Tesla 1", VIN1), ("Tesla 2", VIN2)]
    assert before["sessions"] == 4

    again = run(None)          # VEHICLE_MAP missing: same window, no names

    # Reported under the stored names, and nothing was inserted.
    assert again["teslafi:Tesla 1"] == {"processed": 2, "inserted": 0}
    assert again["teslafi:Tesla 2"] == {"processed": 2, "inserted": 0}
    assert not any(key.startswith("teslafi:Modely") for key in again)
    assert run.state() == before   # no new vehicle, no new session, same totals


@pytest.mark.parametrize(
    "vehicle_map",
    [
        {VIN3: "Some other car"},                 # names neither car
        {VIN1: "Tesla 1"},                        # names only one of them
        {VIN1.lower(): "Tesla 1", VIN2.lower(): "Tesla 2"},   # typed in lower case
        {},
    ],
)
def test_run_with_a_partial_or_mismatched_map_adds_nothing(run, vehicle_map) -> None:
    run(MAP)
    before = run.state()

    result = run(vehicle_map)

    assert result["teslafi:Tesla 1"]["inserted"] == 0
    assert result["teslafi:Tesla 2"]["inserted"] == 0
    assert run.state() == before


def test_run_with_a_new_name_in_the_map_renames_and_adds_nothing(run) -> None:
    run(MAP)
    before = run.state()

    result = run({VIN1: "Tesla 1", VIN2: "The second car"})

    assert result["teslafi:The second car"] == {"processed": 2, "inserted": 0}
    after = run.state()
    assert after["vehicles"] == [("Tesla 1", VIN1), ("The second car", VIN2)]
    assert after["sessions"] == before["sessions"]
    assert after["summary"] == before["summary"]


def test_first_run_without_a_map_uses_default_names_and_a_later_map_renames(run) -> None:
    first = run(None)
    assert first["teslafi:Modely (000001)"] == {"processed": 2, "inserted": 2}
    assert run.state()["vehicles"] == [("Modely (000001)", VIN1), ("Modely (000002)", VIN2)]
    before = run.state()

    run(MAP)

    after = run.state()
    assert after["vehicles"] == [("Tesla 1", VIN1), ("Tesla 2", VIN2)]
    assert after["sessions"] == before["sessions"]
    assert after["summary"] == before["summary"]


def test_new_charges_for_a_stored_car_go_to_it_without_a_map(run) -> None:
    run(MAP)
    run.served["records"] = [*WINDOW, record(VIN2, 15, 600.0)]

    result = run(None)

    assert result["teslafi:Tesla 2"] == {"processed": 3, "inserted": 1}
    state = run.state()
    assert state["vehicles"] == [("Tesla 1", VIN1), ("Tesla 2", VIN2)]
    assert state["sessions"] == 5
    # The new session's miles follow on from the same car's last odometer reading.
    assert state["summary"]["total_miles"] == pytest.approx(40.0 + 60.0 + 40.0)
