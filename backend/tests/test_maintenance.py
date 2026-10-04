"""Maintenance commands: repair-miles and recost.

Both must fix what they are for, report a before/after summary, change nothing
under ``--dry-run`` and change nothing on a second run. Synthetic data only;
the Octopus fetch is replaced by a fake, so nothing touches the network.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from chargewise import maintenance
from chargewise.config import Settings
from chargewise.engine.models import Dispatch, RatePeriod
from chargewise.ingest import pipeline
from chargewise.ingest.fuel_prices import FuelPriceWeek
from chargewise.store import repositories as repo
from chargewise.store.db import init_db, make_engine, make_session_factory
from chargewise.store.models import ChargeSession as StoredSession
from chargewise.store.models import StoredDispatch

UK = ZoneInfo("Europe/London")
SETTINGS = Settings(database_url="sqlite://", petrol_mpg=30.0, fuel_type="petrol")

RATE_PERIODS = [
    RatePeriod(datetime(2026, 1, 1, tzinfo=UK), None, offpeak_inc_vat=0.07, peak_inc_vat=0.30)
]
DISPATCH = Dispatch(
    datetime(2026, 6, 16, 13, 0, tzinfo=UK), datetime(2026, 6, 16, 14, 0, tzinfo=UK), "AT_HOME"
)

# (vehicle, start, end, location, kWh, stored cost, odometer, stored miles)
SESSIONS = [
    # Tesla 2: night charge, daytime charge under a dispatch, daytime charge without.
    (1, "2026-06-15T00:30:00+01:00", "2026-06-15T01:30:00+01:00", "home", 10.0, 9.99, 1000.0, None),
    (1, "2026-06-16T13:00:00+01:00", "2026-06-16T14:00:00+01:00", "home", 8.0, 2.40, 1040.0, None),
    (1, "2026-06-17T13:00:00+01:00", "2026-06-17T14:00:00+01:00", "home", 6.0, 1.80, 1100.0, 60.0),
    (1, "2026-06-18T12:00:00+01:00", "2026-06-18T12:30:00+01:00", "away", 20.0, 9.00, 1250.0, None),
    # Tesla 1: its own odometer chain; one stored value is simply wrong.
    (2, "2026-06-15T02:00:00+01:00", "2026-06-15T03:00:00+01:00", "home", 5.0, 0.35, 500.0, None),
    (2, "2026-06-16T02:00:00+01:00", "2026-06-16T03:00:00+01:00", "home", 5.0, 0.35, 530.0, 999.0),
    # Before any rate period: recost cannot price it and must leave it alone.
    # It is also the car's first reading, so the miles stored on it have no basis.
    (2, "2025-12-01T02:00:00+00:00", "2025-12-01T03:00:00+00:00", "home", 4.0, 1.23, 400.0, 5.0),
]


def populate(db) -> None:
    repo.get_or_create_vehicle(db, "Tesla 2")
    repo.get_or_create_vehicle(db, "Tesla 1")
    repo.upsert_fuel_weeks(db, [FuelPriceWeek(date(2025, 11, 24), 140.0, 150.0)])
    for vehicle, start, end, location, kwh, cost, odometer, miles in SESSIONS:
        db.add(StoredSession(
            vehicle_id=vehicle, start_utc=start, end_utc=end, location_type=location,
            energy_kwh=kwh, cost_gbp=cost, cost_is_estimate=location == "away",
            odometer=odometer, miles=miles, source="teslafi_history",
        ))
    db.commit()


def fresh_db():
    engine = make_engine("sqlite://")
    init_db(engine)
    db = make_session_factory(engine)()
    populate(db)
    return db


def rows(db) -> list[tuple]:
    db.expire_all()
    return [
        (s.id, s.vehicle_id, s.start_utc, s.end_utc, s.location_type, s.energy_kwh,
         s.cost_gbp, s.cost_is_estimate, s.odometer, s.miles, s.source)
        for s in repo.list_charge_sessions(db)
    ]


def miles_by_start(db) -> dict[str, float | None]:
    return {r[2]: r[9] for r in rows(db)}


def cost_by_start(db) -> dict[str, float]:
    return {r[2]: r[6] for r in rows(db)}


# --------------------------------------------------------------------------- #
# repair-miles
# --------------------------------------------------------------------------- #

def test_repair_miles_recomputes_every_session_from_odometers():
    db = fresh_db()
    summary = maintenance.repair_miles(db, settings=SETTINGS)

    assert miles_by_start(db) == {
        "2025-12-01T02:00:00+00:00": None,     # Tesla 1's first reading (was 5)
        "2026-06-15T00:30:00+01:00": None,     # Tesla 2's first reading
        "2026-06-15T02:00:00+01:00": 100.0,    # Tesla 1: 500 - 400
        "2026-06-16T02:00:00+01:00": 30.0,     # Tesla 1: 530 - 500 (was 999)
        "2026-06-16T13:00:00+01:00": 40.0,     # Tesla 2: was blank
        "2026-06-17T13:00:00+01:00": 60.0,     # Tesla 2: already right
        "2026-06-18T12:00:00+01:00": 150.0,    # Tesla 2: was blank
    }
    assert (summary["examined"], summary["changed"]) == (7, 5)
    assert (summary["filled"], summary["corrected"], summary["cleared"]) == (3, 1, 1)
    assert summary["before"]["total_miles"] == 1064.0
    assert summary["after"]["total_miles"] == 380.0
    # Costs are not this command's business.
    assert summary["after"]["total_cost_gbp"] == summary["before"]["total_cost_gbp"]
    assert summary["after"]["session_count"] == 7
    assert set(summary["before"]) == {key for key, _ in maintenance.SUMMARY_FIELDS}


def test_repair_miles_dry_run_reports_but_changes_nothing():
    db = fresh_db()
    before = rows(db)
    dry = maintenance.repair_miles(db, dry_run=True, settings=SETTINGS)
    assert rows(db) == before
    assert dry["dry_run"] is True and dry["changed"] == 5

    real = maintenance.repair_miles(db, settings=SETTINGS)
    assert dry["after"] == real["after"]   # the preview was what then happened
    assert rows(db) != before


def test_repair_miles_second_run_changes_nothing():
    db = fresh_db()
    first = maintenance.repair_miles(db, settings=SETTINGS)
    after_first = rows(db)
    second = maintenance.repair_miles(db, settings=SETTINGS)
    assert second["changed"] == 0
    assert second["before"] == second["after"] == first["after"]
    assert rows(db) == after_first


def test_repair_miles_restores_saving_lost_to_blanked_miles():
    db = fresh_db()
    summary = maintenance.repair_miles(db, settings=SETTINGS)
    # 380 miles at 30 mpg and 140p/litre; the stray 999 had inflated it before.
    assert summary["after"]["petrol_equiv_gbp"] == pytest.approx(80.62, abs=0.01)
    assert summary["after"]["saving_gbp"] == pytest.approx(
        summary["after"]["petrol_equiv_gbp"] - summary["after"]["total_cost_gbp"], abs=0.011
    )


# --------------------------------------------------------------------------- #
# recost
# --------------------------------------------------------------------------- #

EXPECTED_COSTS = {
    "2025-12-01T02:00:00+00:00": 1.23,          # no rate period: untouched
    "2026-06-15T00:30:00+01:00": 10.0 * 0.07,   # core off-peak window
    "2026-06-15T02:00:00+01:00": 5.0 * 0.07,    # already right
    "2026-06-16T02:00:00+01:00": 5.0 * 0.07,    # already right
    "2026-06-16T13:00:00+01:00": 8.0 * 0.07,    # daytime, covered by the dispatch
    "2026-06-17T13:00:00+01:00": 6.0 * 0.30,    # daytime, no dispatch: peak (already right)
    "2026-06-18T12:00:00+01:00": 9.00,          # away: untouched
}


def test_recost_reprices_home_sessions_and_leaves_away_alone():
    db = fresh_db()
    summary = maintenance.recost_sessions(db, RATE_PERIODS, [DISPATCH], settings=SETTINGS)

    assert cost_by_start(db) == pytest.approx(EXPECTED_COSTS)
    assert (summary["examined"], summary["changed"], summary["unpriced"]) == (6, 2, 1)
    assert summary["unpriced_first"] == ["2025-12-01T02:00:00+00:00"]
    assert (summary["raised"], summary["lowered"]) == (0, 2)
    assert summary["before"]["away_cost_gbp"] == summary["after"]["away_cost_gbp"] == 9.0
    assert summary["before"]["home_cost_gbp"] == pytest.approx(16.12)
    assert summary["after"]["home_cost_gbp"] == pytest.approx(4.99)
    away = [r for r in rows(db) if r[4] == "away"]
    assert [(r[6], r[7]) for r in away] == [(9.0, True)]   # cost and estimate flag kept


def test_recost_without_the_dispatch_prices_daytime_at_peak():
    db = fresh_db()
    maintenance.recost_sessions(db, RATE_PERIODS, [], settings=SETTINGS)
    assert cost_by_start(db)["2026-06-16T13:00:00+01:00"] == pytest.approx(8.0 * 0.30)


def test_recost_dry_run_reports_but_changes_nothing():
    db = fresh_db()
    before = rows(db)
    dry = maintenance.recost_sessions(db, RATE_PERIODS, [DISPATCH], dry_run=True,
                                      settings=SETTINGS)
    assert rows(db) == before
    assert dry["dry_run"] is True and dry["changed"] == 2

    real = maintenance.recost_sessions(db, RATE_PERIODS, [DISPATCH], settings=SETTINGS)
    assert dry["after"] == real["after"]
    assert rows(db) != before


def test_recost_second_run_changes_nothing():
    db = fresh_db()
    first = maintenance.recost_sessions(db, RATE_PERIODS, [DISPATCH], settings=SETTINGS)
    after_first = rows(db)
    second = maintenance.recost_sessions(db, RATE_PERIODS, [DISPATCH], settings=SETTINGS)
    assert second["changed"] == 0
    assert second["before"] == second["after"] == first["after"]
    assert rows(db) == after_first


# --------------------------------------------------------------------------- #
# The command wrappers: configured database, Octopus fetch, CLI.
# --------------------------------------------------------------------------- #

def dump(path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return list(conn.iterdump())


@pytest.fixture
def file_settings(tmp_path, monkeypatch):
    """Settings pointing at a populated database file, as the commands find it."""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'cw.sqlite'}",
        octopus_api_key="dummy", octopus_account_number="A-00000000",
        petrol_mpg=30.0, fuel_type="petrol",
    )
    engine = make_engine(settings.database_url)
    init_db(engine)
    db = make_session_factory(engine)()
    populate(db)
    db.close()
    engine.dispose()
    monkeypatch.setattr(maintenance, "get_settings", lambda: settings)

    async def fake_octopus_inputs(rest, gql, account):
        assert account == "A-00000000"
        return RATE_PERIODS, [DISPATCH]

    monkeypatch.setattr(pipeline, "fetch_octopus_inputs", fake_octopus_inputs)
    return settings, tmp_path / "cw.sqlite"


def test_run_recost_fetches_rates_and_stores_fresh_dispatches(file_settings):
    settings, path = file_settings
    summary = asyncio.run(maintenance.run_recost(settings))
    assert (summary["rate_periods"], summary["dispatches_fetched"]) == (1, 1)
    assert (summary["dispatches_new"], summary["dispatches_used"]) == (1, 1)
    assert summary["changed"] == 2

    db = make_session_factory(make_engine(settings.database_url))()
    assert repo.list_dispatches(db) == [DISPATCH]            # the fresh dispatch was kept
    assert cost_by_start(db) == pytest.approx(EXPECTED_COSTS)
    db.close()

    again = asyncio.run(maintenance.run_recost(settings))
    assert (again["changed"], again["dispatches_new"], again["dispatches_used"]) == (0, 0, 1)


def test_run_recost_dry_run_writes_nothing_at_all(file_settings):
    settings, path = file_settings
    before = dump(path)
    summary = asyncio.run(maintenance.run_recost(settings, dry_run=True))
    assert dump(path) == before                              # not even the dispatch
    assert summary["changed"] == 2 and summary["dispatches_used"] == 1
    assert summary["after"]["home_cost_gbp"] == pytest.approx(4.99)


def test_run_recost_dry_run_on_a_database_without_the_dispatch_table(file_settings):
    settings, path = file_settings
    with sqlite3.connect(path) as conn:
        conn.execute(f"DROP TABLE {StoredDispatch.__tablename__}")   # a pre-upgrade file
    before = dump(path)
    summary = asyncio.run(maintenance.run_recost(settings, dry_run=True))
    assert dump(path) == before                              # the table was not created
    assert summary["changed"] == 2


def test_run_recost_needs_octopus_credentials(file_settings):
    settings, path = file_settings
    before = dump(path)
    with pytest.raises(RuntimeError, match="OCTOPUS_API_KEY"):
        asyncio.run(maintenance.run_recost(settings.model_copy(update={"octopus_api_key": ""})))
    assert dump(path) == before


def test_cli_repair_miles_dry_run_then_real(file_settings, capsys):
    settings, path = file_settings
    before = dump(path)

    maintenance.main(["repair-miles", "--dry-run"])
    out = capsys.readouterr().out
    assert dump(path) == before
    assert "repair-miles (DRY RUN - nothing written)" in out
    assert "miles changed: 5 (filled 3, corrected 1, cleared 1)" in out
    for _, label in maintenance.SUMMARY_FIELDS:
        assert label in out
    assert "before" in out and "after" in out

    maintenance.main(["repair-miles"])
    out = capsys.readouterr().out
    assert "DRY RUN" not in out
    assert dump(path) != before

    maintenance.main(["repair-miles"])
    assert "miles changed: 0" in capsys.readouterr().out


def test_cli_recost_exits_non_zero_when_a_session_is_left_unpriced(file_settings, capsys):
    settings, path = file_settings
    with pytest.raises(SystemExit) as stopped:
        maintenance.main(["recost"])
    assert stopped.value.code == 1

    # The summary is still printed ...
    captured = capsys.readouterr()
    assert "ChargeWise recost\n" in captured.out
    assert "home sessions examined: 6, cost changed: 2 (0 up, 2 down)" in captured.out
    assert "no rate period covers them: 1" in captured.out
    assert "dispatches used: 1 (1 from the feed, 1 new)" in captured.out
    assert "Saving (GBP)" in captured.out
    # ... the sessions it could price were written ...
    db = make_session_factory(make_engine(settings.database_url))()
    assert cost_by_start(db) == pytest.approx(EXPECTED_COSTS)
    db.close()
    # ... and stderr names how many were not, and which.
    assert "recost incomplete: 1 home session(s) could not be priced" in captured.err
    assert "First: 2025-12-01T02:00:00+00:00\n" in captured.err


def test_cli_recost_dry_run_also_exits_non_zero_and_writes_nothing(file_settings, capsys):
    settings, path = file_settings
    before = dump(path)
    with pytest.raises(SystemExit) as stopped:
        maintenance.main(["recost", "--dry-run"])
    assert stopped.value.code == 1
    assert dump(path) == before
    assert "recost incomplete: 1 home session(s)" in capsys.readouterr().err


def test_cli_recost_exits_zero_when_every_home_session_is_priced(file_settings, capsys):
    settings, path = file_settings
    with sqlite3.connect(path) as conn:   # drop the one session no rate period covers
        conn.execute("DELETE FROM charge_session WHERE start_utc LIKE '2025-12-01%'")
    maintenance.main(["recost"])          # returns normally: exit status 0
    captured = capsys.readouterr()
    assert "home sessions examined: 5, cost changed: 2 (0 up, 2 down)" in captured.out
    assert "no rate period covers them: 0" in captured.out
    assert captured.err == ""


def test_cli_recost_names_only_the_first_few_unpriced_sessions(file_settings, capsys, monkeypatch):
    async def no_rates(rest, gql, account):
        return [], []

    monkeypatch.setattr(pipeline, "fetch_octopus_inputs", no_rates)
    monkeypatch.setattr(maintenance, "UNPRICED_SHOWN", 2)
    with pytest.raises(SystemExit) as stopped:
        maintenance.main(["recost"])
    assert stopped.value.code == 1
    err = capsys.readouterr().err
    assert "recost incomplete: 6 home session(s) could not be priced" in err
    # Earliest first, and an ellipsis because there are more than were named.
    assert "First: 2025-12-01T02:00:00+00:00, 2026-06-15T00:30:00+01:00, ...\n" in err


def test_cli_recost_failure_is_explained_and_still_fails(file_settings, capsys, monkeypatch):
    settings, path = file_settings
    before = dump(path)

    async def rejected(rest, gql, account):
        import httpx

        request = httpx.Request("GET", "https://api.octopus.energy/v1/accounts/A-00000000/")
        raise httpx.HTTPStatusError(
            "Client error '401 Unauthorized'", request=request,
            response=httpx.Response(401, request=request),
        )

    monkeypatch.setattr(pipeline, "fetch_octopus_inputs", rejected)
    with pytest.raises(Exception, match="401"):
        maintenance.main(["recost"])
    assert "Octopus rejected the API key" in capsys.readouterr().err
    assert dump(path) == before


def test_cli_requires_a_command():
    with pytest.raises(SystemExit):
        maintenance.main([])
