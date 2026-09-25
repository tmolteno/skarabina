#!/usr/bin/env python3
"""New flags in the RFI-free band vs the known RFI windows, on real data.

    python bench/flag_regions.py <ms> <scan> tfcrop|rflag aware|classic [--row-chunk N]

After autos, NaN and clip 0 100 -- and no spectral-window step, so the RFI
windows of bench/spectral-flags-L.yml are still live -- runs the flagger with
baseline-aware chunks on or off (dask_ms.AUTOFIT_BASELINES) and prints, per
region (RFI windows / clean band / band edges and HI) and for baselines shorter
and longer than 600 m (the file's own boundary), new flags as a fraction of the
live samples, plus the flagger's time and the peak RSS.  With no ground truth
this is the proxy used in doc/RFLAG.md §5 and §7.2: flags in the clean band are
mostly false positives, flags in the short-baseline RFI windows mostly RFI.
L band only (the windows are MeerKAT L band).
"""
import argparse
import os
import resource
import time

os.environ.setdefault("DASK_MS_BACKEND", "casacure")
import daskms  # noqa: F401,E402
import dask  # noqa: E402
import dask.array as da  # noqa: E402
import numpy as np  # noqa: E402

from skarabina import dask_ms, rflag, tfcrop  # noqa: E402

RFI = [(900, 915), (925, 960), (1080, 1095), (1565, 1585), (1217, 1237), (1375, 1387),
       (1166, 1186), (1592, 1610), (1242, 1249), (1191, 1217), (1260, 1300), (1453, 1490),
       (1616, 1626), (1526, 1554)]
EDGE = [(850, 900), (1658, 1800), (1419.8, 1421.3)]

parser = argparse.ArgumentParser()
parser.add_argument("ms")
parser.add_argument("scan")
parser.add_argument("algo", choices=("tfcrop", "rflag"))
parser.add_argument("mode", choices=("aware", "classic"))
parser.add_argument("--row-chunk", type=int, default=10000)
args = parser.parse_args()

dask_ms.AUTOFIT_BASELINES = args.mode == "aware"
ms = dask_ms.DaskMS(args.ms, row_chunk=args.row_chunk)
ms.select_scans(args.scan)
ms.flag_autocorrelations()
ms.flag_data({"NAN": True})
ms.flag_data({"CLIP": (0.0, 100.0)})
pre = ms.ds.FLAG.data
t0 = time.perf_counter()
if args.algo == "tfcrop":
    ms.flag_tfcrop(tfcrop.TFCropParams())
else:
    ms.flag_rflag(rflag.RFlagParams())
elapsed = time.perf_counter() - t0

new, live = ms.ds.FLAG.data & ~pre, ~pre
uvw = ms.ds.UVW.data
short = (da.sqrt(uvw[:, 0] ** 2 + uvw[:, 1] ** 2) < 600.0)[:, None, None]
new_s, live_s, new_l, live_l = dask.compute(
    da.sum(new & short, axis=(0, 2)), da.sum(live & short, axis=(0, 2)),
    da.sum(new & ~short, axis=(0, 2)), da.sum(live & ~short, axis=(0, 2)))
freq = ms.chan_freq_hz


def inside(ranges):
    return np.any([(freq >= a * 1e6) & (freq <= b * 1e6) for a, b in ranges], axis=0)


rfi = inside(RFI)
edge = inside(EDGE) & ~rfi
print(f"{args.algo} {args.mode}: new/live, short (<600 m) / long baselines")
for name, m in (("RFI windows", rfi), ("clean", ~rfi & ~edge), ("edges+HI", edge)):
    rs = new_s[m].sum() / max(1, live_s[m].sum())
    rl = new_l[m].sum() / max(1, live_l[m].sum())
    print(f"  {name:<12} {m.sum():5d} chan  {100 * rs:7.3f}%  {100 * rl:7.3f}%")
print(f"  flagger {elapsed:.1f} s, peak RSS"
      f" {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20:.1f} GB")
