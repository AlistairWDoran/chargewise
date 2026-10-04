"""Golden reconciliation test — the project's accuracy gate.

METHODOLOGY.md §7 / DELIVERY-PLAN.md §7: the computed cost for a test month
must reconcile to within ±2% (hard CI gate; target ~1–2%) of the real Octopus
bill. This test prices a snapshot month through the REAL engine (no pricing
logic is re-implemented here), re-totals it billing-style per METHODOLOGY §6,
and compares against the bill figure recorded in the fixture. Two modes:

* ``sessions`` (default) — charging sessions priced via ``cost_home_session``
  then ``reconcile_sessions``; compared to the bill's home-charging portion.
* ``whole_house_consumption`` — half-hourly WHOLE-HOUSE meter consumption,
  each slot priced with the engine's ``rate_for_slot`` selection inside
  ``reconcile_consumption``; compared to the bill's total ENERGY cost and kWh
  (both EXCLUDING the standing charge). Preferred: on IOG the off-peak rate
  applies to the whole home in the core window and dispatch slots, so the
  whole bill reconciles and only two numbers are needed from the bill.

Fixture files live in ``tests/fixtures/golden/*.json`` and are self-contained
(no network, no DB). A real-bill fixture (``*.real.json``) is git-ignored and
kept locally; it is priced here whenever it is present. Schema (schema_version 2; v1 files have no ``mode``/
``consumption`` and default to sessions mode):

    mode              "sessions" (default) | "whole_house_consumption"
    status            "synthetic" | "real" | "placeholder" (placeholder => SKIP)
    month             "YYYY-MM" (informational)
    tariff            {name, tariff_code, region} (informational)
    rate_periods      [{valid_from, valid_to|null, offpeak_inc_vat, peak_inc_vat}]  GBP/kWh
    dispatches        [{start, end, location}]        ISO-8601, tz-aware
    sessions          [{start, end, energy_kwh, vehicle}]  ISO-8601 UTC (sessions mode)
    consumption       [{start, kwh}]  one entry per half-hour settlement slot,
                      ISO-8601 tz-aware UTC (whole_house_consumption mode)
    bill              {month, billed_kwh, billed_cost_inc_vat_gbp,
                       billed_cost_exc_vat_gbp|null, tolerance_pct}
                      sessions mode: the HOME-CHARGING portion of the bill;
                      whole-house mode: the bill's energy total EXCLUDING the
                      standing charge.

A missing/placeholder real-bill fixture SKIPS with an explicit reason — it can
never silently pass. Generate the real fixture on the LAN with
``scripts/export-golden-fixture.py`` (see its --help).

KNOWN CAVEAT (METHODOLOGY §8): Octopus exposes only a recent window of
smart-charge dispatches. For historical months, daytime smart-charge slots
therefore price at peak instead of off-peak, so the computed cost can EXCEED
the billed cost — when that happens the delta direction is positive.

Hand-computed arithmetic for ``2026-06.synthetic.json`` (published IOG
rates: off-peak £0.069/kWh, peak £0.303714/kWh inc VAT; VAT 5%):

* Session A — 8.0 kWh, 23:00–01:00 BST 10–11 Jun (22:00–00:00Z): four equal
  half-hour slots of 2.0 kWh. The 23:00 slot is before the 23:30 core start
  => peak; 23:30/00:00/00:30 => core off-peak.
* Session B — 6.0 kWh, 13:00–14:00 BST 15 Jun: both slots covered by AT_HOME
  dispatches => off-peak, 3.0 kWh each.
* Session C — 4.5 kWh, 02:15–03:45 BST 20 Jun: partial-slot apportionment
  0.75 / 1.5 / 1.5 / 0.75 kWh, all inside the core window => off-peak.

Totals: 2.0 kWh peak + 16.5 kWh off-peak = 18.5 kWh. Every half-hour energy is
already exact at 2 d.p., so the §6 rounding is the identity here (the rounding
behaviour itself is unit-tested below). Expected bill:
  exc VAT = (2.0×0.303714 + 16.5×0.069) / 1.05 = 1.745928 / 1.05 ≈ £1.662789
  inc VAT = exc × 1.05 = 2.0×0.303714 + 16.5×0.069 = 0.607428 + 1.1385
          = £1.745928  -> billed_cost_inc_vat_gbp

Hand-computed arithmetic for ``2026-06.whole-house.synthetic.json`` (same
rates; six whole-house half-hour meter slots on 2026-06-15, BST = UTC+1):

  02:00Z = 03:00 BST  1.25  kWh  core off-peak      -> 1.25
  04:00Z = 05:00 BST  0.80  kWh  core off-peak      -> 0.80
  04:30Z = 05:30 BST  0.40  kWh  core ended => peak -> 0.40
  12:00Z = 13:00 BST  2.375 kWh  dispatch => off-pk -> 2.38 (half-even UP, 7 odd)
  17:00Z = 18:00 BST  0.605 kWh  peak               -> 0.60 (half-even DOWN, 0 even)
  22:30Z = 23:30 BST  1.0   kWh  core off-peak      -> 1.00

Off-peak 1.25+0.80+2.38+1.00 = 5.43 kWh; peak 0.40+0.60 = 1.00 kWh;
billed_kwh = 6.43 (raw sum is also 6.43 — the two rounding deltas cancel).
  exc VAT = (5.43×0.069 + 1.00×0.303714) / 1.05 = 0.678384 / 1.05 = £0.646080
  inc VAT = exc × 1.05 = £0.678384  -> billed_cost_inc_vat_gbp
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chargewise.engine import (
    ChargeSession,
    Dispatch,
    LocationType,
    RatePeriod,
    RateSource,
    SessionCost,
    SlotCost,
    cost_home_session,
    reconcile_consumption,
    reconcile_sessions,
)

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"
UTC = ZoneInfo("UTC")

_fixture_files = sorted(GOLDEN_DIR.glob("*.json"))


def _dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    assert dt.tzinfo is not None, f"golden fixture datetimes must be tz-aware: {value}"
    return dt


# --- the accuracy gate ----------------------------------------------------

@pytest.mark.parametrize(
    "fixture_path",
    _fixture_files or [None],
    ids=[p.stem for p in _fixture_files] or ["no-fixtures"],
)
def test_golden_reconciliation(fixture_path: Path | None) -> None:
    if fixture_path is None:
        pytest.skip(
            "No golden fixtures found in tests/fixtures/golden/ — "
            "run scripts/export-golden-fixture.py on the LAN to create one."
        )
    data = json.loads(fixture_path.read_text())

    if data.get("status") == "placeholder":
        pytest.skip(
            f"Golden fixture {fixture_path.name} is a placeholder — awaiting the real "
            "Octopus bill figures (billed kWh and billed energy cost, both EXCLUDING "
            "the standing charge). Generate it with scripts/export-golden-fixture.py "
            "on the LAN (see that script's --help)."
        )
    bill = data.get("bill") or {}
    if bill.get("billed_cost_inc_vat_gbp") is None:
        pytest.skip(
            f"Golden fixture {fixture_path.name} has no billed_cost_inc_vat_gbp — "
            "fill in the bill figures, EXCLUDING the standing charge (or re-run "
            "scripts/export-golden-fixture.py with --billed-kwh/--billed-cost-inc-vat) "
            "before this gate can run."
        )

    rates = [
        RatePeriod(
            valid_from=_dt(r["valid_from"]),
            valid_to=_dt(r["valid_to"]) if r["valid_to"] else None,
            offpeak_inc_vat=r["offpeak_inc_vat"],
            peak_inc_vat=r["peak_inc_vat"],
        )
        for r in data["rate_periods"]
    ]
    dispatches = [
        Dispatch(_dt(d["start"]), _dt(d["end"]), d.get("location", "unknown"))
        for d in data["dispatches"]
    ]
    mode = data.get("mode", "sessions")
    if mode == "whole_house_consumption":
        half_hours = [(_dt(c["start"]), c["kwh"]) for c in data["consumption"]]
        assert half_hours, f"{fixture_path.name}: non-placeholder fixture has no consumption"
        # REAL engine rate selection (rate_for_slot) + METHODOLOGY §6 re-total.
        recon = reconcile_consumption(half_hours, rates, dispatches)
    else:
        sessions = [
            ChargeSession(_dt(s["start"]), _dt(s["end"]), s["energy_kwh"], LocationType.HOME)
            for s in data["sessions"]
        ]
        assert sessions, f"{fixture_path.name}: non-placeholder fixture has no sessions"

        # REAL engine code path — rate selection/slotting happens only in the engine.
        costs = [cost_home_session(s, rates, dispatches) for s in sessions]
        # METHODOLOGY §6 billing-grade re-total.
        recon = reconcile_sessions(costs)

    billed = float(bill["billed_cost_inc_vat_gbp"])
    tolerance_pct = float(bill.get("tolerance_pct") or 2.0)
    delta_pct = (recon.cost_inc_vat_gbp - billed) / billed * 100.0

    report = (
        f"Golden reconciliation [{fixture_path.name}] month={bill.get('month')}: "
        f"computed £{recon.cost_inc_vat_gbp:.4f} vs billed £{billed:.4f} "
        f"-> delta {delta_pct:+.3f}% (tolerance ±{tolerance_pct}%)"
    )
    if bill.get("billed_kwh") is not None:
        kwh_delta = (recon.energy_kwh - float(bill["billed_kwh"])) / float(bill["billed_kwh"]) * 100
        report += f"; kWh computed {recon.energy_kwh:.2f} vs billed {bill['billed_kwh']} ({kwh_delta:+.2f}%)"
    print("\n" + report)

    assert abs(delta_pct) <= tolerance_pct, report


# --- §6 reconciliation unit tests (the rounding rules themselves) ---------

def _slot(hh: int, mm: int, energy: float, rate: float) -> SlotCost:
    start = datetime(2026, 6, 1, hh, mm, tzinfo=UTC)
    return SlotCost(start, start, energy, rate, RateSource.CORE, energy * rate)


def _as_session_cost(slots: list[SlotCost]) -> SessionCost:
    s = ChargeSession(slots[0].slot_start, slots[-1].slot_end, 1.0, LocationType.HOME)
    return SessionCost(s, sum(sc.cost for sc in slots), is_estimate=False, slots=slots)


def test_reconcile_rounds_half_to_even():
    # inc-VAT rate 0.105 => ex-VAT exactly 0.10. 1.005 -> 1.00, 1.015 -> 1.02
    # (Octopus "unbiased" rounding: ties go to the even 2nd decimal).
    sc = _as_session_cost([_slot(0, 0, 1.005, 0.105), _slot(0, 30, 1.015, 0.105)])
    recon = reconcile_sessions([sc])
    assert recon.energy_kwh == pytest.approx(1.00 + 1.02)
    assert recon.cost_exc_vat_gbp == pytest.approx((1.00 + 1.02) * 0.10)
    assert recon.cost_inc_vat_gbp == pytest.approx((1.00 + 1.02) * 0.105)


def test_reconcile_groups_energy_per_half_hour_before_rounding():
    # Two sessions touching the SAME half-hour slot: the meter registers one
    # 0.014 kWh figure -> rounds to 0.01, not 0.01 + 0.01 rounded separately.
    a = _as_session_cost([_slot(10, 0, 0.007, 1.05)])
    b = _as_session_cost([_slot(10, 10, 0.007, 1.05)])
    recon = reconcile_sessions([a, b])
    assert recon.energy_kwh == pytest.approx(0.01)
    assert recon.cost_exc_vat_gbp == pytest.approx(0.01 * 1.0)
    assert recon.cost_inc_vat_gbp == pytest.approx(0.01 * 1.05)


def test_reconcile_vat_added_at_the_end():
    sc = _as_session_cost([_slot(2, 0, 2.0, 0.069)])
    recon = reconcile_sessions([sc])
    assert recon.cost_inc_vat_gbp == pytest.approx(recon.cost_exc_vat_gbp * 1.05)
    assert recon.cost_inc_vat_gbp == pytest.approx(2.0 * 0.069)
