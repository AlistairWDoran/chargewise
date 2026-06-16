"""SQLAlchemy ORM models. Timestamps stored as ISO-8601 UTC strings."""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Vehicle(Base):
    __tablename__ = "vehicle"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    vin: Mapped[str | None] = mapped_column(String, nullable=True)
    acquired_date: Mapped[str | None] = mapped_column(String, nullable=True)
    disposed_date: Mapped[str | None] = mapped_column(String, nullable=True)


class ChargeSession(Base):
    __tablename__ = "charge_session"
    __table_args__ = (
        UniqueConstraint("vehicle_id", "start_utc", "energy_kwh", name="uq_session"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle.id"))
    start_utc: Mapped[str] = mapped_column(String)
    end_utc: Mapped[str] = mapped_column(String)
    location_type: Mapped[str] = mapped_column(String)   # 'home' | 'away'
    energy_kwh: Mapped[float] = mapped_column(Float)
    cost_gbp: Mapped[float] = mapped_column(Float, default=0.0)
    cost_is_estimate: Mapped[bool] = mapped_column(default=False)
    odometer: Mapped[float | None] = mapped_column(Float, nullable=True)
    miles: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String, default="teslafi_api")


class FuelPriceWeek(Base):
    __tablename__ = "fuel_price_week"

    week_start: Mapped[str] = mapped_column(String, primary_key=True)  # YYYY-MM-DD
    petrol_ppl: Mapped[float] = mapped_column(Float)
    diesel_ppl: Mapped[float] = mapped_column(Float)


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)
