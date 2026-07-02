# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import math

import pytest


def max_integration_time(nu_max_hz, uv_max_m, loss=0.01, fov_rad=1.0):
    """Fringe-rotation integration time limit (seconds).

    From Wijnholds (2018, MNRAS), the amplitude loss for time
    averaging at angular distance ℓ from the phase centre is:

      ρ = sinc(π · ω_⊕ · Δt · B · ν · ℓ / c)

    For small loss L = 1 − |ρ|:

      Δt_max = c · √(6L) / (π · ω_⊕ · B · ν · ℓ)
    """
    c_ms = 299792458.0
    omega_earth = 7.2921150e-5
    if uv_max_m <= 0 or nu_max_hz <= 0 or fov_rad <= 0:
        return float("inf")
    return (
        c_ms
        * math.sqrt(6.0 * loss)
        / (math.pi * omega_earth * uv_max_m * nu_max_hz * fov_rad)
    )


def test_1km_1_4GHz():
    """1 km baseline at 1.4 GHz, 1% loss, ℓ=1 rad → ~0.23 s"""
    dt = max_integration_time(1.4e9, 1000.0, loss=0.01, fov_rad=1.0)
    assert dt == pytest.approx(0.229, rel=0.02)


def test_default_fov():
    """Default ℓ=1° = 0.01745 rad: 1km, 1.4GHz, 1% loss → ~13.1 s"""
    dt = max_integration_time(1.4e9, 1000.0, loss=0.01, fov_rad=0.0174533)
    assert dt == pytest.approx(13.1, rel=0.02)


def test_100m_1_4GHz():
    """100 m baseline at 1.4 GHz, 1% loss, ℓ=1 → ~2.29 s"""
    dt = max_integration_time(1.4e9, 100.0, loss=0.01, fov_rad=1.0)
    assert dt == pytest.approx(2.29, rel=0.02)


def test_10km_1_4GHz():
    """10 km baseline at 1.4 GHz, 1% loss, ℓ=1 → ~23 ms"""
    dt = max_integration_time(1.4e9, 10000.0, loss=0.01, fov_rad=1.0)
    assert dt == pytest.approx(0.0229, rel=0.02)


def test_1km_150MHz():
    """1 km baseline at 150 MHz, 1% loss, ℓ=1 → ~2.14 s"""
    dt = max_integration_time(150e6, 1000.0, loss=0.01, fov_rad=1.0)
    assert dt == pytest.approx(2.14, rel=0.02)


def test_1km_5GHz():
    """1 km baseline at 5 GHz, 1% loss, ℓ=1 → ~64 ms"""
    dt = max_integration_time(5e9, 1000.0, loss=0.01, fov_rad=1.0)
    assert dt == pytest.approx(0.0642, rel=0.02)


def test_zero_baseline():
    """Zero baseline → infinite integration time."""
    dt = max_integration_time(1.4e9, 0.0, loss=0.01, fov_rad=1.0)
    assert dt == float("inf")


def test_3pct_loss():
    """3% loss: sqrt(18/6) = sqrt(3) larger than 1% → ~0.397 s"""
    dt = max_integration_time(1.4e9, 1000.0, loss=0.03, fov_rad=1.0)
    assert dt == pytest.approx(0.397, rel=0.02)


def test_narrow_field():
    """Narrow field ℓ=0.1 rad → 10× longer"""
    dt = max_integration_time(1.4e9, 1000.0, loss=0.01, fov_rad=0.1)
    assert dt == pytest.approx(2.29, rel=0.02)
