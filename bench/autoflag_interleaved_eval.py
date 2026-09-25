#!/usr/bin/env python3
"""Classic vs baseline-aware tfcrop and rflag on synthetic MS-style chunks.

Rows are written time-major -- every baseline in every integration, as an MS
is -- with each baseline at its own complex level on a sloping band,
per-antenna noise, 30 % pre-flagged, 2-integration bursts on 20 baselines,
narrow-band spikes, and one baseline with 3x the noise its antennas predict.
Prints new flags on clean live samples (FP), recall overall and per RFI type,
and how much of the noisy baseline is flagged.  doc/RFLAG.md §7.1.

    python bench/autoflag_interleaved_eval.py
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skarabina.baselines import Baselines  # noqa: E402
from skarabina.rflag import RFlagParams, rflag_plane  # noqa: E402
from skarabina.tfcrop import TFCropParams, tfcrop_plane  # noqa: E402


def chunk(nant=20, ntime=50, nchan=128, seed=0, preflag=0.3, complex_data=True):
    rng = np.random.default_rng(seed)
    pairs = [(i, j) for i in range(nant) for j in range(i + 1, nant)]
    nb = len(pairs)
    a1 = np.tile([p[0] for p in pairs], ntime)
    a2 = np.tile([p[1] for p in pairs], ntime)
    b = np.tile(np.arange(nb), ntime)
    t = np.repeat(np.arange(ntime), nb)
    s = np.exp(rng.normal(0, 0.1, nant))                     # antenna noise factors
    sigma = (s[a1] * s[a2])[:, None]
    band = 1 + 0.3 * np.cos(np.linspace(0, 2.5, nchan))
    level = (rng.uniform(5, 15, nb) * np.exp(1j * rng.uniform(0, 2 * np.pi, nb)))[b][:, None]
    vis = level * band + sigma * (rng.normal(size=(b.size, nchan)) + 1j * rng.normal(size=(b.size, nchan)))
    rfi = np.zeros(vis.shape, dtype=bool)
    burst = np.zeros(vis.shape, dtype=bool)
    # time bursts: 20 baselines, 2 integrations, 10 channels, 8 sigma
    for bb in rng.choice(nb, 20, replace=False):
        t0, c0 = rng.integers(5, ntime - 5), rng.integers(10, nchan - 20)
        rows = (b == bb) & (t >= t0) & (t < t0 + 2)
        burst[np.ix_(rows, np.arange(c0, c0 + 10))] = True
    rfi |= burst
    # narrowband spikes: 3 channels, all baselines, 30 % of integrations
    for c in rng.choice(np.arange(5, nchan - 5), 3, replace=False):
        rfi[np.isin(t, rng.choice(ntime, ntime * 3 // 10, replace=False)), c] = True
    vis = np.where(rfi, vis + 8 * sigma * np.exp(1j * rng.uniform(0, 2 * np.pi, vis.shape)), vis)
    # one noisy baseline (3x noise, no RFI): must not be flagged wholesale
    noisy = b == 7
    vis[noisy] = level[noisy] * band + 3 * sigma[noisy] * (rng.normal(size=(noisy.sum(), nchan))
                                                           + 1j * rng.normal(size=(noisy.sum(), nchan)))
    pre = rng.random(vis.shape) < preflag
    if not complex_data:
        vis = np.abs(vis)
    chunk.burst = burst
    return vis.astype(np.complex64 if complex_data else float), pre, rfi, a1, a2, b, noisy


def score(label, flag, pre, rfi, noisy):
    live = ~pre
    new = flag & live
    clean = live & ~rfi
    burst = chunk.burst
    print(f"{label:>30}: FP clean {100 * new[clean].mean():6.2f}%"
          f"  recall {100 * new[live & rfi].mean():6.1f}%"
          f"  [bursts {100 * new[live & burst].mean():5.1f}%,"
          f" narrowband {100 * new[live & rfi & ~burst].mean():5.1f}%]"
          f"  noisy baseline flagged {100 * new[noisy][live[noisy]].mean():6.2f}%")


if __name__ == "__main__":
    for seed in (0, 1):
        vis, pre, rfi, a1, a2, b, noisy = chunk(seed=seed)
        print(f"-- seed {seed}: {vis.shape[0]} rows, {100 * rfi[~pre].mean():.2f}% of live samples RFI")
        amplitude = np.abs(vis).astype(float)
        for name, run, data in (("tfcrop", tfcrop_plane, amplitude), ("rflag", rflag_plane, vis)):
            params = TFCropParams() if name == "tfcrop" else RFlagParams()
            for label, kwargs in (("classic", {}), ("baseline-aware", {"baselines": Baselines(a1, a2)})):
                t0 = time.perf_counter()
                flag, _ = run(data, params, pre, **kwargs)
                score(f"{name} {label} ({time.perf_counter() - t0:.2f}s)", flag, pre, rfi, noisy)
