# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import types

import dask.array as da
import numpy as np
import pytest

from skarabina import main as main_module
from skarabina.barber import barber


def max_integration_time(nu_max_hz, uv_max_m, loss=0.01, fov_rad=1.0):
    """Fringe-rotation integration time limit (seconds).

    From Wijnholds (2018, MNRAS), the amplitude loss for time
    averaging at angular distance ℓ from the phase centre is:

      ρ = sinc(π · ω_⊕ · Δt · B · ν · ℓ / c)

    For small loss L = 1 − |ρ|:

      Δt_max = c · √(6L) / (π · ω_⊕ · B · ν · ℓ)
    """
    import math

    c_ms = 299792458.0
    omega_earth = 7.2921150e-5
    if uv_max_m <= 0 or nu_max_hz <= 0 or fov_rad <= 0:
        return float("inf")
    return (
        c_ms
        * math.sqrt(6.0 * loss)
        / (math.pi * omega_earth * uv_max_m * nu_max_hz * fov_rad)
    )


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
    """1 km baseline at 1.4 GHz, 1% loss, ℓ=1 rad → ~0.23 s"""
    dt = max_integration_time(1.4e9, 1000.0, loss=0.01, fov_rad=1.0)
    assert dt == pytest.approx(0.229, rel=0.02)


def test_max_integration_time_default_fov():
    """Default ℓ=1° = 0.01745 rad: 1km, 1.4GHz, 1% loss → ~13.1 s"""
    dt = max_integration_time(1.4e9, 1000.0, loss=0.01, fov_rad=0.0174533)
    assert dt == pytest.approx(13.1, rel=0.02)


def test_max_integration_time_100m_1_4GHz():
    """100 m baseline at 1.4 GHz, 1% loss, ℓ=1 → ~2.29 s"""
    dt = max_integration_time(1.4e9, 100.0, loss=0.01, fov_rad=1.0)
    assert dt == pytest.approx(2.29, rel=0.02)


def test_max_integration_time_10km_1_4GHz():
    """10 km baseline at 1.4 GHz, 1% loss, ℓ=1 → ~23 ms"""
    dt = max_integration_time(1.4e9, 10000.0, loss=0.01, fov_rad=1.0)
    assert dt == pytest.approx(0.0229, rel=0.02)


def test_max_integration_time_1km_150MHz():
    """1 km baseline at 150 MHz, 1% loss, ℓ=1 → ~2.14 s"""
    dt = max_integration_time(150e6, 1000.0, loss=0.01, fov_rad=1.0)
    assert dt == pytest.approx(2.14, rel=0.02)


def test_max_integration_time_1km_5GHz():
    """1 km baseline at 5 GHz, 1% loss, ℓ=1 → ~64 ms"""
    dt = max_integration_time(5e9, 1000.0, loss=0.01, fov_rad=1.0)
    assert dt == pytest.approx(0.0642, rel=0.02)


def test_max_integration_time_zero_baseline():
    """Zero baseline → infinite integration time."""
    dt = max_integration_time(1.4e9, 0.0, loss=0.01, fov_rad=1.0)
    assert dt == float("inf")


def test_max_integration_time_3pct_loss():
    """3% loss: sqrt(18/6) = sqrt(3) larger than 1% → ~0.397 s"""
    dt = max_integration_time(1.4e9, 1000.0, loss=0.03, fov_rad=1.0)
    assert dt == pytest.approx(0.397, rel=0.02)


def test_max_integration_time_narrow_field():
    """Narrow field ℓ=0.1 rad → 10× longer"""
    dt = max_integration_time(1.4e9, 1000.0, loss=0.01, fov_rad=0.1)
    assert dt == pytest.approx(2.29, rel=0.02)


# --- Frequency averaging tests ---


def _freq_avg_data(data, flag, factor):
    """Reference implementation: average channels, excluding flagged."""
    nrow, nchan, ncorr = data.shape
    n_full = nchan // factor
    n_rem = nchan % factor
    trim = n_full * factor

    results = []
    for i in range(n_full):
        slc = slice(i * factor, (i + 1) * factor)
        d = data[:, slc, :]
        f = flag[:, slc, :]
        num = np.sum(np.where(f, 0, d), axis=1)
        den = np.sum(~f, axis=1)
        den[den == 0] = 1
        results.append(num / den)
    if n_rem > 0:
        d = data[:, trim:, :]
        f = flag[:, trim:, :]
        num = np.sum(np.where(f, 0, d), axis=1)
        den = np.sum(~f, axis=1)
        den[den == 0] = 1
        results.append(num / den)
    return np.stack(results, axis=1)


def _freq_avg_flag(flag, factor):
    """Reference: OR flags over groups of channels."""
    nrow, nchan, ncorr = flag.shape
    n_full = nchan // factor
    n_rem = nchan % factor
    trim = n_full * factor

    results = []
    for i in range(n_full):
        slc = slice(i * factor, (i + 1) * factor)
        results.append(np.any(flag[:, slc, :], axis=1))
    if n_rem > 0:
        results.append(np.any(flag[:, trim:, :], axis=1))
    return np.stack(results, axis=1)


def test_freq_avg_exact_division():
    """4 channels, factor 2 → 2 channels."""
    data = np.ones((3, 4, 2), dtype=complex)
    flag = np.zeros((3, 4, 2), dtype=bool)
    result = _freq_avg_data(data, flag, 2)
    assert result.shape == (3, 2, 2)
    assert np.allclose(result, 1.0 + 0j)


def test_freq_avg_with_trailing():
    """5 channels, factor 2 → 3 channels (last from 1 trailing)."""
    data = np.ones((3, 5, 2), dtype=complex)
    flag = np.zeros((3, 5, 2), dtype=bool)
    result = _freq_avg_data(data, flag, 2)
    assert result.shape == (3, 3, 2)
    assert np.allclose(result, 1.0 + 0j)


def test_freq_avg_excludes_flagged():
    """Flagged data should be excluded from the average."""
    data = np.full((1, 4, 1), 10.0 + 0j, dtype=complex)
    flag = np.zeros((1, 4, 1), dtype=bool)
    flag[0, 0, 0] = True  # first channel flagged
    # factor 2: groups (0,1) and (2,3)
    # Group 0: chan 0 flagged, chan 1 value 10 → avg 10
    # Group 1: both unflagged → avg 10
    result = _freq_avg_data(data, flag, 2)
    assert result.shape == (1, 2, 1)
    assert np.allclose(result, 10.0 + 0j)


def test_freq_avg_all_flagged_group():
    """When all channels in a group are flagged, result should be 0."""
    data = np.full((1, 4, 1), 10.0 + 0j, dtype=complex)
    flag = np.zeros((1, 4, 1), dtype=bool)
    flag[0, 0, 0] = True
    flag[0, 1, 0] = True
    # Group 0: both flagged → output 0 but also FLAG=True
    result = _freq_avg_data(data, flag, 2)
    assert np.allclose(result[0, 0, 0], 0.0 + 0j)
    assert np.allclose(result[0, 1, 0], 10.0 + 0j)


def test_freq_avg_flag_or():
    """FLAG should be OR'd over the factor dimension."""
    flag = np.zeros((1, 4, 1), dtype=bool)
    flag[0, 0, 0] = True  # only first channel flagged
    result = _freq_avg_flag(flag, 2)
    assert result[0, 0, 0]  # group (0,1): one flagged → output flagged
    assert not result[0, 1, 0]  # group (2,3): none flagged


def test_freq_avg_flag_or_trailing():
    """FLAG OR with trailing channels."""
    flag = np.zeros((1, 5, 1), dtype=bool)
    flag[0, 4, 0] = True  # only trailing channel flagged
    result = _freq_avg_flag(flag, 2)
    assert not result[0, 0, 0]  # group (0,1)
    assert not result[0, 1, 0]  # group (2,3)
    assert result[0, 2, 0]  # trailing group: flagged


def test_freq_avg_chan_freq():
    """Channel frequencies should be averaged."""
    freqs = np.array([100.0, 200.0, 300.0, 400.0, 500.0])
    factor = 2
    n_full = len(freqs) // factor  # 2
    n_rem = len(freqs) % factor  # 1
    trim = n_full * factor  # 4
    avg_full = np.mean(freqs[:trim].reshape(n_full, factor), axis=1)
    assert np.allclose(avg_full, [150.0, 350.0])
    avg_rem = np.mean(freqs[trim:])  # 500.0
    result = np.append(avg_full, avg_rem)
    assert np.allclose(result, [150.0, 350.0, 500.0])
