# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Validate the coarsen-based averaging against pure-numpy references.

These tests construct a DaskMS with a small synthetic, dask-backed dataset
that uses chunk sizes *not* divisible by the averaging factor — the case that
exposed chunk fragmentation in the old reshape-based implementation.
"""
import numpy as np
import pytest
import xarray as xr

from skarabina.dask_ms import DaskMS


def _make_ms(nrow, nchan, ncorr, row_chunk, chan_chunk):
    rng = np.random.default_rng(0)
    data = rng.standard_normal((nrow, nchan, ncorr)) + 1j * rng.standard_normal(
        (nrow, nchan, ncorr)
    )
    data = data.astype(np.complex128)
    flag = rng.random((nrow, nchan, ncorr)) > 0.7
    weight = rng.random((nrow, nchan, ncorr)).astype(np.float64) + 0.1
    sigma = (rng.random((nrow, nchan, ncorr)).astype(np.float64) + 0.1) * 2.0
    uvw = rng.standard_normal((nrow, 3))
    time = np.arange(nrow, dtype=np.float64)
    interval = np.ones(nrow, dtype=np.float64) * 2.0
    exposure = np.ones(nrow, dtype=np.float64) * 2.0
    ant = (np.arange(nrow) % 4).astype(np.int32)

    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), data),
            "FLAG": (("row", "chan", "corr"), flag),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), weight),
            "SIGMA_SPECTRUM": (("row", "chan", "corr"), sigma),
            "UVW": (("row", "uvw"), uvw),
            "TIME": (("row",), time),
            "INTERVAL": (("row",), interval),
            "EXPOSURE": (("row",), exposure),
            "ANTENNA1": (("row",), ant),
            "ANTENNA2": (("row",), ant),
            "FLAG_ROW": (("row",), np.zeros(nrow, bool)),
        },
    )
    # chunk so chunks are NOT divisible by typical factors (mismatched blocks)
    ds = ds.chunk({"row": row_chunk, "chan": chan_chunk, "corr": ncorr})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.chan_freq_hz = None
    ms.name = "<synthetic>"
    return ms


# ---- numpy references ----

def _ref_freq_data(data, flag, factor):
    nrow, nchan, ncorr = data.shape
    n_full = nchan // factor
    n_rem = nchan % factor
    trim = n_full * factor
    out = []
    for i in range(n_full):
        d = data[:, i * factor : (i + 1) * factor, :]
        f = flag[:, i * factor : (i + 1) * factor, :]
        num = np.where(f, 0, d).sum(1)
        den = (~f).sum(1).astype(float)
        den[den == 0] = 1
        out.append(num / den)
    if n_rem:
        d = data[:, trim:, :]
        f = flag[:, trim:, :]
        num = np.where(f, 0, d).sum(1)
        den = (~f).sum(1).astype(float)
        den[den == 0] = 1
        out.append(num / den)
    return np.stack(out, axis=1)


def _ref_freq_flag(flag, factor):
    nrow, nchan, ncorr = flag.shape
    n_full = nchan // factor
    n_rem = nchan % factor
    trim = n_full * factor
    out = [np.any(flag[:, i * factor : (i + 1) * factor, :], 1) for i in range(n_full)]
    if n_rem:
        out.append(np.any(flag[:, trim:, :], 1))
    return np.stack(out, axis=1)


def _ref_time_data(data, flag, factor):
    nrow = data.shape[0] // factor * factor
    d = data[:nrow].reshape(-1, factor, data.shape[1], data.shape[2])
    f = flag[:nrow].reshape(-1, factor, data.shape[1], data.shape[2])
    num = np.where(f, 0, d).sum(1)
    den = (~f).sum(1).astype(float)
    den[den == 0] = 1
    return num / den


def _ref_time_flag(flag, factor):
    nrow = flag.shape[0] // factor * factor
    return np.any(flag[:nrow].reshape(-1, factor, flag.shape[1], flag.shape[2]), axis=1)


# ---- numpy references: WEIGHT_SPECTRUM, SIGMA_SPECTRUM, metadata ----

def _ref_freq_weight(weight, flag, factor):
    """WEIGHT_SPECTRUM over channels: sum of unflagged weights (incl. tail)."""
    nrow, nchan, ncorr = weight.shape
    n_full = nchan // factor
    n_rem = nchan % factor
    trim = n_full * factor
    out = []
    for i in range(n_full):
        slc = slice(i * factor, (i + 1) * factor)
        out.append(np.sum(np.where(flag[:, slc, :], 0, weight[:, slc, :]), axis=1))
    if n_rem:
        out.append(np.sum(np.where(flag[:, trim:, :], 0, weight[:, trim:, :]), axis=1))
    return np.stack(out, axis=1)


def _ref_freq_sigma(sigma, flag, factor):
    """SIGMA_SPECTRUM over channels: sqrt(1 / sum(1/sigma^2)) over unflagged."""
    nrow, nchan, ncorr = sigma.shape
    n_full = nchan // factor
    n_rem = nchan % factor
    trim = n_full * factor
    out = []
    for i in range(n_full):
        slc = slice(i * factor, (i + 1) * factor)
        inv_var = np.where(flag[:, slc, :], 0, 1.0 / (sigma[:, slc, :] ** 2))
        sum_inv = np.sum(inv_var, axis=1)
        sum_safe = np.where(sum_inv == 0, 1, sum_inv)
        out.append(np.sqrt(1.0 / sum_safe))
    if n_rem:
        inv_var = np.where(flag[:, trim:, :], 0, 1.0 / (sigma[:, trim:, :] ** 2))
        sum_inv = np.sum(inv_var, axis=1)
        sum_safe = np.where(sum_inv == 0, 1, sum_inv)
        out.append(np.sqrt(1.0 / sum_safe))
    return np.stack(out, axis=1)


def _ref_time_weight(weight, flag, factor):
    """WEIGHT_SPECTRUM over rows: sum of unflagged weights."""
    nrow = weight.shape[0] // factor * factor
    f = flag[:nrow].reshape(-1, factor, weight.shape[1], weight.shape[2])
    w = weight[:nrow].reshape(-1, factor, weight.shape[1], weight.shape[2])
    return np.sum(np.where(f, 0, w), axis=1)


def _ref_time_sigma(sigma, flag, factor):
    """SIGMA_SPECTRUM over rows: sqrt(1 / sum(1/sigma^2)) over unflagged."""
    nrow = sigma.shape[0] // factor * factor
    f = flag[:nrow].reshape(-1, factor, sigma.shape[1], sigma.shape[2])
    s = sigma[:nrow].reshape(-1, factor, sigma.shape[1], sigma.shape[2])
    inv_var = np.where(f, 0, 1.0 / (s ** 2))
    sum_inv = np.sum(inv_var, axis=1)
    sum_safe = np.where(sum_inv == 0, 1, sum_inv)
    return np.sqrt(1.0 / sum_safe)


def _ref_time_metadata(values, flag_row, flag, factor, op):
    """Masked mean/sum over rows, excluding fully-flagged rows.

    A row is excluded if FLAG_ROW is True OR every visibility in FLAG is
    True (same definition as optimize()).  ``op`` is ``"mean"`` or
    ``"sum"``.  ``values`` has shape (nrow,) or (nrow, k); ``flag`` has
    shape (nrow, nchan, ncorr); ``flag_row`` has shape (nrow,).
    """
    nrow = values.shape[0] // factor * factor
    v = values[:nrow]
    all_data_flagged = np.all(flag[:nrow], axis=(1, 2))
    row_bad = np.logical_or(flag_row[:nrow], all_data_flagged)
    row_good = np.logical_not(row_bad).astype(np.float64)
    # Broadcast good-mask across trailing dims of values.
    mask = row_good.reshape(-1, *([1] * (v.ndim - 1)))
    v_good = v * mask
    n_new = nrow // factor
    summed = v_good.reshape(n_new, factor, *v.shape[1:]).sum(axis=1)
    n_good = row_good.reshape(n_new, factor).sum(axis=1)
    n_good_safe = np.where(n_good == 0, 1, n_good)
    ng = n_good_safe.reshape(-1, *([1] * (summed.ndim - 1)))
    if op == "mean":
        return summed / ng
    return summed


@pytest.mark.parametrize("factor", [2, 3, 5])
@pytest.mark.parametrize("row_chunk,chan_chunk", [(7, 3), (10, 7), (13, 11)])
def test_frequency_average_matches_reference(factor, row_chunk, chan_chunk):
    nrow, nchan, ncorr = 100, 64, 4
    ms = _make_ms(nrow, nchan, ncorr, row_chunk, chan_chunk)
    orig_data = ms.ds.DATA.data.compute()
    orig_flag = ms.ds.FLAG.data.compute()

    ms.frequency_average(factor)

    ref_d = _ref_freq_data(orig_data, orig_flag, factor)
    ref_f = _ref_freq_flag(orig_flag, factor)

    got_d = ms.ds.DATA.data.compute()
    got_f = ms.ds.FLAG.data.compute()

    assert got_d.shape == ref_d.shape
    assert got_f.shape == ref_f.shape
    assert np.allclose(got_d, ref_d)
    assert np.array_equal(got_f, ref_f)


@pytest.mark.parametrize("factor", [2, 3, 7])
@pytest.mark.parametrize("row_chunk", [7, 10, 13])
def test_time_average_matches_reference(factor, row_chunk):
    nrow, nchan, ncorr = 100, 64, 4
    ms = _make_ms(nrow, nchan, ncorr, row_chunk, 16)
    orig_data = ms.ds.DATA.data.compute()
    orig_flag = ms.ds.FLAG.data.compute()

    ms.time_average(factor)

    ref_d = _ref_time_data(orig_data, orig_flag, factor)
    ref_f = _ref_time_flag(orig_flag, factor)

    got_d = ms.ds.DATA.data.compute()
    got_f = ms.ds.FLAG.data.compute()

    n_new = nrow // factor
    assert got_d.shape[0] == n_new
    assert got_d.shape == ref_d.shape
    assert np.allclose(got_d, ref_d)
    assert np.array_equal(got_f, ref_f)


def test_time_average_antenna_is_first_of_block():
    nrow = 30
    ms = _make_ms(nrow, 8, 2, row_chunk=7, chan_chunk=8)
    ant = ms.ds.ANTENNA1.data.compute()
    ms.time_average(3)
    got = ms.ds.ANTENNA1.data.compute()
    assert np.array_equal(got, ant[0 : 30 : 3])


def test_frequency_average_with_tail():
    # nchan not divisible by factor -> tail channel
    nrow, nchan, ncorr = 40, 50, 4  # 50 % 7 = 1
    ms = _make_ms(nrow, nchan, ncorr, row_chunk=10, chan_chunk=7)
    orig_data = ms.ds.DATA.data.compute()
    orig_flag = ms.ds.FLAG.data.compute()
    factor = 7
    ms.frequency_average(factor)
    ref_d = _ref_freq_data(orig_data, orig_flag, factor)
    assert ms.ds.DATA.data.compute().shape[1] == ref_d.shape[1]
    assert np.allclose(ms.ds.DATA.data.compute(), ref_d)


# ---- Flagged-data exclusion: explicit patterns on the real DaskMS path ----

def _make_ms_with_flags(data, flag, weight=None, sigma=None, uvw=None,
                        time=None, interval=None, exposure=None,
                        flag_row=None):
    """Build a DaskMS from explicit arrays (deterministic flags).

    All arrays use the ``("row", "chan", "corr")`` / ``("row",)`` /
    ``("row", "uvw")`` dims.  Passed through to DaskMS.__new__ (offline,
    no casacore), matching the _make_ms convention.
    """
    nrow = data.shape[0]
    variables = {
        "DATA": (("row", "chan", "corr"), data),
        "FLAG": (("row", "chan", "corr"), flag),
    }
    if weight is not None:
        variables["WEIGHT_SPECTRUM"] = (("row", "chan", "corr"), weight)
    if sigma is not None:
        variables["SIGMA_SPECTRUM"] = (("row", "chan", "corr"), sigma)
    if uvw is not None:
        variables["UVW"] = (("row", "uvw"), uvw)
    if time is not None:
        variables["TIME"] = (("row",), time)
    if interval is not None:
        variables["INTERVAL"] = (("row",), interval)
    if exposure is not None:
        variables["EXPOSURE"] = (("row",), exposure)
    if flag_row is not None:
        variables["FLAG_ROW"] = (("row",), flag_row)
    else:
        variables["FLAG_ROW"] = (("row",), np.zeros(nrow, bool))

    ds = xr.Dataset(variables)
    ds = ds.chunk({"row": data.shape[0], "chan": data.shape[1],
                   "corr": data.shape[2]})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.chan_freq_hz = None
    ms.name = "<synthetic>"
    return ms


def test_frequency_average_excludes_flagged_data():
    """Flagged visibilities must not move the DATA mean, must not
    contribute to WEIGHT_SPECTRUM (sum) or SIGMA_SPECTRUM (inverse-var)."""
    # 1 row, 4 chan, 1 corr; factor 2 -> 2 output channels.
    data = np.array([[[10.0], [20.0], [30.0], [40.0]]], dtype=complex)
    # Flag channel 1 only: group 0 = {10, 20-flag} -> mean = 10;
    # group 1 = {30, 40} -> mean = 35.
    flag = np.zeros((1, 4, 1), dtype=bool)
    flag[0, 1, 0] = True
    weight = np.array([[[2.0], [3.0], [5.0], [7.0]]], dtype=np.float64)
    sigma = np.array([[[2.0], [4.0], [3.0], [5.0]]], dtype=np.float64)

    ms = _make_ms_with_flags(data, flag, weight=weight, sigma=sigma)
    ms.frequency_average(2)

    got_d = ms.ds.DATA.data.compute()
    got_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    got_s = ms.ds.SIGMA_SPECTRUM.data.compute()
    got_f = ms.ds.FLAG.data.compute()

    # DATA shape (1,2,1): group 0 = 10 (only unflagged), group 1 = (30+40)/2 = 35
    assert np.allclose(got_d, np.array([[[10.0], [35.0]]], dtype=complex))
    # WEIGHT_SPECTRUM (sum of unflagged): group 0 = 2, group 1 = 5+7 = 12
    assert np.allclose(got_w, np.array([[[2.0], [12.0]]], dtype=np.float64))
    # SIGMA: group 0 = 2.0 (only σ=2 unflagged -> 1/sqrt(1/4) = 2);
    # group 1 = 1/sqrt(1/9 + 1/25)
    assert np.allclose(got_s[0, 0, 0], 2.0)
    assert np.allclose(got_s[0, 1, 0], 1.0 / np.sqrt(1.0 / 9 + 1.0 / 25))
    # FLAG OR'd: group 0 flagged (channel 1), group 1 clean
    assert got_f[0, 0, 0] and not got_f[0, 1, 0]


def test_time_average_excludes_flagged_data():
    """Flagged visibilities must not contribute over the row axis either."""
    # 4 rows, 1 chan, 1 corr; factor 2 -> 2 output rows.
    data = np.array([[[10.0]], [[20.0]], [[30.0]], [[40.0]]], dtype=complex)
    # Flag row 1 only: group 0 = {10, 20-flag} -> mean = 10;
    # group 1 = {30, 40} -> mean = 35.
    flag = np.zeros((4, 1, 1), dtype=bool)
    flag[1, 0, 0] = True
    weight = np.array([[[2.0]], [[3.0]], [[5.0]], [[7.0]]], dtype=np.float64)
    sigma = np.array([[[2.0]], [[4.0]], [[3.0]], [[5.0]]], dtype=np.float64)

    ms = _make_ms_with_flags(data, flag, weight=weight, sigma=sigma)
    ms.time_average(2)

    got_d = ms.ds.DATA.data.compute()
    got_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    got_s = ms.ds.SIGMA_SPECTRUM.data.compute()
    got_f = ms.ds.FLAG.data.compute()

    assert got_d.shape == (2, 1, 1)
    # DATA shape (2,1,1): group 0 = 10, group 1 = 35
    assert np.allclose(got_d, np.array([[[10.0]], [[35.0]]], dtype=complex))
    # WEIGHT_SPECTRUM sum of unflagged: group 0 = 2, group 1 = 5+7 = 12
    assert np.allclose(got_w, np.array([[[2.0]], [[12.0]]], dtype=np.float64))
    # SIGMA: group 0 = 2.0 (only σ=2 unflagged), group 1 = 1/sqrt(1/9+1/25)
    assert np.allclose(got_s[0, 0, 0], 2.0)
    assert np.allclose(got_s[1, 0, 0], 1.0 / np.sqrt(1.0 / 9 + 1.0 / 25))
    assert got_f[0, 0, 0] and not got_f[1, 0, 0]


def test_frequency_average_all_flagged_group_no_nan():
    """A group where every channel is flagged: DATA->0, FLAG->True, no NaN/Inf."""
    data = np.full((1, 4, 1), 10.0 + 0j, dtype=complex)
    flag = np.zeros((1, 4, 1), dtype=bool)
    flag[0, 0, 0] = True
    flag[0, 1, 0] = True  # group 0 entirely flagged
    weight = np.full((1, 4, 1), 2.0, dtype=np.float64)
    sigma = np.full((1, 4, 1), 2.0, dtype=np.float64)

    ms = _make_ms_with_flags(data, flag, weight=weight, sigma=sigma)
    ms.frequency_average(2)

    got_d = ms.ds.DATA.data.compute()
    got_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    got_s = ms.ds.SIGMA_SPECTRUM.data.compute()
    got_f = ms.ds.FLAG.data.compute()

    # All-flagged group: DATA = 0 (0/1 guarded denominator), no NaN/Inf
    assert np.allclose(got_d[0, 0, 0], 0.0)
    assert np.all(np.isfinite(got_d))
    assert np.all(np.isfinite(got_w))
    assert np.all(np.isfinite(got_s))
    assert got_f[0, 0, 0] and not got_f[0, 1, 0]


def test_time_average_all_flagged_group_no_nan():
    """A row group entirely flagged: no NaN/Inf in DATA/WEIGHT/SIGMA."""
    data = np.full((4, 1, 1), 10.0 + 0j, dtype=complex)
    flag = np.zeros((4, 1, 1), dtype=bool)
    flag[0, 0, 0] = True
    flag[1, 0, 0] = True  # group 0 (rows 0,1) entirely flagged
    weight = np.full((4, 1, 1), 2.0, dtype=np.float64)
    sigma = np.full((4, 1, 1), 2.0, dtype=np.float64)

    ms = _make_ms_with_flags(data, flag, weight=weight, sigma=sigma)
    ms.time_average(2)

    got_d = ms.ds.DATA.data.compute()
    got_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    got_s = ms.ds.SIGMA_SPECTRUM.data.compute()
    got_f = ms.ds.FLAG.data.compute()

    assert got_d.shape == (2, 1, 1)
    assert np.allclose(got_d[0, 0, 0], 0.0)
    assert np.all(np.isfinite(got_d))
    assert np.all(np.isfinite(got_w))
    assert np.all(np.isfinite(got_s))
    assert got_f[0, 0, 0] and not got_f[1, 0, 0]


def test_time_average_metadata_excludes_fully_flagged_rows():
    """Fully-flagged rows (FLAG_ROW=True OR all-FLAG=True) must not
    contribute to TIME/UVW (mean) or INTERVAL/EXPOSURE (sum).
    Partially-flagged rows still contribute."""
    # 6 rows, 2 chan, 1 corr; factor 2 -> 3 output rows.
    nrow, nchan, ncorr = 6, 2, 1
    data = np.zeros((nrow, nchan, ncorr), dtype=complex)
    flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    flag_row = np.zeros(nrow, dtype=bool)

    time = np.arange(nrow, dtype=np.float64) * 10.0  # 0,10,20,30,40,50
    uvw = np.arange(nrow * 3, dtype=np.float64).reshape(nrow, 3)
    interval = np.full(nrow, 2.0)
    exposure = np.full(nrow, 2.0)

    # Row 0: fully flagged via FLAG_ROW. Group 0 = {row0(bad), row1(good)}.
    flag_row[0] = True
    # Row 4: fully flagged via all-FLAG. Group 2 = {row4(bad), row5(good)}.
    flag[4, :, :] = True

    ms = _make_ms_with_flags(data, flag, uvw=uvw, time=time,
                             interval=interval, exposure=exposure,
                             flag_row=flag_row)
    ms.time_average(2)

    got_time = ms.ds.TIME.data.compute()
    got_uvw = ms.ds.UVW.data.compute()
    got_int = ms.ds.INTERVAL.data.compute()
    got_exp = ms.ds.EXPOSURE.data.compute()

    # Group 0: only row1 contributes. TIME=10, INTERVAL/EXPOSURE=2.
    assert np.allclose(got_time[0], 10.0)
    assert np.allclose(got_uvw[0], uvw[1])
    assert np.allclose(got_int[0], 2.0)
    assert np.allclose(got_exp[0], 2.0)
    # Group 1: rows 2,3 both good. TIME=(20+30)/2=25, INTERVAL=2+2=4.
    assert np.allclose(got_time[1], 25.0)
    assert np.allclose(got_uvw[1], (uvw[2] + uvw[3]) / 2.0)
    assert np.allclose(got_int[1], 4.0)
    assert np.allclose(got_exp[1], 4.0)
    # Group 2: only row5 contributes. TIME=50, INTERVAL/EXPOSURE=2.
    assert np.allclose(got_time[2], 50.0)
    assert np.allclose(got_uvw[2], uvw[5])
    assert np.allclose(got_int[2], 2.0)
    assert np.allclose(got_exp[2], 2.0)


def test_time_average_flagged_row_then_all_flag_group():
    """Mixed: some rows fully flagged, and one group entirely flagged.
    Metadata = 0 there (guarded denominator), and no NaN anywhere."""
    # 6 rows, 2 chan, 1 corr; factor 2 -> 3 output rows.
    nrow, nchan, ncorr = 6, 2, 1
    data = np.zeros((nrow, nchan, ncorr), dtype=complex)
    flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    flag_row = np.zeros(nrow, dtype=bool)
    time = np.arange(nrow, dtype=np.float64) * 10.0
    interval = np.full(nrow, 2.0)

    # Group 0 (rows 0,1): both fully flagged via FLAG_ROW.
    flag_row[0] = True
    flag_row[1] = True
    # Group 1 (rows 2,3): row 2 bad, row 3 good.
    flag_row[2] = True

    ms = _make_ms_with_flags(data, flag, time=time, interval=interval,
                             flag_row=flag_row)
    ms.time_average(2)

    got_time = ms.ds.TIME.data.compute()
    got_int = ms.ds.INTERVAL.data.compute()

    # Group 0 entirely bad -> guarded mean/sum = 0/0 = 0, no NaN.
    assert np.allclose(got_time[0], 0.0)
    assert np.allclose(got_int[0], 0.0)
    assert not np.any(np.isnan(got_time))
    assert not np.any(np.isnan(got_int))
    # Group 1: only row 3 contributes. TIME=30, INTERVAL=2.
    assert np.allclose(got_time[1], 30.0)
    assert np.allclose(got_int[1], 2.0)


@pytest.mark.parametrize("factor", [2, 3, 5])
@pytest.mark.parametrize("row_chunk,chan_chunk", [(7, 3), (10, 7), (13, 11)])
def test_frequency_average_weight_sigma_match_reference(factor, row_chunk, chan_chunk):
    """WEIGHT_SPECTRUM (sum) and SIGMA_SPECTRUM (inverse-variance) over
    channels match the numpy reference, excluding flagged data."""
    nrow, nchan, ncorr = 100, 64, 4
    ms = _make_ms(nrow, nchan, ncorr, row_chunk, chan_chunk)
    orig_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    orig_s = ms.ds.SIGMA_SPECTRUM.data.compute()
    orig_f = ms.ds.FLAG.data.compute()

    ms.frequency_average(factor)

    ref_w = _ref_freq_weight(orig_w, orig_f, factor)
    ref_s = _ref_freq_sigma(orig_s, orig_f, factor)

    got_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    got_s = ms.ds.SIGMA_SPECTRUM.data.compute()

    assert got_w.shape == ref_w.shape
    assert got_s.shape == ref_s.shape
    assert np.allclose(got_w, ref_w)
    assert np.allclose(got_s, ref_s)


@pytest.mark.parametrize("factor", [2, 3, 7])
@pytest.mark.parametrize("row_chunk", [7, 10, 13])
def test_time_average_weight_sigma_match_reference(factor, row_chunk):
    """WEIGHT_SPECTRUM and SIGMA_SPECTRUM over rows match the numpy
    reference, excluding flagged data."""
    nrow, nchan, ncorr = 100, 64, 4
    ms = _make_ms(nrow, nchan, ncorr, row_chunk, 16)
    orig_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    orig_s = ms.ds.SIGMA_SPECTRUM.data.compute()
    orig_f = ms.ds.FLAG.data.compute()

    ms.time_average(factor)

    ref_w = _ref_time_weight(orig_w, orig_f, factor)
    ref_s = _ref_time_sigma(orig_s, orig_f, factor)

    got_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    got_s = ms.ds.SIGMA_SPECTRUM.data.compute()

    n_new = nrow // factor
    assert got_w.shape[0] == n_new
    assert got_s.shape[0] == n_new
    assert np.allclose(got_w, ref_w)
    assert np.allclose(got_s, ref_s)


def test_time_average_metadata_matches_reference():
    """TIME/UVW (masked mean) and INTERVAL/EXPOSURE (masked sum) over
    rows match the numpy reference that excludes fully-flagged rows."""
    nrow, nchan, ncorr = 40, 16, 2
    ms = _make_ms(nrow, nchan, ncorr, row_chunk=11, chan_chunk=16)
    # Mark some rows fully flagged via FLAG_ROW, some via all-FLAG.
    flag_row = np.zeros(nrow, dtype=bool)
    flag_row[3] = True
    flag_row[17] = True
    ms.ds["FLAG_ROW"] = (("row",), flag_row)
    all_flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    all_flag[8, :, :] = True
    all_flag[25, :, :] = True
    ms.ds["FLAG"] = (("row", "chan", "corr"),
                     all_flag | ms.ds.FLAG.data.compute())
    ms.ds = ms.ds.chunk({"row": 11, "chan": 16, "corr": ncorr})

    orig_time = ms.ds.TIME.data.compute()
    orig_uvw = ms.ds.UVW.data.compute()
    orig_int = ms.ds.INTERVAL.data.compute()
    orig_exp = ms.ds.EXPOSURE.data.compute()
    flag = ms.ds.FLAG.data.compute()

    factor = 4
    ms.time_average(factor)

    ref_time = _ref_time_metadata(orig_time, flag_row, flag, factor, "mean")
    ref_uvw = _ref_time_metadata(orig_uvw, flag_row, flag, factor, "mean")
    ref_int = _ref_time_metadata(orig_int, flag_row, flag, factor, "sum")
    ref_exp = _ref_time_metadata(orig_exp, flag_row, flag, factor, "sum")

    assert np.allclose(ms.ds.TIME.data.compute(), ref_time)
    assert np.allclose(ms.ds.UVW.data.compute(), ref_uvw)
    assert np.allclose(ms.ds.INTERVAL.data.compute(), ref_int)
    assert np.allclose(ms.ds.EXPOSURE.data.compute(), ref_exp)


def test_time_then_frequency_average_matches_reference():
    """Combined: time_average then frequency_average.  DATA/FLAG/WEIGHT/
    SIGMA match numpy references applied in sequence.  Flags propagate
    via OR through both steps so masking composes correctly."""
    nrow, nchan, ncorr = 30, 20, 2
    ms = _make_ms(nrow, nchan, ncorr, row_chunk=11, chan_chunk=9)

    orig_data = ms.ds.DATA.data.compute()
    orig_flag = ms.ds.FLAG.data.compute()
    orig_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    orig_s = ms.ds.SIGMA_SPECTRUM.data.compute()

    tfactor, ffactor = 3, 4
    ms.time_average(tfactor)
    ms.frequency_average(ffactor)

    # Reference: apply time then freq references in sequence.
    d = _ref_time_data(orig_data, orig_flag, tfactor)
    f = _ref_time_flag(orig_flag, tfactor)
    w = _ref_time_weight(orig_w, orig_flag, tfactor)
    s = _ref_time_sigma(orig_s, orig_flag, tfactor)

    d2 = _ref_freq_data(d, f, ffactor)
    f2 = _ref_freq_flag(f, ffactor)
    w2 = _ref_freq_weight(w, f, ffactor)
    s2 = _ref_freq_sigma(s, f, ffactor)

    got_d = ms.ds.DATA.data.compute()
    got_f = ms.ds.FLAG.data.compute()
    got_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    got_s = ms.ds.SIGMA_SPECTRUM.data.compute()

    assert got_d.shape == d2.shape
    assert np.allclose(got_d, d2)
    assert np.array_equal(got_f, f2)
    assert np.allclose(got_w, w2)
    assert np.allclose(got_s, s2)


def test_frequency_then_time_average_matches_reference():
    """Combined: frequency_average then time_average (reverse order).
    References applied in the reverse sequence."""
    nrow, nchan, ncorr = 30, 20, 2
    ms = _make_ms(nrow, nchan, ncorr, row_chunk=11, chan_chunk=9)

    orig_data = ms.ds.DATA.data.compute()
    orig_flag = ms.ds.FLAG.data.compute()
    orig_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    orig_s = ms.ds.SIGMA_SPECTRUM.data.compute()

    tfactor, ffactor = 3, 4
    ms.frequency_average(ffactor)
    ms.time_average(tfactor)

    # Reference: apply freq then time references in sequence.
    d = _ref_freq_data(orig_data, orig_flag, ffactor)
    f = _ref_freq_flag(orig_flag, ffactor)
    w = _ref_freq_weight(orig_w, orig_flag, ffactor)
    s = _ref_freq_sigma(orig_s, orig_flag, ffactor)

    d2 = _ref_time_data(d, f, tfactor)
    f2 = _ref_time_flag(f, tfactor)
    w2 = _ref_time_weight(w, f, tfactor)
    s2 = _ref_time_sigma(s, f, tfactor)

    got_d = ms.ds.DATA.data.compute()
    got_f = ms.ds.FLAG.data.compute()
    got_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    got_s = ms.ds.SIGMA_SPECTRUM.data.compute()

    assert got_d.shape == d2.shape
    assert np.allclose(got_d, d2)
    assert np.array_equal(got_f, f2)
    assert np.allclose(got_w, w2)
    assert np.allclose(got_s, s2)
