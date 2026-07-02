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
