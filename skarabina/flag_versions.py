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

# Import dask-ms before casacore.tables so that, when the casacure backend is
# selected (DASK_MS_BACKEND=casacure), daskms's casacore->casacure aliasing is
# installed before the `casacore` import resolves (see skarabina/dask_ms.py).
import daskms  # noqa: F401,E402
from casacore.tables import makecoldesc, maketabdesc, table  # noqa: E402

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


def _check_version_name(versionname):
    """Reject a version name CASA could not use, or that would escape the
    ``.flagversions`` directory (``../x``)."""
    if not versionname or versionname != versionname.strip():
        raise ValueError("version name must be non-empty without surrounding blanks")
    if os.sep in versionname or "/" in versionname:
        raise ValueError(f"invalid version name: {versionname!r}")


def save_version_streaming(ms_path, versionname, comment=""):
    """Back up the MS's FLAG/FLAG_ROW as a CASA flag version, streaming.

    Reads the source MS's FLAG column row-chunk by row-chunk and writes each
    chunk straight into the new flag-version table, so a save never holds the
    whole flag cube in RAM.  On a large MeerKAT MS the full FLAG cube is
    >8 GB (nrow x nchan x ncorr booleans); the previous read-then-write path
    (``read_ms_flags`` + ``save_version``) materialised it twice and peaked at
    ~16 GB RSS.  CASA's flagmanager semantics are unchanged: the saved version
    covers the whole MS regardless of any in-memory row selection.

    Returns the path of the written version.
    """
    _check_version_name(versionname)
    _rename_existing(ms_path, versionname)

    path = version_path(ms_path, versionname)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    src = table(ms_path, ack=False, readonly=True)
    try:
        nrow = src.nrows()
        # Channel count of the FLAG cube (1 row read just to learn the shape).
        nchan = int(src.getcol("FLAG", startrow=0, nrow=1).shape[1]) if nrow else 1
        flag_desc = _flag_column_description((nchan, 0))
    finally:
        src.close()

    tabdesc = maketabdesc(
        [
            makecoldesc("FLAG", flag_desc),
            makecoldesc("FLAG_ROW", {"valueType": "boolean"}),
        ]
    )
    t = table(path, tabdesc, nrow=0, readonly=False, dminfo=_dminfo(nchan))
    try:
        t.addrows(nrow)
        src = table(ms_path, ack=False, readonly=True)
        try:
            for start in range(0, nrow, CHUNK_ROWS):
                n = min(CHUNK_ROWS, nrow - start)
                chunk = np.asarray(
                    src.getcol("FLAG", startrow=start, nrow=n), dtype=bool
                )
                t.putcol("FLAG", chunk, startrow=start, nrow=n)
                row_chunk = np.asarray(
                    src.getcol("FLAG_ROW", startrow=start, nrow=n), dtype=bool
                )
                t.putcol("FLAG_ROW", row_chunk, startrow=start, nrow=n)
                # Flush per chunk, so the write buffer holds one chunk: a
                # table backend that buffers writes (casacure) otherwise
                # keeps the whole flag cube until close -- 18-19 GB on
                # mergA_tim.ms.  casacure grows the table in place, so each
                # flush costs the chunk, not the table (the first one also
                # gives the shape-less FLAG column its cell shape).
                t.flush()
        finally:
            src.close()
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
    _check_version_name(versionname)
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
        for start in range(0, nrow, CHUNK_ROWS):
            n = min(CHUNK_ROWS, nrow - start)
            t.putcol("FLAG", _freeze(flag[start:start + n]), startrow=start, nrow=n)
            t.putcol("FLAG_ROW", flag_row[start:start + n], startrow=start, nrow=n)
            t.flush()  # one chunk buffered at a time (see save_version_streaming)
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


def _read_version_rows(path, start, nrow):
    """FLAG rows ``[start, start + nrow)`` of a flag-version table."""
    t = table(path, ack=False, readonly=True)
    try:
        return np.asarray(t.getcol("FLAG", startrow=start, nrow=nrow), dtype=bool)
    finally:
        t.close()


def load_version_lazy(ms_path, versionname, row_chunks):
    """A flag version as ``(flag, flag_row)``: FLAG as a lazy dask array.

    ``flag`` is cut into ``row_chunks`` (the dataset's row chunks) and each
    chunk is read from the version table only when a pass needs it, so a
    restored version costs one chunk per dask worker rather than the whole
    flag cube -- 8 GB for a 1.6M-row, 2511-channel MS, held for the rest of
    the run by :func:`load_version`.  ``flag_row`` (one bool per row) is read
    eagerly.  Raises like :func:`load_version` for a missing version.
    """
    import dask.array as da
    from dask import delayed

    path = version_path(ms_path, versionname)
    if not os.path.isdir(path):
        load_version(ms_path, versionname)          # raises the helpful error
    t = table(path, ack=False, readonly=True)
    try:
        nrow = t.nrows()
        cell = t.getcol("FLAG", startrow=0, nrow=1).shape[1:] if nrow else (0, 0)
        flag_row = np.asarray(t.getcol("FLAG_ROW")) if nrow else np.zeros(0, bool)
    finally:
        t.close()
    if sum(row_chunks) != nrow:
        # Let the caller report the mismatch with the shapes.
        row_chunks = (nrow,) if nrow else ()
    blocks, start = [], 0
    for n in row_chunks:
        blocks.append(da.from_delayed(
            delayed(_read_version_rows)(path, start, n),
            shape=(n,) + tuple(cell), dtype=bool,
        ))
        start += n
    flag = da.concatenate(blocks, axis=0) if blocks \
        else da.zeros((0,) + tuple(cell), dtype=bool)
    return flag, flag_row


def list_versions(ms_path):
    """The versions of an MS as ``[(name, comment), ...]``, oldest first."""
    return read_version_list(ms_path)
