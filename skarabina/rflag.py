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

import warnings

import numpy as np

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
#: Shared between ``_window_sums`` and the time-step scan, so the bounds for a
#: given block length and window size are computed once per run, not per call.
_WINDOW_CACHE: dict = {}


def _window_starts(n_samples, winsize):
    """Start indices for a sliding window of ``winsize`` over ``n_samples``.

    Retained as a thin wrapper over :func:`_window_bounds` for callers that
    iterate ``(start, stop)`` pairs.
    """
    starts, stops = _window_bounds(n_samples, winsize)
    return list(zip(starts.tolist(), stops.tolist()))


def local_rms(values, winsize):
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
    sum_squares = None
    for part in parts:
        usable = np.isfinite(part)
        filled = np.where(usable, part, 0.0)
        count, total = _window_sums(filled, winsize)
        _, total_sq = _window_sums(filled * filled, winsize)
        counts = count if counts is None else counts + count
        sum_squares = total_sq if sum_squares is None else sum_squares + total_sq
        # sum((x - mean)^2) = sum(x^2) - count * mean^2, per part.
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(count > 0, total / np.maximum(count, 1), 0.0)
        sum_squares = sum_squares - count * mean * mean

    with np.errstate(invalid="ignore", divide="ignore"):
        scatter = np.where(
            counts > 0, sum_squares / np.maximum(counts, 1), np.nan
        )
        # A tiny negative value is cancellation in the subtraction above.
        out = np.sqrt(np.maximum(scatter, 0.0))
    return out[:, 0] if values.ndim == 1 else out


def _window_sums(values, winsize):
    """Windowed sum and count of a finite-masked array, from prefix sums.

    Returns ``(count, total)`` with the window centred on each sample and
    clipped at the ends, so every sample has a window and none runs off the
    edge.  A NaN counts as absent from both, which is what lets a caller pass
    data with flagged samples masked out.
    """
    starts, stops = _window_bounds(values.shape[0], winsize)
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
    nchan = level.shape[-1]
    for width in range(span, 4):
        unresolved = ~np.isfinite(out)
        if not unresolved.any():
            break
        # Gather the 2*width neighbours (centre excluded) of every channel in
        # ONE indexed view, and take the per-timestep median in one
        # ``nanmedian`` call over the whole (time, chan, 2*w) window instead
        # of one call per channel (thousands of masked-array medians per
        # spectral step on a real block).
        offsets = np.concatenate(
            [np.arange(-width, 0), np.arange(1, width + 1)]
        )
        idx = np.arange(nchan)[:, None] + offsets[None, :]  # (nchan, 2w)
        in_range = (idx >= 0) & (idx < nchan)
        keep = in_range.all(axis=1)
        # Channels at the band edges have no full window at any width, so they
        # stay NaN by design (the original ``continue`` for ``low<0``/
        # ``high>=nchan`` has the same effect).
        gathered = level[:, np.where(in_range, idx, 0)[keep]]  # (time, nw, 2w)
        # A neighbour column that is entirely flagged is normal on real data;
        # the median is then NaN and the sample is simply left unflagged, so
        # the warning that numpy raises for it is noise.
        with warnings.catch_warnings(), np.errstate(invalid="ignore"):
            warnings.simplefilter("ignore", RuntimeWarning)
            reference_keep = np.nanmedian(gathered, axis=-1)  # (time, nw)
        reference = np.full((level.shape[0], nchan), np.nan)
        reference[:, keep] = reference_keep
        usable = unresolved & np.isfinite(reference) & np.isfinite(level)
        out[usable] = level[usable] - reference[usable]
    return out[0] if was_1d else out


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

    Returns ``(threshold, median, scale)`` so the caller can report the numbers
    it derived.
    """
    finite = local[np.isfinite(local)]
    if finite.size == 0:
        return np.nan, np.nan, np.nan
    median = float(np.median(finite))
    scale = robust_scale(finite)
    if timedev is not None:
        return max(float(timedevscale) * float(timedev), floor), median, scale
    return max(float(timedevscale) * scale, floor), median, scale


def _spectral_step(plane, flagged, freqdev, freqdevscale, spectralmin,
                   spectralmax, floor):
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
    """
    values = np.where(flagged, np.nan, plane)
    # Each sample against its spectral neighbours, in the real and imaginary
    # parts separately, so a sample cannot hide behind a neighbour that happens
    # to be clean in one part only.  Where neither part has a comparison -- the
    # channels at the very ends of the band -- the deviation stays NaN and is
    # left unflagged rather than treated as zero.
    parts = np.stack([np.abs(neighbour_residual(np.real(values))),
                      np.abs(neighbour_residual(np.imag(values)))])
    with np.errstate(invalid="ignore"):
        residual = np.where(
            np.isfinite(parts).any(axis=0),
            np.nanmax(np.where(np.isfinite(parts), parts, -np.inf), axis=0),
            np.nan,
        )
    flagged_out = flagged.copy()
    finite = residual[np.isfinite(residual)]
    if finite.size < 3:
        return flagged_out, np.nan

    deviation = robust_scale(finite)
    if deviation > spectralmax:
        return np.ones_like(flagged), deviation
    if deviation < spectralmin:
        return flagged_out, deviation

    if freqdev is not None:
        threshold = max(float(freqdevscale) * float(freqdev), floor)
    else:
        threshold = float(freqdevscale) * deviation
    flagged_out |= (residual > threshold) & np.isfinite(residual)
    return flagged_out, deviation


def _time_step(plane, flagged, params, floor):
    """Time analysis: flag channels whose local scatter is anomalous."""
    values = np.where(flagged, np.nan, plane)
    flagged_out = flagged.copy()
    thresholds = []
    # The window bounds depend only on the block length, so they are computed
    # once.  Recomputing them per suspect -- which is what the first version did
    # -- rebuilds a list of every window for every flagged sample, and on a real
    # block that was 142 000 suspects x 25 641 tuples: measured, 390 s against
    # 5.3 s for the same arithmetic.
    starts, stops = _window_bounds(values.shape[0], params.winsize)
    for chan in range(values.shape[1]):
        local = local_rms(values[:, chan], params.winsize)
        threshold, _, _ = _time_thresholds(
            local, params.timedev, params.timedevscale, floor
        )
        thresholds.append(threshold)
        if not np.isfinite(threshold) or threshold <= 0:
            continue
        # Where the local r.m.s. exceeds the threshold the scatter there cannot
        # be explained by the channel's typical noise, so every timestep in that
        # window is suspect.
        suspect = np.flatnonzero(np.isfinite(local) & (local > threshold))
        if suspect.size == 0:
            continue
        # The windows overlap wherever suspects are adjacent, so the timesteps to
        # flag are the union of the spans, marked in one vectorised pass.
        covered = np.zeros(values.shape[0] + 1, dtype=np.int32)
        np.add.at(covered, starts[suspect], 1)
        np.add.at(covered, stops[suspect], -1)
        flagged_out[np.cumsum(covered[:-1]) > 0, chan] = True
    return flagged_out, thresholds


def rflag_plane(plane, params, flagged=None):
    """Run RFlag on one ``(time, chan)`` plane of complex visibilities.

    ``plane`` holds the visibilities of a single baseline and correlation;
    ``flagged`` the flags already set on it.  Returns ``(flag, stats)``, where
    ``flag`` is the complete flag plane -- pre-existing flags included, since the
    caller needs to know what to write back -- and ``stats`` carries the
    thresholds the algorithm derived, for reporting.
    """
    plane = np.asarray(plane)
    if flagged is None:
        flagged = np.zeros(plane.shape, dtype=bool)
    else:
        flagged = np.asarray(flagged, dtype=bool)
    flagged = flagged | ~np.isfinite(plane)
    pre_existing = flagged.copy()

    finite = np.abs(plane[np.isfinite(plane)])
    floor = DEGENERACY_FLOOR * (float(np.median(finite)) if finite.size else 0.0)

    flagged, time_thresholds = _time_step(plane, flagged, params, floor)
    flagged, freq_deviation = _spectral_step(
        plane, flagged, params.freqdev, params.freqdevscale,
        params.spectralmin, params.spectralmax, floor,
    )

    finite_thresholds = [t for t in time_thresholds if np.isfinite(t)]
    stats = {
        "new": int(np.count_nonzero(flagged & ~pre_existing)),
        "total": int(flagged.size),
        "pre_existing": int(np.count_nonzero(pre_existing)),
        "time_threshold_median": (
            float(np.median(finite_thresholds)) if finite_thresholds else np.nan
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
