"""Repository functions — the only place that touches the ORM directly.

Includes idempotent upserts and the summary aggregation that powers the API
(and therefore the HA + standalone dashboards).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..engine.savings import pence_per_mile, petrol_cost_gbp
from ..ingest.fuel_prices import FuelPriceWeek as FuelWeek
from ..ingest.fuel_prices import price_for_date
from .models import ChargeSession, FuelPriceWeek, Setting, Vehicle


def get_or_create_vehicle(db: Session, name: str, **kw) -> Vehicle:
    v = db.scalar(select(Vehicle).where(Vehicle.name == name))
    if v is None:
        v = Vehicle(name=name, **kw)
        db.add(v)
        db.commit()
    return v


def upsert_charge_session(db: Session, **fields) -> ChargeSession:
    """Insert a charge session unless one with the same (vehicle, start, energy)
    already exists — making re-ingestion idempotent."""
    existing = db.scalar(
        select(ChargeSession).where(
            ChargeSession.vehicle_id == fields["vehicle_id"],
            ChargeSession.start_utc == fields["start_utc"],
            ChargeSession.energy_kwh == fields["energy_kwh"],
        )
    )
    if existing is not None:
        # Refresh derived fields so a re-run re-costs sessions (e.g. after a
        # rate fix or new dispatch data) without duplicating rows.
        for key in ("end_utc", "location_type", "cost_gbp", "cost_is_estimate",
                    "odometer", "miles", "source"):
            if key in fields:
                setattr(existing, key, fields[key])
        db.commit()
        return existing
    row = ChargeSession(**fields)
    db.add(row)
    db.commit()
    return row


def list_charge_sessions(db: Session, location: str | None = None) -> list[ChargeSession]:
    stmt = select(ChargeSession).order_by(ChargeSession.start_utc)
    if location:
        stmt = stmt.where(ChargeSession.location_type == location)
    return list(db.scalars(stmt))


def upsert_fuel_weeks(db: Session, weeks: list[FuelWeek]) -> int:
    n = 0
    for w in weeks:
        key = w.week_start.isoformat()
        row = db.get(FuelPriceWeek, key)
        if row is None:
            db.add(FuelPriceWeek(week_start=key, petrol_ppl=w.petrol_ppl,
                                 diesel_ppl=w.diesel_ppl))
            n += 1
        else:
            row.petrol_ppl, row.diesel_ppl = w.petrol_ppl, w.diesel_ppl
    db.commit()
    return n


def _load_fuel_weeks(db: Session) -> list[FuelWeek]:
    rows = db.scalars(select(FuelPriceWeek)).all()
    return sorted(
        (FuelWeek(date.fromisoformat(r.week_start), r.petrol_ppl, r.diesel_ppl) for r in rows),
        key=lambda w: w.week_start,
    )


def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def lifetime_summary(db: Session, mpg: float, fuel_type: str = "petrol") -> dict:
    """Aggregate lifetime cost, mileage and savings vs petrol across all vehicles."""
    sessions = list_charge_sessions(db)
    fuel_weeks = _load_fuel_weeks(db)

    total_cost = sum(s.cost_gbp for s in sessions)
    total_energy = sum(s.energy_kwh for s in sessions)
    total_miles = sum(s.miles or 0.0 for s in sessions)
    home_cost = sum(s.cost_gbp for s in sessions if s.location_type == "home")
    away_cost = total_cost - home_cost

    petrol_equiv = 0.0
    for s in sessions:
        if not s.miles:
            continue
        day = datetime.fromisoformat(s.start_utc).date()
        ppl = price_for_date(fuel_weeks, day, fuel_type)
        if ppl is not None:
            petrol_equiv += petrol_cost_gbp(s.miles, mpg, ppl)

    dates = [datetime.fromisoformat(s.start_utc).date() for s in sessions]
    return {
        "session_count": len(sessions),
        "total_energy_kwh": round(total_energy, 2),
        "total_miles": round(total_miles, 1),
        "total_cost_gbp": round(total_cost, 2),
        "home_cost_gbp": round(home_cost, 2),
        "away_cost_gbp": round(away_cost, 2),
        "petrol_equiv_gbp": round(petrol_equiv, 2),
        "saving_gbp": round(petrol_equiv - total_cost, 2),
        "electric_pence_per_mile": round(pence_per_mile(total_cost, total_miles), 2),
        "petrol_pence_per_mile": round(pence_per_mile(petrol_equiv, total_miles), 2),
        "first_date": min(dates).isoformat() if dates else None,
        "last_date": max(dates).isoformat() if dates else None,
    }
