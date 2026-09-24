# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import logging
import math
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
from dask import delayed
from dask.diagnostics import ProgressBar
from daskms import xds_from_ms, xds_to_table

from skarabina import flag_versions
from skarabina.rflag import rflag_plane
from skarabina.tfcrop import tfcrop_plane

logger = logging.getLogger(__name__)


def _autofit_block(plane_function, data, existing, params):
    """Run a plane-wise auto-flagger over one ``(time, chan, corr)`` block.

    Both auto-flagging algorithms work on a single baseline and correlation.
    Which correlation a plane belongs to is a bookkeeping detail of how the MS
    stores its data -- the polarisation products are flagged independently,
    exactly as CASA does -- so the loop lives here rather than in the algorithm.
    """
    planes = []
    for corr in range(data.shape[2]):
        flag, _ = plane_function(data[:, :, corr], params, existing[:, :, corr])
        planes.append(flag)
    return np.stack(planes, axis=2)


def _tfcrop_block(amplitude, existing, params):
    """TFCrop over one block; see :func:`_autofit_block`."""
    return _autofit_block(tfcrop_plane, amplitude, existing, params)


def _rflag_block(data, existing, params):
    """RFlag over one block; see :func:`_autofit_block`."""
    return _autofit_block(rflag_plane, data, existing, params)


def _prepare_block(block_function):
    """The block function for a verb, with the conversion that verb needs.

    TFCrop fits the amplitude plane; RFlag measures the scatter of the real and
    imaginary parts, so it needs the complex visibilities as they are.  Doing
    the conversion here keeps it in the same deferred call as the algorithm.
    """
    def run(data, existing, params):
        if block_function is _tfcrop_block:
            data = np.absolute(data)
        return block_function(data, existing, params)
    return run


def _flags_and_counts(block_function):
    """Wrap a block function so one call yields flags and both flag counts.

    The new count and the pre-existing count are reductions of arrays that
    already exist at that point, so returning them here costs nothing and saves
    a second traversal -- which, on a graph that is not persisted, means saving
    a second evaluation of the whole block.
    """
    def run(data, existing, params):
        flags = block_function(data, existing, params)
        return flags, int(np.count_nonzero(flags)), int(np.count_nonzero(existing))
    return run


def _parameter_summary(params):
    """A one-line description of an auto-flagging run, for the console.

    Only the parameters that differ from CASA's defaults are named, so a default
    run prints just its name and an unusual one is obvious at a glance.
    """
    deviations = [
        "%s=%s" % (name, getattr(params, name))
        for name in sorted(params.DEFAULTS)
        if getattr(params, name) != params.DEFAULTS[name]
    ]
    return ", ".join(deviations) if deviations else "CASA defaults"


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


def _ensure_writable(path):
    """Give the owner write permission on ``path``, changing no other bit.

    Every block ``_write_changed_only`` copies into its output is the output's
    own file -- only the *linked* ones are shared with the input -- so it has to
    be writable, or the column write that follows is refused by the filesystem.
    ``shutil.copy2`` preserves the mode, and an input whose blocks an earlier
    sharing run made read-only is exactly the case that produced
    ``storage error: Permission denied`` (issue #3): the first flagging run
    worked, the second one could not.
    """
    try:
        mode = os.stat(path).st_mode
        if not mode & 0o200:
            os.chmod(path, mode | 0o200)
    except OSError:
        pass


# Single source of truth for time-average smearing: both the flagger's summary
# and skarabina-analyze quote the same limit, so the constant and the loss
# criterion live here and nowhere else.
C_MS = 299792458.0
OMEGA_EARTH = 7.2921150e-5
# Default criterion for the reported limit: the longest integration whose
# time-average smearing costs at most this fraction of the amplitude at the
# edge of the field.  Expressed as a loss, not as a sinc argument, so the
# number means what its label says.
TIME_AVERAGE_LOSS = 0.10


def time_average_smearing_loss(dt_s, nu_hz, uv_m, theta_rad):
    """Fractional amplitude lost to time-average smearing.

    A visibility at angular distance ``theta_rad`` from the phase centre has
    delay ``τ = B·θ/c``; the Earth's rotation sweeps θ during an integration, so
    the residual phase sweeps by ``2π·x`` with

        x = ω_⊕ · Δt · B · ν · θ / c

    Averaging the phasor over the integration leaves the fringe-washing factor

        ρ = sinc(π·x) = sin(πx)/(πx)

    and this returns the loss ``1 − ρ``.  (Checked against a direct numerical
    average of the phasor in real units: the two agree to ~1e-6 over
    0 < x < 0.7, the range these limits live in.)  Inverse of
    :func:`time_average_loss_to_dt`; keep the two in step.
    """
    x = OMEGA_EARTH * float(dt_s) * float(uv_m) * float(nu_hz) * float(theta_rad) / C_MS
    if x == 0.0:
        return 0.0
    return 1.0 - math.sin(math.pi * x) / (math.pi * x)


def time_average_loss_to_dt(loss, nu_hz, uv_m, theta_rad):
    """Longest integration whose smearing loss stays at or below ``loss``.

    The inverse of :func:`time_average_smearing_loss`: solve
    ``sinc(π·x) = 1 − L`` for the first crossing and return the corresponding
    Δt.  Inverting the relation exactly rather than through the small-angle
    form keeps the criterion meaning what it says at any loss: the usual
    ``x ≈ √(6L)/π`` is 1.5% low at L = 0.1 and 3.7% low at L = 0.2.
    """
    loss = float(loss)
    if uv_m <= 0 or nu_hz <= 0 or theta_rad <= 0:
        return float("inf")
    if loss <= 0.0:
        return 0.0
    if loss >= 1.0:
        return float("inf")

    # First positive root of sinc(pi*x) = 1 - loss, by bisection on (0, 1).
    target = 1.0 - loss
    lo, hi = 1e-12, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if math.sin(math.pi * mid) / (math.pi * mid) > target:
            lo = mid
        else:
            hi = mid
    x = 0.5 * (lo + hi)

    return x * C_MS / (OMEGA_EARTH * float(uv_m) * float(nu_hz) * float(theta_rad))


def max_integration_time(nu_max_hz, uv_max_m, fov_rad, loss=0.01):
    """Fringe-rotation integration-time limit for a full-width field of view.

    Time averaging decorrelates the visibilities.  Following Wijnholds (2018,
    MNRAS), the amplitude loss at angular distance ℓ from the phase centre is

        ρ = sinc(π · ω_⊕ · Δt · B · ν · ℓ / c)

    and this returns the longest Δt whose loss stays at or below ``loss``, i.e.
    the inverse of that relation.  For small losses it approaches the familiar

        Δt_max = c · √(6L) / (π · ω_⊕ · B_max · ν_max · ℓ)

    which is what earlier versions used; the exact inverse is 1.8% longer at
    L = 0.01 and 15.5% longer at L = 0.1, and the small-angle value is the more
    conservative of the two.

    ``fov_rad`` is the **full width** of the field of view, so ℓ -- the distance
    from the phase centre to its edge -- is half of it.  This is the same
    convention as ``skarabina-analyze --image-fov``.  Degenerate inputs return
    infinity.
    """
    return time_average_loss_to_dt(
        loss, nu_max_hz, uv_max_m, float(fov_rad) / 2.0
    )


# An integration whose rows are stamped with several TIME values, all within
# this fraction of the observing cadence, is treated as one integration.  The
# offset a writer introduces is far smaller than the gap between genuine
# integrations, so the split is unambiguous (see group_integrations).
INTEGRATION_TOLERANCE = 0.5


def group_integrations(time, tol_fraction=INTEGRATION_TOLERANCE):
    """Group rows of a measurement set into logical integrations.

    The obvious grouping -- start a new integration whenever ``TIME`` changes --
    is unreliable.  Some MS writers stamp a *single* integration's rows with
    more than one TIME value, splitting one integration into two partial
    groups.  Consumers then report the observation as having incomplete
    integration groups ("1695 rows where 1711 are needed for 58 antennas"),
    even though no data is missing: the parts hold disjoint baseline sets that
    together are the whole integration.

    On a MeerKAT MT0 file, 4 of 440 integrations were split this way, with
    offsets of exactly 1.000 s and part sizes of 333+1378, 1480+231, 1539+172
    and 1276+435 rows -- each pair summing to the full 1711 baselines.

    This grouping is gap-tolerant: the cadence is estimated from the median
    spacing of distinct TIME values, and a group is extended across any TIME
    change closer than ``tol_fraction`` of that cadence.  Intra-integration
    offsets are small compared with the cadence, so the parts are re-united
    while genuine integrations (one cadence apart) stay separate.

    Args:
        time: per-row TIME values (array-like), e.g. ``ds.TIME.data``.
        tol_fraction: split tolerance as a fraction of the estimated cadence.

    Returns:
        list of ``(start, end)`` index pairs, one per integration, in the order
        the rows appear in the input.
    """
    time = np.asarray(time, dtype=float)
    nrow = len(time)
    if nrow == 0:
        return []

    # Runs of *identical* TIME: the naive grouping before tolerance is applied.
    changes = np.nonzero(time[1:] != time[:-1])[0] + 1
    starts = np.concatenate(([0], changes))
    ends = np.concatenate((changes, [nrow]))

    # cadence = typical spacing between consecutive TIME values
    cadence = float(np.median(np.diff(time[starts]))) if len(starts) > 1 else 0.0
    tol = tol_fraction * cadence

    groups = []
    group_start = starts[0]
    for start, end in zip(starts[1:], ends[1:]):
        # Extend the current group across a TIME change that is merely a
        # re-stamping artefact; a gap of a full cadence starts a new one.
        if time[start] - time[group_start] < tol:
            continue
        groups.append((group_start, start))
        group_start = start
    groups.append((group_start, nrow))
    return groups


def integration_interval(interval, time=None):
    """The nominal, per-integration interval of an MS.

    ``summary()`` reports a single "current integration time".  Taking it from
    the first row's INTERVAL (or EXPOSURE) is fragile: if the writer split an
    integration, the first row may carry a shortened interval -- 5.997 s
    instead of the nominal 7.997 s on the MT0 file above -- so the reported
    figure understates the integration time and makes the fringe-rotation
    comparison misleading.  The most common value is the nominal one.

    Falls back to the median spacing between distinct TIME values when no
    interval column is available, and returns ``None`` if neither can be used.
    """
    if interval is not None:
        values = np.round(np.asarray(interval, dtype=float), 6)
        if values.size:
            counts = np.bincount(
                np.unique(values, return_inverse=True)[1], minlength=len(values)
            )
            return float(values[int(np.argmax(counts))])

    if time is not None:
        time = np.asarray(time, dtype=float)
        starts = np.concatenate(([0], np.nonzero(time[1:] != time[:-1])[0] + 1))
        if len(starts) > 1:
            return float(np.median(np.diff(time[starts])))
    return None


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

    def flag_autocorrelations(self):
        """Flag autocorrelation visibilities (``ANTENNA1 == ANTENNA2``).

        Auto baselines measure the total power of a single antenna: they carry
        no fringe information, so they are useless for imaging and are normally
        excluded from calibration.  Every visibility of an auto baseline is
        flagged, and ``FLAG_ROW`` is set for its row so that ``--optimize`` can
        drop the rows entirely.
        """
        for col in ("ANTENNA1", "ANTENNA2"):
            if col not in self.ds.data_vars:
                raise RuntimeError(
                    f"MS has no {col} column — cannot identify autocorrelations"
                )

        auto_row = da.asarray(self.ds.ANTENNA1.data) == da.asarray(
            self.ds.ANTENNA2.data
        )
        flags = self.ds.FLAG.data
        auto_flags = da.broadcast_to(auto_row[:, None, None], flags.shape)

        new_flags = da.logical_or(flags, auto_flags)
        new_flag_row = da.logical_or(self.ds.FLAG_ROW.data, auto_row)

        n_auto_rows, n_auto_vis, n_vis, n_flag_rows = dask.compute(
            da.sum(auto_row),
            da.sum(auto_flags),
            da.prod(da.array(flags.shape)),
            da.sum(new_flag_row),
        )

        self.ds["FLAG"].data = new_flags
        self.ds["FLAG_ROW"] = (self.ds.FLAG_ROW.dims, new_flag_row)
        self.changed["FLAG"] = True
        self.changed["FLAG_ROW"] = True

        print(
            "flag_autocorrelations: %d auto-baseline rows, %d visibilities"
            " flagged (%.2f%% of all); %d rows now flagged in total"
            % (
                int(n_auto_rows),
                int(n_auto_vis),
                100.0 * int(n_auto_vis) / int(n_vis) if int(n_vis) else 0.0,
                int(n_flag_rows),
            )
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

    #: Rough cap on the floating-point values a single tfcrop block may hold.
    #: The block is the unit the algorithm fits over, so it has to be big enough
    #: for a meaningful bandpass and small enough to hold in memory: 8M values is
    #: 64 MB of float64, or 128 MB once the amplitude and its flags are both
    #: live, which is a reasonable working set per dask worker.
    AUTOFIT_BLOCK_VALUES = 8_000_000

    def _run_autofit(self, params, plane_function, block_function, label):
        """Chunk the cube along time and run a plane-wise flagger over it.

        Shared by ``tfcrop`` and ``rflag``: both take the whole band and every
        correlation as the unit they work on, and neither can be vectorised
        across those axes, so both need the same chunking and reassembly.
        """
        import dask.array as _da

        print("%s: %s" % (label, _parameter_summary(params)))

        shape = self.ds.DATA.shape
        n_time, n_chan, n_corr = shape
        # The cube keeps the MS's own axis order, (time, chan, corr), and only
        # the *time* axis is chunked: a plane needs the whole band and every
        # correlation, so both are kept whole.  Getting this wrong is expensive
        # and silent -- transposing the chan and corr axes here made each block a
        # plane of two channels, and the fit then flagged every visibility in
        # the MS, 100 %, from a change that looked like a tidy-up.
        chunk = max(1, self.AUTOFIT_BLOCK_VALUES // max(1, n_chan * n_corr))
        chunk = min(chunk, max(1, n_time))

        def cube(array):
            """The data cube as (time, chan, corr), chunked along time.

            The band and correlation axes must each be a single chunk, since a
            plane needs both whole.  When the data already arrives that way --
            the usual case for a measurement set, where dask-ms reads a column
            as one array per data description -- the existing graph is used as
            it is.  Re-chunking would be a no-op in meaning but a real pass over
            the data in time, and on a 340k-row MS that pass is most of the run.
            """
            data = _da.asarray(array)
            if data.chunks[1:] == ((n_chan,), (n_corr,)):
                return data
            return _da.rechunk(data, (chunk, n_chan, n_corr))

        payload = cube(self.ds.DATA.data)
        existing = cube(self.ds.FLAG.data)

        # One ``delayed`` call per block.  The blocks are the expensive part,
        # and they must run exactly once: ``delayed`` results are not cached
        # between ``compute`` calls, so forcing the flag counts here and then
        # letting the write's ``compute`` touch the same blocks runs the whole
        # algorithm again.  ``persist`` materialises them once and keeps the
        # results in memory, so the report counts AND the later write (and any
        # downstream computation combined into the same dask graph) share that
        # single evaluation.
        blocks = []
        for time_index in range(payload.numblocks[0]):
            # TFCrop works on amplitudes and RFlag on the complex visibilities,
            # so each block converts its own; the conversion is deferred with
            # the rest of the block to keep it inside the per-block working set.
            block = delayed(_flags_and_counts(_prepare_block(block_function)))(
                payload.blocks[time_index], existing.blocks[time_index], params
            )
            blocks.append(
                _da.from_delayed(
                    block[0], shape=payload.blocks[time_index].shape, dtype=bool
                )
            )
        new_flags = _da.concatenate(blocks, axis=0).persist()

        # The counts are reductions of materialised data now: the newly
        # flagged count comes from the persisted flags, and the pre-existing
        # count from the input FLAG column (a plain dask read, not the
        # algorithm).
        new_count = int(new_flags.sum().compute())
        already = int(_da.sum(existing).compute())
        total = int(np.prod(shape))

        self.ds["FLAG"] = (self.ds.FLAG.dims, new_flags)
        self.changed["FLAG"] = True
        print(
            "%s: %d of %d visibilities flagged, %d newly (%.2f%% of all)"
            % (label, new_count, total, new_count - already,
               100.0 * (new_count - already) / total if total else 0.0)
        )

    def flag_rflag(self, params):
        """Flag outliers from sliding-window statistics (a CASA ``rflag``).

        See :mod:`skarabina.rflag` for the algorithm and its deviations from the
        published one.  Shares its chunking with :meth:`flag_tfcrop`.
        """
        self._run_autofit(params, rflag_plane, _rflag_block, "flag_rflag")

    def flag_tfcrop(self, params):
        """Flag outliers on the 2-D time-frequency plane (a CASA ``tfcrop``).

        See :mod:`skarabina.tfcrop` for the algorithm and its deviations from
        the published one.  Shares its chunking with :meth:`flag_rflag`, which
        needs the same time-chunked, whole-band blocks; the chunk is the fit
        unit, so its length *is* CASA's ``ntime``.
        """
        self._run_autofit(params, tfcrop_plane, _tfcrop_block, "flag_tfcrop")

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

    def flag_data(self, operations=None, defer=None):
        """
        flag_data: Flag all NAN visibilities.

        ``defer`` lets a caller that runs several operations in sequence avoid
        a full pass over ``DATA`` per operation.  Pass a dict; the reductions
        are collected into it as ``defer[label] = (label, |data|, mask)`` and
        nothing is computed.  The caller then evaluates them all at once with
        :meth:`report_data_flags`, which shares the single ``abs(DATA)``
        subgraph between every entry.  When ``defer`` is None the statistics are
        computed and printed here, as before.
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

            if defer is not None:
                # Collect for one shared evaluation; the caller owns the print.
                if "NAN" in operations:
                    defer[("nan",)] = abs_vis, nan_flag_mask
                if "CLIP" in operations:
                    defer[("clip", clip_min, clip_max)] = abs_vis, clip_flag_mask
                return

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

    def report_data_flags(self, defer):
        """Evaluate and print the reductions collected by ``flag_data(defer=...)``.

        One ``dask.compute`` covers every entry, so ``abs(DATA)`` — the
        expensive part, and the only thing here that reads the data column — is
        built once no matter how many operations were deferred.  This is what
        keeps an ordered sequence of N operations at one pass over ``DATA``
        instead of N.
        """
        if not defer:
            return
        reductions = [da.sum(mask) for _, mask in defer.values()]
        total = da.prod(da.array(self.ds.FLAG.shape))
        results = dask.compute(*reductions, total)
        total_v = int(results[-1])
        for (label, _), count in zip(defer.items(), results[:-1]):
            kind = label[0]
            if kind == "nan":
                print(
                    "flag_data (NaN): flagged %d / %d visibilities (%.2f%%)"
                    % (int(count), total_v, 100.0 * int(count) / total_v)
                )
            else:
                _, clip_min, clip_max = label
                print(
                    "flag_data (clip [%s, %s]): flagged %d / %d visibilities (%.2f%%)"
                    % (
                        clip_min,
                        clip_max,
                        int(count),
                        total_v,
                        100.0 * int(count) / total_v,
                    )
                )

    def _report_integrations(self):
        """Report the integration structure of the MS, gap-tolerantly.

        Integrations are counted via :func:`group_integrations` rather than by
        counting distinct TIME values, so an integration whose rows were
        stamped with more than one TIME value counts once instead of twice.
        Groups holding fewer rows than a complete baseline set are called out:
        that is the shape of the "incomplete integration group" warning, and it
        is worth knowing about before time averaging.
        """
        if "TIME" not in self.ds.data_vars:
            return
        time = self.ds.TIME.data.compute()
        if time.size == 0:
            return

        groups = group_integrations(time)
        counts = np.array([end - start for start, end in groups])
        print(f"    Integrations: {len(groups)}")

        starts = np.array([start for start, _ in groups])
        if len(starts) > 1:
            cadence = float(np.median(np.diff(time[starts])))
            print("    Integration cadence: %.3f s" % cadence)

        # Number of baselines in a complete integration, autos included.  A
        # group smaller than this has lost baselines (from flagging + optimize,
        # or because it is a subset of the MS).
        nant = None
        for sub in self.sub_table_names:
            if sub.endswith("/ANTENNA"):
                try:
                    at = table(sub, ack=False)
                    nant = at.nrows()
                    at.close()
                except Exception:
                    nant = None
                break
        if nant:
            complete = nant * (nant + 1) // 2
            # Only meaningful if the MS actually holds full integrations;
            # a deliberately reduced MS (a single field, say) has smaller ones.
            if counts.max() >= complete:
                n_short = int((counts < complete).sum())
                if n_short:
                    print(
                        "    Incomplete integration groups: %d/%d"
                        " (fewer than %d baselines)"
                        % (n_short, len(groups), complete)
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
            span = fmax - fmin
            widths = self.chan_width_hz
            if widths is not None and len(widths) == nchan:
                # Report the spectrum actually present (the sum of the channel
                # widths), not the span between the band edges: those differ
                # when --optimize has dropped channels from the middle of the
                # band, and the span would then overstate the band.
                bw = float(np.sum(widths)) / 1e6
                chan_width = float(np.median(widths)) / 1e3
                print(
                    f"    Spectral windows: {self.nspw}"
                    f" (channels: {nchan},"
                    f" {fmin:.3f}–{fmax:.3f} MHz,"
                    f" {chan_width:.1f} kHz each,"
                    f" {bw:.1f} MHz of spectrum)"
                )
                gaps = np.diff(self.chan_freq_hz) > 0.5 * (
                    np.asarray(widths)[1:] + np.asarray(widths)[:-1]
                )
                if gaps.any():
                    hole = float(np.sum(np.diff(self.chan_freq_hz)[gaps])) - float(
                        np.sum(np.asarray(widths)[1:][gaps])
                    )
                    print(
                        f"    Band has holes: {hole / 1e6:.3f} MHz inside the"
                        f" {span:.1f} MHz span is not covered by any channel"
                    )
            else:
                print(
                    f"    Spectral windows: {self.nspw}"
                    f" (channels: {nchan},"
                    f" {fmin:.3f}–{fmax:.3f} MHz,"
                    f" bandwidth: {span:.1f} MHz)"
                )

            # Fringe-rotation integration time limit (Wijnholds 2018, MNRAS).
            # Time averaging causes decorrelation that depends on baseline
            # length, frequency, and angular distance ℓ from the phase centre
            # (see max_integration_time).  --field-of-view is the full width of
            # the field of view, matching skarabina-analyze --image-fov; ℓ is
            # half of it, the distance from the phase centre to its edge.
            max_uv = percentile_values[-1]
            nu_max = fmax * 1e6
            fov_rad = getattr(self, "_fov_rad", 0.0174533)

            print(
                "    Max integration time (fringe-rotation limit,"
                " FOV=%.2f deg full width):" % math.degrees(fov_rad)
            )
            # The 10% row is TIME_AVERAGE_LOSS, the criterion
            # `skarabina-analyze` quotes as max_integration_time_s, so the two
            # commands can be compared directly.
            loss_pcts = sorted({1, 3, 5, int(round(TIME_AVERAGE_LOSS * 100))})
            for loss_pc in loss_pcts:
                print(
                    "        %d%% loss:  %5.1f s"
                    % (loss_pc, max_integration_time(nu_max, max_uv, fov_rad, loss_pc / 100.0))
                )

            if "INTERVAL" in self.ds.data_vars:
                dt_current = integration_interval(self.ds.INTERVAL.data.compute())
                if dt_current is not None:
                    print("    Current integration time: %.1f s" % dt_current)
            elif "EXPOSURE" in self.ds.data_vars:
                dt_current = integration_interval(self.ds.EXPOSURE.data.compute())
                if dt_current is not None:
                    print("    Current integration time: %.1f s" % dt_current)

            self._report_integrations()

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

    def save_flag_version(self, versionname, comment=""):
        """Back up the flags of the MS as a CASA-compatible flag version.

        Writes ``<ms>.flagversions/flags.<versionname>`` and updates
        ``FLAG_VERSION_LIST``, in the same layout CASA's ``flagmanager`` uses,
        so the version can be listed and restored by either tool.

        The flags are read from the MS on disk, not from the in-memory dataset:
        a version holding only the rows left by a row selection would fail the
        row-count check on restore, and CASA's flagmanager likewise backs up
        the whole MS.
        """
        flag, flag_row = flag_versions.read_ms_flags(self.name)
        path = flag_versions.save_version(
            self.name, versionname, flag, flag_row, comment=comment
        )
        print(f"flag version '{versionname}' saved to {path}")

    def restore_flag_version(self, versionname):
        """Replace the flags in memory with a saved flag version.

        The flags are applied to the dataset, so any later flagging builds on
        the restored state and ``--msout``/``--apply`` write it out.  With
        neither of those the restored flags are discarded, exactly as with any
        other in-memory operation.
        """
        flag, flag_row = flag_versions.load_version(self.name, versionname)

        nrow_ms = int(self.ds.FLAG.shape[0])
        if int(flag.shape[0]) != nrow_ms:
            raise RuntimeError(
                f"flag version '{versionname}' has {flag.shape[0]} rows but the"
                f" MS now has {nrow_ms}: the version no longer matches this data"
                " (CASA warns that versions are unique to the MS they came from;"
                " a reduced selection can also cause this)"
            )
        if tuple(flag.shape[1:]) != tuple(self.ds.FLAG.shape[1:]):
            raise RuntimeError(
                f"flag version '{versionname}' has shape {tuple(flag.shape)} but"
                f" the MS flags are {tuple(self.ds.FLAG.shape)}"
            )

        self.ds["FLAG"].data = da.asarray(flag)
        self.ds["FLAG_ROW"] = (self.ds.FLAG_ROW.dims, da.asarray(flag_row))
        self.changed["FLAG"] = True
        self.changed["FLAG_ROW"] = True
        self._refresh_cached_columns()
        print(
            "flag version '%s' restored: %.2f%% of visibilities and %d rows flagged"
            % (
                versionname,
                100.0 * float(flag.sum()) / flag.size if flag.size else 0.0,
                int(flag_row.sum()),
            )
        )

    def list_flag_versions(self):
        """The saved flag versions of this MS as ``[(name, comment), ...]``."""
        return flag_versions.list_versions(self.name)

    def optimize(self, keep_fully_flagged_channels=False):
        """
        Run through the flags, and remove all completely flagged rows
        and channels.

        A row is removed if either:
        - FLAG_ROW is True (explicitly marked as bad), or
        - Every individual visibility in FLAG is True (all channels ×
          correlations flagged).

        A channel is removed if all rows and all correlations are flagged
        for that channel (e.g. after flag_spectral_window).

        ``keep_fully_flagged_channels`` keeps those dead channels in the data
        instead.  Flagging already excludes them from imaging, so dropping them
        buys file size and nothing else -- and when a dead channel sits inside
        the band rather than at its edge, dropping it leaves a hole that no
        SPECTRAL_WINDOW column records (see :meth:`_warn_about_band_holes`).
        Keeping them is the safer choice when the output feeds other tools.
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
        if keep_fully_flagged_channels:
            # Keep every channel; the flags already exclude the dead ones from
            # any image, and keeping them keeps the band contiguous.
            keep_channels = da.ones_like(chan_fully_flagged, dtype=bool)
        else:
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
            if keep_fully_flagged_channels:
                # Every channel is dead but we are keeping them: the output is
                # fully flagged, which is a legitimate (if useless) MS, so warn
                # rather than refuse.
                print(
                    "WARNING: every channel is fully flagged; the output MS is"
                    " entirely flagged"
                )
            else:
                raise RuntimeError("All channels fully flagged — nothing to write")

        # Find dimension names from DATA (typically "row", "chan")
        row_dim = self.ds.DATA.dims[0]
        chan_dim = self.ds.DATA.dims[1]

        # Build indexers for rows and channels.  Compute both masks in a
        # SINGLE dask pass (the channel mask is only needed when at least
        # one channel is fully flagged).
        drop_channels = int(n_chan_flagged) > 0 and not keep_fully_flagged_channels
        if drop_channels:
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
        if drop_channels:
            if self.chan_freq_hz is not None:
                self.chan_freq_hz = self.chan_freq_hz[keep_chan_idx]
            for col, values in list((self.chan_axis_hz or {}).items()):
                if values is not None:
                    self.chan_axis_hz[col] = values[keep_chan_idx]
            self._warn_about_band_holes(keep_chan_idx, n_chan_total)
        self._refresh_cached_columns()

        # Mark all changed variables
        for var_name in self.ds.data_vars:
            if row_dim in self.ds[var_name].dims:
                self.changed[var_name] = True

        print(
            f"Optimize complete."
            f" Rows: {int(n_unflagged)}, Channels: {int(n_chan_total) - int(n_chan_flagged)}"
        )

    def _warn_about_band_holes(self, keep_chan_idx, n_chan_total):
        """Warn when dropping channels leaves a hole inside the band.

        Removing a fully-flagged channel from the *edge* of the band just
        shortens it.  Removing one from the *middle* leaves a gap that no
        SPECTRAL_WINDOW column records: ``CHAN_WIDTH`` still describes each kept
        channel and ``TOTAL_BANDWIDTH`` still sums what is left, so a consumer
        that assumes contiguous channels will silently read the band as wider
        per channel than it is.  Say so, with the size of the hole.
        """
        freqs = self.chan_freq_hz
        if freqs is None or len(keep_chan_idx) < 2:
            return
        widths = (self.chan_axis_hz or {}).get("CHAN_WIDTH")
        if widths is None:
            widths = (self.chan_axis_hz or {}).get("RESOLUTION")
        if widths is None or len(widths) != len(freqs):
            return

        gaps = np.diff(freqs) > 0.5 * (widths[1:] + widths[:-1])
        n_gaps = int(np.count_nonzero(gaps))
        if n_gaps == 0:
            return
        hole_hz = float(np.sum(np.diff(freqs)[gaps] - widths[1:][gaps]))
        n_dropped_interior = int(
            np.count_nonzero(
                (keep_chan_idx > keep_chan_idx.min())
                & (keep_chan_idx < keep_chan_idx.max())
            )
        )
        print(
            f"WARNING: dropping {int(n_chan_total) - len(keep_chan_idx)} fully"
            f" flagged channel(s) split the band into {n_gaps + 1} pieces,"
            f" leaving {hole_hz / 1e6:.3f} MHz unused inside the band."
        )
        print(
            "         The hole is not recorded in SPECTRAL_WINDOW (CHAN_WIDTH"
            " and TOTAL_BANDWIDTH still describe the kept channels), so tools"
            " that assume contiguous channels will misread the band."
        )
        if n_dropped_interior:
            print(
                "         Use --keep-fully-flagged-channels to retain them and"
                " keep the band contiguous."
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

    def write_new_ms(self, name, clobber, split=None, changed_only=False):
        """
        Write a new MS, and make sure it doesn't already exist.

        If ``split`` is given (a field name or FIELD_ID), only that
        field's rows are written to the output MS.

        ``changed_only`` avoids re-reading and rewriting the columns that did
        not change (see :meth:`_write_changed_only`).  It is only valid when the
        output has the same row and channel shape as the input; ``--split`` and
        any row/channel reduction change that shape, so they fall back to a full
        write with a warning.
        """
        ds_to_write = self.ds
        if split is not None:
            ds_to_write = self._select_field(ds_to_write, split)

        if changed_only:
            reason = self._changed_only_blocker(split)
            if reason is not None:
                logger.warning(
                    "--write-changed-only: %s; writing every column instead",
                    reason,
                )
                print(f"--write-changed-only not applicable: {reason}")
                changed_only = False
            else:
                self._write_changed_only(ds_to_write, name, clobber)
                return

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

    def _changed_only_blocker(self, split):
        """Why ``--write-changed-only`` cannot be used, or None if it can.

        The mode copies the input MS and overwrites only the changed columns in
        place, so it requires the output to have the input's row and channel
        shape: a different shape means every column's data layout changes.
        """
        if split is not None:
            return "--split selects rows, so every column must be rewritten"
        src = table(self.name, ack=False, readonly=True)
        try:
            src_rows = src.nrows()
            src_chan = src.getcell("FLAG", 0).shape[0]
        except Exception:
            src_rows = src.nrows()
            src_chan = None
        finally:
            src.close()
        ds_rows = int(self.ds.FLAG.shape[0])
        ds_chan = int(self.ds.FLAG.shape[1])
        if ds_rows != src_rows:
            return (
                f"the output has {ds_rows} rows against the input's {src_rows}"
                " (row selection or removal), so every column must be rewritten"
            )
        if src_chan is not None and ds_chan != src_chan:
            return (
                f"the output has {ds_chan} channels against the input's"
                f" {src_chan} (frequency averaging or channel removal), so every"
                " column must be rewritten"
            )
        return None

    def _column_file_groups(self, dminfo):
        """Group the table's ``table.fN`` blocks, and name the column each holds.

        Returns ``{base_name: (members, column_or_None)}`` where ``members`` are
        the block files belonging to one storage manager (``table.f1``,
        ``table.f1_TSM0``, ...).  The column is identified from the storage
        manager name recorded inside the block, which is how a bare column file
        can be attributed without parsing the binary ``table.dat``.

        ``None`` means the group could not be attributed.  Callers must treat an
        unattributed group as changed: sharing blocks with a column we cannot
        name would risk rewriting the input MS.
        """
        name2cols = {}
        for spec in dminfo.values():
            for col in spec.get("COLUMNS") or []:
                name2cols.setdefault(spec.get("NAME"), []).append(col)
        names = sorted((n for n in name2cols if n), key=len, reverse=True)

        groups = {}
        for entry in os.listdir(self.name):
            if not entry.startswith("table.f"):
                continue
            groups.setdefault(entry.split("_")[0], []).append(entry)

        result = {}
        for base, members in groups.items():
            blob = b""
            for member in members:
                try:
                    with open(os.path.join(self.name, member), "rb") as fh:
                        blob += fh.read(4096)
                except OSError:
                    pass
            column = None
            for name in names:
                if name.encode() in blob:
                    cols = name2cols[name]
                    column = cols[0] if len(cols) == 1 else None
                    break
            result[base] = (sorted(members), column)
        return result

    def _write_changed_only(self, ds_to_write, name, clobber):
        """Write only the columns that changed, reusing the input's blocks.

        A flagging run changes the flags and nothing else, so rewriting the
        whole measurement set is almost all waste: measured on the 92 GB MT0 MS,
        a full write puts 103 GB through casacore where a flagging run changes
        6.1 GB.  This copies the input MS's structure and shares the unchanged
        columns' data blocks with it, then writes only the changed columns.

        The saving is on writes only.  Reading the input goes through dask-ms,
        which attaches the whole measurement set's read graph to the table it
        opens for the write, so this path still reads every column; use
        ``update_ms`` (--apply) when the read dominates.

        Sharing is by hard link, so unchanged columns cost no data transfer at
        all.  That requires the output to be on the same filesystem as the
        input; where the link fails the block is copied instead, which is no
        worse than the full write.

        Shared blocks are then made read-only.  They are still perfectly
        readable -- both MSes open normally -- but an attempt to rewrite an
        unchanged column in the output is refused by the filesystem instead of
        silently altering the input MS.  Change the changed columns as often as
        you like; the changed columns always get fresh blocks.  ``--apply``
        remains the better option when the input itself may be modified.

        The fresh blocks belong to the output alone, so they are left writable
        even when the input's own block carries a read-only mode -- which is
        what an earlier sharing run leaves behind, a hard link being one inode.
        Without that the second run over the same input would die on the first
        column it had to write (issue #3).
        """
        if os.path.exists(name):
            if not clobber:
                raise RuntimeError(
                    f"Measurement set {name} already exists. Use --clobber to overwrite"
                )
            logger.warning(f"Overwriting {name}")
            shutil.rmtree(name)

        columns = {c for c, changed in self.changed.items() if changed}
        src = table(self.name, ack=False, readonly=True)
        dminfo = src.getdminfo()
        src.close()
        groups = self._column_file_groups(dminfo)

        shared, rewritten = [], []
        for base, (members, column) in sorted(groups.items()):
            if column is not None and column not in columns:
                shared.append((base, members))
            else:
                rewritten.append((base, members, column))

        n_shared = sum(len(m) for _, m in shared)
        print(
            f"Writing {name} (changed only): sharing {len(shared)} unchanged"
            f" column group(s), rewriting {len(columns)}:"
            f" {', '.join(sorted(columns)) or 'none'}"
        )
        for base, _, column in rewritten:
            if column in columns:
                logger.debug("  %s holds %s (changed)", base, column)

        with _maybe_quiet_stderr():
            os.makedirs(name, exist_ok=True)
            linked = copied = 0
            protected = []
            for base, members in shared:
                for member in members:
                    source = os.path.join(self.name, member)
                    target = os.path.join(name, member)
                    try:
                        os.link(source, target)
                        linked += 1
                        protected.append(target)
                    except OSError:
                        shutil.copy2(source, target)
                        # A copy is not shared, so nothing protects it: leave it
                        # writable, whatever mode the input's block carries.
                        _ensure_writable(target)
                        copied += 1
            for base, members, _ in rewritten:
                for member in members:
                    source = os.path.join(self.name, member)
                    if os.path.exists(source):
                        target = os.path.join(name, member)
                        shutil.copy2(source, target)
                        # copy2 keeps the mode, and this block is about to be
                        # written: an input left read-only by an earlier run
                        # must not make the output unwritable (issue #3).
                        _ensure_writable(target)
                        copied += 1

            # Everything that is not a column block: table.dat, table.info,
            # table.lock targets, the subtables and the main-table keywords.
            for entry in os.listdir(self.name):
                if entry.startswith("table.f"):
                    continue
                source = os.path.join(self.name, entry)
                target = os.path.join(name, entry)
                if os.path.isdir(source):
                    shutil.copytree(source, target, symlinks=True)
                elif not os.path.exists(target):
                    shutil.copy2(source, target)

            self._copy_missing_keywords(name)
            if self.chan_freq_hz is not None:
                nchan_in_ds = self.ds.FLAG.shape[1]
                if self.spw_chan_count != nchan_in_ds:
                    self._rewrite_spw_channels(name, nchan_in_ds)

            for col in sorted(columns):
                print(f"Updating table: {col} in {name}")
                # Passing only ``col`` here does *not* avoid reading the other
                # columns: dask-ms attaches the MS's whole read graph to the
                # table handle it opens for the write, so the DATA read tasks
                # run either way (measured: a one-column dataset executes the
                # same 75 ``read~DATA`` tasks as the full one).  Narrowing the
                # dataset is therefore not worth doing, and the read cost is
                # not avoidable on this path.
                writes = xds_to_table(ds_to_write, name, col)
                with ProgressBar():
                    dask.compute(writes)

            # Make the shared blocks read-only, so that rewriting an unchanged
            # column in the output fails loudly rather than altering the input.
            for target in protected:
                try:
                    os.chmod(target, os.stat(target).st_mode & ~0o222)
                except OSError:
                    pass

        print(
            f"  {linked} block(s) shared with the input ({n_shared} column"
            f" group(s), left read-only), {copied} copied or written"
        )

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
