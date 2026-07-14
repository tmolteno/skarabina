# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Regression tests for the flagging/analysis pipeline.

These guard against a class of stale-cache bugs: DaskMS.__init__
snapshots dataset columns (self.flag, self.data, self.u_arr/v_arr,
self.time, self.weight_spectrum, self.antenna1/2) that are never
refreshed, so readers of those attributes after flagging/averaging/
optimize saw the ORIGINAL data.  The methods now read self.ds.<COL>
fresh; these tests lock that in.
"""
import numpy as np
import pytest
import xarray as xr

from skarabina.barber import barber
from skarabina.dask_ms import DaskMS


def _make_full_ms(data, flag, uvw, chan_freq_hz=None, weight=None,
                  time=None, antenna1=None, antenna2=None):
    """Build a DaskMS with the cached attributes populated, mirroring
    what __init__ sets up, so flag_spectral_window (which reads the
    cached self.u_arr/v_arr for its UV-distance computation) works.
    """
    nrow = data.shape[0]
    if weight is None:
        weight = np.ones(data.shape, dtype=np.float64)
    if time is None:
        time = np.arange(nrow, dtype=np.float64)
    if antenna1 is None:
        antenna1 = (np.arange(nrow) % 4).astype(np.int32)
    if antenna2 is None:
        antenna2 = antenna1

    import dask.array as da
    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), data),
            "FLAG": (("row", "chan", "corr"), flag),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), weight),
            "UVW": (("row", "uvw"), uvw),
            "TIME": (("row",), time),
            "ANTENNA1": (("row",), antenna1),
            "ANTENNA2": (("row",), antenna2),
            "FLAG_ROW": (("row",), np.zeros(nrow, bool)),
        }
    )
    ds = ds.chunk({"row": nrow, "chan": data.shape[1], "corr": data.shape[2]})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.chan_freq_hz = chan_freq_hz
    ms.name = "<synthetic>"
    # Populate the cached attributes that some methods still read.
    ms.flag = da.asarray(ds.FLAG)
    ms.flag_row = da.asarray(ds.FLAG_ROW)
    ms.data = da.asarray(ds.DATA)
    ms.uvw = da.asarray(ds.UVW)
    ms.u_arr = ms.uvw[:, 0].T
    ms.v_arr = ms.uvw[:, 1].T
    ms.w_arr = ms.uvw[:, 2].T
    ms.time = da.asarray(ds.TIME)
    ms.weight_spectrum = da.asarray(ds.WEIGHT_SPECTRUM)
    ms.antenna1 = da.asarray(ds.ANTENNA1)
    ms.antenna2 = da.asarray(ds.ANTENNA2)
    ms.sub_table_names = []
    return ms


def test_flag_data_then_spectral_window_preserves_nan_flags(tmp_path):
    """flag_spectral_window must OR onto the CURRENT flags (including
    NaN/clip flags written by flag_data), not the stale __init__
    snapshot.  Before the fix, the NaN flag set by flag_data was
    silently discarded when flag_spectral_window overwrote FLAG."""
    nrow, nchan, ncorr = 2, 4, 1
    data = np.ones((nrow, nchan, ncorr), dtype=complex)
    data[0, 0, 0] = np.nan  # a NaN visibility -> flagged by flag_data
    flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    uvw = np.zeros((nrow, 3))
    # Channel frequencies: 100, 200, 300, 400 MHz
    chan_freq_hz = np.array([1e8, 2e8, 3e8, 4e8])

    ms = _make_full_ms(data, flag, uvw, chan_freq_hz=chan_freq_hz)

    # 1. Flag NaN visibilities (as main.py does via flag_data).
    ms.flag_data({"NAN": True})
    assert ms.ds.FLAG.data.compute()[0, 0, 0], "flag_data should flag the NaN"

    # 2. Flag a spectral range covering 300 MHz (channel 2).
    spw_yml = tmp_path / "spw.yml"
    spw_yml.write_text("- spw:\n    - [250, 350]\n")
    ms.flag_spectral_window(str(spw_yml))

    got = ms.ds.FLAG.data.compute()
    # The NaN flag at [0,0,0] must SURVIVE spectral flagging.
    assert got[0, 0, 0], (
        "NaN flag from flag_data was discarded by flag_spectral_window"
        " (stale self.flag read)"
    )
    # The spectral range flag at channel 2 must also be set.
    assert got[0, 2, 0], "spectral-window flag at channel 2 should be set"
    # Channel 1 (200 MHz, outside [250,350]) should remain unflagged.
    assert not got[0, 1, 0]


def test_summary_uvw_after_time_average(capsys):
    """summary() must compute UV percentiles and max-uv from the LIVE
    dataset, so they reflect post-averaging UVW.  Before the fix,
    summary used the stale self.u_arr/v_arr snapshot and reported the
    original (pre-averaging) max baseline."""
    nrow, nchan, ncorr = 4, 2, 1
    data = np.ones((nrow, nchan, ncorr), dtype=complex)
    flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    # UVW: row 0 has a large u (100); the rest are 0.  Averaging pairs
    # of rows reduces the max from 100 to 50.
    uvw = np.zeros((nrow, 3))
    uvw[0, 0] = 100.0  # u of row 0

    ms = _make_full_ms(data, flag, uvw)
    ms.time_average(2)

    # Sanity: the live dataset's UVW has been averaged.
    live_uvw = ms.ds.UVW.data.compute()
    assert np.allclose(live_uvw[0, 0], 50.0), "UVW should be averaged to 50"

    ms.summary()
    out = capsys.readouterr().out

    # Parse the max-uv line: "    max-uv: <value>"
    max_uv_line = [ln for ln in out.splitlines() if "max-uv:" in ln][0]
    max_uv = float(max_uv_line.split(":")[1].strip())

    # Live UVW max is 50; the stale original would be 100.
    assert max_uv == pytest.approx(50.0, abs=0.5), (
        f"summary max-uv={max_uv} looks like stale pre-averaging UVW"
        f" (expected ~50, stale would be ~100)"
    )


def test_barber_reads_live_dataset_after_averaging(capsys):
    """barber() must report on the post-averaging dataset.  Before the
    fix it read the cached ms.flag/ms.data/... snapshots and reported
    the original row count."""
    nrow, nchan, ncorr = 4, 3, 2
    data = np.ones((nrow, nchan, ncorr), dtype=complex)
    flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    uvw = np.zeros((nrow, 3))

    ms = _make_full_ms(data, flag, uvw)
    ms.time_average(2)

    # After averaging the live dataset has 2 rows.
    assert ms.ds.FLAG.shape[0] == 2

    barber(ms, pol=0)
    out = capsys.readouterr().out

    # The "Max Vis Report (<shape>)" line must show the averaged row
    # count (2), not the original (4).
    shape_line = [ln for ln in out.splitlines() if "Max Vis Report" in ln][0]
    assert "(2, 3, 2)" in shape_line, (
        f"barber reported shape from stale data: {shape_line!r}"
        " (expected (2, 3, 2), stale would be (4, 3, 2))"
    )
