# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for the TFCrop algorithm in :mod:`skarabina.tfcrop`.

These test the algorithm, not the wiring: synthetic planes with RFI at known
positions, so recall and false-positive rate can both be measured.  A flagger
that flags everything scores 100% recall and is useless, so every recall
assertion here is paired with a false-positive bound.
"""
import numpy as np
import pytest

from skarabina.tfcrop import (
    FLAG_DIMENSIONS,
    TFCropParams,
    _baseline_along_rows,
    bandpass_template,
    _piece_edges,
    _safe_template,
    flag_1d,
    mad_sigma,
    robust_fit,
    tfcrop_plane,
)


def bandpass(nchan, rng=None):
    """A smooth, sloping band shape -- the thing that must NOT be flagged."""
    x = np.linspace(0, 2.5, nchan)
    return 10.0 + 4.0 * np.cos(x) + 2.0 * x / 2.5


def make_plane(ntime=60, nchan=256, noise=0.03, seed=0):
    """A clean plane: one smooth bandpass, shared by every timestep."""
    rng = np.random.default_rng(seed)
    shape = np.outer(np.ones(ntime), bandpass(nchan))
    return shape * (1.0 + rng.normal(0, noise, (ntime, nchan)))


def mask_without(shape, rows=(), cols=()):
    """Boolean mask of the pixels *not* in the given rows/columns."""
    mask = np.ones(shape, dtype=bool)
    mask[list(rows), :] = False
    mask[:, list(cols)] = False
    return mask


# --- the robust fit ---------------------------------------------------------


def test_robust_fit_rejects_spikes_a_plain_fit_follows():
    """The whole point of the robust fit: it must sit at the base of the RFI.

    A least-squares polynomial is dragged up by bright narrow spikes; the
    robust fit must be far closer to the true bandpass, and must actually reject
    the spikes rather than merely fitting them.
    """
    nchan = 256
    rng = np.random.default_rng(1)
    true_band = bandpass(nchan)
    data = true_band + rng.normal(0, 0.05, nchan)
    spikes = np.array([60, 61, 150, 151, 152, 200])
    data[spikes] += np.array([8.0, 6.0, 10.0, 9.0, 7.0, 12.0])

    x = np.arange(nchan, dtype=float)
    fitted, keep = robust_fit(x, data, npieces=7, degree=3)
    naive = np.polyval(np.polyfit(x, data, 3), x)

    robust_error = np.abs(fitted - true_band).max()
    naive_error = np.abs(naive - true_band).max()
    assert robust_error < naive_error / 3, (
        f"robust fit error {robust_error:.3f} is not clearly better than the"
        f" plain fit's {naive_error:.3f}"
    )
    assert not keep[spikes].any(), (
        f"spikes {spikes[keep[spikes]].tolist()} survived the robust fit"
    )


def test_robust_fit_follows_a_smooth_bandpass():
    """With no RFI the fit must track the bandpass closely, not flatten it.

    This band is noiseless, so the residuals of a good fit underflow and a
    purely relative rejection rule would reject every point.  Rejecting a clean
    band is the failure this guards: the threshold floor exists precisely for
    data that fits its own polynomial too well for a relative test to be
    meaningful.
    """
    nchan = 256
    true_band = bandpass(nchan)
    fitted, keep = robust_fit(
        np.arange(nchan, dtype=float), true_band, npieces=7, degree=3
    )
    assert keep.all(), (
        f"a clean bandpass must not be rejected; {int((~keep).sum())} channel(s)"
        " were"
    )
    # The piecewise cubics are fitted per span, so a small departure at a piece
    # boundary is expected; the gross band shape must be followed.
    assert np.abs(fitted - true_band).max() < 0.05 * np.ptp(true_band), (
        f"max error {np.abs(fitted - true_band).max():.4f} on a band of width"
        f" {np.ptp(true_band):.3f}"
    )


def test_robust_fit_survives_a_band_that_is_all_rfi_in_one_piece():
    """A piece with every point rejected must not leave a hole in the model."""
    nchan = 210
    true_band = bandpass(nchan)
    data = true_band.copy()
    data[0:30] = np.nan  # one piece's worth of unusable data
    fitted, _ = robust_fit(
        np.arange(nchan, dtype=float), data, npieces=7, degree=3
    )
    assert np.isfinite(fitted).all(), "model has holes where a piece was empty"


def test_robust_fit_handles_an_all_nan_band():
    """Nothing to fit: a flat model, and no exception."""
    data = np.full(64, np.nan)
    fitted, keep = robust_fit(
        np.arange(64, dtype=float), data, npieces=7, degree=3
    )
    assert not keep.any()
    assert np.isfinite(fitted).all()


def test_robust_fit_converges_and_does_not_diverge():
    """The fit must settle, and must never blow up on the way.

    The piece count grows over the passes, so the model legitimately keeps
    refining; what must not happen is the rejection running away, which is what
    an unguarded extrapolation did here -- one pass reached 381 on a band whose
    values run 7 to 16, then rejected almost everything.  Two properties: no
    pass may leave the model wildly wrong, and the later passes must agree.
    """
    nchan = 256
    rng = np.random.default_rng(2)
    truth = bandpass(nchan)
    data = truth + rng.normal(0, 0.05, nchan)
    data[[40, 41, 200]] += np.array([6.0, 5.0, 7.0])
    x = np.arange(nchan, dtype=float)

    errors = []
    for passes in range(1, 9):
        fitted, keep = robust_fit(
            x, data, npieces=7, degree=3, n_iterations=passes
        )
        error = np.abs(fitted - truth).max()
        errors.append(error)
        assert error < 1.0, (
            f"pass {passes} diverged: model is off by {error:.1f} on a band"
            f" spanning {np.ptp(truth):.1f}"
        )
        assert keep[[40, 41, 200]].sum() == 0, f"pass {passes} kept an RFI spike"

    settled, kept = robust_fit(x, data, npieces=7, degree=3, n_iterations=5)
    later, kept_later = robust_fit(x, data, npieces=7, degree=3, n_iterations=8)
    # Not identical: the later passes still add pieces, so the model keeps
    # sharpening.  What matters is that it has stopped *moving* materially.
    drift = np.abs(settled - later).max()
    assert drift < 0.01, (
        f"the fit was still moving by {drift:.4f} between five and eight passes"
    )
    assert kept.sum() == kept_later.sum(), (
        "the set of rejected channels is still changing between passes"
    )


# --- the scatter estimate ---------------------------------------------------


def test_mad_sigma_is_a_scalar_without_an_axis():
    """Callers do float() on this, so a 0-d array would break them."""
    value = mad_sigma(np.array([1.0, 2.0, 3.0, 4.0]))
    assert np.isscalar(value) or np.ndim(value) == 0
    assert float(value) > 0


def test_mad_sigma_ignores_outliers_that_inflate_the_rms():
    """The reason MAD is used rather than the standard deviation."""
    rng = np.random.default_rng(3)
    clean = rng.normal(0, 1.0, 1000)
    spiked = clean.copy()
    spiked[::100] += 50.0
    assert mad_sigma(spiked) < 2.0 * mad_sigma(clean), (
        "a few bright spikes must not inflate the robust scatter"
    )
    assert spiked.std() > 3.0 * clean.std()


# --- the iterative flagging -------------------------------------------------


def test_flag_1d_finds_injected_outliers():
    rng = np.random.default_rng(4)
    plane = 1.0 + rng.normal(0, 0.02, 1000)
    injected = np.array([10, 200, 201, 500])
    plane[injected] += 0.5

    flag, sigma = flag_1d(plane, cutoff=4.0)
    found = set(np.flatnonzero(flag).tolist())
    assert set(injected.tolist()) <= found
    assert len(found - set(injected.tolist())) <= 2, (
        f"too many false positives: {sorted(found - set(injected.tolist()))}"
    )
    assert sigma == pytest.approx(0.02, rel=0.25)


def test_flag_1d_is_adaptive_about_its_scatter():
    """The first pass sees RFI in the scatter; later passes see less.

    This is the published "adaptive stddev": the estimate falls as outliers are
    removed, so the effective threshold tightens and weaker outliers become
    visible.  A single pass must therefore estimate a larger scatter.
    """
    rng = np.random.default_rng(5)
    plane = 1.0 + rng.normal(0, 0.02, 2000)
    plane[::50] += 0.6  # strong, frequent outliers

    _, first_sigma = flag_1d(plane, cutoff=4.0, n_iterations=1)
    _, final_sigma = flag_1d(plane, cutoff=4.0, n_iterations=5)
    assert final_sigma < first_sigma, (
        "the adaptive scatter should shrink once the outliers are removed"
    )


def test_flag_1d_leaves_a_clean_plane_alone():
    """A clean plane at a 4-sigma cutoff: only the Gaussian tail may be flagged."""
    rng = np.random.default_rng(6)
    plane = 1.0 + rng.normal(0, 0.02, 20000)
    flag, _ = flag_1d(plane, cutoff=4.0)
    assert flag.mean() < 0.001, f"flagged {flag.mean():.4f} of a clean plane"


def test_flag_1d_flags_non_finite_points():
    plane = np.array([1.0, 1.0, np.nan, 1.0, np.inf, 1.0])
    flag, _ = flag_1d(plane, cutoff=4.0)
    assert flag[2] and flag[4]


# --- piece handling ---------------------------------------------------------


def test_piece_edges_never_produce_empty_pieces():
    """More pieces than samples must not create zero-width spans."""
    edges = _piece_edges(3, 7)
    assert len(edges) == 4
    assert (np.diff(edges) > 0).all()


def test_piece_edges_cover_the_whole_range():
    edges = _piece_edges(512, 7)
    assert edges[0] == 0 and edges[-1] == 512
    assert len(edges) == 8


def test_safe_template_replaces_only_unusable_values():
    fallback = np.array([1.0, 2.0, 3.0, 4.0])
    template = np.array([1.0, 0.0, np.nan, 4.0])
    out = _safe_template(template, fallback)
    assert out[0] == 1.0 and out[3] == 4.0
    assert out[1] == 2.0 and out[2] == 3.0


# --- the full plane ---------------------------------------------------------


def test_tfcrop_finds_narrowband_rfi_without_flagging_the_band():
    """The headline property: RFI columns found, smooth bandpass left alone.

    A plain amplitude clip cannot do this -- the band edge is brighter than a
    weak spike in the middle -- which is the entire justification for the
    bandpass-flattening step.
    """
    ntime, nchan = 60, 256
    plane = make_plane(ntime, nchan)
    rfi = np.array([40, 41, 150, 200])
    for col in rfi:
        plane[:, col] *= 2.5
    clean = mask_without(plane.shape, cols=rfi)

    flag, stats = tfcrop_plane(plane, TFCropParams(flagdimension="freq"))
    flagged_columns = flag.any(axis=0)
    assert all(flagged_columns[col] for col in rfi), (
        "every RFI column must be detected"
    )
    # The smooth band must survive: a clip at any level that catches these
    # spikes would have flagged the bright end of the band.
    assert flag[clean].mean() < 0.15, (
        f"flagged {flag[clean].mean():.3f} of the clean region"
    )
    assert stats["new"] == int(flag.sum())


def test_tfcrop_finds_time_variable_rfi():
    """The time direction: a few bad integrations across every channel."""
    ntime, nchan = 60, 256
    plane = make_plane(ntime, nchan)
    bad_rows = np.array([7, 8, 50])
    for row in bad_rows:
        plane[row, :] *= 1.8
    clean = mask_without(plane.shape, rows=bad_rows)

    flag, _ = tfcrop_plane(plane, TFCropParams(flagdimension="time"))
    flagged_rows = flag.any(axis=1)
    assert all(flagged_rows[row] for row in bad_rows)
    assert flag[clean].mean() < 0.02, (
        f"the time direction flagged {flag[clean].mean():.4f} of clean data;"
        " it should be the precise one"
    )


def test_tfcrop_flags_both_kinds_together():
    ntime, nchan = 60, 256
    plane = make_plane(ntime, nchan)
    rfi_cols = np.array([40, 150])
    rfi_rows = np.array([7, 50])
    for col in rfi_cols:
        plane[:, col] *= 2.5
    for row in rfi_rows:
        plane[row, :] *= 1.8
    clean = mask_without(plane.shape, rows=rfi_rows, cols=rfi_cols)

    flag, _ = tfcrop_plane(plane, TFCropParams(flagdimension="freqtime"))
    assert all(flag.any(axis=0)[col] for col in rfi_cols)
    assert all(flag.any(axis=1)[row] for row in rfi_rows)
    assert flag[clean].mean() < 0.15


def test_tfcrop_leaves_a_completely_clean_plane_almost_untouched():
    """No RFI at all: the flagger must not invent any."""
    plane = make_plane(40, 128, noise=0.02, seed=11)
    flag, _ = tfcrop_plane(plane, TFCropParams(flagdimension="freqtime"))
    assert flag.mean() < 0.01, (
        f"flagged {flag.mean():.4f} of a plane containing only smooth bandpass"
        " and noise"
    )


def test_tfcrop_preserves_flags_that_were_already_set():
    """The returned flag plane is the complete one, existing flags included."""
    plane = make_plane(20, 64)
    existing = np.zeros(plane.shape, dtype=bool)
    existing[3, 10:20] = True
    flag, stats = tfcrop_plane(plane, TFCropParams(), flagged=existing)
    assert flag[3, 10:20].all(), "pre-existing flags were cleared"
    assert stats["pre_existing"] == 10


def test_tfcrop_ignores_already_flagged_data_when_fitting():
    """Flagged points must be excluded from the statistics, not merely kept.

    Measured directly against the known bandpass.  The contaminated row here is
    100x brighter than the rest, so if it entered the time average the template
    would be wrong by ~36 on a band whose values run 7 to 16 -- and every clean
    pixel would then be flagged against it.  Marking the row flagged must
    recover the bandpass, and must not disturb the clean rows.
    """
    ntime, nchan = 40, 128
    plane = make_plane(ntime, nchan, seed=12)
    contaminated = plane.copy()
    contaminated[0, :] *= 100.0
    truth = bandpass(nchan)

    masked = np.zeros(contaminated.shape, dtype=bool)
    masked[0, :] = True
    recovered = bandpass_template(contaminated, masked, TFCropParams())
    assert np.abs(recovered - truth).max() < 0.15, (
        f"the flagged row still entered the bandpass fit: template is off by"
        f" {np.abs(recovered - truth).max():.2f}"
    )

    # The control must be visibly damaged, or the assertion above proves
    # nothing about masking.
    unmasked = bandpass_template(
        contaminated, np.zeros(contaminated.shape, dtype=bool), TFCropParams()
    )
    assert np.abs(unmasked - truth).max() > 10.0, (
        "the control run was not perturbed, so masking is not being measured"
    )


def test_tfcrop_every_flagdimension_runs_and_flags_the_rfi():
    """Each dimension must find the RFI it is capable of seeing.

    A uniformly bright *column* is invisible to the time direction by
    construction: that direction averages over frequency, so a channel whose
    brightness is constant in time is part of the mean, not a deviation from it.
    Only `freq`-containing modes can detect it.  A bright *row* is likewise
    invisible to the frequency direction.  Each mode is checked against the RFI
    it can see, so a mode that silently does nothing still fails.
    """
    column_rfi = np.array([30, 31])
    row_rfi = np.array([5, 6])
    plane = make_plane(40, 128)
    for col in column_rfi:
        plane[:, col] *= 3.0
    for row in row_rfi:
        plane[row, :] *= 2.0

    for dimension in FLAG_DIMENSIONS:
        flag, _ = tfcrop_plane(plane, TFCropParams(flagdimension=dimension))
        assert flag.any(), f"flagdimension={dimension} flagged nothing"
        if "freq" in dimension:
            assert all(flag[:, col].any() for col in column_rfi), (
                f"flagdimension={dimension} missed the RFI columns"
            )
        if "time" in dimension:
            assert all(flag[row, :].any() for row in row_rfi), (
                f"flagdimension={dimension} missed the RFI rows"
            )


def test_tfcrop_window_statistics_add_flags_without_destroying_the_plane():
    """`usewindowstats` is additive: it must not clear the fit-based flags."""
    plane = make_plane(40, 128)
    plane[:, 30] *= 3.0
    base, _ = tfcrop_plane(plane, TFCropParams(usewindowstats="none"))
    for mode in ("sum", "std", "both"):
        with_window, _ = tfcrop_plane(
            plane, TFCropParams(usewindowstats=mode, halfwin=2)
        )
        assert np.all(with_window[base]), (
            f"usewindowstats={mode} removed flags the fit had set"
        )


def test_tfcrop_single_direction_modes_are_subset_of_the_combined():
    plane = make_plane(40, 128)
    plane[:, 30] *= 3.0
    plane[5, :] *= 2.0
    both, _ = tfcrop_plane(plane, TFCropParams(flagdimension="freqtime"))
    freq, _ = tfcrop_plane(plane, TFCropParams(flagdimension="freq"))
    time, _ = tfcrop_plane(plane, TFCropParams(flagdimension="time"))
    assert np.all(both[freq]) and np.all(both[time])
    assert freq.sum() + time.sum() >= both.sum()


def test_baseline_along_rows_removes_the_bandpass_per_column():
    """Each column's baseline differs, so a single shared level cannot work.

    This pins the bug the first implementation had: subtracting one row-template
    left every column offset by its own bandpass value, and the time direction
    then flagged whole columns of clean data.
    """
    plane = make_plane(30, 64)
    baseline = _baseline_along_rows(plane, np.zeros(plane.shape, bool), 7, 1)
    truth = bandpass(64)
    assert np.allclose(baseline, truth, rtol=0.05), (
        "per-column baselines should recover the bandpass"
    )


# --- parameters -------------------------------------------------------------


def test_defaults_match_casa():
    """A recipe written for flagdata(mode='tfcrop') must transfer unchanged."""
    params = TFCropParams()
    assert params.timecutoff == 4.0
    assert params.freqcutoff == 3.0
    assert params.timefit == "line"
    assert params.freqfit == "poly"
    assert params.maxnpieces == 7
    assert params.flagdimension == "freqtime"
    assert params.usewindowstats == "none"
    assert params.halfwin == 1
    assert params.combinescans is False


@pytest.mark.parametrize("kwargs, message", [
    ({"maxnpieces": 0}, "maxnpieces"),
    ({"maxnpieces": 8}, "maxnpieces"),
    ({"maxnpieces": 2.5}, "maxnpieces"),
    ({"halfwin": 0}, "halfwin"),
    ({"halfwin": 4}, "halfwin"),
    ({"timecutoff": 0}, "timecutoff"),
    ({"freqcutoff": -1}, "freqcutoff"),
    ({"flagdimension": "spacetime"}, "flagdimension"),
    ({"usewindowstats": "median"}, "usewindowstats"),
    ({"freqfit": "spline"}, "freqfit"),
    ({"timefit": "spline"}, "timefit"),
    ({"combinescans": "yes"}, "combinescans"),
])
def test_bad_parameters_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        TFCropParams(**kwargs)


def test_unknown_parameter_names_the_valid_ones():
    """A typo must fail loudly rather than silently keep the default."""
    with pytest.raises(ValueError) as excinfo:
        TFCropParams(maxnpices=3)
    assert "maxnpices" in str(excinfo.value)
    assert "maxnpieces" in str(excinfo.value)


def test_maxnpieces_changes_the_fit():
    """A parameter that does nothing would be worse than no parameter."""
    nchan = 256
    rng = np.random.default_rng(7)
    data = bandpass(nchan) + rng.normal(0, 0.05, nchan)
    x = np.arange(nchan, dtype=float)
    one, _ = robust_fit(x, data, npieces=1, degree=3)
    seven, _ = robust_fit(x, data, npieces=7, degree=3)
    assert not np.allclose(one, seven)
    assert np.abs(seven - data).mean() < np.abs(one - data).mean()


# --- the verb, end to end on a synthetic MS ---------------------------------


def make_rfi_plane(ntime=64, nchan=128, seed=0):
    """A (time, chan) plane with narrow-band RFI and one bright integration."""
    rng = np.random.default_rng(seed)
    base = np.outer(np.ones(ntime), bandpass(nchan))
    plane = base * (1.0 + rng.normal(0, 0.03, (ntime, nchan)))
    plane[:, 30] *= 3.0
    plane[5, :] *= 2.0
    return plane


def _synthetic_ms(values, row_chunk=-1):
    """A DaskMS with DATA set to ``values`` (row, chan, corr), never on disk.

    ``row_chunk`` smaller than the row count makes the cube span several blocks,
    which is what exercises the block assembly in ``flag_tfcrop``.
    """
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


def _plane_as_cube(plane, ncorr=1):
    """A (time, chan) plane as the (row, chan, corr) cube an MS holds."""
    return np.repeat(plane[:, :, None], ncorr, axis=2)


def _rfi_cube(ntime=64, nchan=128, seed=0, ncorr=1):
    """A cube with narrow-band RFI in one channel and a bright row."""
    return _plane_as_cube(make_rfi_plane(ntime, nchan, seed=seed), ncorr)


def test_flag_tfcrop_verb_flags_the_rfi_in_an_ms():
    """The verb must read DATA, flag the RFI, and mark FLAG as changed."""
    cube = _rfi_cube()
    ms = _synthetic_ms(cube)
    ms.flag_tfcrop(TFCropParams())

    flags = np.asarray(ms.ds.FLAG.data)
    assert flags.shape == cube.shape
    assert flags.any(), "the verb flagged nothing"
    assert flags[:, 30].any(), "the RFI channel was not flagged"
    assert flags[5, :].any(), "the bright row was not flagged"
    assert ms.changed.get("FLAG"), "FLAG was not recorded as changed"


def test_flag_tfcrop_leaves_a_clean_ms_almost_untouched():
    rng = np.random.default_rng(1)
    base = np.outer(np.ones(64), bandpass(128))
    plane = base * (1.0 + rng.normal(0, 0.03, (64, 128)))
    ms = _synthetic_ms(_plane_as_cube(plane))
    ms.flag_tfcrop(TFCropParams())
    flags = np.asarray(ms.ds.FLAG.data)
    assert flags.mean() < 0.02, (
        f"flagged {flags.mean():.4f} of a cube with no RFI in it"
    )


def test_flag_tfcrop_respects_flags_that_are_already_set():
    """Existing flags are excluded from the fits and survive the operation."""
    cube = _rfi_cube(seed=3)
    ms = _synthetic_ms(cube)
    existing = np.zeros(cube.shape, dtype=bool)
    existing[0, :] = True
    ms.ds["FLAG"] = (ms.ds.FLAG.dims, existing)
    ms.flag_tfcrop(TFCropParams())

    flags = np.asarray(ms.ds.FLAG.data)
    assert flags[0, :].all(), "pre-existing flags were cleared"


def test_flag_tfcrop_handles_a_cube_shorter_than_a_block():
    """A single block covering every row: the common case for a small MS."""
    cube = _rfi_cube(ntime=8, nchan=32, seed=4)
    ms = _synthetic_ms(cube)
    ms.flag_tfcrop(TFCropParams())
    assert np.asarray(ms.ds.FLAG.data).shape == cube.shape


def test_flag_tfcrop_handles_data_split_across_several_blocks():
    """More rows than one block: the per-block planes must reassemble in order.

    The block grid is transposed on the way in and back on the way out, so an
    off-by-one in the reshape would show up as flags landing in the wrong rows.
    RFI placed in one specific row makes that checkable.
    """
    ntime, nchan = 400, 8
    rng = np.random.default_rng(5)
    base = np.outer(np.ones(ntime), bandpass(nchan))
    plane = base * (1.0 + rng.normal(0, 0.02, (ntime, nchan)))
    target = 250
    plane[target, :] *= 5.0
    cube = _plane_as_cube(plane)

    ms = _synthetic_ms(cube)
    ms.flag_tfcrop(TFCropParams(flagdimension="time"))
    flags = np.asarray(ms.ds.FLAG.data)
    assert flags.shape == cube.shape
    assert flags[target, :].any(), f"row {target} was not flagged"
    # the bright row must stand out, not a neighbour
    assert flags[target, :].sum() > flags[np.arange(ntime) != target, :].sum()


def test_flag_tfcrop_keeps_the_time_and_band_axes_the_right_way_round():
    """The block layout must be (time, chan, corr) -- not (time, corr, chan).

    This is the regression test for a bug that survived every other test here,
    because they all fit a single block and so never depended on how a block is
    sliced.  Transposing the chan and corr axes made each block a plane of two
    channels rather than the whole band, the bandpass fit had nothing to fit,
    and *every* visibility in the MS came back flagged -- 100%, from a change
    that looked like tidying.

    The fingerprint is the shape of the RFI: a bright channel appears in every
    time sample of that channel and in every correlation, so a correct run flags
    a band of channels and leaves most rows clean.  With the axes swapped, the
    "channels" are the correlations and the flags spread across the row axis.
    """
    ntime, nchan, ncorr = 64, 32, 2
    rng = np.random.default_rng(7)
    base = np.outer(np.ones(ntime), bandpass(nchan))
    plane = base * (1.0 + rng.normal(0, 0.02, (ntime, nchan)))
    plane[:, 9] *= 4.0
    cube = np.repeat(plane[:, :, None], ncorr, axis=2)

    ms = _synthetic_ms(cube, row_chunk=16)
    ms.flag_tfcrop(TFCropParams(flagdimension="freq"))
    flags = np.asarray(ms.ds.FLAG.data)

    assert flags.shape == cube.shape
    assert flags.mean() < 0.5, (
        f"{flags.mean():.1%} of the cube was flagged: the block axes are"
        " probably transposed"
    )
    # the RFI channel is flagged in essentially every row and correlation ...
    assert flags[:, 9, :].mean() > 0.9, "the RFI channel was not flagged"
    # ... and almost nothing else is
    others = np.delete(flags, 9, axis=1)
    assert others.mean() < 0.1, (
        f"the clean channels were flagged too ({others.mean():.1%})"
    )


def test_tfcrop_copes_with_a_column_that_is_entirely_flagged():
    """Real MSes arrive with parts of the band already dead.

    An all-flagged column has no data to average, so the bandpass fit is handed
    a NaN there.  It must degrade gracefully -- the channel stays flagged and
    the rest of the band is still processed -- rather than flagging everything
    or failing.
    """
    import warnings

    plane = make_plane(40, 128)
    existing = np.zeros(plane.shape, dtype=bool)
    existing[:, 60] = True

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        flag, stats = tfcrop_plane(plane, TFCropParams(), flagged=existing)

    assert flag[:, 60].all(), "a dead channel must stay flagged"
    assert flag[:, 61:].mean() < 0.05, (
        f"the dead channel leaked into the rest of the band:"
        f" {flag[:, 61:].mean():.3f} flagged"
    )
    assert stats["pre_existing"] == plane.shape[0]
