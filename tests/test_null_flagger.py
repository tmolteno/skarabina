# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import types

import dask.array as da
import numpy as np
import pytest

from skarabina import main as main_module
from skarabina.barber import barber


def max_integration_time(nu_max_hz, uv_max_m):
    """Fringe-rotation integration time limit (seconds).

    Δt_max ≈ c / (ν_max · ω_⊕ · B_max)
    """
    c_ms = 299792458.0
    omega_earth = 7.2921150e-5
    if uv_max_m <= 0:
        return float("inf")
    return c_ms / (nu_max_hz * omega_earth * uv_max_m)


def test_schema_loads():
    """The stimela cab schema should load and define the skarabina cab."""
    cab = main_module.schemas.cabs.get("skarabina")
    assert cab is not None
    inputs = cab.inputs
    for key in ("ms", "summary", "barber", "apply", "clobber"):
        assert key in inputs, f"missing input '{key}' in schema"


def test_barber_runs_on_synthetic_data(capsys):
    """barber() should produce a max-visibility report from in-memory data."""
    shape = (4, 3, 2)  # dumps, channels, polarizations
    data = np.zeros(shape, dtype=complex)
    data[1, 2, 0] = 10.0 + 0j  # a clear maximum
    ms = types.SimpleNamespace(
        data=da.from_array(data),
        flag=da.from_array(np.zeros(shape, dtype=bool)),
        weight_spectrum=da.from_array(np.ones(shape)),
        antenna1=da.from_array(np.array([0, 1, 2, 3])),
        antenna2=da.from_array(np.array([1, 2, 3, 0])),
        time=da.from_array(np.array([0.0, 1.0, 2.0, 3.0])),
    )

    barber(ms, pol=None)

    out = capsys.readouterr().out
    assert "Max |v| = 10" in out
    assert "Percentiles" in out


def test_max_integration_time_1km_1_4GHz():
    """1 km baseline at 1.4 GHz → ~2.94 s"""
    dt = max_integration_time(1.4e9, 1000.0)
    assert dt == pytest.approx(2.94, rel=0.01)


def test_max_integration_time_100m_1_4GHz():
    """100 m baseline at 1.4 GHz → ~29.4 s"""
    dt = max_integration_time(1.4e9, 100.0)
    assert dt == pytest.approx(29.4, rel=0.01)


def test_max_integration_time_10km_1_4GHz():
    """10 km baseline at 1.4 GHz → ~0.294 s"""
    dt = max_integration_time(1.4e9, 10000.0)
    assert dt == pytest.approx(0.294, rel=0.01)


def test_max_integration_time_1km_150MHz():
    """1 km baseline at 150 MHz → ~27.4 s"""
    dt = max_integration_time(150e6, 1000.0)
    assert dt == pytest.approx(27.4, rel=0.01)


def test_max_integration_time_1km_5GHz():
    """1 km baseline at 5 GHz → ~0.82 s"""
    dt = max_integration_time(5e9, 1000.0)
    assert dt == pytest.approx(0.822, rel=0.01)


def test_max_integration_time_zero_baseline():
    """Zero baseline → infinite integration time."""
    dt = max_integration_time(1.4e9, 0.0)
    assert dt == float("inf")
