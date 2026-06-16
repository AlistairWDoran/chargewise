"""API tests using FastAPI's TestClient against an in-memory database."""

from __future__ import annotations

from fastapi.testclient import TestClient

from chargewise.api.app import create_app
from chargewise.config import Settings
from chargewise.store.db import make_engine


def make_client(auth_disabled: bool = True) -> TestClient:
    settings = Settings(auth_disabled=auth_disabled)
    engine = make_engine("sqlite://")
    return TestClient(create_app(settings=settings, engine=engine))


def test_health():
    r = make_client().get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_lifetime_empty_summary():
    r = make_client().get("/api/summary/lifetime")
    assert r.status_code == 200
    assert r.json()["session_count"] == 0
    assert r.json()["saving_gbp"] == 0.0


def test_auth_required_when_enabled():
    r = make_client(auth_disabled=False).get("/api/summary/lifetime")
    assert r.status_code == 401


def test_settings_roundtrip():
    c = make_client()
    body = {
        "petrol_mpg": 32.0, "fuel_type": "petrol",
        "away_rate_gbp_per_kwh": 0.45, "exclude_standing_charge": True,
    }
    assert c.put("/api/settings", json=body).status_code == 200
    got = c.get("/api/settings").json()
    assert got["petrol_mpg"] == 32.0
    assert got["away_rate_gbp_per_kwh"] == 0.45
