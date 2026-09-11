# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import logging
import os
import shutil
import sys
from contextlib import contextmanager

import dask
import dask.array as da
import numpy as np
import yaml
from casacore.tables import table
from dask.array import coarsen as da_coarsen
from dask.diagnostics import ProgressBar
from daskms import xds_from_ms, xds_to_table

logger = logging.getLogger(__name__)


def _block_reduce(func, arr, factor, axis):
    """Apply ``func`` over consecutive blocks of ``factor`` along ``axis``.

    Uses :func:`dask.array.coarsen`, which handles chunk boundaries that
    are not evenly divisible by ``factor`` without fragmenting chunks
    (unlike a manual reshape, which splits irregularly and forces an
    expensive rechunk).  Trailing elements past the last full block are
    dropped (the caller handles them explicitly where needed).
    """
    return da_coarsen(func, arr, {axis: factor}, trim_excess=True)


@contextmanager
def _maybe_quiet_stderr():
    """Suppress stderr unless root logger is at DEBUG level."""
    if logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
        yield
    else:
        with open(os.devnull, "w") as devnull:
            old_stderr = sys.stderr
            sys.stderr = devnull
            try:
                yield
            finally:
                sys.stderr = old_stderr


def parse_scan_spec(spec):
    """Parse a CASA-style scan selection into a sorted list of scan numbers.

    ``spec`` is a comma-separated list of scan numbers and inclusive ranges,
    e.g. ``"1,12,14"``, ``"0~5"`` or ``"0~5,20,30~32"``.  Whitespace is
    ignored.  ``None`` or an empty string selects every scan and returns
    ``None`` (the caller then leaves the dataset untouched).

    Raises :class:`RuntimeError` for anything that is not a number or a
    ``lo~hi`` range, and for a specification that names no scans at all.
    """
    if spec is None:
        return None
    text = str(spec).strip()
    if not text:
        return None

    scans = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "~" in part:
            lo_s, _, hi_s = part.partition("~")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError:
                raise RuntimeError(
                    f"Bad scan range {part!r} in scan selection {text!r}"
                    " (expected e.g. '0~5')"
                ) from None
            if hi < lo:
                lo, hi = hi, lo
            scans.update(range(lo, hi + 1))
        else:
            try:
                scans.add(int(part))
            except ValueError:
                raise RuntimeError(
                    f"Bad scan number {part!r} in scan selection {text!r}"
                    " (expected e.g. '1,12,14' or '0~5')"
                ) from None

    if not scans:
        return None
    return sorted(scans)


def spw_column_updates(nchan, chan_freq_hz, axis_hz=None):
    """Column values for a single-SPW SPECTRAL_WINDOW subtable.

    Returns a ``{column_name: value}`` mapping suitable for ``putcol()`` on a
    one-row SPECTRAL_WINDOW table: ``NUM_CHAN``, ``CHAN_FREQ``, every
    per-channel column supplied in ``axis_hz`` (``CHAN_WIDTH``,
    ``EFFECTIVE_BW`` and/or ``RESOLUTION``) and ``TOTAL_BANDWIDTH``.

    The subtable is copied verbatim from the input MS when a new MS is written,
    so it still describes the *input* channel setup after frequency averaging
    or after fully-flagged channels have been removed.  Writing these columns
    back is what keeps the subtable consistent with the main table -- every
    per-channel column must have exactly ``NUM_CHAN`` entries, or readers such
    as dask-ms reject the MS with "conflicting sizes for dimension 'chan'".
    """
    freq = np.asarray(chan_freq_hz, dtype=float).reshape(-1)
    nchan = int(nchan)
    if freq.size != nchan:
        raise RuntimeError(
            f"SPECTRAL_WINDOW bookkeeping error: {freq.size} channel"
            f" frequencies for {nchan} data channels"
        )

    updates = {
        "NUM_CHAN": np.array([nchan], dtype=np.int32),
        "CHAN_FREQ": freq.reshape(1, -1),
    }

    for name, values in (axis_hz or {}).items():
        if values is None:
            continue
        arr = np.asarray(values, dtype=float).reshape(-1)
        if arr.size != nchan:
            raise RuntimeError(
                f"SPECTRAL_WINDOW bookkeeping error: {arr.size} entries for"
                f" {name} but {nchan} data channels"
            )
        updates[name] = arr.reshape(1, -1)

    # TOTAL_BANDWIDTH is the sum of the channel widths.  CHAN_WIDTH is the
    # physical width, so prefer it over the (usually identical) RESOLUTION.
    for name in ("CHAN_WIDTH", "EFFECTIVE_BW", "RESOLUTION"):
        if name in updates:
            updates["TOTAL_BANDWIDTH"] = np.array(
                [float(np.sum(updates[name]))]
            )
            break

    return updates


class DaskMS:
    # Channel bookkeeping, filled in by __init__ from the SPECTRAL_WINDOW
    # subtable.  Class-level defaults keep instances built via __new__ (and the
    # synthetic doubles used in the tests) working.
    chan_freq_hz = None
    #: Per-channel SPECTRAL_WINDOW columns, keyed by column name.  Each one is
    #: averaged/subsampled alongside the data, so the written subtable stays
    #: consistent with the main table.
    chan_axis_hz = None
    spw_chan_count = None
    nspw = 0

    @property
    def chan_width_hz(self):
        """The SPECTRAL_WINDOW RESOLUTION column (per-channel width, Hz)."""
        return (self.chan_axis_hz or {}).get("RESOLUTION")

    @chan_width_hz.setter
    def chan_width_hz(self, value):
        if self.chan_axis_hz is None:
            self.chan_axis_hz = {}
        self.chan_axis_hz["RESOLUTION"] = value

    def __init__(self, ms_name):
        self.name = ms_name
        print(f"Getting Data from MS file: {self.name}")

        if not os.path.exists(ms_name):
            raise RuntimeError(f"Measurement set {self.name} not found")

        # Some casacore bits here
        t = table(self.name)
        self.sub_table_names = t.getsubtables()
        t.close()
        logger.debug("Sub-table Names:")
        for s in self.sub_table_names:
            logger.debug(f"    {s}")

        # dask-ms groups by (FIELD_ID, DATA_DESC_ID) by default, which splits a
        # multi-field MS into one dataset *per field*.  Flagging, averaging and
        # writing have to see every field of a spectral window at once -- an MS
        # of calibrators plus targets would otherwise be silently reduced to
        # its first field.  Group by DATA_DESC_ID only, so all fields (with a
        # per-row FIELD_ID) travel together.
        self.datasets = xds_from_ms(self.name, group_cols=("DATA_DESC_ID",))
        logger.debug(self.datasets)

        # Everything below operates on a single dataset, so silently processing
        # only the first one would quietly discard data (and write a truncated
        # output MS).
        if len(self.datasets) > 1:
            raise RuntimeError(
                f"Measurement set {self.name} contains {len(self.datasets)}"
                " DATA_DESC_IDs (i.e. more than one spectral window); skarabina"
                " processes a single spectral window. Split the MS by spectral"
                " window first (e.g. CASA mstransform/split), then run"
                " skarabina on each part."
            )

        self.ds = self.datasets[0]
        self.flag = da.asarray(self.ds.FLAG)
        self.flag_row = da.asarray(self.ds.FLAG_ROW)
        logger.debug(f"FLAG_ROW = {self.ds.FLAG_ROW}")
        self.antenna1 = da.asarray(self.ds.ANTENNA1)
        self.antenna2 = da.asarray(self.ds.ANTENNA2)

        self.data = da.asarray(self.ds.DATA)

        self.uvw = da.asarray(self.ds.UVW)
        self.u_arr = self.uvw[:, 0].T
        self.v_arr = self.uvw[:, 1].T
        self.w_arr = self.uvw[:, 2].T
        self.time = da.asarray(self.ds.TIME)

        try:
            self.weight_spectrum = da.asarray(self.ds.WEIGHT_SPECTRUM)
        except AttributeError:
            self.weight_spectrum = da.ones_like(self.data)

        # self.flag_mask = da.where(da.logical_not(self.flag), 1, 0)

        # Use casacore table.get_subtables

        # self.sub_tables = {}
        # for s in self.sub_table_names:
        #     try:
        #         self.sub_tables[s] = xds_from_table(s)
        #     except:
        #         print(f"Failed to open {s} as a subtable")
        #         pass
        self.changed = {}

        # Load the channel axis from the SPECTRAL_WINDOW subtable: the centre
        # frequencies, plus every per-channel width column (CHAN_WIDTH,
        # EFFECTIVE_BW, RESOLUTION).  All of them have NUM_CHAN entries and all
        # of them must be rewritten when the channel count changes.
        self.chan_freq_hz = None
        self.chan_axis_hz = {}
        # Number of channels the *subtable* currently describes.  Kept so that
        # write_new_ms() can tell whether the subtable needs rewriting after
        # frequency averaging / channel removal.
        self.spw_chan_count = None
        self.nspw = 0
        for s in self.sub_table_names:
            if "SPECTRAL_WINDOW" in s:
                try:
                    sw = table(s, ack=False)
                    chan_freq = sw.getcol("CHAN_FREQ")
                    self.nspw = chan_freq.shape[0]
                    self.chan_freq_hz = chan_freq[0]
                    self.spw_chan_count = chan_freq.shape[1]
                    for col in ("CHAN_WIDTH", "EFFECTIVE_BW", "RESOLUTION"):
                        if col in sw.colnames():
                            self.chan_axis_hz[col] = sw.getcol(col)[0]
                    sw.close()
                    # If the subtable has more channels than the actual
                    # data (e.g. from a pre-fix frequency-averaged MS),
                    # truncate to match.
                    nchan_ds = self.ds.FLAG.shape[1]
                    if len(self.chan_freq_hz) != nchan_ds:
                        logger.warning(
                            "CHAN_FREQ has %d entries but data has %d"
                            " channels — truncating",
                            len(self.chan_freq_hz),
                            nchan_ds,
                        )
                        self.chan_freq_hz = self.chan_freq_hz[:nchan_ds]
                        for col, values in self.chan_axis_hz.items():
                            self.chan_axis_hz[col] = values[:nchan_ds]
                except Exception:
                    logger.warning("Could not read CHAN_FREQ from %s", s)

    def _refresh_cached_columns(self):
        """Re-snapshot the column attributes set up in ``__init__``.

        ``__init__`` caches ``data``/``flag``/``uvw``/``u_arr``/``v_arr``/...
        as dask arrays.  Any operation that changes the *shape* of the dataset
        (row or channel selection) must refresh them, or later code that still
        reads the cached attributes would work on the pre-selection arrays.
        """
        ds = self.ds
        self.flag = da.asarray(ds.FLAG)
        self.flag_row = da.asarray(ds.FLAG_ROW)
        self.antenna1 = da.asarray(ds.ANTENNA1)
        self.antenna2 = da.asarray(ds.ANTENNA2)
        self.data = da.asarray(ds.DATA)
        self.uvw = da.asarray(ds.UVW)
        self.u_arr = self.uvw[:, 0].T
        self.v_arr = self.uvw[:, 1].T
        self.w_arr = self.uvw[:, 2].T
        self.time = da.asarray(ds.TIME)
        try:
            self.weight_spectrum = da.asarray(ds.WEIGHT_SPECTRUM)
        except AttributeError:
            self.weight_spectrum = da.ones_like(self.data)

    def select_scans(self, spec):
        """Keep only the rows belonging to the selected scans.

        ``spec`` is a CASA-style selection: a comma-separated list of scan
        numbers and inclusive ranges (e.g. ``"1,12,14"`` or ``"0~5"``); an
        empty specification keeps every scan (see :func:`parse_scan_spec`).

        Filtering happens at read time, so every later operation -- flagging,
        averaging, ``optimize`` and the write-out -- sees only the selected
        scans.
        """
        scans = parse_scan_spec(spec)
        if scans is None:
            return

        if "SCAN_NUMBER" not in self.ds.data_vars:
            raise RuntimeError(
                "MS has no SCAN_NUMBER column — cannot select scans"
            )

        scan_numbers = da.asarray(self.ds.SCAN_NUMBER.data)
        mask = da.isin(scan_numbers, np.asarray(scans))
        indices = da.nonzero(mask)[0].compute()
        if indices.size == 0:
            raise RuntimeError(
                f"Scan selection {str(spec)!r}: no rows in scans {scans}"
            )

        row_dim = self.ds.DATA.dims[0]
        n_before = int(self.ds.DATA.shape[0])
        self.ds = self.ds.isel({row_dim: indices})
        for var_name in self.ds.data_vars:
            if row_dim in self.ds[var_name].dims:
                self.changed[var_name] = True

        self._refresh_cached_columns()
        print(
            f"--scan {str(spec).strip()!r}: kept {indices.size} of {n_before}"
            f" rows, scans {scans}"
        )

    def flag_uv_above(self, uv_limit):
        """
        Flag rows where sqrt(u^2 + v^2) exceeds uv_limit (in meters).
        """
        print("flag_uv_above: %.1f m" % uv_limit)

        # Read from the live dataset: self.u_arr/v_arr/flag_row are __init__
        # snapshots that go stale once rows have been selected.
        uvw = self.ds["UVW"].data
        abs_uv = uvw[:, 0] * uvw[:, 0] + uvw[:, 1] * uvw[:, 1]
        uv_flag_mask = da.greater(abs_uv, uv_limit * uv_limit)
        new_flag_row = da.logical_or(uv_flag_mask, self.ds["FLAG_ROW"].data)

        n_old = da.sum(self.ds["FLAG_ROW"].data)
        n_new = da.sum(new_flag_row)
        n_uv = da.sum(uv_flag_mask)
        max_uv = da.sqrt(da.max(abs_uv))

        n_old_v, n_new_v, n_uv_v, max_uv_v = dask.compute(n_old, n_new, n_uv, max_uv)

        n_added = int(n_new_v) - int(n_old_v)
        print("flag_uv_above: max UV distance = %.1f m" % max_uv_v)
        print(
            "flag_uv_above: %d rows above uv limit, %d newly flagged (total: %d)"
            % (int(n_uv_v), n_added, int(n_new_v))
        )

        self.ds["FLAG_ROW"] = (self.ds.FLAG_ROW.dims, new_flag_row)
        self.changed["FLAG_ROW"] = True

    def flag_spectral_window(self, yaml_file):
        """
        Flag spectral windows from a YAML configuration file.

        YAML format — a list of entries, each with:
          spw: [[fmin_MHz, fmax_MHz], ...]   # frequency ranges to flag
          uv_below: <meters>                  # optional: only flag rows with UV < this
          uv_above: <meters>                  # optional: only flag rows with UV > this
        """
        if self.chan_freq_hz is None:
            raise RuntimeError(
                "No SPECTRAL_WINDOW/CHAN_FREQ found in MS — cannot flag by frequency"
            )

        with open(yaml_file) as f:
            entries = yaml.safe_load(f)

        if not isinstance(entries, list):
            raise RuntimeError("Spectral window YAML must be a list of entries")

        nchan = len(self.chan_freq_hz)
        ncorr = self.ds.FLAG.shape[2]
        # Materialize uv_distance once (numpy) so every UV-constrained
        # entry does a cheap numpy comparison on the cached array rather
        # than re-evaluating the sqrt(u^2+v^2) dask graph per entry.
        # UVW comes from the live dataset (self.u_arr/self.v_arr are the
        # __init__ snapshots and go stale after row selection).
        uvw = self.ds["UVW"].data
        uv_dist = da.sqrt(uvw[:, 0] ** 2 + uvw[:, 1] ** 2).compute()
        # Read FLAG fresh from the live dataset so we OR onto the current
        # flags (including NaN/clip flags from flag_data), not the stale
        # __init__ snapshot.
        old_flags = self.ds.FLAG.data
        new_flags = old_flags

        # Collect per-entry stats and build the combined flag update lazily,
        # so the whole YAML triggers a SINGLE dask pass (rather than one
        # scheduler round-trip per entry).  Per-entry visibility counts
        # factor to (channels x rows x corr) from 1D gates, avoiding a
        # materialized (nrow, nchan, ncorr) sum just to count.
        entry_stats = []  # (idx, n_chan, n_ranges, n_vis, uv_info)
        for idx, entry in enumerate(entries):
            spw_ranges = entry.get("spw", [])
            uv_below = entry.get("uv_below")
            uv_above = entry.get("uv_above")

            # Build channel mask: True where frequency falls in any range.
            # chan_freq_hz is a numpy array, so this is cheap and eager.
            chan_mask = np.zeros(nchan, dtype=bool)
            for fmin_mhz, fmax_mhz in spw_ranges:
                fmin_hz = float(fmin_mhz) * 1e6
                fmax_hz = float(fmax_mhz) * 1e6
                chan_mask = np.logical_or(
                    chan_mask,
                    (self.chan_freq_hz >= fmin_hz) & (self.chan_freq_hz <= fmax_hz),
                )
            n_chan_flagged = int(np.sum(chan_mask))

            # Per-row gate: which rows this entry applies to (numpy
            # comparisons on the cached uv_dist).
            row_gate = np.ones(self.ds.FLAG.shape[0], dtype=bool)
            uv_info = ""
            if uv_below is not None:
                row_gate = row_gate & (uv_dist < float(uv_below))
                uv_info += f", UV < {uv_below} m"
            if uv_above is not None:
                row_gate = row_gate & (uv_dist > float(uv_above))
                uv_info += f", UV > {uv_above} m"
            n_row_flagged = int(np.sum(row_gate))

            entry_stats.append(
                (idx, n_chan_flagged, len(spw_ranges),
                 n_chan_flagged * n_row_flagged * ncorr, uv_info)
            )

            # Build the (nrow, nchan, ncorr) flag contribution from the
            # two 1D gates and OR it into the running combined flag.
            spw_flag = da.logical_and(
                chan_mask[np.newaxis, :, np.newaxis],  # broadcast over row, corr
                row_gate[:, np.newaxis, np.newaxis],   # broadcast over chan, corr
            )
            new_flags = da.logical_or(new_flags, spw_flag)

        for idx, n_chan, n_ranges, n_vis, uv_info in entry_stats:
            print(
                "flag_spectral_window[%d]: %d channels in %d range(s),"
                " flagged %d visibilities%s"
                % (idx, n_chan, n_ranges, n_vis, uv_info)
            )

        # Keep FLAG as a lazy dask array — downstream methods and writers
        # expect self.ds["FLAG"].data to stay lazy (they call .compute()).
        self.ds["FLAG"].data = new_flags
        self.changed["FLAG"] = True

    def flag_data(self, operations=None):
        """
        flag_data: Flag all NAN visibilities.
        """
        if operations is None:
            operations = {}
        # Read from the live dataset: self.data/self.flag are __init__
        # snapshots that go stale once rows have been selected.
        abs_vis = da.abs(self.ds.DATA.data)
        update = False
        n_nan = 0
        n_clip = 0

        old_flags = self.ds.FLAG.data
        if "NAN" in operations:
            nan_flag_mask = da.isnan(abs_vis)
            n_nan = da.sum(nan_flag_mask)
            nan_updated_flags = da.logical_or(nan_flag_mask, old_flags)
            update = True
        else:
            nan_updated_flags = old_flags

        if "CLIP" in operations:
            clip_min, clip_max = operations["CLIP"]
            min_flag_mask = da.less_equal(abs_vis, clip_min)
            max_flag_mask = da.greater_equal(abs_vis, clip_max)
            clip_flag_mask = da.logical_or(min_flag_mask, max_flag_mask)
            n_clip = da.sum(clip_flag_mask)
            clip_updated_flags = da.logical_or(clip_flag_mask, nan_updated_flags)
            update = True
        else:
            clip_updated_flags = nan_updated_flags

        if update:
            self.ds["FLAG"].data = clip_updated_flags
            self.changed["FLAG"] = True
            total_vis = da.prod(da.array(self.ds.FLAG.shape))
            n_nan_v, n_clip_v, total_v = dask.compute(n_nan, n_clip, total_vis)
            if "NAN" in operations:
                print(
                    "flag_data (NaN): flagged %d / %d visibilities (%.2f%%)"
                    % (int(n_nan_v), int(total_v), 100.0 * int(n_nan_v) / int(total_v))
                )
            if "CLIP" in operations:
                print(
                    "flag_data (clip [%s, %s]): flagged %d / %d visibilities (%.2f%%)"
                    % (
                        clip_min,
                        clip_max,
                        int(n_clip_v),
                        int(total_v),
                        100.0 * int(n_clip_v) / int(total_v),
                    )
                )

    def summary(self):
        num_flagged = da.sum(self.ds.FLAG)
        rows_flagged = da.sum(self.ds.FLAG_ROW)
        total = da.prod(da.array(self.ds.FLAG.shape))
        rows_total = da.prod(da.array(self.ds.FLAG_ROW.shape))
        percent = 100.0 * (num_flagged / total)
        rows_percent = 100.0 * (rows_flagged / rows_total)

        # Histogram of unflagged visibilities per row
        n_unflagged = da.sum(da.logical_not(self.ds.FLAG.data), axis=(1, 2))
        n_flagged_per_row = da.sum(self.ds.FLAG.data, axis=(1, 2))
        max_per_row = da.prod(da.array(self.ds.FLAG.shape[1:]))
        frac = n_unflagged / max_per_row
        bins = [
            da.sum(frac == 0.0),
            da.sum((frac > 0.0) & (frac <= 0.25)),
            da.sum((frac > 0.25) & (frac <= 0.50)),
            da.sum((frac > 0.50) & (frac <= 0.75)),
            da.sum((frac > 0.75) & (frac < 1.0)),
            da.sum(frac == 1.0),
        ]
        min_unflagged = da.min(n_unflagged)
        max_unflagged = da.max(n_unflagged)
        min_flagged = da.min(n_flagged_per_row)
        max_flagged = da.max(n_flagged_per_row)
        total_per_row = n_unflagged + n_flagged_per_row
        min_total_per_row = da.min(total_per_row)
        max_total_per_row = da.max(total_per_row)

        # Derive UV coordinates from the live dataset so the UV
        # percentiles, max-uv, and integration-time limit reflect any
        # averaging/optimize that has run (self.u_arr/v_arr are the
        # original __init__ snapshot and go stale once rows change).
        uvw = self.ds["UVW"].data
        u_arr = uvw[:, 0]
        v_arr = uvw[:, 1]
        abs_uv = da.sqrt(u_arr * u_arr + v_arr * v_arr)
        percentile_inputs = [25, 33, 50, 75, 95, 100]
        percentile_values = da.percentile(abs_uv.flatten(), percentile_inputs)

        with ProgressBar():
            (
                percentile_values,
                num_flagged,
                rows_flagged,
                total,
                rows_total,
                percent,
                rows_percent,
                bins,
                min_unflagged,
                max_unflagged,
                min_flagged,
                max_flagged,
                max_per_row,
                min_total_per_row,
                max_total_per_row,
            ) = dask.compute(
                percentile_values,
                num_flagged,
                rows_flagged,
                total,
                rows_total,
                percent,
                rows_percent,
                bins,
                min_unflagged,
                max_unflagged,
                min_flagged,
                max_flagged,
                max_per_row,
                min_total_per_row,
                max_total_per_row,
            )

        print(f"Flagging Summary ({self.name}): {percent} % - {num_flagged}/{total}.")
        print(f"    flags: {percent:4.2f} % - {num_flagged}/{total}.")
        print(f"    rows: {rows_percent:4.2f} % - {rows_flagged}/{rows_total}.")
        print(f"    max-uv: {percentile_values[-1]:4.2f}")
        print("    UV-Percentiles: ")
        for p, v in zip(percentile_inputs, percentile_values):
            print(f"        {p:6f}: \t{v:7.2f}")
        print("    Row flagging histogram (% of visibilities unflagged):")
        labels = ["   0%", " 1-25%", "26-50%", "51-75%", "76-99%", "  100%"]
        for label, count in zip(labels, bins):
            pct = 100.0 * int(count) / int(rows_total) if int(rows_total) > 0 else 0.0
            bar = "#" * max(1, int(pct / 2))
            print(f"        {label}: {int(count):8d} ({pct:5.1f}%) {bar}")
        print(
            f"    Visibilities per row: {int(max_per_row)} total"
            f" (unflagged: min={int(min_unflagged)}, max={int(max_unflagged)};"
            f" flagged: min={int(min_flagged)}, max={int(max_flagged)})",
        )
        if int(min_total_per_row) == int(max_total_per_row):
            print(
                f"    Row size check: all rows consistent"
                f" ({int(min_total_per_row)} elements each)"
            )
        else:
            print(
                f"    Row size check: INCONSISTENT —"
                f" min={int(min_total_per_row)}, max={int(max_total_per_row)}"
            )
        if self.chan_freq_hz is not None:
            nchan = len(self.chan_freq_hz)
            fmin = self.chan_freq_hz[0] / 1e6
            fmax = self.chan_freq_hz[-1] / 1e6
            bw = fmax - fmin
            print(
                f"    Spectral windows: {self.nspw}"
                f" (channels: {nchan},"
                f" {fmin:.3f}–{fmax:.3f} MHz,"
                f" bandwidth: {bw:.1f} MHz)"
            )

            # Fringe-rotation integration time limit (Wijnholds 2018, MNRAS).
            # Time averaging causes decorrelation that depends on baseline
            # length, frequency, and angular distance ℓ from the phase center.
            # The amplitude loss factor is:
            #
            #   ρ = sinc(π · ω_⊕ · Δt · B · ν · ℓ / c)
            #
            # For small loss L = 1 − |ρ|:
            #
            #   Δt_max = c · √(6L) / (π · ω_⊕ · B_max · ν_max · ℓ)
            #
            c_ms = 299792458.0
            omega_earth = 7.2921150e-5
            max_uv = percentile_values[-1]
            nu_max = fmax * 1e6

            # Distance from phase centre in radians (converted from
            # --field-of-view degrees).  Default ℓ ≈ 0.0175 rad (1°).
            ell = getattr(self, "_fov_rad", 0.0174533)

            def dt_max(loss):
                if max_uv <= 0 or nu_max <= 0 or ell <= 0:
                    return float("inf")
                return (
                    c_ms
                    * (6.0 * loss) ** 0.5
                    / (3.14159 * omega_earth * max_uv * nu_max * ell)
                )

            print("    Max integration time (fringe-rotation lim., ℓ=%.2f rad):" % ell)
            print("        1%% loss:  %5.1f s" % dt_max(0.01))
            print("        3%% loss:  %5.1f s" % dt_max(0.03))
            print("        5%% loss:  %5.1f s" % dt_max(0.05))

            if "INTERVAL" in self.ds.data_vars:
                dt_current = float(self.ds.INTERVAL.data[0].compute())
                print("    Current integration time: %.1f s" % dt_current)
            elif "EXPOSURE" in self.ds.data_vars:
                dt_current = float(self.ds.EXPOSURE.data[0].compute())
                print("    Current integration time: %.1f s" % dt_current)

        # Field listing
        print("    Fields:")
        field_names = {}
        for s in self.sub_table_names:
            if s.endswith("/FIELD"):
                try:
                    ft = table(s, ack=False)
                    names = ft.getcol("NAME")
                    ft.close()
                    for i, name in enumerate(names):
                        field_names[i] = name.strip()
                except Exception:
                    pass

        # FIELD_ID may be a data variable (multi-field MS) or an
        # attribute (single-field MS).
        if "FIELD_ID" in self.ds.data_vars:
            field_ids = self.ds.FIELD_ID.data
            unique_ids = da.unique(field_ids).compute()
        else:
            unique_ids = [int(self.ds.attrs.get("FIELD_ID", 0))]

        for fid in sorted(unique_ids):
            if "FIELD_ID" in self.ds.data_vars:
                n = int(da.sum(field_ids == fid).compute())
            else:
                n = int(self.ds.FLAG.shape[0])
            name = field_names.get(int(fid), f"FIELD_ID={fid}")
            print(f"        {fid}: {name:20s} {n:8d} rows")

    def time_average(self, factor):
        """
        Average every <factor> consecutive rows into a single row.

        DATA, WEIGHT_SPECTRUM, and SIGMA_SPECTRUM average only unflagged
        visibilities (flagged entries are excluded from the mean / sum;
        WEIGHT_SPECTRUM is summed, SIGMA_SPECTRUM uses inverse-variance
        combining).  Per-row metadata (UVW, TIME, INTERVAL, EXPOSURE)
        excludes fully-flagged rows (FLAG_ROW True, or every visibility
        in FLAG True): UVW and TIME are masked means, INTERVAL and
        EXPOSURE are masked sums.  FLAG and FLAG_ROW are OR'd
        (any flagged -> flagged).  ANTENNA1/2 keep the first row of each
        group.
        """
        if factor < 2:
            return

        nrow = self.ds.FLAG.shape[0]
        if factor > nrow:
            print(
                f"Time-averaging: factor {factor} is larger than the"
                f" {nrow} rows in the MS — skipping"
            )
            return
        n_new = nrow // factor
        trim = n_new * factor

        print(
            "Time-averaging: factor %d"
            " → %d rows (discarding %d trailing rows)" % (factor, n_new, nrow - trim)
        )

        row_dim = self.ds.DATA.dims[0]

        # Step 1: compute averaged arrays from the ORIGINAL data.
        # Block reductions use dask.array.coarsen (via _block_reduce), which
        # averages every `factor` consecutive rows.  Unlike a manual reshape,
        # coarsen tolerates row-chunk sizes that are not divisible by `factor`
        # without fragmenting chunks, so it builds a far smaller task graph —
        # important for very large MSes.
        #
        # Hoist the FLAG / FLAG_ROW row-slices once: they are consumed by
        # several columns below, and a single slice keeps the task graph
        # small (each independent slice is a separate graph node over the
        # same underlying array).
        flag_trim = self.ds["FLAG"].data[:trim]
        flag_row_trim = self.ds["FLAG_ROW"].data[:trim]

        averaged = {}
        if "DATA" in self.ds.data_vars:
            d = self.ds["DATA"].data[:trim]
            d_masked = da.where(flag_trim, 0j, d)
            n_unflagged = _block_reduce(np.sum, (~flag_trim).astype(np.float64), factor, 0)
            n_safe = da.where(n_unflagged == 0, 1, n_unflagged)
            averaged["DATA"] = _block_reduce(np.sum, d_masked, factor, 0) / n_safe

        if "WEIGHT_SPECTRUM" in self.ds.data_vars:
            w = self.ds["WEIGHT_SPECTRUM"].data[:trim]
            # Weight = 1/σ². Combined weight = Σ wᵢ (sum of unflagged).
            w_masked = da.where(flag_trim, 0, w)
            averaged["WEIGHT_SPECTRUM"] = _block_reduce(np.sum, w_masked, factor, 0)

        if "SIGMA_SPECTRUM" in self.ds.data_vars:
            s = self.ds["SIGMA_SPECTRUM"].data[:trim]
            # σ̄ = 1 / √(Σ 1/σ²) — sum inverse variances, then invert.
            inv_var = da.where(flag_trim, 0, 1.0 / (s * s))
            sum_inv_var = _block_reduce(np.sum, inv_var, factor, 0)
            sum_safe = da.where(sum_inv_var == 0, 1, sum_inv_var)
            averaged["SIGMA_SPECTRUM"] = da.sqrt(1.0 / sum_safe)

        # Per-row metadata (UVW, TIME, INTERVAL, EXPOSURE) excludes
        # fully-flagged rows, using the same "row is bad" definition as
        # optimize(): FLAG_ROW True, or every visibility in FLAG True.
        # A partially-flagged row still carries a valid timestamp and
        # baseline, so it continues to contribute.  UVW/TIME are masked
        # means; INTERVAL/EXPOSURE are masked sums (combined exposure of
        # N integrations is the sum of the unflagged ones).
        row_bad = da.logical_or(
            flag_row_trim,
            da.all(flag_trim, axis=(1, 2)),
        )
        row_good = da.logical_not(row_bad).astype(np.float64)
        n_good = _block_reduce(np.sum, row_good, factor, 0)
        n_good_safe = da.where(n_good == 0, 1, n_good)

        for col in ("UVW", "TIME"):
            if col in self.ds.data_vars:
                v = self.ds[col].data[:trim]
                # Broadcast the (nrow,) good-mask against v's trailing dims.
                mask = da.reshape(
                    row_good, (row_good.shape[0],) + (1,) * (v.ndim - 1)
                )
                v_masked = v * mask
                summed = _block_reduce(np.sum, v_masked, factor, 0)
                # n_good is (n_new,); broadcast to summed's trailing dims.
                ng = da.reshape(
                    n_good_safe, (n_good_safe.shape[0],) + (1,) * (summed.ndim - 1)
                )
                averaged[col] = summed / ng

        for col in ("INTERVAL", "EXPOSURE"):
            if col in self.ds.data_vars:
                v = self.ds[col].data[:trim]
                mask = da.reshape(
                    row_good, (row_good.shape[0],) + (1,) * (v.ndim - 1)
                )
                averaged[col] = _block_reduce(np.sum, v * mask, factor, 0)

        averaged["FLAG"] = _block_reduce(np.any, flag_trim, factor, 0)
        averaged["FLAG_ROW"] = _block_reduce(np.any, flag_row_trim, factor, 0)

        # ANTENNA1/2 are constant within a group of consecutive rows;
        # keep the first of each group via strided indexing.
        for col in ("ANTENNA1", "ANTENNA2"):
            if col in self.ds.data_vars:
                averaged[col] = self.ds[col].data[0:trim:factor]

        # Step 2: subsample the dataset to keep every <factor>-th row.
        # isel gives consistent dimensions and chunking (no conflicts).
        keep_idx = np.arange(0, trim, factor)
        self.ds = self.ds.isel({row_dim: keep_idx})

        # Step 3: replace the averaged columns.  Since isel already
        # reduced all variables to n_new rows, per-column assignment
        # against the same row count is safe.
        # Rechunk to match the existing row chunking from isel.
        row_chunks = self.ds.chunks.get(row_dim, None)
        for col, arr in averaged.items():
            if row_chunks is not None and len(arr.shape) >= 1:
                chunks = list(arr.chunks)
                chunks[0] = row_chunks
                arr = arr.rechunk(tuple(chunks))
            self.ds[col] = (self.ds[col].dims, arr)
            self.changed[col] = True

    def frequency_average(self, factor):
        """
        Average every <factor> consecutive frequency channels into one.

        DATA, WEIGHT_SPECTRUM, and SIGMA_SPECTRUM average only unflagged
        visibilities (flagged entries are excluded; WEIGHT_SPECTRUM is
        summed, SIGMA_SPECTRUM uses inverse-variance combining).
        FLAG is OR'd (any flagged -> flagged).
        Trailing channels (fewer than <factor>) are combined into a
        single narrower channel rather than discarded.
        """
        if factor < 2:
            return

        nchan = self.ds.FLAG.shape[1]
        if factor > nchan:
            print(
                f"Frequency-averaging: factor {factor} is larger than the"
                f" {nchan} channels in the MS — skipping"
            )
            return
        n_full = nchan // factor
        n_rem = nchan % factor
        n_new = n_full + (1 if n_rem > 0 else 0)
        trim = n_full * factor

        if n_full == 0:
            # Factor larger than the channel count: nothing to average
            # (e.g. a single-channel MS).  Leave the data untouched.
            print(
                "Frequency-averaging: factor %d >= %d channels, nothing to do"
                % (factor, nchan)
            )
            return

        msg = "Frequency-averaging: factor %d → %d channels" % (factor, n_new)
        if n_rem > 0:
            msg += " (last channel from %d trailing)" % n_rem
        print(msg)

        chan_dim = self.ds.DATA.dims[1]

        # --- Compute averaged arrays ---
        # Block reductions use _block_reduce (dask.array.coarsen) over the
        # channel axis.  Unlike a manual reshape it tolerates channel-chunk
        # sizes that are not divisible by `factor` without fragmenting the
        # chunk grid, giving a much smaller task graph for large MSes.
        # Trailing channels (n_rem) are averaged separately and appended.
        averaged = {}

        # DATA: masked mean (exclude flagged)
        if "DATA" in self.ds.data_vars:
            d = self.ds["DATA"].data[:, :trim, :]
            f = self.ds["FLAG"].data[:, :trim, :]
            d_masked = da.where(f, 0j, d)
            n_unf = _block_reduce(np.sum, (~f).astype(np.float64), factor, 1)
            n_safe = da.where(n_unf == 0, 1, n_unf)
            avg = _block_reduce(np.sum, d_masked, factor, 1) / n_safe
            if n_rem > 0:
                d_rem = self.ds["DATA"].data[:, trim:, :]
                f_rem = self.ds["FLAG"].data[:, trim:, :]
                d_rem_m = da.where(f_rem, 0j, d_rem)
                n_unf_r = da.sum(da.logical_not(f_rem), axis=1)
                n_safe_r = da.where(n_unf_r == 0, 1, n_unf_r)
                avg_rem = (da.sum(d_rem_m, axis=1) / n_safe_r)[:, None, :]
                avg = da.concatenate([avg, avg_rem], axis=1)
            averaged["DATA"] = avg

        # WEIGHT_SPECTRUM: sum of unflagged weights (w = 1/σ², Σ w)
        if "WEIGHT_SPECTRUM" in self.ds.data_vars:
            s = self.ds["WEIGHT_SPECTRUM"].data[:, :trim, :]
            f = self.ds["FLAG"].data[:, :trim, :]
            s_masked = da.where(f, 0, s)
            avg = _block_reduce(np.sum, s_masked, factor, 1)
            if n_rem > 0:
                s_rem = self.ds["WEIGHT_SPECTRUM"].data[:, trim:, :]
                f_rem = self.ds["FLAG"].data[:, trim:, :]
                s_rem_m = da.where(f_rem, 0, s_rem)
                avg_rem = da.sum(s_rem_m, axis=1, keepdims=True)
                avg = da.concatenate([avg, avg_rem], axis=1)
            averaged["WEIGHT_SPECTRUM"] = avg

        # SIGMA_SPECTRUM: σ̄ = 1 / √(Σ 1/σ²)
        if "SIGMA_SPECTRUM" in self.ds.data_vars:
            s = self.ds["SIGMA_SPECTRUM"].data[:, :trim, :]
            f = self.ds["FLAG"].data[:, :trim, :]
            inv_var = da.where(f, 0, 1.0 / (s * s))
            sum_inv = _block_reduce(np.sum, inv_var, factor, 1)
            sum_safe = da.where(sum_inv == 0, 1, sum_inv)
            avg = da.sqrt(1.0 / sum_safe)
            if n_rem > 0:
                s_rem = self.ds["SIGMA_SPECTRUM"].data[:, trim:, :]
                f_rem = self.ds["FLAG"].data[:, trim:, :]
                inv_var_r = da.where(f_rem, 0, 1.0 / (s_rem * s_rem))
                sum_inv_r = da.sum(inv_var_r, axis=1, keepdims=True)
                sum_safe_r = da.where(sum_inv_r == 0, 1, sum_inv_r)
                avg_rem = da.sqrt(1.0 / sum_safe_r)
                avg = da.concatenate([avg, avg_rem], axis=1)
            averaged["SIGMA_SPECTRUM"] = avg

        if "FLAG" in self.ds.data_vars:
            f = self.ds["FLAG"].data[:, :trim, :]
            avg = _block_reduce(np.any, f, factor, 1)
            if n_rem > 0:
                f_rem = self.ds["FLAG"].data[:, trim:, :]
                avg_rem = da.any(f_rem, axis=1, keepdims=True)
                avg = da.concatenate([avg, avg_rem], axis=1)
            averaged["FLAG"] = avg

        # --- Apply to dataset ---
        keep_idx = np.arange(0, trim, factor)
        if n_rem > 0:
            keep_idx = np.append(keep_idx, trim)  # one extra for tail
        self.ds = self.ds.isel({chan_dim: keep_idx})

        chan_chunks = self.ds.chunks.get(chan_dim, None)
        for col, arr in averaged.items():
            if chan_chunks is not None:
                new_chunks = list(arr.chunks)
                new_chunks[1] = chan_chunks
                arr = arr.rechunk(tuple(new_chunks))
            self.ds[col] = (self.ds[col].dims, arr)
            self.changed[col] = True

        # Update the SPECTRAL_WINDOW CHAN_FREQ to match
        if self.chan_freq_hz is not None:
            freq_reshaped = self.chan_freq_hz[:trim].reshape(n_full, factor)
            avg_freq = np.mean(freq_reshaped, axis=1)
            if n_rem > 0:
                avg_rem = np.mean(self.chan_freq_hz[trim:])
                avg_freq = np.append(avg_freq, avg_rem)
            self.chan_freq_hz = avg_freq

        # ...and the per-channel width columns (CHAN_WIDTH, EFFECTIVE_BW,
        # RESOLUTION), which add up within a group the same way the frequency
        # span does.  Missing one leaves the subtable describing the input
        # channel count, which readers reject.
        for col, values in list((self.chan_axis_hz or {}).items()):
            if values is None:
                continue
            width_reshaped = values[:trim].reshape(n_full, factor)
            avg_width = np.sum(width_reshaped, axis=1)
            if n_rem > 0:
                avg_width = np.append(avg_width, np.sum(values[trim:]))
            self.chan_axis_hz[col] = avg_width

    def optimize(self):
        """
        Run through the flags, and remove all completely flagged rows
        and channels.

        A row is removed if either:
        - FLAG_ROW is True (explicitly marked as bad), or
        - Every individual visibility in FLAG is True (all channels ×
          correlations flagged).

        A channel is removed if all rows and all correlations are flagged
        for that channel (e.g. after flag_spectral_window).
        """
        print("Remove all flagged rows and channels...")

        # Rows explicitly flagged via FLAG_ROW
        row_flagged = self.ds["FLAG_ROW"].data  # (nrow,)

        # Rows where every single visibility is individually flagged.
        # FLAG shape: (nrow, nchan, ncorr) → all over chan (axis=1) and corr (axis=2) → (nrow,)
        all_data_flagged = da.all(self.ds["FLAG"].data, axis=(1, 2))

        # Channels where all rows and correlations are flagged.
        # FLAG shape: (nrow, nchan, ncorr) → all over row (axis=0) and corr (axis=2) → (nchan,)
        chan_fully_flagged = da.all(self.ds["FLAG"].data, axis=(0, 2))

        # Combined: a row is removed if EITHER condition is true
        is_flagged = da.logical_or(row_flagged, all_data_flagged)
        unflagged_rows = da.logical_not(is_flagged)
        keep_channels = da.logical_not(chan_fully_flagged)

        # Compute all statistics in one pass
        n_overlap = da.sum(da.logical_and(row_flagged, all_data_flagged))
        (
            n_total,
            n_row_flagged,
            n_all_data_flagged,
            n_combined,
            n_unflagged,
            n_overlap,
            n_chan_total,
            n_chan_flagged,
        ) = dask.compute(
            row_flagged.size,
            da.sum(row_flagged),
            da.sum(all_data_flagged),
            da.sum(is_flagged),
            da.sum(unflagged_rows),
            n_overlap,
            chan_fully_flagged.size,
            da.sum(chan_fully_flagged),
        )

        n_extra = int(n_all_data_flagged) - int(n_overlap)

        print(f"Total rows:           {int(n_total):8d}")
        print(f"  FLAG_ROW flagged:   {int(n_row_flagged):8d}")
        print(f"  All-data-flagged:   {int(n_all_data_flagged):8d}")
        print(f"  Combined to remove: {int(n_combined):8d}")
        print(f"  Remaining:          {int(n_unflagged):8d}")
        print(f"  Extra rows caught by all(FLAG) check: {n_extra}")
        print(f"  Fully-flagged channels: {int(n_chan_flagged)} / {int(n_chan_total)}")

        if int(n_unflagged) == 0:
            raise RuntimeError(
                "No unflagged rows remain after optimize — nothing to write"
            )

        if int(n_chan_flagged) == int(n_chan_total):
            raise RuntimeError("All channels fully flagged — nothing to write")

        # Find dimension names from DATA (typically "row", "chan")
        row_dim = self.ds.DATA.dims[0]
        chan_dim = self.ds.DATA.dims[1]

        # Build indexers for rows and channels.  Compute both masks in a
        # SINGLE dask pass (the channel mask is only needed when at least
        # one channel is fully flagged).
        if int(n_chan_flagged) > 0:
            keep_row_mask, keep_chan_mask = dask.compute(
                unflagged_rows, keep_channels
            )
            keep_row_idx = np.nonzero(keep_row_mask)[0]
            keep_chan_idx = np.nonzero(keep_chan_mask)[0]
            isel_indexers = {row_dim: keep_row_idx, chan_dim: keep_chan_idx}
        else:
            keep_row_mask = unflagged_rows.compute()
            keep_row_idx = np.nonzero(keep_row_mask)[0]
            isel_indexers = {row_dim: keep_row_idx}

        self.ds = self.ds.isel(isel_indexers)

        # Removing channels must also drop them from the SPECTRAL_WINDOW
        # bookkeeping, otherwise the subtable written by write_new_ms() would
        # describe channels the data no longer has.
        if int(n_chan_flagged) > 0:
            if self.chan_freq_hz is not None:
                self.chan_freq_hz = self.chan_freq_hz[keep_chan_idx]
            for col, values in list((self.chan_axis_hz or {}).items()):
                if values is not None:
                    self.chan_axis_hz[col] = values[keep_chan_idx]
        self._refresh_cached_columns()

        # Mark all changed variables
        for var_name in self.ds.data_vars:
            if row_dim in self.ds[var_name].dims:
                self.changed[var_name] = True

        print(
            f"Optimize complete."
            f" Rows: {int(n_unflagged)}, Channels: {int(n_chan_total) - int(n_chan_flagged)}"
        )

    def _resolve_field_id(self, field):
        """Resolve a field name or numeric id to a FIELD_ID integer.

        A purely numeric spec is treated as a FIELD_ID; anything else is
        matched against the NAME column of the FIELD subtable.
        """
        field_names = {}
        for s in self.sub_table_names:
            if s.endswith("/FIELD"):
                try:
                    ft = table(s, ack=False)
                    names = ft.getcol("NAME")
                    ft.close()
                    for i, name in enumerate(names):
                        field_names[i] = name.strip()
                except Exception:
                    pass

        try:
            return int(field)
        except (ValueError, TypeError):
            pass

        spec = str(field).strip()
        for i, name in field_names.items():
            if name == spec:
                return i
        available = ", ".join(
            f"{i}: {n!r}" for i, n in sorted(field_names.items())
        )
        raise RuntimeError(
            f"Field {field!r} not found. Available fields: {available}"
        )

    def _select_field(self, ds, field):
        """Return ``ds`` reduced to the rows of a single field.

        ``field`` may be a numeric FIELD_ID or a field NAME.  Only the
        row dimension is filtered; every column (DATA, FLAG, UVW, ...)
        is carried over unchanged.
        """
        field_id = self._resolve_field_id(field)

        # Multi-field MS: FIELD_ID is a per-row data variable.
        if "FIELD_ID" in ds.data_vars:
            mask = ds.FIELD_ID.data == field_id
            indices = da.nonzero(mask)[0].compute()
            if indices.size == 0:
                raise RuntimeError(
                    f"--split: no rows with FIELD_ID={field_id} ({field!r})"
                )
            row_dim = ds.DATA.dims[0]
            print(
                f"--split: selected field {field!r} (FIELD_ID={field_id}),"
                f" {indices.size} rows"
            )
            return ds.isel({row_dim: indices})

        # Single-field MS: FIELD_ID is stored as a dataset attribute.
        attr_id = int(ds.attrs.get("FIELD_ID", 0))
        if attr_id != field_id:
            raise RuntimeError(
                f"--split: MS only contains FIELD_ID={attr_id},"
                f" cannot select {field_id} ({field!r})"
            )
        print(f"--split: single-field MS (FIELD_ID={field_id}), no rows removed")
        return ds

    def write_new_ms(self, name, clobber, split=None):
        """
        Write a new MS, and make sure it doesn't already exist.

        If ``split`` is given (a field name or FIELD_ID), only that
        field's rows are written to the output MS.
        """
        ds_to_write = self.ds
        if split is not None:
            ds_to_write = self._select_field(ds_to_write, split)

        all_tables = list(ds_to_write.keys())
        print(f"Writing {all_tables} to {name}")

        if os.path.exists(name):
            if not clobber:
                raise RuntimeError(
                    f"Measurement set {name} already exists. Use --clobber to overwrite"
                )
            logger.warning(f"Overwriting {name}")
            shutil.rmtree(name)

        writes = xds_to_table(ds_to_write, name, "ALL")

        with ProgressBar():
            dask.compute(writes)

        # Copy subtables (SPECTRAL_WINDOW, ANTENNA, FIELD, etc.) from
        # the input MS.  xds_to_table only writes the main table.
        # Suppress casacore C++ stderr noise (SORT_COLUMNS etc.) unless
        # --debug is set.
        with _maybe_quiet_stderr():
            for sub in self.sub_table_names:
                sub_name = os.path.basename(sub)
                dest = os.path.join(name, sub_name)
                if os.path.exists(dest):
                    shutil.rmtree(dest)
                t = table(sub, ack=False)
                t.copy(dest, deep=True)
                t.close()
                logger.debug("  copied subtable %s", sub_name)

            # The main table that dask-ms created may not carry every keyword
            # of the input -- notably SOURCE, which links the MS to its SOURCE
            # subtable.  Without that link the copied subtable is invisible
            # (getsubtables() omits it) and CASA's Calibrater dies with
            # "NullTable::lock - Table object is empty".  Copy what is missing,
            # repointing subtable links at the newly written MS.
            self._copy_missing_keywords(name)

        # If channels were reduced (frequency_average or optimize), update
        # the SPECTRAL_WINDOW columns in the output MS.  The subtable was
        # copied verbatim from the input, so it still describes the input
        # channel setup until this rewrite happens.
        if self.chan_freq_hz is not None:
            nchan_in_ds = self.ds.FLAG.shape[1]
            if len(self.chan_freq_hz) != nchan_in_ds:
                raise RuntimeError(
                    "SPECTRAL_WINDOW bookkeeping error:"
                    f" {len(self.chan_freq_hz)} channel frequencies for"
                    f" {nchan_in_ds} data channels in {name}"
                )
            if self.spw_chan_count != nchan_in_ds:
                self._rewrite_spw_channels(name, nchan_in_ds)

    def _copy_missing_keywords(self, name):
        """Copy main-table keywords the written MS is missing.

        A measurement set's sub-tables are reached through keywords on the main
        table (``SOURCE``, ``FIELD``, ... each holding ``Table: <path>``).  The
        table dask-ms writes carries most of them but not, for instance,
        ``SOURCE`` -- so a SOURCE sub-table copied alongside is orphaned and
        CASA cannot open the MS for calibration.  Sub-table links are rewritten
        to point inside the newly written MS; other keywords are copied
        verbatim.
        """
        src = table(self.name, ack=False)
        try:
            src_keywords = set(src.getkeywords())
            missing = src_keywords - set(table(name, ack=False).getkeywords())
            if not missing:
                return
            dest = table(name, readonly=False)
            try:
                # Keywords need an explicit user lock, unlike putcol().
                dest.lock()
                for key in sorted(missing):
                    value = src.getkeyword(key)
                    if isinstance(value, str) and value.startswith("Table:"):
                        sub = value.split(":", 1)[1].strip()
                        value = "Table: " + os.path.join(
                            os.path.abspath(name), os.path.basename(sub)
                        )
                    dest.putkeyword(key, value)
                    logger.debug("  copied keyword %s", key)
            finally:
                dest.unlock()
                dest.close()
            print(f"Copied {len(missing)} missing keyword(s) to {name}: {sorted(missing)}")
        finally:
            src.close()

    def _rewrite_spw_channels(self, name, nchan):
        """Rewrite the per-channel SPECTRAL_WINDOW columns of a written MS.

        Channel frequencies and every per-channel width column are taken from
        the live bookkeeping, so the subtable matches the main table after
        frequency averaging or channel removal.
        """
        if self.nspw > 1:
            raise RuntimeError(
                f"Cannot rewrite SPECTRAL_WINDOW for {self.name}: it has"
                f" {self.nspw} spectral windows, but only single-SPW"
                " measurement sets are supported"
            )

        updates = spw_column_updates(nchan, self.chan_freq_hz, self.chan_axis_hz)

        with _maybe_quiet_stderr():
            for sub in self.sub_table_names:
                if "SPECTRAL_WINDOW" not in sub:
                    continue
                dest = os.path.join(name, os.path.basename(sub))
                sw = table(dest, ack=False, readonly=False)
                try:
                    columns = set(sw.colnames())
                    for col, value in updates.items():
                        if col in columns:
                            sw.putcol(col, value)
                        else:
                            logger.warning(
                                "SPECTRAL_WINDOW has no %s column; skipping", col
                            )
                    # Safety net: every per-channel column must now have the
                    # new channel count.  A stale one (the CHAN_WIDTH bug that
                    # made dask-ms report "conflicting sizes for dimension
                    # 'chan'") is an error, not something to leave on disk.
                    stale = []
                    for col in sw.colnames():
                        if col in updates:
                            continue
                        try:
                            arr = np.asarray(sw.getcol(col))
                        except Exception:
                            continue
                        if arr.ndim >= 2 and arr.shape[-1] == self.spw_chan_count:
                            stale.append(col)
                    if stale:
                        raise RuntimeError(
                            "SPECTRAL_WINDOW still describes"
                            f" {self.spw_chan_count} channels in {stale} after"
                            f" rewriting to {nchan} channels; refusing to leave"
                            f" an inconsistent subtable in {dest}"
                        )
                finally:
                    sw.close()
                print(
                    f"SPECTRAL_WINDOW: rewrote {sorted(set(updates) & columns)}"
                    f" for {nchan} channels in {name}"
                )
                break

    def update_ms(self, name, clobber):
        """
        Update MS in place
        """
        if not clobber:
            raise RuntimeError(
                f"Measurement set {name} can't be changed. Use --clobber to overwrite"
            )
        logger.warning(f"Updating {name}")

        for to_update in self.changed.keys():
            if self.changed[to_update]:
                print(f"Updating table: {to_update} in {name}")
                logger.debug(f"    ds={self.ds[to_update]}")
                writes = xds_to_table(self.ds, f"{name}", to_update)
                with ProgressBar():
                    dask.compute(writes)

                self.changed[to_update] = False
