# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for SPECTRAL_WINDOW bookkeeping.

A written MS gets its sub-tables copied verbatim from the input, so after
frequency averaging (or after fully-flagged channels are removed) the
SPECTRAL_WINDOW sub-table still describes the *input* channel setup unless it
is rewritten.  These tests pin the bookkeeping that feeds that rewrite:
channel frequencies and widths must follow the data through averaging and
``optimize``.
"""
import numpy as np
import pytest
import xarray as xr

from skarabina.dask_ms import DaskMS, spw_column_updates


def _make_ms(nchan, flags=None, chan_freq_hz=None, chan_width_hz=None):
    """Synthetic single-row, single-SPW MS with channel bookkeeping set."""
    nrow, ncorr = 2, 1
    data = np.ones((nrow, nchan, ncorr), dtype=complex)
    flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    if flags is not None:
        flag[:] = flags

    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), data),
            "FLAG": (("row", "chan", "corr"), flag),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), np.ones(data.shape)),
            "UVW": (("row", "uvw"), np.zeros((nrow, 3))),
            "TIME": (("row",), np.arange(nrow, dtype=float)),
            "ANTENNA1": (("row",), np.zeros(nrow, dtype=np.int32)),
            "ANTENNA2": (("row",), np.ones(nrow, dtype=np.int32)),
            "FLAG_ROW": (("row",), np.zeros(nrow, dtype=bool)),
        }
    ).chunk({"row": nrow, "chan": nchan, "corr": ncorr})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.name = "<synthetic>"
    ms.sub_table_names = []
    ms.nspw = 1
    ms.chan_freq_hz = chan_freq_hz
    ms.chan_width_hz = chan_width_hz
    ms.spw_chan_count = None if chan_freq_hz is None else len(chan_freq_hz)
    ms._refresh_cached_columns()
    return ms


def test_spw_column_updates_values_and_shapes():
    freq = np.array([1.0e8, 2.0e8])
    width = np.array([1.0e6, 1.0e6])
    updates = spw_column_updates(2, freq, width)

    assert list(updates["NUM_CHAN"]) == [2]
    assert updates["CHAN_FREQ"].shape == (1, 2)
    assert np.allclose(updates["CHAN_FREQ"][0], freq)
    assert updates["RESOLUTION"].shape == (1, 2)
    assert np.allclose(updates["RESOLUTION"][0], width)
    assert np.allclose(updates["TOTAL_BANDWIDTH"], [2.0e6])


def test_spw_column_updates_without_widths():
    updates = spw_column_updates(2, np.array([1.0e8, 2.0e8]))
    assert set(updates) == {"NUM_CHAN", "CHAN_FREQ"}


@pytest.mark.parametrize(
    "nchan,freq,width",
    [
        (3, np.array([1.0e8, 2.0e8]), None),  # too few frequencies
        (1, np.array([1.0e8]), np.array([1.0e6, 1.0e6])),  # too many widths
    ],
)
def test_spw_column_updates_rejects_mismatch(nchan, freq, width):
    with pytest.raises(RuntimeError, match="bookkeeping error"):
        spw_column_updates(nchan, freq, width)


def test_frequency_average_updates_freqs_and_widths():
    freq = np.array([1.0e8, 1.1e8, 1.2e8, 1.3e8])
    width = np.full(4, 1.0e7)
    ms = _make_ms(4, chan_freq_hz=freq.copy(), chan_width_hz=width.copy())

    ms.frequency_average(2)

    assert ms.ds.DATA.shape[1] == 2
    assert np.allclose(ms.chan_freq_hz, [1.05e8, 1.25e8])
    assert np.allclose(ms.chan_width_hz, [2.0e7, 2.0e7])
    # bookkeeping now matches the data
    updates = spw_column_updates(ms.ds.DATA.shape[1], ms.chan_freq_hz, ms.chan_width_hz)
    assert list(updates["NUM_CHAN"]) == [2]


def test_frequency_average_trailing_channel():
    freq = np.array([1.0e8, 1.1e8, 1.2e8, 1.3e8, 1.4e8])
    width = np.full(5, 1.0e7)
    ms = _make_ms(5, chan_freq_hz=freq.copy(), chan_width_hz=width.copy())

    ms.frequency_average(2)

    # 5 channels, factor 2 -> 2 full groups + 1 narrower trailing channel
    assert len(ms.chan_freq_hz) == 3
    assert np.isclose(ms.chan_freq_hz[-1], 1.4e8)
    assert np.isclose(ms.chan_width_hz[-1], 1.0e7)


def test_optimize_drops_removed_channels_from_bookkeeping():
    nchan = 3
    freq = np.array([1.0e8, 2.0e8, 3.0e8])
    width = np.full(nchan, 1.0e7)
    # Channel 1 is flagged for every row and correlation -> removed.
    flags = np.zeros((1, nchan, 1), dtype=bool)
    flags[0, 1, 0] = True
    ms = _make_ms(nchan, flags=flags, chan_freq_hz=freq.copy(), chan_width_hz=width.copy())

    ms.optimize()

    assert ms.ds.DATA.shape[1] == 2
    assert np.allclose(ms.chan_freq_hz, [1.0e8, 3.0e8])
    assert np.allclose(ms.chan_width_hz, [1.0e7, 1.0e7])
    # The sub-table rewrite is driven by spw_chan_count vs the data shape.
    assert ms.spw_chan_count == 3
    assert ms.spw_chan_count != ms.ds.DATA.shape[1]
