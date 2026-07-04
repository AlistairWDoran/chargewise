"""Storage layer tests: idempotent upserts and lifetime summary with savings."""

from __future__ import annotations

from datetime import date

import pytest

from chargewise.ingest.fuel_prices import FuelPriceWeek
from chargewise.store import repositories as repo
from chargewise.store.db import init_db, make_engine, make_session_factory


def fresh_session():
    engine = make_engine("sqlite://")
    init_db(engine)
    return make_session_factory(engine)()


def test_upsert_is_idempotent():
    db = fresh_session()
    v = repo.get_or_create_vehicle(db, "Friday")
    fields = dict(
        vehicle_id=v.id, start_utc="2025-01-10T00:00:00+00:00",
        end_utc="2025-01-10T02:00:00+00:00", location_type="home",
        energy_kwh=10.0, cost_gbp=0.69, miles=40.0,
    )
    repo.upsert_charge_session(db, **fields)
    repo.upsert_charge_session(db, **fields)  # same key again
    assert len(repo.list_charge_sessions(db)) == 1


def test_upsert_refreshes_cost_on_rerun():
    """Re-ingesting an existing session must re-cost it, not skip it.

    Regression: after the pence/pounds rate fix, a re-run has to correct the
    stored costs of the 4,000+ sessions ingested with inflated rates.
    """
    db = fresh_session()
    v = repo.get_or_create_vehicle(db, "Friday")
    fields = dict(
        vehicle_id=v.id, start_utc="2025-01-10T00:00:00+00:00",
        end_utc="2025-01-10T02:00:00+00:00", location_type="home",
        energy_kwh=10.0, cost_gbp=69.0, miles=40.0,
    )
    repo.upsert_charge_session(db, **fields)
    repo.upsert_charge_session(db, **{**fields, "cost_gbp": 0.69})
    sessions = repo.list_charge_sessions(db)
    assert len(sessions) == 1
    assert sessions[0].cost_gbp == 0.69


def test_lifetime_summary_with_savings():
    db = fresh_session()
    v = repo.get_or_create_vehicle(db, "Friday")
    repo.upsert_fuel_weeks(db, [FuelPriceWeek(date(2025, 1, 6), 140.0, 150.0)])
    repo.upsert_charge_session(
        db, vehicle_id=v.id, start_utc="2025-01-10T01:00:00+00:00",
        end_utc="2025-01-10T03:00:00+00:00", location_type="home",
        energy_kwh=30.0, cost_gbp=2.07, miles=100.0,
    )
    s = repo.lifetime_summary(db, mpg=30.0, fuel_type="petrol")
    assert s["session_count"] == 1
    assert s["total_cost_gbp"] == 2.07
    assert s["total_miles"] == 100.0
    # 100 mi / 30 mpg = 3.333 gal -> 15.154 L -> @140p = £21.22
    assert s["petrol_equiv_gbp"] == pytest.approx(21.22, abs=0.05)
    assert s["saving_gbp"] == pytest.approx(19.15, abs=0.05)
    assert s["home_cost_gbp"] == 2.07
    assert s["away_cost_gbp"] == 0.0
