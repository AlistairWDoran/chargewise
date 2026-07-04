"""Generic charge-session source — a simple CSV the pipeline can ingest today.

The TeslaFi history adapter is still pending (awaiting TeslaFi support on field
names, pagination and Supercharger invoice cost — see docs/TESLAFI-SUPPORT-EMAIL.md).
Until it lands, any source that can emit charge sessions in this columnar form
(a TeslaFi CSV export, a manual log) flows through the *same* costing path, so the
pipeline is runnable and testable end-to-end now.

Columns (header row required; order-independent):
    start                 ISO-8601, timezone-aware (e.g. 2026-06-15T23:00:00+01:00)
    end                   ISO-8601, timezone-aware
    energy_kwh            float
    location_type         'home' | 'away'
    odometer              float, optional (vehicle odometer in miles at session)
    raw_cost              float GBP, optional (away/public recorded cost)
    raw_cost_is_estimate  'true' | 'false', optional (default true)

Parsing is pure (text in, models out) so it needs no network and is unit-tested.
"""

from __future__ import annotations

import csv
from datetime import datetime
from io import StringIO

from ..engine.models import ChargeSession, LocationType


def _opt_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    return float(value) if value else None


def parse_charge_sessions_csv(text: str) -> list[ChargeSession]:
    """Parse charge-session CSV text into engine ChargeSession models (sorted)."""
    text = text.lstrip("﻿")  # strip BOM if present
    reader = csv.DictReader(StringIO(text))
    out: list[ChargeSession] = []
    for row in reader:
        start_value = (row.get("start") or "").strip()
        if not start_value:
            continue
        raw_cost = _opt_float(row.get("raw_cost"))
        is_estimate_raw = (row.get("raw_cost_is_estimate") or "").strip().lower()
        out.append(
            ChargeSession(
                start=datetime.fromisoformat(start_value),
                end=datetime.fromisoformat(row["end"].strip()),
                energy_kwh=float(row["energy_kwh"]),
                location_type=LocationType(row["location_type"].strip().lower()),
                odometer=_opt_float(row.get("odometer")),
                raw_cost=raw_cost,
                # Default to estimate=True (conservative) unless explicitly 'false'.
                raw_cost_is_estimate=is_estimate_raw != "false",
            )
        )
    out.sort(key=lambda s: s.start)
    return out
