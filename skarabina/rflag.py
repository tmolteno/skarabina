# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""RFlag: outlier flagging from sliding-window statistics.

A reimplementation of the algorithm CASA's ``flagdata(mode='rflag')`` uses,
which Eric Greisen developed in AIPS (31DEC11); the CASA User Reference
§3.4.2.8 describes it, and the AIPS Cookbook Appendix E.5 is the original.

The idea
--------

TFCrop fits the bandpass and flags what does not follow it.  RFlag asks a
different question: is the *scatter* here unusual?  RFI raises the local noise,
and it does so either as a burst in time or as a spike in frequency, so the
algorithm looks for both and needs no model of the band shape at all:

1. **Time analysis, per channel.**  Slide a window of ``winsize`` integrations
   along time and measure the local r.m.s. of the real and imaginary parts in
   each position.  Take the median r.m.s. over the window positions and the
   median absolute deviation from it, then flag where the local r.m.s. sits more
   than ``timedevscale`` deviations from that median.
2. **Spectral analysis, per timestep.**  Average the real and imaginary parts
   across channels, measure each channel's deviation from that average, and flag
   where the deviation exceeds ``freqdevscale`` times the median deviation.

Both steps are medians, which is the whole point: a mean would be dragged
around by the very RFI being looked for, and the algorithm would then miss it.
"""

import numpy as np

from skarabina.baselines import baseline_noise
from skarabina.nanstats import nanmedian as _nanmedian

#: Defaults, all from CASA's ``flagdata`` so a recipe transfers unchanged.
DEFAULTS = {
    "winsize": 3,
    "timedev": None,
    "freqdev": None,
    "timedevscale": 5.0,
    "freqdevscale": 5.0,
    "spectralmax": 1.0e6,
    "spectralmin": 0.0,
}

#: Degeneracy guard: the threshold used when the measured deviation is exactly
#: zero, as a fraction of the plane's median level.  A running median
#: sits *exactly* on a smooth band, so most residuals are identically zero and
#: their median deviation is zero -- a scale that no quantile estimator can
#: recover.  Zero has a sound meaning here (the band is smoother than the
#: arithmetic can resolve, so any departure is an outlier), but a threshold of
#: literally zero would also flag the one-channel blip the clipped kernel leaves
#: at each end of the band.  This guard is deliberately far below any real noise
#: level, so it only ever decides that degenerate case; where there is genuine
#: fine structure the measured deviation is orders of magnitude larger and is
#: used untouched.
DEGENERACY_FLOOR = 1.0e-6

#: What a flagged sample is replaced with before the statistics are taken.
#: NaN in *both* parts: ``np.where(flagged, np.nan, plane)`` on complex data
#: gives ``nan+0j``, so the imaginary part of every flagged sample was counted
#: as a zero -- depressing the local scatter and, in the spectral step, pulling
#: the measured deviation of heavily flagged data down by an order of
#: magnitude, after which nearly every unflagged sample exceeded the threshold.
_MISSING = complex(np.nan, np.nan)

#: Rough cap on the float64 values a temporary may hold while one plane is
#: processed.  The time step works on groups of channels and the spectral step
#: on groups of rows so that the vectorised arithmetic -- several full-size
#: temporaries per step -- stays a bounded multiple of this, however wide the
#: band or long the dask chunk.  1M values is 8 MB per temporary.
GROUP_VALUES = 1 << 20


def robust_scale(values):
    """A robust measure of how large a typical value is: median(|values|).

    The median of the absolute values, *not* the median absolute deviation from
    the median.  The two agree when the values are centred on zero, but the MAD
    is measured about the median and so is inflated by the outliers themselves
    once they are more than a small fraction of the sample -- which is exactly
    the case here, where the quantity being scaled is a residual whose typical
    value is zero and whose few large values are the RFI.

    Measured on a time burst three channels wide: the median of |residual| is
    0.0011 against a burst of 0.72, whereas the MAD about the median comes out
    0.0021 because the burst has pulled the median away from zero.  Scaled by
    five, the first puts the threshold at 0.0056 and flags the burst, and the
    second puts it at 0.011 and flags nothing at all.
    """
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return 0.0
    return float(np.median(np.abs(values)))


def _window_bounds(n_samples, winsize):
    """(starts, stops) index arrays for a sliding centred window of
    ``winsize`` over ``n_samples``.

    The window is centred on each sample, so there is one position per sample
    and the flagged positions line up with the data.  At the ends it is
    clipped rather than wrapped or shrunk, so the first and last integration
    are judged with as much data as exists rather than with a window that has
    been allowed to run off the edge.

    Computed as one vectorised ``arange`` (no Python list) and cached per
    ``(n_samples, winsize)``: a run reuses the same block length and window
    sizes over and over, and the original rebuilt a list of ``n_samples``
    tuples on every call -- tens of milliseconds per call, tens of seconds
    per block -- for indices that never change.
    """
    key = (n_samples, winsize)
    cached = _WINDOW_CACHE.get(key)
    if cached is not None:
        return cached
    half = winsize // 2
    i = np.arange(n_samples)
    starts = np.maximum(i - half, 0)
    stops = np.minimum(i - half + winsize, n_samples)
    # A single run works over a handful of (length, winsize) pairs; cap the
    # cache so a long-lived process cannot grow it without bound.
    if len(_WINDOW_CACHE) > 32:
        _WINDOW_CACHE.clear()
    _WINDOW_CACHE[key] = (starts, stops)
    return starts, stops


#: Cache of ``(n_samples, winsize) -> (starts, stops)`` window-index arrays.
#: Used by ``_window_sums``, so the bounds for a given block length and window
#: size are computed once per run, not per call.
_WINDOW_CACHE: dict = {}


def _window_starts(n_samples, winsize):
    """Start indices for a sliding window of ``winsize`` over ``n_samples``.

    Retained as a thin wrapper over :func:`_window_bounds` for callers that
    iterate ``(start, stop)`` pairs.
    """
    starts, stops = _window_bounds(n_samples, winsize)
    return list(zip(starts.tolist(), stops.tolist()))


def local_rms(values, winsize, windows=None):
    """Local scatter of the real and imaginary parts, over a sliding window.

    The scatter is measured *about the window's own mean*, not about zero::

        sqrt(mean((re - mean(re))^2 + (im - mean(im))^2))

    That distinction is the whole reason the two halves of the algorithm fit
    together.  The r.m.s. about zero of a visibility is dominated by the signal
    -- a source of 10 Jy in a 0.05 Jy noise floor gives an r.m.s. of 10 -- so it
    could never be compared with a noise estimate, and a supplied ``timedev``
    would flag everything in sight.  Measured from the window mean the same data
    gives ~0.05, which is what ``timedev`` means and what
    ``timedevscale * timedev`` is a threshold on.  The two parts are combined in
    quadrature, which is the r.m.s. of the complex samples so measured; taking
    the r.m.s. of the magnitude instead would bias it high, because the
    magnitude of Gaussian noise follows a Rayleigh distribution.

    Computed from prefix sums rather than a pass per window.  The straightforward
    loop builds two small arrays for every window -- 25 000 of them for a
    twenty-five-thousand-row block -- and measured 71 s of the 71 s the whole
    plane took, against well under a second here.

    ``values`` may be 1-D, or 2-D with the window sliding along the first axis
    for each column.  NaN, where the data is already flagged, is left out of the
    statistic rather than counted as zero, which would depress the scatter and
    hide the very thing being looked for.
    """
    values = np.asarray(values)
    if np.iscomplexobj(values):
        parts = (np.real(values).astype(float), np.imag(values).astype(float))
    else:
        parts = (values.astype(float),)
    if values.ndim == 1:
        parts = tuple(part[:, None] for part in parts)

    counts = None
    fewest = None
    sum_squares = None
    for part in parts:
        usable = np.isfinite(part)
        filled = np.where(usable, part, 0.0)
        # The count is of the *usable* samples in each window.  Taking the
        # window's length instead -- as this did until 1.0.6 -- counts every
        # flagged sample as a visibility of zero: against a signal of ~10 a
        # window with one flagged sample shows a scatter of ~5, and with 30 %
        # of samples flagged two windows in three hold one, which lifts the
        # median, and so the threshold, until the time step flags nothing.
        _, count = _window_sums(usable.astype(float), winsize, windows)
        _, total = _window_sums(filled, winsize, windows)
        _, total_sq = _window_sums(filled * filled, winsize, windows)
        counts = count if counts is None else counts + count
        fewest = count if fewest is None else np.minimum(fewest, count)
        sum_squares = total_sq if sum_squares is None else sum_squares + total_sq
        # sum((x - mean)^2) = sum(x^2) - count * mean^2, per part.
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(count > 0, total / np.maximum(count, 1), 0.0)
        sum_squares = sum_squares - count * mean * mean

    # A window with fewer than two usable samples has no scatter to measure:
    # one sample about its own mean is exactly zero, and on heavily flagged
    # data enough of those zeros would pull the median -- and the threshold --
    # down onto the noise.  They are NaN, which the statistics leave out.
    with np.errstate(invalid="ignore", divide="ignore"):
        scatter = np.where(
            fewest >= 2, sum_squares / np.maximum(counts, 1), np.nan
        )
        # A tiny negative value is cancellation in the subtraction above.
        out = np.sqrt(np.maximum(scatter, 0.0))
    return out[:, 0] if values.ndim == 1 else out


def _window_sums(values, winsize, windows=None):
    """Windowed sum and count of a finite-masked array, from prefix sums.

    Returns ``(count, total)`` with the window centred on each sample and
    clipped at the ends, so every sample has a window and none runs off the
    edge.  A NaN counts as absent from both, which is what lets a caller pass
    data with flagged samples masked out.  ``windows``, a ``(starts, stops)``
    pair, overrides the centred windows -- how a window is kept inside one
    baseline's rows (see :func:`_baseline_windows`).
    """
    starts, stops = windows if windows is not None \
        else _window_bounds(values.shape[0], winsize)
    padded = np.zeros((values.shape[0] + 1,) + values.shape[1:], dtype=float)
    np.cumsum(values, axis=0, out=padded[1:])
    total = padded[stops] - padded[starts]
    count = (stops - starts).astype(float)
    if values.ndim > 1:
        count = count.reshape((-1,) + (1,) * (values.ndim - 1))
    return count, total


def neighbour_residual(level, span=1):
    """Departure of every channel from the median of its neighbouring channels.

    Works on a 1-D spectrum or a 2-D ``(time, chan)`` block, the median being
    taken along the last axis.  This is the spectral step's deviation: a narrow
    feature stands out against the channels beside it whether or not the band as
    a whole slopes, so a smooth bandpass and a genuine spike are separated
    without modelling the band.  Taking it per timestep rather than from the
    time-averaged band is what keeps a burst confined to its own rows -- a
    channel that is bright for five integrations out of two hundred is invisible
    in the average but obvious in the five rows where it happens.

    A span is widened where a channel has no neighbour, and a channel with no
    usable comparison at all -- a band of three channels -- is left as NaN and
    so is never flagged for lack of evidence.
    """
    level = np.asarray(level, dtype=float)
    was_1d = level.ndim == 1
    if was_1d:
        level = level[None, :]
    out = np.full(level.shape, np.nan)
    # Every timestep is independent, so the rows are taken a group at a time:
    # the neighbour stack is six values per sample at the widest span, and on a
    # whole dask chunk that would be the largest array in the run.
    rows = max(1, GROUP_VALUES // max(1, 6 * level.shape[-1]))
    for start in range(0, level.shape[0], rows):
        stop = start + rows
        out[start:stop] = _neighbour_residual_rows(level[start:stop], span)
    return out[0] if was_1d else out


def _neighbour_residual_rows(level, span):
    """:func:`neighbour_residual` of a 2-D ``(time, chan)`` group of rows."""
    out = np.full(level.shape, np.nan)
    nchan = level.shape[-1]
    usable_level = np.isfinite(level)
    for width in range(span, 4):
        unresolved = ~np.isfinite(out)
        if not unresolved.any():
            break
        # Channels at the band edges have no full window at this width, so they
        # stay NaN; only channels width .. nchan-width-1 get a reference.
        inner = nchan - 2 * width
        if inner <= 0:
            continue
        # The 2*width neighbours (centre excluded) of every inner channel, as
        # shifted views stacked on a new last axis, and their per-timestep
        # median in one call over the whole (time, chan, 2*w) stack.
        offsets = [*range(-width, 0), *range(1, width + 1)]
        stacked = np.stack(
            [level[:, width + offset:width + offset + inner]
             for offset in offsets],
            axis=-1,
        )
        # A neighbour column that is entirely flagged is normal on real data;
        # the median is then NaN and the sample is simply left unflagged.
        reference = np.full(level.shape, np.nan)
        reference[:, width:width + inner] = _nanmedian(stacked, axis=-1)
        usable = unresolved & np.isfinite(reference) & usable_level
        out[usable] = level[usable] - reference[usable]
    return out


def _time_thresholds(local, timedev, timedevscale, floor):
    """Threshold each channel's local r.m.s. is compared against.

    Two ways to arrive at it, both described by CASA:

    * ``timedev`` given: the threshold is simply ``timedevscale * timedev``.  A
      noise estimate is supplied (or was calculated on an earlier pass) and used
      as-is, which is what makes the two-pass workflow work.
    * ``timedev`` absent: the threshold is measured from the channel's own local
      r.m.s. -- ``timedevscale * (median + median deviation)``.  ``+ median
      deviation`` rather than ``* sigma`` because that is what the published
      description says, and it makes the threshold an upper bound on the
      *typical* scatter rather than a multiple of it.

    Vectorised over channels: ``local`` is ``(time, chan)`` and the result is
    one threshold per channel, NaN for a channel with no usable window.  The
    scale is ``robust_scale`` of the channel's finite local r.m.s.; those are
    never negative, so it is simply their median.
    """
    usable = np.isfinite(local).any(axis=0)
    if timedev is not None:
        threshold = np.full(
            local.shape[1], max(float(timedevscale) * float(timedev), floor)
        )
    else:
        threshold = np.maximum(
            float(timedevscale) * _nanmedian(local, axis=0), floor
        )
    return np.where(usable, threshold, np.nan)


def _spectral_step(plane, flagged, freqdev, freqdevscale, spectralmin,
                   spectralmax, floor, scale=None):
    """Spectral analysis: flag channels whose level departs from the band.

    Each channel is reduced to one number -- the mean of its real and imaginary
    parts over the chunk -- and the band's smooth shape is removed before the
    scatter is measured.  The smooth part is a running median over a small
    number of channels, which the bandpass passes through and a narrow RFI spike
    does not; what is left is the fine structure, and the median deviation of
    that is the quantity ``freqdev`` means and ``freqdevscale`` multiplies.

    Comparing each channel with its neighbours rather than with the band as a
    whole is not optional.  Measuring the deviation straight from the channel
    levels makes the threshold follow the *bandpass*: on a 64 % peak-to-peak band
    the median deviation is 2.2 Jy where the noise is 0.05 Jy, so the threshold
    comes out 44x too loose and narrow-band RFI goes unflagged.  That is also why
    CASA notes that the step "depends on having a relatively-flat bandshape", and
    it stops being true as soon as the band is uncalibrated -- which is the
    situation RFlag exists for.

    Working on the channel means rather than per timestep is what makes this a
    *spectral* test: it finds the channel that is bright compared with its
    neighbours whether or not that brightness varies with time.

    ``spectralmin``/``spectralmax`` bound the measured deviation, and an
    excursion flags the whole spectrum.  Below ``spectralmin`` the band is
    smoother than it should be -- the signature of a correlator or a model gone
    flat -- and above ``spectralmax`` it is so rough that no channel can be
    trusted.

    A pre-existing flag is excluded from the statistics, as for the time step.

    ``scale``, one noise level per row, is given when the rows are several
    baselines (see :func:`rflag_plane`): the measured deviation and the
    threshold are then taken on the residuals in units of each row's noise, so
    one threshold serves every baseline, and a noisier baseline is not flagged
    for its noise.  ``spectralmax``/``spectralmin`` stay in data units.
    """
    values = np.where(flagged, _MISSING, plane)
    # Each sample against its spectral neighbours, in the real and imaginary
    # parts separately, so a sample cannot hide behind a neighbour that happens
    # to be clean in one part only.  Where neither part has a comparison -- the
    # channels at the very ends of the band -- the deviation stays NaN and is
    # left unflagged rather than treated as zero.
    #
    # The parts are taken one after the other and combined in place: every
    # array here is the size of the whole plane, and stacking both parts and
    # their absolute values made the spectral step ~5x the plane's own size.
    residual = neighbour_residual(np.real(values))
    np.abs(residual, out=residual)
    other = neighbour_residual(np.imag(values))
    del values
    np.abs(other, out=other)
    # fmax takes the larger of the two and ignores a NaN in either one, so a
    # sample with no comparison in both parts stays NaN.
    np.fmax(residual, other, out=residual)
    del other
    flagged_out = flagged.copy()
    usable = np.isfinite(residual)
    if np.count_nonzero(usable) < 3:
        return flagged_out, np.nan

    # robust_scale of values that are already finite and non-negative: their
    # median, taken on a copy that np.median may reorder in place.
    deviation = float(np.median(residual[usable], overwrite_input=True))
    if deviation > spectralmax:
        return np.ones_like(flagged), deviation
    if deviation < spectralmin:
        return flagged_out, deviation

    if freqdev is not None:
        threshold = max(float(freqdevscale) * float(freqdev), floor)
    elif scale is not None:
        residual /= scale[:, None]
        threshold = float(freqdevscale) * float(
            np.median(residual[usable], overwrite_input=True)
        )
    else:
        threshold = float(freqdevscale) * deviation
    flagged_out |= (residual > threshold) & usable
    return flagged_out, deviation


def _time_step(plane, flagged, params, floor, groups=None, scale=None):
    """Time analysis: flag channels whose local scatter is anomalous.

    Every channel is independent, so the channels are taken a group at a time
    and each group is done in one vectorised pass (the per-channel loop this
    replaces made ~10 numpy calls per channel per plane).  The group is sized
    so that the handful of ``(time, group)`` float temporaries stays bounded
    whatever the length of the dask chunk.

    ``groups``, a ``(group_start, group_stop)`` pair per row, clips every
    window to its row's group -- one baseline's rows, sorted into time order --
    so a window never spans two baselines.  ``scale``, one noise level per row,
    puts the local r.m.s. in units of each row's noise before the threshold is
    measured, so the threshold is pooled over every baseline, as CASA pools
    its statistics over all baselines and timesteps.  A supplied ``timedev``
    stays in data units.
    """
    n_time, n_chan = plane.shape
    flagged_out = flagged.copy()
    thresholds = np.full(n_chan, np.nan)
    if n_time == 0:
        return flagged_out, thresholds
    # A suspect at s puts the window [s - half, s - half + winsize) under
    # suspicion, clipped to the plane.  So timestep t is flagged when a suspect
    # lies in [t - (winsize - 1 - half), t + half]: a running count of the
    # suspects over that span, from one cumulative sum.  Windows overlap
    # wherever suspects are adjacent; the count takes their union for free.
    half = params.winsize // 2
    t = np.arange(n_time)
    low, high = (0, n_time) if groups is None else groups
    span_start = np.clip(t - (params.winsize - 1 - half), low, high)
    span_stop = np.clip(t + half + 1, low, high)
    windows = None
    if groups is not None:
        # The same clipped, centred window as _window_bounds, inside a group.
        windows = (np.maximum(t - half, low),
                   np.minimum(t - half + params.winsize, high))
    normalise = scale is not None and params.timedev is None
    unit_floor = floor / float(np.median(scale)) if normalise else floor

    group = max(1, GROUP_VALUES // n_time)
    for first in range(0, n_chan, group):
        chans = slice(first, min(first + group, n_chan))
        values = np.where(flagged[:, chans], _MISSING, plane[:, chans])
        local = local_rms(values, params.winsize, windows)
        if normalise:
            local = local / scale[:, None]
        threshold = _time_thresholds(
            local, params.timedev, params.timedevscale, unit_floor
        )
        thresholds[chans] = threshold * (float(np.median(scale)) if normalise else 1.0)
        # A channel with no usable threshold is left alone.  Elsewhere, where
        # the local r.m.s. exceeds the threshold the scatter there cannot be
        # explained by the channel's typical noise, so every timestep in that
        # window is suspect.
        active = np.isfinite(threshold) & (threshold > 0)
        with np.errstate(invalid="ignore"):
            suspect = (local > threshold[None, :]) & active[None, :]
        if not suspect.any():
            continue
        running = np.zeros((n_time + 1, suspect.shape[1]), dtype=np.int32)
        np.cumsum(suspect, axis=0, out=running[1:])
        flagged_out[:, chans] |= running[span_stop] > running[span_start]
    return flagged_out, thresholds


def rflag_plane(plane, params, flagged=None, baselines=None):
    """Run RFlag on one ``(time, chan)`` plane of complex visibilities.

    ``plane`` holds the visibilities of one correlation; ``flagged`` the flags
    already set on it.  Returns ``(flag, stats)``, where ``flag`` is the
    complete flag plane -- pre-existing flags included, since the caller needs
    to know what to write back -- and ``stats`` carries the thresholds the
    algorithm derived, for reporting.

    Without ``baselines`` the rows are taken to be one baseline's timesteps.
    A measurement-set chunk is many baselines interleaved, and ``baselines``
    (a :class:`skarabina.baselines.Baselines` for the rows) says which: the
    rows are then sorted into baselines, each sliding window stays inside one
    baseline's time series, and both steps measure their threshold in units of
    each baseline's noise as predicted by the per-antenna model, so a single
    threshold serves the whole chunk (doc/RFLAG.md).
    """
    if baselines is not None:
        return _rflag_baselines(plane, params, flagged, baselines)
    return _rflag(plane, params, flagged)


def _rflag_baselines(plane, params, flagged, baselines):
    """:func:`rflag_plane` over a chunk of interleaved baselines."""
    plane = np.asarray(plane)
    flagged = np.zeros(plane.shape, dtype=bool) if flagged is None \
        else np.asarray(flagged, dtype=bool)
    scale = baseline_noise(plane, flagged | ~np.isfinite(plane), baselines)
    usable = np.isfinite(scale) & (scale > 0)
    scale = np.where(usable, scale, np.median(scale[usable]) if usable.any() else 1.0)
    order = baselines.order
    flag, stats = _rflag(
        plane[order], params, flagged[order],
        groups=(baselines.group_start, baselines.group_stop), scale=scale[order],
    )
    out = np.empty_like(flag)
    out[order] = flag
    return out, stats


def _rflag(plane, params, flagged, groups=None, scale=None):
    """The algorithm proper, on rows already in (baseline, time) order."""
    plane = np.asarray(plane)
    if flagged is None:
        flagged = np.zeros(plane.shape, dtype=bool)
    else:
        flagged = np.asarray(flagged, dtype=bool)
    flagged = flagged | ~np.isfinite(plane)
    pre_existing = flagged.copy()

    finite = np.abs(plane[np.isfinite(plane)])
    floor = DEGENERACY_FLOOR * (float(np.median(finite)) if finite.size else 0.0)

    flagged, time_thresholds = _time_step(
        plane, flagged, params, floor, groups=groups, scale=scale
    )
    flagged, freq_deviation = _spectral_step(
        plane, flagged, params.freqdev, params.freqdevscale,
        params.spectralmin, params.spectralmax, floor, scale=scale,
    )

    finite_thresholds = time_thresholds[np.isfinite(time_thresholds)]
    stats = {
        "new": int(np.count_nonzero(flagged & ~pre_existing)),
        "total": int(flagged.size),
        "pre_existing": int(np.count_nonzero(pre_existing)),
        "time_threshold_median": (
            float(np.median(finite_thresholds)) if finite_thresholds.size
            else np.nan
        ),
        "freq_deviation": float(freq_deviation),
    }
    return flagged, stats


class RFlagParams:
    """Validated parameters for one ``rflag`` operation.

    Names and defaults are CASA's, so a recipe written for
    ``flagdata(mode='rflag')`` transfers unchanged.  ``ntime`` is deliberately
    absent, as for ``tfcrop``: the chunk the statistics are gathered over is the
    dask chunk.
    """

    __slots__ = tuple(sorted(DEFAULTS))

    DEFAULTS = DEFAULTS

    def __init__(self, **kwargs):
        unknown = set(kwargs) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(
                f"unknown rflag parameter(s): {', '.join(sorted(unknown))}."
                f" Valid parameters are {', '.join(sorted(self.DEFAULTS))}"
            )
        merged = dict(self.DEFAULTS)
        merged.update(kwargs)
        for name in self.DEFAULTS:
            setattr(self, name, merged[name])
        self._validate()

    def _validate(self):
        if isinstance(self.winsize, bool) or not isinstance(self.winsize, int) \
                or self.winsize < 1:
            raise ValueError(
                f"winsize must be a positive integer, got {self.winsize!r}"
            )
        for name in ("timedev", "freqdev"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or value <= 0:
                raise ValueError(
                    f"{name} must be a positive number or omitted, got {value!r}"
                )
        for name in ("timedevscale", "freqdevscale"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or value <= 0:
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        for name in ("spectralmax", "spectralmin"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number, got {value!r}")
        if self.spectralmin >= self.spectralmax:
            raise ValueError(
                "spectralmin must be below spectralmax, got"
                f" {self.spectralmin!r} and {self.spectralmax!r}"
            )

    def __repr__(self):
        shown = ", ".join(
            f"{name}={getattr(self, name)!r}" for name in sorted(self.DEFAULTS)
        )
        return f"RFlagParams({shown})"
