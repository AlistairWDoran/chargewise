"""Ingestion pipeline tests — orchestration, idempotency and end-to-end ingest.

Async functions are driven via ``asyncio.run`` with fake clients so the suite
needs no network and no pytest-asyncio plugin.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from chargewise.engine.models import ChargeSession, Dispatch, LocationType, RatePeriod
from chargewise.ingest import pipeline
from chargewise.ingest.charge_sessions import parse_charge_sessions_csv
from chargewise.ingest.fuel_prices import extract_csv_url
from chargewise.ingest.octopus_rest import (
    RateRecord,
    TariffAgreement,
    product_code_from_tariff,
    region_from_tariff,
)
from chargewise.store import repositories as repo
from chargewise.store.db import init_db, make_engine, make_session_factory

UK = ZoneInfo("Europe/London")


def fresh_session():
    engine = make_engine("sqlite://")
    init_db(engine)
    return make_session_factory(engine)()


# --------------------------------------------------------------------------- #
# Pure helpers.
# --------------------------------------------------------------------------- #

def test_product_code_and_region_from_tariff():
    assert product_code_from_tariff("E-1R-INTELLI-VAR-24-10-29-H") == "INTELLI-VAR-24-10-29"
    assert region_from_tariff("E-1R-INTELLI-VAR-24-10-29-H") == "H"


def test_product_code_rejects_malformed_tariff():
    with pytest.raises(ValueError):
        product_code_from_tariff("not-a-tariff")


def test_extract_csv_url_prefers_fuel_csv():
    html = (
        '<a href="https://assets.gov.uk/weekly-fuel-prices-2018-present.csv">CSV</a>'
        '<a href="https://assets.gov.uk/other.ods">ODS</a>'
    )
    assert extract_csv_url(html).endswith("weekly-fuel-prices-2018-present.csv")


def test_extract_csv_url_raises_when_absent():
    with pytest.raises(ValueError):
        extract_csv_url("<html>no csv here</html>")


def test_parse_charge_sessions_csv():
    csv_text = (
        "start,end,energy_kwh,location_type,odometer,raw_cost,raw_cost_is_estimate\n"
        "2026-06-15T23:30:00+01:00,2026-06-16T00:00:00+01:00,5,home,1000,,\n"
        "2026-06-17T12:00:00+01:00,2026-06-17T12:30:00+01:00,8,away,1100,4.50,false\n"
    )
    sessions = parse_charge_sessions_csv(csv_text)
    assert len(sessions) == 2
    assert sessions[0].location_type is LocationType.HOME
    assert sessions[0].energy_kwh == 5.0
    assert sessions[1].location_type is LocationType.AWAY
    assert sessions[1].raw_cost == 4.50
    assert sessions[1].raw_cost_is_estimate is False


def test_assign_miles_uses_odometer_deltas():
    def sess(day, odo):
        start = datetime(2026, 6, day, 23, 30, tzinfo=UK)
        return ChargeSession(start, start, 5.0, LocationType.HOME, odometer=odo)

    sessions = [sess(15, 100.0), sess(16, 140.0), sess(17, None), sess(18, 200.0)]
    assert pipeline.assign_miles(sessions) == [None, 40.0, None, 60.0]


def test_assign_miles_ignores_negative_delta():
    def sess(day, odo):
        start = datetime(2026, 6, day, 23, 30, tzinfo=UK)
        return ChargeSession(start, start, 5.0, LocationType.HOME, odometer=odo)

    # Odometer goes backwards (bad data) -> miles None, not negative.
    assert pipeline.assign_miles([sess(15, 100.0), sess(16, 90.0)]) == [None, None]


# --------------------------------------------------------------------------- #
# Cost-and-store (DB + engine, no network).
# --------------------------------------------------------------------------- #

RATE_PERIODS = [
    RatePeriod(
        datetime(2026, 1, 1, tzinfo=UK), None,
        offpeak_inc_vat=0.069, peak_inc_vat=0.303714,
    )
]


def _home_then_away_sessions():
    # Home session fully inside the 23:30–05:30 core off-peak window.
    home = ChargeSession(
        datetime(2026, 6, 15, 23, 30, tzinfo=UK),
        datetime(2026, 6, 16, 0, 0, tzinfo=UK),
        5.0, LocationType.HOME, odometer=1000.0,
    )
    # Away session with a recorded (non-estimate) cost.
    away = ChargeSession(
        datetime(2026, 6, 17, 12, 0, tzinfo=UK),
        datetime(2026, 6, 17, 12, 30, tzinfo=UK),
        8.0, LocationType.AWAY, odometer=1100.0,
        raw_cost=4.50, raw_cost_is_estimate=False,
    )
    return [home, away]


def test_cost_and_store_costs_and_persists():
    db = fresh_session()
    vehicle = repo.get_or_create_vehicle(db, "Friday")
    result = pipeline.cost_and_store_sessions(
        db, vehicle.id, _home_then_away_sessions(), RATE_PERIODS, [], away_rate=0.50,
    )
    assert result == {"processed": 2, "inserted": 2}

    sessions = repo.list_charge_sessions(db)
    home, away = sessions[0], sessions[1]
    # Home: 5 kWh entirely in the core window @ £0.069.
    assert home.cost_gbp == pytest.approx(5.0 * 0.069)
    assert home.cost_is_estimate is False
    assert home.location_type == "home"
    # Away: uses the recorded cost; miles = 1100 - 1000.
    assert away.cost_gbp == pytest.approx(4.50)
    assert away.miles == pytest.approx(100.0)


def test_cost_and_store_is_idempotent():
    db = fresh_session()
    vehicle = repo.get_or_create_vehicle(db, "Friday")
    sessions = _home_then_away_sessions()
    pipeline.cost_and_store_sessions(db, vehicle.id, sessions, RATE_PERIODS, [], 0.50)
    second = pipeline.cost_and_store_sessions(db, vehicle.id, sessions, RATE_PERIODS, [], 0.50)
    assert second == {"processed": 2, "inserted": 0}
    assert len(repo.list_charge_sessions(db)) == 2


def test_cost_and_store_dispatch_makes_daytime_offpeak():
    db = fresh_session()
    vehicle = repo.get_or_create_vehicle(db, "Friday")
    # Daytime home session that would be peak, but a dispatch covers it.
    session = ChargeSession(
        datetime(2026, 6, 15, 13, 0, tzinfo=UK),
        datetime(2026, 6, 15, 13, 30, tzinfo=UK),
        5.0, LocationType.HOME, odometer=500.0,
    )
    dispatches = [
        Dispatch(
            datetime(2026, 6, 15, 13, 0, tzinfo=UK),
            datetime(2026, 6, 15, 13, 30, tzinfo=UK),
            "AT_HOME",
        )
    ]
    pipeline.cost_and_store_sessions(db, vehicle.id, [session], RATE_PERIODS, dispatches, 0.50)
    stored = repo.list_charge_sessions(db)[0]
    assert stored.cost_gbp == pytest.approx(5.0 * 0.069)  # off-peak via dispatch


# --------------------------------------------------------------------------- #
# Network fetchers, driven with fakes.
# --------------------------------------------------------------------------- #

class _FakeRest:
    async def get_account(self, account_number):
        return {
            "mpan": "2000051688869",
            "serial_number": "19L2139904",
            "agreements": [
                TariffAgreement(
                    "E-1R-INTELLI-VAR-24-10-29-H",
                    datetime(2024, 11, 1, tzinfo=UK), None,
                )
            ],
        }

    async def get_unit_rates(self, product, tariff_code, period_from, period_to):
        assert product == "INTELLI-VAR-24-10-29"
        return [
            RateRecord(datetime(2026, 6, 15, 4, 30, tzinfo=UK),
                       datetime(2026, 6, 15, 22, 30, tzinfo=UK), 30.3714, 28.9251),
            RateRecord(datetime(2026, 6, 15, 22, 30, tzinfo=UK), None, 6.9, 6.571),
        ]


class _FakeGql:
    async def get_completed_dispatches(self, account_number):
        return [
            Dispatch(
                datetime(2026, 6, 15, 13, 0, tzinfo=UK),
                datetime(2026, 6, 15, 13, 30, tzinfo=UK),
                "AT_HOME",
            )
        ]


def test_fetch_octopus_inputs_builds_periods_and_dispatches():
    periods, dispatches = asyncio.run(
        pipeline.fetch_octopus_inputs(_FakeRest(), _FakeGql(), "A-A8D3E32A")
    )
    assert len(periods) == 1
    assert periods[0].offpeak_inc_vat == pytest.approx(6.9)
    assert periods[0].peak_inc_vat == pytest.approx(30.3714)
    assert len(dispatches) == 1


# --------------------------------------------------------------------------- #
# End-to-end run_pipeline with network monkeypatched.
# --------------------------------------------------------------------------- #

FUEL_CSV = (
    "Date,ULSP Pump price in pence/litre,ULSD Pump price in pence/litre,"
    "ULSP Duty,ULSD Duty,ULSP VAT,ULSD VAT\n"
    "09/06/2026,140.00,150.00,52.95,52.95,20,20\n"
)


def test_run_pipeline_end_to_end(monkeypatch, tmp_path):
    from chargewise.config import Settings

    settings = Settings(
        database_url="sqlite://",
        octopus_api_key="dummy",
        octopus_account_number="A-A8D3E32A",
    )

    async def fake_fetch_fuel_csv(url):
        return FUEL_CSV

    async def fake_octopus_inputs(rest, gql, account):
        return RATE_PERIODS, []

    monkeypatch.setattr(pipeline, "fetch_fuel_csv", fake_fetch_fuel_csv)
    monkeypatch.setattr(pipeline, "fetch_octopus_inputs", fake_octopus_inputs)

    csv_path = tmp_path / "charges.csv"
    csv_path.write_text(
        "start,end,energy_kwh,location_type,odometer\n"
        "2026-06-15T23:30:00+01:00,2026-06-16T00:00:00+01:00,5,home,1000\n"
        "2026-06-16T23:30:00+01:00,2026-06-17T00:00:00+01:00,6,home,1140\n",
        encoding="utf-8",
    )

    summary = asyncio.run(
        pipeline.run_pipeline(
            settings,
            charges_csv=str(csv_path),
            vehicle_name="Friday",
            fuel_url="https://example/fuel.csv",  # skip the landing-page resolver
        )
    )
    assert summary["fuel_weeks_inserted"] == 1
    assert summary["rate_periods"] == 1
    assert summary["charges"] == {"processed": 2, "inserted": 2}


def test_run_pipeline_requires_octopus_credentials(monkeypatch):
    from chargewise.config import Settings

    settings = Settings(database_url="sqlite://", octopus_api_key="", octopus_account_number="")

    async def fake_fetch_fuel_csv(url):
        return FUEL_CSV

    monkeypatch.setattr(pipeline, "fetch_fuel_csv", fake_fetch_fuel_csv)

    with pytest.raises(RuntimeError, match="OCTOPUS_API_KEY"):
        asyncio.run(
            pipeline.run_pipeline(settings, charges_csv=None, fuel_url="https://example/fuel.csv")
        )


def test_run_pipeline_fuel_only(monkeypatch):
    from chargewise.config import Settings

    settings = Settings(database_url="sqlite://")

    async def fake_fetch_fuel_csv(url):
        return FUEL_CSV

    monkeypatch.setattr(pipeline, "fetch_fuel_csv", fake_fetch_fuel_csv)

    summary = asyncio.run(
        pipeline.run_pipeline(settings, fuel_only=True, fuel_url="https://example/fuel.csv")
    )
    assert summary == {"fuel_weeks_inserted": 1}
