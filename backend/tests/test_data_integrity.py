"""Regression tests: the daily re-ingest must never destroy stored data.

Each test reproduces one way the rolling 35-day job corrupted the database:

- miles blanked for the first session of every fetch window,
- smart-charge dispatches forgotten once Octopus stopped serving them, so a
  session first priced off-peak was later re-priced at the peak rate,
- a second row inserted when a charge came back with a different energy figure,

plus the schema step the dispatch fix needs: an existing database gains the
new table at start-up without losing anything. All data here is synthetic.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from chargewise.config import Settings
from chargewise.engine.models import ChargeSession, Dispatch, LocationType, RatePeriod
from chargewise.ingest import pipeline
from chargewise.ingest.fuel_prices import FuelPriceWeek
from chargewise.store import repositories as repo
from chargewise.store.db import init_db, make_engine, make_session_factory
from chargewise.store.models import ChargeSession as StoredSession

UK = ZoneInfo("Europe/London")

RATE_PERIODS = [
    RatePeriod(datetime(2026, 1, 1, tzinfo=UK), None, offpeak_inc_vat=0.07, peak_inc_vat=0.30)
]

FUEL_CSV = (
    "Date,ULSP Pump price in pence/litre,ULSD Pump price in pence/litre,"
    "ULSP Duty,ULSD Duty,ULSP VAT,ULSD VAT\n"
    "09/06/2026,140.00,150.00,52.95,52.95,20,20\n"
)


def fresh_session():
    engine = make_engine("sqlite://")
    init_db(engine)
    return make_session_factory(engine)()


def open_file_db(url: str):
    engine = make_engine(url)
    init_db(engine)
    return make_session_factory(engine)()


def night_session(day: int, odometer: float | None, energy: float = 10.0) -> ChargeSession:
    """A home charge wholly inside the off-peak core window."""
    start = datetime(2026, 6, day, 0, 30, tzinfo=UK)
    return ChargeSession(start, start + timedelta(hours=1), energy, LocationType.HOME,
                         odometer=odometer)


# --------------------------------------------------------------------------- #
# 1. Mileage survives a sliding fetch window.
# --------------------------------------------------------------------------- #

def test_assign_miles_seed_gives_first_session_its_miles():
    sessions = [night_session(16, 1040.0), night_session(17, 1100.0)]
    assert pipeline.assign_miles(sessions) == [None, 60.0]            # unchanged default
    assert pipeline.assign_miles(sessions, 1000.0) == [40.0, 60.0]    # seeded
    # A seed above the first reading is a negative delta: None, never negative.
    assert pipeline.assign_miles(sessions, 2000.0) == [None, 60.0]


def test_sliding_window_keeps_miles_and_saving():
    db = fresh_session()
    vehicle = repo.get_or_create_vehicle(db, "Tesla 2")
    repo.upsert_fuel_weeks(db, [FuelPriceWeek(date(2026, 6, 8), 140.0, 150.0)])
    first, second, third = (
        night_session(15, 1000.0), night_session(16, 1040.0), night_session(17, 1100.0),
    )

    pipeline.cost_and_store_sessions(db, vehicle.id, [first, second, third], RATE_PERIODS, [], 0.5)
    before = repo.lifetime_summary(db, mpg=30.0)
    assert [s.miles for s in repo.list_charge_sessions(db)] == [None, 40.0, 60.0]
    assert before["saving_gbp"] > 0

    # A day later the window has slid: only the last two sessions are fetched.
    result = pipeline.cost_and_store_sessions(
        db, vehicle.id, [second, third], RATE_PERIODS, [], 0.5
    )
    assert result == {"processed": 2, "inserted": 0}
    assert [s.miles for s in repo.list_charge_sessions(db)] == [None, 40.0, 60.0]
    assert repo.lifetime_summary(db, mpg=30.0) == before


def test_window_seed_is_per_vehicle_and_strictly_earlier():
    db = fresh_session()
    tesla_2 = repo.get_or_create_vehicle(db, "Tesla 2")
    other = repo.get_or_create_vehicle(db, "Tesla 1")
    pipeline.cost_and_store_sessions(db, other.id, [night_session(14, 50_000.0)],
                                     RATE_PERIODS, [], 0.5)
    pipeline.cost_and_store_sessions(db, tesla_2.id, [night_session(15, 1000.0)],
                                     RATE_PERIODS, [], 0.5)

    when = night_session(16, None).start
    assert repo.latest_odometer_before(db, tesla_2.id, when) == 1000.0
    assert repo.latest_odometer_before(db, other.id, when) == 50_000.0
    # Nothing earlier than the vehicle's own first session.
    assert repo.latest_odometer_before(db, tesla_2.id, night_session(15, None).start) is None

    # The other car's (much higher, earlier) odometer must not seed this one.
    pipeline.cost_and_store_sessions(db, tesla_2.id, [night_session(16, 1040.0)],
                                     RATE_PERIODS, [], 0.5)
    assert [s.miles for s in repo.list_charge_sessions(db) if s.vehicle_id == tesla_2.id] == [
        None, 40.0,
    ]


def test_seed_is_the_latest_earlier_reading_not_the_earliest_or_the_last_stored():
    db = fresh_session()
    vehicle = repo.get_or_create_vehicle(db, "Tesla 2")
    # Stored out of date order, so neither "first row" nor "last row" is the answer.
    for day, odometer in ((14, 990.0), (10, 900.0), (20, 1200.0), (12, 950.0)):
        pipeline.cost_and_store_sessions(db, vehicle.id, [night_session(day, odometer)],
                                         RATE_PERIODS, [], 0.5)

    before = night_session(16, None).start
    assert repo.latest_odometer_before(db, vehicle.id, before) == 990.0
    assert repo.latest_odometer_before(db, vehicle.id, night_session(13, None).start) == 950.0
    assert repo.latest_odometer_before(db, vehicle.id, night_session(11, None).start) == 900.0

    # So a window starting on the 16th measures its first session from the 14th.
    pipeline.cost_and_store_sessions(db, vehicle.id, [night_session(16, 1040.0)],
                                     RATE_PERIODS, [], 0.5)
    stored = {s.odometer: s.miles for s in repo.list_charge_sessions(db)}
    assert stored[1040.0] == 50.0


def test_seed_compares_instants_not_timestamp_strings():
    """With mixed UTC offsets the later instant can sort first as text."""
    db = fresh_session()
    vehicle = repo.get_or_create_vehicle(db, "Tesla 2")
    for start, odometer in (
        ("2026-06-15T23:30:00+01:00", 1000.0),   # 22:30 UTC — the earlier instant
        ("2026-06-15T22:45:00+00:00", 1007.0),   # 22:45 UTC — later, but sorts first as text
    ):
        repo.upsert_charge_session(
            db, vehicle_id=vehicle.id, start_utc=start, end_utc=start, location_type="home",
            energy_kwh=1.0, cost_gbp=0.07, odometer=odometer,
        )
    before = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)
    assert repo.latest_odometer_before(db, vehicle.id, before) == 1007.0
    # Between the two instants, only the earlier one counts.
    between = datetime(2026, 6, 15, 22, 40, tzinfo=timezone.utc)
    assert repo.latest_odometer_before(db, vehicle.id, between) == 1000.0


def test_upsert_never_blanks_stored_miles_or_odometer():
    db = fresh_session()
    vehicle = repo.get_or_create_vehicle(db, "Tesla 2")
    fields = dict(
        vehicle_id=vehicle.id, start_utc="2026-06-16T00:30:00+01:00",
        end_utc="2026-06-16T01:30:00+01:00", location_type="home",
        energy_kwh=10.0, cost_gbp=0.7, odometer=1040.0, miles=40.0,
    )
    repo.upsert_charge_session(db, **fields)
    repo.upsert_charge_session(db, **{**fields, "odometer": None, "miles": None})
    stored = repo.list_charge_sessions(db)[0]
    assert (stored.odometer, stored.miles) == (1040.0, 40.0)
    # A real new value still replaces the old one.
    repo.upsert_charge_session(db, **{**fields, "miles": 41.5})
    assert repo.list_charge_sessions(db)[0].miles == 41.5


# --------------------------------------------------------------------------- #
# 2. Dispatches are stored, so a later run prices the session the same.
# --------------------------------------------------------------------------- #

DISPATCH = Dispatch(
    datetime(2026, 6, 15, 13, 0, tzinfo=UK), datetime(2026, 6, 15, 13, 30, tzinfo=UK), "AT_HOME"
)


def test_upsert_dispatches_is_idempotent_and_roundtrips():
    from chargewise.store.models import StoredDispatch

    db = fresh_session()
    # The same instant expressed in UTC is the same dispatch.
    same_in_utc = Dispatch(DISPATCH.start.astimezone(timezone.utc),
                           DISPATCH.end.astimezone(timezone.utc), "AT_HOME")
    later = Dispatch(datetime(2026, 6, 16, 10, 0, tzinfo=UK),
                     datetime(2026, 6, 16, 11, 0, tzinfo=UK))

    assert repo.upsert_dispatches(db, [DISPATCH, same_in_utc], "2026-06-16T04:00:00+00:00") == 1
    assert repo.upsert_dispatches(db, [later, DISPATCH], "2026-06-17T04:00:00+00:00") == 1
    assert repo.upsert_dispatches(db, [], "2026-06-18T04:00:00+00:00") == 0

    stored = repo.list_dispatches(db)
    assert stored == [same_in_utc, Dispatch(later.start, later.end, "unknown")]
    assert all(isinstance(d, Dispatch) and d.start.tzinfo is not None for d in stored)
    # first-seen is kept from the first sighting, not refreshed.
    first_seen = db.scalars(
        select(StoredDispatch.first_seen_utc).order_by(StoredDispatch.start_utc)
    ).all()
    assert first_seen == ["2026-06-16T04:00:00+00:00", "2026-06-17T04:00:00+00:00"]


def _run(settings: Settings, csv_path) -> dict[str, object]:
    return asyncio.run(
        pipeline.run_pipeline(
            settings, charges_csv=str(csv_path), vehicle_name="Tesla 2",
            fuel_url="https://example/fuel.csv",
        )
    )


def test_session_keeps_dispatch_price_after_dispatch_leaves_the_feed(monkeypatch, tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'cw.sqlite'}",
        octopus_api_key="dummy", octopus_account_number="A-00000000",
    )
    feed: list[Dispatch] = [DISPATCH]

    async def fake_fetch_fuel_csv(url):
        return FUEL_CSV

    async def fake_octopus_inputs(rest, gql, account):
        return RATE_PERIODS, list(feed)

    monkeypatch.setattr(pipeline, "fetch_fuel_csv", fake_fetch_fuel_csv)
    monkeypatch.setattr(pipeline, "fetch_octopus_inputs", fake_octopus_inputs)

    # A daytime home charge: peak rate unless a smart-charge dispatch covers it.
    csv_path = tmp_path / "charges.csv"
    csv_path.write_text(
        "start,end,energy_kwh,location_type,odometer\n"
        "2026-06-15T13:00:00+01:00,2026-06-15T13:30:00+01:00,5,home,1000\n",
        encoding="utf-8",
    )

    def stored_cost() -> float:
        db = open_file_db(settings.database_url)
        try:
            return repo.list_charge_sessions(db)[0].cost_gbp
        finally:
            db.close()

    first = _run(settings, csv_path)
    assert stored_cost() == pytest.approx(5 * 0.07)

    # Later, Octopus no longer serves that dispatch.
    feed.clear()
    second = _run(settings, csv_path)
    assert stored_cost() == pytest.approx(5 * 0.07)   # not re-priced at the 0.30 peak rate

    assert (first["dispatches"], first["dispatches_new"], first["dispatches_stored"]) == (1, 1, 1)
    assert (second["dispatches"], second["dispatches_new"], second["dispatches_stored"]) == (
        0, 0, 1,
    )


# --------------------------------------------------------------------------- #
# 3. A charge re-served with a different energy figure updates its row.
# --------------------------------------------------------------------------- #

def test_energy_drift_updates_the_row_instead_of_adding_one():
    db = fresh_session()
    vehicle = repo.get_or_create_vehicle(db, "Tesla 2")
    fields = dict(
        vehicle_id=vehicle.id, start_utc="2026-06-16T00:30:00+01:00",
        end_utc="2026-06-16T01:30:00+01:00", location_type="home",
        energy_kwh=10.0, cost_gbp=0.70, miles=40.0,
    )
    repo.upsert_charge_session(db, **fields)
    repo.upsert_charge_session(db, **{**fields, "energy_kwh": 10.4, "cost_gbp": 0.728})

    sessions = repo.list_charge_sessions(db)
    assert len(sessions) == 1
    assert sessions[0].energy_kwh == 10.4
    assert sessions[0].cost_gbp == 0.728


def test_reingest_with_drifted_energy_does_not_double_count():
    db = fresh_session()
    vehicle = repo.get_or_create_vehicle(db, "Tesla 2")
    pipeline.cost_and_store_sessions(
        db, vehicle.id, [night_session(15, 1000.0), night_session(16, 1040.0, energy=10.0)],
        RATE_PERIODS, [], 0.5,
    )
    result = pipeline.cost_and_store_sessions(
        db, vehicle.id, [night_session(16, 1040.0, energy=10.4)], RATE_PERIODS, [], 0.5,
    )
    assert result == {"processed": 1, "inserted": 0}
    summary = repo.lifetime_summary(db, mpg=30.0)
    assert summary["session_count"] == 2
    assert summary["total_energy_kwh"] == pytest.approx(20.4)
    assert summary["total_cost_gbp"] == pytest.approx(20.4 * 0.07, abs=0.005)
    assert summary["total_miles"] == 40.0


def test_upsert_with_existing_duplicates_updates_the_exact_energy_row():
    """Rows duplicated by the old (vehicle, start, energy) identity must not make
    the upsert collide with the table's unique key."""
    db = fresh_session()
    vehicle = repo.get_or_create_vehicle(db, "Tesla 2")
    base = dict(vehicle_id=vehicle.id, start_utc="2026-06-16T00:30:00+01:00",
                end_utc="2026-06-16T01:30:00+01:00", location_type="home")
    db.add_all([
        StoredSession(**base, energy_kwh=10.0, cost_gbp=0.70),
        StoredSession(**base, energy_kwh=10.4, cost_gbp=0.73),
    ])
    db.commit()

    repo.upsert_charge_session(db, **base, energy_kwh=10.4, cost_gbp=0.99)
    assert [(s.energy_kwh, s.cost_gbp) for s in repo.list_charge_sessions(db)] == [
        (10.0, 0.70), (10.4, 0.99),
    ]


# --------------------------------------------------------------------------- #
# 4. An existing database gains the dispatch table at start-up, losing nothing.
# --------------------------------------------------------------------------- #

class OldBase(DeclarativeBase):
    """The ORM models exactly as they were before the dispatch table existed."""


class OldVehicle(OldBase):
    __tablename__ = "vehicle"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    vin: Mapped[str | None] = mapped_column(String, nullable=True)
    acquired_date: Mapped[str | None] = mapped_column(String, nullable=True)
    disposed_date: Mapped[str | None] = mapped_column(String, nullable=True)


class OldChargeSession(OldBase):
    __tablename__ = "charge_session"
    __table_args__ = (
        UniqueConstraint("vehicle_id", "start_utc", "energy_kwh", name="uq_session"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle.id"))
    start_utc: Mapped[str] = mapped_column(String)
    end_utc: Mapped[str] = mapped_column(String)
    location_type: Mapped[str] = mapped_column(String)
    energy_kwh: Mapped[float] = mapped_column(Float)
    cost_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    cost_is_estimate: Mapped[bool] = mapped_column(default=False)
    odometer: Mapped[float | None] = mapped_column(Float, nullable=True)
    miles: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String, default="teslafi_api")


class OldFuelPriceWeek(OldBase):
    __tablename__ = "fuel_price_week"
    week_start: Mapped[str] = mapped_column(String, primary_key=True)
    petrol_ppl: Mapped[float] = mapped_column(Float)
    diesel_ppl: Mapped[float] = mapped_column(Float)


class OldSetting(OldBase):
    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


def make_old_schema_db(path) -> str:
    """Create a database file from the OLD models, with a little data in it."""
    url = f"sqlite:///{path}"
    engine = make_engine(url)
    OldBase.metadata.create_all(engine)
    db = make_session_factory(engine)()
    db.add(OldVehicle(id=1, name="Tesla 1", vin="TESTVIN0000000001"))
    db.add_all([
        OldChargeSession(vehicle_id=1, start_utc="2026-06-15T23:30:00+00:00",
                         end_utc="2026-06-16T00:30:00+00:00", location_type="home",
                         energy_kwh=10.0, cost_gbp=0.7, odometer=1000.0, miles=None,
                         source="teslafi_history"),
        OldChargeSession(vehicle_id=1, start_utc="2026-06-16T23:30:00+00:00",
                         end_utc="2026-06-17T00:30:00+00:00", location_type="home",
                         energy_kwh=12.0, cost_gbp=0.84, odometer=1040.0, miles=40.0,
                         source="teslafi_history"),
    ])
    db.add(OldFuelPriceWeek(week_start="2026-06-08", petrol_ppl=140.0, diesel_ppl=150.0))
    db.add(OldSetting(key="sync:fuel", value="2026-06-17T04:00:00+00:00"))
    db.commit()
    db.close()
    engine.dispose()
    return url


def dump(path) -> list[str]:
    """Schema and every row of a SQLite file, as SQL text."""
    with sqlite3.connect(path) as conn:
        return list(conn.iterdump())


def test_existing_database_gains_dispatch_table_without_losing_data(tmp_path):
    path = tmp_path / "old.sqlite"
    url = make_old_schema_db(path)
    old = dump(path)
    assert not any("dispatch" in line for line in old)   # really is the old schema

    init_db(make_engine(url))   # what the API and the pipeline do at start-up

    new = dump(path)
    created = [line for line in new if line not in old]
    assert len(created) == 1 and created[0].startswith("CREATE TABLE dispatch")
    assert "UNIQUE (start_utc, end_utc)" in created[0]
    assert [line for line in old if line not in new] == []   # nothing dropped or altered

    # The upgraded file is fully usable by the current code.
    db = open_file_db(url)
    assert [(s.energy_kwh, s.miles) for s in repo.list_charge_sessions(db)] == [
        (10.0, None), (12.0, 40.0),
    ]
    assert repo.get_setting(db, "sync:fuel") == "2026-06-17T04:00:00+00:00"
    assert repo.upsert_dispatches(db, [DISPATCH]) == 1
    assert len(repo.list_dispatches(db)) == 1
    db.close()

    init_db(make_engine(url))   # and a second start-up changes nothing
    assert len(dump(path)) == len(new) + 1   # only the dispatch row just added
