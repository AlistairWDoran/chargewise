"""Petrol-equivalent savings tests."""

from __future__ import annotations

import pytest

from chargewise.engine.savings import compute_savings, petrol_cost_gbp


def test_petrol_cost_known_values():
    # 300 miles at 30 mpg = 10 gallons = 45.4609 L; at 130p/L => £59.10
    assert petrol_cost_gbp(300, 30, 130) == pytest.approx(59.099, abs=0.01)


def test_petrol_cost_rejects_zero_mpg():
    with pytest.raises(ValueError):
        petrol_cost_gbp(100, 0, 130)


def test_compute_savings_end_to_end():
    r = compute_savings(miles=1000, electric_cost_gbp=30.0, pence_per_litre=140, mpg=30)
    assert r.petrol_cost_gbp == pytest.approx(212.15, abs=0.01)
    assert r.saving_gbp == pytest.approx(182.15, abs=0.01)
    assert r.electric_pence_per_mile == pytest.approx(3.0, abs=0.01)
    assert r.petrol_pence_per_mile == pytest.approx(21.22, abs=0.05)
