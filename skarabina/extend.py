# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The ``extend`` verb -- grow flags into their neighbours.

Reproduces ``flagdata(mode='extend')`` (CASA) so a recipe written for it
transfers unchanged.  Parameters and defaults are CASA's; the semantics below
are the contract, and where CASA's documentation is terse the choice made here
is called out.

Growth is computed on the per-(row, channel) union of the correlations and
applies as follows (each rule is applied once, from the input flags -- they do
not iterate to a fixed point):

``extendpols`` (default True)
    If any correlation is flagged, flag all correlations.  When False, each
    correlation is grown independently instead.

``growtime`` (default 50, percent)
    Per baseline and scan (``ntime='scan'``, CASA's default) and per channel:
    if **more than** this percentage of the group's integrations is flagged,
    flag every integration of the group at that channel.  ``<= 0`` disables
    the rule.  (CASA documents growtime as a percentage of the timerange, not
    seconds.)

``growfreq`` (default 50, percent)
    Per row: if more than this percentage of the channels is flagged, flag the
    whole row.  ``<= 0`` disables the rule.

``flagneartime`` (default False)
    Flag one timestep before and after each flagged sample, at the same
    channel, on the same baseline (adjacent integrations of that baseline --
    neighbouring *rows* of other baselines are never touched).

``flagnearfreq`` (default False)
    Flag one channel before and after each flagged sample, at the same
    timestep.  Channels are taken in table order, which is frequency order in
    every MS skarabina has seen.

``growaround`` (default False)
    Flag the eight-sample neighbourhood (time x frequency, diagonals
    included) of every flagged sample: the composition of the one-step time
    and one-step frequency growths.  CASA documents this only as "flag data
    based on surrounding flags"; this is the natural reading.

``ntime``/``combinescans`` are deliberately absent, as for ``tfcrop`` and
``rflag``: the growth groups are the scans themselves.
"""
import dask
import dask.array as da
import numpy as np


class ExtendParams:
    """Validated parameters for one ``extend`` operation."""

    __slots__ = tuple(sorted((
        "extendpols", "growtime", "growfreq", "growaround",
        "flagneartime", "flagnearfreq",
    )))

    DEFAULTS = {
        "extendpols": True,
        "growtime": 50.0,
        "growfreq": 50.0,
        "growaround": False,
        "flagneartime": False,
        "flagnearfreq": False,
    }

    def __init__(self, **kwargs):
        unknown = set(kwargs) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(
                f"unknown extend parameter(s): {', '.join(sorted(unknown))}."
                f" Valid parameters are {', '.join(sorted(self.DEFAULTS))}"
            )
        merged = dict(self.DEFAULTS)
        merged.update(kwargs)
        for name in self.DEFAULTS:
            setattr(self, name, merged[name])
        self._validate()

    def _validate(self):
        for name in ("extendpols", "growaround", "flagneartime", "flagnearfreq"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"extend parameter {name} must be true or false")
        for name in ("growtime", "growfreq"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"extend parameter {name} must be a number")
            if not 0.0 <= float(value) <= 100.0:
                raise ValueError(
                    f"extend parameter {name} is a percentage (0-100),"
                    f" got {value}"
                )
            setattr(self, name, float(value))


def time_neighbours(ant1, ant2, scan, time):
    """Per-row bookkeeping for time-axis growth: who is adjacent to whom.

    Returns ``(prev_idx, next_idx, group_ids, group_sizes)``:

    * ``prev_idx[r]``/``next_idx[r]`` -- the rows of the same baseline whose
      integration is immediately before/after row ``r``'s, or ``r`` itself at
      the edges of the baseline's time series;
    * ``group_ids[r]`` -- the row's growth group: its (baseline, scan) pair
      (``growtime``'s "timerange", CASA's ``ntime='scan'``);
    * ``group_sizes`` -- integrations per group.

    Rows need not be time-ordered or baseline-contiguous: the neighbours are
    found by sorting on (group, time), so interleaved baselines are handled.
    """
    ant1 = np.asarray(ant1)
    ant2 = np.asarray(ant2)
    scan = np.asarray(scan)
    time = np.asarray(time, dtype=float)
    nrow = ant1.size

    # Group by (baseline, scan): lexicographic inverse gives one id per pair.
    pairs = np.stack(
        [ant1.astype(np.int64), ant2.astype(np.int64), scan.astype(np.int64)],
        axis=1,
    )
    _, group_ids = np.unique(pairs, axis=0, return_inverse=True)
    group_ids = np.asarray(group_ids, dtype=np.int64)
    group_sizes = np.bincount(group_ids)

    order = np.lexsort((time, group_ids))  # row ids, sorted by group then time
    prev_sorted = order.copy()
    next_sorted = order.copy()
    if nrow > 1:
        same_prev = group_ids[order[1:]] == group_ids[order[:-1]]
        same_next = same_prev
        prev_sorted[1:][same_prev] = order[:-1][same_prev]
        next_sorted[:-1][same_next] = order[1:][same_next]

    prev_idx = np.empty(nrow, dtype=np.int64)
    next_idx = np.empty(nrow, dtype=np.int64)
    prev_idx[order] = prev_sorted
    next_idx[order] = next_sorted
    return prev_idx, next_idx, group_ids, group_sizes


def _shift_channels(mask, offset):
    """``mask`` with its channel axis shifted by ``offset`` (zero-filled)."""
    nchan = mask.shape[1]
    if offset == 0:
        return mask
    if offset > 0:
        head = da.zeros(
            (mask.shape[0], offset), dtype=bool, chunks=(mask.chunks[0], offset)
        )
        return da.concatenate([head, mask[:, : nchan - offset]], axis=1)
    shift = -offset
    tail = da.zeros(
        (mask.shape[0], shift), dtype=bool, chunks=(mask.chunks[0], shift)
    )
    return da.concatenate([mask[:, shift:], tail], axis=1)


def _group_flag_counts(mblock, gblock, ngroup, nchan):
    """Flagged counts per (group, channel) of one row block.  Fast enough
    with one bincount per channel; ``np.add.at`` would dominate the run."""
    m = np.asarray(mblock, dtype=bool)
    g = np.asarray(gblock, dtype=np.int64)
    out = np.zeros((ngroup, nchan), dtype=np.int64)
    for c in range(nchan):
        out[:, c] = np.bincount(g, weights=m[:, c], minlength=ngroup)
    return out


def grow_flags(mask, prev_idx, next_idx, group_ids, group_sizes, params):
    """Grow a 2-D per-(row, channel) flag mask, per :class:`ExtendParams`.

    ``mask`` is a lazy ``(nrow, nchan)`` dask array; the result is lazy of the
    same shape.  Each enabled rule is applied once, from the input mask.
    """
    out = mask

    if params.growtime > 0.0:
        # (group, channel) columns whose flagged fraction exceeds growtime%
        ngroup = len(group_sizes)
        nchan = mask.shape[1]
        row_chunks = mask.chunks[0]
        partials = []
        start = 0
        for block in range(len(row_chunks)):
            stop = start + row_chunks[block]
            partials.append(
                dask.delayed(_group_flag_counts)(
                    mask.blocks[block], group_ids[start:stop], ngroup, nchan
                )
            )
            start = stop
        counts = da.from_delayed(
            dask.delayed(np.add.reduce)(partials),
            shape=(ngroup, nchan),
            dtype=np.int64,
        )
        grow_cols = counts > (params.growtime / 100.0) * group_sizes[:, None]
        out = da.logical_or(out, grow_cols[group_ids])

    if params.growfreq > 0.0:
        row_fraction = da.sum(mask, axis=1) / float(mask.shape[1])
        grow_rows = row_fraction > (params.growfreq / 100.0)
        out = da.logical_or(out, grow_rows[:, None])

    if params.flagneartime:
        out = da.logical_or(out, mask[prev_idx])
        out = da.logical_or(out, mask[next_idx])

    if params.flagnearfreq:
        out = da.logical_or(out, _shift_channels(mask, 1))
        out = da.logical_or(out, _shift_channels(mask, -1))

    if params.growaround:
        around_time = da.logical_or(da.logical_or(mask, mask[prev_idx]), mask[next_idx])
        around = da.logical_or(
            da.logical_or(around_time, _shift_channels(around_time, 1)),
            _shift_channels(around_time, -1),
        )
        out = da.logical_or(out, around)

    return out
