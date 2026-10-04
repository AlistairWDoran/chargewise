"""A run refused for a malformed VEHICLE_MAP is visible on ``/api/status``.

The CLI stops such a run before it fetches anything. When the run was a
TeslaFi one (the daily scheduler's is), the refusal is recorded as a failed
attempt of the ``teslafi`` source, so the status API and the Home Assistant
alert show it at once instead of after the 36-hour staleness rule.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chargewise.api.app import create_app
from chargewise.config import Settings
from chargewise.engine.models import RatePeriod
from chargewise.ingest import pipeline
from chargewise.store import repositories as repo
from chargewise.store.db import init_db, make_engine, make_session_factory

VIN1 = "TESTVIN0000000001"
VIN2 = "TESTVIN0000000002"
MALFORMED = f"{VIN1}=Tesla 1,{VIN2}=Tesla 2"          # comma where ";" belongs
GOOD = f"{VIN1}=Tesla 1;{VIN2}=Tesla 2"
REFUSAL = (
    'ChargeWise ingestion not started: VEHICLE_MAP entry 1 has more than one "=": '
    'entries are separated by ";", and a name cannot contain "=". '
    'Expected VEHICLE_MAP="VIN1=Name 1;VIN2=Name 2".'
)
FUEL_CSV = (
    "Date,ULSP Pump price in pence/litre,ULSD Pump price in pence/litre,"
    "ULSP Duty,ULSD Duty,ULSP VAT,ULSD VAT\n"
    "09/06/2026,140.00,150.00,52.95,52.95,20,20\n"
)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Run ``pipeline.main`` against a file database, every network call a tripwire."""
    url = f"sqlite:///{tmp_path / 'cw.sqlite'}"
    calls: list[str] = []

    async def fetch_fuel_csv(url):
        calls.append("fuel")
        return FUEL_CSV

    async def fetch_octopus_inputs(rest, gql, account):
        calls.append("octopus")
        return [RatePeriod(datetime(2026, 1, 1, tzinfo=timezone.utc), None, 0.07, 0.30)], []

    class FakeTeslaFi:
        def __init__(self, token):
            pass

        async def backfill(self, start, end):
            calls.append("teslafi")
            return []

    async def fetch_latest_csv_url():
        calls.append("fuel-url")
        return "https://example/fuel.csv"

    monkeypatch.setattr(pipeline, "fetch_fuel_csv", fetch_fuel_csv)
    monkeypatch.setattr(pipeline, "fetch_latest_csv_url", fetch_latest_csv_url)
    monkeypatch.setattr(pipeline, "fetch_octopus_inputs", fetch_octopus_inputs)
    monkeypatch.setattr(pipeline, "TeslaFiHistoryClient", FakeTeslaFi)

    def settings_for(vehicle_map: str, database_url: str = url) -> Settings:
        return Settings(
            _env_file=None, database_url=database_url, vehicle_map=vehicle_map,
            octopus_api_key="dummy", octopus_account_number="A-00000000",
            teslafi_token="dummy",
        )

    def run(argv: list[str], vehicle_map: str, database_url: str = url) -> None:
        settings = settings_for(vehicle_map, database_url)
        monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
        real_run = pipeline.run_pipeline
        monkeypatch.setattr(
            pipeline, "run_pipeline", lambda **kwargs: real_run(settings, **kwargs)
        )
        try:
            pipeline.main(argv)
        finally:
            monkeypatch.setattr(pipeline, "run_pipeline", real_run)

    def status(now: datetime | None = None) -> dict:
        engine = make_engine(url)
        init_db(engine)
        db = make_session_factory(engine)()
        try:
            return repo.sync_status(db, now=now)
        finally:
            db.close()
            engine.dispose()

    def api_status() -> dict:
        engine = make_engine(url)
        try:
            app = create_app(settings_for(GOOD), engine=engine)
            return TestClient(app).get("/api/status").json()
        finally:
            engine.dispose()

    run.calls = calls
    run.status = status
    run.api_status = api_status
    run.url = url
    return run


def test_refused_teslafi_run_is_recorded_and_exits_1(cli, capsys) -> None:
    before = datetime.now(timezone.utc) - timedelta(seconds=2)

    with pytest.raises(SystemExit) as excinfo:
        cli(["--teslafi", "--from", "2026-06-01"], MALFORMED)

    assert excinfo.value.code == REFUSAL          # a string code: exit status 1, text on stderr
    assert cli.calls == []                        # nothing was fetched
    assert "ingestion complete" not in capsys.readouterr().out

    teslafi = cli.status()["teslafi"]
    assert teslafi["last_error"] == REFUSAL
    assert datetime.fromisoformat(teslafi["last_attempt_utc"]) >= before
    assert teslafi["last_success_utc"] is None
    # Only the TeslaFi source is marked: the other stages were not attempted.
    for source in ("octopus", "fuel"):
        assert cli.status()[source]["last_error"] is None
        assert cli.status()[source]["last_attempt_utc"] is None


def test_status_api_is_unhealthy_with_the_reason_straight_after_a_refused_run(cli) -> None:
    # A healthy installation: yesterday's run succeeded for every source.
    cli(["--teslafi"], GOOD)
    healthy = cli.api_status()
    assert healthy["healthy"] is True
    assert healthy["teslafi"]["last_error"] is None
    cli.calls.clear()

    # Someone edits VEHICLE_MAP badly; the next run is refused.
    with pytest.raises(SystemExit):
        cli(["--teslafi"], MALFORMED)

    after = cli.api_status()
    assert after["healthy"] is False              # at once, not after 36 hours
    assert after["teslafi"]["last_error"] == REFUSAL
    assert after["teslafi"]["last_attempt_utc"] >= healthy["teslafi"]["last_attempt_utc"]
    assert after["teslafi"]["last_success_utc"] == healthy["teslafi"]["last_success_utc"]
    assert after["octopus"] == healthy["octopus"] and after["fuel"] == healthy["fuel"]
    assert cli.calls == []


def test_recorded_reason_carries_no_vin_and_fits_the_field(cli) -> None:
    with pytest.raises(SystemExit):
        cli(["--teslafi"], MALFORMED)
    reason = cli.status()["teslafi"]["last_error"]
    assert VIN1 not in reason.upper() and VIN2 not in reason.upper() and "TESLA" not in reason.upper()
    assert len(reason) <= pipeline.MAX_ERROR_CHARS and not reason.endswith("...")


def test_next_good_run_clears_the_refusal(cli) -> None:
    with pytest.raises(SystemExit):
        cli(["--teslafi"], MALFORMED)
    assert cli.api_status()["healthy"] is False

    cli(["--teslafi"], GOOD)

    status = cli.api_status()
    assert status["teslafi"]["last_error"] is None
    assert status["healthy"] is True


@pytest.mark.parametrize("argv", [["--fuel-only"], ["--charges-csv", "missing.csv"], []])
def test_malformed_map_stops_every_mode_but_only_teslafi_runs_are_recorded(cli, argv) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli(argv, MALFORMED)

    assert excinfo.value.code == REFUSAL
    assert cli.calls == []
    status = cli.status()
    assert status["teslafi"]["last_error"] is None
    assert status["teslafi"]["last_attempt_utc"] is None


def test_flags_bypass_a_malformed_setting_and_record_nothing(cli) -> None:
    cli(["--teslafi", "--vehicle-map", f"{VIN1}=Tesla 1"], MALFORMED)
    assert cli.calls == ["fuel-url", "fuel", "octopus", "teslafi"]
    assert cli.status()["teslafi"]["last_error"] is None


def test_refusal_still_exits_1_when_it_cannot_be_recorded(cli, tmp_path) -> None:
    """No usable database: the run is refused with the same message all the same."""
    unusable = f"sqlite:///{tmp_path / 'no-such-directory' / 'cw.sqlite'}"

    with pytest.raises(SystemExit) as excinfo:
        cli(["--teslafi"], MALFORMED, database_url=unusable)

    assert excinfo.value.code == REFUSAL
    assert cli.calls == []
    assert not (tmp_path / "no-such-directory").exists()


def test_record_refused_run_reports_whether_it_was_stored(cli, tmp_path) -> None:
    good = Settings(_env_file=None, database_url=cli.url)
    bad = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'no-such-directory' / 'cw.sqlite'}"
    )
    assert pipeline.record_refused_run(good, "teslafi", "a   reason\nover two lines") is True
    assert cli.status()["teslafi"]["last_error"] == "a reason over two lines"
    assert pipeline.record_refused_run(bad, "teslafi", "a reason") is False

    long = "x" * (pipeline.MAX_ERROR_CHARS + 50)
    pipeline.record_refused_run(good, "teslafi", long)
    stored = cli.status()["teslafi"]["last_error"]
    assert len(stored) == pipeline.MAX_ERROR_CHARS and stored.endswith("...")


def test_refused_run_as_a_real_process(tmp_path) -> None:
    """Exit status 1 and one line on stderr, as scheduler.sh sees it."""
    import os
    import subprocess
    import sys

    db_path = tmp_path / "cw.sqlite"
    backend = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        # Run this checkout's package, installed or not.
        "PYTHONPATH": os.pathsep.join(
            [str(backend), *filter(None, [os.environ.get("PYTHONPATH")])]
        ),
        "VEHICLE_MAP": MALFORMED,
        "DATABASE_URL": f"sqlite:///{db_path}",
        "TESLAFI_TOKEN": "dummy",
    }
    done = subprocess.run(
        [sys.executable, "-m", "chargewise.ingest.pipeline", "--teslafi", "--from", "2026-06-01"],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 1
    assert done.stderr.strip() == REFUSAL
    assert done.stdout == ""

    engine = make_engine(f"sqlite:///{db_path}")
    db = make_session_factory(engine)()
    try:
        assert repo.sync_status(db)["teslafi"]["last_error"] == REFUSAL
        assert repo.sync_status(db)["healthy"] is False
    finally:
        db.close()
        engine.dispose()


def test_run_pipeline_is_untouched_by_the_setting(cli) -> None:
    """Only the CLI reads VEHICLE_MAP; ``run_pipeline`` takes the map it is given."""
    settings = Settings(
        _env_file=None, database_url=cli.url, vehicle_map=MALFORMED,
        octopus_api_key="dummy", octopus_account_number="A-00000000", teslafi_token="dummy",
    )
    summary = asyncio.run(pipeline.run_pipeline(
        settings, teslafi=True, fuel_url="https://example/fuel.csv",
    ))
    assert summary["fuel_weeks_inserted"] == 1
    assert cli.status()["teslafi"]["last_error"] is None
