"""Sync-status tracking: last successful ingest per source + data freshness.

Motivated by two silent TeslaFi data gaps (Jan–May and post-4-Jul 2026): a
sync can succeed while the source's own feed is stalled, so the dashboard
needs both "when did we last sync" and "how fresh is the data".
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from chargewise.api.app import create_app
from chargewise.config import Settings
from chargewise.ingest.fuel_prices import FuelPriceWeek
from chargewise.store import repositories as repo
from chargewise.store.db import init_db, make_engine, make_session_factory


def fresh_session():
    engine = make_engine("sqlite://")
    init_db(engine)
    return make_session_factory(engine)(), engine


def test_record_sync_and_status_roundtrip() -> None:
    db, _ = fresh_session()
    repo.record_sync(db, "fuel", "2026-07-11T04:45:00+00:00")
    repo.record_sync(db, "teslafi")  # defaults to now
    status = repo.sync_status(db)
    assert status["fuel"]["last_success_utc"] == "2026-07-11T04:45:00+00:00"
    assert status["teslafi"]["last_success_utc"] is not None
    assert status["octopus"]["last_success_utc"] is None  # never synced


def test_status_reports_data_freshness_separately_from_sync() -> None:
    """A successful sync with stale data must show both facts."""
    db, _ = fresh_session()
    v = repo.get_or_create_vehicle(db, "Friday")
    repo.upsert_charge_session(
        db, vehicle_id=v.id, start_utc="2026-07-04T13:30:07+00:00",
        end_utc="2026-07-04T14:30:07+00:00", location_type="home",
        energy_kwh=7.17, cost_gbp=0.49,
    )
    repo.upsert_fuel_weeks(db, [FuelPriceWeek(date(2026, 7, 6), 140.0, 150.0)])
    repo.record_sync(db, "teslafi", "2026-07-11T04:45:00+00:00")
    status = repo.sync_status(db)
    # Synced today, but newest charge is a week old — the gap is visible.
    assert status["teslafi"]["last_success_utc"].startswith("2026-07-11")
    assert status["teslafi"]["latest_charge_utc"].startswith("2026-07-04")
    assert status["fuel"]["latest_week"] == "2026-07-06"


def test_api_status_endpoint() -> None:
    db, engine = fresh_session()
    repo.record_sync(db, "octopus", "2026-07-11T04:46:00+00:00")
    app = create_app(Settings(auth_disabled=True), engine=engine)
    client = TestClient(app)
    body = client.get("/api/status").json()
    assert body["octopus"]["last_success_utc"] == "2026-07-11T04:46:00+00:00"
    assert set(body) == {"teslafi", "octopus", "fuel"}
