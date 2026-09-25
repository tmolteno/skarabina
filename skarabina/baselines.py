# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Per-baseline bookkeeping for the auto-flaggers.

A dask row chunk of a measurement set is not one baseline's time series.  An
MS is written time-major, so consecutive rows are the *different baselines* of
one integration -- ~1900 of them for MeerKAT -- and a 10 000-row chunk holds
about five integrations of each.  An algorithm that treats the rows of a chunk
as successive timesteps of one signal compares unrelated baselines with each
other, and every baseline's own level (source amplitude, uncalibrated gains)
then looks like an outlier.  Measured on a MeerKAT L-band calibrator scan, that
alone made tfcrop flag 27 % of the RFI-free band, against 4.3 % on proper
single-baseline planes (doc/RFLAG.md).

:class:`Baselines` groups a chunk's rows by baseline (and scan, when known) so
the algorithms can keep each baseline's level and time series apart, and
:func:`antenna_noise_model` predicts each baseline's noise from per-antenna
factors fitted over the whole chunk.  Thermal noise on baseline i-j scales as
sqrt(SEFD_i SEFD_j), times |g_i g_j| for data that are not yet calibrated: on
the same scan one factor per antenna predicted every baseline's noise to 1.5 %,
where baseline length explained almost nothing.  A model fitted over ~1800
baselines is also not fooled by one baseline whose own estimate is inflated by
RFI.
"""

import numpy as np

from skarabina.nanstats import nanmedian

#: Most channel pairs a row contributes to its noise estimate.  The estimate is
#: a median, so a few hundred samples per row already pin it down, and a strided
#: subset keeps the cost independent of the band width.
NOISE_SAMPLES_PER_ROW = 256

#: Robust-sigma cut used to drop baselines the antenna model does not fit (RFI,
#: a bad correlator input) before the model is refitted without them.
MODEL_REJECT_SIGMA = 3.0


class Baselines:
    """The baseline (and scan) of every row of a chunk, grouped.

    ``labels[row]`` numbers the groups 0..n-1; ``antenna1``/``antenna2`` give
    each group's antennas.  ``order`` sorts the rows into contiguous groups,
    keeping each group's rows in their original -- time -- order, and
    ``group_start``/``group_stop`` give, for every *sorted* position, the span
    of its group, which is what keeps a sliding window inside one baseline.
    """

    def __init__(self, antenna1, antenna2, scan=None):
        antenna1 = np.asarray(antenna1, dtype=np.int64)
        antenna2 = np.asarray(antenna2, dtype=np.int64)
        keys = [antenna1, antenna2]
        if scan is not None:
            keys.insert(0, np.asarray(scan, dtype=np.int64))
        stacked = np.stack(keys, axis=1)
        unique, self.labels = np.unique(stacked, axis=0, return_inverse=True)
        self.labels = self.labels.reshape(-1)
        self.antenna1, self.antenna2 = unique[:, -2], unique[:, -1]
        self.count = unique.shape[0]
        self.order = np.argsort(self.labels, kind="stable")
        sizes = np.bincount(self.labels, minlength=self.count)
        starts = np.cumsum(sizes) - sizes
        sorted_labels = self.labels[self.order]
        self.group_start = starts[sorted_labels]
        self.group_stop = self.group_start + sizes[sorted_labels]
        self.sizes = sizes

    @property
    def nrows(self):
        return self.labels.size


def row_noise(values, flagged):
    """A robust noise estimate for every row: the scatter of one-channel steps.

    The difference of adjacent channels cancels anything smooth in frequency --
    the bandpass, the source -- and leaves the noise, sqrt(2) times over.  For
    complex values the real and imaginary parts are pooled; for amplitudes the
    steps themselves are used.  At most :data:`NOISE_SAMPLES_PER_ROW` pairs per
    row, evenly strided, and only pairs whose samples are both unflagged.
    Returns NaN for a row with fewer than two usable pairs.
    """
    nchan = values.shape[1]
    if nchan < 2:
        return np.full(values.shape[0], np.nan)
    stride = max(1, (nchan - 1) // NOISE_SAMPLES_PER_ROW)
    left = np.arange(0, nchan - 1, stride)
    usable = ~flagged[:, left] & ~flagged[:, left + 1]
    step = values[:, left + 1] - values[:, left]
    if np.iscomplexobj(step):
        parts = np.concatenate([step.real, step.imag], axis=1).astype(float)
        usable = np.concatenate([usable, usable], axis=1)
    else:
        parts = np.asarray(step, dtype=float)
    parts = np.where(usable & np.isfinite(parts), parts, np.nan)
    centre = nanmedian(parts, axis=1)
    sigma = 1.4826 * nanmedian(np.abs(parts - centre[:, None]), axis=1) / np.sqrt(2)
    return np.where(np.count_nonzero(~np.isnan(parts), axis=1) >= 2, sigma, np.nan)


def group_median(values, baselines):
    """The median of ``values`` (one per row) over each group's rows, NaN-aware."""
    sorted_values = np.asarray(values, dtype=float)[baselines.order]
    width = int(baselines.sizes.max()) if baselines.count else 0
    padded = np.full((baselines.count, max(1, width)), np.nan)
    position = np.arange(baselines.nrows) - baselines.group_start
    padded[baselines.labels[baselines.order], position] = sorted_values
    return nanmedian(padded, axis=1)


def group_median_rows(values, baselines, budget=1 << 22):
    """Per-group, per-column median of a ``(row, column)`` array, NaN-aware.

    Returns ``(groups, columns)``.  Groups are taken a batch at a time, each
    laid out as a ``(group, row-in-group, column)`` block of at most about
    ``budget`` values, so the working set stays bounded.
    """
    values = np.asarray(values, dtype=float)
    ncol = values.shape[1]
    out = np.full((baselines.count, ncol), np.nan)
    order = baselines.order
    starts = np.cumsum(baselines.sizes) - baselines.sizes
    widest = int(baselines.sizes.max()) if baselines.count else 0
    batch = max(1, budget // max(1, widest * ncol))
    for first in range(0, baselines.count, batch):
        last = min(first + batch, baselines.count)
        width = int(baselines.sizes[first:last].max())
        block = np.full((last - first, width, ncol), np.nan)
        for group in range(first, last):
            rows = order[starts[group]:starts[group] + baselines.sizes[group]]
            block[group - first, :rows.size] = values[rows]
        out[first:last] = nanmedian(block, axis=1)
    return out


def antenna_noise_model(noise, antenna1, antenna2):
    """Each baseline's noise as predicted from per-antenna factors.

    Fits ``log noise_ij = s_i + s_j`` by least squares over the baselines with a
    usable estimate, drops those further than :data:`MODEL_REJECT_SIGMA` robust
    sigmas from the fit, and refits.  Returns the prediction for every
    baseline.  Where the model cannot be fitted -- fewer baselines than
    antennas, or a baseline with an antenna seen nowhere else -- the baseline's
    own estimate is used, and failing that the median of all of them.
    """
    noise = np.asarray(noise, dtype=float)
    usable = np.isfinite(noise) & (noise > 0)
    fallback = np.where(usable, noise, np.nanmedian(noise[usable]) if usable.any() else np.nan)
    cross = antenna1 != antenna2
    fit = usable & cross
    antennas = np.unique(np.concatenate([antenna1[fit], antenna2[fit]]))
    if fit.sum() < antennas.size + 2:
        return fallback
    column = np.searchsorted(antennas, np.stack([antenna1, antenna2]))
    column = np.where(np.isin(np.stack([antenna1, antenna2]), antennas), column, -1)
    known = (column >= 0).all(axis=0) & cross
    design = np.zeros((noise.size, antennas.size))
    rows = np.flatnonzero(known)
    np.add.at(design, (rows, column[0, rows]), 1.0)
    np.add.at(design, (rows, column[1, rows]), 1.0)
    log_noise = np.log(np.where(usable, noise, 1.0))
    keep = fit & known
    for _ in range(2):
        coef, *_ = np.linalg.lstsq(design[keep], log_noise[keep], rcond=None)
        residual = log_noise - design @ coef
        spread = 1.4826 * np.median(np.abs(residual[keep] - np.median(residual[keep])))
        if not np.isfinite(spread) or spread == 0:
            break
        refined = keep & (np.abs(residual) <= MODEL_REJECT_SIGMA * spread)
        if refined.sum() < antennas.size + 2 or refined.sum() == keep.sum():
            break
        keep = refined
    return np.where(known, np.exp(design @ coef), fallback)


def baseline_noise(values, flagged, baselines):
    """The antenna-model noise of every row's baseline (one value per row)."""
    per_group = group_median(row_noise(values, flagged), baselines)
    return antenna_noise_model(
        per_group, baselines.antenna1, baselines.antenna2
    )[baselines.labels]
