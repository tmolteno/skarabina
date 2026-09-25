# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Choosing the dask row chunk from a memory budget.

Every chunked phase of a run -- reading, flagging, averaging, writing -- holds
about one row chunk per dask worker at a time, and its memory is a fixed
multiple of the chunk's visibilities.  So the peak is roughly

    BASE_BYTES + workers * row_chunk * nchan * ncorr * BYTES_PER_VISIBILITY

and the largest row chunk that fits a budget follows directly.  The two
constants are measured (doc/RFLAG.md §7.3): rflag and tfcrop, the heaviest
chunked steps, on a MeerKAT L-band scan at several chunk sizes and worker
counts.  A larger chunk is better for the flaggers -- each baseline's time
series in a chunk is longer -- so the chunk is made as large as the budget
allows, but no larger than keeps every worker busy.

Not governed by the chunk: ``save:`` writes a CASA flag-version table through
casacure's buffered writer, which holds the whole flag cube (AGENTS.md).
"""

import math
import os

#: Peak bytes per visibility of one row chunk in flight, per worker: the chunk's
#: DATA and FLAG as read, the flagger's working set, and dask's copies.
#: Measured on mergA_tim scan 1: 33 for rflag, 26 for tfcrop; rounded up.
BYTES_PER_VISIBILITY = 36

#: Memory a run holds whatever the chunk: the interpreter and libraries, the
#: per-row columns (TIME, UVW, ANTENNA*), the task graph.  Measured: 2.0 GB
#: with rflag, 3.2 GB with tfcrop; rounded up.
BASE_BYTES = 3.5 * 2**30

#: Fraction of the budget the chunked phases may plan to use; the rest is
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


def planned_bytes(row_chunk, workers, nchan, ncorr):
    """What the model expects a run's chunked phases to peak at, in bytes."""
    return BASE_BYTES + workers * row_chunk * nchan * ncorr * BYTES_PER_VISIBILITY


def row_chunk_for(memory_bytes, workers, nchan, ncorr, nrow=None):
    """The largest row chunk whose chunked phases fit ``memory_bytes``.

    Returns ``(row_chunk, reason)``, the reason a one-line explanation for the
    log.  ``nrow`` caps the chunk so that every worker gets one.
    """
    per_row = BYTES_PER_VISIBILITY * max(1, nchan) * max(1, ncorr)
    budget = SAFETY * memory_bytes - BASE_BYTES
    rows = int(budget // (workers * per_row)) if budget > 0 else 0
    reason = (f"{memory_bytes / 2**30:.1f} GB x {SAFETY:.0%} - {BASE_BYTES / 2**30:.1f} GB"
              f" over {workers} workers x {nchan} chan x {ncorr} corr"
              f" x {BYTES_PER_VISIBILITY} B")
    if rows < MIN_ROW_CHUNK:
        return MIN_ROW_CHUNK, reason + (
            f" allows {rows} rows; using the minimum, {MIN_ROW_CHUNK}"
            " -- lower --workers to stay within the limit")
    cap = MAX_ROW_CHUNK
    if nrow:
        cap = min(cap, max(MIN_ROW_CHUNK, math.ceil(nrow / workers)))
    if rows > cap:
        return cap, reason + f" allows {rows} rows; capped at {cap}"
    return rows, reason
