# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""``flag_data`` only flags what it is asked to.

``--flag-nan`` was once tested with ``is not None`` against a click flag whose
default is ``False``, so NaN flagging happened even when it was not requested.
The switches are gone in favour of ``--flag nan`` / ``--flag "clip lo hi"``, and
the contract they guarded still holds: each operation is honoured independently,
and nothing runs that was not asked for.
"""
import numpy as np
import xarray as xr

from skarabina.dask_ms import DaskMS


def _make_ms(values):
    """Synthetic MS whose DATA array is ``values`` (nrow, nchan, ncorr)."""
    data = np.asarray(values, dtype=complex)
    nrow, nchan, ncorr = data.shape
    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), data),
            "FLAG": (("row", "chan", "corr"), np.zeros(data.shape, dtype=bool)),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), np.ones(data.shape)),
            "UVW": (("row", "uvw"), np.zeros((nrow, 3))),
            "TIME": (("row",), np.zeros(nrow)),
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
    ms._refresh_cached_columns()
    return ms


def test_nan_flagging_only_when_requested():
    ms = _make_ms([[[np.nan], [1.0 + 0j]], [[1.0 + 0j], [1.0 + 0j]]])
    ms.flag_data({})
    assert not np.asarray(ms.ds.FLAG.data)[0, 0, 0], (
        "NaN flagging must not run unless nan is in the --flag list"
    )

    ms.flag_data({"NAN": True})
    assert np.asarray(ms.ds.FLAG.data)[0, 0, 0]


def test_clip_flags_out_of_range_visibilities():
    ms = _make_ms([[[1.0 + 0j]], [[1.0e6 + 0j]], [[0.0 + 0j]]])

    ms.flag_data({"CLIP": (0.0, 100.0)})
    flags = np.asarray(ms.ds.FLAG.data).reshape(-1)
    assert not flags[0]  # 1 Jy is inside the range
    assert flags[1]  # 1 MJy is above it
    assert flags[2]  # exactly zero is at the lower bound


def test_operations_are_independent():
    """Asking for one operation must not perform the other."""
    ms = _make_ms([[[np.nan]], [[1.0e6 + 0j]]])
    ms.flag_data({"CLIP": (0.0, 100.0)})
    flags = np.asarray(ms.ds.FLAG.data).reshape(-1)
    assert flags[1], "clip should flag the out-of-range visibility"
    # the NaN row is still unflagged, because nan was not requested
    assert not flags[0]


def test_deferred_statistics_do_not_change_the_flags():
    """``defer`` must be a reporting change only."""
    values = [[[np.nan]], [[1.0e6 + 0j]], [[1.0 + 0j]]]

    eager = _make_ms(values)
    eager.flag_data({"NAN": True})
    eager.flag_data({"CLIP": (0.0, 100.0)})

    deferred = _make_ms(values)
    sink = {}
    deferred.flag_data({"NAN": True}, defer=sink)
    deferred.flag_data({"CLIP": (0.0, 100.0)}, defer=sink)
    assert len(sink) == 2, "both operations should be deferred"

    assert np.array_equal(
        np.asarray(eager.ds.FLAG.data), np.asarray(deferred.ds.FLAG.data)
    )
    # the deferred reductions must still give the same counts
    deferred.report_data_flags(sink)
