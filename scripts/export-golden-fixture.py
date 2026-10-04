#!/usr/bin/env python3
"""Export a golden-reconciliation fixture for one bill month (run on the LAN).

Produces ``backend/tests/fixtures/golden/<YYYY-MM>.real.json`` — the
self-contained snapshot that ``backend/tests/test_golden_reconciliation.py``
prices offline (METHODOLOGY.md §6–§7 accuracy gate). This script is the ONLY
step that needs the live system; the fixture it writes needs no network. The
fixture holds a household's sessions or consumption, so ``*.real.json`` is
git-ignored: it stays on the machine that made it, and the test runs it there.

Two modes (``--mode``)
----------------------
* ``sessions`` (default) — snapshot the month's HOME CHARGING sessions; the
  bill figures you supply must be the home-charging portion of the bill.
* ``whole-house`` — snapshot the month's half-hourly WHOLE-HOUSE meter
  consumption from the Octopus consumption endpoint (MPAN + meter serial are
  resolved from the account automatically). On IOG the off-peak rate applies
  to the whole home during the core window and smart-dispatch slots, so the
  entire energy bill reconciles: you supply just two numbers from the bill —
  billed kWh and billed energy cost. BOTH MUST EXCLUDE THE STANDING CHARGE
  (energy/consumption charges only). Requires OCTOPUS_API_KEY and
  OCTOPUS_ACCOUNT_NUMBER (backend/.env).

What it gathers
---------------
* **Sessions** (sessions mode) — home charge sessions whose start falls in the bill month
  (Europe/London local time), either from the running ChargeWise API
  (``GET /api/charges?location=home``, default ``http://localhost:8000``;
  pass ``--api`` for an instance on another host)
  or, with ``--db``, read directly from the SQLite file (``charge_session``
  table). Note: the DB stores costed sessions and every dispatch seen since
  October 2026, but not rates. This script fetches rates and the current
  dispatch feed separately (below); it does not yet read stored dispatches.
* **Rate periods** — via the Octopus API using the same code path as ingestion
  (``chargewise.ingest.pipeline.fetch_octopus_inputs``), which needs
  ``OCTOPUS_API_KEY`` and ``OCTOPUS_ACCOUNT_NUMBER`` in the environment (the
  backend ``.env`` works: ``set -a; . backend/.env; set +a``). If you can't or
  don't want to hit Octopus, pass ``--offpeak/--peak`` (GBP/kWh inc VAT) and
  optionally ``--rates-valid-from`` instead.
* **Dispatches** — from the same Octopus fetch. CAVEAT (METHODOLOGY §8): the
  dispatch feed only covers a recent window, so export the fixture SOON after
  the bill month ends or daytime smart charges will be priced at peak
  (conservative — computed cost overstated).
* **Bill figures** — from the Octopus bill PDF/email, supplied by hand:
  ``--billed-kwh`` and ``--billed-cost-inc-vat`` (the HOME-CHARGING portion of
  the bill, not the whole-house total). If omitted, the fixture is written
  with ``status: placeholder`` and the CI test SKIPS it until they're added.

Examples
--------
  # PREFERRED: whole-house reconciliation — consumption, rates and dispatches
  # all from Octopus; bill figures EXCLUDING the standing charge:
  set -a; . backend/.env; set +a
  python3 scripts/export-golden-fixture.py --mode whole-house --month 2026-06 \
      --billed-kwh 350.20 --billed-cost-inc-vat 45.67

  # Session mode: sessions from a running ChargeWise API, rates/dispatches
  # from Octopus:
  set -a; . backend/.env; set +a
  python3 scripts/export-golden-fixture.py --month 2026-06 \
      --api http://<chargewise-host>:8000 \
      --billed-kwh 123.45 --billed-cost-inc-vat 12.34

  # Sessions straight from the SQLite file, manual rates, bill figures later:
  python3 scripts/export-golden-fixture.py --month 2026-06 \
      --db backend/data/chargewise.sqlite \
      --offpeak 0.069 --peak 0.303714

Then run the gate locally:  cd backend && pytest tests/test_golden_reconciliation.py -rs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "backend" / "tests" / "fixtures" / "golden"
UK = ZoneInfo("Europe/London")
DEFAULT_API = "http://localhost:8000"

# Allow running from a checkout without `pip install -e backend`.
sys.path.insert(0, str(REPO_ROOT / "backend"))


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    """[start, end) of the bill month in Europe/London local time."""
    year, mon = (int(p) for p in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=UK)
    end = datetime(year + 1, 1, 1, tzinfo=UK) if mon == 12 else datetime(year, mon + 1, 1, tzinfo=UK)
    return start, end


def _in_month(start_utc_iso: str, lo: datetime, hi: datetime) -> bool:
    dt = datetime.fromisoformat(start_utc_iso)
    if dt.tzinfo is None:
        # The CSV-import path (charge_sessions.py) can store tz-naive strings;
        # they are UTC — pin that down rather than letting astimezone assume
        # the system-local zone.
        dt = dt.replace(tzinfo=timezone.utc)
    return lo <= dt.astimezone(UK) < hi


def _utc_iso(value: str) -> str:
    """Emit a tz-aware UTC ISO string (naive stored values are UTC — see _in_month)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def sessions_from_api(api: str, token: str | None, month: str) -> list[dict]:
    """GET /api/charges?location=home and keep the bill month's sessions."""
    req = urllib.request.Request(f"{api.rstrip('/')}/api/charges?location=home")
    if token:  # only needed if the API runs with auth enabled (v1 LAN: AUTH_DISABLED)
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.load(resp)
    lo, hi = _month_bounds(month)
    return [
        {"start": _utc_iso(r["start_utc"]), "end": _utc_iso(r["end_utc"]),
         "energy_kwh": r["energy_kwh"], "vehicle": None}
        for r in rows
        if _in_month(r["start_utc"], lo, hi)
    ]


def sessions_from_db(db_path: str, month: str) -> list[dict]:
    """Read home sessions for the month straight from the SQLite file."""
    lo, hi = _month_bounds(month)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT cs.start_utc, cs.end_utc, cs.energy_kwh, v.name "
            "FROM charge_session cs JOIN vehicle v ON v.id = cs.vehicle_id "
            "WHERE cs.location_type = 'home' ORDER BY cs.start_utc"
        ).fetchall()
    finally:
        con.close()
    return [
        {"start": _utc_iso(s), "end": _utc_iso(e), "energy_kwh": kwh, "vehicle": name}
        for (s, e, kwh, name) in rows
        if _in_month(s, lo, hi)
    ]


def _octopus_creds() -> tuple[str, str]:
    api_key = os.environ.get("OCTOPUS_API_KEY")
    account = os.environ.get("OCTOPUS_ACCOUNT_NUMBER")
    if not api_key or not account:
        raise SystemExit(
            "OCTOPUS_API_KEY / OCTOPUS_ACCOUNT_NUMBER not set — either source "
            "backend/.env (set -a; . backend/.env; set +a) or (sessions mode "
            "only) pass manual rates with --offpeak/--peak instead."
        )
    return api_key, account


def _shape_octopus(periods, dispatches) -> tuple[list[dict], list[dict]]:
    return (
        [
            {
                "valid_from": p.valid_from.isoformat(),
                "valid_to": p.valid_to.isoformat() if p.valid_to else None,
                "offpeak_inc_vat": p.offpeak_inc_vat,
                "peak_inc_vat": p.peak_inc_vat,
            }
            for p in periods
        ],
        [
            {"start": d.start.isoformat(), "end": d.end.isoformat(), "location": d.location}
            for d in dispatches
        ],
    )


def octopus_rates_and_dispatches() -> tuple[list[dict], list[dict]]:
    """Fetch rate periods + dispatches via the SAME code path ingestion uses."""
    from chargewise.ingest.octopus_graphql import OctopusGraphQLClient
    from chargewise.ingest.octopus_rest import OctopusRestClient
    from chargewise.ingest.pipeline import fetch_octopus_inputs

    api_key, account = _octopus_creds()
    periods, dispatches = asyncio.run(
        fetch_octopus_inputs(OctopusRestClient(api_key), OctopusGraphQLClient(api_key), account)
    )
    return _shape_octopus(periods, dispatches)


def octopus_whole_house(month: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Rates + dispatches (ingestion code path) plus the month's half-hourly
    whole-house consumption, with MPAN/serial resolved from the account."""
    from chargewise.ingest.octopus_graphql import OctopusGraphQLClient
    from chargewise.ingest.octopus_rest import OctopusRestClient
    from chargewise.ingest.pipeline import fetch_octopus_inputs

    api_key, account = _octopus_creds()
    rest = OctopusRestClient(api_key)
    lo, hi = _month_bounds(month)

    async def _run():
        periods, dispatches = await fetch_octopus_inputs(
            rest, OctopusGraphQLClient(api_key), account
        )
        acct = await rest.get_account(account)
        if not acct.get("serial_number"):
            raise SystemExit("Account has no meter serial number — cannot fetch consumption.")
        consumption = await rest.get_consumption(
            acct["mpan"],
            acct["serial_number"],
            lo.astimezone(timezone.utc).isoformat(),
            hi.astimezone(timezone.utc).isoformat(),
        )
        return periods, dispatches, consumption

    periods, dispatches, consumption = asyncio.run(_run())
    rate_dicts, dispatch_dicts = _shape_octopus(periods, dispatches)
    half_hours = [
        {"start": c.interval_start.astimezone(timezone.utc).isoformat(), "kwh": c.kwh}
        for c in consumption
        if lo <= c.interval_start.astimezone(UK) < hi  # defensive re-clip to the month
    ]
    return rate_dicts, dispatch_dicts, half_hours


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--month", required=True, metavar="YYYY-MM",
                   help="Bill month (Europe/London calendar month).")
    p.add_argument("--mode", choices=["sessions", "whole-house"], default="sessions",
                   help="sessions: snapshot home charge sessions (bill figures = "
                        "home-charging portion). whole-house: snapshot half-hourly "
                        "whole-house meter consumption from Octopus (bill figures = "
                        "the bill's energy total EXCLUDING the standing charge).")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--api", default=None,
                     help=f"ChargeWise API base URL (sessions mode only; "
                          f"default {DEFAULT_API}).")
    src.add_argument("--db", default=None, metavar="PATH",
                     help="Read sessions from this SQLite file instead of the API "
                          "(sessions mode only).")
    p.add_argument("--token", default=None,
                   help="Bearer token for the API if auth is enabled (LAN default: disabled).")
    p.add_argument("--offpeak", type=float, default=None, metavar="GBP_PER_KWH",
                   help="Manual off-peak rate inc VAT (skips the Octopus fetch).")
    p.add_argument("--peak", type=float, default=None, metavar="GBP_PER_KWH",
                   help="Manual peak rate inc VAT (skips the Octopus fetch).")
    p.add_argument("--rates-valid-from", default="2024-10-29T00:00:00+00:00",
                   help="valid_from for the manual rate period (default: IOG go-live).")
    p.add_argument("--billed-kwh", type=float, default=None,
                   help="Billed kWh from the Octopus bill: home-charging portion in "
                        "sessions mode; total energy kWh in whole-house mode. Always "
                        "EXCLUDING the standing charge.")
    p.add_argument("--billed-cost-inc-vat", type=float, default=None,
                   help="Billed cost inc VAT (GBP): home-charging portion in sessions "
                        "mode; total ENERGY cost in whole-house mode. Always "
                        "EXCLUDING the standing charge.")
    p.add_argument("--billed-cost-exc-vat", type=float, default=None,
                   help="Optional: same figure exc VAT (GBP), also excluding the "
                        "standing charge.")
    p.add_argument("--tolerance-pct", type=float, default=2.0,
                   help="Hard CI tolerance in percent (default 2.0, per METHODOLOGY §7).")
    p.add_argument("--out", default=None,
                   help="Output path (default backend/tests/fixtures/golden/<month>.real.json).")
    args = p.parse_args(argv)

    if (args.offpeak is None) != (args.peak is None):
        p.error("--offpeak and --peak must be given together.")
    if args.mode == "whole-house" and args.offpeak is not None:
        p.error("--offpeak/--peak are sessions-mode only: whole-house mode fetches "
                "consumption from Octopus anyway, so rates/dispatches come from the "
                "same authenticated fetch.")
    if args.mode == "whole-house" and (args.api is not None or args.db is not None):
        p.error("--api/--db are sessions-mode only: whole-house mode reads the meter's "
                "half-hourly consumption from the Octopus API, not from ChargeWise.")

    sessions: list[dict] = []
    consumption: list[dict] = []
    if args.mode == "whole-house":
        rate_periods, dispatches, consumption = octopus_whole_house(args.month)
        if not consumption:
            raise SystemExit(
                f"No half-hourly consumption returned for {args.month} — smart-meter "
                "data can lag; wrong month, or try again in a few days."
            )
    else:
        sessions = (
            sessions_from_db(args.db, args.month) if args.db
            else sessions_from_api(args.api or DEFAULT_API, args.token, args.month)
        )
        if not sessions:
            raise SystemExit(f"No home sessions found for {args.month} — wrong month or source?")

        if args.offpeak is not None:
            rate_periods = [{
                "valid_from": args.rates_valid_from, "valid_to": None,
                "offpeak_inc_vat": args.offpeak, "peak_inc_vat": args.peak,
            }]
            dispatches = []
            print("NOTE: manual rates given — dispatches omitted; daytime smart charges "
                  "will price at peak (conservative, METHODOLOGY §8).")
        else:
            rate_periods, dispatches = octopus_rates_and_dispatches()

    have_bill = args.billed_cost_inc_vat is not None
    whole_house = args.mode == "whole-house"
    fixture = {
        "schema_version": 2,
        "mode": "whole_house_consumption" if whole_house else "sessions",
        "status": "real" if have_bill else "placeholder",
        "description": (
            f"Real-bill golden fixture ({args.mode} mode) for {args.month}, exported "
            f"{datetime.now(UK).date().isoformat()} by scripts/export-golden-fixture.py. "
            "Bill figures exclude the standing charge."
            + ("" if have_bill else
               " BILL FIGURES PENDING: fill bill.billed_kwh and "
               "bill.billed_cost_inc_vat_gbp, then set status to 'real'.")
        ),
        "month": args.month,
        "timezone": "Europe/London",
        "tariff": {
            "name": "Intelligent Octopus Go",
            "tariff_code": "E-1R-INTELLI-VAR-24-10-29-H",
            "region": "H",
        },
        "rate_periods": rate_periods,
        "dispatches": dispatches,
        "bill": {
            "month": args.month,
            "billed_kwh": args.billed_kwh,
            "billed_cost_inc_vat_gbp": args.billed_cost_inc_vat,
            "billed_cost_exc_vat_gbp": args.billed_cost_exc_vat,
            "tolerance_pct": args.tolerance_pct,
        },
    }
    if whole_house:
        fixture["consumption"] = consumption
    else:
        fixture["sessions"] = sessions

    default_name = (
        f"{args.month}.whole-house.real.json" if whole_house else f"{args.month}.real.json"
    )
    out = Path(args.out) if args.out else GOLDEN_DIR / default_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=2) + "\n")
    if whole_house:
        total_kwh = sum(c["kwh"] for c in consumption)
        shape = f"{len(consumption)} half-hour slots ({total_kwh:.2f} kWh whole-house)"
    else:
        total_kwh = sum(s["energy_kwh"] for s in sessions)
        shape = f"{len(sessions)} home sessions ({total_kwh:.2f} kWh)"
    print(f"Wrote {out} — {shape}, "
          f"{len(rate_periods)} rate period(s), {len(dispatches)} dispatch(es), "
          f"status={fixture['status']}.")
    if not have_bill:
        print("The golden test will SKIP this fixture until the bill figures are added.")


if __name__ == "__main__":
    main()
