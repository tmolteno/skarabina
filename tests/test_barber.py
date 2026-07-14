# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""barber() now reads columns from the live dataset (ms.ds.FLAG.data etc.)
rather than the stale ms.flag/ms.data __init__ snapshots.  This test
builds a minimal dataset-backed DaskMS to match."""
import numpy as np
import xarray as xr

from skarabina.barber import barber
from skarabina.dask_ms import DaskMS


def _make_ms_from_dataset(data, flag, weight, antenna1, antenna2, time):
    nrow = data.shape[0]
    uvw = np.zeros((nrow, 3))
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
    ms.chan_freq_hz = None
    ms.name = "<synthetic>"
    return ms


def test_barber_runs_on_synthetic_data(capsys):
    """barber() should produce a max-visibility report from in-memory data."""
    shape = (4, 3, 2)  # dumps, channels, polarizations
    data = np.zeros(shape, dtype=complex)
    data[1, 2, 0] = 10.0 + 0j  # a clear maximum
    ms = _make_ms_from_dataset(
        data=data,
        flag=np.zeros(shape, dtype=bool),
        weight=np.ones(shape),
        antenna1=np.array([0, 1, 2, 3]),
        antenna2=np.array([1, 2, 3, 0]),
        time=np.array([0.0, 1.0, 2.0, 3.0]),
    )

    barber(ms, pol=None)

    out = capsys.readouterr().out
    assert "Max |v| = 10" in out
    assert "Percentiles" in out
