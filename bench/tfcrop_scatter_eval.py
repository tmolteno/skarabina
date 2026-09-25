#!/usr/bin/env python3
"""Evaluate tfcrop's short-lane scatter fallback on synthetic planes.

10 000 x 79 planes shaped like one dask chunk: a bandpass, complex noise,
pre-flags per scenario, and RFI on 0.5 % of samples at 5, 7 and 10 noise
sigmas.  Prints false positives (% of clean live samples) and recall per RFI
strength for each fallback configuration (LANE_POOL, POOL_SAMPLES,
MIN_LANE_SAMPLES), averaged over two seeds.

Scenarios: clean-preflags, dead-preflags, sparse-rows, none, and the
varying-* forms of the first/third/fourth with the noise level drifting
0.6x-1.6x through the chunk.

    python bench/tfcrop_scatter_eval.py clean-preflags varying-sparse
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import skarabina.tfcrop as T  # noqa: E402

NT, NC = 10000, 79


def plane(scenario, seed):
    rng = np.random.default_rng(seed)
    band = 10 + 4 * np.cos(np.linspace(0, 2.5, NC))
    scale = 1.0
    if scenario.startswith("varying"):
        # noise level drifting 0.6x .. 1.6x over the chunk (elevation, Tsys)
        scale = (1.1 + 0.5 * np.sin(np.linspace(0, 3 * np.pi, NT)))[:, None]
    noise = rng.normal(0, 1, (NT, NC)) + 1j * rng.normal(0, 1, (NT, NC))
    amp = np.abs(band + scale * noise)
    pre = np.zeros((NT, NC), bool)
    if scenario in ("clean-preflags", "dead-preflags", "varying-preflags"):
        pre |= rng.random((NT, NC)) < 0.45
        pre[rng.random(NT) < 0.37] = True
        pre[:, :8] = True
    elif scenario in ("sparse-rows", "varying-sparse"):          # ~85 % flagged: ~12 live per row
        pre |= rng.random((NT, NC)) < 0.85
    if scenario == "dead-preflags":
        amp[pre] = 0.0
    # RFI on 0.5 % of samples, each 5, 7 or 10 noise-sigmas above the band
    rfi = rng.random((NT, NC)) < 0.005
    strength = rng.choice([5.0, 7.0, 10.0], size=(NT, NC))
    amp = np.where(rfi, amp + strength * scale, amp)
    return amp, pre, rfi, strength


def run(amp, pre, config):
    T.LANE_POOL, T.POOL_SAMPLES, T.MIN_LANE_SAMPLES = config
    t = time.perf_counter()
    flag, _ = T.tfcrop_plane(amp, T.TFCropParams(), pre)
    return flag, time.perf_counter() - t


ALL = 10**9
configs = [("off", ("block", 0, 0)),
           ("block @all", ("block", 0, ALL)),
           ("local300 @60", ("local", 300, 60)),
           ("local300 @80", ("local", 300, 80)),
           ("local1000 @60", ("local", 1000, 60)),
           ("local1000 @80", ("local", 1000, 80))]
for scenario in sys.argv[1:]:
    print(f"\n== {scenario}")
    print(f"{'config':>15} {'FP %':>7} {'rec 5σ':>7} {'7σ':>7} {'10σ':>7} {'s/plane':>8}")
    for label, config in configs:
        fp, ts = [], []
        rec = {5.0: [], 7.0: [], 10.0: []}
        for seed in (1, 2):
            amp, pre, rfi, strength = plane(scenario, seed)
            flag, dt = run(amp, pre, config)
            ts.append(dt)
            live = ~pre
            new = flag & live
            fp.append(new[live & ~rfi].sum() / (live & ~rfi).sum())
            for k in rec:
                sel = live & rfi & (strength == k)
                rec[k].append(new[sel].sum() / max(1, sel.sum()))
        print(f"{label:>15} {100 * np.mean(fp):7.2f}"
              + "".join(f" {100 * np.mean(rec[k]):6.1f}%" for k in (5.0, 7.0, 10.0))
              + f" {np.mean(ts):8.3f}")
