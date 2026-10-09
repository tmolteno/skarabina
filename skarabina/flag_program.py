# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The flag program: one run's replayable verbs, evaluated per block in
worker processes.

Why
---
The auto-flaggers (``rflag``, ``tfcrop``, ``tf-nn``, ``nn-flagger``) are the
expensive part of a run, and their blocks -- one dask task per row chunk --
cannot use more than a couple of cores under the threaded scheduler dask runs
them in: a block is hundreds of medium-size numpy calls, and the Python-level
dispatch of each holds the GIL (measured with ``py-spy --gil``: ~58 % of the
GIL-held time is ``numpy``'s ``_wrapfunc`` alone).  Measured on the 430k-row
bench MS, a whole flag list ran at 317 % CPU on 8 cores, and wall time stopped
improving beyond two workers (17.6 s at one worker, 9.7 s at two, 8.3 s at
four, 9.1 s at eight -- the last *slower* than four, on 37 % more CPU).

What
----
The verbs whose flags are a pure function of their block -- the elementwise
masks (``nan``, ``clip``, ``autos`` and ``spectral-window``'s contribution to
FLAG) and the auto-flaggers themselves -- are recorded, in the order the
``--flag`` list runs them, as a *program* of operations over a block of rows.
When the run's final pass needs the flags, each block's program is executed by
a forked worker process: the child opens the input table read-only with its
own casacure handle, reads its rows of the visibility column (and FLAG, and
the small per-row columns), replays the masks, runs the auto-flaggers, packs
the flags at one bit per visibility into a shared-memory buffer, and returns
the counts.  The flags never cross a process boundary and never touch the
disk.

The GIL is per-process, so the blocks scale with the worker count, and the
bytes read are what they were: one read of the visibility column per block,
in whoever reads it.  A flags-only write or a no-write run reads DATA exactly
once, as before (tests/test_single_pass.py counts the child reads too).  A
full ``--msout`` write reads DATA a second time for its own column -- the one
way the pool changes a run's IO.

How the single pass is kept
---------------------------
The cheap verbs before the auto-flagger become the *input* flags of it, so the
child replays them; the cheap verbs after it become trailing layers, ORed onto
its result in the same child.  ``nan, rflag, clip`` still reads DATA once --
the child computes all three from the block it already holds.  The counts the
data-reading verbs would have printed come back from the children as per-block
sums, so no second pass is needed for the reports either.  (``uv-above`` sets
FLAG_ROW only, so it contributes no FLAG layer; its report, like ``autos``'
row counts and ``spectral-window``'s per-entry counts, is measured over the
small per-row columns and stays with the parent's dask pass.)

Fallbacks
---------
The pool is used only when it can be: at least two blocks, at least two
workers, a fork start method, and ``SKARABINA_FLAG_POOL`` not disabling it.
Anything the program cannot replay -- ``extend`` before an auto-flagger (its
growth crosses rows), or any FLAG graph built outside the vocabulary above --
leaves that auto-flagger on the lazy in-process path, with identical results
(tests assert the two paths write the same flags).  A failure to create the
pool or the shared memory falls back to running the same per-block program
sequentially in this process: slow, but correct.

Fork safety
-----------
The pool is forked **before** any table is opened and before dask has spawned
a thread, when the CLI knows the ``--flag`` list contains an auto-flagger
(:func:`prepare_pool`, called from ``main()`` before the DaskMS is built), so
the children inherit no casacure handles and no threads.  Children never touch
a handle inherited across the fork: they open their own read-only tables, and
casacure's lock-file registry and flush gates are per-process by construction
after a copy-on-write fork.  Nothing writes the input while the children read
it -- the run's writes happen after the program has finished.
"""

import logging
import mmap
import os
import threading
import weakref

import dask.array as da
import numpy as np
from dask import delayed

logger = logging.getLogger(__name__)


def pool_enabled_by_env():
    """False when ``SKARABINA_FLAG_POOL`` asks for the in-process path."""
    return os.environ.get("SKARABINA_FLAG_POOL", "1").strip().lower() \
        not in ("0", "off", "no", "false")


#: Column bytes read by the worker processes, accumulated here by the parent
#: from every child's report.  tests/test_single_pass.py adds these to the
#: in-process dask-ms reads, so the "each column once" invariant is counted
#: across processes too.
CHILD_READ_BYTES = {}

_pool = None
_pool_lock = threading.Lock()


def prepare_pool(workers):
    """Fork the worker pool early, if a run could use it.

    Called from the CLI as soon as the ``--flag`` list is known to hold an
    auto-flagger, before the DaskMS opens the input and before any dask
    compute has spawned threads -- the cleanest moment to fork.  Does nothing
    when the pool is disabled, when there is no fork start method, or when
    fewer than two workers were asked for; in those cases the program is never
    used and the verbs stay on the lazy path.  Failure is logged and returned
    as None, never raised: the run falls back.
    """
    global _pool
    if workers < 2 or not pool_enabled_by_env() or not hasattr(os, "fork"):
        return None
    with _pool_lock:
        if _pool is not None:
            return _pool or None
        try:
            import multiprocessing

            _pool = multiprocessing.get_context("fork").Pool(workers)
            logger.debug(
                "flag program worker pool: %d forked processes", workers)
            return _pool
        except Exception as exc:  # a hostile environment, not a bug
            logger.warning("flag program pool unavailable: %s", exc)
            _pool = False
            return None


def _the_pool():
    """The prepared pool, or None when there is none to use."""
    return _pool if _pool else None


# ---------------------------------------------------------------------------
# the operations, as replayable data
# ---------------------------------------------------------------------------

def _mask_nan(vis, rows):
    return np.isnan(np.abs(vis))


def _mask_clip(vis, rows, low, high):
    magnitude = np.abs(vis)
    return (magnitude <= low) | (magnitude >= high)


def _mask_autos(vis, rows):
    return rows.antenna1 == rows.antenna2


def _mask_spectral(vis, rows, entries):
    """One ``spectral-window`` verb: the OR of its entries' contributions.

    ``entries`` is ``[(chan_mask, uv_below, uv_above), ...]`` in file order,
    exactly what :meth:`DaskMS.flag_spectral_window` ORs together lazily: a
    channel mask per entry and the entry's optional uv gates.
    """
    uvw = rows.uvw
    uv_dist = np.sqrt(uvw[:, 0] ** 2 + uvw[:, 1] ** 2)
    out = np.zeros(vis.shape, dtype=bool)
    for chan_mask, uv_below, uv_above in entries:
        gate = np.ones(vis.shape[0], dtype=bool)
        if uv_below is not None:
            gate &= uv_dist < float(uv_below)
        if uv_above is not None:
            gate &= uv_dist > float(uv_above)
        out |= gate[:, None, None] & chan_mask[np.newaxis, :, None]
    return out


class Layer:
    """One elementwise mask ORed onto the flags, at its position in the list.

    ``mask`` is a callable ``(vis, rows) -> bool`` -- a partial over one of
    the ``_mask_*`` functions above.  ``report``, when set, is called with
    the mask's scope-applied count when the program runs, replacing the
    ``da.sum`` the lazy path would have computed over DATA; ``settled``
    marks that it did, so the lazy reduction queued beside it is dropped.
    """

    __slots__ = ("mask", "report", "count", "settled")

    def __init__(self, mask, report=None):
        self.mask = mask
        self.report = report
        self.count = 0
        self.settled = False


class Stage:
    """One auto-flagger over the block, at its position in the list."""

    __slots__ = ("kind", "params", "abs_input", "rows", "report", "counts")

    def __init__(self, kind, params, abs_input, rows, report=None):
        self.kind = kind          # "rflag" | "tfcrop" | "tf_nn" | "nn_flagger"
        self.params = params
        self.abs_input = abs_input    # tfcrop fits the amplitude plane
        self.rows = rows              # "baselines" | "nn" | "none"
        self.report = report          # callable(flagged, newly) or None
        self.counts = np.zeros((0, 3), np.int64)


class FlagProgram:
    """The ordered, per-block-replayable operations of one flag run.

    Kept by :class:`skarabina.dask_ms.DaskMS` (``ms._program``); the verbs
    append to it as they run, and :meth:`flags_array` stands in for FLAG once
    an auto-flagger has joined.  Executed at most once, by :meth:`run`, at
    whichever point of the run first needs the flags.
    """

    def __init__(self, ms_name, row_chunks, shape, data_source, rowids,
                 workers, scope=None):
        self.ms_name = ms_name
        self.row_chunks = tuple(int(n) for n in row_chunks)
        self.shape = tuple(int(n) for n in shape)    # (nrow, nchan, ncorr)
        #: ``(columns, op)`` as in DaskMS._DATA_SOURCES[data_column]
        self.data_source = data_source
        self.rowids = np.asarray(rowids)             # dataset row -> MS row
        self.workers = workers
        self.scope = scope                           # per-row bool, or None
        self.ops = []                # Layer | Stage, in --flag order
        #: where the flags start: "input", ("version", name) or
        #: ("program", FlagProgram) -- a spent program's shared buffer.
        self.base = "input"
        self.base_report = None      # a restore:'s report, or None
        self.poisoned = False        # a non-replayable verb ran: stay lazy
        self._done = False
        self._run_lock = threading.Lock()
        #: set by run(): (shm name or None, offsets, packed sizes), and the
        #: buffer/keep-alive handle behind them.
        self.shm = None
        self._shm_handle = None
        self._buffer = None

    # -- recording ----------------------------------------------------------

    def recordable(self):
        """Whether an auto-flagger may still join this program."""
        return not self.poisoned and not self._done

    def append_layer(self, mask, report=None):
        """Append one mask layer; returns the :class:`Layer` recorded."""
        layer = Layer(mask, report)
        self.ops.append(layer)
        return layer

    def append_stage(self, kind, params, abs_input, rows, report=None):
        self.ops.append(Stage(kind, params, abs_input, rows, report))

    def append_reset(self, versionname, holder=None):
        """A ``restore:``: discard everything recorded and start from this.

        ``holder`` is the :class:`Layer` carrying the verb's report (and
        ``settled`` bookkeeping): only the surviving base gets its line from
        the program, and a restore a later one replaces leaves its holder
        unsettled, so the lazy reduction queued beside it still computes --
        exactly what the lazy path would have printed.
        """
        self.ops = []
        self.base = ("version", versionname)
        self.base_report = holder

    def has_stage(self):
        return any(isinstance(op, Stage) for op in self.ops)

    # -- shape ----------------------------------------------------------

    def block_bounds(self):
        """``[(start_row, n_rows), ...]`` per block, over the dataset rows."""
        bounds = []
        start = 0
        for n in self.row_chunks:
            bounds.append((start, int(n)))
            start += int(n)
        return bounds

    def used_columns(self):
        """The per-row columns the children have to read."""
        names = set()
        for op in self.ops:
            if isinstance(op, Layer):
                if op.mask.func is _mask_autos:
                    names.update(("ANTENNA1", "ANTENNA2"))
                elif op.mask.func is _mask_spectral:
                    names.add("UVW")
            elif isinstance(op, Stage):
                if op.rows == "baselines":
                    names.update(("ANTENNA1", "ANTENNA2"))
                elif op.rows == "nn":
                    names.update(("ANTENNA1", "ANTENNA2", "TIME"))
        return names

    # -- execution ----------------------------------------------------------

    def _jobs(self, shm_name, offsets, sizes):
        """One child job per block: everything the block needs, picklable."""
        scope_slices = [None] * len(self.row_chunks)
        if self.scope is not None:
            start = 0
            for i, n in enumerate(self.row_chunks):
                scope_slices[i] = self.scope[start:start + n]
                start += n
        columns = sorted(self.used_columns())
        base = self.base
        if isinstance(base, FlagProgram):
            base = ("program", base.shm[0], list(base.row_chunks),
                    [int(x) for x in base.shm[1]],
                    [int(x) for x in base.shm[2]], base.shape[1:])
        jobs = []
        ops = [
            ("layer", op.mask) if isinstance(op, Layer) else
            ("stage", op.kind, op.params, op.abs_input, op.rows)
            for op in self.ops
        ]
        for index, (start, n) in enumerate(self.block_bounds()):
            jobs.append({
                "table": self.ms_name,
                "base": base,
                "runs": _row_runs(self.rowids[start:start + n]),
                "row_start": int(start),
                "data_source": self.data_source,
                "ops": ops,
                "columns": columns,
                "shape": (int(n),) + self.shape[1:],
                "scope": scope_slices[index],
                "shm": (shm_name, int(offsets[index]), int(sizes[index])),
            })
        return jobs

    def run(self):
        """Execute the program once, in the pool -- or here, as a fallback.

        Idempotent; a second call is a no-op.  Fills the ops' counts, prints
        the queued reports, and installs :attr:`shm`.
        """
        if self._done:
            return
        with self._run_lock:
            if self._done:
                return
            per_vis = self.shape[1] * self.shape[2]
            packed = [(int(n) * per_vis + 7) // 8 for n in self.row_chunks]
            offsets = np.concatenate(([0], np.cumsum(packed)[:-1])) \
                if packed else np.zeros(0, np.int64)
            total = int(sum(packed))
            pool = _the_pool() if len(self.row_chunks) >= 2 \
                and self.workers >= 2 else None
            shm_name = None
            try:
                from multiprocessing import shared_memory

                shm = shared_memory.SharedMemory(
                    create=True, size=max(1, total))
                shm_name = shm.name
                jobs = self._jobs(shm.name, offsets, packed)
                results = pool.map(_run_block, jobs) if pool is not None \
                    else [_run_block(job) for job in jobs]
                self._shm_handle = shm
                # Unlink when the program dies -- the spill directory's
                # lifecycle.  Mappings held at unlink time stay valid (POSIX),
                # so a FLAG array evaluated after this still reads its block.
                # The tracker that creation registered with is told at the
                # same moment (see _release_shm), so a released program is
                # neither unlinked twice nor reported leaked at exit.
                weakref.finalize(self, _release_shm, shm)
            except Exception:
                if shm_name is not None:
                    _release_shm_by_name(shm_name)
                logger.warning(
                    "flag program: the blocks run in this process, not the"
                    " pool (%s)", "no pool" if pool is None else
                    "the shared buffer failed", exc_info=True)
                buffer = bytearray(max(1, total))
                jobs = self._jobs(None, offsets, packed)
                results = [
                    _run_block(job, _buffer=buffer, _offset=int(off))
                    for job, off in zip(jobs, offsets.tolist())
                ]
                self._buffer = bytes(buffer)
            self.shm = (shm_name, offsets, packed)
            self._report(results)
            self._done = True

    def _report(self, results):
        """Combine the blocks' results and print every queued report."""
        child_bytes = {}
        for _counts, _sums, _base, read_bytes in results:
            for column, nbytes in read_bytes.items():
                child_bytes[column] = child_bytes.get(column, 0) + nbytes
        self.child_bytes = child_bytes
        CHILD_READ_BYTES.clear()
        CHILD_READ_BYTES.update(child_bytes)

        if isinstance(self.base_report, Layer):
            self.base_report.count = int(sum(r[2] for r in results))
            self.base_report.settled = True
            if self.base_report.report is not None:
                self.base_report.report(self.base_report.count)
        stage_i = layer_i = 0
        for op in self.ops:
            if isinstance(op, Layer):
                op.count = int(sum(r[1][layer_i] for r in results))
                op.settled = True
                layer_i += 1
                if op.report is not None:
                    op.report(op.count)
            else:
                op.counts = np.array([r[0][stage_i] for r in results],
                                     dtype=np.int64)
                stage_i += 1
                if op.report is not None:
                    flagged, _existing, newly = op.counts.sum(axis=0)
                    op.report(int(flagged), int(newly))

    # -- the flags, before and after the run --------------------------------

    def flags_array(self):
        """FLAG as a lazy dask array over the program's result.

        Evaluating a block runs the whole program once (idempotent, so the
        blocks of one pass share it) and unpacks that block from the result.
        In the normal flow nothing evaluates FLAG before the run's final
        pass, and the blocks trigger the program themselves there.
        """
        blocks = []
        for index, (_start, n) in enumerate(self.block_bounds()):
            blocks.append(da.from_delayed(
                delayed(_flags_from_program)(self, index),
                shape=(int(n),) + self.shape[1:], dtype=bool,
            ))
        if not blocks:
            return da.zeros(self.shape, dtype=bool,
                            chunks=(self.row_chunks,) + self.shape[1:])
        return da.concatenate(blocks, axis=0)


# ---------------------------------------------------------------------------
# the shared buffer
# ---------------------------------------------------------------------------

def _unregister_tracked(name):
    """Take the buffer this process *created* out of its resource tracker.

    Creation registers the buffer with the tracker, which would unlink it
    again at process exit; the program's finaliser unlinks it earlier and
    deterministically, and this -- in the creating process, for the name as
    creation spelled it -- is what stops the tracker from racing it.  (An
    unregister for a name the tracker does not hold raises *inside the
    tracker* and prints, so callers must keep the pairing exact.)
    """
    try:
        from multiprocessing import resource_tracker

        resource_tracker.unregister(name, "shared_memory")
    except Exception:
        pass


def _release_shm(shm):
    # Unregister and unlink together, both in the creating process: see
    # _unregister_tracked.
    _unregister_tracked(shm._name)
    _release_shm_by_name(shm.name)
    try:
        shm.close()
    except Exception:
        pass


def _release_shm_by_name(name):
    from multiprocessing import shared_memory

    try:
        shared_memory.unlink(name)
    except Exception:
        pass


class _MappedShm:
    """A mapped view of a shared buffer, opened without ``SharedMemory``.

    The attach path of ``multiprocessing.shared_memory`` registers the buffer
    with a resource tracker spawned by the *attaching* process on Python
    3.10 to 3.12 -- and under a differently-spelled name than the creating
    process used, so it cannot be unregistered again: every child's tracker
    would try to unlink the live buffer at that child's death.  Mapping the
    ``/dev/shm`` file directly does not touch the tracker at all, so that is
    how children reach the buffer (with the SharedMemory attach as a
    fallback for a host without /dev/shm, where the noise is the lesser
    evil).  On the shared-memory filesystems that matter -- Linux -- this is
    exactly what SharedMemory does underneath.
    """

    def __init__(self, name):
        try:
            self._file = open("/dev/shm/" + name, "r+b")
            self._map = mmap.mmap(self._file.fileno(), 0)
            self.buf = memoryview(self._map)
        except OSError:
            from multiprocessing import shared_memory

            self._file = None
            self._map = None
            self._shm = shared_memory.SharedMemory(name=name)
            self.buf = self._shm.buf

    def close(self):
        try:
            if self._map is not None:
                self.buf.release()
                self._map.close()
                self._file.close()
            else:
                self._shm.close()
        except Exception:
            pass


def _row_runs(rowids):
    """Contiguous ``(start, count)`` runs of ascending MS row numbers.

    A block's rows are the dataset's rows -- the MS's own rows when nothing
    was selected, the ROWID selection's rows after ``--scan``.  On a real MS
    they come in a handful of long runs (one per scan), so a block is a few
    ``getcol`` calls.
    """
    if rowids.size == 0:
        return []
    breaks = np.nonzero(np.diff(rowids) != 1)[0] + 1
    return [(int(g[0]), int(g.size)) for g in np.split(rowids, breaks)]


# ---------------------------------------------------------------------------
# the child (and the sequential fallback, which is the same code)
# ---------------------------------------------------------------------------

_CHILD_TABLES = {}


def _child_table(path):
    """A read-only handle for ``path``, opened fresh if this is the child's
    first for it, and closed again when the block is done
    (:func:`_close_child_tables`).

    The handles cannot outlive the block: casacure's lock file arbitrates
    between processes, and a child parked on a read handle holds a read lock
    that the parent's write (``--apply`` writes this very table) then waits
    on forever.  Opening per block costs milliseconds against the block's
    seconds, and by the time ``pool.map`` returns -- before the run's write
    pass opens anything -- no child holds any handle.
    """
    t = _CHILD_TABLES.get(path)
    if t is None:
        from casacore.tables import table

        t = table(path, readonly=True, ack=False)
        _CHILD_TABLES[path] = t
    return t


def _close_child_tables():
    """Close this process's cached read handles, locks and all."""
    for path in list(_CHILD_TABLES):
        try:
            _CHILD_TABLES.pop(path).close()
        except Exception:
            pass


def _read_runs(tab, column, runs, read_bytes):
    parts = [tab.getcol(column, start, count) for start, count in runs]
    read_bytes[column] = read_bytes.get(column, 0) + \
        sum(part.nbytes for part in parts)
    return parts[0] if len(parts) == 1 else np.concatenate(parts, axis=0)


def _flags_from_program(program, index):
    """One block of the program's flags, running the program if it has not."""
    program.run()
    return _unpack_block(program, index)


def _unpack_block(program, index):
    name, offsets, sizes = program.shm
    start, size = int(offsets[index]), int(sizes[index])
    if name is None:
        packed = np.frombuffer(program._buffer, dtype=np.uint8,
                               count=size, offset=start)
    elif program._shm_handle is not None:
        # This process created the buffer and still maps it: slice it
        # directly, without opening anything.
        packed = np.array(program._shm_handle.buf[start:start + size],
                          dtype=np.uint8)
    else:
        mapped = _MappedShm(name)
        try:
            packed = np.array(mapped.buf[start:start + size], dtype=np.uint8)
        finally:
            mapped.close()
    shape = (int(program.row_chunks[index]),) + program.shape[1:]
    count = int(np.prod(shape))
    return np.unpackbits(packed, count=count).view(bool).reshape(shape)


def _run_block(job, _buffer=None, _offset=0):
    """Execute one block's program: in a worker process, or in this one on
    the fallback (with ``_buffer`` in place of the shared memory).

    Returns ``(stage_counts, layer_counts, base_count, read_bytes)``:
    ``stage_counts`` is one ``[flagged, existing, newly]`` row per auto-
    flagger stage, ``layer_counts`` one scope-applied mask count per layer,
    ``base_count`` the set flags of the starting point (a ``restore:``'s
    line), and ``read_bytes`` what this block read of each column.
    """
    try:
        return _run_block_open(job, _buffer, _offset)
    finally:
        # Always: a handle left open by a failed block would hold the read
        # lock the parent's write waits on (see _child_table).
        _close_child_tables()


def _run_block_open(job, _buffer, _offset):
    read_bytes = {}
    runs = job["runs"]
    tab = _child_table(job["table"])

    columns, op = job["data_source"]
    if len(columns) == 1:
        vis = _read_runs(tab, columns[0], runs, read_bytes)
    else:
        vis = _read_runs(tab, columns[0], runs, read_bytes) - \
            _read_runs(tab, columns[1], runs, read_bytes)

    base = job["base"]
    if base == "input":
        flags = _read_runs(tab, "FLAG", runs, read_bytes).astype(bool)
    elif base[0] == "version":
        vtab = _child_table(_version_table_path(job["table"], base[1]))
        flags = _read_runs(vtab, "FLAG", runs, read_bytes).astype(bool)
    else:   # ("program", name, row_chunks, offsets, sizes, cell_shape)
        flags = _unpack_bytes(
            base[1], base[3], base[4], base[2], base[5],
            job["row_start"], len(runs) and sum(r[1] for r in runs))
    base_count = int(np.count_nonzero(flags))

    class _Rows:
        pass

    rows = _Rows()
    for name in job["columns"]:
        setattr(rows, {
            "UVW": "uvw", "TIME": "time", "ANTENNA1": "antenna1",
            "ANTENNA2": "antenna2", "SCAN_NUMBER": "scan",
        }[name], _read_runs(tab, name, runs, read_bytes))
    if not hasattr(rows, "scan"):
        rows.scan = None

    scope = job["scope"]
    scope3 = None if scope is None else scope.reshape((-1, 1, 1))

    stage_counts = []
    layer_counts = []
    for op in job["ops"]:
        if op[0] == "layer":
            mask = op[1](vis, rows)
            if mask.ndim == 1:
                mask = mask[:, None, None]
            if scope3 is not None:
                mask = mask & scope3
            layer_counts.append(int(np.count_nonzero(mask)))
            flags = np.logical_or(flags, mask)
        else:
            _, kind, params, abs_input, rows_kind = op
            data = np.absolute(vis) if abs_input else vis
            block = _stage_function(kind)
            existing = flags
            if rows_kind == "baselines":
                stage_rows = (rows.antenna1, rows.antenna2,
                              rows.scan if rows.scan is not None
                              else np.full(len(vis), -1, np.int32))
            elif rows_kind == "nn":
                stage_rows = (rows.antenna1, rows.antenna2, rows.scan,
                              rows.time)
            else:
                stage_rows = None
            out = block(data, existing, params, stage_rows)
            if scope3 is not None:
                out = np.logical_or(np.logical_and(out, scope3), existing)
            stage_counts.append([
                int(np.count_nonzero(out)),
                int(np.count_nonzero(existing)),
                int(np.count_nonzero(out & ~existing)),
            ])
            flags = out

    packed = np.packbits(flags, axis=None)
    if _buffer is not None:
        _buffer[_offset:_offset + packed.nbytes] = packed
    else:
        name, offset, _size = job["shm"]
        mapped = _MappedShm(name)
        try:
            mapped.buf[offset:offset + packed.nbytes] = packed.tobytes()
        finally:
            mapped.close()
    return stage_counts, layer_counts, base_count, read_bytes


def _unpack_bytes(name, offsets, sizes, row_chunks, cell_shape, row_start,
                  nrows):
    """This block's rows of the flags a spent program left in shared memory.

    ``row_chunks``/``offsets``/``sizes`` are the spent program's and this
    block may start anywhere in them, so the blocks covering
    ``[row_start, row_start + nrows)`` are unpacked and the block's rows cut
    out of their concatenation.
    """
    if name is None:
        raise RuntimeError(
            "the previous program's flags are not in shared memory"
            " (sequential fallback); rerun without the earlier save:")
    start_block = 0
    rows_before = 0
    for n in row_chunks:
        if rows_before + int(n) > row_start:
            break
        rows_before += int(n)
        start_block += 1
    blocks = []
    covered = rows_before
    shm = _MappedShm(name)
    try:
        index = start_block
        while covered < row_start + nrows and index < len(row_chunks):
            n = int(row_chunks[index])
            packed = np.array(
                shm.buf[int(offsets[index]):int(offsets[index]) + int(sizes[index])],
                dtype=np.uint8,
            )
            shape = (n,) + tuple(cell_shape)
            blocks.append(np.unpackbits(packed, count=int(np.prod(shape)))
                          .view(bool).reshape(shape))
            covered += n
            index += 1
    finally:
        shm.close()
    return np.concatenate(blocks, axis=0)[row_start - rows_before:
                                          row_start - rows_before + nrows]


def _stage_function(kind):
    """The block function the lazy path uses, resolved in the child.

    The fork inherited skarabina.dask_ms, so this is the module the parent
    already loaded: both paths share one implementation of the algorithms.
    """
    from skarabina import dask_ms

    return {
        "rflag": dask_ms._rflag_block,
        "tfcrop": dask_ms._tfcrop_block,
        "tf_nn": dask_ms._tf_nn_block,
        "nn_flagger": dask_ms._nn_flagger_block,
    }[kind]


def _version_table_path(ms_name, versionname):
    from skarabina import flag_versions

    return flag_versions.version_path(ms_name, versionname)
