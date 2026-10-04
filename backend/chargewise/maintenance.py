"""Maintenance commands — repair figures already stored in the database.

    python -m chargewise.maintenance repair-miles [--dry-run]
    python -m chargewise.maintenance recost [--dry-run]

``repair-miles`` recomputes every session's miles from the stored odometer
readings over each vehicle's full history (undoing miles blanked by partial
re-ingests). ``recost`` re-prices every stored *home* session from its stored
start, end and energy against current Octopus rates and every stored dispatch;
away sessions keep their recorded cost.

Both commands print a before/after lifetime summary, are idempotent (a second
run changes nothing) and write in a single transaction. ``--dry-run`` shows the
same summary and writes nothing. The database is the one in the normal settings
(``DATABASE_URL``). ``recost`` exits non-zero if it had to leave any home
session unpriced (it still re-prices the rest and names the ones it could not).

The data-shaping parts (``repair_miles``, ``recost_sessions``) take a database
session and plain inputs so they are testable offline; only ``run_recost``
touches the network.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from datetime import datetime
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .engine import cost_session
from .engine.models import ChargeSession, Dispatch, LocationType, RatePeriod
from .ingest import pipeline
from .ingest.octopus_graphql import OctopusGraphQLClient
from .ingest.octopus_rest import OctopusRestClient
from .store import repositories as repo
from .store.db import init_db, make_engine, make_session_factory
from .store.models import ChargeSession as StoredSession
from .store.models import StoredDispatch

#: How many unpriced sessions ``recost`` names when it reports them.
UNPRICED_SHOWN = 5

#: Lifetime-summary figures shown before and after each command.
SUMMARY_FIELDS = (
    ("session_count", "Sessions"),
    ("total_miles", "Total miles"),
    ("home_cost_gbp", "Home cost (GBP)"),
    ("away_cost_gbp", "Away cost (GBP)"),
    ("total_cost_gbp", "Total cost (GBP)"),
    ("petrol_equiv_gbp", "Petrol equivalent (GBP)"),
    ("saving_gbp", "Saving (GBP)"),
)


def _totals(db: Session, settings: Settings) -> dict[str, float]:
    full = repo.lifetime_summary(db, settings.petrol_mpg, settings.fuel_type)
    return {key: full[key] for key, _ in SUMMARY_FIELDS}


def _engine_session(row: StoredSession) -> ChargeSession:
    """A stored session as the engine model (what the pipeline costed it from)."""
    return ChargeSession(
        start=datetime.fromisoformat(row.start_utc),
        end=datetime.fromisoformat(row.end_utc),
        energy_kwh=row.energy_kwh,
        location_type=LocationType(row.location_type),
        odometer=row.odometer,
    )


def _finish(db: Session, dry_run: bool, settings: Settings) -> dict[str, float]:
    """Read the resulting totals, then keep (commit) or discard (dry run) the edits."""
    db.flush()
    after = _totals(db, settings)
    if dry_run:
        db.rollback()
    else:
        db.commit()
    return after


# --------------------------------------------------------------------------- #
# Pure / DB-only parts (no network) — unit testable.
# --------------------------------------------------------------------------- #

def repair_miles(
    db: Session, dry_run: bool = False, settings: Settings | None = None
) -> dict[str, Any]:
    """Recompute miles for every stored session from the odometer readings.

    Works per vehicle over its whole history, exactly as a full ingest would
    (``pipeline.assign_miles``), and writes back only the values that differ.
    """
    settings = settings or get_settings()
    before = _totals(db, settings)

    by_vehicle: dict[int, list[StoredSession]] = {}
    for row in repo.list_charge_sessions(db):
        by_vehicle.setdefault(row.vehicle_id, []).append(row)

    filled = corrected = cleared = 0
    for rows in by_vehicle.values():
        pairs = sorted(((_engine_session(r), r) for r in rows), key=lambda p: p[0].start)
        miles = pipeline.assign_miles([session for session, _ in pairs])
        for (_, row), value in zip(pairs, miles):
            if row.miles is None and value is None:
                continue
            if row.miles is None:
                filled += 1
            elif value is None:
                cleared += 1
            elif math.isclose(row.miles, value, abs_tol=1e-9):
                continue
            else:
                corrected += 1
            row.miles = value

    return {
        "command": "repair-miles",
        "dry_run": dry_run,
        "examined": sum(len(rows) for rows in by_vehicle.values()),
        "changed": filled + corrected + cleared,
        "filled": filled,
        "corrected": corrected,
        "cleared": cleared,
        "before": before,
        "after": _finish(db, dry_run, settings),
    }


def recost_sessions(
    db: Session,
    rate_periods: list[RatePeriod],
    dispatches: list[Dispatch],
    dry_run: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Re-price every stored home session; away sessions are left untouched.

    Each session is costed from its stored start, end and energy through the
    same engine call the pipeline uses. A session that no rate period covers is
    left as it is and counted as ``unpriced`` rather than aborting the run;
    ``unpriced_first`` lists the start times of the earliest few.
    """
    settings = settings or get_settings()
    before = _totals(db, settings)

    rows = repo.list_charge_sessions(db, LocationType.HOME.value)
    changed = raised = lowered = 0
    unpriced: list[str] = []
    for row in rows:
        try:
            result = cost_session(
                _engine_session(row), rate_periods, dispatches,
                settings.away_rate_gbp_per_kwh,
            )
        except ValueError:  # no rate period covers this session
            unpriced.append(row.start_utc)
            continue
        cost = round(result.total_cost, 4)
        if cost == row.cost_gbp and result.is_estimate == row.cost_is_estimate:
            continue
        changed += 1
        raised += cost > row.cost_gbp
        lowered += cost < row.cost_gbp
        row.cost_gbp = cost
        row.cost_is_estimate = result.is_estimate

    return {
        "command": "recost",
        "dry_run": dry_run,
        "examined": len(rows),
        "changed": changed,
        "raised": raised,
        "lowered": lowered,
        "unpriced": len(unpriced),
        "unpriced_first": unpriced[:UNPRICED_SHOWN],
        "before": before,
        "after": _finish(db, dry_run, settings),
    }


# --------------------------------------------------------------------------- #
# Wrappers: open the configured database (and, for recost, fetch from Octopus).
# --------------------------------------------------------------------------- #

def _open_db(settings: Settings, dry_run: bool) -> Session:
    engine = make_engine(settings.database_url)
    if not dry_run:  # a dry run must not even add the tables a new release brings
        init_db(engine)
    return make_session_factory(engine)()


def run_repair_miles(settings: Settings | None = None, dry_run: bool = False) -> dict[str, Any]:
    settings = settings or get_settings()
    db = _open_db(settings, dry_run)
    try:
        return repair_miles(db, dry_run, settings)
    finally:
        db.close()


async def run_recost(settings: Settings | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Fetch rates and dispatches as the pipeline does, then re-price home sessions.

    Dispatches from the feed are stored (they are only served for a while) and
    the sessions are priced against every dispatch stored so far. In a dry run
    the fresh dispatches are used for the preview but not stored.
    """
    settings = settings or get_settings()
    if not settings.octopus_api_key or not settings.octopus_account_number:
        raise RuntimeError(
            "recost needs OCTOPUS_API_KEY and OCTOPUS_ACCOUNT_NUMBER in the "
            "environment / .env to fetch current rates."
        )
    rate_periods, fresh = await pipeline.fetch_octopus_inputs(
        OctopusRestClient(settings.octopus_api_key),
        OctopusGraphQLClient(settings.octopus_api_key),
        settings.octopus_account_number,
    )

    db = _open_db(settings, dry_run)
    try:
        if dry_run:
            has_table = inspect(db.get_bind()).has_table(StoredDispatch.__tablename__)
            stored = repo.list_dispatches(db) if has_table else []
            merged = {(d.start, d.end): d for d in [*stored, *fresh]}
            dispatches = list(merged.values())
            new = len(dispatches) - len(stored)
        else:
            new = repo.upsert_dispatches(db, fresh)
            dispatches = repo.list_dispatches(db)
        summary = recost_sessions(db, rate_periods, dispatches, dry_run, settings)
        summary.update(
            rate_periods=len(rate_periods),
            dispatches_fetched=len(fresh),
            dispatches_new=new,
            dispatches_used=len(dispatches),
        )
        return summary
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #

def format_summary(summary: dict[str, Any]) -> str:
    """The before/after report printed by each command."""
    note = " (DRY RUN - nothing written)" if summary["dry_run"] else ""
    lines = [f"ChargeWise {summary['command']}{note}"]
    if summary["command"] == "repair-miles":
        lines.append(
            f"  sessions examined: {summary['examined']}, miles changed: {summary['changed']} "
            f"(filled {summary['filled']}, corrected {summary['corrected']}, "
            f"cleared {summary['cleared']})"
        )
    else:
        if "rate_periods" in summary:
            lines.append(
                f"  rate periods: {summary['rate_periods']}, dispatches used: "
                f"{summary['dispatches_used']} ({summary['dispatches_fetched']} from the "
                f"feed, {summary['dispatches_new']} new)"
            )
        lines.append(
            f"  home sessions examined: {summary['examined']}, cost changed: "
            f"{summary['changed']} ({summary['raised']} up, {summary['lowered']} down), "
            f"left alone - no rate period covers them: {summary['unpriced']}"
        )
    lines.append(f"  {'':<24}{'before':>12}{'after':>12}{'change':>12}")
    for key, label in SUMMARY_FIELDS:
        before, after = summary["before"][key], summary["after"][key]
        if key == "session_count":
            lines.append(f"  {label:<24}{before:>12}{after:>12}{after - before:>+12}")
        else:
            lines.append(
                f"  {label:<24}{before:>12.2f}{after:>12.2f}{after - before:>+12.2f}"
            )
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m chargewise.maintenance",
        description="ChargeWise maintenance: repair figures already in the database.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    for name, text in (
        ("repair-miles", "Recompute every session's miles from stored odometer readings."),
        ("recost", "Re-price every stored home session (current rates, all stored dispatches)."),
    ):
        cmd = sub.add_parser(name, help=text, description=text)
        cmd.add_argument("--dry-run", action="store_true",
                         help="Show the before/after summary without writing anything.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    settings = get_settings()
    if args.command == "repair-miles":
        summary = run_repair_miles(settings, args.dry_run)
    else:
        try:
            summary = asyncio.run(run_recost(settings, args.dry_run))
        except Exception as exc:
            reason = pipeline.describe_error(exc, "octopus", (settings.octopus_api_key,))
            print(f"recost failed, no session was re-priced: {reason}", file=sys.stderr)
            raise
    print(format_summary(summary))
    if summary.get("unpriced"):
        # Partly done is not done: say so, and fail, so it cannot pass unnoticed.
        shown = summary["unpriced_first"]
        more = ", ..." if summary["unpriced"] > len(shown) else ""
        print(
            f"recost incomplete: {summary['unpriced']} home session(s) could not be "
            f"priced - no rate period covers them - and were left as they were. "
            f"First: {', '.join(shown)}{more}",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
