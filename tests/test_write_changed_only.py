# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""``--write-changed-only``: a flagging run should not rewrite the whole MS.

A flagging run changes the flags and nothing else.  On a 124 GB measurement set,
copying every column reads and writes ~104 GB to change ~6 GB of flags, which
dominates the runtime on a network mount.  This mode shares the unchanged
columns' data blocks with the input and writes only what changed.

The sharing tests are built from a real measurement set rather than from
``ms_fixture``: casacore's ``default_ms`` puts every column in one
``IncrementalStMan`` file, so nothing could be shared there, whereas a real MS
writes each large column with its own tiled storage manager -- which is exactly
what makes this mode work.  The fallback tests need no particular layout, so
they use the synthetic fixture, which is the only one that can hold more than
one channel: a casacore array column cannot be resized once it holds rows, so
the template's channel count is fixed.
"""
import os
import shutil

import numpy as np
import pytest
from casacore.tables import table

from ms_fixture import make_synthetic_ms

from skarabina.dask_ms import DaskMS

#: A single-row MS on disk used only for its table layout.
TEMPLATE = "/home/tim/git/spotless/test_data/tart.ms"

pytestmark = pytest.mark.skipif(
    not os.path.isdir(TEMPLATE),
    reason=f"no template measurement set at {TEMPLATE}",
)


@pytest.fixture
def ms(tmp_path):
    """A real-layout MS holding deterministic data.

    The template's row and channel shape is kept exactly as it is: the point of
    the fixture is the table *layout*, and resizing a column would rewrite the
    storage it is here to preserve.
    """
    path = str(tmp_path / "in.ms")
    shutil.copytree(TEMPLATE, path)
    t = table(path, readonly=False)
    shape = t.getcell("DATA", 0).shape
    nrow = t.nrows()
    rng = np.random.default_rng(0)
    data = (rng.normal(size=(nrow,) + shape)
            + 1j * rng.normal(size=(nrow,) + shape)).astype(complex)
    t.putcol("DATA", data)
    t.putcol("FLAG", np.zeros((nrow,) + shape, bool))
    if "WEIGHT_SPECTRUM" in t.colnames():
        t.putcol("WEIGHT_SPECTRUM", rng.random((nrow,) + shape))
    t.close()
    return path


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
    for col in sorted(full_cols):
        assert np.array_equal(_read(changed, col), _read(full, col)), col


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
