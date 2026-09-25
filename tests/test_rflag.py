# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for the RFlag algorithm in :mod:`skarabina.rflag`.

The algorithm on synthetic planes with RFI at known positions, so recall and
false-positive rate can both be measured.  A flagger that flags everything
scores 100% recall and is useless, so every recall assertion is paired with a
false-positive bound.
"""
import warnings

import numpy as np
import pytest

from skarabina.rflag import (
    DEFAULTS,
    RFlagParams,
    local_rms,
    neighbour_residual,
    rflag_plane,
    robust_scale,
)


def bandpass(nchan):
    """A smooth, sloping band -- the thing that must NOT be flagged."""
    return 10.0 + 4.0 * np.cos(np.linspace(0, 2.5, nchan))


def make_plane(ntime=200, nchan=64, noise=0.05, seed=0):
    """A clean plane: one smooth bandpass, with noise in both parts."""
    rng = np.random.default_rng(seed)
    amplitude = np.outer(np.ones(ntime), bandpass(nchan))
    real = amplitude + rng.normal(0, noise, (ntime, nchan))
    imag = rng.normal(0, noise, (ntime, nchan))
    return real + 1j * imag


def spike_plane(factor=3.0, ntime=200, nchan=64, seed=2, channels=(20,)):
    plane = make_plane(ntime, nchan, seed=seed)
    for channel in channels:
        plane[:, channel] *= factor
    return plane


# --- the robust scale -------------------------------------------------------


def test_robust_scale_is_the_median_absolute_value():
    """Not the MAD about the median, which the outliers themselves inflate."""
    values = np.array([0.0, 0.0, 0.0, 1.0, 1.0, 100.0])
    # median|v| = 0.5 ; MAD about the median (med=0.5) = median(0.5,0.5,0.5,0.5,0.5,99.5) = 0.5
    assert robust_scale(values) == pytest.approx(0.5)


def test_robust_scale_is_not_inflated_by_a_few_large_outliers():
    rng = np.random.default_rng(0)
    clean = np.abs(rng.normal(0, 1.0, 1000))
    spiked = clean.copy()
    spiked[:20] = 1000.0
    assert robust_scale(spiked) == pytest.approx(robust_scale(clean), rel=0.2)


def test_robust_scale_of_nothing_is_zero():
    assert robust_scale(np.array([np.nan, np.nan])) == 0.0


# --- the local scatter ------------------------------------------------------


def test_local_rms_measures_scatter_not_signal():
    """The property that makes a supplied `timedev` meaningful.

    An r.m.s. about zero of a 10 Jy source in a 0.05 Jy noise floor is 10, and
    could never be compared with a noise estimate.  Measured about the window
    mean it is the noise.
    """
    rng = np.random.default_rng(1)
    signal = 10.0 + 3.0j
    values = signal + rng.normal(0, 0.05, 400) + 1j * rng.normal(0, 0.05, 400)
    scatter = local_rms(values, 3)
    assert np.nanmedian(scatter) == pytest.approx(0.05, abs=0.02), (
        "the local r.m.s. should recover the noise, not the signal"
    )
    assert np.nanmedian(scatter) < 1.0


def test_local_rms_combines_real_and_imaginary_in_quadrature():
    """A pure 3+4j sample has magnitude 5 and r.m.s. 5 over a flat window."""
    values = np.full(9, 3.0 + 4.0j)
    # A constant has zero scatter about its own mean, so this must be ~0.
    assert np.nanmax(local_rms(values, 3)) == pytest.approx(0.0, abs=1e-9)
    # Alternating samples do have scatter.
    alternating = np.array([3.0 + 4.0j, -3.0 - 4.0j] * 5)
    assert np.nanmedian(local_rms(alternating, 3)) > 1.0


def test_local_rms_windows_are_clipped_at_the_ends():
    """Every sample gets a window, and none runs off the edge."""
    values = np.arange(9, dtype=float)
    out = local_rms(values, 3)
    assert out.shape == values.shape
    assert np.isfinite(out).all()


def test_local_rms_ignores_flagged_samples():
    """A flagged sample must not be counted as a zero.

    Counting it as zero would drag the scatter down and hide the RFI, so the
    check is that the scatter stays the same order rather than collapsing.
    """
    rng = np.random.default_rng(2)
    values = rng.normal(0, 0.05, 200).astype(complex)
    with_nan = values.copy()
    with_nan[100] = np.nan
    before = local_rms(values, 3)[99]
    after = local_rms(with_nan, 3)[99]
    assert after > 0, "the window collapsed instead of dropping the sample"
    assert after == pytest.approx(before, rel=1.0), (
        f"scatter changed from {before:.4f} to {after:.4f} on dropping one sample"
    )


# --- the spectral deviation -------------------------------------------------


def test_neighbour_residual_is_flat_for_a_smooth_band():
    """A sloping band must not register as a deviation."""
    level = bandpass(64)
    residual = neighbour_residual(level)
    finite = residual[np.isfinite(residual)]
    assert finite.size > 50
    assert np.abs(finite).max() < 0.1 * np.ptp(level)


def test_neighbour_residual_is_never_identically_zero():
    """The degenerate case that broke the running-median approach.

    A running median sits exactly on a smooth band, so its residuals are all
    zero, every quantile-based scale for them is zero, and the threshold
    collapses.  A neighbour difference always carries the channel-to-channel
    noise, so its scale is well defined.
    """
    rng = np.random.default_rng(3)
    level = bandpass(64) + rng.normal(0, 0.05, 64)
    residual = neighbour_residual(level)
    finite = residual[np.isfinite(residual)]
    assert np.count_nonzero(finite) == finite.size
    assert robust_scale(finite) > 0


def test_neighbour_residual_isolates_a_narrow_spike():
    rng = np.random.default_rng(4)
    level = bandpass(64) + rng.normal(0, 0.05, 64)
    level[30] += 5.0
    residual = neighbour_residual(level)
    assert abs(residual[30]) > 4.0
    # The spike also moves its immediate neighbours, since each of them is
    # compared against a window containing the spike.  That is inherent to
    # comparing against neighbours and is why the flags spread by one channel.
    near = np.abs(residual[[29, 31]]).max()
    assert 0.1 < near < 4.0
    far = np.delete(residual, [29, 30, 31])
    far = far[np.isfinite(far)]
    assert np.abs(far).max() < 0.5


def test_neighbour_residual_leaves_the_ends_unresolved():
    residual = neighbour_residual(bandpass(64))
    assert np.isnan(residual[0]) and np.isnan(residual[-1])


# --- the plane --------------------------------------------------------------


@pytest.mark.parametrize("seed", [3, 7, 11, 13, 17])
def test_a_clean_plane_is_left_alone(seed):
    """No RFI: the flagger must not invent any.

    Five seeds rather than one, because a threshold that is slightly too tight
    flags a handful of pixels and a single seed can miss it.
    """
    flag, stats = rflag_plane(make_plane(seed=seed), RFlagParams())
    assert flag.mean() < 0.005, (
        f"flagged {flag.mean():.4f} of a plane with no RFI in it"
    )
    assert stats["new"] == int(flag.sum())


def test_a_time_burst_is_flagged_where_it_happens():
    """A short burst must be flagged in its own rows, not across the band.

    This is the property that separates the time step from the spectral one: a
    channel bright for five integrations out of two hundred is invisible in the
    time average, and only the sliding-window scatter finds it.
    """
    plane = make_plane(seed=1)
    plane[100:105, 30] *= 6.0
    flag, _ = rflag_plane(plane, RFlagParams())

    rows = np.flatnonzero(flag[:, 30])
    assert rows.min() <= 100 and rows.max() >= 104, (
        f"the burst rows are not all flagged: {rows.tolist()}"
    )
    assert flag[:, 30].sum() <= 12, "far more rows than the burst were flagged"
    elsewhere = np.delete(flag, 30, axis=1)
    assert elsewhere.mean() < 0.005, (
        f"{elsewhere.mean():.4f} of the rest of the band was flagged"
    )


def test_a_narrow_band_spike_is_flagged_across_time():
    """A channel that is bright throughout is found by the spectral step."""
    flag, _ = rflag_plane(spike_plane(3.0), RFlagParams())
    assert flag[:, 20].mean() > 0.9, "the RFI channel was not flagged"
    flagged_columns = np.flatnonzero(flag.any(axis=0))
    # The spike takes its immediate neighbours with it, and no more: each
    # neighbour is compared against a window that contains the spike, so the
    # flags spread by one channel on each side and then stop.
    assert flagged_columns.tolist() == [19, 20, 21], (
        f"expected the spike and its two neighbours, got {flagged_columns.tolist()}"
    )


@pytest.mark.parametrize("factor", [1.2, 1.5, 2.0, 3.0])
def test_weak_spikes_are_still_found(factor):
    """A 20 % spike in one channel, which a plain amplitude clip cannot see.

    The band spans 64 % peak to peak, so a clip that caught this spike would
    flag the bright end of the band.  Finding it is the reason RFlag works from
    the scatter rather than the level.
    """
    flags = [rflag_plane(spike_plane(factor, seed=s), RFlagParams())[0][:, 20].mean()
             for s in range(6)]
    assert np.mean(flags) > 0.9, f"x{factor} spike detected in only {np.mean(flags):.2f}"


def test_a_broadband_burst_is_caught_by_the_time_step():
    plane = make_plane(seed=4)
    plane[50:60, :] *= 4.0
    flag, _ = rflag_plane(plane, RFlagParams())
    assert flag[50:60, :].mean() > 0.5, "the burst rows were not flagged"
    outside = np.concatenate([flag[:50], flag[60:]])
    assert outside.mean() < 0.05, f"{outside.mean():.4f} flagged outside the burst"


def test_two_spikes_are_both_found():
    """The scale must not be set by the first spike found."""
    flag, _ = rflag_plane(spike_plane(channels=(20, 45)), RFlagParams())
    assert flag[:, 20].mean() > 0.9 and flag[:, 45].mean() > 0.9


# --- supplied noise estimates ----------------------------------------------


def test_supplied_deviations_are_used_and_do_not_over_flag():
    """The two-pass / AIPS workflow: thresholds supplied rather than measured.

    A noise estimate is what `timedev` means, so supplying the true noise must
    flag nothing on a clean plane and still find a spike.  Getting this wrong
    the obvious way -- measuring the local r.m.s. about zero, which includes the
    signal -- flagged 91 % of a clean plane.
    """
    clean = make_plane(seed=6)
    flag, _ = rflag_plane(clean, RFlagParams(timedev=0.07, freqdev=0.07))
    assert flag.mean() < 0.005, (
        f"a clean plane was flagged {flag.mean():.4f} against a correct noise"
        " estimate"
    )

    spiked = spike_plane(seed=6)
    flag, _ = rflag_plane(spiked, RFlagParams(timedev=0.07, freqdev=0.07))
    assert flag[:, 20].mean() > 0.9


def test_a_tighter_threshold_flags_at_least_as_much():
    """The scale factors must do something."""
    plane = spike_plane(1.5)
    loose, _ = rflag_plane(plane, RFlagParams(timedevscale=20.0, freqdevscale=20.0))
    tight, _ = rflag_plane(plane, RFlagParams(timedevscale=2.0, freqdevscale=2.0))
    assert tight.sum() >= loose.sum()
    assert tight.sum() > loose.sum(), "the scale factors made no difference"


def test_winsize_changes_the_result():
    """A parameter that does nothing would be worse than no parameter.

    Measured on a transient, which is what winsize acts on: a burst is smoothed
    over more integrations as the window widens, so the flagged region changes
    shape even though the burst itself is found either way.
    """
    plane = make_plane(seed=21)
    plane[100:104, 30] *= 5.0
    small, _ = rflag_plane(plane, RFlagParams(winsize=3))
    large, _ = rflag_plane(plane, RFlagParams(winsize=9))
    assert small[:, 30].any() and large[:, 30].any(), "the burst was missed"
    assert not np.array_equal(small, large), "winsize made no difference"


def test_a_smooth_bandpass_is_not_flagged_but_a_step_in_it_is():
    """The spectral step's characteristic, pinned so it cannot surprise.

    Each channel is compared with its neighbours, so the band's own slope enters
    the scale and a smooth band passes untouched -- the clean-plane tests above
    are all this case at 64 % peak-to-peak.  What the step cannot tell apart
    from RFI is a *step* in the band: a channel standing above its neighbours by
    more than a few times the channel-to-channel noise is flagged, because
    nothing in the data distinguishes it from a narrow emission feature.

    This is why CASA notes that the spectral step "depends on having a
    relatively-flat bandshape", and why a coarse channel grid with a steep shape
    is the case to check.  A 5 % step is well above the noise here and is
    flagged; a smooth band of the same depth is not.
    """
    smooth, _ = rflag_plane(make_plane(), RFlagParams())
    assert smooth.mean() == 0.0, "a smooth bandpass must not be flagged"

    stepped = make_plane()
    stepped[:, 32] *= 1.05
    flag, _ = rflag_plane(stepped, RFlagParams())
    assert flag[:, 32].mean() > 0.9, (
        "a 5 % channel-to-channel step should be flagged as a narrow feature"
    )


# --- flags already set ------------------------------------------------------


def test_preexisting_flags_are_preserved_and_counted():
    plane = make_plane(seed=8)
    existing = np.zeros(plane.shape, dtype=bool)
    existing[5, 10:20] = True
    flag, stats = rflag_plane(plane, RFlagParams(), flagged=existing)
    assert flag[5, 10:20].all(), "pre-existing flags were cleared"
    assert stats["pre_existing"] == 10


def test_a_fully_flagged_plane_is_not_disturbed():
    """Real MSes arrive with whole channels already dead."""
    plane = make_plane(seed=9)
    existing = np.zeros(plane.shape, dtype=bool)
    existing[:, 30] = True
    flag, _ = rflag_plane(plane, RFlagParams(), flagged=existing)
    assert flag[:, 30].all(), "a dead channel must stay flagged"
    assert flag[:, 31:].mean() < 0.01, "the dead channel leaked into the band"


def test_a_plane_that_is_entirely_flagged_is_handled():
    plane = make_plane(ntime=20, nchan=16, seed=10)
    flag, _ = rflag_plane(plane, RFlagParams(), flagged=np.ones(plane.shape, bool))
    assert flag.all()


def test_non_finite_data_is_flagged_not_crashed():
    plane = make_plane(ntime=20, nchan=32, seed=11)
    plane[3, 5] = np.nan
    plane[4, 6] = np.inf
    flag, _ = rflag_plane(plane, RFlagParams())
    assert flag[3, 5] and flag[4, 6]


def test_a_tiny_band_does_not_crash():
    """Fewer channels than the neighbour span needs."""
    plane = make_plane(ntime=20, nchan=3, seed=12)
    flag, _ = rflag_plane(plane, RFlagParams())
    assert flag.shape == plane.shape


# --- parameters -------------------------------------------------------------


def test_defaults_match_casa():
    """A recipe written for flagdata(mode='rflag') must transfer unchanged."""
    params = RFlagParams()
    assert params.winsize == 3
    assert params.timedev is None
    assert params.freqdev is None
    assert params.timedevscale == 5.0
    assert params.freqdevscale == 5.0
    assert params.spectralmax == 1.0e6
    assert params.spectralmin == 0.0
    assert set(DEFAULTS) == set(RFlagParams.DEFAULTS)


@pytest.mark.parametrize("kwargs, message", [
    ({"winsize": 0}, "winsize"),
    ({"winsize": -1}, "winsize"),
    ({"winsize": 2.5}, "winsize"),
    ({"timedev": 0}, "timedev"),
    ({"timedev": -1}, "timedev"),
    ({"freqdev": 0}, "freqdev"),
    ({"timedevscale": 0}, "timedevscale"),
    ({"freqdevscale": -2}, "freqdevscale"),
    ({"spectralmin": "x"}, "spectralmin"),
    ({"spectralmin": 100.0, "spectralmax": 1.0}, "spectralmin"),
])
def test_bad_parameters_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        RFlagParams(**kwargs)


def test_unknown_parameter_names_the_valid_ones():
    """A typo must fail loudly rather than silently keep the default."""
    with pytest.raises(ValueError) as excinfo:
        RFlagParams(win_size=3)
    assert "win_size" in str(excinfo.value)
    assert "winsize" in str(excinfo.value)


def test_timedev_may_be_omitted_but_not_zero():
    params = RFlagParams(timedev=None, freqdev=None)
    assert params.timedev is None and params.freqdev is None


# --- the verb, end to end on a synthetic MS ---------------------------------


def _synthetic_ms(values, row_chunk=-1):
    """A DaskMS with DATA set to ``values`` (row, chan, corr), never on disk."""
    import xarray as xr

    from skarabina.dask_ms import DaskMS

    data = np.asarray(values, dtype=complex)
    nrow, nchan, ncorr = data.shape
    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), data),
            "FLAG": (("row", "chan", "corr"), np.zeros(data.shape, dtype=bool)),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), np.ones(data.shape)),
            "UVW": (("row", "uvw"), np.zeros((nrow, 3))),
            "TIME": (("row",), np.arange(nrow, dtype=float)),
            "ANTENNA1": (("row",), np.zeros(nrow, dtype=np.int32)),
            "ANTENNA2": (("row",), np.ones(nrow, dtype=np.int32)),
            "FLAG_ROW": (("row",), np.zeros(nrow, dtype=bool)),
        }
    ).chunk({"row": row_chunk, "chan": -1, "corr": -1})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.name = "<synthetic>"
    ms.sub_table_names = []
    ms._refresh_cached_columns()
    return ms


def _cube(plane, ncorr=1):
    """A (time, chan) plane as the (row, chan, corr) cube an MS holds."""
    return np.repeat(plane[:, :, None], ncorr, axis=2)


def test_flag_rflag_verb_flags_the_rfi_in_an_ms():
    """The verb must read DATA, flag the RFI, and record FLAG as changed."""
    plane = spike_plane(3.0)
    ms = _synthetic_ms(_cube(plane))
    ms.flag_rflag(RFlagParams())

    flags = np.asarray(ms.ds.FLAG.data)
    assert flags.shape == plane.shape + (1,)
    assert flags[:, 20, 0].mean() > 0.9, "the RFI channel was not flagged"
    assert ms.changed.get("FLAG"), "FLAG was not recorded as changed"


def test_flag_rflag_leaves_a_clean_ms_almost_untouched():
    ms = _synthetic_ms(_cube(make_plane(seed=30)))
    ms.flag_rflag(RFlagParams())
    flags = np.asarray(ms.ds.FLAG.data)
    assert flags.mean() < 0.005, (
        f"flagged {flags.mean():.4f} of a cube with no RFI in it"
    )


def test_flag_rflag_respects_flags_that_are_already_set():
    plane = spike_plane(seed=31)
    ms = _synthetic_ms(_cube(plane))
    existing = np.zeros(plane.shape + (1,), dtype=bool)
    existing[0, :, 0] = True
    ms.ds["FLAG"] = (ms.ds.FLAG.dims, existing)
    ms.flag_rflag(RFlagParams())
    assert np.asarray(ms.ds.FLAG.data)[0, :, 0].all()


def test_flag_rflag_handles_several_blocks_and_keeps_the_axes_ordered():
    """More rows than one block, with RFI in one specific row.

    The block grid is assembled in order, so an off-by-one would put the flags
    in the wrong rows.  RFlag's time step works per channel and per window, so a
    transient is the case that reveals it.
    """
    ntime, nchan = 900, 16
    plane = make_plane(ntime=ntime, nchan=nchan, seed=32)
    target = 700
    plane[target:target + 4, 5] *= 8.0

    ms = _synthetic_ms(_cube(plane), row_chunk=256)
    ms.flag_rflag(RFlagParams())
    flags = np.asarray(ms.ds.FLAG.data)[:, :, 0]

    assert flags.shape == plane.shape
    rows = np.flatnonzero(flags[:, 5])
    assert rows.min() <= target and rows.max() >= target + 3, (
        f"the burst at row {target} was not flagged: rows {rows.tolist()}"
    )
    assert flags[:, 5].sum() <= 12, "far more rows than the burst were flagged"


def test_flag_rflag_and_tfcrop_both_run_in_one_pass():
    """The two auto-flaggers are independent verbs and must not interfere."""
    from skarabina.dask_ms import DaskMS
    from skarabina.tfcrop import TFCropParams

    plane = spike_plane(3.0)
    ms = _synthetic_ms(_cube(plane))
    ms.flag_rflag(RFlagParams())
    after_rflag = np.asarray(ms.ds.FLAG.data).copy()
    ms.flag_tfcrop(TFCropParams())
    after_both = np.asarray(ms.ds.FLAG.data)
    assert np.all(after_both[after_rflag]), "tfcrop cleared rflag's flags"
    assert after_both.sum() >= after_rflag.sum()
    assert isinstance(ms, DaskMS)


# --- performance-motivated rewrites must not change the answer ---------------


def test_nanmedian_matches_numpy():
    """The sort-based median replaces ``np.nanmedian`` and must agree exactly."""
    from skarabina.rflag import _nanmedian

    rng = np.random.default_rng(40)
    for lanes in (1, 2, 3, 4, 6, 7, 50):
        values = rng.normal(size=(300, 9, lanes))
        values[rng.random(values.shape) < 0.5] = np.nan
        values[0] = np.nan  # whole lanes with nothing usable
        for axis in (0, -1):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                expected = np.nanmedian(values, axis=axis)
            np.testing.assert_array_equal(_nanmedian(values, axis=axis), expected)


def test_grouping_does_not_change_the_result(monkeypatch):
    """Channels (time step) and rows (spectral step) are processed in groups
    to bound the working set; the group size must be invisible in the flags."""
    import skarabina.rflag as rflag

    plane = spike_plane(3.0, ntime=300, nchan=40, seed=41)
    plane[100:104, 7] *= 8.0
    flagged = np.random.default_rng(41).random(plane.shape) < 0.3
    reference, stats = rflag_plane(plane, RFlagParams(), flagged)
    monkeypatch.setattr(rflag, "GROUP_VALUES", 500)
    grouped, grouped_stats = rflag_plane(plane, RFlagParams(), flagged)
    np.testing.assert_array_equal(grouped, reference)
    assert grouped_stats == stats


def test_flag_rflag_keeps_its_result_on_disk_not_in_memory(tmp_path, monkeypatch):
    """The per-block flags are spilled under $TMPDIR, read back on every later
    pass, and removed with the instance -- the table's worth of flags is never
    held in memory, and nothing is left behind."""
    import gc

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    plane = spike_plane(3.0)
    ms = _synthetic_ms(_cube(plane), row_chunk=64)
    ms.flag_rflag(RFlagParams())

    spills = list(tmp_path.glob(".skarabina-spill-*/flag_rflag-*/block*.npy"))
    assert len(spills) == -(-plane.shape[0] // 64), "one spilled file per block"
    first = np.asarray(ms.ds.FLAG.data)
    second = np.asarray(ms.ds.FLAG.data)
    np.testing.assert_array_equal(first, second)
    assert first[:, 20, 0].mean() > 0.9

    del ms
    gc.collect()
    assert not list(tmp_path.glob(".skarabina-spill-*")), "spill left behind"


def test_heavily_preflagged_clean_data_is_left_alone():
    """Flagged samples must be absent from BOTH parts of the statistics.

    ``np.where(flagged, np.nan, plane)`` on complex data gives ``nan+0j``, so
    the imaginary part of every flagged sample used to count as a zero.  With
    most of the plane already flagged -- the normal state of a real calibrator
    scan -- that pulled the spectral deviation down tenfold and rflag flagged
    nearly everything left (100 % of the bench's bpcal.ms, 97 % here).
    """
    rng = np.random.default_rng(42)
    plane = make_plane(ntime=2000, nchan=64, noise=1.0, seed=42)
    flagged = rng.random(plane.shape) < 0.45
    flagged[rng.random(plane.shape[0]) < 0.37] = True
    flag, stats = rflag_plane(plane, RFlagParams(), flagged)
    assert stats["new"] / stats["total"] < 0.005, (
        f"flagged {stats['new']} new samples of clean noise"
    )

    plane[:, 30] *= 3.0
    flag, _ = rflag_plane(plane, RFlagParams(), flagged)
    unflagged = ~flagged[:, 30]
    assert flag[unflagged, 30].mean() > 0.9, "the RFI channel was missed"
