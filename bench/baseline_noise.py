#!/usr/bin/env python3
"""What predicts a baseline's noise: its length, or its antennas?

    python bench/baseline_noise.py <ms> <scan>

The measurement behind skarabina.baselines.antenna_noise_model (doc/RFLAG.md
§4).  Loads the scan's correlation 0 (~3 GB for a MeerKAT L-band scan).

Per cross baseline, corr 0, clean band only (outside every window of
spectral-flags-L.yml and the band edges), unflagged samples:
  noise  = robust sigma of adjacent-channel complex differences / sqrt(2)
           (the bandpass and the source cancel in a one-channel difference)
  level  = median amplitude
  length = mean |uv|
"""
import os

os.environ.setdefault("DASK_MS_BACKEND", "casacure")
import daskms  # noqa: F401,E402
from casacore.tables import table  # noqa: E402
import numpy as np  # noqa: E402

import sys  # noqa: E402

MS, SCAN = sys.argv[1], int(sys.argv[2])
RFI = [(900, 915), (925, 960), (1080, 1095), (1565, 1585), (1217, 1237), (1375, 1387), (1166, 1186),
       (1592, 1610), (1242, 1249), (1191, 1217), (1260, 1300), (1453, 1490), (1616, 1626), (1526, 1554),
       (850, 900), (1658, 1800), (1419.8, 1421.3)]
t = table(MS, ack=False)
rows = np.flatnonzero(t.getcol("SCAN_NUMBER") == SCAN)
r0, n = int(rows[0]), int(rows[-1] + 1 - rows[0])
freq = table(MS + "/SPECTRAL_WINDOW", ack=False).getcol("CHAN_FREQ")[0]
clean = ~np.any([(freq >= a * 1e6) & (freq <= b * 1e6) for a, b in RFI], axis=0)
a1, a2 = t.getcol("ANTENNA1", r0, n), t.getcol("ANTENNA2", r0, n)
uvw = t.getcol("UVW", r0, n)
data = t.getcolslice("DATA", [0, 0], [-1, 0], startrow=r0, nrow=n)[:, :, 0]
flag = t.getcolslice("FLAG", [0, 0], [-1, 0], startrow=r0, nrow=n)[:, :, 0]
flag |= ~np.isfinite(data) | (np.abs(data) >= 100) | (np.abs(data) <= 0)
flag[:, ~clean] = True

key = a1.astype(np.int64) * 1000 + a2
bl, inverse = np.unique(key[a1 != a2], return_inverse=True)
cross = np.flatnonzero(a1 != a2)
out = []
for b in range(bl.size):
    sel = cross[inverse == b]
    d, f = data[sel], flag[sel]
    both = ~f[:, 1:] & ~f[:, :-1]
    if both.sum() < 200:
        continue
    diff = (d[:, 1:] - d[:, :-1])[both]
    parts = np.concatenate([diff.real, diff.imag])
    noise = 1.4826 * np.median(np.abs(parts - np.median(parts))) / np.sqrt(2)
    level = np.median(np.abs(d[~f]))
    length = np.mean(np.hypot(uvw[sel, 0], uvw[sel, 1]))
    out.append((bl[b] // 1000, bl[b] % 1000, noise, level, length))
i, j, noise, level, length = (np.array(c) for c in zip(*out))
i, j = i.astype(int), j.astype(int)
print(f"{i.size} baselines, lengths {length.min():.0f}-{length.max():.0f} m, "
      f"noise {np.percentile(noise, 5):.4g}-{np.percentile(noise, 95):.4g} (5-95 %)")


def spearman(x, y):
    rx, ry = np.argsort(np.argsort(x)), np.argsort(np.argsort(y))
    return np.corrcoef(rx, ry)[0, 1]


ln = np.log(noise)
print(f"Spearman(noise, length)        = {spearman(noise, length):+.3f}")
print(f"Spearman(noise/level, length)  = {spearman(noise / level, length):+.3f}")
print(f"Spearman(level, length)        = {spearman(level, length):+.3f}")

# Antenna model: log noise_ij = s_i + s_j  (noise ~ sqrt(SEFD_i SEFD_j) |g_i g_j|)
ants = np.unique(np.concatenate([i, j]))
col = {a: k for k, a in enumerate(ants)}
A = np.zeros((i.size, ants.size))
A[np.arange(i.size), [col[a] for a in i]] = 1
A[np.arange(i.size), [col[a] for a in j]] += 1
coef, *_ = np.linalg.lstsq(A, ln, rcond=None)
resid_ant = ln - A @ coef
# Length model: log noise vs length in 10 quantile bins
edges = np.quantile(length, np.linspace(0, 1, 11))
binid = np.clip(np.searchsorted(edges, length, side="right") - 1, 0, 9)
resid_len = ln - np.array([np.median(ln[binid == k]) for k in range(10)])[binid]
# Both
B = np.hstack([A, np.eye(10)[binid][:, 1:]])
coef2, *_ = np.linalg.lstsq(B, ln, rcond=None)
resid_both = ln - B @ coef2


def rmad(r):
    return 1.4826 * np.median(np.abs(r - np.median(r)))


print("spread of log(noise) across baselines, robust sigma (0.10 ~ 10 %):")
print(f"  no model              {rmad(ln - np.median(ln)):.3f}")
print(f"  length bins only      {rmad(resid_len):.3f}")
print(f"  antenna terms only    {rmad(resid_ant):.3f}")
print(f"  antenna + length      {rmad(resid_both):.3f}")
worst = np.argsort(np.abs(resid_ant))[-5:]
print("largest antenna-model misfits (ant pair, length m, noise / predicted):")
for k in worst[::-1]:
    print(f"  {i[k]:3d}-{j[k]:<3d} {length[k]:7.0f}  {np.exp(resid_ant[k]):.2f}x")
ant_scale = np.exp(coef - np.median(coef))
print(f"per-antenna noise factor, 5-95 %:"
      f" {np.percentile(ant_scale, 5):.2f}-{np.percentile(ant_scale, 95):.2f}; "
      f"outliers >1.5x: {[int(a) for a, s in zip(ants, ant_scale) if s > 1.5 or s < 1 / 1.5]}")
