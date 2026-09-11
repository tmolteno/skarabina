# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for scan selection (``--scan``).

``select_scans`` filters rows at read time, so flagging, averaging,
``optimize`` and the write-out all see the selected scans.  It must also
refresh the column snapshots taken in ``__init__`` -- the flagging methods
used to read those, and would otherwise work on the pre-selection arrays.
"""
import dask.array as da
import numpy as np
import pytest
import xarray as xr

from skarabina.dask_ms import DaskMS, parse_scan_spec


def _make_ms(scan_numbers, nchan=4, ncorr=1):
    """Synthetic single-DDID MS with a SCAN_NUMBER column."""
    scan_numbers = np.asarray(scan_numbers, dtype=np.int32)
    nrow = scan_numbers.size
    data = np.ones((nrow, nchan, ncorr), dtype=complex)
    flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    uvw = np.stack(
        [np.arange(nrow, dtype=float) * 100.0, np.zeros(nrow), np.zeros(nrow)],
        axis=1,
    )

    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), data),
            "FLAG": (("row", "chan", "corr"), flag),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), np.ones(data.shape)),
            "UVW": (("row", "uvw"), uvw),
            "TIME": (("row",), np.arange(nrow, dtype=float)),
            "ANTENNA1": (("row",), np.zeros(nrow, dtype=np.int32)),
            "ANTENNA2": (("row",), np.ones(nrow, dtype=np.int32)),
            "FLAG_ROW": (("row",), np.zeros(nrow, dtype=bool)),
            "SCAN_NUMBER": (("row",), scan_numbers),
        }
    ).chunk({"row": nrow, "chan": nchan, "corr": ncorr})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.name = "<synthetic>"
    ms.sub_table_names = []
    ms._refresh_cached_columns()
    return ms


@pytest.mark.parametrize(
    "spec,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("1", [1]),
        ("1,12,14", [1, 12, 14]),
        ("0~5", [0, 1, 2, 3, 4, 5]),
        ("0~5,20,30~32", [0, 1, 2, 3, 4, 5, 20, 30, 31, 32]),
        ("3~1", [1, 2, 3]),  # reversed range is normalised
        (" 1 , 2 ", [1, 2]),  # whitespace tolerated
        ("1,", [1]),  # trailing comma tolerated
        ("2,2,1", [1, 2]),  # duplicates collapse
    ],
)
def test_parse_scan_spec(spec, expected):
    assert parse_scan_spec(spec) == expected


@pytest.mark.parametrize("spec", ["abc", "1~x", "~", "1~2~3"])
def test_parse_scan_spec_rejects_garbage(spec):
    with pytest.raises(RuntimeError):
        parse_scan_spec(spec)


def test_select_scans_keeps_only_selected_rows():
    ms = _make_ms([0, 1, 2, 3, 4])
    ms.select_scans("1,3")

    assert ms.ds.DATA.shape[0] == 2
    assert list(np.asarray(ms.ds.SCAN_NUMBER.data)) == [1, 3]
    # The __init__ snapshots must be refreshed along with the dataset.
    assert ms.data.shape[0] == 2
    assert ms.u_arr.shape[0] == 2
    assert ms.flag_row.shape[0] == 2


def test_select_scans_range_and_empty_spec():
    ms = _make_ms([0, 1, 2, 3, 4])
    ms.select_scans("1~3")
    assert list(np.asarray(ms.ds.SCAN_NUMBER.data)) == [1, 2, 3]

    untouched = _make_ms([0, 1, 2, 3, 4])
    untouched.select_scans("")
    assert untouched.ds.DATA.shape[0] == 5

    untouched = _make_ms([0, 1, 2, 3, 4])
    untouched.select_scans(None)
    assert untouched.ds.DATA.shape[0] == 5


def test_flagging_after_scan_selection():
    """flag_uv_above/flag_data must operate on the selected rows only."""
    ms = _make_ms([0, 1, 2, 3, 4])
    ms.select_scans("1,3")

    # uvw distances are 100 m and 300 m after selection.
    ms.flag_uv_above(150)
    assert list(np.asarray(ms.ds.FLAG_ROW.data)) == [False, True]

    # NaN flagging still works on the reduced dataset (shape must match).
    data = np.array(ms.ds.DATA.data)
    data[0, 0, 0] = np.nan
    ms.ds["DATA"].data = da.from_array(data, chunks=data.shape)

    ms.flag_data({"NAN": True})
    flags = np.asarray(ms.ds.FLAG.data)
    assert flags.shape[0] == 2
    assert flags[0, 0, 0]
    assert not flags[1, 0, 0]


def test_select_scans_without_scan_column_raises():
    ms = _make_ms([0, 1, 2])
    ms.ds = ms.ds.drop_vars("SCAN_NUMBER")
    with pytest.raises(RuntimeError, match="SCAN_NUMBER"):
        ms.select_scans("1")


def test_select_scans_with_no_matching_rows_raises():
    ms = _make_ms([0, 1, 2])
    with pytest.raises(RuntimeError, match="no rows"):
        ms.select_scans("99")
