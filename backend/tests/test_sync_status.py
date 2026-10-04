"""Sync-status tracking: last successful ingest per source + data freshness.

Motivated by silent gaps in the TeslaFi data: a
sync can succeed while the source's own feed is stalled, so the dashboard
needs both "when did we last sync" and "how fresh is the data".

And by the daily job then failing for months unnoticed: each source's last
attempt and last error are recorded too, and ``healthy`` sums them up.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from chargewise.api.app import create_app
from chargewise.config import Settings
from chargewise.engine.models import RatePeriod
from chargewise.ingest import pipeline
from chargewise.ingest.fuel_prices import FuelPriceWeek
from chargewise.ingest.octopus_graphql import OctopusGraphQLError
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
    v = repo.get_or_create_vehicle(db, "Tesla 2")
    repo.upsert_charge_session(
        db, vehicle_id=v.id, start_utc="2026-07-03T12:00:00+00:00",
        end_utc="2026-07-03T13:00:00+00:00", location_type="home",
        energy_kwh=7.0, cost_gbp=0.5,
    )
    repo.upsert_fuel_weeks(db, [FuelPriceWeek(date(2026, 7, 6), 140.0, 150.0)])
    repo.record_sync(db, "teslafi", "2026-07-11T04:45:00+00:00")
    status = repo.sync_status(db)
    # Synced today, but newest charge is over a week old — the gap is visible.
    assert status["teslafi"]["last_success_utc"].startswith("2026-07-11")
    assert status["teslafi"]["latest_charge_utc"].startswith("2026-07-03")
    assert status["fuel"]["latest_week"] == "2026-07-06"


def test_api_status_endpoint() -> None:
    db, engine = fresh_session()
    repo.record_sync(db, "octopus", "2026-07-11T04:46:00+00:00")
    app = create_app(Settings(auth_disabled=True), engine=engine)
    client = TestClient(app)
    body = client.get("/api/status").json()
    assert body["octopus"]["last_success_utc"] == "2026-07-11T04:46:00+00:00"
    assert set(body) == {"teslafi", "octopus", "fuel", "healthy"}


# --------------------------------------------------------------------------- #
# healthy: no source has an error, and every source succeeded within 36 hours.
# --------------------------------------------------------------------------- #

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def all_synced(db, hours_ago: float = 1.0) -> None:
    for source in ("teslafi", "octopus", "fuel"):
        repo.record_sync(db, source, stamp(hours_ago))


def test_status_keeps_existing_fields_and_adds_attempt_error_healthy() -> None:
    db, _ = fresh_session()
    all_synced(db)
    repo.record_sync_attempt(db, "octopus", stamp(0.5))
    assert repo.sync_status(db, now=NOW) == {
        "teslafi": {"last_success_utc": stamp(1), "latest_charge_utc": None,
                    "last_attempt_utc": None, "last_error": None},
        "octopus": {"last_success_utc": stamp(1),
                    "last_attempt_utc": stamp(0.5), "last_error": None},
        "fuel": {"last_success_utc": stamp(1), "latest_week": None,
                 "last_attempt_utc": None, "last_error": None},
        "healthy": True,
    }


def test_never_synced_is_unhealthy() -> None:
    db, _ = fresh_session()
    assert repo.sync_status(db, now=NOW)["healthy"] is False
    repo.record_sync(db, "fuel", stamp(1))
    repo.record_sync(db, "octopus", stamp(1))
    assert repo.sync_status(db, now=NOW)["healthy"] is False   # teslafi never has
    repo.record_sync(db, "teslafi", stamp(1))
    assert repo.sync_status(db, now=NOW)["healthy"] is True


@pytest.mark.parametrize("source", ["teslafi", "octopus", "fuel"])
def test_unhealthy_when_a_source_has_not_succeeded_for_36_hours(source: str) -> None:
    db, _ = fresh_session()
    all_synced(db)
    repo.record_sync(db, source, stamp(35.9))
    assert repo.sync_status(db, now=NOW)["healthy"] is True
    repo.record_sync(db, source, stamp(36.1))
    status = repo.sync_status(db, now=NOW)
    assert status["healthy"] is False
    assert status[source]["last_error"] is None   # stale, not failed: both are unhealthy


def test_unreadable_or_naive_success_stamp() -> None:
    db, _ = fresh_session()
    all_synced(db)
    repo.set_setting(db, "sync:fuel", "not-a-timestamp")
    assert repo.sync_status(db, now=NOW)["healthy"] is False
    repo.set_setting(db, "sync:fuel", "2026-10-04T11:00:00")   # no offset: taken as UTC
    assert repo.sync_status(db, now=NOW)["healthy"] is True


def test_error_makes_unhealthy_until_a_later_success_clears_it() -> None:
    db, _ = fresh_session()
    all_synced(db)
    repo.record_sync_attempt(db, "octopus", stamp(0.2))
    repo.record_sync_error(db, "octopus", "RuntimeError: boom")
    status = repo.sync_status(db, now=NOW)
    assert status["healthy"] is False                       # despite a success an hour ago
    assert status["octopus"]["last_error"] == "RuntimeError: boom"
    assert status["octopus"]["last_success_utc"] == stamp(1)
    assert status["octopus"]["last_attempt_utc"] == stamp(0.2)
    assert status["fuel"]["last_error"] is None

    repo.record_sync(db, "octopus", stamp(0.1))
    status = repo.sync_status(db, now=NOW)
    assert status["octopus"]["last_error"] is None
    assert status["healthy"] is True


def test_api_status_reports_error_and_health() -> None:
    db, engine = fresh_session()
    for source in ("teslafi", "octopus", "fuel"):
        repo.record_sync(db, source)   # now
    client = TestClient(create_app(Settings(auth_disabled=True), engine=engine))
    assert client.get("/api/status").json()["healthy"] is True

    repo.record_sync_error(db, "teslafi", "RuntimeError: boom")
    body = client.get("/api/status").json()
    assert body["healthy"] is False
    assert body["teslafi"]["last_error"] == "RuntimeError: boom"
    assert body["octopus"]["last_error"] is None
    assert "last_attempt_utc" in body["fuel"]


# --------------------------------------------------------------------------- #
# run_pipeline records each source's attempt and failure, and still raises.
# --------------------------------------------------------------------------- #

FUEL_CSV = (
    "Date,ULSP Pump price in pence/litre,ULSD Pump price in pence/litre,"
    "ULSP Duty,ULSD Duty,ULSP VAT,ULSD VAT\n"
    "09/06/2026,140.00,150.00,52.95,52.95,20,20\n"
)
RATE_PERIODS = [RatePeriod(datetime(2026, 1, 1, tzinfo=timezone.utc), None, 0.07, 0.30)]
TOKEN = "tfi-secret-token-123"


def http_error(code: int, url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError(
        f"Client error '{code}' for url '{url}'\nFor more information check: https://httpstatuses",
        request=request, response=httpx.Response(code, request=request),
    )


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Run the pipeline against a file database with every network call faked.

    ``run.fail`` maps a source to the exception its fetch should raise;
    ``run.status()`` reads the result back the way the API would.
    """
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'cw.sqlite'}",
        octopus_api_key="oct-secret-key", octopus_account_number="A-00000000",
        teslafi_token=TOKEN,
    )
    fail: dict[str, Exception] = {}

    async def fake_fetch_fuel_csv(url):
        if "fuel" in fail:
            raise fail["fuel"]
        return FUEL_CSV

    async def fake_octopus_inputs(rest, gql, account):
        if "octopus" in fail:
            raise fail["octopus"]
        return RATE_PERIODS, []

    class FakeTeslaFi:
        def __init__(self, token):
            pass

        async def backfill(self, start, end):
            if "teslafi" in fail:
                raise fail["teslafi"]
            return []

    monkeypatch.setattr(pipeline, "fetch_fuel_csv", fake_fetch_fuel_csv)
    monkeypatch.setattr(pipeline, "fetch_octopus_inputs", fake_octopus_inputs)
    monkeypatch.setattr(pipeline, "TeslaFiHistoryClient", FakeTeslaFi)

    def run_once(run_settings: Settings = settings) -> dict:
        return asyncio.run(pipeline.run_pipeline(
            run_settings, teslafi=True, fuel_url="https://example/fuel.csv",
        ))

    def status() -> dict:
        engine = make_engine(settings.database_url)
        db = make_session_factory(engine)()
        try:
            return repo.sync_status(db)
        finally:
            db.close()
            engine.dispose()

    run_once.fail = fail
    run_once.status = status
    run_once.settings = settings
    return run_once


def test_successful_run_is_healthy_with_attempts_recorded(run) -> None:
    run()
    status = run.status()
    assert status["healthy"] is True
    for source in ("teslafi", "octopus", "fuel"):
        assert status[source]["last_error"] is None
        assert status[source]["last_attempt_utc"] is not None
        assert status[source]["last_success_utc"] >= status[source]["last_attempt_utc"]


def test_octopus_401_is_recorded_plainly_and_the_run_still_fails(run) -> None:
    run()
    run.fail["octopus"] = http_error(401, "https://api.octopus.energy/v1/accounts/A-00000000/")
    with pytest.raises(httpx.HTTPStatusError):
        run()

    status = run.status()
    assert status["healthy"] is False
    assert status["octopus"]["last_error"] == (
        "Octopus rejected the API key (HTTP 401) - check OCTOPUS_API_KEY"
    )
    # Fuel ran first and succeeded; TeslaFi was never reached, so it has no error.
    assert status["fuel"]["last_error"] is None
    assert status["teslafi"]["last_error"] is None
    assert status["octopus"]["last_attempt_utc"] >= status["octopus"]["last_success_utc"]


def test_later_success_clears_the_error(run) -> None:
    run.fail["octopus"] = http_error(401, "https://api.octopus.energy/v1/accounts/A-00000000/")
    with pytest.raises(httpx.HTTPStatusError):
        run()
    assert run.status()["octopus"]["last_error"] is not None
    assert run.status()["octopus"]["last_success_utc"] is None

    run.fail.clear()
    run()
    status = run.status()
    assert status["octopus"]["last_error"] is None
    assert status["octopus"]["last_success_utc"] is not None
    assert status["healthy"] is True


def test_teslafi_error_is_short_and_never_leaks_the_token(run) -> None:
    url = f"https://www.teslafi.com/history.php?token={TOKEN}&command=charges&" + "x" * 400
    run.fail["teslafi"] = http_error(500, url)
    with pytest.raises(httpx.HTTPStatusError):
        run()

    error = run.status()["teslafi"]["last_error"]
    assert error.startswith("HTTPStatusError: Client error '500' for url")
    assert TOKEN not in error and "token=***" in error
    assert "\n" not in error
    assert len(error) <= pipeline.MAX_ERROR_CHARS <= 255   # fits a Home Assistant state
    assert run.status()["octopus"]["last_error"] is None   # the earlier sources were fine


# Secrets are masked in two independent ways, and each must work on its own:
# every configured secret is blanked wherever it appears (in any letter case),
# and any credential-looking URL parameter is blanked whatever its value.

def test_configured_secrets_are_masked_wherever_they_appear() -> None:
    """Only the list of secrets can catch these: none is a ``name=value`` parameter."""
    exc = RuntimeError(
        "refused tfi-secret-token-123 for /accounts/A-00AB12CD/ using oct-secret-key"
    )
    secrets = ("oct-secret-key", "tfi-secret-token-123", "A-00AB12CD")
    assert pipeline.describe_error(exc, "teslafi", secrets) == (
        "RuntimeError: refused *** for /accounts/***/ using ***"
    )
    # Unmasked without the list - which is what makes passing it matter.
    assert "tfi-secret-token-123" in pipeline.describe_error(exc, "teslafi")


def test_account_number_is_masked_in_any_letter_case() -> None:
    exc = RuntimeError("404 for url 'https://api.octopus.energy/v1/accounts/a-00ab12cd/'")
    assert pipeline.describe_error(exc, "octopus", ("A-00AB12CD",)) == (
        "RuntimeError: 404 for url 'https://api.octopus.energy/v1/accounts/***/'"
    )
    # A secret is matched literally, never as a pattern.
    assert pipeline.describe_error(RuntimeError("a.b axb"), "fuel", ("a.b",)) == (
        "RuntimeError: *** axb"
    )


def test_credential_url_parameters_are_masked_even_when_not_a_known_secret() -> None:
    """Only the parameter pattern can catch these: the values are in no secrets list."""
    exc = RuntimeError(
        "500 for url 'https://x.test/history.php?token=UNLISTED-1&command=charges"
        "&api_key=UNLISTED-2&apikey=UNLISTED-3&Password=UNLISTED-4'"
    )
    described = pipeline.describe_error(exc, "teslafi", ("something-else",))
    assert "UNLISTED" not in described
    assert described.endswith(
        "?token=***&command=charges&api_key=***&apikey=***&Password=***'"
    )


def test_run_masks_the_teslafi_token_outside_a_url_parameter(run) -> None:
    """Fails if the pipeline stops passing the TeslaFi token as a secret."""
    run.fail["teslafi"] = RuntimeError(f"TeslaFi refused {TOKEN} (rate limited)")
    with pytest.raises(RuntimeError):
        run()
    assert run.status()["teslafi"]["last_error"] == "RuntimeError: TeslaFi refused *** (rate limited)"


def test_run_masks_the_octopus_account_number_and_api_key(run) -> None:
    """Fails if the pipeline stops passing the account number or the API key as secrets."""
    run.fail["octopus"] = http_error(
        404, "https://api.octopus.energy/v1/accounts/a-00000000/"   # as a URL may case it
    )
    with pytest.raises(httpx.HTTPStatusError):
        run()
    error = run.status()["octopus"]["last_error"]
    assert "00000000" not in error and "/accounts/***/" in error

    run.fail["octopus"] = RuntimeError("basic auth with oct-secret-key failed")
    with pytest.raises(RuntimeError):
        run()
    assert run.status()["octopus"]["last_error"] == "RuntimeError: basic auth with *** failed"


def test_graphql_auth_failure_is_recorded_like_a_rejected_key(run) -> None:
    run.fail["octopus"] = OctopusGraphQLError(
        "obtainKrakenToken", "Invalid data. [KT-CT-1139 Authentication failed.]", auth_failed=True
    )
    with pytest.raises(OctopusGraphQLError):
        run()
    assert run.status()["octopus"]["last_error"] == (
        "Octopus rejected the API key (GraphQL: authentication failed) - check OCTOPUS_API_KEY"
    )

    # Any other GraphQL error is reported as itself.
    run.fail["octopus"] = OctopusGraphQLError("completedDispatches", "Internal server error")
    with pytest.raises(OctopusGraphQLError):
        run()
    assert run.status()["octopus"]["last_error"] == (
        "OctopusGraphQLError: Octopus GraphQL completedDispatches failed: Internal server error"
    )


def test_fuel_failure_is_recorded_and_stops_the_run(run) -> None:
    run.fail["fuel"] = ValueError("no CSV link on the GOV.UK page")
    with pytest.raises(ValueError):
        run()
    status = run.status()
    assert status["fuel"]["last_error"] == "ValueError: no CSV link on the GOV.UK page"
    assert status["fuel"]["last_attempt_utc"] is not None
    assert status["octopus"]["last_attempt_utc"] is None   # not attempted


def test_missing_credentials_are_recorded_as_that_sources_error(run) -> None:
    with pytest.raises(RuntimeError, match="TESLAFI_TOKEN"):
        run(run.settings.model_copy(update={"teslafi_token": ""}))
    assert run.status()["teslafi"]["last_error"].startswith(
        "RuntimeError: TeslaFi ingestion needs TESLAFI_TOKEN"
    )
    with pytest.raises(RuntimeError, match="OCTOPUS_API_KEY"):
        run(run.settings.model_copy(update={"octopus_api_key": ""}))
    assert "OCTOPUS_API_KEY" in run.status()["octopus"]["last_error"]


def test_failing_to_record_the_error_does_not_hide_the_real_failure(run, monkeypatch) -> None:
    def broken(db, source, message):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(repo, "record_sync_error", broken)
    run.fail["fuel"] = ValueError("no CSV link on the GOV.UK page")
    with pytest.raises(ValueError, match="no CSV link"):
        run()


def test_describe_error_finds_a_401_behind_a_wrapping_exception() -> None:
    inner = http_error(401, "https://api.octopus.energy/v1/accounts/A-00000000/")
    try:
        try:
            raise inner
        except httpx.HTTPStatusError as exc:
            raise RuntimeError("could not read the Octopus account") from exc
    except RuntimeError as wrapped:
        assert "rejected the API key" in pipeline.describe_error(wrapped, "octopus")
        # Only Octopus gets the API-key wording.
        assert pipeline.describe_error(wrapped, "teslafi") == (
            "RuntimeError: could not read the Octopus account"
        )
    assert pipeline.describe_error(KeyError("data"), "octopus", ["", "unused"]) == (
        "KeyError: 'data'"
    )
