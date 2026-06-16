"""FastAPI application factory.

Run with: uvicorn chargewise.api.app:create_app --factory
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from fastapi import Depends, FastAPI, Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .. import __version__
from ..config import Settings, get_settings
from ..store import repositories as repo
from ..store.db import init_db, make_engine, make_session_factory
from .auth import require_user
from .schemas import ChargeOut, LifetimeSummary, SettingsModel


def _ensure_sqlite_dir(database_url: str) -> None:
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        path = database_url[len(prefix):]
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or get_settings()
    if engine is None:
        _ensure_sqlite_dir(settings.database_url)
        engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)

    app = FastAPI(title="ChargeWise API", version=__version__)
    app.state.settings = settings
    app.state.session_factory = factory

    def get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/summary/lifetime", response_model=LifetimeSummary)
    def lifetime(db: Session = Depends(get_db), _: str = Depends(require_user)):
        return repo.lifetime_summary(db, settings.petrol_mpg, settings.fuel_type)

    @app.get("/api/charges", response_model=list[ChargeOut])
    def charges(
        location: str | None = None,
        db: Session = Depends(get_db),
        _: str = Depends(require_user),
    ):
        return repo.list_charge_sessions(db, location)

    @app.get("/api/settings", response_model=SettingsModel)
    def read_settings(db: Session = Depends(get_db), _: str = Depends(require_user)):
        return SettingsModel(
            petrol_mpg=float(repo.get_setting(db, "petrol_mpg", str(settings.petrol_mpg))),
            fuel_type=repo.get_setting(db, "fuel_type", settings.fuel_type),
            away_rate_gbp_per_kwh=float(
                repo.get_setting(db, "away_rate_gbp_per_kwh", str(settings.away_rate_gbp_per_kwh))
            ),
            exclude_standing_charge=repo.get_setting(
                db, "exclude_standing_charge", str(settings.exclude_standing_charge)
            ).lower() == "true",
        )

    @app.put("/api/settings", response_model=SettingsModel)
    def update_settings(
        body: SettingsModel,
        db: Session = Depends(get_db),
        _: str = Depends(require_user),
    ):
        repo.set_setting(db, "petrol_mpg", str(body.petrol_mpg))
        repo.set_setting(db, "fuel_type", body.fuel_type)
        repo.set_setting(db, "away_rate_gbp_per_kwh", str(body.away_rate_gbp_per_kwh))
        repo.set_setting(db, "exclude_standing_charge", str(body.exclude_standing_charge))
        return body

    return app
