# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Guards for averaging factors that exceed the size of the MS.

``--frequency-average-factor``/``--time-average-factor`` larger than the number
of channels/rows would produce an empty result (and an empty output MS), which
looks like data loss rather than a bad parameter.  Both now decline to average
and say so.
"""
import numpy as np
import xarray as xr

from skarabina.dask_ms import DaskMS


def _make_ms(nrow=4, nchan=4, ncorr=1):
    data = np.ones((nrow, nchan, ncorr), dtype=complex)
    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), data),
            "FLAG": (("row", "chan", "corr"), np.zeros(data.shape, dtype=bool)),
            "FLAG_ROW": (("row",), np.zeros(nrow, dtype=bool)),
            "UVW": (("row", "uvw"), np.zeros((nrow, 3))),
            "TIME": (("row",), np.arange(nrow, dtype=float)),
            "ANTENNA1": (("row",), np.zeros(nrow, dtype=np.int32)),
            "ANTENNA2": (("row",), np.ones(nrow, dtype=np.int32)),
        }
    ).chunk({"row": nrow, "chan": nchan, "corr": ncorr})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.name = "<synthetic>"
    ms.sub_table_names = []
    ms.chan_freq_hz = np.arange(nchan, dtype=float) * 1e7 + 1e9
    ms.chan_width_hz = np.full(nchan, 1e7)
    ms.spw_chan_count = nchan
    ms._refresh_cached_columns()
    return ms


def test_frequency_average_factor_larger_than_channel_count_is_a_no_op():
    ms = _make_ms(nchan=4)
    ms.frequency_average(8)

    assert ms.ds.DATA.shape[1] == 4, "channels must not be dropped"
    assert ms.chan_freq_hz.size == 4


def test_time_average_factor_larger_than_row_count_is_a_no_op():
    ms = _make_ms(nrow=4)
    ms.time_average(8)

    assert ms.ds.DATA.shape[0] == 4, "rows must not be dropped"
