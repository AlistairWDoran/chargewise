"""Ingestion pipeline: wire the adapters into the engine and the store.

Flow
----
1. **Fuel** — resolve the latest GOV.UK weekly road fuel-prices CSV, parse it and
   upsert the weekly records (idempotent). Runnable with no credentials.
2. **Octopus** — read the account's tariff agreements, fetch the unit-rate history
   per agreement (so tariff changes produce distinct rate eras), derive off-peak/
   peak ``RatePeriod``s, and fetch Intelligent Octopus Go smart-charge dispatches.
3. **Charge sessions** — load sessions from a source (the generic CSV today; the
   TeslaFi adapter once it lands), cost each one through the dispatch-aware engine
   and upsert the costed session into the store.

The orchestration is split so the data-shaping steps are pure/DB-only and unit
testable; only ``run_pipeline`` (and the per-step ``async`` fetchers) touch the
network. Secrets come from config (a local ``.env`` in dev, Key Vault in Azure) —
never from the command line or source.

CLI
---
    python -m chargewise.ingest.pipeline --fuel-only
    python -m chargewise.ingest.pipeline --charges-csv data/charges.csv --vehicle "Friday"
    python -m chargewise.ingest.pipeline --charges-csv data/charges.csv --no-octopus
    python -m chargewise.ingest.pipeline --teslafi                       # full backfill
    python -m chargewise.ingest.pipeline --teslafi --from 2026-06-01     # recent window
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from ..config import Settings, get_settings
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
from .octopus_graphql import OctopusGraphQLClient
from .octopus_rest import OctopusRestClient, derive_iog_rate_periods, product_code_from_tariff
from .teslafi_history import TeslaFiCharge, TeslaFiHistoryClient

#: Earliest data in the TeslaFi account (Tesla #1 acquired Feb 2022).
TESLAFI_EPOCH = date(2022, 2, 1)


# --------------------------------------------------------------------------- #
# Pure / DB-only steps (no network) — unit testable.
# --------------------------------------------------------------------------- #

def assign_miles(sessions: list[ChargeSession]) -> list[float | None]:
    """Per-session miles from consecutive odometer readings (sorted by start).

    Miles for a session = odometer at this session − odometer at the previous
    session that had a reading (i.e. the distance driven since the last charge).
    The first session with an odometer has no prior reference, so its miles are
    ``None``; combined lifetime mileage is therefore last odometer − first
    odometer, matching METHODOLOGY.md §4. A negative delta (odometer reset or
    out-of-order data) is treated as ``None`` rather than a spurious figure.
    """
    miles: list[float | None] = []
    last_odo: float | None = None
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
    start + energy).
    """
    ordered = sorted(sessions, key=lambda s: s.start)
    miles = assign_miles(ordered)
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


def vehicle_name_for(vin: str, model: str, mapping: dict[str, str] | None = None) -> str:
    """Human-friendly vehicle name for a VIN, honouring an explicit mapping."""
    if mapping and vin in mapping:
        return mapping[vin]
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

async def fetch_octopus_inputs(
    rest_client: OctopusRestClient,
    gql_client: OctopusGraphQLClient,
    account_number: str,
) -> tuple[list[RatePeriod], list[Dispatch]]:
    """Fetch derived rate periods (one era per tariff agreement) and dispatches."""
    account = await rest_client.get_account(account_number)
    periods: list[RatePeriod] = []
    for agreement in account["agreements"]:
        # Tariff switches can leave zero-length agreements (valid_from ==
        # valid_to); Octopus returns 400 for an empty rates window, so skip.
        if agreement.valid_to is not None and agreement.valid_to <= agreement.valid_from:
            continue
        product = product_code_from_tariff(agreement.tariff_code)
        period_from = agreement.valid_from.isoformat()
        period_to = (agreement.valid_to or datetime.now(timezone.utc)).isoformat()
        records = await rest_client.get_unit_rates(
            product, agreement.tariff_code, period_from, period_to
        )
        periods.extend(derive_iog_rate_periods(records))
    periods.sort(key=lambda p: p.valid_from)

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
        summary["fuel_weeks_inserted"] = await ingest_fuel(db, fuel_url)
        repo.record_sync(db, "fuel")

        if fuel_only:
            return summary

        rate_periods: list[RatePeriod] = []
        dispatches: list[Dispatch] = []
        if use_octopus:
            if not settings.octopus_api_key or not settings.octopus_account_number:
                raise RuntimeError(
                    "Octopus ingestion needs OCTOPUS_API_KEY and "
                    "OCTOPUS_ACCOUNT_NUMBER in the environment / .env "
                    "(use --no-octopus to skip, e.g. for away-only data)."
                )
            rest = OctopusRestClient(settings.octopus_api_key)
            gql = OctopusGraphQLClient(settings.octopus_api_key)
            rate_periods, dispatches = await fetch_octopus_inputs(
                rest, gql, settings.octopus_account_number
            )
            repo.record_sync(db, "octopus")
            summary["rate_periods"] = len(rate_periods)
            summary["dispatches"] = len(dispatches)

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
                vehicle = repo.get_or_create_vehicle(db, name, vin=vin)
                summary[f"teslafi:{name}"] = cost_and_store_sessions(
                    db,
                    vehicle.id,
                    sessions,
                    rate_periods,
                    dispatches,
                    settings.away_rate_gbp_per_kwh,
                    source="teslafi_history",
                )
            repo.record_sync(db, "teslafi")
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
                   help="Name a vehicle by VIN, e.g. --vehicle-map XP7...=Friday. Repeatable.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    vehicle_map = dict(m.split("=", 1) for m in args.vehicle_map)
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
