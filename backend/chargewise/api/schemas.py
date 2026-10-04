"""Pydantic response/request schemas for the API."""

from __future__ import annotations

from pydantic import BaseModel


class LifetimeSummary(BaseModel):
    session_count: int
    total_energy_kwh: float
    total_miles: float
    total_cost_gbp: float
    home_cost_gbp: float
    away_cost_gbp: float
    petrol_equiv_gbp: float
    saving_gbp: float
    electric_pence_per_mile: float
    petrol_pence_per_mile: float
    first_date: str | None
    last_date: str | None


class ChargeOut(BaseModel):
    id: int
    start_utc: str
    end_utc: str
    location_type: str
    energy_kwh: float
    cost_gbp: float
    cost_is_estimate: bool
    miles: float | None


class SettingsModel(BaseModel):
    petrol_mpg: float
    fuel_type: str
    away_rate_gbp_per_kwh: float
    exclude_standing_charge: bool
