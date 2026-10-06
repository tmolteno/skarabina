# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Flagging calibration tables.

CASA's ``flagdata`` flags caltables as well as measurement sets --
``flagdata(vis=bandpass_table, mode='tfcrop', datacolumn='CPARAM')`` flags
outlier bandpass solutions, which is how meerkat_imaging's stage 1 rejects
bad antennas from ``multi.B0``/``multi.B1``.  This module gives skarabina
the same reach, without dask: a caltable is small (one row per antenna per
solution time -- the fast profile's ``multi.B0`` is 183 rows x 79 channels
x 2 correlations), so the whole table is read once with casacore and the
verbs run on plain numpy planes through the *same* plane functions the MS
path uses (:func:`skarabina.tfcrop.tfcrop_plane`, :func:`skarabina.rflag.rflag_plane`).

The rows are one antenna's solutions, interleaved across antennas in time
order exactly as an MS chunk interleaves baselines, so the per-antenna
grouping (:class:`skarabina.baselines.Baselines`, fed ANTENNA1/ANTENNA2)
applies unchanged.

Supported: CPARAM tables (complex gain solutions) and the verbs
``tfcrop``, ``rflag`` and the ``nan``/``clip`` pair of ``--flag``.  A
caltable has no UVW, no scans to average, no flag-version manager here and
no neural-flagger server, so every other verb and every MS-only option is
rejected with the reason (see the ``CalTable`` methods).  ``extend`` is a
dask-graph verb on the MS path and is not reimplemented here; nothing in
the pipeline's caltable steps needs it.
"""

import os

import numpy as np
from casacore.tables import table

from skarabina.dask_ms import _rflag_block, _tfcrop_block

#: How many rows are processed per numpy pass.  A caltable row is a few
#: hundred floats, so this bounds the working set the way the MS path's
#: row chunk does, while keeping the per-antenna groups intact (a group is
#: one antenna's solutions, a handful of rows).
ROW_CHUNK = 10000


def table_kind(path):
    """'ms' for a measurement set, 'cal' for a calibration table.

    The same rule :func:`skarabina.plotms._table_kind` applies (that one
    raises click's exception instead of ``RuntimeError``): an ``MS_VERSION``
    table keyword marks an MS; a CPARAM/FPARAM/SPARAM column marks a
    caltable; a DATA column is taken as an MS that predates the keyword.
    """
    with table(path, ack=False) as tab:
        cols = set(tab.colnames())
        keywords = (list(tab.keywordnames())
                    if hasattr(tab, "keywordnames") else list(tab.getkeywords()))
        if "MS_VERSION" in keywords:
            return "ms"
        if cols & {"CPARAM", "FPARAM", "SPARAM"}:
            return "cal"
        if "DATA" in cols:
            return "ms"
    raise RuntimeError(
        f"{path}: neither a measurement set nor a caltable"
        " (no MS_VERSION keyword, no CPARAM/FPARAM/SPARAM column)"
    )


def _subtable_int(path, subtable, column):
    """The first value of a subtable column, or None when absent."""
    for candidate in (f"{path}::{subtable}", os.path.join(path, subtable)):
        if os.path.exists(candidate):
            with table(candidate, ack=False) as tab:
                if column in tab.colnames() and tab.nrows() > 0:
                    return int(np.asarray(tab.getcol(column)).ravel()[0])
    return None


class CalTable:
    """The flagging verbs on one calibration table, CPARAM only.

    Implements the slice of the ``DaskMS`` interface that
    :func:`skarabina.flag_ops.run` and ``main`` drive, so the ``--flag``
    list runs through the same dispatcher; every method the MS path has
    that a caltable cannot support raises ``RuntimeError`` naming the
    reason, so a mis-aimed run fails loudly rather than doing nothing.
    """

    #: Written by flag_ops.run; the statistics here are computed
    #: immediately, so deferral is accepted and ignored.
    defer_reports = False

    def __init__(self, path):
        self.path = path
        kind = table_kind(path)
        if kind != "cal":
            raise RuntimeError(
                f"{path}: a measurement set needs the DaskMS path, not"
                " CalTable"
            )
        with table(path, ack=False) as tab:
            cols = set(tab.colnames())
        for column in ("FPARAM", "SPARAM"):
            if column in cols and "CPARAM" not in cols:
                raise RuntimeError(
                    f"{path}: a {column} caltable (delay/other real-valued"
                    " solutions) is not supported -- the flaggers judge the"
                    " amplitude of a complex solution, which CPARAM holds."
                    " Only CPARAM caltables can be flagged."
                )
        self.column = "CPARAM"
        # Cell orientation is not fixed across caltable kinds and writers:
        # CASA's bandpass tables store (chan, corr) cells while gain tables
        # (and the test fixture) store (corr, chan).  Read the axes from the
        # subtables rather than assuming either.
        nchan = _subtable_int(path, "SPECTRAL_WINDOW", "NUM_CHAN")
        ncorr = _subtable_int(path, "POLARIZATION", "NUM_CORR")
        with table(path, ack=False) as tab:
            head = tab.getcol(self.column, 0, 1)
        if head.ndim != 3:
            raise RuntimeError(
                f"{path}: {self.column} cell is not (axis, axis) shaped"
                f" (first row {head.shape})"
            )
        axes = {head.shape[1], head.shape[2]}
        if nchan in axes and ncorr in axes and nchan != ncorr:
            self._cells_chan_first = head.shape[1] == nchan
        else:
            # Undetectable (equal, or missing subtable facts): CASA's own
            # bandpass writer, the one pipeline caltables come from.
            self._cells_chan_first = True
        self._data = None       # (nrow, nchan, ncorr) complex128
        self._flag = None       # same shape, bool, the working copy
        self._rows = None       # (antenna1, antenna2, scan) per row
        self._perm = None       # load-time sort permutation, or None

    # --- loading -----------------------------------------------------

    def _load(self):
        """Read CPARAM/FLAG once; the working flags mutate after this."""
        if self._data is not None:
            return
        with table(self.path, ack=False) as tab:
            data = tab.getcol(self.column).astype(np.complex128)
            flag = np.asarray(tab.getcol("FLAG"), dtype=bool)
            antenna1 = np.asarray(tab.getcol("ANTENNA1"), dtype=np.int64)
            antenna2 = np.asarray(tab.getcol("ANTENNA2"), dtype=np.int64)
            scan = (np.asarray(tab.getcol("SCAN_NUMBER"), dtype=np.int64)
                    if "SCAN_NUMBER" in tab.colnames()
                    else np.zeros(data.shape[0], dtype=np.int64))
            times = np.asarray(tab.getcol("TIME"), dtype=np.float64)
        if not self._cells_chan_first:
            data = np.swapaxes(data, 1, 2)
            flag = np.swapaxes(flag, 1, 2)
        if data.ndim != 3 or data.shape != flag.shape:
            raise RuntimeError(
                f"{self.path}: {self.column} {data.shape} and FLAG"
                f" {flag.shape} disagree"
            )
        # The verbs take each row group to be a time series; a caltable
        # written out of time order would be judged in the wrong order.
        # Sort once and remember the permutation for the write-back.
        if (np.diff(times) < 0).any():
            self._perm = np.argsort(times, kind="stable")
            data, flag = data[self._perm], flag[self._perm]
            antenna1, antenna2, scan = (antenna1[self._perm],
                                        antenna2[self._perm], scan[self._perm])
        self._data = data
        self._flag = flag
        self._rows = (antenna1, antenna2, scan)

    # --- interface no-ops --------------------------------------------

    def flush_reports(self):
        """Nothing is deferred; the counts printed as each verb ran."""

    def materialise_flags(self):
        """The flags are plain numpy already."""

    # --- verbs ---------------------------------------------------------

    def set_data_column(self, spec):
        """The caltable's data column is CPARAM; accept the default or the
        explicit name and reject the MS spellings."""
        if spec.upper() in ("DATA", "CPARAM"):
            return
        raise RuntimeError(
            f"--data-column {spec!r}: a caltable is flagged on CPARAM (its"
            " complex solutions). CORRECTED/MODEL/RESIDUAL are measurement"
            " set columns."
        )

    def flag_data(self, operations=None):
        """The ``nan`` and ``clip`` verbs, on the CPARAM amplitudes.

        Same rule as the MS path: ``nan`` flags a non-finite amplitude,
        ``clip lo hi`` flags an amplitude at or beyond the bounds.
        """
        operations = operations or {}
        self._load()
        amp = np.abs(self._data)
        total = amp.size
        if "NAN" in operations:
            mask = ~np.isfinite(amp)
            self._flag |= mask
            print("flag_data (NaN): flagged %d / %d visibilities (%.2f%%)"
                  % (int(mask.sum()), total, 100.0 * mask.sum() / total))
        if "CLIP" in operations:
            lo, hi = operations["CLIP"]
            mask = (amp <= lo) | (amp >= hi)
            self._flag |= mask
            print("flag_data (clip [%s, %s]): flagged %d / %d visibilities"
                  " (%.2f%%)"
                  % (lo, hi, int(mask.sum()), total, 100.0 * mask.sum() / total))

    def flag_tfcrop(self, params):
        """:meth:`DaskMS.flag_tfcrop` on the caltable's planes."""
        self._run(_tfcrop_block, params, "flag_tfcrop")

    def flag_rflag(self, params):
        """:meth:`DaskMS.flag_rflag` on the caltable's planes."""
        self._run(_rflag_block, params, "flag_rflag")

    def _run(self, block_function, params, label):
        """Run one verb over the table in row chunks, flags accumulated.

        The chunk is a slice of rows -- whole antennas at a time, since a
        caltable groups an antenna's solutions together in time order and
        :class:`Baselines` reorders within the chunk.  On a table the size
        of a bandpass solve the chunk is the whole table, which also makes
        the MS path's per-chunk thresholds here behave like CASA's
        selection-wide ones.
        """
        self._load()
        data, flag = self._data, self._flag
        pre = flag.copy()
        total = flag.size
        nrow = data.shape[0]
        for start in range(0, nrow, ROW_CHUNK):
            stop = min(start + ROW_CHUNK, nrow)
            chunk = data[start:stop]
            existing = flag[start:stop]
            rows = tuple(column[start:stop] for column in self._rows)
            flag[start:stop] = block_function(chunk, existing, params, rows)
        newly = int(np.count_nonzero(flag & ~pre))
        print("%s: %d of %d visibilities flagged, %d newly (%.2f%% of all)"
              % (label, int(flag.sum()), total, newly,
                 100.0 * newly / total if total else 0.0))

    # --- MS-only verbs: rejected with the reason ----------------------

    def _unsupported(self, what, why):
        raise RuntimeError(
            f"{what}: not supported on a caltable ({self.path}) -- {why}"
        )

    def save_flag_version(self, versionname, comment="", snapshot=False):
        self._unsupported(
            f"save:{versionname}",
            "flag versions are a measurement-set flagmanager; flag the"
            " caltable and keep the table itself (a caltable is small).")

    def restore_flag_version(self, versionname):
        self._unsupported(
            f"restore:{versionname}",
            "flag versions are a measurement-set flagmanager.")

    def flag_autocorrelations(self):
        self._unsupported(
            "autos", "a caltable has no autocorrelations (it holds one"
            " solution per antenna).")

    def flag_uv_above(self, uv_limit):
        self._unsupported(
            f"uv-above {uv_limit}", "a caltable has no UVW.")

    def flag_uv_below(self, uv_limit):
        self._unsupported(
            f"uv-below {uv_limit}", "a caltable has no UVW.")

    def flag_spectral_window(self, yaml_file):
        self._unsupported(
            "spectral-window", "a caltable has one spectral window per"
            " solve and no RFI bands to pre-flag.")

    def flag_extend(self, params):
        self._unsupported(
            "extend", "extend is a dask-graph verb on the MS path and is"
            " not implemented for caltables.")

    def flag_tf_nn(self, params):
        self._unsupported("tf-nn", "no neural-flagger server for solutions.")

    def flag_nn_flagger(self, params):
        self._unsupported("nn-flagger", "no neural-flagger server for"
                          " solutions.")

    # --- measurement-set plumbing: rejected ----------------------------

    def select_scans(self, spec):
        self._unsupported("--scan", "scan selection of a caltable is not"
                          " implemented; every solution is flagged.")

    def set_field_scope(self, spec):
        self._unsupported("--field", "field scoping of a caltable is not"
                          " implemented; every solution is flagged.")

    def rebase_data_column(self, source):
        self._unsupported("--data-from", "a caltable has one data column.")

    def frequency_average(self, factor):
        self._unsupported("--frequency-average-factor", "caltables are"
                          " written, not regridded.")

    def time_average(self, factor):
        self._unsupported("--time-average-factor", "caltables are written,"
                          " not averaged.")

    def optimize(self, keep_fully_flagged_channels=False):
        self._unsupported("--optimize", "caltables are updated in place.")

    def write_new_ms(self, name, clobber, split=None, changed_only=False):
        self._unsupported("--msout", "a caltable is updated in place with"
                          " --apply.")

    def barber(self, *args, **kwargs):
        self._unsupported("--barber", "a caltable has no visibilities.")

    # --- reports and the write ------------------------------------------

    def summary(self):
        """Flagged fraction overall and per antenna, as the MS summary's
        per-antenna block is."""
        self._load()
        flag, (a1, _, _) = self._flag, self._rows
        total = flag.size
        print("summary: %d of %d visibilities flagged (%.2f%%)"
              % (int(flag.sum()), total, 100.0 * flag.mean() if total else 0.0))
        for antenna in np.unique(a1):
            sel = a1 == antenna
            print("  antenna %d: %.2f%% flagged"
                  % (antenna, 100.0 * flag[sel].mean()))

    def update_ms(self, name, clobber):
        """Write the flags back to the table's FLAG column, in place."""
        if not clobber:
            raise RuntimeError(
                f"Calibration table {name} can't be changed. Use --clobber"
                " to overwrite"
            )
        self._load()
        with table(self.path, readonly=False, ack=False) as tab:
            if "FLAG" not in tab.colnames():
                raise RuntimeError(
                    f"{self.path}: no FLAG column to write"
                )
            # Back to the cell orientation the table stores: the working
            # flags are (row, chan, corr) throughout.
            out = self._flag if self._cells_chan_first \
                else np.swapaxes(self._flag, 1, 2)
            if self._perm is None:
                tab.putcol("FLAG", out)
            else:
                final = np.empty_like(out)
                final[self._perm] = out
                tab.putcol("FLAG", final)
        print("Updated table: FLAG in %s" % self.path)
