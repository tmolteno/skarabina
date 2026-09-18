# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""TFCrop: outlier flagging on the 2-D time-frequency plane.

A reimplementation of the algorithm CASA's ``flagdata(mode='tfcrop')`` uses,
described in the CASA User Reference (section 3.4.2.7) and NCRA Technical
Report 202 (Oct 2003).

The idea
--------

Radio-frequency interference shows up as outliers in the time-frequency plane
of a single baseline and correlation.  A plain amplitude clip cannot separate
it from the bandpass, because the bandpass itself is a large, smooth,
frequency-dependent gain: a threshold that catches a weak spike at the band
edge would flag the whole bright end of the band.

TFCrop therefore fits the bandpass first and flags the *residuals*:

1. Average the chunk over time to get the mean bandpass, and fit a robust
   piece-wise polynomial to it.  "Robust" matters: the fit must follow the base
   of the RFI spikes, not be dragged up by them.
2. Divide that fit out of every timestep.  The result is flat -- near 1
   wherever the band is clean -- so one threshold now means the same thing at
   the band edge and in the middle.
3. Flag points deviating from 1, iterating so that the scatter estimate is
   itself computed from the surviving points.
4. Repeat the whole thing in the other direction: average over frequency, fit
   the time series, and flag deviations from that.

Steps 1-4 run per baseline and per correlation, over chunks of time.
"""

import numpy as np

#: Fitting directions, in CASA's spelling.
FLAG_DIMENSIONS = ("freqtime", "timefreq", "freq", "time")

#: Fit functions accepted by ``timefit``/``freqfit``.
FIT_TYPES = ("line", "poly")

#: Sliding-window statistics modes.
WINDOW_STATS = ("none", "sum", "std", "both")

#: Maximum polynomial degree used for a "poly" fit.
POLY_DEGREE = 3

#: Fraction of each piece's span over which the polynomial fit is tapered down
#: at the ends.  See :func:`_edge_taper` for why this is not zero.
POLY_EDGE_TAPER = 0.25

#: Smallest weight the taper may apply, so no sample is dropped outright.
TAPER_FLOOR = 1.0e-3

#: How far past its own surviving channels a piece's polynomial may be trusted,
#: as a fraction of the span those channels cover.  See :func:`_fit_pieces`.
EXTRAPOLATION_MARGIN = 0.5

#: Floor on the rejection threshold, as a fraction of the data's own scatter
#: about zero.  A noiseless or nearly-noiseless plane -- a deterministic model,
#: or data already calibrated and averaged -- fits its own polynomial so exactly
#: that the residuals underflow, and a purely relative 3-sigma rule then rejects
#: every point.  Tying the floor to the data's scale (rather than to an absolute
#: number, which would be meaningless across Jy and K) keeps the rule inert in
#: that case while leaving it unchanged whenever real noise sets the scatter.
FIT_FLOOR_FRACTION = 1.0e-3

#: Number of robust-fit and flagging iterations, per the published algorithm.
N_FIT_ITERATIONS = 5
N_FLAG_ITERATIONS = 5

#: Threshold, in robust sigmas, used to reject outliers *while fitting*.  This
#: is deliberately tighter than the flagging cutoffs: it exists to keep RFI out
#: of the fit, not to decide the final flags.
FIT_REJECT_SIGMA = 3.0


def mad_sigma(values, axis=None):
    """Robust scatter: 1.4826 * median absolute deviation.

    The median absolute deviation is used rather than the r.m.s. because the
    r.m.s. of a spectrum containing RFI measures the RFI: a single bright spike
    inflates it enough to hide every other outlier.  The 1.4826 factor makes the
    result an unbiased estimate of sigma for Gaussian data, so the cutoffs keep
    their meaning as "sigmas".
    """
    values = np.asarray(values, dtype=float)
    if axis is None:
        # A plain scalar, not a 0-d array: callers do float() on this.
        return 1.4826 * np.median(np.abs(values - np.median(values)))
    median = np.median(values, axis=axis, keepdims=True)
    return 1.4826 * np.median(np.abs(values - median), axis=axis, keepdims=True)


def _edge_taper(x, taper=POLY_EDGE_TAPER):
    """Cosine weights that fall to ``taper`` at the two ends of ``x``.

    A polynomial fitted to a span of samples is least trustworthy at the
    outermost ones, where it is closest to extrapolating.  Weighting the ends
    down keeps those samples from dominating, while still letting them
    contribute.  A boxcar (all weights 1) was tried first, and rejected the
    first and last channel of a perfectly clean band as outliers: the cubic had
    nothing beyond them and dived away.

    The ramp occupies a fraction ``taper`` of the span at each end, so on a
    short piece the two ramps meet and every sample is weighted down evenly --
    which changes nothing, since a common factor cancels in least squares.
    """
    if x.size < 3 or taper <= 0.0:
        return np.ones(x.size)
    ramp = max(1.0, taper * x.size)
    position = np.clip(np.arange(x.size, dtype=float) / ramp, 0.0, 1.0)
    weight = 0.5 * (1.0 - np.cos(np.pi * position))
    return np.clip(weight, TAPER_FLOOR, 1.0)


def _piece_edges(n, npieces):
    """Merged edge indices splitting ``n`` samples into at most ``npieces`` spans.

    Spans are sized by sample count, so each holds roughly the same number of
    channels.  On an irregular channel grid that means unequal frequency ranges,
    which is what a fit weighted by channel count wants.
    """
    return np.unique(np.linspace(0, n, max(1, min(npieces, n)) + 1).round().astype(int))


def _poly_fit(x_eval, x_fit, y_fit, degree, taper=POLY_EDGE_TAPER):
    """Weighted least-squares polynomial through ``(x_fit, y_fit)``, evaluated at
    ``x_eval``.

    ``x`` is mapped into [-1, 1] first, using the range of the *fitting* points:
    an affine reparametrisation leaves the fitted curve unchanged but keeps the
    normal equations well conditioned when the abscissa is a channel index
    running to thousands.

    The points are weighted with :func:`_edge_taper`, so the fit is not asked to
    extrapolate past its own data at the ends of a piece.  Without this the
    outermost sample of each piece -- and so the first and last channel of the
    band -- sits on the steep flank of a cubic that has no data beyond it, and
    the robust iteration rejects it as an outlier.  The fit then reads the edge
    channels of a perfectly clean band as RFI.
    """
    x_fit = np.asarray(x_fit, dtype=float)
    span = x_fit.max() - x_fit.min()
    centre = x_fit.mean()
    if span <= 0:
        span, centre = 1.0, 0.0
    deg = min(degree, max(1, x_fit.size - 1))
    scaled = (x_fit - centre) / span
    design = np.vander(scaled, deg + 1, increasing=True)
    weights = _edge_taper(x_fit, taper)
    weighted = design * weights[:, None]
    y = np.asarray(y_fit, dtype=float)
    coeffs, *_ = np.linalg.lstsq(weighted, y * weights, rcond=None)
    return np.vander(
        (np.asarray(x_eval, dtype=float) - centre) / span, deg + 1, increasing=True
    ) @ coeffs


def robust_fit(x, y, npieces, degree, n_iterations=N_FIT_ITERATIONS,
               reject_sigma=FIT_REJECT_SIGMA):
    """Robust piece-wise polynomial fit, growing the piece count as it iterates.

    Two things iterate together, following the published description:

    * Within a piece count, the fit is re-made from the surviving points only,
      dropping those further than ``reject_sigma`` from it.  This is what makes
      the fit follow the *base* of the RFI rather than its peaks: one pass is
      already dragged up by a strong spike, but the points it then rejects let
      the next pass sit lower.
    * Between piece counts, the number of pieces grows from one toward
      ``npieces``.  Starting wide matters.  A band is smooth over most of its
      length, and a fit with too many pieces and no rejection yet will happily
      bend to follow an RFI spike, so the spike never looks like an outlier and
      is never removed.  Starting at one piece makes the first fit a single
      low-order curve that RFI cannot bend, so the outliers are obvious
      immediately; the extra pieces are then added to a fit that has already
      found them, and refine the band shape rather than the RFI.

    Growth was not in the first implementation of this module, which fixed the
    piece count from the start.  That version was measurably wrong: on a band
    with spikes straddling a piece boundary the boundary region could not be
    followed by the cubics and stayed mis-fitted by 0.31 in a band whose clean
    points fit to 0.0001.  Growing the piece count removes that error while
    still rejecting every spike.

    Returns ``(fitted, keep)`` -- the model at every ``x``, and the mask of
    points the final pass kept.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    finite = np.isfinite(y)
    if finite.sum() < 2:
        flat = np.full_like(y, np.nanmean(y) if finite.any() else 0.0)
        return flat, finite

    keep = finite.copy()
    # Scale of the data about zero, as a floor for the rejection threshold.
    threshold_floor = FIT_FLOOR_FRACTION * float(mad_sigma(np.abs(y[finite])))
    attempts = max(1, n_iterations)
    max_pieces = max(1, min(npieces, x.size))
    fitted = _fit_pieces(x, y, keep, _piece_edges(x.size, 1), degree)
    for attempt in range(attempts):
        pieces = max_pieces if max_pieces == 1 else 1 + round(
            (max_pieces - 1) * attempt / max(1, attempts - 1)
        )
        fitted = _fit_pieces(x, y, keep, _piece_edges(x.size, pieces), degree)
        residuals = y - fitted
        sigma = float(mad_sigma(residuals[keep]))
        if not np.isfinite(sigma):
            break
        limit = max(reject_sigma * sigma, threshold_floor)
        new_keep = finite & (np.abs(residuals) <= limit)
        if new_keep.sum() < 2:
            break
        converged = new_keep.sum() == keep.sum()
        keep = new_keep
        if converged:
            # Nothing more was rejected at this piece count; the remaining
            # iterations exist to add pieces, not to re-reject.
            continue
    return fitted, keep


def _fit_pieces(x, y, keep, edges, degree):
    """Fit one polynomial per piece to the kept points, evaluated across ``x``.

    Two guards keep the model from diverging, both of them learned from watching
    it diverge:

    * The degree is limited by how many kept points a piece has, not just by
      ``degree``.  A cubic fitted to five points is an interpolant, and it
      swings between them.
    * A piece is only trusted near its own data.  When the rejection iterations
      strip a piece down to a cluster of surviving channels at one end -- which
      is exactly what happens to a piece containing RFI at the other end -- the
      polynomial has nothing to say about the empty part, and extrapolating
      there is meaningless: measured, one such piece reached 395 on a band whose
      values run 7 to 16.  Those channels are interpolated between the
      neighbouring fitted regions instead, which is also what fills a piece with
      no usable points at all.  Should nothing at all be fittable, the mean of
      the kept data is used, making the residuals zero and flagging nothing
      rather than flagging everything.
    """
    model = np.full(x.size, np.nan)
    supported = np.zeros(x.size, dtype=bool)
    for start, stop in zip(edges[:-1], edges[1:]):
        sel = np.zeros(x.size, dtype=bool)
        sel[start:stop] = True
        inside = sel & keep
        chosen = np.flatnonzero(inside)
        if chosen.size < 2:
            continue
        model[start:stop] = _poly_fit(
            x[start:stop], x[chosen], y[chosen],
            _affordable_degree(chosen.size, degree),
        )
        # Trust the polynomial only near its own surviving channels.  Just past
        # the last one it is still the best local description available -- and
        # requiring the model to be exactly bracketed by data would leave the
        # first and last channel of the band unsupported, so a clean band's
        # edges would be rejected as outliers.  Far beyond them it is
        # extrapolating, which is what reached 395 on a band running 7 to 16.
        # The margin is scaled by the polynomial's own span and degree, so a
        # wiggly high-order fit is trusted less far.
        span = x[chosen].max() - x[chosen].min()
        margin = EXTRAPOLATION_MARGIN * span * max(1, degree) / 3.0
        supported[start:stop] = (
            (x[start:stop] >= x[chosen].min() - margin)
            & (x[start:stop] <= x[chosen].max() + margin)
        )

    gaps = ~supported
    if gaps.all():
        model[:] = np.nanmean(y[keep]) if keep.any() else 0.0
    elif gaps.any():
        known = np.flatnonzero(supported & np.isfinite(model))
        model[gaps] = np.interp(np.flatnonzero(gaps), known, model[known])
    return model


def _affordable_degree(npoints, degree):
    """The highest degree ``npoints`` points can support without diverging.

    Fitting a polynomial needs comfortably more points than coefficients; the
    ``+ 2`` margin is what stops a well-conditioned fit from becoming an
    interpolating one that swings wildly between the samples.
    """
    return max(1, min(degree, npoints - 2))


def _flatten(plane, template):
    """Divide a plane by a template, guarding against zero and non-finite."""
    with np.errstate(divide="ignore", invalid="ignore"):
        flat = np.asarray(plane, dtype=float) / np.asarray(template, dtype=float)
    return np.where(np.isfinite(flat), flat, 1.0)


def _safe_template(template, fallback):
    """A template usable as a divisor: finite and non-zero, else the fallback.

    A fit through an all-NaN or all-zero bandpass would otherwise turn every
    ratio into a non-finite value and flag the whole plane.
    """
    bad = ~np.isfinite(template) | (template == 0.0)
    return np.where(bad, fallback, template)


def flag_1d(values, cutoff, n_iterations=N_FLAG_ITERATIONS):
    """Flag values deviating from 1 by more than ``cutoff`` robust sigmas.

    The scatter is re-measured each iteration from the values that survived the
    previous one -- the "adaptive" scatter of the published description, and
    what makes deep flagging safe.  The first pass measures scatter over a
    spectrum still containing RFI, so it flags only the strongest outliers;
    removing those lowers the estimate, and later passes can then see weaker
    ones against a threshold that has effectively tightened.

    Returns ``(flag, sigma)`` with the final scatter estimate.
    """
    values = np.asarray(values, dtype=float)
    flagged = ~np.isfinite(values)
    sigma = np.nan
    for _ in range(max(1, n_iterations)):
        surviving = values[~flagged]
        if surviving.size < 2:
            break
        sigma = float(mad_sigma(surviving))
        if not np.isfinite(sigma) or sigma == 0.0:
            # A noiseless plane: a sigma-scaled rule has nothing to scale by,
            # so fall back to flagging exact deviations from 1.
            new = np.abs(values - 1.0) > 0.0
        else:
            new = np.abs(values - 1.0) > cutoff * sigma
        new |= ~np.isfinite(values)
        if np.array_equal(new, flagged):
            break
        flagged = new
    return flagged, sigma


def _window_pass(flat, flagged, mode, halfwin, cutoff):
    """Extra flags from sliding-window statistics around each point.

    ``mode='sum'`` approximates the LOFAR sum-threshold method and ``mode='std'``
    the AIPS ``rflag`` statistic.  Both are marked experimental in CASA's own
    documentation and are kept here for parity rather than because either is
    well founded.

    Only points already deviating from 1 contribute to the window: the
    statistics describe the outlier content of a neighbourhood, so a clean
    window has a zero sum and flags nothing, while a window straddling an RFI
    patch accumulates a large one.  Windows are clipped at the edges of the
    plane rather than wrapped or shrunk, so a spike at the band edge is judged
    with the same window size as one in the middle.
    """
    deviating = np.where(flagged, np.abs(flat - 1.0), 0.0)
    ny, nx = flat.shape
    extra = np.zeros_like(flagged)
    for y in range(ny):
        y0, y1 = max(0, y - halfwin), min(ny, y + halfwin + 1)
        for x in range(nx):
            if flagged[y, x]:
                continue
            x0, x1 = max(0, x - halfwin), min(nx, x + halfwin + 1)
            window = deviating[y0:y1, x0:x1]
            if mode in ("sum", "both"):
                if window.sum() > cutoff * np.sqrt(window.size):
                    extra[y, x] = True
                    continue
            if mode in ("std", "both"):
                spread = float(window.std())
                if spread > cutoff and abs(flat[y, x] - 1.0) > spread:
                    extra[y, x] = True
    return extra


def _baseline_along_rows(plane, flagged, npieces, degree):
    """Robust baseline of each column, taken along the time axis.

    One baseline per column, not one shared template: a column's mean level
    differs from the next column's by the bandpass, so subtracting a single
    global level leaves every column offset by its own bandpass ratio and the
    time-direction test then flags the column wholesale.  The baseline is fitted
    (rather than taken as a median) so that real time structure -- a source
    rising, gain drifts -- is followed and only deviations from it are flagged.
    """
    baseline = np.empty(plane.shape[1], dtype=float)
    x = np.arange(plane.shape[0], dtype=float)
    for column in range(plane.shape[1]):
        series = np.where(flagged[:, column], np.nan, plane[:, column])
        finite = np.isfinite(series)
        if finite.sum() < 2:
            baseline[column] = np.nan
            continue
        fitted, _ = robust_fit(x, series, npieces, degree)
        baseline[column] = np.nanmedian(fitted)
    return baseline


def bandpass_template(plane, flagged, params):
    """The fitted bandpass of a plane: the time-average, robustly fitted.

    Exposed because it is the step the whole algorithm turns on, and because a
    caller can check it directly against a known band shape.
    """
    # Sum and count rather than ``nanmean``: an all-flagged channel is normal on
    # real data -- an MS can arrive with much of its band already dead -- and
    # ``nanmean`` reports "Mean of empty slice" for it.  That is information
    # rather than a problem, since the fit below handles a NaN, but a warning per
    # dead channel would drown the log.  Dividing explicitly also keeps the NaN
    # and avoids depending on how a numpy version chooses to raise it.
    with np.errstate(invalid="ignore"):
        present = np.where(flagged, np.nan, plane)
        counts = np.sum(~np.isnan(present), axis=0)
        totals = np.nansum(present, axis=0)
        bandpass = np.where(counts > 0, totals / np.maximum(counts, 1), np.nan)
    template, _ = robust_fit(
        np.arange(bandpass.size, dtype=float), bandpass,
        params.maxnpieces, _fit_for(params.freqfit),
    )
    return _safe_template(template, bandpass)


def _fit_and_flag_freq(plane, flagged, params):
    """Bandpass direction: fit the time-average, flatten, flag across frequency."""
    template = bandpass_template(plane, flagged, params)
    flat = _flatten(plane, template)
    flags = np.zeros_like(flagged)
    for row in range(flat.shape[0]):
        flags[row], _ = flag_1d(flat[row], params.freqcutoff)
    return flags, flat


def _fit_and_flag_time(plane, flagged, params):
    """Time direction: flatten each column by its own robust baseline, flag in time."""
    baseline = _baseline_along_rows(
        plane, flagged, params.maxnpieces, _fit_for(params.timefit)
    )
    safe = _safe_template(baseline, np.ones_like(baseline))
    flat = _flatten(plane, safe[None, :])
    flags = np.zeros_like(flagged)
    for column in range(flat.shape[1]):
        flags[:, column], _ = flag_1d(flat[:, column], params.timecutoff)
    return flags, flat


def _fit_for(fit_type):
    """Polynomial degree for a fit type: a line is degree 1, 'poly' cubic."""
    if fit_type == "line":
        return 1
    if fit_type == "poly":
        return POLY_DEGREE
    raise ValueError(
        f"unknown fit type {fit_type!r}; expected one of {', '.join(FIT_TYPES)}"
    )


def _combine(freq_flags, time_flags, flagdimension):
    """Combine the two directions.

    ``freqtime`` and ``timefreq`` union them, so a point is flagged when either
    direction calls it an outlier; ``freq`` and ``time`` use one direction only.
    CASA lists the four as distinct spellings; here the order within the name
    carries no meaning, because both directions are computed from the *input*
    flags and neither sees the other's output.  The published description runs
    the second direction after the first, so the first direction's flags are
    already excluded from the second's average; keeping them independent is a
    deliberate simplification, since it stops the first direction from biasing
    the second.
    """
    if flagdimension == "freq":
        return freq_flags
    if flagdimension == "time":
        return time_flags
    return np.logical_or(freq_flags, time_flags)


def tfcrop_plane(plane, params, flagged=None):
    """Run TFCrop on one ``(time, chan)`` plane.

    ``plane`` is the visibility amplitude of one baseline and correlation;
    ``flagged`` the flags already set on it.  Returns ``(flag, stats)``, where
    ``flag`` is the complete flag plane -- pre-existing flags included, since
    the caller needs to know what to write back -- and ``stats`` the counts the
    caller reports.
    """
    plane = np.asarray(plane, dtype=float)
    if flagged is None:
        flagged = np.zeros(plane.shape, dtype=bool)
    else:
        flagged = np.asarray(flagged, dtype=bool)
    flagged = flagged | ~np.isfinite(plane)
    pre_existing = flagged.copy()

    freq_flags = np.zeros_like(flagged)
    time_flags = np.zeros_like(flagged)
    flat_freq = flat_time = None
    if params.flagdimension in ("freqtime", "freq"):
        freq_flags, flat_freq = _fit_and_flag_freq(plane, flagged, params)
    if params.flagdimension in ("freqtime", "timefreq", "time"):
        time_flags, flat_time = _fit_and_flag_time(plane, flagged, params)

    combined = _combine(freq_flags, time_flags, params.flagdimension)

    window_extra = np.zeros_like(combined)
    if params.usewindowstats != "none":
        # Window statistics run on a bandpass-flattened plane, so the window
        # sees deviations rather than the band shape.
        reference = flat_freq if flat_freq is not None else flat_time
        if reference is not None:
            window_extra = _window_pass(
                reference, combined, params.usewindowstats, params.halfwin,
                params.timecutoff,
            )

    flag = pre_existing | combined | window_extra
    stats = {
        "new": int(np.count_nonzero(flag & ~pre_existing)),
        "total": int(flag.size),
        "pre_existing": int(np.count_nonzero(pre_existing)),
    }
    return flag, stats


class TFCropParams:
    """Validated parameters for one ``tfcrop`` operation.

    Names and defaults are CASA's, so a recipe written for
    ``flagdata(mode='tfcrop')`` transfers unchanged.  ``ntime`` is deliberately
    absent: the fit chunk is the dask chunk (see doc/NEW_FLAGGING.md), so there
    is no separate time-range control to get wrong.
    """

    __slots__ = tuple(sorted((
        "timecutoff", "freqcutoff", "timefit", "freqfit", "maxnpieces",
        "flagdimension", "usewindowstats", "halfwin", "combinescans",
    )))

    DEFAULTS = {
        "timecutoff": 4.0,
        "freqcutoff": 3.0,
        "timefit": "line",
        "freqfit": "poly",
        "maxnpieces": 7,
        "flagdimension": "freqtime",
        "usewindowstats": "none",
        "halfwin": 1,
        "combinescans": False,
    }

    def __init__(self, **kwargs):
        unknown = set(kwargs) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(
                f"unknown tfcrop parameter(s): {', '.join(sorted(unknown))}."
                f" Valid parameters are {', '.join(sorted(self.DEFAULTS))}"
            )
        merged = dict(self.DEFAULTS)
        merged.update(kwargs)
        for name in self.DEFAULTS:
            setattr(self, name, merged[name])
        self._validate()

    def _validate(self):
        for name in ("timecutoff", "freqcutoff"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or value <= 0:
                raise ValueError(f"{name} must be a positive number, got {value!r}")
        for name in ("timefit", "freqfit"):
            value = getattr(self, name)
            if value not in FIT_TYPES:
                raise ValueError(
                    f"{name} must be one of {', '.join(FIT_TYPES)}, got {value!r}"
                )
        if isinstance(self.maxnpieces, bool) or not isinstance(self.maxnpieces, int) \
                or not 1 <= self.maxnpieces <= 7:
            raise ValueError(
                f"maxnpieces must be an integer from 1 to 7, got {self.maxnpieces!r}"
            )
        if self.flagdimension not in FLAG_DIMENSIONS:
            raise ValueError(
                f"flagdimension must be one of {', '.join(FLAG_DIMENSIONS)},"
                f" got {self.flagdimension!r}"
            )
        if self.usewindowstats not in WINDOW_STATS:
            raise ValueError(
                f"usewindowstats must be one of {', '.join(WINDOW_STATS)},"
                f" got {self.usewindowstats!r}"
            )
        if isinstance(self.halfwin, bool) or not isinstance(self.halfwin, int) \
                or not 1 <= self.halfwin <= 3:
            raise ValueError(
                f"halfwin must be an integer from 1 to 3, got {self.halfwin!r}"
            )
        if not isinstance(self.combinescans, bool):
            raise ValueError(
                f"combinescans must be true or false, got {self.combinescans!r}"
            )

    def __repr__(self):
        shown = ", ".join(
            f"{name}={getattr(self, name)!r}" for name in sorted(self.DEFAULTS)
        )
        return f"TFCropParams({shown})"
