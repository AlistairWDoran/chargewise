"""TeslaFi history API adapter.

Fetches charge history from ``https://www.teslafi.com/history.php`` and maps it
into engine ``ChargeSession`` models. Verified empirically (July 2026) against
Alistair's account:

- ``command=charges&dateFrom=YYYY-MM-DD&dateTo=YYYY-MM-DD`` returns
  ``{"count": N, "results": [...]}``; history reaches back to Feb 2022.
- ``date`` is the session start in **UTC** (winter records match the local
  ``firstDataT`` exactly; summer records differ by +1h — i.e. BST).
- ``totalMinutes`` is the full plugged-in window (``minutes`` is active-charging
  time only); session end = ``date`` + ``totalMinutes``.
- ``chargerKWH`` is energy drawn from the wall (what you pay for) — preferred
  over ``energyAdded`` (energy into the battery, net of losses).
- ``vin`` distinguishes vehicles (LRW… = Tesla #1 to Aug 2024, XP7… = Tesla #2).
- ``homeChargeFlag``/``superChargerFlag`` classify location; ``superCost`` /
  ``travelCost`` carry TeslaFi's away-cost figures.

Parsing is pure (JSON in, models out); the network client is a thin httpx
wrapper, matching the other adapters. Open questions to TeslaFi support
(pagination, invoice-cost provenance, beta stability) are tracked in
``docs/TESLAFI-SUPPORT-EMAIL.md``; until answered, away costs are treated as
estimates unless a positive Supercharger cost is present.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..engine.models import ChargeSession, LocationType

BASE_URL = "https://www.teslafi.com/history.php"

# Sessions with less energy than this are metering noise, not charges.
MIN_ENERGY_KWH = 0.1


@dataclass(frozen=True)
class TeslaFiCharge:
    """A parsed TeslaFi charge record: the session plus vehicle attribution."""

    vin: str
    model: str
    session: ChargeSession


def _f(value: object) -> float | None:
    """TeslaFi floats arrive as strings, empty strings, numbers or null."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_teslafi_charges(payload: dict[str, Any]) -> list[TeslaFiCharge]:
    """Map a TeslaFi ``command=charges`` payload to charge sessions (sorted).

    Records without usable energy or duration are skipped (TeslaFi logs
    zero-energy plug-ins). Home sessions carry no raw cost — the engine
    re-costs them against real Octopus rates. Away sessions prefer the
    Supercharger cost (non-estimate when positive), else TeslaFi's travel
    cost flagged as an estimate.
    """
    out: list[TeslaFiCharge] = []
    for rec in payload.get("results", []):
        start_raw = (rec.get("date") or "").strip()
        if not start_raw:
            continue
        start = datetime.strptime(start_raw, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )

        duration_min = _f(rec.get("totalMinutes")) or _f(rec.get("minutes"))
        if not duration_min or duration_min <= 0:
            continue

        energy = (
            _f(rec.get("chargerKWH"))
            or _f(rec.get("totalEnergyAdded"))
            or _f(rec.get("energyAdded"))
        )
        if not energy or energy < MIN_ENERGY_KWH:
            continue

        is_home = str(rec.get("homeChargeFlag")) == "1"
        is_super = str(rec.get("superChargerFlag")) == "1"

        raw_cost: float | None = None
        raw_cost_is_estimate = True
        if not is_home:
            super_cost = _f(rec.get("superCost"))
            travel_cost = _f(rec.get("travelCost"))
            if is_super and super_cost and super_cost > 0:
                raw_cost, raw_cost_is_estimate = super_cost, False
            elif travel_cost and travel_cost > 0:
                raw_cost = travel_cost

        out.append(
            TeslaFiCharge(
                vin=(rec.get("vin") or "").strip(),
                model=(rec.get("model") or "").strip(),
                session=ChargeSession(
                    start=start,
                    end=start + timedelta(minutes=duration_min),
                    energy_kwh=energy,
                    location_type=LocationType.HOME if is_home else LocationType.AWAY,
                    odometer=_f(rec.get("odometer")),
                    raw_cost=raw_cost,
                    raw_cost_is_estimate=raw_cost_is_estimate,
                ),
            )
        )
    out.sort(key=lambda c: c.session.start)
    return out


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """Inclusive monthly (from, to) chunks covering [start, end] for backfill."""
    ranges: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        ranges.append((cursor, min(next_month - timedelta(days=1), end)))
        cursor = next_month
    return ranges


class TeslaFiHistoryClient:
    """Thin network wrapper. Parsing lives in the pure functions above."""

    def __init__(self, token: str, base_url: str = BASE_URL) -> None:
        self.token = token
        self.base_url = base_url

    async def get_charges(self, date_from: date, date_to: date) -> list[TeslaFiCharge]:
        """Fetch one window, backing off and retrying on TeslaFi's rate limit.

        Empirically the endpoint 429s after a burst of ~30 rapid calls, so a
        long backfill must expect to be throttled mid-run and wait it out.
        """
        import asyncio

        import httpx

        params = {
            "token": self.token,
            "command": "charges",
            "dateFrom": date_from.isoformat(),
            "dateTo": date_to.isoformat(),
        }
        async with httpx.AsyncClient(timeout=60) as client:
            for attempt in range(6):
                resp = await client.get(self.base_url, params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(65 * (attempt + 1))
                    continue
                resp.raise_for_status()
                payload = resp.json()
                # Checksum: TeslaFi's `count` should match the results list.
                # Partial/truncated responses were observed on 11 Jul 2026
                # (first block of a window only) — retry rather than accept.
                declared = payload.get("count")
                actual = len(payload.get("results", []))
                if isinstance(declared, int) and declared != actual:
                    print(
                        f"  teslafi {date_from}..{date_to}: partial response "
                        f"({actual}/{declared}) — retrying", flush=True,
                    )
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                return parse_teslafi_charges(payload)
        raise RuntimeError(
            f"TeslaFi still rate-limiting or truncating after retries "
            f"({date_from}..{date_to})"
        )

    async def backfill(self, start: date, end: date) -> list[TeslaFiCharge]:
        """Fetch charges month-by-month (gentle on the beta endpoint), deduped.

        Monthly windows are kept deliberately small while TeslaFi's pagination
        behaviour is unconfirmed; with rate-limit backoff a full multi-year
        backfill can take 15–20 minutes. Progress is printed per chunk so a
        long run is visibly alive.
        """
        import asyncio

        seen: set[tuple[str, datetime]] = set()
        charges: list[TeslaFiCharge] = []
        for date_from, date_to in month_ranges(start, end):
            got = await self.get_charges(date_from, date_to)
            for charge in got:
                key = (charge.vin, charge.session.start)
                if key not in seen:
                    seen.add(key)
                    charges.append(charge)
            print(f"  teslafi {date_from:%Y-%m}: {len(got)} charges", flush=True)
            await asyncio.sleep(3)
        charges.sort(key=lambda c: c.session.start)
        return charges
