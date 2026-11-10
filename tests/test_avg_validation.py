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
#
# CASA mstransform's flag semantics (skarabina#16): an output sample is
# taken over its unflagged inputs and is unflagged; only when every input is
# flagged is it flagged, and then it is taken over all of them.

def _ref_mean(d, f, axis):
    """Unflagged mean along axis; the mean of all where all are flagged."""
    n = (~f).sum(axis).astype(float)
    m_unf = np.where(f, 0, d).sum(axis) / np.where(n == 0, 1, n)
    return np.where(n > 0, m_unf, d.mean(axis))


def _ref_sum(w, f, axis):
    """Unflagged sum along axis; the sum of all where all are flagged."""
    n = (~f).sum(axis)
    return np.where(n > 0, np.where(f, 0, w).sum(axis), w.sum(axis))


def _ref_sigma(s, f, axis):
    """1/sqrt(sum 1/sigma^2) over the unflagged; over all where all are flagged."""
    inv = 1.0 / (s ** 2)
    tot = _ref_sum(inv, f, axis)
    return np.sqrt(1.0 / np.where(tot == 0, 1, tot))


def _chan_groups(nchan, factor):
    n_full = nchan // factor
    groups = [slice(i * factor, (i + 1) * factor) for i in range(n_full)]
    if nchan % factor:
        groups.append(slice(n_full * factor, nchan))
    return groups


def _ref_freq_data(data, flag, factor):
    return np.stack([_ref_mean(data[:, g, :], flag[:, g, :], 1)
                     for g in _chan_groups(data.shape[1], factor)], axis=1)


def _ref_freq_flag(flag, factor):
    return np.stack([np.all(flag[:, g, :], 1)
                     for g in _chan_groups(flag.shape[1], factor)], axis=1)


def _time_average_plan(a1, a2, time, factor):
    """Independent numpy model of ``time_average``'s grouping (skarabina#15).

    Groups rows by (antenna1, antenna2) — the fixtures have no FIELD_ID /
    SCAN_NUMBER, which the implementation treats as constant — bins each
    group by time rank (``factor`` integrations per bin, incomplete tails
    dropped) and returns ``(perm, n_new)``: the output row order (bin centre,
    then baseline) and the output row count.
    """
    a1 = np.asarray(a1)
    a2 = np.asarray(a2)
    time = np.asarray(time, dtype=float)
    key = np.stack([a1, a2], axis=1)
    _, labels = np.unique(key, axis=0, return_inverse=True)
    labels = labels.reshape(-1)
    order = np.lexsort((time, labels))
    lab = labels[order]
    sizes = np.bincount(lab)
    starts = np.cumsum(sizes) - sizes
    within = np.arange(len(order)) - np.repeat(starts, sizes)
    n_bins = sizes // factor
    keep = within < (n_bins * factor)[lab]
    kept = order[keep]
    lab_k, within_k = lab[keep], within[keep]
    binid = within_k // factor
    maxb = int(binid.max()) + 1
    gid = lab_k.astype(np.int64) * maxb + binid
    counts = np.bincount(gid)
    bin_mean = np.bincount(gid, weights=time[kept]) / np.maximum(counts, 1)
    centre = np.empty(len(time), dtype=float)
    centre[kept] = bin_mean[gid]
    perm = kept[np.lexsort((lab_k, centre[kept]))]
    return perm, int(n_bins.sum())


def _bins(x, factor, perm):
    x = x[perm]
    return x.reshape(-1, factor, *x.shape[1:])


def _ref_time_data(data, flag, factor, perm):
    return _ref_mean(_bins(data, factor, perm), _bins(flag, factor, perm), 1)


def _ref_time_flag(flag, factor, perm):
    return np.all(_bins(flag, factor, perm), axis=1)


# ---- numpy references: WEIGHT_SPECTRUM, SIGMA_SPECTRUM, metadata ----

def _ref_freq_weight(weight, flag, factor):
    """WEIGHT_SPECTRUM over channels: sum of unflagged weights (incl. tail)."""
    return np.stack([_ref_sum(weight[:, g, :], flag[:, g, :], 1)
                     for g in _chan_groups(weight.shape[1], factor)], axis=1)


def _ref_freq_sigma(sigma, flag, factor):
    """SIGMA_SPECTRUM over channels: sqrt(1 / sum(1/sigma^2)) over unflagged."""
    return np.stack([_ref_sigma(sigma[:, g, :], flag[:, g, :], 1)
                     for g in _chan_groups(sigma.shape[1], factor)], axis=1)


def _ref_time_weight(weight, flag, factor, perm):
    """WEIGHT_SPECTRUM over a bin: sum of unflagged weights."""
    return _ref_sum(_bins(weight, factor, perm), _bins(flag, factor, perm), 1)


def _ref_time_sigma(sigma, flag, factor, perm):
    """SIGMA_SPECTRUM over a bin: sqrt(1 / sum(1/sigma^2)) over unflagged."""
    return _ref_sigma(_bins(sigma, factor, perm), _bins(flag, factor, perm), 1)


def _ref_time_metadata(values, factor, perm, op):
    """Unmasked geometry over a bin: mean (TIME, UVW) or sum (INTERVAL,
    EXPOSURE), by skarabina#15's spec — flags mask DATA/WEIGHT/SIGMA only,
    never the geometry, so a fully-flagged bin keeps a valid timestamp.
    ``op`` is ``"mean"`` or ``"sum"``; ``values`` is (nrow,) or (nrow, k).
    """
    v = np.asarray(values)[perm]
    blocks = v.reshape(-1, factor, *v.shape[1:])
    if op == "mean":
        return blocks.mean(axis=1)
    return blocks.sum(axis=1)


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
    perm, n_new = _time_average_plan(
        ms.ds.ANTENNA1.data.compute(), ms.ds.ANTENNA2.data.compute(),
        ms.ds.TIME.data.compute(), factor)

    ms.time_average(factor)

    ref_d = _ref_time_data(orig_data, orig_flag, factor, perm)
    ref_f = _ref_time_flag(orig_flag, factor, perm)

    got_d = ms.ds.DATA.data.compute()
    got_f = ms.ds.FLAG.data.compute()

    assert got_d.shape[0] == n_new
    assert got_d.shape == ref_d.shape
    assert np.allclose(got_d, ref_d)
    assert np.array_equal(got_f, ref_f)


def test_time_average_antenna_is_first_of_block():
    """ANTENNA1/2 stay constant within their bin (skarabina#15: rows are
    grouped by baseline, so first-of-bin is that baseline)."""
    nrow = 30
    ms = _make_ms(nrow, 8, 2, row_chunk=7, chan_chunk=8)
    ms.time_average(3)
    got = ms.ds.ANTENNA1.data.compute()
    # a1 = arange % 4: baselines 0..3 with 8,8,7,7 samples; factor 3 keeps two
    # bins each, emitted time-major -> the baseline label cycles 0,1,2,3 twice.
    assert np.array_equal(got, np.tile([0, 1, 2, 3], 2)), got
    assert np.array_equal(ms.ds.ANTENNA2.data.compute(), got)


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
    # mstransform (skarabina#16): group 0 still has an unflagged channel, so
    # its average is valid and UNflagged; group 1 is clean.
    assert not got_f[0, 0, 0] and not got_f[0, 1, 0]


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
    # mstransform (skarabina#16): bin 0 keeps an unflagged sample -> unflagged.
    assert not got_f[0, 0, 0] and not got_f[1, 0, 0]
    assert not ms.ds.FLAG_ROW.data.compute().any()


def test_frequency_average_all_flagged_group_no_nan():
    """A group where every channel is flagged: flagged, and (mstransform) its
    DATA / WEIGHT / SIGMA are taken over all of its channels -- kept, not
    zeroed -- with no NaN/Inf."""
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

    # All-flagged group: the mean / sum / inverse variance of ALL its channels
    assert np.allclose(got_d[0, 0, 0], 10.0)
    assert np.allclose(got_w[0, 0, 0], 4.0)
    assert np.allclose(got_s[0, 0, 0], np.sqrt(2.0))
    assert np.all(np.isfinite(got_d))
    assert np.all(np.isfinite(got_w))
    assert np.all(np.isfinite(got_s))
    assert got_f[0, 0, 0] and not got_f[0, 1, 0]


def test_time_average_all_flagged_group_no_nan():
    """A row group entirely flagged: flagged, DATA/WEIGHT/SIGMA over all of
    its samples (mstransform), no NaN/Inf."""
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
    assert np.allclose(got_d[0, 0, 0], 10.0)
    assert np.allclose(got_w[0, 0, 0], 4.0)
    assert np.allclose(got_s[0, 0, 0], np.sqrt(2.0))
    assert np.all(np.isfinite(got_d))
    assert np.all(np.isfinite(got_w))
    assert np.all(np.isfinite(got_s))
    assert got_f[0, 0, 0] and not got_f[1, 0, 0]


def test_time_average_geometry_ignores_flags():
    """Geometry is averaged UNMASKED (skarabina#15's spec): TIME/UVW take
    the bin centre and INTERVAL/EXPOSURE sum every sample, flagged or not,
    so a fully-flagged bin keeps a valid timestamp instead of being zeroed.
    Flags mask DATA/WEIGHT/SIGMA only (covered elsewhere) and set FLAG."""
    # 6 rows, 2 chan, 1 corr; factor 2 -> 3 output rows, one baseline
    # (the fixture carries no antenna columns -> a single group).
    nrow, nchan, ncorr = 6, 2, 1
    data = np.zeros((nrow, nchan, ncorr), dtype=complex)
    flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    flag_row = np.zeros(nrow, dtype=bool)

    time = np.arange(nrow, dtype=np.float64) * 10.0  # 0,10,20,30,40,50
    uvw = np.arange(nrow * 3, dtype=np.float64).reshape(nrow, 3)
    interval = np.full(nrow, 2.0)
    exposure = np.full(nrow, 2.0)

    # Row 0: fully flagged via FLAG_ROW. Bin 0 = {row0(bad), row1(good)}.
    flag_row[0] = True
    # Row 4: fully flagged via all-FLAG. Bin 2 = {row4(bad), row5(good)}.
    flag[4, :, :] = True

    ms = _make_ms_with_flags(data, flag, uvw=uvw, time=time,
                             interval=interval, exposure=exposure,
                             flag_row=flag_row)
    ms.time_average(2)

    got_time = ms.ds.TIME.data.compute()
    got_uvw = ms.ds.UVW.data.compute()
    got_int = ms.ds.INTERVAL.data.compute()
    got_exp = ms.ds.EXPOSURE.data.compute()

    # Bin 0 {0,1}: centre (0+10)/2 = 5, UVW mean, INTERVAL 2+2 = 4.
    assert np.allclose(got_time[0], 5.0)
    assert np.allclose(got_uvw[0], (uvw[0] + uvw[1]) / 2.0)
    assert np.allclose(got_int[0], 4.0)
    assert np.allclose(got_exp[0], 4.0)
    # Bin 1 {2,3}: TIME=(20+30)/2=25, UVW mean, INTERVAL=4.
    assert np.allclose(got_time[1], 25.0)
    assert np.allclose(got_uvw[1], (uvw[2] + uvw[3]) / 2.0)
    assert np.allclose(got_int[1], 4.0)
    assert np.allclose(got_exp[1], 4.0)
    # Bin 2 {4,5}: the all-flagged row still contributes its geometry.
    assert np.allclose(got_time[2], 45.0)
    assert np.allclose(got_uvw[2], (uvw[4] + uvw[5]) / 2.0)
    assert np.allclose(got_int[2], 4.0)
    assert np.allclose(got_exp[2], 4.0)
    # ...and (mstransform, skarabina#16) every bin keeps a good row, so no
    # output row is flagged: the bad row's samples are simply left out.
    assert np.array_equal(ms.ds.FLAG_ROW.data.compute(),
                          [False, False, False])
    assert not ms.ds.FLAG.data.compute().any()


def test_time_average_flagged_row_then_all_flag_group():
    """Mixed: some rows fully flagged, and one bin entirely flagged.

    The all-flagged bin keeps valid geometry (skarabina#15: the old masked
    mean divided 0/1 and zeroed TIME/INTERVAL), stays flagged, and nothing
    is NaN."""
    # 6 rows, 2 chan, 1 corr; factor 2 -> 3 output rows, one baseline.
    nrow, nchan, ncorr = 6, 2, 1
    data = np.zeros((nrow, nchan, ncorr), dtype=complex)
    flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    flag_row = np.zeros(nrow, dtype=bool)
    time = np.arange(nrow, dtype=np.float64) * 10.0
    interval = np.full(nrow, 2.0)

    # Bin 0 (rows 0,1): both fully flagged via FLAG_ROW.
    flag_row[0] = True
    flag_row[1] = True
    # Bin 1 (rows 2,3): row 2 bad, row 3 good.
    flag_row[2] = True

    ms = _make_ms_with_flags(data, flag, time=time, interval=interval,
                             flag_row=flag_row)
    ms.time_average(2)

    got_time = ms.ds.TIME.data.compute()
    got_int = ms.ds.INTERVAL.data.compute()

    # Bin 0 entirely flagged: geometry is the unmasked bin mean/sum.
    assert np.allclose(got_time[0], 5.0)      # (0 + 10) / 2
    assert np.allclose(got_int[0], 4.0)       # 2 + 2
    assert not np.any(np.isnan(got_time))
    assert not np.any(np.isnan(got_int))
    # ...and it stays flagged.  Bin 1 holds a good row (3) beside the
    # row-flagged row 2, so it is NOT flagged (mstransform, skarabina#16).
    assert np.array_equal(ms.ds.FLAG_ROW.data.compute(),
                          [True, False, False])
    # Bin 1: both rows contribute their geometry. TIME=(20+30)/2, INTERVAL=4.
    assert np.allclose(got_time[1], 25.0)
    assert np.allclose(got_int[1], 4.0)
    # Bin 2: untouched rows. TIME=45, INTERVAL=4.
    assert np.allclose(got_time[2], 45.0)
    assert np.allclose(got_int[2], 4.0)


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
    perm, n_new = _time_average_plan(
        ms.ds.ANTENNA1.data.compute(), ms.ds.ANTENNA2.data.compute(),
        ms.ds.TIME.data.compute(), factor)

    ms.time_average(factor)

    ref_w = _ref_time_weight(orig_w, orig_f, factor, perm)
    ref_s = _ref_time_sigma(orig_s, orig_f, factor, perm)

    got_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    got_s = ms.ds.SIGMA_SPECTRUM.data.compute()

    assert got_w.shape[0] == n_new
    assert got_s.shape[0] == n_new
    assert np.allclose(got_w, ref_w)
    assert np.allclose(got_s, ref_s)


def test_time_average_metadata_matches_reference():
    """TIME/UVW (bin-centre mean) and INTERVAL/EXPOSURE (bin sum) match
    the unmasked numpy reference (skarabina#15's geometry spec)."""
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

    factor = 4
    perm, n_new = _time_average_plan(
        ms.ds.ANTENNA1.data.compute(), ms.ds.ANTENNA2.data.compute(),
        orig_time, factor)
    ms.time_average(factor)

    ref_time = _ref_time_metadata(orig_time, factor, perm, "mean")
    ref_uvw = _ref_time_metadata(orig_uvw, factor, perm, "mean")
    ref_int = _ref_time_metadata(orig_int, factor, perm, "sum")
    ref_exp = _ref_time_metadata(orig_exp, factor, perm, "sum")
    assert ms.ds.TIME.shape[0] == n_new

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
    perm, _ = _time_average_plan(
        ms.ds.ANTENNA1.data.compute(), ms.ds.ANTENNA2.data.compute(),
        ms.ds.TIME.data.compute(), tfactor)
    ms.time_average(tfactor)
    ms.frequency_average(ffactor)

    # Reference: apply time then freq references in sequence.
    d = _ref_time_data(orig_data, orig_flag, tfactor, perm)
    f = _ref_time_flag(orig_flag, tfactor, perm)
    w = _ref_time_weight(orig_w, orig_flag, tfactor, perm)
    s = _ref_time_sigma(orig_s, orig_flag, tfactor, perm)

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
    perm, _ = _time_average_plan(
        ms.ds.ANTENNA1.data.compute(), ms.ds.ANTENNA2.data.compute(),
        ms.ds.TIME.data.compute(), tfactor)
    ms.frequency_average(ffactor)
    ms.time_average(tfactor)

    # Reference: apply freq then time references in sequence (frequency
    # averaging does not move rows, so the same grouping plan applies).
    d = _ref_freq_data(orig_data, orig_flag, ffactor)
    f = _ref_freq_flag(orig_flag, ffactor)
    w = _ref_freq_weight(orig_w, orig_flag, ffactor)
    s = _ref_freq_sigma(orig_s, orig_flag, ffactor)

    d2 = _ref_time_data(d, f, tfactor, perm)
    f2 = _ref_time_flag(f, tfactor, perm)
    w2 = _ref_time_weight(w, f, tfactor, perm)
    s2 = _ref_time_sigma(s, f, tfactor, perm)

    got_d = ms.ds.DATA.data.compute()
    got_f = ms.ds.FLAG.data.compute()
    got_w = ms.ds.WEIGHT_SPECTRUM.data.compute()
    got_s = ms.ds.SIGMA_SPECTRUM.data.compute()

    assert got_d.shape == d2.shape
    assert np.allclose(got_d, d2)
    assert np.array_equal(got_f, f2)
    assert np.allclose(got_w, w2)
    assert np.allclose(got_s, s2)


def test_time_average_flag_row_excludes_samples():
    """A row with FLAG_ROW set (FLAG cells clear) joins no average: CASA's
    flag cube ORs FLAG_ROW into every cell (skarabina#16).  The bin keeps
    the good row's DATA / weight, unflagged."""
    data = np.array([[[100.0]], [[20.0]], [[30.0]], [[40.0]]], dtype=complex)
    flag = np.zeros((4, 1, 1), dtype=bool)
    flag_row = np.array([True, False, False, False])
    weight = np.array([[[9.0]], [[3.0]], [[5.0]], [[7.0]]], dtype=np.float64)
    ms = _make_ms_with_flags(data, flag, weight=weight, flag_row=flag_row)
    ms.time_average(2)
    assert np.allclose(ms.ds.DATA.data.compute()[:, 0, 0], [20.0, 35.0])
    assert np.allclose(ms.ds.WEIGHT_SPECTRUM.data.compute()[:, 0, 0], [3.0, 12.0])
    assert not ms.ds.FLAG.data.compute().any()
    assert not ms.ds.FLAG_ROW.data.compute().any()
