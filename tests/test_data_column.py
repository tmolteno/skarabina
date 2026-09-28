# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""``--data-column``: fit the residual instead of DATA.

CASA's ``flagdata datacolumn``: the data-reading verbs (``nan``, ``clip``,
``rflag``, ``tfcrop``) can measure CORRECTED_DATA, MODEL_DATA or the
residuals RESIDUAL (CORRECTED_DATA - MODEL_DATA) and RESIDUAL_DATA
(DATA - MODEL_DATA) instead of DATA.  This is what the 1GC recipe's
``flagdata(datacolumn='residual')`` steps do on the calibrators.  The flags
always land on the FLAG column.
"""
import numpy as np
import pytest
import xarray as xr
from click.testing import CliRunner

from skarabina.dask_ms import DaskMS
from skarabina.rflag import RFlagParams

from casacore.tables import table  # noqa: E402
from ms_fixture import make_synthetic_ms  # noqa: E402
from skarabina.main import main  # noqa: E402
from test_rflag import make_plane, spike_plane  # noqa: E402


def _make_ms(data=None, corrected=None, model=None):
    """A synthetic MS carrying the three visibility columns."""
    if data is None:
        data = np.ones((4, 5, 1), dtype=complex)
    data = np.asarray(data, dtype=complex)
    nrow, nchan, ncorr = data.shape
    columns = {
        "DATA": (("row", "chan", "corr"), data),
        "FLAG": (("row", "chan", "corr"), np.zeros(data.shape, dtype=bool)),
        "WEIGHT_SPECTRUM": (("row", "chan", "corr"), np.ones(data.shape)),
        "UVW": (("row", "uvw"), np.zeros((nrow, 3))),
        "TIME": (("row",), np.arange(nrow, dtype=float) * 10.0),
        "ANTENNA1": (("row",), np.zeros(nrow, dtype=np.int32)),
        "ANTENNA2": (("row",), np.ones(nrow, dtype=np.int32)),
        "FLAG_ROW": (("row",), np.zeros(nrow, dtype=bool)),
    }
    if corrected is not None:
        columns["CORRECTED_DATA"] = (
            ("row", "chan", "corr"), np.asarray(corrected, dtype=complex),
        )
    if model is not None:
        columns["MODEL_DATA"] = (
            ("row", "chan", "corr"), np.asarray(model, dtype=complex),
        )
    ds = xr.Dataset(columns).chunk({"row": 2, "chan": nchan, "corr": ncorr})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.name = "<synthetic>"
    ms.sub_table_names = []
    ms._refresh_cached_columns()
    return ms


def test_residual_is_corrected_minus_model():
    corrected = np.ones((4, 5, 1), dtype=complex) * 3.0
    model = np.ones((4, 5, 1), dtype=complex) * 0.5
    ms = _make_ms(corrected=corrected, model=model)

    ms.set_data_column("RESIDUAL")

    assert np.allclose(np.asarray(ms._visibilities()), 2.5)


def test_residual_data_is_data_minus_model():
    data = np.ones((4, 5, 1), dtype=complex) * 4.0
    model = np.ones((4, 5, 1), dtype=complex)
    ms = _make_ms(data=data, model=model)

    ms.set_data_column("residual_data")   # spellings are CASA's, case-free

    assert np.allclose(np.asarray(ms._visibilities()), 3.0)


def test_nan_flags_the_residual_source_not_data():
    data = np.ones((4, 5, 1), dtype=complex)
    corrected = np.ones((4, 5, 1), dtype=complex)
    corrected[2] = np.nan                 # the residual will be NaN here
    model = np.ones((4, 5, 1), dtype=complex)

    ms = _make_ms(data=data, corrected=corrected, model=model)
    ms.set_data_column("RESIDUAL")
    ms.flag_data({"NAN": True})
    flags = np.asarray(ms.ds.FLAG.data)
    assert flags[2].all(), "the NaN residual must be flagged"
    assert not flags[:2].any(), "clean residuals stay unflagged"

    # The same MS fitted on DATA finds nothing: DATA has no NaN.
    plain = _make_ms(data=data, corrected=corrected, model=model)
    plain.flag_data({"NAN": True})
    assert not np.asarray(plain.ds.FLAG.data).any()


def test_rflag_measures_the_residual():
    # DATA is clean, but the residual (CORRECTED - MODEL) carries an RFI
    # channel: with --data-column RESIDUAL the flagger must find it, and
    # with the default DATA it must find almost nothing.
    rows = 64
    data = make_plane(ntime=rows, seed=7)[:, :, None]
    model = make_plane(ntime=rows, seed=5)[:, :, None]
    corrected = model + spike_plane(3.0, ntime=rows, seed=2)[:, :, None]

    ms = _make_ms(data=data, corrected=corrected, model=model)
    ms.set_data_column("RESIDUAL")
    ms.flag_rflag(RFlagParams())

    flags = np.asarray(ms.ds.FLAG.data)
    assert flags[:, 20, 0].mean() > 0.9, "the RFI channel of the residual"

    plain = _make_ms(data=data, corrected=corrected, model=model)
    plain.flag_rflag(RFlagParams())
    assert np.asarray(plain.ds.FLAG.data).mean() < 0.005, (
        "the DATA column is clean; fitting it must not flag the residual's RFI"
    )


def test_unknown_source_is_rejected_with_the_valid_ones():
    ms = _make_ms()
    with pytest.raises(RuntimeError, match="unknown --data-column"):
        ms.set_data_column("PARAM")
    with pytest.raises(RuntimeError, match="RESIDUAL_DATA"):
        ms.set_data_column("PARAM")


def test_missing_columns_are_reported():
    ms = _make_ms()  # no CORRECTED_DATA / MODEL_DATA
    with pytest.raises(RuntimeError, match="CORRECTED_DATA"):
        ms.set_data_column("RESIDUAL")
    # With CORRECTED_DATA present, the message names the other one.
    corrected = np.ones((4, 5, 1), dtype=complex)
    ms = _make_ms(corrected=corrected)
    with pytest.raises(RuntimeError, match="MODEL_DATA"):
        ms.set_data_column("RESIDUAL")


def test_cli_data_column_reaches_the_output(tmp_path):
    """End to end: --data-column RESIDUAL with --flag nan flags the row whose
    residual is NaN and leaves the clean rows alone."""
    path = make_synthetic_ms(str(tmp_path / "res.ms"), nchan=4, nrow=4)
    t = table(path, readonly=False, ack=False)
    corrected = np.ones((4, 4, 1), dtype=complex)
    corrected[2] = np.nan
    t.putcol("CORRECTED_DATA", corrected)
    t.putcol("MODEL_DATA", np.ones((4, 4, 1), dtype=complex))
    t.close()

    out = str(tmp_path / "out.ms")
    result = CliRunner().invoke(
        main,
        ["--ms", path, "--data-column", "RESIDUAL", "--flag", "nan",
         "--msout", out],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "flagging on CORRECTED_DATA - MODEL_DATA" in result.output

    t = table(out, ack=False)
    try:
        flags = np.asarray(t.getcol("FLAG"))
        assert flags[2].all(), "the NaN residual row is flagged"
        assert not flags[:2].any()
    finally:
        t.close()
