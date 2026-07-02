# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import types

import dask.array as da
import numpy as np

from skarabina.barber import barber


def test_barber_runs_on_synthetic_data(capsys):
    """barber() should produce a max-visibility report from in-memory data."""
    shape = (4, 3, 2)  # dumps, channels, polarizations
    data = np.zeros(shape, dtype=complex)
    data[1, 2, 0] = 10.0 + 0j  # a clear maximum
    ms = types.SimpleNamespace(
        data=da.from_array(data),
        flag=da.from_array(np.zeros(shape, dtype=bool)),
        weight_spectrum=da.from_array(np.ones(shape)),
        antenna1=da.from_array(np.array([0, 1, 2, 3])),
        antenna2=da.from_array(np.array([1, 2, 3, 0])),
        time=da.from_array(np.array([0.0, 1.0, 2.0, 3.0])),
    )

    barber(ms, pol=None)

    out = capsys.readouterr().out
    assert "Max |v| = 10" in out
    assert "Percentiles" in out
