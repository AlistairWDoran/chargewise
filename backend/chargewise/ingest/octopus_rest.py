"""Octopus Energy REST adapter.

Pulls tariff agreements (which tariff applied when) and half-hourly unit-rate
history, then derives the off-peak/peak RatePeriod pairs the cost engine needs.

REST base: https://api.octopus.energy/v1
Auth: HTTP basic, API key as username, blank password. The product/rate
endpoints are public, so no auth header is sent when the key is empty.
Endpoints used:
  /accounts/{account}/                                  -> meters + tariff agreements
  /products/{product}/electricity-tariffs/{tariff}/standard-unit-rates/  -> rate history
  /products/{product}/electricity-tariffs/{tariff}/{day,night}-unit-rates/
                                        -> rate history of two-register tariffs
  /electricity-meter-points/{mpan}/meters/{serial}/consumption/
                                        -> half-hourly whole-house consumption
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from ..engine.cost import in_core_window
from ..engine.models import RatePeriod

BASE_URL = "https://api.octopus.energy/v1"

#: Records per page of unit rates (the API's maximum); further pages are followed.
RATES_PAGE_SIZE = 1500

# Octopus answers 400 with this detail when a tariff is asked for the wrong
# register type ("This tariff has day and night rates, not standard." and vice
# versa). That means "no such rates here", not a failed request.
_WRONG_REGISTER = "This tariff has"


def _dt(value: str) -> datetime:
    """Parse an Octopus ISO timestamp (handles trailing 'Z')."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class OctopusRatesUnavailable(RuntimeError):
    """A non-zero-length tariff agreement yielded no usable unit rates."""


class OctopusRatesOverlap(RuntimeError):
    """Rate records or rate periods overlap, so an instant would have two prices."""


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


@dataclass(frozen=True)
class ConsumptionRecord:
    """One half-hour of whole-house meter consumption."""

    interval_start: datetime
    interval_end: datetime
    kwh: float


def product_code_from_tariff(tariff_code: str) -> str:
    """Derive the Octopus product code from a tariff code.

    Tariff codes look like ``E-1R-INTELLI-VAR-24-10-29-H`` = energy type (``E``),
    register (``1R``), product (``INTELLI-VAR-24-10-29``) and region letter (``H``).
    The standard-unit-rates endpoint is keyed by the product code, so strip the
    leading energy/register pair and the trailing single-letter region.
    """
    parts = tariff_code.split("-")
    if len(parts) < 4 or len(parts[-1]) != 1 or not parts[-1].isalpha():
        raise ValueError(f"Unexpected tariff code: {tariff_code!r}")
    return "-".join(parts[2:-1])


def region_from_tariff(tariff_code: str) -> str:
    """Return the GSP region letter (e.g. ``H``) from a tariff code."""
    return tariff_code.split("-")[-1]


def parse_agreements(items: Iterable[dict[str, Any]]) -> list[TariffAgreement]:
    """Parse ``{tariff_code, valid_from, valid_to}`` dicts into agreements."""
    return [
        TariffAgreement(a["tariff_code"], _dt(a["valid_from"]),
                        _dt(a["valid_to"]) if a.get("valid_to") else None)
        for a in items
    ]


def parse_account(payload: dict) -> dict:
    """Extract the first electricity meter point's mpan, serial and agreements."""
    prop = payload["properties"][0]
    emp = prop["electricity_meter_points"][0]
    agreements = parse_agreements(emp.get("agreements", []))
    return {
        "mpan": emp["mpan"],
        "serial_number": emp["meters"][0]["serial_number"] if emp.get("meters") else None,
        "agreements": agreements,
    }


def parse_unit_rates(payload: dict) -> list[RateRecord]:
    """Parse a standard/day/night-unit-rates response into rate records (sorted by start)."""
    records = [
        RateRecord(_dt(r["valid_from"]),
                   _dt(r["valid_to"]) if r.get("valid_to") else None,
                   float(r["value_inc_vat"]), float(r["value_exc_vat"]))
        for r in payload.get("results", [])
    ]
    records.sort(key=lambda r: r.valid_from)
    return records


def parse_consumption(payload: dict[str, Any]) -> list[ConsumptionRecord]:
    """Parse one page of a consumption response (sorted by interval start)."""
    records = [
        ConsumptionRecord(_dt(r["interval_start"]), _dt(r["interval_end"]),
                          float(r["consumption"]))
        for r in payload.get("results", [])
    ]
    records.sort(key=lambda r: r.interval_start)
    return records


#: A closed record shorter than this is a time-of-use slice (IOG publishes one
#: off-peak and one peak record per day); flat tariffs publish records that run
#: for weeks or months, or are open-ended.
_SLICE_MAX = timedelta(hours=24)

# (start, end, off-peak pence, peak pence) — pence until the final conversion.
_Segment = tuple[datetime, datetime | None, float, float]


def _checked_records(records: Iterable[RateRecord]) -> list[RateRecord]:
    """Sort records by start, drop exact duplicates and refuse overlaps.

    The API's ordering is never trusted. Exact duplicates are harmless (a
    record published mid-pagination shifts the pages by one); records that
    overlap with different content would give one instant two prices (e.g. a
    tariff publishing per-payment-method variants), so that is an error rather
    than a silent pick.
    """
    ordered = sorted(
        {r for r in records if r.valid_to is None or r.valid_to > r.valid_from},
        key=lambda r: (r.valid_from, r.valid_to is None, r.valid_to or r.valid_from),
    )
    for prev, cur in zip(ordered, ordered[1:]):
        if prev.valid_to is None or cur.valid_from < prev.valid_to:
            raise OctopusRatesOverlap(
                "Overlapping unit-rate records: "
                f"{prev.value_inc_vat}p from {prev.valid_from.isoformat()} to "
                f"{prev.valid_to.isoformat() if prev.valid_to else 'open'} and "
                f"{cur.value_inc_vat}p from {cur.valid_from.isoformat()}"
            )
    return ordered


def _clip_records(
    records: Iterable[RateRecord], valid_from: datetime | None, valid_to: datetime | None
) -> list[RateRecord]:
    """Clip records to ``[valid_from, valid_to)``, dropping those left empty."""
    clipped: list[RateRecord] = []
    for r in records:
        start = r.valid_from if valid_from is None else max(r.valid_from, valid_from)
        end = r.valid_to
        if valid_to is not None and (end is None or end > valid_to):
            end = valid_to
        if end is None or end > start:
            clipped.append(replace(r, valid_from=start, valid_to=end))
    return clipped


def _merge_segments(segments: Iterable[_Segment]) -> list[RatePeriod]:
    """Merge adjacent segments with an identical rate pair into RatePeriods."""
    merged: list[_Segment] = []
    for start, end, off_pence, peak_pence in segments:
        if merged and merged[-1][1] == start and merged[-1][2:] == (off_pence, peak_pence):
            merged[-1] = (merged[-1][0], end, off_pence, peak_pence)
        else:
            merged.append((start, end, off_pence, peak_pence))
    # Octopus unit rates arrive in PENCE per kWh; the engine works in GBP.
    return [
        RatePeriod(start, end, off_pence / 100.0, peak_pence / 100.0)
        for start, end, off_pence, peak_pence in merged
    ]


def derive_iog_rate_periods(
    records: Iterable[RateRecord],
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> list[RatePeriod]:
    """Derive off-peak/peak RatePeriods from a standard-unit-rates history.

    One RatePeriod is emitted per *pricing era* — a contiguous interval during
    which the (off-peak, peak) pair is constant — so every slot is priced at
    the rates in force at that time. ``valid_from``/``valid_to`` are the bounds
    of the tariff agreement the records were fetched for: no period starts
    before or ends after them, however long the records themselves run.

    The rule is deterministic and looks only at *when* a record applies, never
    at how its value compares with others:

    - **Time-sliced tariff** (the history contains a closed record shorter
      than 24 hours — Intelligent Octopus Go publishes one off-peak and one
      peak record per day): a record sets the **off-peak** rate if it starts
      inside the core off-peak window (``in_core_window``), otherwise the
      **peak** rate, from its start until the next record on the same side.
      A price change at 00:00 that splits an overnight record therefore
      re-prices only the off-peak member until the peak record at 05:30.
    - **Flat tariff** (every record runs for at least 24 hours or is
      open-ended): each record sets off-peak = peak = its value.

    A side with no record yet (e.g. the peak rate during the first night of
    an agreement) is back-filled from that side's first record; a side that
    never appears equals the other. Periods cover exactly the span the
    published records cover: a gap in the records stays a gap (the engine
    then refuses to price a slot inside it), and the last period is
    open-ended iff the last record is.

    Raises ``OctopusRatesOverlap`` if records overlap.
    """
    ordered = _checked_records(records)
    time_sliced = any(
        r.valid_to is not None and r.valid_to - r.valid_from < _SLICE_MAX for r in ordered
    )

    # The rate pair in force during each (clipped) record; None = not seen yet.
    raw: list[tuple[datetime, datetime | None, float | None, float | None]] = []
    offpeak: float | None = None
    peak: float | None = None
    for r in _clip_records(ordered, valid_from, valid_to):
        if not time_sliced:
            offpeak = peak = r.value_inc_vat
        elif in_core_window(r.valid_from):
            offpeak = r.value_inc_vat
        else:
            peak = r.value_inc_vat
        raw.append((r.valid_from, r.valid_to, offpeak, peak))

    first_offpeak = next((s[2] for s in raw if s[2] is not None), None)
    first_peak = next((s[3] for s in raw if s[3] is not None), None)
    segments: list[_Segment] = []
    for start, end, off, pk in raw:
        off = off if off is not None else first_offpeak
        pk = pk if pk is not None else first_peak
        if off is None or pk is None:  # one side never appears: off-peak = peak
            only = off if off is not None else pk
            assert only is not None  # every record sets at least one side
            off = pk = only
        segments.append((start, end, off, pk))
    return _merge_segments(segments)


def derive_day_night_rate_periods(
    day: Iterable[RateRecord],
    night: Iterable[RateRecord],
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> list[RatePeriod]:
    """Derive RatePeriods for a two-register tariff (off-peak = night, peak = day).

    Such tariffs publish nothing under standard-unit-rates; the day and night
    rates each have their own history. A new period starts at every instant
    either rate changes, clipped to the agreement's ``[valid_from, valid_to)``.
    Only spans where *both* rates are published are covered, and the final
    period is open-ended iff both final records are.
    """
    days = _clip_records(_checked_records(day), valid_from, valid_to)
    nights = _clip_records(_checked_records(night), valid_from, valid_to)

    def covering(records: list[RateRecord], when: datetime) -> RateRecord | None:
        for r in records:
            if r.valid_from <= when and (r.valid_to is None or when < r.valid_to):
                return r
        return None

    cuts = sorted(
        {r.valid_from for r in days + nights}
        | {r.valid_to for r in days + nights if r.valid_to is not None}
    )
    segments: list[_Segment] = []
    for i, start in enumerate(cuts):
        day_rate, night_rate = covering(days, start), covering(nights, start)
        if day_rate is None or night_rate is None:
            continue
        end = cuts[i + 1] if i + 1 < len(cuts) else None
        segments.append((start, end, night_rate.value_inc_vat, day_rate.value_inc_vat))
    return _merge_segments(segments)


def derive_agreement_rate_periods(
    agreement: TariffAgreement,
    standard: Sequence[RateRecord],
    day: Sequence[RateRecord] = (),
    night: Sequence[RateRecord] = (),
) -> list[RatePeriod]:
    """RatePeriods for one tariff agreement, clipped to the agreement's dates.

    Uses the standard-unit-rates history when there is one, else the
    day/night histories. Raises ``OctopusRatesUnavailable`` when neither
    yields a period inside the agreement — silently pricing nothing would
    surface much later as an unexplained "no rate period covers" failure.
    """
    if standard:
        periods = derive_iog_rate_periods(standard, agreement.valid_from, agreement.valid_to)
    else:
        periods = derive_day_night_rate_periods(
            day, night, agreement.valid_from, agreement.valid_to
        )
    if not periods:
        raise OctopusRatesUnavailable(
            f"No usable unit rates for tariff {agreement.tariff_code} "
            f"({agreement.valid_from.isoformat()} to "
            f"{agreement.valid_to.isoformat() if agreement.valid_to else 'open'}): "
            f"standard-unit-rates gave {len(standard)} record(s), "
            f"day-unit-rates {len(day)}, night-unit-rates {len(night)}"
        )
    return periods


def sort_rate_periods(periods: Iterable[RatePeriod]) -> list[RatePeriod]:
    """Sort periods by start and refuse any overlap.

    The engine prices a slot with the first period that covers it, so an
    overlap would let one period silently shadow another.
    """
    ordered = sorted(periods, key=lambda p: p.valid_from)
    for prev, cur in zip(ordered, ordered[1:]):
        if prev.valid_to is None or cur.valid_from < prev.valid_to:
            raise OctopusRatesOverlap(
                "Overlapping rate periods: "
                f"{prev.valid_from.isoformat()} to "
                f"{prev.valid_to.isoformat() if prev.valid_to else 'open'} and "
                f"{cur.valid_from.isoformat()} to "
                f"{cur.valid_to.isoformat() if cur.valid_to else 'open'} "
                "(do two tariff agreements overlap?)"
            )
    return ordered


class OctopusRestClient:
    """Thin network wrapper. Parsing lives in the pure functions above."""

    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._get_url(f"{self.base_url}{path}", params)

    async def _get_url(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        import httpx

        # The product/rate endpoints are public: with no key, send no auth header.
        auth = (self.api_key, "") if self.api_key else None
        async with httpx.AsyncClient(timeout=30, auth=auth) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 400 and _WRONG_REGISTER in resp.text:
                # e.g. "This tariff has day and night rates, not standard."
                return {"results": []}
            resp.raise_for_status()
            payload: dict[str, Any] = resp.json()
            return payload

    async def get_account(self, account_number: str) -> dict:
        return parse_account(await self._get(f"/accounts/{account_number}/"))

    async def get_unit_rates(
        self, product: str, tariff_code: str, period_from: str, period_to: str | None
    ) -> list[RateRecord]:
        """Standard unit rates overlapping the window (``period_to=None``: no upper bound)."""
        return await self._get_rates(product, tariff_code, "standard", period_from, period_to)

    async def get_day_unit_rates(
        self, product: str, tariff_code: str, period_from: str, period_to: str | None
    ) -> list[RateRecord]:
        """Day-register unit rates of a two-register (day/night) tariff."""
        return await self._get_rates(product, tariff_code, "day", period_from, period_to)

    async def get_night_unit_rates(
        self, product: str, tariff_code: str, period_from: str, period_to: str | None
    ) -> list[RateRecord]:
        """Night-register unit rates of a two-register (day/night) tariff."""
        return await self._get_rates(product, tariff_code, "night", period_from, period_to)

    async def _get_rates(
        self, product: str, tariff_code: str, register: str,
        period_from: str, period_to: str | None,
    ) -> list[RateRecord]:
        """One register's unit-rate history, following pagination.

        Returns ``[]`` when the tariff has no such register (Octopus answers
        400 "This tariff has ... rates, not ..." for the wrong register type).
        """
        path = f"/products/{product}/electricity-tariffs/{tariff_code}/{register}-unit-rates/"
        params: dict[str, Any] = {"period_from": period_from, "page_size": RATES_PAGE_SIZE}
        if period_to is not None:
            params["period_to"] = period_to
        payload = await self._get(path, params)
        records = parse_unit_rates(payload)
        pages = 1
        while payload.get("next"):
            pages += 1
            if pages > 100:  # defensive: a cyclic `next` chain must not loop forever
                raise RuntimeError(
                    f"Unit-rate pagination exceeded 100 pages for {path} — "
                    "cyclic 'next' link or runaway window?"
                )
            payload = await self._get_url(payload["next"])
            records.extend(parse_unit_rates(payload))
        records.sort(key=lambda r: r.valid_from)
        return records

    async def get_consumption(
        self, mpan: str, serial: str, period_from: str, period_to: str
    ) -> list[ConsumptionRecord]:
        """Half-hourly whole-house consumption for a meter, following pagination."""
        path = f"/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
        payload = await self._get(
            path,
            {"period_from": period_from, "period_to": period_to,
             "page_size": 1500, "order_by": "period"},
        )
        records = parse_consumption(payload)
        pages = 1
        while payload.get("next"):
            pages += 1
            if pages > 100:  # defensive: a cyclic `next` chain must not loop forever
                raise RuntimeError(
                    f"Consumption pagination exceeded 100 pages for {path} — "
                    "cyclic 'next' link or runaway window?"
                )
            payload = await self._get_url(payload["next"])
            records.extend(parse_consumption(payload))
        records.sort(key=lambda r: r.interval_start)
        return records
