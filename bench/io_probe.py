#!/usr/bin/env python3
"""Count MS column reads per dask.compute while running the skarabina CLI.

Usage: python bench/io_probe.py <skarabina args...>
       e.g. python bench/io_probe.py --ms x.ms --flag "nan, autos" --summary

DATA read N times over its size = N passes.  To compare a version, run from a
worktree with PYTHONPATH=<worktree> (the "code from" line says which ran).
Wraps daskms.reads.ndarray_getcol (every chunk read of an array column goes
through it) and dask.compute / Array.compute, attributing reads to the
skarabina function that called compute.  Prints a table at exit.
"""
import atexit
import collections
import inspect
import os
import sys
import threading

os.environ.setdefault("DASK_MS_BACKEND", "casacure")
import daskms  # noqa: F401,E402
import daskms.reads as reads  # noqa: E402
import dask  # noqa: E402
import dask.array as da  # noqa: E402

lock = threading.Lock()
current = ["(outside compute)"]
tally = collections.OrderedDict()

orig_getcol = reads.ndarray_getcol


def counting_getcol(row_runs, table_future, column, result, dtype):
    out = orig_getcol(row_runs, table_future, column, result, dtype)
    with lock:
        per = tally.setdefault(current[0], collections.Counter())
        per[column] += result.nbytes
    return out


reads.ndarray_getcol = counting_getcol


def caller():
    for frame in inspect.stack()[2:]:
        if "/skarabina/" in frame.filename and "ioprobe" not in frame.filename:
            return f"{os.path.basename(frame.filename)}:{frame.function}"
    return "?"


orig_compute = dask.compute


def compute(*args, **kw):
    label = caller()
    prev, current[0] = current[0], label
    try:
        return orig_compute(*args, **kw)
    finally:
        current[0] = prev


dask.compute = compute
orig_arr_compute = da.Array.compute


def arr_compute(self, **kw):
    label = caller()
    prev, current[0] = current[0], label
    try:
        return orig_arr_compute(self, **kw)
    finally:
        current[0] = prev


da.Array.compute = arr_compute


def report():
    print("\nIOPROBE reads per compute (MB):", file=sys.stderr)
    totals = collections.Counter()
    for label, per in tally.items():
        cols = ", ".join(f"{c}={b / 2**20:.2f}" for c, b in per.most_common())
        print(f"IOPROBE   {label:<40} {cols}", file=sys.stderr)
        totals.update(per)
    print("IOPROBE TOTAL " + ", ".join(f"{c}={b / 2**20:.2f}" for c, b in totals.most_common()),
          file=sys.stderr)


atexit.register(report)

from skarabina.main import main  # noqa: E402
import skarabina  # noqa: E402
print("IOPROBE code from", skarabina.__file__, file=sys.stderr)

main(sys.argv[1:], standalone_mode=False)
