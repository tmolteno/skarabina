#!/usr/bin/env python3
"""Peak working memory of one tfcrop/rflag block against its row count.

    python bench/mem_block.py <ms> <scan> 2500,5000,10000 [aware,classic]

Reads the first rows of the scan (both correlations), flags autos, NaN and
|v| outside (0, 100), then runs each block function single-threaded under
tracemalloc; the inputs exist before tracing starts, so the peak is the
algorithm's own working set.  Linear in the rows = scales with the chunk.
"""
import os
import sys
import time
import tracemalloc

os.environ.setdefault("DASK_MS_BACKEND", "casacure")
import daskms  # noqa: F401,E402
from casacore.tables import table  # noqa: E402
import numpy as np  # noqa: E402

from skarabina import dask_ms  # noqa: E402
from skarabina.rflag import RFlagParams  # noqa: E402
from skarabina.tfcrop import TFCropParams  # noqa: E402

ms_path, scan_number = sys.argv[1], int(sys.argv[2])
sizes = [int(n) for n in sys.argv[3].split(",")]
modes = sys.argv[4].split(",") if len(sys.argv) > 4 else ["aware"]
t = table(ms_path, ack=False)
scan = t.getcol("SCAN_NUMBER")
r0, nmax = int(np.flatnonzero(scan == scan_number)[0]), max(sizes)
data = t.getcol("DATA", r0, nmax)
flag = t.getcol("FLAG", r0, nmax)
a1, a2 = t.getcol("ANTENNA1", r0, nmax), t.getcol("ANTENNA2", r0, nmax)
sc = scan[r0:r0 + nmax]
amp = np.abs(data)
flag |= ~np.isfinite(amp) | (amp <= 0) | (amp >= 100)
flag[a1 == a2] = True
print(f"{'algo':>6} {'mode':>8} {'rows':>6} {'DATA MB':>8} {'peak MB':>8} {'MB/1k rows':>10} {'s':>6}")
for algo in ("tfcrop", "rflag"):
    block = dask_ms._prepare_block(
        dask_ms._tfcrop_block if algo == "tfcrop" else dask_ms._rflag_block)
    params = TFCropParams() if algo == "tfcrop" else RFlagParams()
    for mode in modes:
        for n in sizes:
            d, f = np.ascontiguousarray(data[:n]), np.ascontiguousarray(flag[:n])
            rows = (a1[:n], a2[:n], sc[:n]) if mode == "aware" else None
            tracemalloc.start()
            t0 = time.perf_counter()
            block(d, f, params, rows)
            dt = time.perf_counter() - t0
            peak = tracemalloc.get_traced_memory()[1] / 2**20
            tracemalloc.stop()
            print(f"{algo:>6} {mode:>8} {n:6d} {d.nbytes / 2**20:8.0f} {peak:8.0f}"
                  f" {1000 * peak / n:10.1f} {dt:6.1f}", flush=True)
