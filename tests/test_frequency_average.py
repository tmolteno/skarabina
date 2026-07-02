# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import numpy as np


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


def test_exact_division():
    """4 channels, factor 2 → 2 channels."""
    data = np.ones((3, 4, 2), dtype=complex)
    flag = np.zeros((3, 4, 2), dtype=bool)
    result = _freq_avg_data(data, flag, 2)
    assert result.shape == (3, 2, 2)
    assert np.allclose(result, 1.0 + 0j)


def test_with_trailing():
    """5 channels, factor 2 → 3 channels (last from 1 trailing)."""
    data = np.ones((3, 5, 2), dtype=complex)
    flag = np.zeros((3, 5, 2), dtype=bool)
    result = _freq_avg_data(data, flag, 2)
    assert result.shape == (3, 3, 2)
    assert np.allclose(result, 1.0 + 0j)


def test_excludes_flagged():
    """Flagged data should be excluded from the average."""
    data = np.full((1, 4, 1), 10.0 + 0j, dtype=complex)
    flag = np.zeros((1, 4, 1), dtype=bool)
    flag[0, 0, 0] = True  # first channel flagged
    result = _freq_avg_data(data, flag, 2)
    assert result.shape == (1, 2, 1)
    assert np.allclose(result, 10.0 + 0j)


def test_all_flagged_group():
    """When all channels in a group are flagged, result should be 0."""
    data = np.full((1, 4, 1), 10.0 + 0j, dtype=complex)
    flag = np.zeros((1, 4, 1), dtype=bool)
    flag[0, 0, 0] = True
    flag[0, 1, 0] = True
    result = _freq_avg_data(data, flag, 2)
    assert np.allclose(result[0, 0, 0], 0.0 + 0j)
    assert np.allclose(result[0, 1, 0], 10.0 + 0j)


def test_flag_or():
    """FLAG should be OR'd over the factor dimension."""
    flag = np.zeros((1, 4, 1), dtype=bool)
    flag[0, 0, 0] = True  # only first channel flagged
    result = _freq_avg_flag(flag, 2)
    assert result[0, 0, 0]  # group (0,1): one flagged → output flagged
    assert not result[0, 1, 0]  # group (2,3): none flagged


def test_flag_or_trailing():
    """FLAG OR with trailing channels."""
    flag = np.zeros((1, 5, 1), dtype=bool)
    flag[0, 4, 0] = True  # only trailing channel flagged
    result = _freq_avg_flag(flag, 2)
    assert not result[0, 0, 0]  # group (0,1)
    assert not result[0, 1, 0]  # group (2,3)
    assert result[0, 2, 0]  # trailing group: flagged


def test_chan_freq():
    """Channel frequencies should be averaged."""
    freqs = np.array([100.0, 200.0, 300.0, 400.0, 500.0])
    factor = 2
    n_full = len(freqs) // factor
    n_rem = len(freqs) % factor
    trim = n_full * factor
    avg_full = np.mean(freqs[:trim].reshape(n_full, factor), axis=1)
    assert np.allclose(avg_full, [150.0, 350.0])
    avg_rem = np.mean(freqs[trim:])
    result = np.append(avg_full, avg_rem)
    assert np.allclose(result, [150.0, 350.0, 500.0])


def test_weight_spectrum_sum():
    """WEIGHT_SPECTRUM: sum of unflagged weights (w = 1/σ², Σ w)."""
    # 2 groups of 2 channels, all unflagged
    weight = np.ones((1, 4, 1), dtype=np.float32) * 2.0
    flag = np.zeros((1, 4, 1), dtype=bool)
    n_full = 4 // 2
    trim = n_full * 2
    reshaped = weight[:, :trim, :].reshape(1, n_full, 2, 1)
    result = np.sum(reshaped, axis=2)  # sum, not mean
    assert np.allclose(result, [4.0, 4.0])


def test_sigma_spectrum_inv_var():
    """SIGMA_SPECTRUM: σ̄ = 1 / √(Σ 1/σ²)."""
    # 2 groups of 2: σ = 2.0 each → 1/σ² = 0.25 each → Σ = 0.5 → σ̄ = 1/√0.5 = √2
    sigma = np.ones((1, 4, 1), dtype=np.float32) * 2.0
    flag = np.zeros((1, 4, 1), dtype=bool)
    n_full = 4 // 2
    trim = n_full * 2
    s = sigma[:, :trim, :].reshape(1, n_full, 2, 1)
    inv_var = 1.0 / (s * s)
    sum_inv = np.sum(inv_var, axis=2)
    sum_safe = np.where(sum_inv == 0, 1, sum_inv)
    result = np.sqrt(1.0 / sum_safe)
    assert np.allclose(result, np.sqrt(2.0))  # 1/√(2 × 0.25) = √2


def test_sigma_with_flagged():
    """SIGMA with one flagged: only use unflagged."""
    sigma = np.array(
        [[[2.0], [4.0], [3.0], [5.0]]], dtype=np.float32
    )  # 1 row, 4 ch, 1 corr
    flag = np.array([[[True], [False], [False], [False]]], dtype=bool)
    # Group 0: σ=[2,4], 1/σ²=[0, 0.0625], Σ=0.0625, σ̄=1/√0.0625=4.0
    # Group 1: σ=[3,5], 1/σ²=[0.1111, 0.04], Σ=0.1511, σ̄=1/√0.1511≈2.57
    n_full = 4 // 2
    trim = n_full * 2
    s = sigma[:, :trim, :].reshape(1, n_full, 2, 1)
    f = flag[:, :trim, :].reshape(1, n_full, 2, 1)
    inv_var = np.where(f, 0, 1.0 / (s * s))
    sum_inv = np.sum(inv_var, axis=2)
    sum_safe = np.where(sum_inv == 0, 1, sum_inv)
    result = np.sqrt(1.0 / sum_safe)
    assert np.allclose(result[0, 0, 0], 4.0)  # only the 4.0 contributes
    assert np.allclose(result[0, 1, 0], np.sqrt(1 / (1 / 9 + 1 / 25)))  # ~2.57
