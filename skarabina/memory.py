# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Planning a run's memory: the row chunk, and what no chunk can bound.

A run is a sequence of steps -- reading, the ``--flag`` verbs, the write --
and its peak is the peak of its most expensive step, not their sum (measured:
the meerkat stage-0 list with rflag peaked at 21.1 GB, rflag alone at
20.6 GB).  Each step's memory is one of two kinds:

* **per chunk**: the step holds about one row chunk per dask worker, so it
  costs ``fixed + workers x row_chunk x nchan x ncorr x b``, and a smaller
  chunk bounds it.  Reading and every flag verb are of this kind.
* **per table**: the step holds the whole table whatever the chunk.  casacure
  buffers what it writes until the table is flushed, so ``save:<name>`` holds
  the whole flag cube and a full ``--msout`` write the whole output table
  (~56 bytes per output visibility: 40.7 GB to write an 11 GB MS).  No chunk
  size helps; the plan reports them and warns when they exceed the limit.

The row chunk is the largest that keeps every per-chunk step of the run within
the limit -- so it is set by the most expensive verb in the list -- and no
larger than keeps every worker busy.  A larger chunk is better for the
flaggers (each baseline's time series in a chunk is longer).  The constants are
measured on mergA_tim (doc/RFLAG.md §7.3-7.4) and rounded up.
"""

import math
import os
from dataclasses import dataclass, field

GB = 2**30

#: Per-chunk steps: ``(bytes per visibility of one row chunk in flight per
#: worker, fixed bytes)``.  Measured as peak RSS over rows in flight at 5000-
#: and 10 000-row chunks x 12 workers on a 2511-channel, 2-correlation MS;
#: tfcrop and rflag are the linear fits of doc/RFLAG.md §7.3.
CHUNK_COST = {
    "read": (1, 0),
    "autos": (2, 0),
    "uv-above": (1, 0),
    "nan": (4, 0),
    "clip": (4, 0),
    "spectral-window": (5, 0),
    "restore": (3, 0),
    "tfcrop": (30, 3.5 * GB),
    "rflag": (36, 2.5 * GB),
}

#: Added to every per-chunk step when the write shares the flagging pass (it
#: does unless ``--optimize``/``--barber`` split it): the chunks the write holds
#: are in flight at the same time as the flaggers'.  A full write (with its
#: averaging) holds DATA, WEIGHT_SPECTRUM and SIGMA_SPECTRUM -- measured: the
#: stage-0 list + rflag peaked 5.4 GB above the same run with a separate write
#: pass, at 12 workers x 11 977 rows x 2511 x 2, 7.5 B per visibility; a
#: flags-only write holds FLAG and FLAG_ROW.
CONCURRENT_WRITE_COST = {"write": 8, "write-flags": 1}

#: Per-table steps: bytes per visibility of the whole table they hold --
#: ``save`` per visibility of the input MS, the writes per visibility of the
#: output (after averaging).
TABLE_COST = {
    "save": 2.5,
    "write": 56.0,          # --msout, every column
    "write-flags": 1.5,     # --write-changed-only / --apply: flag columns only
}

#: Memory a run holds whatever it does: interpreter, libraries, per-row
#: columns, the task graph.
BASE_BYTES = 0.5 * GB

#: Fraction of the limit the per-chunk steps may plan to use; the rest is
#: headroom for what the model does not count (allocator slack, the OS).
SAFETY = 0.8

#: Bounds on an automatically chosen chunk.  Below the minimum the per-chunk
#: overhead dominates and a baseline has too few integrations in a chunk for
#: rflag's time windows; the maximum only keeps a chunk's task sane.
MIN_ROW_CHUNK = 1000
MAX_ROW_CHUNK = 500_000


def _cgroup_limit():
    """The container's memory limit in bytes, or None when there is none."""
    for path in ("/sys/fs/cgroup/memory.max",                    # cgroup v2
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):  # cgroup v1
        try:
            with open(path) as fh:
                text = fh.read().strip()
        except OSError:
            continue
        if text.isdigit() and int(text) < 2**60:   # "max" or a huge sentinel = none
            return int(text)
    return None


def available_memory():
    """Bytes of RAM this process can use now, or None if it cannot be told.

    ``MemAvailable`` from /proc/meminfo -- memory that can be had without
    swapping, counting reclaimable cache -- capped by a cgroup limit when the
    process runs in a container.  Falls back to the physical memory size.
    """
    available = None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                    break
    except OSError:
        pass
    if available is None:
        try:
            available = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            available = None
    limit = _cgroup_limit()
    if limit is not None:
        available = limit if available is None else min(available, limit)
    return available


def effective_workers(workers):
    """The dask thread count a ``--workers`` value means (0: all cores)."""
    return workers if workers and workers > 0 else (os.cpu_count() or 1)


def chunk_bytes(step, row_chunk, workers, nchan, ncorr, extra=0):
    """Estimated peak of a per-chunk step, in bytes (base included).

    ``extra`` is added to the step's bytes per visibility and ``reserve`` --
    see :func:`plan` -- is how a concurrent write enters.
    """
    per_vis, fixed = CHUNK_COST[step]
    return BASE_BYTES + fixed + workers * row_chunk * nchan * ncorr * (per_vis + extra)


def table_bytes(step, visibilities):
    """Estimated peak of a per-table step, in bytes (base included)."""
    return BASE_BYTES + TABLE_COST[step] * visibilities


@dataclass
class Plan:
    """A run's memory plan: the row chunk, and log lines and warnings."""
    row_chunk: int
    limiting_step: str
    lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _largest_chunk(steps, budget, workers, nchan, ncorr, extra=0):
    """The largest row chunk keeping every per-chunk step within ``budget``."""
    chosen, limiting = MAX_ROW_CHUNK, "the maximum"
    for step in steps:
        per_vis, fixed = CHUNK_COST[step]
        room = budget - BASE_BYTES - fixed
        rows = int(room // (workers * nchan * ncorr * (per_vis + extra))) if room > 0 else 0
        if rows < chosen:
            chosen, limiting = rows, step
    return chosen, limiting


def plan(steps, memory_bytes, workers, nrow, nchan, ncorr,
         write=None, out_visibilities=None, row_chunk=None, concurrent_write=False):
    """Plan a run: choose the row chunk (unless given) and estimate each step.

    ``steps`` are the flag verbs in order (``"save"``, ``"nan"``, ...).
    ``write`` is ``None``, ``"write"`` (a full ``--msout``) or
    ``"write-flags"`` (``--write-changed-only``/``--apply``);
    ``out_visibilities`` is the output's size after averaging (default: the
    input's).  ``concurrent_write`` says the full write shares the flagging
    pass (the CLI's single pass): its per-chunk cost is then added to every
    step, and its whole-table buffer reserved out of the budget, because both
    grow while the flaggers' chunks are in flight.
    """
    nchan, ncorr = max(1, nchan), max(1, ncorr)
    chunked = ["read"] + [s for s in dict.fromkeys(steps) if s in CHUNK_COST]
    whole = [s for s in dict.fromkeys(steps) if s in TABLE_COST]
    nvis = nrow * nchan * ncorr
    out = nvis if out_visibilities is None else out_visibilities
    concurrent = concurrent_write and write in CONCURRENT_WRITE_COST
    extra = CONCURRENT_WRITE_COST[write] if concurrent else 0
    reserve = TABLE_COST[write] * out if concurrent else 0
    budget = SAFETY * memory_bytes - reserve
    if row_chunk is None:
        chosen, limiting = _largest_chunk(chunked, budget, workers, nchan, ncorr, extra)
        if nrow:
            idle_cap = max(MIN_ROW_CHUNK, math.ceil(nrow / workers))
            if idle_cap < chosen:
                chosen, limiting = idle_cap, f"{workers} workers over {nrow} rows"
        too_small = chosen < MIN_ROW_CHUNK
        chosen = max(MIN_ROW_CHUNK, chosen)
    else:
        chosen, limiting, too_small = row_chunk, "--row-chunk", False

    result = Plan(chosen, limiting)
    result.lines.append(
        f"Memory plan: limit {memory_bytes / GB:.1f} GB, {workers} workers,"
        f" {nrow} rows x {nchan} chan x {ncorr} corr;"
        f" row chunk {chosen} rows, set by {limiting}")
    suffix = "  per chunk, with the write in the same pass" if concurrent else "  per chunk"
    for step in chunked:
        estimate = chunk_bytes(step, chosen, workers, nchan, ncorr, extra) + reserve
        result.lines.append(f"  {step:<16} {estimate / GB:7.1f} GB{suffix}")
    tables = [(step, table_bytes(step, nvis)) for step in whole]
    if write is not None:
        tables.append((write, table_bytes(write, out)))
    for step, estimate in tables:
        result.lines.append(f"  {step:<16} {estimate / GB:7.1f} GB  whole table")
        if estimate > memory_bytes:
            advice = (" -- average first, or write only the flags"
                      " (--write-changed-only / --apply)") if step == "write" else ""
            result.warnings.append(
                f"{step}: needs ~{estimate / GB:.0f} GB whatever the row chunk"
                f" (casacure buffers the whole table it writes), over the"
                f" {memory_bytes / GB:.0f} GB limit{advice}")

    if too_small:
        result.warnings.append(
            f"{limiting} does not fit the limit even at the minimum chunk of"
            f" {MIN_ROW_CHUNK} rows; lower --workers")
    if row_chunk is not None:
        worst = max(chunked, key=lambda s: chunk_bytes(s, chosen, workers, nchan, ncorr))
        estimate = chunk_bytes(worst, chosen, workers, nchan, ncorr, extra) + reserve
        if estimate > budget:
            result.warnings.append(
                f"--row-chunk {chosen} with {workers} workers plans ~{estimate / GB:.1f} GB"
                f" for {worst}, over {SAFETY:.0%} of the {memory_bytes / GB:.1f} GB limit")
    return result
