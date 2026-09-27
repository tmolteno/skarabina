# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The ``--optimize`` path: what it removes, what it reports, how the CLI
reaches it.

``DaskMS.optimize()`` removes rows (``FLAG_ROW`` set *or* every visibility in
``FLAG`` set) and channels whose visibilities are all flagged, prints a block
of counts, and -- when a dropped channel splits the band -- warns that the
hole is not recorded in SPECTRAL_WINDOW.  ``main()`` must refuse an optimize
with nowhere to write (only ``--msout``: an in-place ``--apply`` cannot
remove rows or channels), and must let the ``--flag`` verbs run *before*
optimize so it decides on the flags they just set.  ``update_ms`` holds the
backstop for anything ``main()`` does not guard, through the
``shape_reduction`` marker the reducing passes set.

The method-level tests build a synthetic DaskMS with ``__new__`` and never
touch casacore; the CLI tests run a small real MS through click.  The
band-hole warnings themselves are pinned in ``test_optimize_band.py``; the
channel bookkeeping it feeds to ``write_new_ms`` in
``test_spw_bookkeeping.py``.
"""
import re

import numpy as np
import pytest
import xarray as xr

from skarabina import dask_ms  # noqa: F401  (daskms before casacore.tables)
from skarabina.analyze import band_info
from skarabina.dask_ms import DaskMS

from casacore.tables import table  # noqa: E402
from click.testing import CliRunner
from ms_fixture import make_synthetic_ms  # noqa: E402
from skarabina.main import main  # noqa: E402

NROW = 4
NCHAN = 3
WIDTH_HZ = 1.0e7
BASE_HZ = 1.0e9


def _make_ms(nrow=NROW, nchan=NCHAN, flags=None, flag_row=None, with_widths=True):
    """A synthetic MS as ``__new__`` builds it: no casacore, fully in memory.

    Rows come in dask chunks of two, so the row and channel masks ``optimize``
    computes run through real chunked reductions rather than a single block.
    """
    data = np.ones((nrow, nchan, 1), dtype=complex)
    flag = np.zeros((nrow, nchan, 1), dtype=bool)
    if flags is not None:
        flag[:] = flags
    row_flag = np.zeros(nrow, dtype=bool)
    if flag_row is not None:
        row_flag[:] = flag_row

    ds = xr.Dataset(
        {
            "DATA": (("row", "chan", "corr"), data),
            "FLAG": (("row", "chan", "corr"), flag),
            "WEIGHT_SPECTRUM": (("row", "chan", "corr"), np.ones(data.shape)),
            "UVW": (("row", "uvw"), np.zeros((nrow, 3))),
            "TIME": (("row",), np.arange(nrow, dtype=float) * 10.0),
            "ANTENNA1": (("row",), np.zeros(nrow, dtype=np.int32)),
            "ANTENNA2": (("row",), np.ones(nrow, dtype=np.int32)),
            "FLAG_ROW": (("row",), row_flag),
            # A column with no row dimension: optimize must leave it alone.
            "STATIC": (("entry",), np.zeros(1, dtype=np.int32)),
        }
    ).chunk({"row": 2, "chan": nchan, "corr": 1})

    ms = DaskMS.__new__(DaskMS)
    ms.ds = ds
    ms.changed = {}
    ms.name = "<synthetic>"
    ms.sub_table_names = []
    ms.nspw = 1
    ms.chan_freq_hz = BASE_HZ + np.arange(nchan, dtype=float) * WIDTH_HZ
    ms.chan_axis_hz = (
        {col: np.full(nchan, WIDTH_HZ, dtype=float)
         for col in ("CHAN_WIDTH", "EFFECTIVE_BW", "RESOLUTION")}
        if with_widths else {}
    )
    ms.spw_chan_count = nchan
    ms._refresh_cached_columns()
    return ms


def _stats(capsys):
    return capsys.readouterr().out


def _run(*args):
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result


def _shape(path):
    t = table(path, ack=False)
    try:
        return t.nrows(), t.getcol("DATA").shape[1]
    finally:
        t.close()


@pytest.fixture
def real_ms(tmp_path):
    return make_synthetic_ms(str(tmp_path / "in.ms"), nchan=8, nrow=8)


# --- Row removal -----------------------------------------------------------


def test_flag_row_removes_a_row_whose_visibilities_are_partly_live(capsys):
    """FLAG_ROW alone condemns a row, however few visibilities it flags."""
    flags = np.zeros((NROW, NCHAN, 1), dtype=bool)
    flags[0, 0, 0] = True                      # one bad visibility
    ms = _make_ms(flags=flags, flag_row=[True, False, False, False])

    ms.optimize()

    assert ms.ds.FLAG.shape == (NROW - 1, NCHAN, 1)
    assert ms.ds.TIME.values.tolist() == [10.0, 20.0, 30.0]
    out = _stats(capsys)
    assert re.search(r"FLAG_ROW flagged:\s+1", out)
    assert re.search(r"All-data-flagged:\s+0", out)
    assert re.search(r"Remaining:\s+3", out)


def test_a_fully_flagged_row_is_removed_without_flag_row(capsys):
    """Every visibility flagged is enough; FLAG_ROW may still say False."""
    flags = np.zeros((NROW, NCHAN, 1), dtype=bool)
    flags[1] = True
    ms = _make_ms(flags=flags, flag_row=[False] * NROW)

    ms.optimize()

    assert ms.ds.FLAG.shape == (NROW - 1, NCHAN, 1)
    assert ms.ds.TIME.values.tolist() == [0.0, 20.0, 30.0]
    out = _stats(capsys)
    assert re.search(r"FLAG_ROW flagged:\s+0", out)
    assert re.search(r"All-data-flagged:\s+1", out)
    assert re.search(r"Extra rows caught by all\(FLAG\) check: 1", out)


def test_the_statistics_separate_flag_row_all_flagged_and_overlap(capsys):
    """The four counts in the report must add up over an overlapping case.

    Row 0 is condemned only by FLAG_ROW, row 1 by both conditions (the
    overlap), row 2 only by all(FLAG) -- the "extra rows" line -- and row 3
    survives.  Naive sums would double-count row 1.
    """
    flags = np.zeros((NROW, NCHAN, 1), dtype=bool)
    flags[0, 0, 0] = True                       # FLAG_ROW only
    flags[1] = True                            # FLAG_ROW *and* all(FLAG)
    flags[2] = True                             # all(FLAG) only: the extra
    ms = _make_ms(flags=flags, flag_row=[True, True, False, False])

    ms.optimize()

    assert ms.ds.TIME.values.tolist() == [30.0]
    out = _stats(capsys)
    assert re.search(r"Total rows:\s+4", out)
    assert re.search(r"FLAG_ROW flagged:\s+2", out)
    assert re.search(r"All-data-flagged:\s+2", out)
    assert re.search(r"Combined to remove:\s+3", out)
    assert re.search(r"Remaining:\s+1", out)
    assert re.search(r"Extra rows caught by all\(FLAG\) check: 1", out)
    assert "Fully-flagged channels: 0 / 3" in out


def test_a_clean_input_keeps_every_row_and_channel(capsys):
    """With nothing flagged, both masks fall through to the no-op path."""
    ms = _make_ms()

    ms.optimize()

    assert ms.ds.FLAG.shape == (NROW, NCHAN, 1)
    assert np.allclose(ms.chan_freq_hz, BASE_HZ + np.arange(NCHAN) * WIDTH_HZ)
    out = _stats(capsys)
    assert re.search(r"Combined to remove:\s+0", out)
    assert "Fully-flagged channels: 0 / 3" in out
    assert "WARNING" not in out


# --- Row and channel removal together --------------------------------------


def test_rows_and_channels_come_out_in_the_same_pass():
    """Both masks in one compute: a dead row and a dead channel together.

    The channel mask is only computed when a channel is actually dead
    (``dask_ms.py`` folds it into the row-mask compute), so a run where both
    kinds of removal fire exercises the branch that builds the two indexers.
    """
    nrow, nchan = 5, 4
    flags = np.zeros((nrow, nchan, 1), dtype=bool)
    flags[0] = True                # a dead row (FLAG_ROW still False)
    flags[:, 2] = True              # a dead interior channel
    ms = _make_ms(nrow=nrow, nchan=nchan, flags=flags)

    ms.optimize()

    assert ms.ds.FLAG.shape == (nrow - 1, nchan - 1, 1)
    assert ms.ds.TIME.values.tolist() == [10.0, 20.0, 30.0, 40.0]
    # The cached column snapshots follow the selection...
    assert ms.data.shape == ms.ds.DATA.shape
    # ...and so does the SPECTRAL_WINDOW bookkeeping (channel 2 dropped).
    assert np.allclose(ms.chan_freq_hz, [1.00e9, 1.01e9, 1.03e9])
    for col, values in ms.chan_axis_hz.items():
        assert len(values) == nchan - 1, col
    for col in ("DATA", "FLAG", "FLAG_ROW", "UVW", "TIME"):
        assert ms.changed.get(col) is True, col


def test_optimize_marks_every_row_column_as_changed():
    """The write decides what to save from ``changed``: row selection
    invalidates every column carrying a row, and only those."""
    flags = np.zeros((NROW, NCHAN, 1), dtype=bool)
    flags[3] = True                             # last row goes
    ms = _make_ms(flags=flags)

    ms.optimize()

    for col in ("DATA", "FLAG", "FLAG_ROW", "UVW", "TIME", "ANTENNA1"):
        assert ms.changed.get(col) is True, col
    assert "STATIC" not in ms.changed, "a column without a row dimension is untouched"


# --- The band-hole warning's guards ----------------------------------------


def test_the_hole_warning_is_skipped_without_channel_widths(capsys):
    """The hole's size comes from CHAN_WIDTH; without widths it cannot be
    measured, and the warning must stay silent rather than guess."""
    flags = np.zeros((NROW, NCHAN, 1), dtype=bool)
    flags[:, 1, :] = True                       # interior channel, dead everywhere
    ms = _make_ms(flags=flags, with_widths=False)

    ms.optimize()

    out = _stats(capsys)
    assert ms.ds.FLAG.shape == (NROW, NCHAN - 1, 1)
    assert np.allclose(ms.chan_freq_hz, [1.00e9, 1.02e9])
    assert "split the band" not in out
    assert "not recorded in SPECTRAL_WINDOW" not in out


def test_the_hole_warning_is_skipped_when_one_channel_remains(capsys):
    """Fewer than two survivors define no gap at all."""
    flags = np.zeros((NROW, 2, 1), dtype=bool)
    flags[:, 0, :] = True
    ms = _make_ms(nchan=2, flags=flags)

    ms.optimize()

    out = _stats(capsys)
    assert ms.ds.FLAG.shape == (NROW, 1, 1)
    assert len(ms.chan_freq_hz) == 1
    assert "split the band" not in out
    assert "not recorded in SPECTRAL_WINDOW" not in out


# --- Through the CLI -------------------------------------------------------


def test_optimize_without_anywhere_to_write_is_refused(real_ms):
    """The removal is in-memory, so main() must demand --msout rather than
    let the result evaporate on a successful-looking run."""
    with pytest.raises(RuntimeError, match="optimize has no effect without --msout"):
        _run("--ms", real_ms, "--optimize")


# --- --apply cannot represent a reduction ----------------------------------


def test_optimize_with_apply_is_refused_and_leaves_the_input_alone(real_ms):
    """The row case: dask-ms writes each row back where it came from, so
    ``--optimize --apply`` used to exit 0 having changed nothing -- the
    removed rows stayed and "Optimize complete" was a fiction."""
    t = table(real_ms, readonly=False, ack=False)
    flags = t.getcol("FLAG")
    flags[0] = True                             # a fully-flagged row
    t.putcol("FLAG", flags)
    t.close()
    before = table(real_ms, ack=False)
    try:
        original = before.getcol("FLAG").copy()
    finally:
        before.close()

    with pytest.raises(RuntimeError, match="optimize has no effect without --msout"):
        _run("--ms", real_ms, "--optimize", "--apply", "--clobber")

    after = table(real_ms, ack=False)
    try:
        assert after.nrows() == 8
        assert np.array_equal(after.getcol("FLAG"), original)
    finally:
        after.close()


def test_optimize_with_apply_is_refused_when_a_channel_is_dead(real_ms):
    """The channel case: the old run rewrote the cells at the reduced width
    while SPECTRAL_WINDOW still described the input's -- an inconsistent
    MS, also with exit code 0."""
    t = table(real_ms, readonly=False, ack=False)
    flags = t.getcol("FLAG")
    flags[:, 3, :] = True                       # dead interior channel
    t.putcol("FLAG", flags)
    t.close()

    with pytest.raises(RuntimeError, match="optimize has no effect without --msout"):
        _run("--ms", real_ms, "--optimize", "--apply", "--clobber")

    assert _shape(real_ms) == (8, 8), "the input must be untouched"
    assert band_info(real_ms).n_chan == 8


def test_update_ms_refuses_a_dataset_optimize_reduced(tmp_path):
    """The write-side backstop: update_ms itself refuses, so an API caller
    that skips main()'s guard gets the same protection as the CLI."""
    path = make_synthetic_ms(str(tmp_path / "api.ms"), nchan=4, nrow=6)
    t = table(path, readonly=False, ack=False)
    flags = t.getcol("FLAG")
    flags[0] = True
    t.putcol("FLAG", flags)
    t.close()

    ms = DaskMS(path, row_chunk=1000)
    ms.optimize()
    assert ms.shape_reduction is not None

    with pytest.raises(RuntimeError, match="cannot write the reduced dataset"):
        ms.update_ms(path, clobber=True)
    assert _shape(path) == (6, 4), "the refusal must come before any write"


@pytest.mark.parametrize("extra", [
    ["--time-average-factor", "2"],
    ["--frequency-average-factor", "2"],
])
def test_apply_refuses_averaging_that_changes_the_shape(real_ms, extra):
    """Averaging halves the rows or the channels, so it cannot be applied
    in place either: time-averaging used to write each averaged row into
    the group's first slot and leave the rest stale, frequency-averaging
    to narrow the cells against a full-width SPECTRAL_WINDOW."""
    with pytest.raises(RuntimeError, match="cannot write the reduced dataset"):
        _run("--ms", real_ms, *extra, "--apply", "--clobber")
    assert _shape(real_ms) == (8, 8), "the input must be untouched"


def test_scan_selection_still_applies_flags_in_place(tmp_path):
    """Row *selection* is the guard's boundary, not its target: writing the
    selected rows' columns back at their original positions is exactly what
    --scan + --apply should do, and must keep working."""
    path = make_synthetic_ms(
        str(tmp_path / "scan.ms"), nrow=8, nchan=4,
        scan_numbers=[1, 2, 1, 2, 1, 2, 1, 2], auto_rows=4,
    )

    _run("--ms", path, "--scan", "2", "--flag", "autos", "--apply", "--clobber")

    t = table(path, ack=False)
    try:
        assert t.nrows() == 8, "an in-place flag update removes no rows"
        # scan 2 holds rows 1, 3, 5, 7; only rows 1 and 3 are autos, and
        # only those -- not the scan-1 autos -- may come back flagged.
        assert np.asarray(t.getcol("FLAG_ROW")).tolist() == [
            False, True, False, True, False, False, False, False,
        ]
    finally:
        t.close()


def test_flags_from_the_flag_list_reach_the_output(tmp_path):
    """End to end: flag verbs set the flags, optimize removes on them.

    Pins the CLI ordering -- the ``--flag`` list runs first, and the rows it
    condemns (here via ``nan``, with FLAG_ROW still False) are what optimize
    drops from the written MS.  Losing the ``ms.optimize()`` call from
    ``main()``, as 1.0.8 did, or running it on the pristine input flags, both
    fail this.
    """
    path = make_synthetic_ms(str(tmp_path / "nan.ms"), nchan=8, nrow=8)
    t = table(path, readonly=False, ack=False)
    data = t.getcol("DATA")
    data[2] = np.nan                 # a wholly-NaN row: all(FLAG) must catch it
    data[5, 0, 0] = np.nan           # one NaN: this row must survive
    t.putcol("DATA", data)
    t.close()

    out = str(tmp_path / "out.ms")
    _run("--ms", path, "--flag", "nan", "--optimize", "--msout", out)

    assert _shape(out) == (7, 8), "the NaN row should be gone, live rows kept"
    t = table(out, ack=False)
    try:
        flags = np.asarray(t.getcol("FLAG"))
        assert flags.sum() == 1, "the partially-NaN visibility keeps its flag"
        assert not flags.all(axis=(1, 2)).any()
    finally:
        t.close()


def test_keep_fully_flagged_channels_is_honoured_by_the_cli(real_ms, tmp_path):
    """The option is CLI wiring: it must reach ``optimize`` and survive the
    SPECTRAL_WINDOW rewrite of the written MS."""
    t = table(real_ms, readonly=False, ack=False)
    flags = t.getcol("FLAG")
    flags[:, 3, :] = True               # interior channel, dead in every row
    t.putcol("FLAG", flags)
    t.close()

    keep = str(tmp_path / "keep.ms")
    _run("--ms", real_ms, "--optimize", "--keep-fully-flagged-channels", "--msout", keep)
    assert _shape(keep) == (8, 8)
    assert band_info(keep).n_chan == 8

    drop = str(tmp_path / "drop.ms")
    _run("--ms", real_ms, "--optimize", "--msout", drop)
    assert _shape(drop) == (8, 7)
    assert band_info(drop).n_chan == 7
