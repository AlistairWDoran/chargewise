"""GOV.UK fuel-price ingester tests, using real rows from the published CSV."""

from __future__ import annotations

from datetime import date

from chargewise.ingest.fuel_prices import parse_fuel_csv, price_for_date

# Real rows around Alistair's car changeover (early August 2024).
SAMPLE_CSV = """﻿Date,ULSP Pump price in pence/litre,ULSD Pump price in pence/litre,ULSP Duty,ULSD Duty,ULSP VAT,ULSD VAT
22/07/2024,144.69,150.59,52.95,52.95,20,20
29/07/2024,144.19,150.16,52.95,52.95,20,20
05/08/2024,143.42,149.1,52.95,52.95,20,20
12/08/2024,142.91,148.48,52.95,52.95,20,20
"""


def test_parse_returns_sorted_weeks():
    weeks = parse_fuel_csv(SAMPLE_CSV)
    assert len(weeks) == 4
    assert weeks[0].week_start == date(2024, 7, 22)
    assert weeks[2].week_start == date(2024, 8, 5)
    assert weeks[2].petrol_ppl == 143.42
    assert weeks[2].diesel_ppl == 149.1


def test_price_for_date_uses_week_commencing():
    weeks = parse_fuel_csv(SAMPLE_CSV)
    # A Wednesday inside the week commencing Mon 5 Aug 2024.
    assert price_for_date(weeks, date(2024, 8, 7), "petrol") == 143.42
    assert price_for_date(weeks, date(2024, 8, 7), "diesel") == 149.1


def test_price_for_date_before_first_week_is_none():
    weeks = parse_fuel_csv(SAMPLE_CSV)
    assert price_for_date(weeks, date(2024, 1, 1)) is None
