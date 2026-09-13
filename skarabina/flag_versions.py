# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""CASA-compatible flag version backups.

``flagmanager`` in CASA keeps backups of the flags of a measurement set so a
bad flagging pass can be undone.  This module implements the same on-disk
layout, so versions written by skarabina can be listed and restored by CASA's
``flagmanager`` and vice versa:

    <msname>.flagversions/                 plain directory beside the MS
        FLAG_VERSION_LIST                  one "<name> : <comment>" line each
        flags.<versionname>/               a casacore table per version
            FLAG                           same shape as the MS FLAG column
            FLAG_ROW                       the table's row flag

The layout was taken from versions written by CASA itself (verified against a
real ``mt0_e45_casa_rflag.flagversions``): ``FLAG_VERSION_LIST`` is a plain
text file, *not* a casacore table, and each version is a standalone table with
exactly two columns.  ``FLAG_ROW`` is stored as the table's row flag, so it is
read back with ``getcol`` on a ``FLAG_ROW``-named column, exactly as CASA does.

The ``FLAG_CATEGORY`` column is not copied: CASA's own flagmanager only stores
FLAG and FLAG_ROW, and this module follows it.
"""
import datetime
import os
import shutil

import numpy as np
from casacore.tables import makecoldesc, maketabdesc, table

# Suffix of the directory holding all versions, matching CASA's flagmanager.
FLAGVERSIONS_SUFFIX = ".flagversions"

# Prefix of a per-version directory, e.g. "flags.Original".
FLAGVERSIONS_PREFIX = "flags."

# Rows read per chunk when copying flags.  Bounds peak memory: a chunk of
# FLAG is nrow_chunk x nchan x ncorr booleans.
CHUNK_ROWS = 20000

# Tile shape used for the FLAG column's TiledShapeStMan, mirroring CASA's.
_TILE_ROWS = 1024
_MAX_TILE_CHANNELS = 4096


def flagversions_path(ms_path):
    """Path of the ``<ms>.flagversions`` directory holding flag versions.

    The directory is created if it does not exist.
    """
    ms_path = os.path.abspath(str(ms_path).rstrip("/"))
    path = ms_path + FLAGVERSIONS_SUFFIX
    os.makedirs(path, exist_ok=True)
    return path


def version_path(ms_path, versionname):
    """Path of the table directory for one flag version."""
    return os.path.join(flagversions_path(ms_path), FLAGVERSIONS_PREFIX + versionname)


def parse_version_list(text):
    """Parse a ``FLAG_VERSION_LIST`` file into ``[(name, comment), ...]``.

    Each line is ``<name> : <comment>``.  Blank lines are ignored.  A line
    without a colon is read as a name with an empty comment, so a hand-edited
    file does not crash the reader.
    """
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        name, sep, comment = line.partition(":")
        entries.append((name.strip(), comment.strip() if sep else ""))
    return entries


def format_version_list(entries):
    """Render ``[(name, comment), ...]`` as ``FLAG_VERSION_LIST`` contents."""
    return "".join(f"{name} : {comment}\n" for name, comment in entries)


def read_version_list(ms_path):
    """Read the version list of an MS, in file order (oldest first)."""
    path = os.path.join(flagversions_path(ms_path), "FLAG_VERSION_LIST")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return parse_version_list(f.read())


def write_version_list(ms_path, entries):
    """Write the version list of an MS."""
    path = os.path.join(flagversions_path(ms_path), "FLAG_VERSION_LIST")
    with open(path, "w") as f:
        f.write(format_version_list(entries))


def _freeze(value):
    """Materialize a dask array (or pass a numpy array through)."""
    if hasattr(value, "compute"):
        return np.asarray(value.compute())
    return np.asarray(value)


def _flag_column_description(shape2d):
    """Column description for the version table's FLAG column.

    The column is declared with ``ndim: 2`` exactly as CASA declares it; the
    full shape is fixed by the first cell written, so no shape is given here.
    A TiledShapeStMan group is used to match CASA's layout and to keep sparse
    flags cheap on disk.
    """
    desc = {
        "valueType": "boolean",
        "ndim": 2,
        "_c_order": True,
    }
    if shape2d and shape2d[0] > 0:
        desc["dataManagerType"] = "TiledShapeStMan"
        desc["dataManagerGroup"] = "TiledFlag"
    return desc


def _dminfo(nchan):
    """Storage manager assignment for the version table.

    ``nchan`` is included only to bound the tile shape; TiledShapeStMan
    requires the tile shape to multiply to at most 2**31 elements, so very
    wide MSes need a larger first dimension.
    """
    nchan = max(1, int(nchan))
    other = max(1, _MAX_TILE_CHANNELS // nchan)
    return {
        "TiledFlag": {
            "TYPE": "TiledShapeStMan",
            "NAME": "TiledFlag",
            "SEQNR": 0,
            "SPEC": {"DEFAULTTILESHAPE": np.array([nchan, other], dtype=np.int32)},
            "COLUMNS": ["FLAG"],
        }
    }


def read_ms_flags(ms_path):
    """Read the FLAG column and row flags of a measurement set, from disk.

    Reading straight from the MS (rather than from any in-memory dataset) keeps
    a saved version complete and restorable: a version that held only the rows
    left by a row selection would fail the row-count check on restore.  This is
    also what CASA's flagmanager does.
    """
    t = table(ms_path, ack=False, readonly=True)
    try:
        nrow = t.nrows()
        chunks = []
        for start in range(0, nrow, CHUNK_ROWS):
            n = min(CHUNK_ROWS, nrow - start)
            chunks.append(np.asarray(t.getcol("FLAG", startrow=start, nrow=n)))
        flag = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 0, 0), bool)
        flag_row = np.asarray(t.getcol("FLAG_ROW")) if nrow else np.zeros(0, bool)
    finally:
        t.close()
    return flag, flag_row


def save_version(ms_path, versionname, flag, flag_row, comment=""):
    """Write one flag version, CASA-flagmanager style.

    Args:
        ms_path: the measurement set the flags belong to.
        versionname: name of the version (no embedded blanks, as in CASA).
        flag: the FLAG data, shape ``(nrow, nchan, ncorr)``; may be a dask array.
        flag_row: the per-row flags, shape ``(nrow,)``.
        comment: short description stored in ``FLAG_VERSION_LIST``.

    Returns:
        the path of the written version table.

    An existing version of the same name is moved aside to
    ``<name>.old.<timestamp>`` rather than silently overwritten, matching CASA's
    behaviour, and the version list is updated accordingly.
    """
    if not versionname or versionname != versionname.strip():
        raise ValueError("version name must be non-empty without surrounding blanks")
    if os.sep in versionname or "/" in versionname:
        raise ValueError(f"invalid version name: {versionname!r}")

    _rename_existing(ms_path, versionname)

    path = version_path(ms_path, versionname)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    flag_row = _freeze(flag_row).astype(bool).ravel()
    nrow = int(flag_row.size)

    shape = getattr(flag, "shape", None)
    nchan = int(shape[1]) if shape and len(shape) > 1 else 1

    tabdesc = maketabdesc(
        [
            makecoldesc("FLAG", _flag_column_description((nchan, 0))),
            makecoldesc("FLAG_ROW", {"valueType": "boolean"}),
        ]
    )
    t = table(path, tabdesc, nrow=0, readonly=False, dminfo=_dminfo(nchan))
    try:
        t.addrows(nrow)
        t.putcol("FLAG_ROW", flag_row)
        for start in range(0, nrow, CHUNK_ROWS):
            n = min(CHUNK_ROWS, nrow - start)
            t.putcol("FLAG", _freeze(flag[start:start + n]), startrow=start, nrow=n)
    finally:
        t.close()

    entries = [(n, c) for n, c in read_version_list(ms_path) if n != versionname]
    if not comment:
        comment = "Saved by skarabina on %s" % datetime.datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    entries.append((versionname, comment))
    write_version_list(ms_path, entries)
    return path


def _rename_existing(ms_path, versionname):
    """Move an existing version aside, as CASA's flagmanager does."""
    path = version_path(ms_path, versionname)
    if not os.path.isdir(path):
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    oldname = f"{versionname}.old.{stamp}"
    shutil.move(path, version_path(ms_path, oldname))
    entries = read_version_list(ms_path)
    for i, (name, comment) in enumerate(entries):
        if name == versionname:
            entries[i] = (oldname, comment)
    write_version_list(ms_path, entries)
    print(f"flag version '{versionname}' already existed; moved to '{oldname}'")


def load_version(ms_path, versionname):
    """Read a flag version, returning ``(flag, flag_row)`` as numpy arrays.

    ``flag`` has the shape stored in the version table and ``flag_row`` the
    table's row flags.
    """
    path = version_path(ms_path, versionname)
    if not os.path.isdir(path):
        available = ", ".join(n for n, _ in read_version_list(ms_path)) or "none"
        raise FileNotFoundError(
            f"flag version '{versionname}' not found in "
            f"{os.path.basename(flagversions_path(ms_path))} (available: {available})"
        )

    t = table(path, ack=False, readonly=True)
    try:
        nrow = t.nrows()
        chunks = []
        for start in range(0, nrow, CHUNK_ROWS):
            n = min(CHUNK_ROWS, nrow - start)
            chunks.append(np.asarray(t.getcol("FLAG", startrow=start, nrow=n)))
        flag = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 0, 0), bool)
        flag_row = np.asarray(t.getcol("FLAG_ROW")) if nrow else np.zeros(0, bool)
    finally:
        t.close()
    return flag, flag_row


def list_versions(ms_path):
    """The versions of an MS as ``[(name, comment), ...]``, oldest first."""
    return read_version_list(ms_path)
