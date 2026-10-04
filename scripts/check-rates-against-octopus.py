#!/usr/bin/env python3
"""Check ChargeWise's derived rate periods against Octopus's published rates.

NETWORK-USING acceptance check — deliberately NOT part of the test suite. It
needs no API key: the Octopus product/rate endpoints are public.

What it does
------------
1. Loads a tariff-agreement history (a JSON list of
   ``{tariff_code, valid_from, valid_to}``) and derives rate periods with the
   same code path ingestion uses:
   ``fetch_rate_periods(OctopusRestClient(""), agreements)``.
2. INDEPENDENTLY downloads the raw published unit-rate records for every
   non-zero-length agreement (own HTTP calls, own pagination, own parsing).
3. For every half-hour instant from ``--from`` to now compares

   * **truth**  — the raw record covering the instant, from the agreement in
     force at that instant (day/night tariff: the night rate inside the core
     off-peak window, the day rate outside);
   * **engine** — the one period covering the instant: its off-peak rate if
     ``in_core_window(instant)`` else its peak rate.

   Every instant must be covered by exactly one period and engine must equal
   truth to 1e-9 GBP. Oddities in the published records themselves (gaps,
   overlaps, records straddling the core-window boundary) are reported, not
   papered over.
4. Optional sanity figure (``--db``): re-price every home session in a COPY of
   a ChargeWise SQLite DB with the derived periods and NO dispatches.

Exit status is 0 only if there are no mismatches and no coverage problems.

Usage
-----
  python3 scripts/check-rates-against-octopus.py agreements.json
  python3 scripts/check-rates-against-octopus.py agreements.json \
      --from 2022-02-01T00:00:00Z --db chargewise.db
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from chargewise.engine import (  # noqa: E402
    ChargeSession,
    LocationType,
    RatePeriod,
    cost_home_session,
    in_core_window,
)
from chargewise.ingest.octopus_rest import (  # noqa: E402
    OctopusRestClient,
    TariffAgreement,
    parse_agreements,
)
from chargewise.ingest.pipeline import fetch_rate_periods  # noqa: E402

API = "https://api.octopus.energy/v1"
HALF_HOUR = timedelta(minutes=30)
TOLERANCE_GBP = 1e-9

Raw = tuple[datetime, datetime | None, float]  # (valid_from, valid_to, pence inc VAT)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if value else "open"


# --------------------------------------------------------------------------- #
# Independent download of the raw published records (no ChargeWise code).
# --------------------------------------------------------------------------- #

def download_raw(client: httpx.Client, agreement: TariffAgreement, register: str) -> list[Raw]:
    """All published ``register``-unit-rates records overlapping the agreement."""
    product = "-".join(agreement.tariff_code.split("-")[2:-1])
    url: str | None = (
        f"{API}/products/{product}/electricity-tariffs/"
        f"{agreement.tariff_code}/{register}-unit-rates/"
    )
    params: dict[str, Any] | None = {
        "period_from": agreement.valid_from.isoformat(), "page_size": 500,
    }
    if agreement.valid_to is not None and params is not None:
        params["period_to"] = agreement.valid_to.isoformat()
    raw: list[Raw] = []
    while url:
        resp = client.get(url, params=params)
        if resp.status_code == 400 and "This tariff has" in resp.text:
            return []  # wrong register type for this tariff
        resp.raise_for_status()
        payload = resp.json()
        raw.extend(
            (_dt(r["valid_from"]), _dt(r["valid_to"]) if r.get("valid_to") else None,
             float(r["value_inc_vat"]))
            for r in payload["results"]
        )
        url, params = payload.get("next"), None
    return sorted(set(raw), key=lambda r: r[0])


class RawRates:
    """The raw records of one agreement, with point lookup."""

    def __init__(self, agreement: TariffAgreement, client: httpx.Client) -> None:
        self.agreement = agreement
        self.standard = download_raw(client, agreement, "standard")
        self.day: list[Raw] = []
        self.night: list[Raw] = []
        if not self.standard:
            self.day = download_raw(client, agreement, "day")
            self.night = download_raw(client, agreement, "night")
        self._starts = {
            id(recs): [r[0] for r in recs] for recs in (self.standard, self.day, self.night)
        }

    def _lookup(self, records: list[Raw], when: datetime) -> float | None:
        i = bisect.bisect_right(self._starts[id(records)], when) - 1
        if i < 0:
            return None
        _, end, value = records[i]
        return value if end is None or when < end else None

    def truth_pence(self, when: datetime) -> float | None:
        if self.standard:
            return self._lookup(self.standard, when)
        return self._lookup(self.night if in_core_window(when) else self.day, when)

    def oddities(self) -> list[str]:
        """Gaps, overlaps and window-straddling records in the published data."""
        found: list[str] = []
        a = self.agreement
        for name, records in (("standard", self.standard), ("day", self.day),
                              ("night", self.night)):
            if not records:
                continue
            for prev, cur in zip(records, records[1:]):
                if prev[1] is None or cur[0] < prev[1]:
                    found.append(f"{name}: OVERLAP {_iso(prev[0])}..{_iso(prev[1])} "
                                 f"and {_iso(cur[0])}..{_iso(cur[1])}")
                elif cur[0] > prev[1]:
                    found.append(f"{name}: GAP {_iso(prev[1])}..{_iso(cur[0])}")
            if records[0][0] > a.valid_from:
                found.append(f"{name}: first record starts {_iso(records[0][0])}, after "
                             f"the agreement start {_iso(a.valid_from)}")
            last_end = records[-1][1]
            if a.valid_to is not None and last_end is not None and last_end < a.valid_to:
                found.append(f"{name}: last record ends {_iso(last_end)}, before the "
                             f"agreement end {_iso(a.valid_to)}")
        sliced = [r for r in self.standard if r[1] is not None and r[1] - r[0] < timedelta(hours=24)]
        straddlers = [
            r for r in sliced
            if r[1] is not None and in_core_window(r[0]) != in_core_window(r[1] - HALF_HOUR)
        ]
        for r in straddlers[:5]:
            found.append(f"standard: record {_iso(r[0])}..{_iso(r[1])} ({r[2]}p) straddles "
                         "the core-window boundary")
        if len(straddlers) > 5:
            found.append(f"standard: ... {len(straddlers) - 5} more straddling record(s)")
        return found


# --------------------------------------------------------------------------- #
# Comparison.
# --------------------------------------------------------------------------- #

def in_force(agreements: list[TariffAgreement], when: datetime) -> list[TariffAgreement]:
    return [
        a for a in agreements
        if a.valid_from <= when and (a.valid_to is None or when < a.valid_to)
    ]


def compare(
    agreements: list[TariffAgreement],
    raw: dict[int, RawRates],
    periods: list[RatePeriod],
    start: datetime,
    end: datetime,
) -> bool:
    total = 0
    problems: Counter[str] = Counter()
    examples: list[str] = []

    def problem(kind: str, line: str) -> None:
        problems[kind] += 1
        if len(examples) < 10:
            examples.append(f"{kind}: {line}")

    when = start
    while when < end:
        total += 1
        tag = _iso(when)
        covering = [p for p in periods if p.covers(when)]
        active = in_force(agreements, when)
        truth: float | None = None
        tariff = "-"
        if len(active) != 1:
            problem("agreements in force != 1", f"{tag} {[a.tariff_code for a in active]}")
        else:
            tariff = active[0].tariff_code
            pence = raw[id(active[0])].truth_pence(when)
            if pence is None:
                problem("no published record (truth missing)", f"{tag} {tariff}")
            else:
                truth = pence / 100.0
        if len(covering) != 1:
            problem("periods covering != 1", f"{tag} {tariff} covered by {len(covering)}")
        elif truth is not None:
            period = covering[0]
            engine = period.offpeak_inc_vat if in_core_window(when) else period.peak_inc_vat
            if abs(engine - truth) > TOLERANCE_GBP:
                problem("MISMATCH", f"{tariff} {tag} truth={truth:.6f} engine={engine:.6f}")
        when += HALF_HOUR

    print(f"\nHalf-hour instants checked: {total}  ({_iso(start)} .. {_iso(end)})")
    print(f"Mismatches (|engine - truth| > {TOLERANCE_GBP} GBP): {problems['MISMATCH']}")
    print(f"Instants not covered by exactly one period: {problems['periods covering != 1']}")
    print("Instants with no published record: "
          f"{problems['no published record (truth missing)']}")
    print("Instants without exactly one agreement in force: "
          f"{problems['agreements in force != 1']}")
    if examples:
        print("First problems:")
        for line in examples:
            print(f"  {line}")
    return not problems


def report_periods(agreements: list[TariffAgreement], periods: list[RatePeriod]) -> None:
    print(f"\nRate periods derived: {len(periods)}")
    for a in agreements:
        zero = a.valid_to is not None and a.valid_to <= a.valid_from
        mine = [
            p for p in periods
            if a.valid_from <= p.valid_from and (a.valid_to is None or p.valid_from < a.valid_to)
        ]
        note = "  (zero-length: skipped)" if zero else ""
        print(f"  {a.tariff_code}  {_iso(a.valid_from)} .. {_iso(a.valid_to)}: "
              f"{len(mine)} period(s){note}")
        for p in mine:
            print(f"      {_iso(p.valid_from)} .. {_iso(p.valid_to)}  "
                  f"off-peak {p.offpeak_inc_vat * 100:.4f}p  peak {p.peak_inc_vat * 100:.4f}p")


# --------------------------------------------------------------------------- #
# Sanity figure: re-price home sessions from a COPY of the DB.
# --------------------------------------------------------------------------- #

def reprice_home_sessions(db_path: str, periods: list[RatePeriod]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.sqlite"
        shutil.copyfile(db_path, copy)  # never open the original
        con = sqlite3.connect(copy)
        rows = con.execute(
            "SELECT start_utc, end_utc, energy_kwh, cost_gbp FROM charge_session "
            "WHERE location_type = 'home' ORDER BY start_utc"
        ).fetchall()
        con.close()

    years: dict[str, list[float]] = {}  # year -> [sessions, kWh, repriced, stored]
    unpriced: list[str] = []
    for start_utc, end_utc, kwh, stored in rows:
        session = ChargeSession(_dt(start_utc), _dt(end_utc), kwh, LocationType.HOME)
        try:
            cost = cost_home_session(session, periods, dispatches=[]).total_cost
        except ValueError as exc:
            unpriced.append(f"{start_utc}: {exc}")
            continue
        acc = years.setdefault(start_utc[:4], [0, 0.0, 0.0, 0.0])
        acc[0] += 1
        acc[1] += kwh
        acc[2] += cost
        acc[3] += stored

    print(f"\nSanity re-price of {len(rows)} home session(s), NO dispatches "
          "(daytime smart-charge slots priced at peak, so this is an upper bound):")
    print(f"  {'year':<6}{'sessions':>9}{'kWh':>11}{'re-priced GBP':>15}{'stored GBP':>12}")
    for year in sorted(years):
        n, kwh, cost, stored = years[year]
        print(f"  {year:<6}{int(n):>9}{kwh:>11.1f}{cost:>15.2f}{stored:>12.2f}")
    n, kwh, cost, stored = (sum(v[i] for v in years.values()) for i in range(4))
    print(f"  {'total':<6}{int(n):>9}{kwh:>11.1f}{cost:>15.2f}{stored:>12.2f}")
    if rows:
        print(f"  sessions span {rows[0][0]} .. {rows[-1][0]}")
    print(f"  sessions that could not be priced: {len(unpriced)}")
    for line in unpriced[:10]:
        print(f"    {line}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("agreements", help="JSON list of {tariff_code, valid_from, valid_to}")
    ap.add_argument("--from", dest="start", default=None, metavar="ISO",
                    help="First half-hour instant to check, e.g. 2022-02-01T00:00:00Z "
                         "(default: the start of the earliest agreement)")
    ap.add_argument("--db", default=None,
                    help="ChargeWise SQLite DB; a COPY is re-priced as a sanity figure")
    args = ap.parse_args()

    agreements = parse_agreements(json.loads(Path(args.agreements).read_text()))
    start = _dt(args.start) if args.start else min(a.valid_from for a in agreements)
    now = datetime.now(timezone.utc)
    end = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0) + HALF_HOUR

    periods = asyncio.run(fetch_rate_periods(OctopusRestClient(""), agreements))
    report_periods(agreements, periods)

    raw: dict[int, RawRates] = {}
    print("\nPublished records (independent download):")
    with httpx.Client(timeout=30) as client:
        for a in agreements:
            if a.valid_to is not None and a.valid_to <= a.valid_from:
                continue
            rates = raw[id(a)] = RawRates(a, client)
            counts = (f"standard {len(rates.standard)}" if rates.standard
                      else f"standard 0, day {len(rates.day)}, night {len(rates.night)}")
            print(f"  {a.tariff_code}  {_iso(a.valid_from)} .. {_iso(a.valid_to)}: {counts}")
            for line in rates.oddities():
                print(f"      ODDITY {line}")

    ok = compare(agreements, raw, periods, start, end)
    if args.db:
        reprice_home_sessions(args.db, periods)
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
