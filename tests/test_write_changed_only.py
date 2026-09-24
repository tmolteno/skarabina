# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""``--write-changed-only``: a flagging run should not rewrite the whole MS.

A flagging run changes the flags and nothing else.  On a 124 GB measurement set,
copying every column reads and writes ~104 GB to change ~6 GB of flags, which
dominates the runtime on a network mount.  This mode shares the unchanged
columns' data blocks with the input and writes only what changed.

The sharing tests need a *real* table layout, which ``ms_fixture`` does not
provide: ``default_ms`` puts every column in one ``IncrementalStMan`` file, so
nothing could be shared, whereas a real MS writes each large column with its own
tiled storage manager -- exactly what makes this mode work.  ``real_layout_ms``
below builds one, giving each bulk column a ``TiledShapeStMan`` of its own.

It is built rather than copied from a measurement set on the author's disk.  An
earlier version read a fixture from a sibling checkout, which meant that on any
machine without that checkout every sharing test silently *skipped* instead of
failing -- the tests for the feature disappeared exactly when they could not be
checked.
"""
import errno
import os

import numpy as np
import pytest
from casacore.tables import makearrcoldesc, maketabdesc, table

from ms_fixture import make_synthetic_ms

from skarabina.dask_ms import DaskMS

#: Bulk columns re-laid into one tiled store each, as a real MS has them.
BULK_COLUMNS = (
    ("DATA", "TiledData"),
    ("FLAG", "TiledFlag"),
    ("WEIGHT_SPECTRUM", "TiledWtSpec"),
)


def real_layout_ms(path, nrow=24, nchan=4, ncorr=2):
    """A small MS in the layout a real one uses: one tile store per bulk column.

    ``ms_fixture`` builds a valid MS, but with every column in a single
    ``IncrementalStMan`` store, so there would be nothing to share.  Each bulk
    column is therefore moved into its own ``TiledShapeStMan`` here, which is
    what gives it a separate ``table.fN_TSM1`` file.

    Built rather than copied from a measurement set on the author's disk: an
    earlier version read a fixture from a sibling checkout, so on any machine
    without that checkout every sharing test silently *skipped* rather than
    failed, and the tests for this feature vanished exactly where they could not
    be checked.
    """
    path = str(path)
    make_synthetic_ms(path, nchan=nchan, nrow=nrow, ncorr=ncorr)
    t = table(path, readonly=False)
    for column, group in BULK_COLUMNS:
        cell = t.getcell(column, 0)
        values = t.getcol(column)
        if column in set(t.colnames()):
            t.removecols(column)
        t.addcols(
            maketabdesc(
                makearrcoldesc(column, [], ndim=cell.ndim, shape=list(cell.shape),
                               valuetype=_CASACORE_TYPE[cell.dtype.kind])
            ),
            {"TYPE": "TiledShapeStMan", "NAME": group,
             "SPEC": {"DEFAULTTILESHAPE": np.array(cell.shape, dtype=np.int32)}},
        )
        t.putcol(column, values)
    t.close()
    return path


#: numpy kind -> the name casacore wants in ``valuetype``.
_CASACORE_TYPE = {"c": "complex", "b": "bool", "f": "double", "i": "int"}


@pytest.fixture
def ms(tmp_path):
    """A real-layout MS holding deterministic data."""
    return real_layout_ms(str(tmp_path / "in.ms"))


def _nrow(path):
    t = table(path, ack=False, readonly=True)
    try:
        return t.nrows()
    finally:
        t.close()


def _read(path, col):
    t = table(path, ack=False, readonly=True)
    try:
        return np.asarray(t.getcol(col))
    finally:
        t.close()


def _readable(path, col):
    """The column's data, or None if casacore cannot read it back.

    ``FLAG_CATEGORY`` and the other hypercolumns exist in the schema but hold no
    arrays, so reading one raises.  Both outputs must be read the same way, so
    the comparison below skips what neither can produce rather than treating the
    failure as a difference.
    """
    try:
        return _read(path, col)
    except Exception:
        return None


def _cols(path):
    t = table(path, ack=False, readonly=True)
    try:
        return set(t.colnames())
    finally:
        t.close()


def _inodes(ms, ds):
    """``{file: inode}`` for every data block, via the mode's own attribution."""
    t = table(ms, ack=False, readonly=True)
    dminfo = t.getdminfo()
    t.close()
    out = {}
    for _base, (members, column) in ds._column_file_groups(dminfo).items():
        for member in members:
            full = os.path.join(ms, member)
            if os.path.exists(full):
                out[member] = (os.stat(full).st_ino, column)
    return out


def _flag_blocks(ms, ds):
    return {f for f, (_ino, col) in _inodes(ms, ds).items() if col == "FLAG"}


def _shareable_blocks(ms, ds):
    """Blocks attributed to a column that is not FLAG."""
    return {f for f, (_ino, col) in _inodes(ms, ds).items()
            if col is not None and col != "FLAG"}


def test_the_fixture_has_sharable_columns(ms):
    """Guard the guard: if the template ever loses its tiled columns, the
    sharing tests below would pass vacuously."""
    ds = DaskMS(ms)
    assert _shareable_blocks(ms, ds), (
        "expected at least one non-FLAG column in its own storage manager"
    )
    assert _flag_blocks(ms, ds), "expected FLAG in its own storage manager"


def test_output_matches_a_full_write(ms, tmp_path):
    """The changed-only output must be at least equivalent to a full write."""
    ds = DaskMS(ms)
    ds.flag_data({"CLIP": (0.0, 100.0)})

    full = str(tmp_path / "full.ms")
    DaskMS(ms).write_new_ms(full, clobber=True)

    changed = str(tmp_path / "changed.ms")
    DaskMS(ms).write_new_ms(changed, clobber=True, changed_only=True)

    full_cols = _cols(full)
    changed_cols = _cols(changed)
    # The changed-only output keeps the input's full column *definition* (some
    # columns exist in the schema but hold no rows), so it is a superset of what
    # a full write produces.  Every column a full write has must match.
    assert full_cols <= changed_cols, full_cols - changed_cols
    compared = 0
    for col in sorted(full_cols):
        expected = _readable(full, col)
        if expected is None:
            continue
        assert np.array_equal(_readable(changed, col), expected), col
        compared += 1
    assert compared > 5, (
        f"only {compared} column(s) were comparable; the test is not checking"
        " much"
    )


def test_unchanged_columns_are_shared_with_the_input(ms, tmp_path):
    """Sharing is the point: the bulk columns must not be duplicated."""
    out = str(tmp_path / "out.ms")
    ds = DaskMS(ms)
    ds.flag_data({"NAN": True})
    ds.write_new_ms(out, clobber=True, changed_only=True)

    shared = 0
    for member in _shareable_blocks(ms, ds):
        target = os.path.join(out, member)
        if os.path.exists(target):
            if os.stat(os.path.join(ms, member)).st_ino == os.stat(target).st_ino:
                shared += 1
    assert shared, "no unchanged column block was shared with the input"


def test_the_changed_column_gets_its_own_blocks(ms, tmp_path):
    """FLAG was rewritten, so it must not point at the input's blocks."""
    out = str(tmp_path / "out.ms")
    ds = DaskMS(ms)
    ds.flag_data({"NAN": True})
    ds.write_new_ms(out, clobber=True, changed_only=True)

    for member in _flag_blocks(ms, ds):
        target = os.path.join(out, member)
        if os.path.exists(target):
            assert os.stat(os.path.join(ms, member)).st_ino != os.stat(target).st_ino, (
                f"{member} holds FLAG and must have been rewritten"
            )


def test_input_is_not_altered_by_the_output_write(ms, tmp_path):
    """The decisive safety property: the input keeps its own flags."""
    before = _read(ms, "FLAG")
    out = str(tmp_path / "out.ms")
    ds = DaskMS(ms)
    ds.flag_data({"NAN": True})
    ds.write_new_ms(out, clobber=True, changed_only=True)
    assert np.array_equal(_read(ms, "FLAG"), before), "input flags were touched"


def test_shared_blocks_are_read_only_so_the_input_cannot_be_edited(ms, tmp_path):
    """A write to a shared column must fail rather than alter the input.

    The protection is the file mode of the shared blocks, and a hard link has
    one inode, so *both* paths lose write access to them.  That is what makes
    the sharing safe: without it, writing the output's DATA would silently
    rewrite the input's DATA through the shared block.

    The refusal can surface as early as opening a read/write table, so the whole
    attempt -- open, read, write -- is what must fail.
    """
    before = _read(ms, "DATA")
    out = str(tmp_path / "out.ms")
    ds = DaskMS(ms)
    ds.flag_data({"NAN": True})
    ds.write_new_ms(out, clobber=True, changed_only=True)

    # Sharing must not cost readability: both paths still return the data.
    assert np.array_equal(_read(out, "DATA"), before)
    assert _read(out, "FLAG").shape == before.shape

    def try_to_overwrite(path):
        t = table(path, readonly=False)
        try:
            t.putcol("DATA", np.zeros_like(before))
        finally:
            t.close()

    for path in (out, ms):
        with pytest.raises(Exception):
            try_to_overwrite(path)
    assert np.array_equal(_read(ms, "DATA"), before), "input DATA was corrupted"


def test_the_rewritten_column_is_still_writable(ms, tmp_path):
    """Refusing every write would make the output useless: FLAG must accept one."""
    out = str(tmp_path / "out.ms")
    ds = DaskMS(ms)
    ds.flag_data({"NAN": True})
    ds.write_new_ms(out, clobber=True, changed_only=True)

    t = table(out, readonly=False)
    try:
        flags = t.getcol("FLAG")
        flags[...] = True
        t.putcol("FLAG", flags)
    finally:
        t.close()
    assert _read(out, "FLAG").all()


def test_a_block_left_read_only_by_an_earlier_run_can_still_be_rewritten(ms, tmp_path):
    """A run must survive an input that an earlier run made read-only (#3).

    Sharing makes the shared blocks read-only, and a hard link is one inode, so
    a run that changes nothing leaves the *input's* blocks read-only too -- by
    design, see ``test_shared_blocks_are_read_only_...`` above.  The next run
    that has to change one of those columns copies the block into its output,
    and ``shutil.copy2`` preserves the mode: the copy arrived read-only, and the
    write that followed died with ``storage error: Permission denied``.  The
    input is only usable once if that happens, which is what this pins down.
    """
    first = str(tmp_path / "first.ms")
    DaskMS(ms).write_new_ms(first, clobber=True, changed_only=True)

    blocks = _flag_blocks(ms, DaskMS(ms))
    assert blocks, "expected FLAG in its own storage manager"
    read_only = [b for b in blocks
                 if not os.stat(os.path.join(ms, b)).st_mode & 0o200]
    assert read_only, (
        "the first run changed no column, so it should have left FLAG's blocks"
        " read-only"
    )

    second = str(tmp_path / "second.ms")
    ds = DaskMS(ms)
    ds.flag_data({"NAN": True})
    ds.write_new_ms(second, clobber=True, changed_only=True)

    # The rewritten column is the output's own block, and it is usable.
    assert _read(second, "FLAG").shape == _read(ms, "FLAG").shape
    t = table(second, readonly=False)
    try:
        flags = t.getcol("FLAG")
        flags[...] = True
        t.putcol("FLAG", flags)
    finally:
        t.close()


def test_a_copied_shared_block_is_left_writable(ms, tmp_path, monkeypatch):
    """Across filesystems the block is copied, not shared, so nothing protects it.

    The read-only mode exists to keep a write to the *output* from reaching the
    *input* through a shared inode.  A copy has no such link: the output owns it
    outright, so carrying the input's read-only mode onto it only invents a
    failure -- and the input here is read-only already, because the first run
    shared its blocks.
    """
    first = str(tmp_path / "first.ms")
    DaskMS(ms).write_new_ms(first, clobber=True, changed_only=True)

    ds = DaskMS(ms)
    blocks = _shareable_blocks(ms, ds) | _flag_blocks(ms, ds)
    assert blocks, "expected columns in storage managers of their own"
    assert all(not os.stat(os.path.join(ms, b)).st_mode & 0o200 for b in blocks)

    def no_link(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", no_link)
    out = str(tmp_path / "out.ms")
    DaskMS(ms).write_new_ms(out, clobber=True, changed_only=True)

    for member in blocks:
        target = os.path.join(out, member)
        assert os.path.exists(target), f"{member} was not copied into the output"
        assert (os.stat(target).st_ino
                != os.stat(os.path.join(ms, member)).st_ino), (
            f"{member} was linked, so this test did not exercise the copy path"
        )
        assert os.stat(target).st_mode & 0o200, (
            f"{member} was copied into the output read-only"
        )
    # Nothing was shared, so the input keeps the modes the first run gave it.
    for member in blocks:
        assert not os.stat(os.path.join(ms, member)).st_mode & 0o200, (
            f"{member} was made writable in the input, but nothing was shared"
        )


def test_frequency_averaging_falls_back_to_a_full_write(tmp_path, capsys):
    """Averaging changes the channel count, so sharing is impossible."""
    ms = make_synthetic_ms(tmp_path / "avg_in.ms", nchan=4, nrow=6)
    out = str(tmp_path / "avg.ms")
    ds = DaskMS(ms)
    ds.frequency_average(2)
    ds.write_new_ms(out, clobber=True, changed_only=True)
    assert "not applicable" in capsys.readouterr().out
    assert _read(out, "FLAG").shape[1] == 2
    # the fallback is a real write, not a silent no-op
    assert os.path.isdir(os.path.join(out, "SPECTRAL_WINDOW"))
