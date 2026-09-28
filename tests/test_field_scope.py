# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""``--field``: the flag verbs confine their new flags to the selected fields.

CASA's ``field=`` selection for flagging.  Every verb's *new* flags are
scoped to the listed fields' rows -- flags that are already set are never
cleared -- while ``save:``/``restore:`` stay whole-table and the write-out
keeps every field.  The synthetic MS interleaves two fields in time on
shared baselines, so a time-neighbour growth that ignored the scope would
bleed from one field's row into the other's and be caught here.
"""
import numpy as np
import pytest
import xarray as xr
from click.testing import CliRunner

from skarabina.dask_ms import DaskMS
from skarabina.extend import ExtendParams
from skarabina.rflag import RFlagParams

from casacore.tables import table  # noqa: E402
from ms_fixture import make_synthetic_ms  # noqa: E402
from skarabina.main import main  # noqa: E402
from test_rflag import spike_plane  # noqa: E402


def _make_ms(flags=None, field_ids=(0, 0, 1, 1), ant1=None, ant2=None,
             data=None, uvw_u=None):
    """A synthetic two-field MS: rows in chunks of two."""
    field_ids = np.asarray(field_ids, dtype=np.int32)
    nrow = field_ids.size
    nchan, ncorr = 5, 1
    if flags is None:
        flags = np.zeros((nrow, nchan, ncorr), dtype=bool)
    if data is None:
        data = np.ones((nrow, nchan, ncorr), dtype=complex)
    if ant1 is None:
        ant1 = np.zeros(nrow, dtype=np.int32)
        ant2 = np.ones(nrow, dtype=np.int32)
    uvw = np.zeros((nrow, 3))
    if uvw_u is not None:
        uvw[:, 0] = uvw_u

    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), np.asarray(data, dtype=complex)),
            "FLAG": (("row", "chan", "corr"), np.asarray(flags, dtype=bool)),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), np.ones(flags.shape)),
            "UVW": (("row", "uvw"), uvw),
            "TIME": (("row",), np.arange(nrow, dtype=float) * 10.0),
            "ANTENNA1": (("row",), np.asarray(ant1, dtype=np.int32)),
            "ANTENNA2": (("row",), np.asarray(ant2, dtype=np.int32)),
            "FLAG_ROW": (("row",), np.zeros(nrow, dtype=bool)),
            "FIELD_ID": (("row",), field_ids),
        }
    ).chunk({"row": 2, "chan": nchan, "corr": ncorr})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.name = "<synthetic>"
    ms.sub_table_names = []
    ms._refresh_cached_columns()
    return ms


def test_autos_flag_only_the_scoped_fields_rows():
    # Row 0: an auto in field 0; row 2: an auto in field 1.
    ms = _make_ms(
        ant1=[0, 0, 0, 0], ant2=[0, 1, 0, 1], field_ids=[0, 0, 1, 1],
    )
    ms.set_field_scope("1")

    ms.flag_autocorrelations()

    flags = np.asarray(ms.ds.FLAG.data)
    flag_row = np.asarray(ms.ds.FLAG_ROW.data)
    assert flags[2].all() and flag_row[2], "the scoped field's auto is flagged"
    assert not flags[0].any() and not flag_row[0], (
        "the other field's auto must be left alone"
    )


def test_nan_flagging_is_scoped():
    data = np.ones((4, 5, 1), dtype=complex)
    data[0] = np.nan          # field 0
    data[2] = np.nan          # field 1
    ms = _make_ms(data=data)
    ms.set_field_scope("1")

    ms.flag_data({"NAN": True})

    flags = np.asarray(ms.ds.FLAG.data)
    assert flags[2].all(), "the scoped field's NaN row is flagged"
    assert not flags[0].any(), "the other field's NaN row is untouched"


def test_uv_above_is_scoped():
    ms = _make_ms(uvw_u=[9000.0, 100.0, 9000.0, 100.0])
    ms.set_field_scope("1")

    ms.flag_uv_above(8000.0)

    flag_row = np.asarray(ms.ds.FLAG_ROW.data)
    assert flag_row.tolist() == [False, False, True, False]


def test_extend_growth_stays_inside_the_scope():
    # One baseline, four integrations, fields interleaved by pair: row 2's
    # time neighbours are rows 1 (field 0) and 3 (field 1).  Scoping to
    # field 1, the growth must reach row 3 but not row 1.
    flags = np.zeros((4, 5, 1), dtype=bool)
    flags[2, 2, 0] = True
    ms = _make_ms(flags=flags, field_ids=[0, 0, 1, 1])
    ms.set_field_scope("1")

    ms.flag_extend(ExtendParams(flagneartime=True))

    out = np.asarray(ms.ds.FLAG.data)
    assert out[3, 2, 0], "the in-field neighbour is flagged"
    assert not out[1, 2, 0], "the other field's neighbour must not be reached"
    assert out.sum() == 2


def test_rflag_flags_only_the_scoped_fields_rfi():
    """The auto-flaggers' blocks are scoped in the block wrapper: the
    statistics may see every row, but the flags they produce land only in
    the selected field."""
    plane = spike_plane(3.0, ntime=64)
    data = np.repeat(plane[:, :, None], 1, axis=2).astype(complex)
    flags = np.zeros(data.shape, dtype=bool)
    ms = _make_ms(flags=flags, data=data, field_ids=[0] * 32 + [1] * 32)
    ms.set_field_scope("0")

    ms.flag_rflag(RFlagParams())

    out = np.asarray(ms.ds.FLAG.data)
    assert out[:32, 20, 0].mean() > 0.9, "the scoped field's RFI channel flagged"
    assert not out[32:].any(), "not one flag outside the scoped field"


def test_set_field_scope_resolves_names_and_rejects_unknown(tmp_path):
    path = make_synthetic_ms(
        str(tmp_path / "fields.ms"), nrow=4, nchan=3,
        field_ids=[0, 0, 1, 1], field_names=("CAL", "PCAL"),
    )
    ms = DaskMS(path, row_chunk=1000)

    ms.set_field_scope("PCAL, 0")
    assert ms.field_scope.tolist() == [True, True, True, True]

    with pytest.raises(RuntimeError, match="not found"):
        ms.set_field_scope("NOPE")


def test_cli_field_scopes_autos_but_keeps_every_field(tmp_path):
    # Autos in rows 0 and 1 (fields CAL and PCAL); --field CAL must flag
    # only row 0, and the applied MS keeps all four rows.
    path = make_synthetic_ms(
        str(tmp_path / "cli.ms"), nrow=4, nchan=4,
        field_ids=[0, 1, 0, 1], field_names=("CAL", "PCAL"), auto_rows=2,
    )
    result = CliRunner().invoke(
        main,
        ["--ms", path, "--field", "CAL", "--flag", "autos", "--apply",
         "--clobber"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "flagging 2 of 4 rows, fields [0]" in result.output

    t = table(path, ack=False)
    try:
        assert t.nrows() == 4, "--field is not a row selection"
        assert np.asarray(t.getcol("FLAG_ROW")).tolist() == [
            True, False, False, False,
        ]
    finally:
        t.close()
