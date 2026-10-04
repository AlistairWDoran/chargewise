"""Tariff agreements -> rate periods: clipping, day/night tariffs, pagination.

Offline and deterministic. Every fixture is SYNTHETIC (made-up tariff codes,
dates and prices) but modelled on the shapes the Octopus API really returns:

- a flat tariff publishes one long record that can outlive the agreement by
  years;
- Intelligent Octopus Go publishes two time-sliced records per day — off-peak
  23:30-05:30 Europe/London and peak 05:30-23:30 — and a price change dated
  00:00 splits that night's off-peak record in two;
- a two-register tariff publishes nothing under standard-unit-rates, only
  day-unit-rates and night-unit-rates, and a dated future change arrives as an
  open-ended record;
- tariff switches leave zero-length agreements behind.
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from chargewise.engine import (
    ChargeSession,
    LocationType,
    RatePeriod,
    cost_home_session,
    in_core_window,
)
from chargewise.ingest import pipeline
from chargewise.ingest.octopus_rest import (
    OctopusRatesOverlap,
    OctopusRatesUnavailable,
    OctopusRestClient,
    RateRecord,
    TariffAgreement,
    derive_agreement_rate_periods,
    derive_day_night_rate_periods,
    derive_iog_rate_periods,
    sort_rate_periods,
)

UK = ZoneInfo("Europe/London")
UTC = timezone.utc
HALF_HOUR = timedelta(minutes=30)

FLAT = "E-1R-EXAMPLE-FLAT-19-12-01-A"
TOU = "E-1R-EXAMPLE-TOU-20-01-01-A"
TOU_2 = "E-1R-EXAMPLE-TOU-21-01-01-A"
SWITCH = "E-1R-EXAMPLE-VAR-20-01-01-A"
DAY_NIGHT = "E-1R-EXAMPLE-DN-20-01-01-A"


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def local(day: date, hour: int, minute: int = 0) -> datetime:
    """A Europe/London wall-clock time, as UTC (DST-aware)."""
    return datetime.combine(day, time(hour, minute), tzinfo=UK).astimezone(UTC)


def record(start: datetime, end: datetime | None, pence: float) -> RateRecord:
    return RateRecord(start, end, pence, pence)


def tou_records(first: date, last: date, prices) -> list[RateRecord]:
    """IOG-shaped records for the local days ``first..last`` inclusive.

    ``prices(day) -> (offpeak_pence, peak_pence)`` is the price list in force
    on that local day. Each day gets its off-peak record (from 23:30 the
    evening before to 05:30) and its peak record (05:30-23:30); when the
    off-peak price differs from the previous day's, the overnight record is
    split at 00:00, exactly as Octopus publishes a dated price change.
    """
    records: list[RateRecord] = []
    day = first
    while day <= last:
        previous = day - timedelta(days=1)
        offpeak, peak = prices(day)
        old_offpeak = prices(previous)[0]
        evening, midnight, morning = local(previous, 23, 30), local(day, 0), local(day, 5, 30)
        if old_offpeak == offpeak:
            records.append(record(evening, morning, offpeak))
        else:
            records.append(record(evening, midnight, old_offpeak))
            records.append(record(midnight, morning, offpeak))
        records.append(record(morning, local(day, 23, 30), peak))
        day += timedelta(days=1)
    return records


class FakeRates:
    """Stands in for OctopusRestClient: canned records per (tariff, register).

    Like the real API it returns only records overlapping the requested window
    — but NOT clipped to it — and newest first.
    """

    def __init__(self, rates: dict[tuple[str, str], list[RateRecord]]) -> None:
        self.rates = rates
        self.calls: list[tuple[str, str, str, str | None]] = []

    def _get(self, register, product, tariff_code, period_from, period_to):
        assert tariff_code.startswith(f"E-1R-{product}-")
        self.calls.append((tariff_code, register, period_from, period_to))
        lo = datetime.fromisoformat(period_from)
        hi = datetime.fromisoformat(period_to) if period_to else None
        hits = [
            r for r in self.rates.get((tariff_code, register), [])
            if (hi is None or r.valid_from < hi) and (r.valid_to is None or r.valid_to > lo)
        ]
        return sorted(hits, key=lambda r: r.valid_from, reverse=True)

    async def get_unit_rates(self, *window):
        return self._get("standard", *window)

    async def get_day_unit_rates(self, *window):
        return self._get("day", *window)

    async def get_night_unit_rates(self, *window):
        return self._get("night", *window)


def fetch(rest: FakeRates, agreements: list[TariffAgreement]) -> list[RatePeriod]:
    return asyncio.run(pipeline.fetch_rate_periods(rest, agreements))


def engine_price(periods: list[RatePeriod], when: datetime) -> float:
    """What the engine charges at ``when`` with no dispatches; asserts sole cover."""
    covering = [p for p in periods if p.covers(when)]
    assert len(covering) == 1, f"{when.isoformat()} covered by {len(covering)} periods"
    return covering[0].offpeak_inc_vat if in_core_window(when) else covering[0].peak_inc_vat


def published_price(records: list[RateRecord], when: datetime) -> float:
    covering = [
        r for r in records
        if r.valid_from <= when and (r.valid_to is None or when < r.valid_to)
    ]
    assert len(covering) == 1
    return covering[0].value_inc_vat / 100.0


# --------------------------------------------------------------------------- #
# Clipping to the agreement (the shadowing bug).
# --------------------------------------------------------------------------- #

# A 5-day flat tariff whose single rate record runs for years after it ended,
# then a switch (leaving a zero-length agreement) onto a time-of-use tariff.
FLAT_RECORD = record(utc(2019, 12, 1), utc(2024, 6, 1), 34.0)
SHADOW_AGREEMENTS = [
    TariffAgreement(FLAT, utc(2020, 1, 10), utc(2020, 1, 15)),
    TariffAgreement(SWITCH, utc(2020, 1, 15), utc(2020, 1, 15)),
    TariffAgreement(TOU, utc(2020, 1, 15), utc(2020, 2, 1)),
]
SHADOW_RATES = {
    (FLAT, "standard"): [FLAT_RECORD],
    (TOU, "standard"): tou_records(date(2020, 1, 15), date(2020, 2, 1), lambda d: (7.5, 30.0)),
}


def test_long_lived_record_is_clipped_to_its_agreement():
    periods = fetch(FakeRates(SHADOW_RATES), SHADOW_AGREEMENTS)
    assert periods == [
        RatePeriod(utc(2020, 1, 10), utc(2020, 1, 15), 0.34, 0.34),
        RatePeriod(utc(2020, 1, 15), utc(2020, 2, 1), 0.075, 0.30),
    ]


def test_early_flat_tariff_does_not_shadow_the_later_tariff():
    """Regression: the flat record (valid for years) became a period covering
    every later tariff, and the engine prices with the first covering period —
    so years of off-peak charging were billed at the old flat rate."""
    periods = fetch(FakeRates(SHADOW_RATES), SHADOW_AGREEMENTS)
    overnight = ChargeSession(
        utc(2020, 1, 20, 1, 0), utc(2020, 1, 20, 3, 0), 10.0, LocationType.HOME
    )
    assert cost_home_session(overnight, periods, []).total_cost == pytest.approx(10 * 0.075)
    daytime = ChargeSession(
        utc(2020, 1, 20, 12, 0), utc(2020, 1, 20, 13, 0), 10.0, LocationType.HOME
    )
    assert cost_home_session(daytime, periods, []).total_cost == pytest.approx(10 * 0.30)
    # ... while the flat tariff still prices its own five days.
    assert engine_price(periods, utc(2020, 1, 12, 2, 0)) == pytest.approx(0.34)
    assert engine_price(periods, utc(2020, 1, 12, 14, 0)) == pytest.approx(0.34)


def test_no_period_extends_beyond_its_agreement_and_none_overlap():
    periods = fetch(FakeRates(SHADOW_RATES), SHADOW_AGREEMENTS)
    for p in periods:
        owners = [
            a for a in SHADOW_AGREEMENTS
            if a.valid_from <= p.valid_from and p.valid_to is not None
            and a.valid_to is not None and p.valid_to <= a.valid_to
        ]
        assert len(owners) == 1
    for earlier, later in zip(periods, periods[1:]):
        assert earlier.valid_to is not None and earlier.valid_to <= later.valid_from


def test_zero_length_agreements_are_skipped_without_a_fetch():
    rest = FakeRates(SHADOW_RATES)
    fetch(rest, SHADOW_AGREEMENTS)
    assert SWITCH not in {tariff for tariff, *_ in rest.calls}
    # Nothing but zero-length agreements is not an error either.
    assert fetch(FakeRates({}), [SHADOW_AGREEMENTS[1]]) == []


def test_agreement_order_from_the_api_is_not_trusted():
    expected = fetch(FakeRates(SHADOW_RATES), SHADOW_AGREEMENTS)
    assert fetch(FakeRates(SHADOW_RATES), SHADOW_AGREEMENTS[::-1]) == expected


def test_closed_agreement_window_is_passed_and_open_one_is_unbounded():
    """An open agreement is fetched with no upper bound so that rates already
    published for the future (a dated price change) are included."""
    rest = FakeRates({
        (FLAT, "standard"): [FLAT_RECORD],
        (TOU, "standard"): tou_records(date(2020, 1, 15), date(2020, 1, 20), lambda d: (7.5, 30.0)),
    })
    fetch(rest, [
        TariffAgreement(FLAT, utc(2020, 1, 10), utc(2020, 1, 15)),
        TariffAgreement(TOU, utc(2020, 1, 15), None),
    ])
    assert rest.calls == [
        (FLAT, "standard", "2020-01-10T00:00:00+00:00", "2020-01-15T00:00:00+00:00"),
        (TOU, "standard", "2020-01-15T00:00:00+00:00", None),
    ]


def test_overlapping_agreements_fail_loudly():
    rates = {
        (FLAT, "standard"): [FLAT_RECORD],
        (TOU, "standard"): tou_records(date(2020, 1, 10), date(2020, 2, 1), lambda d: (7.5, 30.0)),
    }
    agreements = [
        TariffAgreement(FLAT, utc(2020, 1, 10), utc(2020, 1, 20)),
        TariffAgreement(TOU, utc(2020, 1, 15), utc(2020, 2, 1)),
    ]
    with pytest.raises(OctopusRatesOverlap, match="Overlapping rate periods"):
        fetch(FakeRates(rates), agreements)


def test_sort_rate_periods_sorts_and_rejects_an_open_period_followed_by_another():
    first = RatePeriod(utc(2020, 1, 1), utc(2020, 2, 1), 0.07, 0.30)
    second = RatePeriod(utc(2020, 2, 1), None, 0.08, 0.31)
    assert sort_rate_periods([second, first]) == [first, second]
    with pytest.raises(OctopusRatesOverlap):
        sort_rate_periods([second, RatePeriod(utc(2020, 3, 1), None, 0.09, 0.32)])


# --------------------------------------------------------------------------- #
# Price changes inside an agreement.
# --------------------------------------------------------------------------- #

def changing_prices(day: date) -> tuple[float, float]:
    """Off-peak cut 10.0 -> 7.5 dated 00:00 on 1 Apr 2020; the peak rate moves
    with it (first peak record at 05:30) and again on 10 Apr."""
    offpeak = 10.0 if day < date(2020, 4, 1) else 7.5
    peak = 40.0 if day < date(2020, 4, 1) else (35.0 if day < date(2020, 4, 10) else 30.0)
    return offpeak, peak


# Spans the 29 Mar 2020 clock change, so off-peak records are 22:30Z-04:30Z
# from then on (and that night's record is five hours long).
CHANGE_AGREEMENT = TariffAgreement(TOU, local(date(2020, 3, 20), 0), local(date(2020, 4, 20), 0))
CHANGE_RECORDS = tou_records(date(2020, 3, 20), date(2020, 4, 20), changing_prices)


def test_mid_agreement_price_changes_each_start_a_new_period():
    periods = fetch(FakeRates({(TOU, "standard"): CHANGE_RECORDS}), [CHANGE_AGREEMENT])
    assert periods == [
        RatePeriod(local(date(2020, 3, 20), 0), local(date(2020, 4, 1), 0), 0.10, 0.40),
        # The overnight record is split at 00:00: the off-peak cut applies
        # from midnight, the peak rate only re-prices at 05:30.
        RatePeriod(local(date(2020, 4, 1), 0), local(date(2020, 4, 1), 5, 30), 0.075, 0.40),
        RatePeriod(local(date(2020, 4, 1), 5, 30), local(date(2020, 4, 10), 5, 30), 0.075, 0.35),
        RatePeriod(local(date(2020, 4, 10), 5, 30), local(date(2020, 4, 20), 0), 0.075, 0.30),
    ]


def test_offpeak_record_split_by_a_midnight_price_change_prices_both_halves():
    periods = fetch(FakeRates({(TOU, "standard"): CHANGE_RECORDS}), [CHANGE_AGREEMENT])
    night = date(2020, 3, 31)
    assert engine_price(periods, local(night, 23, 30)) == pytest.approx(0.10)   # old rate
    assert engine_price(periods, local(date(2020, 4, 1), 0)) == pytest.approx(0.075)
    assert engine_price(periods, local(date(2020, 4, 1), 5)) == pytest.approx(0.075)
    assert engine_price(periods, local(date(2020, 4, 1), 5, 30)) == pytest.approx(0.35)
    # One session across the split is billed half at each off-peak rate.
    session = ChargeSession(
        local(night, 23, 30), local(date(2020, 4, 1), 0, 30), 8.0, LocationType.HOME
    )
    result = cost_home_session(session, periods, [])
    assert result.total_cost == pytest.approx(4.0 * 0.10 + 4.0 * 0.075)


def test_every_half_hour_is_priced_at_the_published_rate():
    """The acceptance property, offline: at every half-hour of the agreement
    exactly one period covers the instant and the engine's rate equals the
    published record's — across a clock change and three price changes."""
    periods = fetch(FakeRates({(TOU, "standard"): CHANGE_RECORDS}), [CHANGE_AGREEMENT])
    when, checked = CHANGE_AGREEMENT.valid_from, 0
    while when < CHANGE_AGREEMENT.valid_to:
        assert engine_price(periods, when) == pytest.approx(
            published_price(CHANGE_RECORDS, when), abs=1e-12
        ), when.isoformat()
        when += HALF_HOUR
        checked += 1
    assert checked == 31 * 48 - 2  # the 29 March clock change drops an hour


def test_record_order_and_duplicates_do_not_change_the_result():
    expected = derive_iog_rate_periods(CHANGE_RECORDS)
    shuffled = CHANGE_RECORDS + CHANGE_RECORDS[:40]  # a page boundary repeated
    random.Random(7).shuffle(shuffled)
    assert derive_iog_rate_periods(shuffled) == expected


def test_offpeak_rate_may_rise_above_an_earlier_peak():
    """Records are classified by WHEN they apply, never by comparing values —
    so a price list whose off-peak rate later exceeds an old peak rate (or a
    change inside the first day of the agreement) cannot be mis-assigned."""
    def prices(day: date) -> tuple[float, float]:
        return (5.0, 8.0) if day < date(2020, 6, 2) else (9.0, 50.0)

    records = tou_records(date(2020, 6, 1), date(2020, 6, 5), prices)
    agreement = TariffAgreement(TOU, local(date(2020, 6, 1), 12), local(date(2020, 6, 5), 12))
    periods = derive_agreement_rate_periods(agreement, records)
    assert periods == [
        RatePeriod(local(date(2020, 6, 1), 12), local(date(2020, 6, 2), 0), 0.05, 0.08),
        RatePeriod(local(date(2020, 6, 2), 0), local(date(2020, 6, 2), 5, 30), 0.09, 0.08),
        RatePeriod(local(date(2020, 6, 2), 5, 30), local(date(2020, 6, 5), 12), 0.09, 0.50),
    ]


def test_flat_tariff_price_change_gives_one_flat_period_per_price():
    records = [
        record(utc(2019, 1, 1), utc(2020, 4, 1), 20.0),
        record(utc(2020, 4, 1), None, 25.0),
    ]
    agreement = TariffAgreement(FLAT, utc(2020, 1, 1, 12), None)
    assert derive_agreement_rate_periods(agreement, records) == [
        RatePeriod(utc(2020, 1, 1, 12), utc(2020, 4, 1), 0.20, 0.20),
        RatePeriod(utc(2020, 4, 1), None, 0.25, 0.25),
    ]


def test_gap_in_published_records_stays_a_gap():
    """Missing history is not invented: no period covers the hole, so the
    engine refuses to price a slot inside it instead of guessing."""
    records = [r for r in CHANGE_RECORDS if r.valid_from.date() != date(2020, 4, 5)]
    periods = derive_agreement_rate_periods(CHANGE_AGREEMENT, records)
    hole = utc(2020, 4, 5, 12, 0)
    assert not any(p.covers(hole) for p in periods)
    session = ChargeSession(hole, hole + HALF_HOUR, 1.0, LocationType.HOME)
    with pytest.raises(ValueError, match="No rate period covers"):
        cost_home_session(session, periods, [])


def test_overlapping_records_fail_loudly():
    records = [
        record(utc(2020, 1, 1), utc(2020, 3, 1), 20.0),
        record(utc(2020, 2, 1), utc(2020, 4, 1), 22.0),  # e.g. a second payment method
    ]
    with pytest.raises(OctopusRatesOverlap, match="Overlapping unit-rate records"):
        derive_iog_rate_periods(records)


# --------------------------------------------------------------------------- #
# Two-register (day/night) tariffs.
# --------------------------------------------------------------------------- #

# The current prices started before the agreement; a change dated 15 Oct is
# already published as open-ended records.
DAY_RECORDS = [
    record(utc(2020, 7, 5, 23), utc(2020, 10, 15, 23), 29.0),
    record(utc(2020, 10, 15, 23), None, 36.0),
]
NIGHT_RECORDS = [
    record(utc(2020, 7, 5, 23), utc(2020, 10, 15, 23), 6.5),
    record(utc(2020, 10, 15, 23), None, 6.6),
]
DN_AGREEMENT = TariffAgreement(DAY_NIGHT, utc(2020, 7, 20, 23), None)


def test_day_night_tariff_uses_night_as_offpeak_and_day_as_peak():
    """Regression: a tariff with no standard-unit-rates produced no period at
    all, so every later charge raised "No rate period covers"."""
    rest = FakeRates({(DAY_NIGHT, "day"): DAY_RECORDS, (DAY_NIGHT, "night"): NIGHT_RECORDS})
    periods = fetch(rest, [DN_AGREEMENT])
    assert periods == [
        RatePeriod(utc(2020, 7, 20, 23), utc(2020, 10, 15, 23), 0.065, 0.29),
        RatePeriod(utc(2020, 10, 15, 23), None, 0.066, 0.36),  # open-ended
    ]
    assert [register for _, register, *_ in rest.calls] == ["standard", "day", "night"]

    session = ChargeSession(  # 02:00-03:00 local: the night rate
        utc(2020, 8, 1, 1, 0), utc(2020, 8, 1, 2, 0), 10.0, LocationType.HOME
    )
    assert cost_home_session(session, periods, []).total_cost == pytest.approx(10 * 0.065)
    far_future = ChargeSession(
        utc(2031, 1, 1, 12, 0), utc(2031, 1, 1, 13, 0), 10.0, LocationType.HOME
    )
    assert cost_home_session(far_future, periods, []).total_cost == pytest.approx(10 * 0.36)


def test_day_night_periods_split_whenever_either_rate_changes():
    day = [record(utc(2020, 1, 1), utc(2020, 3, 1), 30.0), record(utc(2020, 3, 1), None, 33.0)]
    night = [record(utc(2020, 1, 1), utc(2020, 2, 1), 8.0), record(utc(2020, 2, 1), None, 9.0)]
    assert derive_day_night_rate_periods(day, night, utc(2020, 1, 10), utc(2020, 4, 1)) == [
        RatePeriod(utc(2020, 1, 10), utc(2020, 2, 1), 0.08, 0.30),
        RatePeriod(utc(2020, 2, 1), utc(2020, 3, 1), 0.09, 0.30),
        RatePeriod(utc(2020, 3, 1), utc(2020, 4, 1), 0.09, 0.33),
    ]


def test_day_night_covers_only_where_both_rates_are_published():
    day = [record(utc(2020, 1, 1), None, 30.0)]
    night = [record(utc(2020, 2, 1), None, 9.0)]
    assert derive_day_night_rate_periods(day, night) == [
        RatePeriod(utc(2020, 2, 1), None, 0.09, 0.30),
    ]
    assert derive_day_night_rate_periods(day, []) == []


# --------------------------------------------------------------------------- #
# Loud failure when an agreement has no rates.
# --------------------------------------------------------------------------- #

def test_agreement_with_no_rates_anywhere_fails_loudly():
    rest = FakeRates({(FLAT, "standard"): [FLAT_RECORD]})
    agreements = [
        TariffAgreement(FLAT, utc(2020, 1, 10), utc(2020, 1, 15)),
        TariffAgreement(TOU_2, utc(2020, 1, 15), utc(2020, 6, 1)),
    ]
    with pytest.raises(OctopusRatesUnavailable) as excinfo:
        fetch(rest, agreements)
    message = str(excinfo.value)
    assert TOU_2 in message
    assert "2020-01-15" in message and "2020-06-01" in message
    assert isinstance(excinfo.value, RuntimeError)
    # Both the standard and the day/night endpoints were tried first.
    assert [r for tariff, r, *_ in rest.calls if tariff == TOU_2] == ["standard", "day", "night"]


def test_open_agreement_with_no_rates_names_it_as_open():
    with pytest.raises(OctopusRatesUnavailable, match=f"{TOU_2} .*to open"):
        fetch(FakeRates({}), [TariffAgreement(TOU_2, utc(2020, 1, 15), None)])


def test_records_entirely_outside_the_agreement_count_as_no_rates():
    stale = [record(utc(2019, 1, 1), utc(2019, 6, 1), 20.0)]
    with pytest.raises(OctopusRatesUnavailable, match=FLAT):
        derive_agreement_rate_periods(
            TariffAgreement(FLAT, utc(2020, 1, 10), utc(2020, 1, 15)), stale
        )


# --------------------------------------------------------------------------- #
# The HTTP client: pagination, public (keyless) access, wrong-register 400.
# --------------------------------------------------------------------------- #

RATES_URL = (
    "https://api.octopus.energy/v1/products/EXAMPLE-TOU-20-01-01/"
    "electricity-tariffs/E-1R-EXAMPLE-TOU-20-01-01-A/standard-unit-rates/"
)


REAL_ASYNC_CLIENT = httpx.AsyncClient


def api_result(r: RateRecord) -> dict:
    def z(value: datetime | None) -> str | None:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None

    return {"value_exc_vat": r.value_exc_vat, "value_inc_vat": r.value_inc_vat,
            "valid_from": z(r.valid_from), "valid_to": z(r.valid_to), "payment_method": None}


def mock_api(monkeypatch, handler) -> list[httpx.Request]:
    """Route OctopusRestClient's HTTP calls to ``handler``; returns the requests seen."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(recording), **kwargs),
    )
    return seen


def test_unit_rates_follow_pagination_across_two_pages(monkeypatch):
    newest_first = sorted(CHANGE_RECORDS, key=lambda r: r.valid_from, reverse=True)
    page_1, page_2 = newest_first[:50], newest_first[50:]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={
                "count": len(newest_first), "next": None,
                "results": [api_result(r) for r in page_2],
            })
        return httpx.Response(200, json={
            "count": len(newest_first), "next": f"{RATES_URL}?page=2&page_size=1500",
            "results": [api_result(r) for r in page_1],
        })

    seen = mock_api(monkeypatch, handler)
    records = asyncio.run(OctopusRestClient("").get_unit_rates(
        "EXAMPLE-TOU-20-01-01", TOU, "2020-03-20T00:00:00+00:00", "2020-04-20T00:00:00+01:00"
    ))
    assert len(seen) == 2
    assert records == sorted(CHANGE_RECORDS, key=lambda r: r.valid_from)  # all, oldest first
    first = seen[0].url
    assert first.path.endswith("/E-1R-EXAMPLE-TOU-20-01-01-A/standard-unit-rates/")
    assert first.params["period_from"] == "2020-03-20T00:00:00+00:00"
    assert first.params["period_to"] == "2020-04-20T00:00:00+01:00"
    assert first.params["page_size"] == "1500"


def test_unit_rates_without_period_to_send_no_upper_bound(monkeypatch):
    seen = mock_api(monkeypatch, lambda request: httpx.Response(200, json={
        "count": 1, "next": None, "results": [api_result(DAY_RECORDS[1])],
    }))
    records = asyncio.run(OctopusRestClient("").get_day_unit_rates(
        "EXAMPLE-DN-20-01-01", DAY_NIGHT, "2020-07-20T23:00:00+00:00", None
    ))
    assert records == [DAY_RECORDS[1]]
    assert seen[0].url.path.endswith("/day-unit-rates/")
    assert "period_to" not in seen[0].url.params


def test_cyclic_next_link_is_cut_off(monkeypatch):
    mock_api(monkeypatch, lambda request: httpx.Response(200, json={
        "count": 1, "next": f"{RATES_URL}?page=2", "results": [api_result(CHANGE_RECORDS[0])],
    }))
    with pytest.raises(RuntimeError, match="pagination exceeded 100 pages"):
        asyncio.run(OctopusRestClient("").get_unit_rates(
            "EXAMPLE-TOU-20-01-01", TOU, "2020-03-20T00:00:00+00:00", None
        ))


def test_no_auth_header_is_sent_without_an_api_key(monkeypatch):
    seen = mock_api(monkeypatch, lambda request: httpx.Response(200, json={
        "count": 0, "next": None, "results": [],
    }))
    asyncio.run(OctopusRestClient("").get_unit_rates(
        "EXAMPLE-TOU-20-01-01", TOU, "2020-03-20T00:00:00+00:00", None
    ))
    assert "authorization" not in seen[0].headers

    asyncio.run(OctopusRestClient("example-key").get_unit_rates(
        "EXAMPLE-TOU-20-01-01", TOU, "2020-03-20T00:00:00+00:00", None
    ))
    assert seen[1].headers["authorization"].startswith("Basic ")


def test_wrong_register_400_means_no_rates_but_other_errors_still_raise(monkeypatch):
    mock_api(monkeypatch, lambda request: httpx.Response(
        400, json={"detail": "This tariff has day and night rates, not standard."}
    ))
    assert asyncio.run(OctopusRestClient("").get_unit_rates(
        "EXAMPLE-DN-20-01-01", DAY_NIGHT, "2020-07-20T23:00:00+00:00", None
    )) == []

    mock_api(monkeypatch, lambda request: httpx.Response(
        400, json={"period_from": ["Must not be greater than `period_to`."]}
    ))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(OctopusRestClient("").get_unit_rates(
            "EXAMPLE-DN-20-01-01", DAY_NIGHT, "2020-07-20T23:00:00+00:00", None
        ))


def test_fetch_octopus_inputs_keeps_its_shape_and_uses_fetch_rate_periods():
    class Rest(FakeRates):
        async def get_account(self, account_number):
            return {"mpan": "0000000000000", "serial_number": "X", "agreements": SHADOW_AGREEMENTS}

    class Gql:
        async def get_completed_dispatches(self, account_number):
            return []

    periods, dispatches = asyncio.run(
        pipeline.fetch_octopus_inputs(Rest(SHADOW_RATES), Gql(), "A-00000000")
    )
    assert periods == fetch(FakeRates(SHADOW_RATES), SHADOW_AGREEMENTS)
    assert dispatches == []
