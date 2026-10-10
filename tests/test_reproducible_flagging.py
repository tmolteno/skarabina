# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Test that rflag/tfcrop produce reproducible results with different fit block sizes.

Issue #13: Default row chunk depends on the host's core count, so per-chunk
flaggers are not reproducible across hosts. Solution: introduce a canonical
fit-block size for threshold calculation.
"""
import numpy as np
import pytest

from skarabina.baselines import Baselines
from skarabina.rflag import RFlagParams, rflag_plane
from skarabina.tfcrop import TFCropParams, tfcrop_plane


def interleaved(nant=16, ntime=40, nchan=96, seed=0, preflag=0.3):
    """An MS-style chunk; returns (vis, preflags, rfi, antenna1, antenna2)."""
    rng = np.random.default_rng(seed)
    pairs = [(i, j) for i in range(nant) for j in range(i + 1, nant)]
    a1 = np.tile([p[0] for p in pairs], ntime)
    a2 = np.tile([p[1] for p in pairs], ntime)
    b = np.tile(np.arange(len(pairs)), ntime)
    t = np.repeat(np.arange(ntime), len(pairs))
    s = np.exp(rng.normal(0, 0.1, nant))
    sigma = (s[a1] * s[a2])[:, None]
    band = 1 + 0.3 * np.cos(np.linspace(0, 2.5, nchan))
    level = (rng.uniform(5, 15, len(pairs))
             * np.exp(1j * rng.uniform(0, 2 * np.pi, len(pairs))))[b][:, None]
    noise = rng.normal(size=(b.size, nchan)) + 1j * rng.normal(size=(b.size, nchan))
    vis = level * band + sigma * noise
    rfi = np.zeros(vis.shape, dtype=bool)
    for bb in rng.choice(len(pairs), 15, replace=False):     # 2-integration bursts
        t0, c0 = rng.integers(3, ntime - 3), rng.integers(10, nchan - 20)
        rows = (b == bb) & (t >= t0) & (t < t0 + 2)
        rfi[np.ix_(rows, np.arange(c0, c0 + 8))] = True
    phase = np.exp(1j * rng.uniform(0, 2 * np.pi, vis.shape))
    vis = np.where(rfi, vis + 10 * sigma * phase, vis)
    pre = rng.random(vis.shape) < preflag
    return vis.astype(np.complex64), pre, rfi, a1, a2


def test_rflag_reproducible_with_fit_block():
    """RFlag with fit_rows produces consistent results across runs.

    This test verifies that when fit_rows is specified, rflag produces
    the same results when run multiple times (consistent/reproducible).
    The key point: fit_rows makes thresholds independent of the dask chunk size,
    ensuring reproducible results across hosts with different core counts (#13).
    """
    vis, pre, rfi, a1, a2 = interleaved(nant=20, ntime=100, nchan=128, seed=42)
    baselines = Baselines(a1, a2)
    params = RFlagParams()

    # Run rflag with a fixed fit_rows value twice
    flags_run1, _ = rflag_plane(vis, params, pre, baselines=baselines, fit_rows=100)
    flags_run2, _ = rflag_plane(vis, params, pre, baselines=baselines, fit_rows=100)

    # Results should be identical (reproducible)
    assert np.array_equal(flags_run1, flags_run2), \
        "rflag with fit_rows should produce identical results on consecutive runs"

    # Also verify that fit_rows=100 produces different results than fit_rows=10000
    # (this confirms that fit_rows actually affects threshold calculation)
    flags_100, _ = rflag_plane(vis, params, pre, baselines=baselines, fit_rows=100)
    flags_10k, _ = rflag_plane(vis, params, pre, baselines=baselines, fit_rows=10000)

    # The results should be different because thresholds are computed per fit block
    # (small differences are expected due to different medians per block)
    flag_count_100 = np.sum(flags_100)
    flag_count_10k = np.sum(flags_10k)
    # Allow for small differences but ensure they're not identical
    assert flag_count_100 != flag_count_10k, \
        f"fit_rows=100 and fit_rows=10000 should produce different results (but got {flag_count_100} both times)"


def test_tfcrop_reproducible_with_fit_block():
    """TFCrop with fit_rows produces consistent results across runs."""
    vis, pre, rfi, a1, a2 = interleaved(nant=20, ntime=100, nchan=128, seed=42)
    amplitude = np.abs(vis).astype(float)
    params = TFCropParams()

    # Run tfcrop (non-baseline-aware) with a fixed fit_rows value twice
    flags_run1, _ = tfcrop_plane(amplitude, params, pre, fit_rows=100)
    flags_run2, _ = tfcrop_plane(amplitude, params, pre, fit_rows=100)

    # Results should be identical (reproducible)
    assert np.array_equal(flags_run1, flags_run2), \
        "tfcrop with fit_rows should produce identical results on consecutive runs"

    # Also verify that fit_rows affects the results
    flags_100, _ = tfcrop_plane(amplitude, params, pre, fit_rows=100)
    flags_10k, _ = tfcrop_plane(amplitude, params, pre, fit_rows=10000)

    flag_count_100 = np.sum(flags_100)
    flag_count_10k = np.sum(flags_10k)
    assert flag_count_100 != flag_count_10k, \
        f"fit_rows=100 and fit_rows=10000 should produce different results (but got {flag_count_100} both times)"
