"""GOV.UK weekly road fuel prices ingester.

Source: https://www.gov.uk/government/statistics/weekly-road-fuel-prices
Licence: Open Government Licence v3.0 (© Crown copyright). Attribution required.

CSV columns (2018+ file):
    Date (DD/MM/YYYY), ULSP pump price p/L, ULSD pump price p/L,
    ULSP duty, ULSD duty, ULSP VAT %, ULSD VAT %
`Date` is the Monday the price week commences.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from io import StringIO

# Current "2018 to present" CSV. The dated filename changes weekly; the landing
# page is scraped for the latest asset URL by fetch_latest_csv_url().
LANDING_PAGE = "https://www.gov.uk/government/statistics/weekly-road-fuel-prices"


@dataclass(frozen=True)
class FuelPriceWeek:
    week_start: date          # Monday the week commences
    petrol_ppl: float         # ULSP pence/litre
    diesel_ppl: float         # ULSD pence/litre


def parse_fuel_csv(text: str) -> list[FuelPriceWeek]:
    """Parse the GOV.UK weekly road fuel prices CSV text into weekly records."""
    text = text.lstrip("﻿")  # strip BOM if present
    reader = csv.reader(StringIO(text))
    rows = list(reader)
    out: list[FuelPriceWeek] = []
    for row in rows[1:]:  # skip header
        if len(row) < 3 or not row[0].strip():
            continue
        try:
            week_start = datetime.strptime(row[0].strip(), "%d/%m/%Y").date()
            petrol = float(row[1])
            diesel = float(row[2])
        except ValueError:
            continue
        out.append(FuelPriceWeek(week_start, petrol, diesel))
    out.sort(key=lambda w: w.week_start)
    return out


def price_for_date(
    weeks: list[FuelPriceWeek], when: date, fuel: str = "petrol"
) -> float | None:
    """Return the p/L for the week containing `when` (latest week_start <= when)."""
    if not weeks:
        return None
    starts = [w.week_start for w in weeks]
    idx = bisect_right(starts, when) - 1
    if idx < 0:
        return None
    week = weeks[idx]
    return week.petrol_ppl if fuel == "petrol" else week.diesel_ppl


async def fetch_fuel_csv(url: str) -> str:
    """Download the CSV text. Network wrapper kept thin; parsing is pure above."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text
