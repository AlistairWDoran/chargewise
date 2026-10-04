"""Ingestion pipeline: wire the adapters into the engine and the store.

Flow
----
1. **Fuel** — resolve the latest GOV.UK weekly road fuel-prices CSV, parse it and
   upsert the weekly records (idempotent). Runnable with no credentials.
2. **Octopus** — read the account's tariff agreements, fetch the unit-rate history
   per agreement (so tariff changes produce distinct rate eras), derive off-peak/
   peak ``RatePeriod``s, and fetch Intelligent Octopus Go smart-charge dispatches.
3. **Charge sessions** — load sessions from a source (the TeslaFi history API,
   or the generic CSV), cost each one through the dispatch-aware engine and
   upsert the costed session into the store.

The orchestration is split so the data-shaping steps are pure/DB-only and unit
testable; only ``run_pipeline`` (and the per-step ``async`` fetchers) touch the
network. Secrets come from config (a local ``.env`` in dev, Key Vault in Azure) —
never from the command line or source.

Dispatches are stored as they are seen (Octopus only serves recent ones) and
sessions are always costed against every dispatch stored so far. Each source's
attempt and outcome is recorded too (see ``repositories.sync_status``), so a
failing run is visible on ``/api/status`` as well as in the process exit code.

CLI
---
    python -m chargewise.ingest.pipeline --fuel-only
    python -m chargewise.ingest.pipeline --charges-csv data/charges.csv --vehicle "Tesla 2"
    python -m chargewise.ingest.pipeline --charges-csv data/charges.csv --no-octopus
    python -m chargewise.ingest.pipeline --teslafi                       # full backfill
    python -m chargewise.ingest.pipeline --teslafi --from 2026-06-01     # recent window

Vehicle names for ``--teslafi`` come from ``--vehicle-map VIN=Name`` flags when
any are given, otherwise from the ``VEHICLE_MAP`` setting (environment / .env,
``"VIN1=Name 1;VIN2=Name 2"`` — see ``config.parse_vehicle_map``).

A vehicle is identified by its VIN; the name is a label. A vehicle already in
the store is found by VIN and keeps its stored name unless the map names that
VIN, so a run without a map adds nothing new for a car stored with its VIN.

A malformed ``VEHICLE_MAP`` stops every mode of the CLI (``--fuel-only`` and
``--charges-csv`` included) before anything is fetched, with exit status 1.
When the run was a ``--teslafi`` one, the refusal is also recorded as a failed
attempt of the TeslaFi source, so ``/api/status`` shows it straight away.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from ..config import Settings, get_settings, parse_vehicle_map
from ..engine.models import ChargeSession, Dispatch, RatePeriod
from ..engine import cost_session
from ..store import repositories as repo
from ..store.db import init_db, make_engine, make_session_factory
from ..store.repositories import upsert_fuel_weeks
from .charge_sessions import parse_charge_sessions_csv
from .fuel_prices import (
    fetch_fuel_csv,
    fetch_latest_csv_url,
    parse_fuel_csv,
)
from .octopus_graphql import OctopusGraphQLClient, OctopusGraphQLError
from .octopus_rest import (
    OctopusRestClient,
    RateRecord,
    TariffAgreement,
    derive_agreement_rate_periods,
    product_code_from_tariff,
    sort_rate_periods,
)
from .teslafi_history import TeslaFiCharge, TeslaFiHistoryClient

#: Default backfill start: the earliest data in the reference TeslaFi account.
TESLAFI_EPOCH = date(2022, 2, 1)


# --------------------------------------------------------------------------- #
# Pure / DB-only steps (no network) — unit testable.
# --------------------------------------------------------------------------- #

def assign_miles(
    sessions: list[ChargeSession], seed_odometer: float | None = None
) -> list[float | None]:
    """Per-session miles from consecutive odometer readings (sorted by start).

    Miles for a session = odometer at this session − odometer at the previous
    session that had a reading (i.e. the distance driven since the last charge).
    The first session with an odometer has no prior reference, so its miles are
    ``None``; combined lifetime mileage is therefore last odometer − first
    odometer, matching METHODOLOGY.md §4. A negative delta (odometer reset or
    out-of-order data) is treated as ``None`` rather than a spurious figure.

    ``seed_odometer`` is the reading from the charge before the first of
    ``sessions`` when these are only part of a vehicle's history (a re-ingest
    window); it gives that first session its prior reference.
    """
    miles: list[float | None] = []
    last_odo: float | None = seed_odometer
    for s in sorted(sessions, key=lambda x: x.start):
        if s.odometer is None or last_odo is None:
            miles.append(None)
        else:
            delta = s.odometer - last_odo
            miles.append(delta if delta >= 0 else None)
        if s.odometer is not None:
            last_odo = s.odometer
    return miles


def cost_and_store_sessions(
    db: Session,
    vehicle_id: int,
    sessions: list[ChargeSession],
    rate_periods: list[RatePeriod],
    dispatches: list[Dispatch],
    away_rate: float,
    source: str = "csv_import",
) -> dict[str, int]:
    """Cost each session through the engine and upsert it (idempotent).

    Returns counts of sessions processed and newly inserted. Re-running with the
    same input inserts nothing further (the store upsert is keyed on vehicle +
    start).

    ``sessions`` may be only a recent window of the vehicle's history: the
    first session's miles are measured from the latest odometer already stored
    before it, not left blank.
    """
    ordered = sorted(sessions, key=lambda s: s.start)
    seed = repo.latest_odometer_before(db, vehicle_id, ordered[0].start) if ordered else None
    miles = assign_miles(ordered, seed)
    before = len(repo.list_charge_sessions(db))

    for s, session_miles in zip(ordered, miles):
        result = cost_session(s, rate_periods, dispatches, away_rate)
        repo.upsert_charge_session(
            db,
            vehicle_id=vehicle_id,
            start_utc=s.start.isoformat(),
            end_utc=s.end.isoformat(),
            location_type=s.location_type.value,
            energy_kwh=s.energy_kwh,
            cost_gbp=round(result.total_cost, 4),
            cost_is_estimate=result.is_estimate,
            odometer=s.odometer,
            miles=session_miles,
            source=source,
        )

    after = len(repo.list_charge_sessions(db))
    return {"processed": len(ordered), "inserted": after - before}


def mapped_name(vin: str, mapping: dict[str, str] | None) -> str | None:
    """The name an explicit mapping gives this VIN, or ``None``.

    VINs are compared without regard to case or surrounding whitespace, so a
    map typed in lower case still matches.
    """
    wanted = vin.strip().upper()
    if not mapping or not wanted:
        return None
    for key, name in mapping.items():
        if key.strip().upper() == wanted:
            return name
    return None


def vehicle_name_for(vin: str, model: str, mapping: dict[str, str] | None = None) -> str:
    """Human-friendly vehicle name for a VIN, honouring an explicit mapping."""
    explicit = mapped_name(vin, mapping)
    if explicit is not None:
        return explicit
    label = model.strip().title() if model.strip() else "Tesla"
    return f"{label} ({vin[-6:]})" if vin else label


def group_by_vin(charges: list[TeslaFiCharge]) -> dict[tuple[str, str], list[ChargeSession]]:
    """Group parsed TeslaFi charges into per-vehicle session lists."""
    grouped: dict[tuple[str, str], list[ChargeSession]] = {}
    for charge in charges:
        grouped.setdefault((charge.vin, charge.model), []).append(charge.session)
    return grouped


# --------------------------------------------------------------------------- #
# Network fetchers.
# --------------------------------------------------------------------------- #

async def fetch_rate_periods(
    rest_client: OctopusRestClient,
    agreements: list[TariffAgreement],
) -> list[RatePeriod]:
    """Fetch and derive the rate periods for a tariff-agreement history.

    Needs no account lookup (the rate endpoints are public). Each agreement's
    periods are clipped to that agreement's dates, so a tariff whose published
    rate record outlives the agreement cannot shadow a later tariff; the
    combined result is sorted and checked to be non-overlapping.

    Raises ``OctopusRatesUnavailable`` if a non-zero-length agreement has no
    usable rates and ``OctopusRatesOverlap`` if agreements overlap.
    """
    periods: list[RatePeriod] = []
    for agreement in agreements:
        # Tariff switches can leave zero-length agreements (valid_from ==
        # valid_to); Octopus returns 400 for an empty rates window, so skip.
        if agreement.valid_to is not None and agreement.valid_to <= agreement.valid_from:
            continue
        product = product_code_from_tariff(agreement.tariff_code)
        period_from = agreement.valid_from.isoformat()
        # An open agreement has no upper bound, so already-published future
        # rates (e.g. a dated price change) are picked up too.
        period_to = agreement.valid_to.isoformat() if agreement.valid_to else None
        window = (product, agreement.tariff_code, period_from, period_to)
        standard = await rest_client.get_unit_rates(*window)
        day: list[RateRecord] = []
        night: list[RateRecord] = []
        if not standard:
            # Two-register tariffs publish nothing under standard-unit-rates.
            day = await rest_client.get_day_unit_rates(*window)
            night = await rest_client.get_night_unit_rates(*window)
        periods.extend(derive_agreement_rate_periods(agreement, standard, day, night))
    return sort_rate_periods(periods)


async def fetch_octopus_inputs(
    rest_client: OctopusRestClient,
    gql_client: OctopusGraphQLClient,
    account_number: str,
) -> tuple[list[RatePeriod], list[Dispatch]]:
    """Fetch derived rate periods (one per pricing era, per agreement) and dispatches."""
    account = await rest_client.get_account(account_number)
    periods = await fetch_rate_periods(rest_client, account["agreements"])
    dispatches = await gql_client.get_completed_dispatches(account_number)
    return periods, dispatches


async def ingest_fuel(db: Session, url: str | None = None) -> int:
    """Resolve (if needed), fetch, parse and upsert GOV.UK weekly fuel prices.

    Returns the number of new weeks inserted.
    """
    if url is None:
        url = await fetch_latest_csv_url()
    text = await fetch_fuel_csv(url)
    weeks = parse_fuel_csv(text)
    return upsert_fuel_weeks(db, weeks)


# --------------------------------------------------------------------------- #
# Orchestration.
# --------------------------------------------------------------------------- #

#: Longest error text recorded per source. Kept under Home Assistant's
#: 255-character state limit so a sensor can show it whole.
MAX_ERROR_CHARS = 250

# Credentials passed as URL query parameters (TeslaFi's token is) appear in
# HTTP error messages; they must never reach the status API.
_SECRET_PARAM = re.compile(r"(?i)\b(token|api_?key|key|password|secret)=[^&\s'\"]+")


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """An exception, then whatever it was raised from, and so on."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _http_status(exc: BaseException) -> int | None:
    """HTTP status behind an exception (or whatever it was raised from), if any."""
    for current in _chain(exc):
        code = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(code, int):
            return code
    return None


def describe_error(exc: BaseException, source: str, secrets: Iterable[str] = ()) -> str:
    """A short, secret-free, one-line description of a failed ingest.

    This is what ``/api/status`` shows as a source's ``last_error``: exception
    type and message, capped at ``MAX_ERROR_CHARS``. ``secrets`` (in any
    letter case) and any credential-looking URL parameter are masked. Octopus
    refusing the API key gets a plain message, however it was reported: HTTP
    401 from the REST API, or an authentication error from GraphQL (which
    arrives as HTTP 200).
    """
    if source == "octopus":
        if _http_status(exc) == 401:
            return "Octopus rejected the API key (HTTP 401) - check OCTOPUS_API_KEY"
        if any(isinstance(e, OctopusGraphQLError) and e.auth_failed for e in _chain(exc)):
            return ("Octopus rejected the API key (GraphQL: authentication failed)"
                    " - check OCTOPUS_API_KEY")
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    for secret in secrets:
        if secret:
            text = re.sub(re.escape(secret), "***", text, flags=re.IGNORECASE)
    text = _SECRET_PARAM.sub(r"\1=***", text)
    if len(text) > MAX_ERROR_CHARS:
        text = text[: MAX_ERROR_CHARS - 3] + "..."
    return text


@contextmanager
def _tracked(db: Session, source: str, settings: Settings) -> Iterator[None]:
    """Record one source's ingest attempt and how it ended.

    Success stamps the source's last-sync time and clears its error; a failure
    stores a short error string and re-raises, so the run still exits non-zero.
    """
    repo.record_sync_attempt(db, source)
    try:
        yield
    except Exception as exc:
        try:
            db.rollback()
            secrets = (settings.octopus_api_key, settings.teslafi_token,
                       settings.octopus_account_number)
            repo.record_sync_error(db, source, describe_error(exc, source, secrets))
        except Exception:  # bookkeeping must never mask the real failure
            pass
        raise
    repo.record_sync(db, source)


async def run_pipeline(
    settings: Settings | None = None,
    *,
    charges_csv: str | None = None,
    vehicle_name: str = "Tesla",
    teslafi: bool = False,
    teslafi_from: date | None = None,
    teslafi_to: date | None = None,
    vehicle_map: dict[str, str] | None = None,
    fuel_only: bool = False,
    use_octopus: bool = True,
    fuel_url: str | None = None,
) -> dict[str, object]:
    """Run the ingestion pipeline and return a summary of what was ingested."""
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    db = make_session_factory(engine)()

    summary: dict[str, object] = {}
    try:
        with _tracked(db, "fuel", settings):
            summary["fuel_weeks_inserted"] = await ingest_fuel(db, fuel_url)

        if fuel_only:
            return summary

        rate_periods: list[RatePeriod] = []
        if use_octopus:
            with _tracked(db, "octopus", settings):
                if not settings.octopus_api_key or not settings.octopus_account_number:
                    raise RuntimeError(
                        "Octopus ingestion needs OCTOPUS_API_KEY and "
                        "OCTOPUS_ACCOUNT_NUMBER in the environment / .env "
                        "(use --no-octopus to skip, e.g. for away-only data)."
                    )
                rest = OctopusRestClient(settings.octopus_api_key)
                gql = OctopusGraphQLClient(settings.octopus_api_key)
                rate_periods, fresh = await fetch_octopus_inputs(
                    rest, gql, settings.octopus_account_number
                )
                summary["rate_periods"] = len(rate_periods)
                summary["dispatches"] = len(fresh)
                summary["dispatches_new"] = repo.upsert_dispatches(db, fresh)
        # Cost against every dispatch ever stored, not just those still in the
        # feed: Octopus drops old ones, and a session priced off-peak under a
        # dispatch must not flip to the peak rate when re-costed later.
        dispatches: list[Dispatch] = repo.list_dispatches(db)
        summary["dispatches_stored"] = len(dispatches)

        if charges_csv:
            with open(charges_csv, encoding="utf-8") as fh:
                sessions = parse_charge_sessions_csv(fh.read())
            vehicle = repo.get_or_create_vehicle(db, vehicle_name)
            summary["charges"] = cost_and_store_sessions(
                db,
                vehicle.id,
                sessions,
                rate_periods,
                dispatches,
                settings.away_rate_gbp_per_kwh,
            )

        if teslafi:
            with _tracked(db, "teslafi", settings):
                if not settings.teslafi_token:
                    raise RuntimeError(
                        "TeslaFi ingestion needs TESLAFI_TOKEN in the environment / .env."
                    )
                client = TeslaFiHistoryClient(settings.teslafi_token)
                charges = await client.backfill(
                    teslafi_from or TESLAFI_EPOCH,
                    teslafi_to or datetime.now(timezone.utc).date(),
                )
                for (vin, model), sessions in group_by_vin(charges).items():
                    name = vehicle_name_for(vin, model, vehicle_map)
                    # The VIN identifies the vehicle. One already stored keeps
                    # its name unless the map names this VIN explicitly.
                    vehicle = repo.get_or_create_vehicle(
                        db, name, vin=vin,
                        rename=mapped_name(vin, vehicle_map) is not None,
                    )
                    summary[f"teslafi:{vehicle.name}"] = cost_and_store_sessions(
                        db,
                        vehicle.id,
                        sessions,
                        rate_periods,
                        dispatches,
                        settings.away_rate_gbp_per_kwh,
                        source="teslafi_history",
                    )
        return summary
    finally:
        db.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ChargeWise ingestion pipeline")
    p.add_argument("--fuel-only", action="store_true",
                   help="Only ingest GOV.UK fuel prices, then stop.")
    p.add_argument("--charges-csv", default=None,
                   help="Path to a charge-session CSV to cost and store.")
    p.add_argument("--vehicle", default="Tesla",
                   help="Vehicle name to attribute the sessions to.")
    p.add_argument("--no-octopus", action="store_true",
                   help="Skip Octopus rate/dispatch fetch (away-only costing).")
    p.add_argument("--fuel-url", default=None,
                   help="Override the fuel-prices CSV URL (else auto-resolved).")
    p.add_argument("--teslafi", action="store_true",
                   help="Ingest charge history from the TeslaFi API.")
    p.add_argument("--from", dest="teslafi_from", default=None, metavar="YYYY-MM-DD",
                   help="TeslaFi backfill start (default: Feb 2022).")
    p.add_argument("--to", dest="teslafi_to", default=None, metavar="YYYY-MM-DD",
                   help="TeslaFi backfill end (default: today).")
    p.add_argument("--vehicle-map", action="append", default=[], metavar="VIN=Name",
                   help="Name a vehicle by VIN, e.g. --vehicle-map \"<VIN>=My car\". "
                        "Repeatable. When given, replaces the VEHICLE_MAP setting.")
    return p.parse_args(argv)


def record_refused_run(settings: Settings, source: str, reason: str) -> bool:
    """Record a run that was refused before it started as a failed attempt.

    Stores the attempt time and ``reason`` as ``source``'s last error, which is
    what ``/api/status`` reports (and what makes ``healthy`` false) until the
    source next succeeds. Touches only the database: no network. Returns
    whether it could be recorded; a database that cannot be opened is not an
    error here, because the caller is already reporting a failure.
    """
    try:
        engine = make_engine(settings.database_url)
        init_db(engine)
        db = make_session_factory(engine)()
        try:
            text = " ".join(reason.split())
            if len(text) > MAX_ERROR_CHARS:
                text = text[: MAX_ERROR_CHARS - 3] + "..."
            repo.record_sync_attempt(db, source)
            repo.record_sync_error(db, source, text)
        finally:
            db.close()
            engine.dispose()
    except Exception:  # the refusal itself is still reported by the caller
        return False
    return True


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    vehicle_map = dict(m.split("=", 1) for m in args.vehicle_map)
    if not vehicle_map:
        # No --vehicle-map flag: use the VEHICLE_MAP setting (environment / .env).
        settings = get_settings()
        try:
            vehicle_map = parse_vehicle_map(settings.vehicle_map)
        except ValueError as exc:
            message = f"ChargeWise ingestion not started: {exc}"
            if args.teslafi:
                # Vehicle names belong to the TeslaFi stage: make the refusal
                # visible where that stage's failures are shown.
                record_refused_run(settings, "teslafi", message)
            raise SystemExit(message) from None
    summary = asyncio.run(
        run_pipeline(
            charges_csv=args.charges_csv,
            vehicle_name=args.vehicle,
            teslafi=args.teslafi,
            teslafi_from=date.fromisoformat(args.teslafi_from) if args.teslafi_from else None,
            teslafi_to=date.fromisoformat(args.teslafi_to) if args.teslafi_to else None,
            vehicle_map=vehicle_map or None,
            fuel_only=args.fuel_only,
            use_octopus=not args.no_octopus,
            fuel_url=args.fuel_url,
        )
    )
    print("ChargeWise ingestion complete:")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
