"""Petrol-equivalent cost and savings calculations.

Compares the electric running cost against what the same mileage would have cost
in a petrol car, using GOV.UK weekly average petrol prices. See METHODOLOGY.md.
"""

from __future__ import annotations

from dataclasses import dataclass

LITRES_PER_UK_GALLON = 4.54609

DEFAULT_MPG = 30.0  # Alistair's prior petrol car averaged ~29–32 mpg.


def petrol_cost_gbp(miles: float, mpg: float, pence_per_litre: float) -> float:
    """Cost in GBP to cover `miles` in a petrol car at the given mpg and fuel price."""
    if mpg <= 0:
        raise ValueError("mpg must be positive")
    gallons = miles / mpg
    litres = gallons * LITRES_PER_UK_GALLON
    return litres * pence_per_litre / 100.0


def pence_per_mile(cost_gbp: float, miles: float) -> float:
    if miles <= 0:
        return 0.0
    return cost_gbp / miles * 100.0


@dataclass(frozen=True)
class SavingsResult:
    miles: float
    electric_cost_gbp: float
    petrol_cost_gbp: float
    saving_gbp: float
    electric_pence_per_mile: float
    petrol_pence_per_mile: float


def compute_savings(
    miles: float,
    electric_cost_gbp: float,
    pence_per_litre: float,
    mpg: float = DEFAULT_MPG,
) -> SavingsResult:
    petrol = petrol_cost_gbp(miles, mpg, pence_per_litre)
    return SavingsResult(
        miles=round(miles, 2),
        electric_cost_gbp=round(electric_cost_gbp, 2),
        petrol_cost_gbp=round(petrol, 2),
        saving_gbp=round(petrol - electric_cost_gbp, 2),
        electric_pence_per_mile=round(pence_per_mile(electric_cost_gbp, miles), 2),
        petrol_pence_per_mile=round(pence_per_mile(petrol, miles), 2),
    )
