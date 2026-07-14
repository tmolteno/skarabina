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
    uvw = rng.standard_normal((nrow, 3))
    time = np.arange(nrow, dtype=np.float64)
    interval = np.ones(nrow, dtype=np.float64) * 2.0
    ant = (np.arange(nrow) % 4).astype(np.int32)

    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), data),
            "FLAG": (("row", "chan", "corr"), flag),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), weight),
            "UVW": (("row", "uvw"), uvw),
            "TIME": (("row",), time),
            "INTERVAL": (("row",), interval),
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
