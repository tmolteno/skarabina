# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""CASA flagmanager-compatible flag versions.

The on-disk layout implemented by ``skarabina.flag_versions`` was taken from
versions written by CASA itself, so these tests pin two things: that saving and
restoring round-trips flags faithfully, and that the bytes on disk keep the
shape CASA's ``flagmanager`` expects.  The format oracle is a real
``*.flagversions`` written by CASA, when one is available.
"""
import os
from pathlib import Path

import dask.array as da
import numpy as np
import pytest
from casacore.tables import table

from skarabina import flag_versions
from skarabina.dask_ms import DaskMS
from ms_fixture import make_synthetic_ms

# A real flagversions directory produced by CASA's flagmanager, if present.
CASA_FLAGVERSIONS = Path.home() / "astro/mt0_e45_casa_rflag.flagversions"

NROW, NCHAN, NCORR = 24, 3, 2
FULL = np.ones((NROW, NCHAN, NCORR), bool)


def flag_pattern():
    """A known, mixed flag pattern."""
    flags = np.zeros((NROW, NCHAN, NCORR), bool)
    flags[0, 0, 0] = True
    flags[5:8, 1, :] = True
    flags[3, :, :] = True
    return flags


@pytest.fixture
def ms(tmp_path):
    """A small real MS on disk holding ``flag_pattern()``.

    The flags go through casacore, so the MS on disk really holds them and a
    restore can be checked against ``--apply``.
    """
    path = str(tmp_path / "obs.ms")
    make_synthetic_ms(path, nchan=NCHAN, nrow=NROW, ncorr=NCORR)
    t = table(path, readonly=False)
    t.putcol("FLAG", flag_pattern())
    t.close()
    return path


def write_flags(ms, flags):
    """Put flags into the MS on disk (dask-ms must not hold it open)."""
    t = table(ms, readonly=False)
    t.putcol("FLAG", flags)
    t.close()


@pytest.fixture
def saved(ms):
    """The MS with a version called 'before' already saved."""
    DaskMS(ms).save_flag_version("before", comment="baseline flags")
    return ms


# --- the on-disk format -----------------------------------------------------


def test_version_list_format_matches_casa(saved):
    """FLAG_VERSION_LIST is plain text: '<name> : <comment>' per line."""
    listfile = Path(saved + ".flagversions") / "FLAG_VERSION_LIST"
    assert listfile.is_file(), "FLAG_VERSION_LIST must be a plain file, not a table"
    assert listfile.read_text() == "before : baseline flags\n"


@pytest.mark.skipif(
    not CASA_FLAGVERSIONS.is_dir(), reason="no CASA-written flagversions available"
)
def test_parse_real_casa_version_list():
    """Our parser reads a file written by CASA's flagmanager."""
    text = (CASA_FLAGVERSIONS / "FLAG_VERSION_LIST").read_text()
    entries = flag_versions.parse_version_list(text)
    assert entries, "expected at least one version in the CASA file"
    for name, comment in entries:
        assert name and " " not in name, f"CASA names have no blanks: {name!r}"
        assert ":" not in name
    assert all(comment for _, comment in entries), "CASA stores a comment per version"


@pytest.mark.skipif(
    not (CASA_FLAGVERSIONS / "flags.flagdata_1").is_dir(),
    reason="no CASA-written version table available",
)
def test_our_table_schema_matches_casa(saved):
    """Compare our version table column-for-column with the one CASA wrote."""
    ours = table(flag_versions.version_path(saved, "before"), ack=False, readonly=True)
    casa = table(str(CASA_FLAGVERSIONS / "flags.flagdata_1"), ack=False, readonly=True)
    try:
        assert ours.colnames() == casa.colnames() == ["FLAG", "FLAG_ROW"]
        for col in ("FLAG", "FLAG_ROW"):
            od, cd = ours.getcoldesc(col), casa.getcoldesc(col)
            for key in ("valueType", "ndim", "_c_order", "maxlen"):
                if key in cd:
                    assert od.get(key) == cd.get(key), f"{col}.{key} differs from CASA"
        assert ours.getcol("FLAG_ROW").dtype == casa.getcol("FLAG_ROW").dtype
    finally:
        ours.close()
        casa.close()


def test_saved_table_holds_the_flags(saved):
    t = table(flag_versions.version_path(saved, "before"), ack=False, readonly=True)
    try:
        assert t.nrows() == NROW
        assert np.array_equal(t.getcol("FLAG"), flag_pattern()), "FLAG round-trips"
        assert not t.getcol("FLAG_ROW").any()
    finally:
        t.close()


# --- list parsing -----------------------------------------------------------


def test_version_list_roundtrip():
    entries = [("Original", "Original flags at import into CASA"),
               ("flagdata_1", "Flags autosave on 2018-04-23 20:47:14")]
    assert flag_versions.parse_version_list(
        flag_versions.format_version_list(entries)
    ) == entries


def test_version_list_tolerates_odd_lines():
    text = "\n  spaced  :  a comment with : colons \nnoname-line\n\n"
    entries = flag_versions.parse_version_list(text)
    assert entries[0] == ("spaced", "a comment with : colons")
    assert entries[1] == ("noname-line", "")


# --- save / restore ---------------------------------------------------------


def test_save_then_restore_recovers_the_flags(saved):
    ds = DaskMS(saved)
    # flag everything, then restore the saved version
    ds.ds["FLAG"].data = da.asarray(FULL.copy())
    ds.changed["FLAG"] = True
    assert np.asarray(ds.ds["FLAG"].data).all()

    ds.restore_flag_version("before")
    assert np.array_equal(np.asarray(ds.ds["FLAG"].data), flag_pattern())
    # the cached attribute must be refreshed, or later flagging reads stale flags
    assert np.array_equal(np.asarray(ds.flag.compute()), flag_pattern())


def test_restore_updates_the_on_disk_ms_when_applied(saved):
    """--apply must write the restored flags through to the MS."""
    ds = DaskMS(saved)
    ds.ds["FLAG"].data = da.asarray(FULL.copy())
    ds.changed["FLAG"] = True
    ds.restore_flag_version("before")
    ds.update_ms(saved, clobber=True)

    t = table(saved, ack=False, readonly=True)
    try:
        assert np.array_equal(t.getcol("FLAG"), flag_pattern())
    finally:
        t.close()


def test_second_save_moves_the_old_version_aside(saved):
    """CASA keeps the superseded version as '<name>.old.<timestamp>'."""
    ds = DaskMS(saved)
    ds.save_flag_version("before", comment="second save")
    names = [n for n, _ in ds.list_flag_versions()]
    assert "before" in names
    assert any(n.startswith("before.old.") for n in names), names
    for name in names:
        flag, _ = flag_versions.load_version(saved, name)
        assert flag.shape == (NROW, NCHAN, NCORR)


def test_two_versions_restore_independently(ms):
    """Two versions of the same MS restore independently of each other.

    Saved straight from disk with the module functions, which take no dask-ms
    lock; the DaskMS handle is opened once, at the end, to restore them.
    """
    flag_versions.save_version(ms, "before", *flag_versions.read_ms_flags(ms),
                               comment="baseline flags")
    write_flags(ms, FULL)
    flag_versions.save_version(ms, "all-flagged", *flag_versions.read_ms_flags(ms))

    ds = DaskMS(ms)
    ds.restore_flag_version("before")
    assert np.array_equal(np.asarray(ds.ds["FLAG"].data), flag_pattern())
    ds.restore_flag_version("all-flagged")
    assert np.asarray(ds.ds["FLAG"].data).all()


def test_restore_rejects_a_version_from_a_different_row_count(ms, tmp_path):
    """A version whose row count no longer matches must be an error, not a
    silently misaligned restore.

    The mismatch is built by taking a version from a larger MS of the same
    shape and dropping it into this MS's flagversions directory -- the
    practical way to end up with a stale version (dask-ms holds the MS locked,
    so the MS itself cannot be shrunk under an open handle).
    """
    donor = str(tmp_path / "donor.ms")
    make_synthetic_ms(donor, nchan=NCHAN, nrow=NROW * 2, ncorr=NCORR)
    DaskMS(donor).save_flag_version("stale")

    # publish that version under this MS's flagversions directory
    donor_dir = flag_versions.version_path(donor, "stale")
    target_dir = flag_versions.version_path(ms, "stale")
    os.replace(donor_dir, target_dir)
    flag_versions.write_version_list(
        ms, flag_versions.read_version_list(ms) + [("stale", "from another MS")]
    )

    ds = DaskMS(ms)
    with pytest.raises(RuntimeError, match="no longer matches this data"):
        ds.restore_flag_version("stale")


def test_missing_version_lists_what_is_available(saved):
    with pytest.raises(FileNotFoundError, match="available: before"):
        DaskMS(saved).restore_flag_version("nope")


def test_save_rejects_a_path_like_name(ms):
    ds = DaskMS(ms)
    with pytest.raises(ValueError):
        ds.save_flag_version("../escape")
    with pytest.raises(ValueError):
        ds.save_flag_version(" has-blanks ")


def test_flagversions_dir_sits_beside_the_ms(ms):
    assert flag_versions.flagversions_path(ms) == os.path.abspath(ms) + ".flagversions"
    assert os.path.isdir(ms + ".flagversions")
