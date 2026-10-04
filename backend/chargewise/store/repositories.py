"""Repository functions — the only place that touches the ORM directly.

Includes idempotent upserts and the summary aggregation that powers the API
(and therefore the HA + standalone dashboards).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..engine.models import Dispatch
from ..engine.savings import pence_per_mile, petrol_cost_gbp
from ..ingest.fuel_prices import FuelPriceWeek as FuelWeek
from ..ingest.fuel_prices import price_for_date
from .models import ChargeSession, FuelPriceWeek, Setting, StoredDispatch, Vehicle

#: Data sources tracked by ``record_sync`` / ``sync_status``.
SYNC_SOURCES = ("teslafi", "octopus", "fuel")

#: The job runs daily, so a source with no success for this long has missed a run.
STALE_AFTER = timedelta(hours=36)

#: Stored values an upsert must never blank: a re-ingest that cannot derive
#: them (e.g. the first session of a fetch window) must not erase what is held.
_KEEP_IF_NONE = ("odometer", "miles")


def get_or_create_vehicle(db: Session, name: str, *, rename: bool = False, **kw) -> Vehicle:
    """Find a vehicle by VIN when one is given, otherwise by name; create it if absent.

    The VIN is the vehicle's identity; the name is only a label.

    * **With a VIN** (``vin=...``): the stored vehicle with that VIN (compared
      trimmed of spaces, tabs and line ends, and without regard to case; the
      oldest row if there are several) is returned, whatever it is called. It
      keeps its stored name unless ``rename`` is true, which a caller sets when
      it has an explicit name for that VIN. If no vehicle has the VIN, a vehicle called ``name`` that has
      no VIN yet is taken to be the same car and adopts the VIN. A vehicle
      called ``name`` with a *different* VIN is a different car and is left
      alone.
    * **Without a VIN**: the oldest vehicle called ``name`` is returned.

    Only when neither finds a vehicle is a new one created, and its VIN is
    stored trimmed.

    What this guarantees: a vehicle stored with its VIN is found again by that
    VIN whatever name comes with it, so a missing or changed name does not
    start a second vehicle for it. What it does not cover: a vehicle stored
    *without* a VIN (for example from a CSV import) that later arrives with a
    VIN under a different name is not recognised, and becomes a second
    vehicle.
    """
    raw_vin = kw.get("vin")
    vin = raw_vin.strip() if isinstance(raw_vin, str) else ""
    if isinstance(raw_vin, str):
        kw = {**kw, "vin": vin}
    stored_vin = func.trim(func.coalesce(Vehicle.vin, ""), " \t\r\n")
    if vin:
        v = db.scalar(
            select(Vehicle)
            .where(func.upper(stored_vin) == vin.upper())
            .order_by(Vehicle.id)
            .limit(1)
        )
        if v is not None:
            if rename and v.name != name:
                v.name = name
                db.commit()
            return v
        v = db.scalar(
            select(Vehicle)
            .where(Vehicle.name == name, stored_vin == "")
            .order_by(Vehicle.id)
            .limit(1)
        )
        if v is not None:
            v.vin = vin
            db.commit()
            return v
    else:
        v = db.scalar(select(Vehicle).where(Vehicle.name == name).order_by(Vehicle.id).limit(1))
        if v is not None:
            return v
    v = Vehicle(name=name, **kw)
    db.add(v)
    db.commit()
    return v


def upsert_charge_session(db: Session, **fields) -> ChargeSession:
    """Insert a charge session unless one with the same (vehicle, start) already
    exists — making re-ingestion idempotent.

    Energy is not part of the identity: a source can re-serve a charge with a
    slightly different energy figure, which must update the row rather than add
    a second one (double-counting the totals). A stored odometer or miles value
    is never replaced by ``None``.
    """
    matches = db.scalars(
        select(ChargeSession)
        .where(
            ChargeSession.vehicle_id == fields["vehicle_id"],
            ChargeSession.start_utc == fields["start_utc"],
        )
        .order_by(ChargeSession.id)
    ).all()
    # Should older duplicate rows exist, take the exact-energy one so updating
    # energy_kwh cannot collide with the table's (vehicle, start, energy) key.
    existing = next(
        (m for m in matches if m.energy_kwh == fields["energy_kwh"]),
        matches[0] if matches else None,
    )
    if existing is not None:
        # Refresh derived fields so a re-run re-costs sessions (e.g. after a
        # rate fix or new dispatch data) without duplicating rows.
        for key in ("end_utc", "location_type", "energy_kwh", "cost_gbp",
                    "cost_is_estimate", "odometer", "miles", "source"):
            if key not in fields or (key in _KEEP_IF_NONE and fields[key] is None):
                continue
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


def latest_odometer_before(db: Session, vehicle_id: int, before: datetime) -> float | None:
    """The vehicle's most recent stored odometer reading from a session that
    started before ``before`` (``None`` if it has none).

    Lets a partial re-ingest work out the miles of its first session, whose
    previous charge lies outside the fetched window. Starts are compared as
    datetimes, not strings, so mixed UTC offsets cannot mis-order them.
    """
    rows = db.execute(
        select(ChargeSession.start_utc, ChargeSession.odometer).where(
            ChargeSession.vehicle_id == vehicle_id,
            ChargeSession.odometer.is_not(None),
        )
    ).all()
    latest: tuple[datetime, float] | None = None
    for start_utc, odometer in rows:
        start = datetime.fromisoformat(start_utc)
        if start < before and (latest is None or start > latest[0]):
            latest = (start, odometer)
    return latest[1] if latest else None


def _utc_iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def upsert_dispatches(
    db: Session, dispatches: list[Dispatch], seen_at: str | None = None
) -> int:
    """Store smart-charge dispatches not already held; returns how many were new.

    Idempotent on (start, end), both normalised to UTC. Nothing is ever removed:
    Octopus stops serving a dispatch after a while, and the stored copy is then
    the only record that the slot was billed off-peak.
    """
    stamp = seen_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    known = {(r.start_utc, r.end_utc): r for r in db.scalars(select(StoredDispatch))}
    added = 0
    for d in dispatches:
        key = (_utc_iso(d.start), _utc_iso(d.end))
        row = known.get(key)
        if row is None:
            row = StoredDispatch(start_utc=key[0], end_utc=key[1],
                                 location=d.location, first_seen_utc=stamp)
            db.add(row)
            known[key] = row
            added += 1
        elif row.location == "unknown" and d.location != "unknown":
            row.location = d.location
    db.commit()
    return added


def list_dispatches(db: Session) -> list[Dispatch]:
    """Every stored dispatch as an engine ``Dispatch``, oldest first."""
    rows = db.scalars(select(StoredDispatch).order_by(StoredDispatch.start_utc))
    return [
        Dispatch(datetime.fromisoformat(r.start_utc), datetime.fromisoformat(r.end_utc),
                 r.location)
        for r in rows
    ]


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


def record_sync(db: Session, source: str, when: str | None = None) -> None:
    """Record the last successful ingest for a data source (teslafi/octopus/fuel).

    A "sync" means the source was fetched and stored without error — distinct
    from data freshness (TeslaFi can sync fine while its own feed is stalled;
    sync_status exposes both signals).
    """
    stamp = when or datetime.now(timezone.utc).isoformat(timespec="seconds")
    set_setting(db, f"sync:{source}", stamp)
    if get_setting(db, f"sync_error:{source}"):
        set_setting(db, f"sync_error:{source}", "")  # a success clears the failure


def record_sync_attempt(db: Session, source: str, when: str | None = None) -> None:
    """Record that an ingest of ``source`` is starting (it may yet fail)."""
    stamp = when or datetime.now(timezone.utc).isoformat(timespec="seconds")
    set_setting(db, f"sync_attempt:{source}", stamp)


def record_sync_error(db: Session, source: str, message: str) -> None:
    """Record why the latest ingest of ``source`` failed; cleared by ``record_sync``."""
    set_setting(db, f"sync_error:{source}", message)


def _is_stale(stamp: str | None, now: datetime) -> bool:
    """True if ``stamp`` (an ISO last-success time) is missing or too old."""
    if not stamp:
        return True
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return now - when > STALE_AFTER


def sync_status(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Per-source sync outcome plus data-freshness markers.

    Each source reports its last success, last attempt and last error (``None``
    unless the most recent attempt failed). ``healthy`` is False when any source
    has an error or has not succeeded within ``STALE_AFTER`` — which also
    catches a job that has stopped running altogether.
    """
    now = now or datetime.now(timezone.utc)
    latest_charge = db.scalar(select(func.max(ChargeSession.start_utc)))
    latest_fuel_week = db.scalar(select(func.max(FuelPriceWeek.week_start)))
    status: dict[str, Any] = {
        "teslafi": {
            "last_success_utc": get_setting(db, "sync:teslafi"),
            "latest_charge_utc": latest_charge,
        },
        "octopus": {
            "last_success_utc": get_setting(db, "sync:octopus"),
        },
        "fuel": {
            "last_success_utc": get_setting(db, "sync:fuel"),
            "latest_week": latest_fuel_week,
        },
    }
    healthy = True
    for source in SYNC_SOURCES:
        entry = status[source]
        entry["last_attempt_utc"] = get_setting(db, f"sync_attempt:{source}")
        entry["last_error"] = get_setting(db, f"sync_error:{source}") or None
        if entry["last_error"] or _is_stale(entry["last_success_utc"], now):
            healthy = False
    status["healthy"] = healthy
    return status


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
