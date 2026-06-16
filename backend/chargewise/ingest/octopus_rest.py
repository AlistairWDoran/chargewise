"""Octopus Energy REST adapter.

Pulls tariff agreements (which tariff applied when) and half-hourly unit-rate
history, then derives the off-peak/peak RatePeriod pairs the cost engine needs.

REST base: https://api.octopus.energy/v1
Auth: HTTP basic, API key as username, blank password.
Endpoints used:
  /accounts/{account}/                                  -> meters + tariff agreements
  /products/{product}/electricity-tariffs/{tariff}/standard-unit-rates/  -> rate history
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..engine.models import RatePeriod

BASE_URL = "https://api.octopus.energy/v1"


def _dt(value: str) -> datetime:
    """Parse an Octopus ISO timestamp (handles trailing 'Z')."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class TariffAgreement:
    tariff_code: str
    valid_from: datetime
    valid_to: datetime | None


@dataclass(frozen=True)
class RateRecord:
    valid_from: datetime
    valid_to: datetime | None
    value_inc_vat: float
    value_exc_vat: float


def parse_account(payload: dict) -> dict:
    """Extract the first electricity meter point's mpan, serial and agreements."""
    prop = payload["properties"][0]
    emp = prop["electricity_meter_points"][0]
    agreements = [
        TariffAgreement(a["tariff_code"], _dt(a["valid_from"]),
                        _dt(a["valid_to"]) if a.get("valid_to") else None)
        for a in emp.get("agreements", [])
    ]
    return {
        "mpan": emp["mpan"],
        "serial_number": emp["meters"][0]["serial_number"] if emp.get("meters") else None,
        "agreements": agreements,
    }


def parse_unit_rates(payload: dict) -> list[RateRecord]:
    """Parse a standard-unit-rates response into rate records (sorted by start)."""
    records = [
        RateRecord(_dt(r["valid_from"]),
                   _dt(r["valid_to"]) if r.get("valid_to") else None,
                   float(r["value_inc_vat"]), float(r["value_exc_vat"]))
        for r in payload.get("results", [])
    ]
    records.sort(key=lambda r: r.valid_from)
    return records


def derive_iog_rate_periods(records: list[RateRecord]) -> list[RatePeriod]:
    """Derive off-peak/peak RatePeriod pairs from the unit-rate history.

    Intelligent Octopus Go has two price levels at any moment: the off-peak
    (lower) and the standard/peak (higher) rate. v1 collapses the supplied
    records into a single era using the min/max inc-VAT values; multi-era
    handling (when the peak rate changes over time) is a planned refinement,
    so callers should fetch rates per pricing era for full historical accuracy.
    """
    if not records:
        return []
    offpeak = min(r.value_inc_vat for r in records)
    peak = max(r.value_inc_vat for r in records)
    valid_from = min(r.valid_from for r in records)
    valid_to = None if any(r.valid_to is None for r in records) else max(
        r.valid_to for r in records  # type: ignore[type-var]
    )
    return [RatePeriod(valid_from, valid_to, offpeak, peak)]


class OctopusRestClient:
    """Thin network wrapper. Parsing lives in the pure functions above."""

    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def _get(self, path: str, params: dict | None = None) -> dict:
        import httpx

        async with httpx.AsyncClient(timeout=30, auth=(self.api_key, "")) as client:
            resp = await client.get(f"{self.base_url}{path}", params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_account(self, account_number: str) -> dict:
        return parse_account(await self._get(f"/accounts/{account_number}/"))

    async def get_unit_rates(
        self, product: str, tariff_code: str, period_from: str, period_to: str
    ) -> list[RateRecord]:
        path = f"/products/{product}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
        payload = await self._get(
            path, {"period_from": period_from, "period_to": period_to, "page_size": 1500}
        )
        return parse_unit_rates(payload)
