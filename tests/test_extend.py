# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The ``extend`` verb -- flag growth into neighbours.

``flagdata(mode='extend')`` with CASA's parameter names and defaults; the
semantics pinned here are the ones ``skarabina/extend.py`` documents.  The
synthetic MS is chunked in rows of two, so the growth runs through real
chunked reductions rather than one block, and half the tests use two
baselines whose time axes overlap -- the growth must never bleed from one
baseline's flagged sample into another baseline's neighbouring row.
"""
import numpy as np
import pytest
import xarray as xr

from skarabina.dask_ms import DaskMS
from skarabina.extend import ExtendParams
from skarabina.flag_ops import FlagOrderError, parse


def _make_ms(flags, ant1=None, ant2=None, scan=None):
    """A synthetic MS: rows in chunks of two, optional scan numbers."""
    flags = np.asarray(flags, dtype=bool)
    nrow, nchan, ncorr = flags.shape
    if ant1 is None:
        ant1 = np.zeros(nrow, dtype=np.int32)
        ant2 = np.ones(nrow, dtype=np.int32)
    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), np.ones(flags.shape, dtype=complex)),
            "FLAG": (("row", "chan", "corr"), flags),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), np.ones(flags.shape)),
            "UVW": (("row", "uvw"), np.zeros((nrow, 3))),
            "TIME": (("row",), np.arange(nrow, dtype=float) * 10.0),
            "ANTENNA1": (("row",), np.asarray(ant1, dtype=np.int32)),
            "ANTENNA2": (("row",), np.asarray(ant2, dtype=np.int32)),
            "FLAG_ROW": (("row",), np.zeros(nrow, dtype=bool)),
        }
    )
    if scan is not None:
        ds["SCAN_NUMBER"] = (("row",), np.asarray(scan, dtype=np.int32))
    ds = ds.chunk({"row": 2, "chan": nchan, "corr": ncorr})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.name = "<synthetic>"
    ms.sub_table_names = []
    ms._refresh_cached_columns()
    return ms


def _extend(ms, **kwargs):
    ms.flag_extend(ExtendParams(**kwargs))
    return np.asarray(ms.ds.FLAG.data)


def test_extendpols_flags_all_correlations():
    flags = np.zeros((3, 5, 2), dtype=bool)
    flags[1, 2, 0] = True
    ms = _make_ms(flags)

    out = _extend(ms)  # defaults: extendpols on, growth thresholds too high
    # to fire on 1-of-3 / 1-of-5

    assert out[1, 2].all(), "a one-correlation flag must reach the other"
    assert out.sum() == 2, "no other visibility may be flagged"


def test_extendpols_false_grows_each_correlation_on_its_own():
    flags = np.zeros((3, 5, 2), dtype=bool)
    flags[1, 2, 0] = True
    ms = _make_ms(flags)

    out = _extend(ms, extendpols=False, flagneartime=True)

    assert out[0, 2, 0] and out[2, 2, 0], "grown along time in corr 0"
    assert not out[1, 2, 1], "extendpols=false must not leak into corr 1"
    assert not out[0, 2, 1] and not out[2, 2, 1]


def test_flagneartime_grows_one_timestep_on_the_same_baseline():
    # Rows 0-3: baseline (0,1); rows 4-5: baseline (0,2).  The time axes
    # overlap (rows 4,5 sit at t=0,10), so a row-neighbour growth would bleed.
    flags = np.zeros((6, 5, 1), dtype=bool)
    flags[1, 2, 0] = True
    ms = _make_ms(
        flags,
        ant1=[0, 0, 0, 0, 0, 0], ant2=[1, 1, 1, 1, 2, 2],
    )

    out = _extend(ms, extendpols=False, flagneartime=True)

    assert out[0, 2, 0] and out[1, 2, 0] and out[2, 2, 0]
    assert not out[3, 2, 0], "growth stops one timestep away"
    assert not out[4:].any(), (
        "the other baseline's rows share the times but not the baseline"
    )


def test_flagnearfreq_grows_one_channel():
    flags = np.zeros((3, 5, 1), dtype=bool)
    flags[1, 2, 0] = True
    ms = _make_ms(flags)

    out = _extend(ms, extendpols=False, flagnearfreq=True)

    assert out[1, 1, 0] and out[1, 2, 0] and out[1, 3, 0]
    assert not out[1, 0, 0] and not out[1, 4, 0], "only one channel either side"
    assert out.sum() == 3


def test_growaround_flags_the_eight_neighbourhood():
    flags = np.zeros((5, 5, 1), dtype=bool)
    flags[2, 2, 0] = True
    ms = _make_ms(flags)

    out = _extend(ms, extendpols=False, growaround=True)

    assert out[1:4, 1:4, 0].all(), "the 3x3 block around the flag is flagged"
    assert out.sum() == 9, "nothing outside the block"


def test_growtime_flags_a_mostly_flagged_scan():
    # Scans 1 and 2 of one baseline.  Scan 1 has 2 of 3 integrations flagged
    # at channel 0 (66% > growtime's 50%): the third must follow.  Scan 2 is
    # clean and must stay clean -- growtime grows within its scan only.
    flags = np.zeros((6, 5, 1), dtype=bool)
    flags[0, 0, 0] = True
    flags[1, 0, 0] = True
    ms = _make_ms(flags, scan=[1, 1, 1, 2, 2, 2])

    out = _extend(ms)

    assert out[2, 0, 0], "the rest of the mostly-flagged scan follows"
    assert not out[3:, 0, 0].any(), "the clean scan is untouched"
    assert out.sum() == 3


def test_growtime_respects_its_threshold():
    # The pipeline's recipes run growtime=90: 66% must NOT grow.
    flags = np.zeros((6, 5, 1), dtype=bool)
    flags[0, 0, 0] = True
    flags[1, 0, 0] = True
    ms = _make_ms(flags, scan=[1, 1, 1, 2, 2, 2])

    out = _extend(ms, growtime=90)

    assert not out[2, 0, 0]
    assert out.sum() == 2


def test_growtime_zero_disables_the_rule():
    flags = np.zeros((6, 5, 1), dtype=bool)
    flags[0, 0, 0] = True
    flags[1, 0, 0] = True
    ms = _make_ms(flags, scan=[1, 1, 1, 2, 2, 2])

    out = _extend(ms, growtime=0)

    assert not out[2, 0, 0]
    assert out.sum() == 2


def test_growfreq_flags_a_mostly_flagged_row():
    # Row 1 has 4 of 5 channels flagged (80% > 50%): channel 4 follows.
    # Row 2 has 2 of 5 (40%): it stays as it is.  growtime off so the
    # channel-majority columns cannot grow along time on their own.
    flags = np.zeros((3, 5, 1), dtype=bool)
    flags[1, 0:4, 0] = True
    flags[2, 0:2, 0] = True
    ms = _make_ms(flags)

    out = _extend(ms, growtime=0)

    assert out[1].all(), "the mostly-flagged row becomes fully flagged"
    assert out[2].sum() == 2, "the 40% row does not grow"


def test_growfreq_zero_disables_the_rule():
    flags = np.zeros((3, 5, 1), dtype=bool)
    flags[1, 0:4, 0] = True
    ms = _make_ms(flags)

    out = _extend(ms, growtime=0, growfreq=0)

    assert not out[1, 4, 0]
    assert out.sum() == 4


def test_growth_sums_across_row_chunks():
    """The flagged fractions are a reduction over chunks: a group split
    across chunks must still be counted whole."""
    # 8 rows in chunks of two, one baseline: rows 0-5 flagged at chan 0
    # (6 of 8 = 75% > 50) -- rows 6,7 must follow even though they live in
    # the last chunk.
    flags = np.zeros((8, 3, 1), dtype=bool)
    flags[0:6, 0, 0] = True
    ms = _make_ms(flags)

    out = _extend(ms, growfreq=0)

    assert out[:, 0, 0].all()


@pytest.mark.parametrize("extendpols", [True, False])
def test_extend_keeps_flags_row_chunks(extendpols):
    """The grown FLAG must keep FLAG's row chunks: the growth runs per
    time-neighbour group, and a FLAG chunked by those groups no longer matches
    ROWID, which the in-place write refuses."""
    flags = np.zeros((8, 3, 2), dtype=bool)
    flags[3, 1, 0] = True
    ms = _make_ms(flags, ant1=[0, 0, 0, 0, 1, 1, 1, 1], ant2=[1, 2, 1, 2, 2, 3, 2, 3])
    chunks = ms.ds.FLAG.data.chunks

    ms.flag_extend(ExtendParams(flagneartime=True, extendpols=extendpols))

    assert ms.ds.FLAG.data.chunks == chunks


def test_extend_applies_in_place(tmp_path):
    """`--flag extend --apply` writes the grown flags back (1.0.12 raised
    'ROWID shape and/or chunking does not match that of FLAG'), with and
    without --field."""
    from casacore.tables import table
    from click.testing import CliRunner

    from ms_fixture import make_synthetic_ms
    from skarabina.main import main

    for field in ([], ["--field", "CAL"]):
        path = make_synthetic_ms(
            str(tmp_path / ("f%d.ms" % len(field))), nrow=64, nchan=16,
            field_ids=[0, 1] * 32, field_names=("CAL", "PCAL"),
        )
        with table(path, readonly=False, ack=False) as t:
            flag = t.getcol("FLAG")
            flag[:, 7, :] = True          # one channel: extend grows it
            flag[20, :, :] = True         # one row
            t.putcol("FLAG", flag)
            before = flag.sum()
        result = CliRunner().invoke(
            main,
            ["--ms", path, "--row-chunk", "10", "--apply", "--clobber", *field,
             "--flag", "extend [growtime=40, growfreq=40, growaround=true,"
             " flagneartime=true, flagnearfreq=true]"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        with table(path, ack=False) as t:
            assert t.getcol("FLAG").sum() > before


def test_extend_records_the_change_and_reports(capsys):
    flags = np.zeros((3, 5, 2), dtype=bool)
    flags[1, 2, 0] = True
    ms = _make_ms(flags)

    ms.flag_extend(ExtendParams(flagneartime=True))

    assert ms.changed.get("FLAG") is True
    out = capsys.readouterr().out
    assert "extend: +" in out


# --- The --flag grammar ----------------------------------------------------


def test_extend_parses_with_and_without_parameters():
    assert parse(["extend"])[0].args == ()
    op = parse(["extend [growtime=90, flagneartime=true]"])[0]
    assert op.args == ("growtime=90", "flagneartime=true")


def test_extend_rejects_unknown_parameters():
    with pytest.raises(FlagOrderError, match="unknown extend parameter"):
        parse(["extend nope=1"])


def test_extend_rejects_percentages_outside_range():
    with pytest.raises(FlagOrderError, match="percentage"):
        parse(["extend growtime=200"])
