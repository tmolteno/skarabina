# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Baseline-aware auto-flagging: :mod:`skarabina.baselines` and its use.

A measurement-set row chunk interleaves baselines; the tests build chunks the
way an MS is written -- time-major, every baseline in every integration -- with
each baseline at its own level, per-antenna noise and RFI at known positions.
"""
import warnings

import numpy as np
import pytest

from skarabina.baselines import (
    Baselines,
    antenna_noise_model,
    baseline_noise,
    group_median,
    group_median_rows,
    row_noise,
)
from skarabina.rflag import RFlagParams, local_rms, rflag_plane
from skarabina.tfcrop import TFCropParams, robust_fit, tfcrop_plane


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


# --- bookkeeping --------------------------------------------------------------


def test_baselines_group_rows_and_keep_time_order():
    a1 = np.array([0, 0, 1, 0, 0, 1])
    a2 = np.array([1, 2, 2, 1, 2, 2])
    bl = Baselines(a1, a2)
    assert bl.count == 3
    assert list(bl.labels) == [0, 1, 2, 0, 1, 2]
    assert list(bl.order) == [0, 3, 1, 4, 2, 5]          # time order kept
    assert list(bl.group_start) == [0, 0, 2, 2, 4, 4]
    assert list(bl.group_stop) == [2, 2, 4, 4, 6, 6]


def test_baselines_split_by_scan():
    bl = Baselines([0, 0, 0, 0], [1, 1, 1, 1], scan=[1, 1, 2, 2])
    assert bl.count == 2 and list(bl.labels) == [0, 0, 1, 1]


def test_group_medians_match_brute_force():
    rng = np.random.default_rng(1)
    a1, a2 = rng.integers(0, 4, 50), rng.integers(4, 6, 50)
    bl = Baselines(a1, a2)
    values = rng.normal(size=(50, 7))
    values[rng.random(values.shape) < 0.2] = np.nan
    rows = group_median_rows(values, bl, budget=40)
    per_row = group_median(values[:, 0], bl)
    for g in range(bl.count):
        members = bl.labels == g
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN columns
            expected = np.nanmedian(values[members], axis=0)
        np.testing.assert_array_equal(rows[g], expected)
        np.testing.assert_array_equal(per_row[g], expected[0])


def test_antenna_model_predicts_noise_and_ignores_an_rfi_baseline():
    rng = np.random.default_rng(2)
    nant = 20
    pairs = np.array([(i, j) for i in range(nant) for j in range(i + 1, nant)])
    s = np.exp(rng.normal(0, 0.1, nant))
    true = s[pairs[:, 0]] * s[pairs[:, 1]]
    measured = true * np.exp(rng.normal(0, 0.01, true.size))
    measured[7] *= 2.0                                  # inflated by RFI
    predicted = antenna_noise_model(measured, pairs[:, 0], pairs[:, 1])
    assert np.abs(np.log(predicted / true)).max() < 0.05
    assert predicted[7] == pytest.approx(true[7], rel=0.05)


def test_baseline_noise_recovers_each_rows_noise():
    vis, pre, _, a1, a2 = interleaved(preflag=0.3)
    bl = Baselines(a1, a2)
    # Channel-to-channel steps cancel the level and the smooth band.
    assert np.nanmedian(row_noise(vis, pre)) == pytest.approx(1.0, rel=0.1)
    per_row = baseline_noise(vis, pre, bl)
    assert per_row.shape == (vis.shape[0],)
    assert np.all(np.isfinite(per_row))


# --- the bugs the real-data test turned up ---------------------------------------


def test_local_rms_leaves_flagged_samples_out_at_a_nonzero_level():
    """A flagged sample in a window must not count as a zero visibility.

    Until 1.0.6 the window's length was its count, so a flagged sample in a
    window about a level of 10 read as a scatter of ~5.
    """
    rng = np.random.default_rng(3)
    values = 10.0 + rng.normal(0, 0.05, 200) + 1j * rng.normal(0, 0.05, 200)
    with_gap = values.copy()
    with_gap[100] = complex(np.nan, np.nan)
    assert local_rms(with_gap, 3)[99] < 0.2
    assert local_rms(with_gap, 3)[99] == pytest.approx(local_rms(values, 3)[99], rel=1.5)


def test_local_rms_has_no_scatter_for_a_single_sample():
    values = np.full(5, complex(np.nan, np.nan))
    values[2] = 3 + 4j
    assert np.isnan(local_rms(values, 3)[2])


def test_robust_fit_follows_a_narrow_band():
    """8 channels, 7 pieces: every piece was unfittable and the fit a step."""
    x = np.arange(8.0)
    band = 10 + 4 * np.cos(np.linspace(0, 2.5, 8))
    fitted, _ = robust_fit(x, band, 7, 3)
    # Was 1.3 off.  What is left is at channel 0, which _edge_taper weights
    # down to TAPER_FLOOR, so the fit extrapolates to it.
    assert np.abs(fitted - band).max() < 0.3


# --- the algorithms on an interleaved chunk ------------------------------------------


def _rates(flag, pre, rfi):
    live = ~pre
    new = flag & live
    return new[live & ~rfi].mean(), new[live & rfi].mean()


def test_tfcrop_does_not_flag_baselines_for_their_level():
    """One bandpass for the whole chunk flags every baseline brighter or
    fainter than average; a fit per baseline flags the RFI and little else."""
    vis, pre, rfi, a1, a2 = interleaved()
    amplitude = np.abs(vis).astype(float)
    mixed, _ = tfcrop_plane(amplitude, TFCropParams(), pre)
    aware, _ = tfcrop_plane(amplitude, TFCropParams(), pre, baselines=Baselines(a1, a2))
    mixed_fp, _ = _rates(mixed, pre, rfi)
    aware_fp, aware_recall = _rates(aware, pre, rfi)
    assert mixed_fp > 0.2, "the interleaved chunk no longer shows the problem"
    assert aware_fp < 0.01
    assert aware_recall > 0.6


def test_rflag_windows_stay_inside_a_baseline_and_find_bursts():
    """A burst of two integrations on one baseline is found by the time step
    only when its windows follow that baseline's rows."""
    vis, pre, rfi, a1, a2 = interleaved()
    mixed, _ = rflag_plane(vis, RFlagParams(), pre)
    aware, _ = rflag_plane(vis, RFlagParams(), pre, baselines=Baselines(a1, a2))
    _, mixed_recall = _rates(mixed, pre, rfi)
    aware_fp, aware_recall = _rates(aware, pre, rfi)
    assert aware_recall > mixed_recall + 0.05
    assert aware_fp < 0.01


def test_flags_come_back_in_the_original_row_order():
    vis, pre, rfi, a1, a2 = interleaved()
    bl = Baselines(a1, a2)
    aware, _ = rflag_plane(vis, RFlagParams(), pre, baselines=bl)
    # Pre-existing flags are kept exactly where they were.
    assert aware[pre].all()
    shuffled = np.random.default_rng(4).permutation(vis.shape[0])
    again, _ = rflag_plane(vis[shuffled], RFlagParams(), pre[shuffled],
                           baselines=Baselines(a1[shuffled], a2[shuffled]))
    assert again.sum() > 0
    np.testing.assert_array_equal(again[pre[shuffled]], True)


def test_dask_path_passes_the_baselines(monkeypatch):
    """flag_rflag/flag_tfcrop give each block its rows' antennas."""
    import xarray as xr

    import skarabina.dask_ms as dask_ms
    from skarabina.dask_ms import DaskMS

    vis, pre, _, a1, a2 = interleaved(ntime=30)
    nrow, nchan = vis.shape
    ds = xr.Dataset({
        "DATA": (("row", "chan", "corr"), vis[:, :, None]),
        "FLAG": (("row", "chan", "corr"), pre[:, :, None]),
        "ANTENNA1": (("row",), a1.astype(np.int32)),
        "ANTENNA2": (("row",), a2.astype(np.int32)),
        "FLAG_ROW": (("row",), np.zeros(nrow, dtype=bool)),
    }).chunk({"row": 1200, "chan": -1, "corr": -1})
    ms = DaskMS.__new__(DaskMS)
    ms.ds, ms.changed, ms.name = ds, {}, "<synthetic>"
    ms.flag_rflag(RFlagParams())
    got = np.asarray(ms.ds.FLAG.data)[:, :, 0]
    expected = np.concatenate([
        rflag_plane(vis[s:s + 1200], RFlagParams(), pre[s:s + 1200],
                    baselines=Baselines(a1[s:s + 1200], a2[s:s + 1200],
                                        np.full(min(1200, nrow - s), -1)))[0]
        for s in range(0, nrow, 1200)
    ])
    np.testing.assert_array_equal(got, expected)

    monkeypatch.setattr(dask_ms, "AUTOFIT_BASELINES", False)
    ms.ds["FLAG"] = (("row", "chan", "corr"), pre[:, :, None])
    ms.flag_rflag(RFlagParams())
    classic = np.asarray(ms.ds.FLAG.data)[:, :, 0]
    assert not np.array_equal(classic, got)
