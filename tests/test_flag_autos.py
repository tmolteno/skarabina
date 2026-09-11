# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for autocorrelation flagging (``--flag-autos``).

Auto baselines (``ANTENNA1 == ANTENNA2``) measure the total power of a single
antenna: no fringes, so they are useless for imaging and calibration.  Flagging
them must mark every visibility of those rows *and* set ``FLAG_ROW``, so that
``--optimize`` can drop the rows altogether -- and it must leave
cross-correlations alone.
"""
import numpy as np
import pytest
import xarray as xr
from casacore.tables import table

from skarabina.dask_ms import DaskMS
from ms_fixture import make_synthetic_ms


def _make_ms(ant1, ant2, nchan=3, ncorr=2, flags=None):
    """Synthetic MS with explicit antenna pairs."""
    nrow = len(ant1)
    data = np.ones((nrow, nchan, ncorr), dtype=complex)
    flag = (
        np.zeros((nrow, nchan, ncorr), dtype=bool)
        if flags is None
        else np.asarray(flags, dtype=bool)
    )
    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), data),
            "FLAG": (("row", "chan", "corr"), flag),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), np.ones(data.shape)),
            "UVW": (("row", "uvw"), np.zeros((nrow, 3))),
            "TIME": (("row",), np.arange(nrow, dtype=float)),
            "ANTENNA1": (("row",), np.asarray(ant1, dtype=np.int32)),
            "ANTENNA2": (("row",), np.asarray(ant2, dtype=np.int32)),
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


def test_only_auto_rows_are_flagged():
    ms = _make_ms([0, 1, 0, 1], [0, 1, 1, 0])  # rows 0,1 auto; rows 2,3 cross

    ms.flag_autocorrelations()

    flags = np.asarray(ms.ds.FLAG.data)
    flag_row = np.asarray(ms.ds.FLAG_ROW.data)
    assert flag_row.tolist() == [True, True, False, False]
    assert flags[0].all() and flags[1].all()
    assert not flags[2].any() and not flags[3].any()


def test_existing_flags_are_preserved():
    flags = np.zeros((3, 3, 2), dtype=bool)
    flags[2, 1, 0] = True  # a flag on a cross-correlation row
    ms = _make_ms([0, 1, 0], [0, 1, 1], flags=flags)

    ms.flag_autocorrelations()

    out = np.asarray(ms.ds.FLAG.data)
    assert out[0].all() and out[1].all()  # autos flagged
    assert out[2, 1, 0] and not out[2].all()  # pre-existing flag kept


def test_missing_antenna_columns_raise():
    ms = _make_ms([0, 1, 0], [0, 1, 1])
    ms.ds = ms.ds.drop_vars("ANTENNA2")
    with pytest.raises(RuntimeError, match="ANTENNA2"):
        ms.flag_autocorrelations()


def test_optimize_drops_the_auto_rows():
    ms = _make_ms([0, 1, 0, 1, 0], [0, 1, 1, 0, 0])  # rows 0, 1 and 4 are auto

    ms.flag_autocorrelations()
    ms.optimize()

    assert ms.ds.DATA.shape[0] == 2, "auto rows should be removable by optimize"


def test_write_ms_carries_the_auto_flags(tmp_path):
    """End-to-end: the flags reach the written MS."""
    in_ms = make_synthetic_ms(tmp_path / "in.ms", nchan=4, nrow=6, auto_rows=2)

    ms = DaskMS(in_ms)
    ms.flag_autocorrelations()
    out_ms = str(tmp_path / "out.ms")
    ms.write_new_ms(out_ms, clobber=True)

    t = table(out_ms, ack=False)
    try:
        flag_row = np.asarray(t.getcol("FLAG_ROW"))
        flags = np.asarray(t.getcol("FLAG"))
        ant1 = np.asarray(t.getcol("ANTENNA1"))
        ant2 = np.asarray(t.getcol("ANTENNA2"))
    finally:
        t.close()

    auto = ant1 == ant2
    assert auto.sum() == 2
    assert flag_row.tolist() == auto.tolist()
    assert flags[auto].all(), "every visibility of an auto row must be flagged"
    assert not flags[~auto].any(), "cross-correlations must be left alone"
