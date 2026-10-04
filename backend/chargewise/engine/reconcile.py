"""Octopus-bill reconciliation for home-charging costs (METHODOLOGY.md §6).

Octopus bills are computed from half-hourly meter consumption rounded to
0.01 kWh with "unbiased" rounding (round-half-to-even), priced ex-VAT per
half-hour line, summed, with VAT (5%) added at the end. To reconcile the
engine's session costs against a real bill we must replay that arithmetic, not
just round the final total. (Note: ``SlotCost.energy_kwh`` arrives already
rounded to 4 d.p. by the engine (``cost.py``), so the 0.01 kWh rounding here
is a second rounding of a 4 d.p. value — immaterial at the ±2% gate.)

This module is pure and reconciles along two paths:

* :func:`reconcile_sessions` — consumes the ``SessionCost``/``SlotCost``
  output of :func:`chargewise.engine.cost.cost_home_session` (charging
  sessions only).
* :func:`reconcile_consumption` — consumes half-hourly WHOLE-HOUSE meter
  consumption (the Octopus consumption endpoint). On IOG the off-peak rate
  applies to the whole home during the core window and smart-dispatch slots,
  so every half-hour is priced with the engine's own
  :func:`chargewise.engine.cost.rate_for_slot` selection rules.

Either way, rate selection stays in one place — the engine — and this module
only re-applies the billing-grade rounding.

``RatePeriod`` carries VAT-inclusive rates (what the engine prices with), so
the ex-VAT unit rate is recovered as ``inc_vat / 1.05`` before the per-slot
lines are summed and VAT is re-applied at the end, mirroring the bill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from .cost import half_hour_floor, rate_for_slot
from .models import Dispatch, RatePeriod, SessionCost

#: UK domestic electricity VAT rate.
VAT_RATE = Decimal("0.05")

_TWO_DP = Decimal("0.01")


@dataclass(frozen=True)
class ReconciledBill:
    """Billing-grade totals for a set of home sessions (see METHODOLOGY §6)."""

    energy_kwh: float        #: half-hourly consumption rounded to 0.01 kWh, summed
    cost_exc_vat_gbp: float  #: sum of per-half-hour lines at ex-VAT rates
    cost_inc_vat_gbp: float  #: cost_exc_vat_gbp with VAT added at the end


def _total(
    by_slot: dict[tuple[datetime, Decimal], Decimal], vat_rate: Decimal
) -> ReconciledBill:
    """§6 totalling: round each half-hour to 0.01 kWh (half-even), price the
    line at the ex-VAT rate (``inc_vat / (1+VAT)``), sum, add VAT at the end."""
    one_plus_vat = Decimal(1) + vat_rate
    total_kwh = Decimal(0)
    total_exc = Decimal(0)
    for (_, rate_inc_vat), energy in by_slot.items():
        rounded_kwh = energy.quantize(_TWO_DP, rounding=ROUND_HALF_EVEN)
        total_kwh += rounded_kwh
        total_exc += rounded_kwh * (rate_inc_vat / one_plus_vat)

    return ReconciledBill(
        energy_kwh=float(total_kwh),
        cost_exc_vat_gbp=float(total_exc),
        cost_inc_vat_gbp=float(total_exc * one_plus_vat),
    )


def reconcile_sessions(
    session_costs: list[SessionCost], vat_rate: Decimal = VAT_RATE
) -> ReconciledBill:
    """Re-total engine session costs the way an Octopus bill would.

    1. Group slot energy by half-hour settlement slot (the meter registers one
       consumption figure per half hour, however many sessions touch it).
    2. Round each half-hour's kWh to 0.01 with round-half-to-even.
    3. Price each half-hour at the ex-VAT unit rate (``inc_vat / (1+VAT)``).
    4. Sum the lines, then add VAT at the end.
    """
    # (settlement-slot start, inc-VAT unit rate) -> summed kWh. The rate is part
    # of the key defensively; slots sharing a start always share a rate today.
    by_slot: dict[tuple[datetime, Decimal], Decimal] = {}
    for sc in session_costs:
        for slot in sc.slots:
            key = (half_hour_floor(slot.slot_start), Decimal(str(slot.unit_rate)))
            by_slot[key] = by_slot.get(key, Decimal(0)) + Decimal(str(slot.energy_kwh))

    return _total(by_slot, vat_rate)


def reconcile_consumption(
    half_hours: list[tuple[datetime, float]],
    rates: list[RatePeriod],
    dispatches: list[Dispatch],
    vat_rate: Decimal = VAT_RATE,
) -> ReconciledBill:
    """Reconcile half-hourly WHOLE-HOUSE meter consumption against a bill.

    ``half_hours`` is ``[(slot_start, kwh), ...]`` — one entry per half-hour
    settlement slot as served by the Octopus consumption endpoint. Each slot's
    rate is chosen by the engine's :func:`~chargewise.engine.cost.rate_for_slot`
    (core window / smart dispatch / standard — METHODOLOGY §2), then the §6
    billing rounding is applied exactly as for sessions.

    Caveat (METHODOLOGY §8): Octopus only exposes recent smart-charge
    dispatches, so for historical months daytime dispatch slots are priced at
    peak — the computed cost can then EXCEED the bill (positive delta).
    """
    by_slot: dict[tuple[datetime, Decimal], Decimal] = {}
    for slot_start, kwh in half_hours:
        rate, _source = rate_for_slot(slot_start, rates, dispatches)
        key = (half_hour_floor(slot_start), Decimal(str(rate)))
        by_slot[key] = by_slot.get(key, Decimal(0)) + Decimal(str(kwh))

    return _total(by_slot, vat_rate)
