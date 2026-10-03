# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""skarabina-plotms: plot a measurement set or a caltable with matplotlib.

The meerkat_imaging pipeline plots its stage-1 gain tables with ``casa.plotms``
-- casaplotms is an x86_64-only AppImage, so an arm64 host skips those steps
entirely.  This is the drop-in replacement: the same parameters the pipeline
passes (``ms``, ``plotfile``, ``overwrite``, ``xaxis``, ``yaxis``) and the
plotms defaults (x = time, y = amplitude, flagged data left out, one panel),
rendered with matplotlib's non-interactive Agg backend so it runs in a
container with no display.

Both table kinds are supported:

* a measurement set (``DATA``/``CORRECTED_DATA``/``MODEL_DATA`` vs time,
  uv distance, channel, frequency, ...), and
* a caltable written by ``gaincal``/``bandpass`` (``CPARAM``/``FPARAM``
  vs time, channel, antenna, baseline, ...).

Large tables are decimated to about ``--max-points`` plotted points per
correlation, and the read is chunked so the plot never materialises the whole
visibility column at once.
"""

import os
import tempfile
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime

import click
import numpy as np

# Import dask-ms before casacore.tables so that, when the casacure backend is
# selected (DASK_MS_BACKEND=casacure), daskms's casacore->casacure aliasing is
# installed before the `casacore` import resolves (see skarabina/dask_ms.py).
import daskms  # noqa: F401,E402
from casacore.tables import table  # noqa: E402


def _ensure_mpl_configdir():
    """Point matplotlib's cache somewhere writable.

    Matplotlib wants a config/cache directory even just to import, and a
    pipeline container often has a read-only HOME; without this it prints a
    warning and falls back to a fresh temp dir on every run.
    """
    if os.environ.get("MPLCONFIGDIR"):
        return
    conf = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    probe = os.path.join(conf, "matplotlib")
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if probe and os.access(probe, os.W_OK):
        return
    # Per-user (a shared /tmp is not): matplotlib creates the font cache in
    # there on first import, so it must be writable by this user alone.
    user = getattr(os, "getuid", lambda: "user")()
    os.environ["MPLCONFIGDIR"] = os.path.join(
        tempfile.gettempdir(), f"skarabina-mpl-{user}")


_ensure_mpl_configdir()
import matplotlib  # noqa: E402

matplotlib.use("Agg")  # file output only; no display, no Xvfb
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

C_MPS = 299792458.0
# matplotlib's date number for MJD 0 (1858-11-17), so TIME (MJD seconds,
# casacore's convention) becomes a plotted date by simple arithmetic.
MJD0_DATE = mdates.date2num(datetime(1858, 11, 17))
MJD_SECONDS_PER_DAY = 86400.0

# Read at most this much of a data column at once while chunking a big table.
DATA_CHUNK_BYTES = 64 * 1024 * 1024
# For a non-spectral x axis, how many channels a decimated plot samples across
# the band: enough to see band structure, few enough to spend the point budget
# on time resolution (plotms shows such plots as it decimates them too).
CHAN_CAP = 64
DEFAULT_MAX_POINTS = 200_000

# plotms spells every axis several ways ("chan"/"Channel", "uvdist_l"/...);
# accept the same spellings so a recipe's xaxis/yaxis= transfers unchanged.
X_AXIS_ALIASES = {
    "": "time",
    "time": "time",
    "interval": "interval",
    "time_interval": "interval",
    "timeinterval": "interval",
    "timeint": "interval",
    "chan": "channel",
    "channel": "channel",
    "freq": "frequency",
    "frequency": "frequency",
    "uvdist": "uvdist",
    "uvdist_l": "uvwave",
    "uvdistl": "uvwave",
    "uvwave": "uvwave",
    "u": "u",
    "v": "v",
    "w": "w",
    "scan": "scan",
    "field": "field",
    "spw": "spw",
    "row": "row",
    "antenna": "antenna",
    "ant": "antenna",
    "baseline": "baseline",
    "antenna1": "antenna1",
    "ant1": "antenna1",
    "antenna2": "antenna2",
    "ant2": "antenna2",
}
Y_AXIS_ALIASES = {
    "": "amp",
    "amp": "amp",
    "amplitude": "amp",
    "phase": "phase",
    "real": "real",
    "imag": "imag",
    "imaginary": "imag",
    "wt": "wt",
    "weight": "wt",
    "snr": "snr",
}

X_LABELS = {
    "time": "Time",
    "interval": "Interval (s)",
    "channel": "Channel",
    "frequency": "Frequency (GHz)",
    "uvdist": "UV distance (m)",
    "uvwave": "UV distance (wavelengths)",
    "u": "u (m)",
    "v": "v (m)",
    "w": "w (m)",
    "scan": "Scan",
    "field": "Field",
    "spw": "Spectral window",
    "row": "Row",
    "antenna": "Antenna",
    "antenna1": "Antenna 1",
    "antenna2": "Antenna 2",
    "baseline": "Baseline",
}
Y_LABELS = {
    "amp": "Amplitude",
    "phase": "Phase (deg)",
    "real": "Real",
    "imag": "Imaginary",
    "wt": "Weight",
    "snr": "SNR",
}

# The axes each table kind can produce: a caltable has no UVW or scan column,
# an MS has no SNR column.
MS_X_AXES = set(X_LABELS)
CAL_X_AXES = {"time", "interval", "field", "spw", "row", "channel",
              "frequency", "antenna", "antenna1", "antenna2", "baseline"}
MS_Y_AXES = {"amp", "phase", "real", "imag", "wt"}
CAL_Y_AXES = {"amp", "phase", "real", "imag", "snr", "wt"}

# casacore Stokes codes -> polarization names (POLARIZATION.CORR_TYPE).
STOKES_NAMES = {
    1: "I", 2: "Q", 3: "U", 4: "V",
    5: "RR", 6: "RL", 7: "LR", 8: "LL",
    9: "XX", 10: "XY", 11: "YX", 12: "YY",
}
# A caltable's own POLARIZATION subtable is often empty, so its correlations
# are then named from their count -- linear basis, as MeerKAT records them.
CAL_CORR_FALLBACK = {
    1: ("",),
    2: ("XX", "YY"),
    4: ("XX", "XY", "YX", "YY"),
}

DATA_COLUMN_NAMES = {
    "DATA": "DATA",
    "CORRECTED": "CORRECTED_DATA",
    "CORRECTED_DATA": "CORRECTED_DATA",
    "MODEL": "MODEL_DATA",
    "MODEL_DATA": "MODEL_DATA",
}


@dataclass
class PlotData:
    """A collected plot: one (label, x, y) series per correlation."""
    name: str
    xlabel: str
    ylabel: str
    x_is_time: bool
    series: list = dataclass_field(default_factory=list)
    # Points kept, and points dropped because they are flagged.
    n_points: int = 0
    n_flagged: int = 0
    # Antenna names and baseline pairs for axis ticks, when relevant.
    antenna_names: list = None
    baseline_pairs: list = None


def _ceil_div(a, b):
    return -(-a // b)


def _parse_selection(text):
    """Comma-separated tokens -> stripped non-empty tokens."""
    return [t.strip() for t in str(text or "").split(",") if t.strip()]


def _parse_ints(text, what):
    values = []
    for token in _parse_selection(text):
        try:
            values.append(int(token))
        except ValueError:
            raise click.ClickException(
                f"{what} selection {text!r} expects comma-separated integers,"
                f" got {token!r}"
            )
    return values


def _resolve_axis(name, aliases, supported, kind, what):
    canon = aliases.get(str(name or "").strip().lower())
    if canon is None or canon not in supported:
        choices = ", ".join(sorted(supported))
        raise click.ClickException(
            f"unknown {what} axis {name!r} for a {kind} table;"
            f" choices: {choices}"
        )
    return canon


def _keyword_names(tab):
    """The table's keyword names, across the casacore bindings.

    casacure (the ``DASK_MS_BACKEND=casacure`` alias) has no
    ``keywordnames()`` and returns a dict from ``getkeywords()``.
    """
    if hasattr(tab, "keywordnames"):
        return list(tab.keywordnames())
    return list(tab.getkeywords().keys())


def _read_head(tab, path, datacol, row):
    """The first row of the data column: its (chan, corr) shape.

    A column present in the descriptor but never written (an MS with an
    untouched CORRECTED_DATA) makes casacore raise instead, so turn that into
    the error a pipeline step should show.
    """
    try:
        head = tab.getcol(datacol, int(row), 1)
    except RuntimeError as exc:
        raise click.ClickException(
            f"{path}: cannot read {datacol}: {exc}"
        ) from exc
    if head.ndim != 3:
        # casacure answers an unwritten column with an empty array instead of
        # raising, where casacore raises -- both end here.
        raise click.ClickException(
            f"{path}: cannot read {datacol}: row {row} holds no data"
            f" (shape {head.shape})"
        )
    return head


def _table_kind(tab, path):
    """'ms' for a measurement set, 'cal' for a caltable."""
    cols = set(tab.colnames())
    if "MS_VERSION" in _keyword_names(tab):
        return "ms"
    if cols & {"CPARAM", "FPARAM", "SPARAM"}:
        return "cal"
    if "DATA" in cols:
        return "ms"
    raise click.ClickException(
        f"{path}: neither a measurement set nor a caltable"
        " (no MS_VERSION keyword, no CPARAM/FPARAM/SPARAM column)"
    )


def _open_subtable(path, name):
    """Open a subtable by keyword, falling back to a plain directory."""
    for candidate in (f"{path}::{name}", os.path.join(path, name)):
        try:
            return table(candidate, readonly=True, ack=False)
        except Exception:
            continue
    return None


def _subtable_column(path, name, column):
    """A column of a subtable, or None when the subtable/column is absent."""
    sub = _open_subtable(path, name)
    if sub is None:
        return None
    try:
        if column not in sub.colnames():
            return None
        return sub.getcol(column)
    finally:
        sub.close()


def _read_rows(tab, name, rows):
    """Read ``name`` for the sorted row indices in ``rows``.

    ``rows`` may be strided or scattered (a decimated plot, a scan or field
    selection), and neither binding can read it directly: casacore's
    ``getcol(..., rowincr)`` is ignored by casacure, which also has no
    ``selectrows``.  So the span is read in contiguous chunks of about
    ``DATA_CHUNK_BYTES`` and the wanted rows are sliced out of each chunk.
    Memory stays bounded by the chunk; at most the selected span is read.
    """
    rows = np.asarray(rows, dtype=np.int64)
    if rows.size == 0:
        return None
    lo, hi = int(rows[0]), int(rows[-1])
    per_row = max(int(tab.getcol(name, lo, 1).nbytes), 8)
    block = max(1, DATA_CHUNK_BYTES // per_row)
    parts = []
    for base in range(lo, hi + 1, block):
        end = min(base + block, hi + 1)
        take = rows[(rows >= base) & (rows < end)]
        if take.size == 0:
            continue
        try:
            chunk = tab.getcol(name, base, end - base)
        except RuntimeError as exc:
            raise click.ClickException(
                f"cannot read the {name} column: {exc}"
            ) from exc
        parts.append(chunk[take - base])
    if len(parts) == 1:
        return parts[0]
    return np.concatenate(parts, axis=0)


def _antenna_names(path, count):
    names = _subtable_column(path, "ANTENNA", "NAME")
    if names is None:
        return [str(i) for i in range(count)]
    return [str(n) for n in names]


def _corr_names_ms(path, ncorr):
    pol_id = _subtable_column(path, "DATA_DESCRIPTION", "POLARIZATION_ID")
    corr_types = _subtable_column(path, "POLARIZATION", "CORR_TYPE")
    codes = None
    if corr_types is not None and len(corr_types):
        index = 0 if pol_id is None else int(pol_id[0])
        if 0 <= index < len(corr_types):
            codes = corr_types[index]
    if codes is None or len(codes) != ncorr:
        # No usable POLARIZATION subtable (a hand-built or reduced MS):
        # fall back to the count, which for these is a linear basis.
        return list(CAL_CORR_FALLBACK.get(ncorr, tuple(str(i) for i in range(ncorr))))
    return [STOKES_NAMES.get(int(c), str(int(c))) for c in codes]


def _corr_names_cal(path, ncorr):
    corr_types = _subtable_column(path, "POLARIZATION", "CORR_TYPE")
    if corr_types is not None and len(corr_types) and len(corr_types[0]) == ncorr:
        return [STOKES_NAMES.get(int(c), str(int(c))) for c in corr_types[0]]
    return list(CAL_CORR_FALLBACK.get(ncorr, tuple(str(i) for i in range(ncorr))))


def _select_corr(corr_spec, names):
    """--corr names/indices -> indices into the correlation axis."""
    tokens = _parse_selection(corr_spec)
    if not tokens:
        return list(range(len(names)))
    lowered = [n.lower() for n in names]
    indices = []
    for token in tokens:
        if token.lower() in lowered:
            indices.append(lowered.index(token.lower()))
            continue
        try:
            index = int(token)
        except ValueError:
            raise click.ClickException(
                f"unknown correlation {token!r}; choices:"
                f" {', '.join(names)} or 0..{len(names) - 1}"
            )
        if not 0 <= index < len(names):
            raise click.ClickException(
                f"correlation index {index} out of range 0..{len(names) - 1}"
            )
        indices.append(index)
    return sorted(set(indices))


def _resolve_field_ids(path, field_spec):
    """--field names/ids -> FIELD_IDs (names need a FIELD subtable)."""
    tokens = _parse_selection(field_spec)
    if not tokens:
        return None
    ids = set()
    names_wanted = []
    for token in tokens:
        try:
            ids.add(int(token))
        except ValueError:
            names_wanted.append(token)
    if names_wanted:
        names = _subtable_column(path, "FIELD", "NAME")
        if names is None:
            raise click.ClickException(
                f"--field {field_spec!r}: names cannot be resolved without a"
                " FIELD subtable; use numeric field ids"
            )
        available = {str(n).lower(): str(n) for n in names}
        for token in names_wanted:
            if token.lower() not in available:
                raise click.ClickException(
                    f"unknown field name {token!r}; this table has"
                    f" {', '.join(str(n) for n in names)}"
                )
            ids.add(list(names).index(available[token.lower()]))
    return sorted(ids)


def _mask_from(values, selected):
    if selected is None:
        return np.ones(len(values), dtype=bool)
    return np.isin(values, selected)


def _decimation_plan(x, nrow, nchan, ncorr, max_points):
    """(row_stride, channel_stride) keeping roughly max_points points."""
    if not max_points or max_points <= 0 or nrow * nchan * ncorr <= max_points:
        return 1, 1
    if x in ("channel", "frequency"):
        # The x axis IS the channel axis: keep every channel, stride rows.
        return max(1, _ceil_div(nrow * nchan * ncorr, max_points)), 1
    chan_step = max(1, _ceil_div(nchan, CHAN_CAP))
    nchan_eff = _ceil_div(nchan, chan_step)
    return max(1, _ceil_div(nrow * nchan_eff * ncorr, max_points)), chan_step


def _chunk_rows(nchan, ncorr):
    """Rows to read at a time, bounding the temporary data cube."""
    per_row = max(16, nchan * ncorr * 16)  # complex128 + bool, with slack
    return max(1, DATA_CHUNK_BYTES // per_row)


def _y_values(values, y):
    if y == "amp":
        return np.abs(values)
    if y == "phase":
        return np.degrees(np.angle(values))
    if y == "real":
        return np.real(values)
    if y == "imag":
        return np.imag(values)
    raise AssertionError(f"unhandled y axis {y!r}")


def _baseline_codes(a1, a2, uniq_pairs):
    """Index of each (antenna1, antenna2) pair in the sorted unique pairs."""
    keys = a1.astype(np.int64) * (1 << 32) + a2.astype(np.int64)
    uniq_keys = uniq_pairs[:, 0].astype(np.int64) * (1 << 32) + uniq_pairs[:, 1]
    return np.searchsorted(uniq_keys, keys)


def _pair_table(a1, a2):
    pairs = np.stack([np.asarray(a1), np.asarray(a2)], axis=1)
    uniq = np.unique(pairs, axis=0)
    return uniq


def _decide_marker(npoints):
    if npoints < 5_000:
        return 4.0, 0.9
    if npoints < 50_000:
        return 2.0, 0.6
    return 1.0, 0.35


def collect(path, xaxis="", yaxis="", corr="", field="", spw="", scan="",
            data_column="DATA", max_points=DEFAULT_MAX_POINTS,
            show_flagged=False):
    """Read ``path`` and return the :class:`PlotData` a plot would show.

    Raises :class:`click.ClickException` for a bad axis, an empty selection
    or a table missing the requested column -- the errors a pipeline step
    should fail loudly on.
    """
    path = str(path)
    if not os.path.exists(path):
        raise click.ClickException(f"{path}: no such measurement set or caltable")
    tab = table(path, readonly=True, ack=False)
    try:
        kind = _table_kind(tab, path)
        x = _resolve_axis(
            xaxis, X_AXIS_ALIASES,
            MS_X_AXES if kind == "ms" else CAL_X_AXES, kind, "x",
        )
        y = _resolve_axis(
            yaxis, Y_AXIS_ALIASES,
            MS_Y_AXES if kind == "ms" else CAL_Y_AXES, kind, "y",
        )
        if kind == "ms":
            return _collect_ms(tab, path, x, y, corr, field, spw, scan,
                               data_column, max_points, show_flagged)
        if _parse_selection(scan):
            raise click.ClickException(
                "--scan applies to measurement sets; a caltable has no scans"
            )
        return _collect_cal(tab, path, x, y, corr, field, spw,
                            max_points, show_flagged)
    finally:
        tab.close()


def _collect_ms(tab, path, x, y, corr, field, spw, scan, data_column,
                max_points, show_flagged):
    cols = set(tab.colnames())
    nrow = tab.nrows()
    if nrow == 0:
        raise click.ClickException(f"{path}: the measurement set has no rows")
    if "TIME" not in cols:
        raise click.ClickException(f"{path}: no TIME column")
    datacol = DATA_COLUMN_NAMES.get(str(data_column).upper())
    if datacol is None:
        raise click.ClickException(
            f"unknown data column {data_column!r}; choices: DATA, CORRECTED,"
            " MODEL"
        )
    if datacol not in cols:
        raise click.ClickException(
            f"{path}: no {datacol} column (has: {', '.join(sorted(cols))})"
        )

    # Row-scoped scalars are read whole: a few MB even for a big MS, and the
    # selection mask needs them before the data read can be planned.
    time = tab.getcol("TIME")
    mask = np.ones(nrow, dtype=bool)
    field_ids = _resolve_field_ids(path, field)
    if field_ids is not None:
        if "FIELD_ID" not in cols:
            raise click.ClickException(f"{path}: no FIELD_ID column")
        mask &= _mask_from(tab.getcol("FIELD_ID"), field_ids)
    scan_ids = _parse_ints(scan, "scan")
    if scan_ids:
        if "SCAN_NUMBER" not in cols:
            raise click.ClickException(f"{path}: no SCAN_NUMBER column")
        mask &= _mask_from(tab.getcol("SCAN_NUMBER"), scan_ids)
    spw_ids = _parse_ints(spw, "spw")
    spw_of_row = None
    if spw_ids or x in ("frequency", "uvwave", "spw"):
        if "DATA_DESC_ID" not in cols:
            raise click.ClickException(f"{path}: no DATA_DESC_ID column")
        dd = _open_subtable(path, "DATA_DESCRIPTION")
        if dd is None:
            raise click.ClickException(f"{path}: no DATA_DESCRIPTION subtable")
        try:
            dd_spw = dd.getcol("SPECTRAL_WINDOW_ID")
        finally:
            dd.close()
        spw_of_row = dd_spw[tab.getcol("DATA_DESC_ID")]
        if spw_ids:
            mask &= _mask_from(spw_of_row, spw_ids)
    if "FLAG_ROW" in cols and not show_flagged:
        mask &= ~tab.getcol("FLAG_ROW")
    rows_kept = np.flatnonzero(mask)
    if rows_kept.size == 0:
        raise click.ClickException(f"{path}: the selection excludes every row")

    # Correlations, before any cube is read.
    head = _read_head(tab, path, datacol, rows_kept[0])
    nchan, ncorr = head.shape[1], head.shape[2]
    corr_names = _corr_names_ms(path, ncorr)
    corr_sel = _select_corr(corr, corr_names)

    if x in ("frequency", "uvwave"):
        chan_freq = _subtable_column(path, "SPECTRAL_WINDOW", "CHAN_FREQ")
        if chan_freq is None:
            raise click.ClickException(
                f"{path}: no SPECTRAL_WINDOW CHAN_FREQ for the {x} axis"
            )
        ref_freq = _subtable_column(path, "SPECTRAL_WINDOW", "REF_FREQUENCY") \
            if x == "uvwave" else None
        if x == "uvwave" and ref_freq is None:
            raise click.ClickException(
                f"{path}: no SPECTRAL_WINDOW REF_FREQUENCY for the uvwave axis"
            )

    row_step, chan_step = _decimation_plan(
        x, rows_kept.size, nchan, ncorr, max_points)
    rows = rows_kept[::row_step]
    chan_idx = np.arange(0, nchan, chan_step)

    # Column reads the chosen x axis needs (whole columns; cheap scalars,
    # cached so a multi-chunk loop reads each one once).
    cache = {}

    def scalar(name):
        if name not in cols:
            raise click.ClickException(
                f"{path}: no {name} column (needed for the {x} axis)"
            )
        if name not in cache:
            cache[name] = tab.getcol(name)
        return cache[name]

    uvw = tab.getcol("UVW") if x in ("uvdist", "uvwave", "u", "v", "w") else None
    # WEIGHT is per-row (not per-channel), so read it once for the whole
    # column; the per-channel WEIGHT_SPECTRUM is read chunk by chunk below.
    weight_rows = tab.getcol("WEIGHT") \
        if y == "wt" and "WEIGHT" in cols else None
    a1 = a2 = None
    if x in ("antenna", "antenna1", "antenna2", "baseline"):
        a1 = scalar("ANTENNA1")
        if x in ("antenna2", "baseline"):
            a2 = scalar("ANTENNA2")
    uniq_pairs = _pair_table(a1, a2) if x == "baseline" else None

    antenna_names = None
    if x in ("antenna", "antenna1", "antenna2", "baseline"):
        count = int(np.max(a1)) + 1
        antenna_names = _antenna_names(path, count)

    name = os.path.basename(os.path.normpath(path))
    plot = PlotData(
        name=name, xlabel=X_LABELS[x], ylabel=Y_LABELS[y],
        x_is_time=(x == "time"), antenna_names=antenna_names,
        baseline_pairs=(
            [(int(p[0]), int(p[1])) for p in uniq_pairs]
            if uniq_pairs is not None else None
        ),
    )
    chunk = _chunk_rows(nchan, ncorr)
    for start in range(0, rows.size, chunk):
        part = rows[start:start + chunk]
        cube = _read_rows(tab, datacol, part)[:, ::chan_step, :][:, :, corr_sel]
        flags = None
        if "FLAG" in cols and not show_flagged:
            flags = _read_rows(tab, "FLAG", part)[:, ::chan_step, :][:, :, corr_sel]
        shape = (cube.shape[0], cube.shape[1])
        xrow = xchan = xmat = None
        if x == "time":
            xrow = time[part] / MJD_SECONDS_PER_DAY + MJD0_DATE
        elif x == "interval":
            xrow = scalar("INTERVAL")[part]
        elif x == "scan":
            xrow = scalar("SCAN_NUMBER")[part]
        elif x == "field":
            xrow = scalar("FIELD_ID")[part]
        elif x == "spw":
            xrow = spw_of_row[part]
        elif x == "row":
            xrow = part
        elif x in ("channel", "frequency"):
            xchan = chan_idx
            if x == "frequency":
                xmat = chan_freq[spw_of_row[part]][:, ::chan_step] / 1e9
        elif x == "uvdist":
            u = uvw[part]
            xrow = np.hypot(u[:, 0], u[:, 1])
        elif x == "uvwave":
            u = uvw[part]
            xrow = (np.hypot(u[:, 0], u[:, 1])
                    * ref_freq[spw_of_row[part]] / C_MPS)
        elif x in ("u", "v", "w"):
            xrow = uvw[part, "uvw".index(x)]
        elif x == "antenna":
            xrow = a1[part]
        elif x == "antenna1":
            xrow = a1[part]
        elif x == "antenna2":
            xrow = a2[part]
        elif x == "baseline":
            xrow = _baseline_codes(a1[part], a2[part], uniq_pairs)
        if xmat is None:
            xmat = np.broadcast_to(
                (chan_idx[None, :] if xchan is not None else xrow[:, None]),
                shape,
            )
        weight = None
        if y == "wt":
            if "WEIGHT_SPECTRUM" in cols:
                weight = _read_rows(tab, "WEIGHT_SPECTRUM", part)[:, ::chan_step]
                weight = weight[:, :, corr_sel]
            elif weight_rows is not None:
                weight = weight_rows[part][:, None, :][:, :, corr_sel]
            else:
                raise click.ClickException(
                    f"{path}: no WEIGHT(_SPECTRUM) column for the wt axis"
                )
        for position, corr_index in enumerate(corr_sel):
            values = cube[:, :, position]
            if y == "wt":
                yv = np.broadcast_to(weight[:, :, position], values.shape)
            else:
                yv = _y_values(values, y)
            keep = np.ones(values.shape, dtype=bool) \
                if show_flagged or flags is None else ~flags[:, :, position]
            plot.n_points += int(keep.sum())
            plot.n_flagged += int((~keep).sum())
            label = corr_names[corr_index] if len(corr_sel) > 1 else ""
            plot.series.append((label, xmat[keep], yv[keep]))
    return plot


def _collect_cal(tab, path, x, y, corr, field, spw, max_points, show_flagged):
    cols = set(tab.colnames())
    nrow = tab.nrows()
    if nrow == 0:
        raise click.ClickException(f"{path}: the caltable has no rows")
    if "TIME" not in cols or "ANTENNA1" not in cols:
        raise click.ClickException(f"{path}: not a caltable (no TIME/ANTENNA1)")
    datacol = next((c for c in ("CPARAM", "FPARAM", "SPARAM") if c in cols), None)
    if datacol is None:
        raise click.ClickException(f"{path}: no CPARAM/FPARAM/SPARAM column")
    if y == "snr" and "SNR" not in cols:
        raise click.ClickException(f"{path}: no SNR column for the snr axis")
    if y == "wt" and "WEIGHT" not in cols:
        raise click.ClickException(f"{path}: no WEIGHT column for the wt axis")

    time = tab.getcol("TIME")
    mask = np.ones(nrow, dtype=bool)
    field_ids = _resolve_field_ids(path, field)
    if field_ids is not None:
        if "FIELD_ID" not in cols:
            raise click.ClickException(f"{path}: no FIELD_ID column")
        mask &= _mask_from(tab.getcol("FIELD_ID"), field_ids)
    spw_ids = _parse_ints(spw, "spw")
    if spw_ids:
        if "SPECTRAL_WINDOW_ID" not in cols:
            raise click.ClickException(f"{path}: no SPECTRAL_WINDOW_ID column")
        mask &= _mask_from(tab.getcol("SPECTRAL_WINDOW_ID"), spw_ids)
    rows_kept = np.flatnonzero(mask)
    if rows_kept.size == 0:
        raise click.ClickException(f"{path}: the selection excludes every row")

    head = _read_head(tab, path, datacol, rows_kept[0])
    ncorr, nchan = head.shape[1], head.shape[2]
    corr_names = _corr_names_cal(path, ncorr)
    corr_sel = _select_corr(corr, corr_names)

    chan_freq = None
    if x == "frequency":
        chan_freq = _subtable_column(path, "SPECTRAL_WINDOW", "CHAN_FREQ")
        if chan_freq is None:
            raise click.ClickException(
                f"{path}: no SPECTRAL_WINDOW CHAN_FREQ for the frequency axis"
                " (use --xaxis chan)"
            )

    row_step, chan_step = _decimation_plan(
        x, rows_kept.size, nchan, ncorr, max_points)
    rows = rows_kept[::row_step]
    chan_idx = np.arange(0, nchan, chan_step)

    cache = {}

    def scalar(name):
        if name not in cols:
            raise click.ClickException(
                f"{path}: no {name} column (needed for the {x} axis)"
            )
        if name not in cache:
            cache[name] = tab.getcol(name)
        return cache[name]

    a1 = scalar("ANTENNA1") if x in ("antenna", "antenna1", "antenna2",
                                     "baseline") else None
    a2 = None
    if x in ("antenna2", "baseline"):
        a2 = scalar("ANTENNA2") if "ANTENNA2" in cols else -np.ones_like(a1)
    uniq_pairs = _pair_table(a1, a2) if x == "baseline" else None

    antenna_names = None
    if a1 is not None:
        antenna_names = _antenna_names(path, int(np.max(a1)) + 1)

    name = os.path.basename(os.path.normpath(path))
    plot = PlotData(
        name=name, xlabel=X_LABELS[x], ylabel=Y_LABELS[y],
        x_is_time=(x == "time"), antenna_names=antenna_names,
        baseline_pairs=(
            [(int(p[0]), int(p[1])) for p in uniq_pairs]
            if uniq_pairs is not None else None
        ),
    )
    chunk = _chunk_rows(nchan, ncorr)
    for start in range(0, rows.size, chunk):
        part = rows[start:start + chunk]
        # A caltable cube is (row, correlation, channel); an MS's is
        # (row, channel, correlation).
        cube = _read_rows(tab, datacol, part)[:, corr_sel, :]
        cube = cube[:, :, ::chan_step]
        flags = None
        if "FLAG" in cols and not show_flagged:
            flags = _read_rows(tab, "FLAG", part)[:, corr_sel, :][:, :, ::chan_step]
        shape = (cube.shape[0], cube.shape[2])
        xrow = xchan = xmat = None
        if x == "time":
            xrow = time[part] / MJD_SECONDS_PER_DAY + MJD0_DATE
        elif x == "interval":
            xrow = scalar("INTERVAL")[part]
        elif x == "field":
            xrow = scalar("FIELD_ID")[part]
        elif x == "spw":
            xrow = scalar("SPECTRAL_WINDOW_ID")[part]
        elif x == "row":
            xrow = part
        elif x in ("channel", "frequency"):
            xchan = chan_idx
            if x == "frequency":
                spw_row = scalar("SPECTRAL_WINDOW_ID")[part] \
                    if "SPECTRAL_WINDOW_ID" in cols else np.zeros(len(part))
                xmat = chan_freq[spw_row][:, ::chan_step] / 1e9
        elif x in ("antenna", "antenna1"):
            xrow = a1[part]
        elif x == "antenna2":
            xrow = a2[part]
        elif x == "baseline":
            xrow = _baseline_codes(a1[part], a2[part], uniq_pairs)
        if xmat is None:
            xmat = np.broadcast_to(
                (chan_idx[None, :] if xchan is not None else xrow[:, None]),
                shape,
            )
        weight = snr = None
        if y == "wt":
            weight = _read_rows(tab, "WEIGHT", part)[:, None, corr_sel]
        elif y == "snr":
            snr = _read_rows(tab, "SNR", part)[:, None, corr_sel]
        for position, corr_index in enumerate(corr_sel):
            values = cube[:, position, :]
            if y == "wt":
                yv = np.broadcast_to(weight[:, 0, position][:, None], values.shape)
            elif y == "snr":
                yv = np.broadcast_to(snr[:, 0, position][:, None], values.shape)
            else:
                yv = _y_values(values, y)
            keep = np.ones(values.shape, dtype=bool) \
                if flags is None else ~flags[:, position, :]
            plot.n_points += int(keep.sum())
            plot.n_flagged += int((~keep).sum())
            label = corr_names[corr_index] if len(corr_sel) > 1 else ""
            plot.series.append((label, xmat[keep], yv[keep]))
    return plot


def _style_antenna_axis(ax, plot):
    """Name the antenna/baseline ticks when there are few enough to read."""
    if plot.xlabel not in ("Antenna", "Antenna 1", "Antenna 2", "Baseline"):
        return
    names = plot.antenna_names or []

    def name_at(i):
        return names[i] if 0 <= i < len(names) else str(i)

    if plot.xlabel == "Baseline":
        if not plot.baseline_pairs or len(plot.baseline_pairs) > 40:
            return
        ticks = list(range(len(plot.baseline_pairs)))
        labels = [f"{name_at(a)}-{name_at(b)}"
                  for a, b in plot.baseline_pairs]
    else:
        if not names or len(names) > 64:
            return
        ticks = list(range(len(names)))
        labels = list(names)
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, rotation=90, fontsize=8)


def render(plot, plotfile, title=""):
    """Write the collected plot to ``plotfile`` (format from the extension)."""
    fig, ax = plt.subplots(figsize=(10, 6))
    plotted = 0
    for label, x, y in plot.series:
        if len(x) == 0:
            continue
        size, alpha = _decide_marker(len(x))
        ax.plot(x, y, linestyle="none", marker=".", markersize=size,
                alpha=alpha, label=label)
        plotted += len(x)
    if plotted == 0:
        ax.text(0.5, 0.5, "no unflagged data to plot", ha="center",
                va="center", transform=ax.transAxes)
    if any(label and len(x) for label, x, y in plot.series):
        ax.legend(markerscale=3)
    ax.grid(True, alpha=0.25)
    ax.set_xlabel(plot.xlabel)
    ax.set_ylabel(plot.ylabel)
    ax.set_title(title or f"{plot.name}: {plot.ylabel} vs {plot.xlabel}")
    if plot.x_is_time:
        ax.xaxis_date()
        fig.autofmt_xdate()
    _style_antenna_axis(ax, plot)
    fig.tight_layout()
    try:
        fig.savefig(plotfile)
    except ValueError as exc:
        ext = os.path.splitext(plotfile)[1] or "(none)"
        raise click.ClickException(
            f"{plotfile}: cannot write a {ext} plot ({exc}); use a .png,"
            " .pdf, .svg, .ps or .eps extension"
        )
    finally:
        plt.close(fig)


def collect_and_render(ms, plotfile, xaxis="", yaxis="", corr="", field="",
                       spw="", scan="", data_column="DATA",
                       max_points=DEFAULT_MAX_POINTS, show_flagged=False,
                       title="", overwrite=False):
    """The whole command: collect, then write; returns the PlotData."""
    if not os.path.exists(str(ms)):
        raise click.ClickException(f"{ms}: no such measurement set or caltable")
    if os.path.exists(plotfile) and not overwrite:
        raise click.ClickException(
            f"{plotfile}: already exists (pass --overwrite to replace it)"
        )
    plot = collect(
        ms, xaxis=xaxis, yaxis=yaxis, corr=corr, field=field, spw=spw,
        scan=scan, data_column=data_column, max_points=max_points,
        show_flagged=show_flagged,
    )
    render(plot, plotfile, title=title)
    note = f", {plot.n_flagged} flagged excluded" if plot.n_flagged else ""
    print(
        f"Wrote {plotfile}: {plot.ylabel} vs {plot.xlabel}"
        f" ({plot.n_points} points{note})"
    )
    return plot


@click.command("skarabina-plotms")
@click.option("--ms", required=True, help="Input measurement set or caltable")
@click.option(
    "--plotfile", required=True,
    help="Output plot file; the extension selects the format"
    " (.png, .pdf, .svg, .ps, .eps)",
)
@click.option(
    "--overwrite", is_flag=True, default=False,
    help="Overwrite the plot file if it already exists",
)
@click.option(
    "--xaxis", default="",
    help="X axis (blank for plotms' default, time): time, channel,"
    " frequency, uvdist, uvwave, scan, field, spw, antenna, baseline,"
    " interval, row, u, v, w",
)
@click.option(
    "--yaxis", default="",
    help="Y axis (blank for plotms' default, amplitude): amp, phase, real,"
    " imag, wt (weight), snr (caltables only)",
)
@click.option(
    "--corr", default="",
    help="Correlations to plot, comma-separated names or 0-based indices"
    " (blank for all)",
)
@click.option(
    "--field", default="",
    help="Fields to plot, comma-separated names or ids (blank for all)",
)
@click.option(
    "--spw", default="",
    help="Spectral windows to plot, comma-separated ids (blank for all)",
)
@click.option(
    "--scan", default="",
    help="Scans to plot, comma-separated numbers (blank for all;"
    " measurement sets only)",
)
@click.option(
    "--data-column", "data_column", default="DATA",
    help="Measurement-set column to plot: DATA, CORRECTED or MODEL",
)
@click.option(
    "--max-points", "max_points", type=int, default=DEFAULT_MAX_POINTS,
    help="Decimate to about this many points per correlation"
    " (0 disables decimation)",
)
@click.option(
    "--show-flagged", "show_flagged", is_flag=True, default=False,
    help="Include flagged data (plotms leaves flagged points out)",
)
@click.option(
    "--title", default="",
    help="Plot title (blank for '<table>: <y> vs <x>')",
)
def main(ms, plotfile, overwrite, xaxis, yaxis, corr, field, spw, scan,
         data_column, max_points, show_flagged, title):
    """Plot a measurement set or a caltable.

    A matplotlib stand-in for casaplotms (x86_64-only), with plotms'
    defaults: x = time, y = amplitude, flagged data left out.  Typical use,
    the stage-1 gain-table plots of the meerkat_imaging pipeline:

        skarabina-plotms --ms out/multi.G0 --plotfile out/cal_G0.pdf --overwrite
    """
    collect_and_render(
        ms, plotfile, xaxis=xaxis, yaxis=yaxis, corr=corr, field=field,
        spw=spw, scan=scan, data_column=data_column, max_points=max_points,
        show_flagged=show_flagged, title=title, overwrite=overwrite,
    )


if __name__ == "__main__":
    main()
