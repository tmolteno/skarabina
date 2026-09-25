#!/usr/bin/env python3
"""Peak RSS of a --flag list run the way the CLI runs it, plus one final pass.

    python bench/mem_run.py <ms> <row_chunk> <workers> "<flag list>" [--scan S]

The list goes through flag_ops.run, then one chunked pass over FLAG and
FLAG_ROW (what a summary or a write does) evaluates everything lazy.  Prints
one RESULT line.  Peak RSS is ``base + rows in flight x nchan x ncorr x b``
(rows in flight = row_chunk x the workers that get a chunk); this is how
skarabina.memory's CHUNK_COST was measured (doc/RFLAG.md §7.3-7.4).
``save:<name>`` writes a flag version beside the MS -- use a copy.
"""
import argparse
import os
import resource
import time
from multiprocessing.pool import ThreadPool

os.environ.setdefault("DASK_MS_BACKEND", "casacure")
import daskms  # noqa: F401,E402
import dask  # noqa: E402
import dask.array as da  # noqa: E402

from skarabina import dask_ms, flag_ops  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("ms")
parser.add_argument("row_chunk", type=int)
parser.add_argument("workers", type=int)
parser.add_argument("flags", help='e.g. "nan, clip 0 100, rflag"; "none" for no verb')
parser.add_argument("--scan", default=None)
args = parser.parse_args()

dask.config.set(pool=ThreadPool(args.workers))
t0 = time.perf_counter()
ms = dask_ms.DaskMS(args.ms, row_chunk=args.row_chunk)
if args.scan:
    ms.select_scans(args.scan)
if args.flags != "none":
    flag_ops.run(ms, flag_ops.parse([args.flags]), log=lambda *_: None)
dask.compute(da.sum(ms.ds.FLAG.data), da.sum(ms.ds.FLAG_ROW.data))
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
print(f"RESULT [{args.flags}] row_chunk={args.row_chunk} workers={args.workers}"
      f" t={time.perf_counter() - t0:.1f}s maxrss_mb={rss:.0f}", flush=True)
