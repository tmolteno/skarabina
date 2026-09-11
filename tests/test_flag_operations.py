# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for the flag_data operation mapping (``--flag-nan`` / ``--flag-clip``).

``--flag-nan`` used to be tested with ``is not None`` against a click flag whose
default is ``False``, so NaN flagging happened even when it was not requested.
These tests pin the contract: each switch is honoured independently.
"""
import numpy as np
import pytest
import xarray as xr

from skarabina.dask_ms import DaskMS
from skarabina.main import build_flag_data_operations


@pytest.mark.parametrize(
    "flag_nan,flag_clip,expected",
    [
        (False, None, {}),
        (True, None, {"NAN": True}),
        (False, (0.0, 100.0), {"CLIP": (0.0, 100.0)}),
        (False, [0.0, 100.0], {"CLIP": (0.0, 100.0)}),
        (True, (0.0, 100.0), {"NAN": True, "CLIP": (0.0, 100.0)}),
    ],
)
def test_build_flag_data_operations(flag_nan, flag_clip, expected):
    assert build_flag_data_operations(flag_nan, flag_clip) == expected


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
    ms.flag_data(build_flag_data_operations(False, None))
    assert not np.asarray(ms.ds.FLAG.data)[0, 0, 0], (
        "NaN flagging must not run unless --flag-nan is given"
    )

    ms.flag_data(build_flag_data_operations(True, None))
    assert np.asarray(ms.ds.FLAG.data)[0, 0, 0]


def test_clip_flags_out_of_range_visibilities():
    ms = _make_ms([[[1.0 + 0j]], [[1.0e6 + 0j]], [[0.0 + 0j]]])

    ms.flag_data(build_flag_data_operations(False, (0.0, 100.0)))
    flags = np.asarray(ms.ds.FLAG.data).reshape(-1)
    assert not flags[0]  # 1 Jy is inside the range
    assert flags[1]  # 1 MJy is above it
    assert flags[2]  # exactly zero is at the lower bound
