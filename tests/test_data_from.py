# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""``--data-from``: the written DATA column comes from another column.

``mstransform(datacolumn='corrected')`` semantics: the split's output holds
the corrected visibilities as DATA, which is what a downstream imager reads.
The substitution runs before the averaging factors, so they average the
substituted column exactly as mstransform would.
"""
import numpy as np
import pytest
import xarray as xr
from click.testing import CliRunner

from skarabina.dask_ms import DaskMS

from casacore.tables import table  # noqa: E402
from ms_fixture import make_synthetic_ms  # noqa: E402
from skarabina.main import main  # noqa: E402


def _run(*args):
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result


def _read(path, column):
    t = table(path, ack=False)
    try:
        return np.asarray(t.getcol(column))
    finally:
        t.close()


def _ms_with_columns(tmp_path, nrow=4, nchan=4):
    path = make_synthetic_ms(str(tmp_path / "cols.ms"), nrow=nrow, nchan=nchan)
    t = table(path, readonly=False, ack=False)
    corrected = np.ones((nrow, nchan, 1), dtype=complex) * 2.0
    model = np.ones((nrow, nchan, 1), dtype=complex) * 0.5
    t.putcol("CORRECTED_DATA", corrected)
    t.putcol("MODEL_DATA", model)
    t.close()
    return path, corrected, model


def test_corrected_is_written_as_data(tmp_path):
    path, corrected, _ = _ms_with_columns(tmp_path)
    out = str(tmp_path / "out.ms")

    _run("--ms", path, "--data-from", "CORRECTED", "--msout", out)

    assert np.allclose(_read(out, "DATA"), corrected), (
        "the output's DATA must hold the corrected visibilities"
    )
    assert np.allclose(_read(path, "DATA"), 1.0), "the input is untouched"


def test_model_is_written_as_data(tmp_path):
    path, _, model = _ms_with_columns(tmp_path)
    out = str(tmp_path / "out.ms")

    _run("--ms", path, "--data-from", "MODEL", "--msout", out)

    assert np.allclose(_read(out, "DATA"), model)


def test_averaging_averages_the_substituted_column(tmp_path):
    """The substitution runs before the averaging factors: what comes out is
    the average of CORRECTED_DATA, not of the original DATA."""
    nrow, nchan = 4, 4
    path = make_synthetic_ms(str(tmp_path / "avg.ms"), nrow=nrow, nchan=nchan)
    t = table(path, readonly=False, ack=False)
    corrected = np.ones((nrow, nchan, 1), dtype=complex)
    corrected[0] = 1.0
    corrected[1] = 3.0
    corrected[2] = 5.0
    corrected[3] = 7.0
    t.putcol("CORRECTED_DATA", corrected)
    t.close()

    out = str(tmp_path / "out.ms")
    _run("--ms", path, "--data-from", "CORRECTED", "--time-average-factor", "2",
         "--msout", out)

    data = _read(out, "DATA")
    assert data.shape == (2, nchan, 1)
    assert np.allclose(data[0], 2.0), "mean of the first pair of corrected rows"
    assert np.allclose(data[1], 6.0), "mean of the second pair"


def test_unknown_and_missing_sources():
    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), np.ones((2, 3, 1), dtype=complex)),
            "FLAG": (("row", "chan", "corr"), np.zeros((2, 3, 1), dtype=bool)),
        }
    ).chunk({"row": 2, "chan": 3, "corr": 1})
    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.name = "<synthetic>"
    ms.sub_table_names = []

    with pytest.raises(RuntimeError, match="unknown --data-from"):
        ms.rebase_data_column("RESIDUAL")
    with pytest.raises(RuntimeError, match="CORRECTED_DATA"):
        ms.rebase_data_column("CORRECTED")
