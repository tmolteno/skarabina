"""skarabina#15: time averaging must group by baseline, not by row.

``time_average`` used to average every ``factor`` consecutive ROWS, which is
only correct for baseline-major input.  Real MSs are time-major (all baselines
for one timestamp, then the next), so the old code averaged *different
baselines together*, lost half the baseline labels, zeroed TIME on
fully-flagged groups and produced a row count killMS rejects
(``rows % nbl != 0``).

These tests build the time-major MS the issue asks for: 4 antennas, 6
baselines, 10 timestamps, DATA distinct per baseline and time.
"""

import numpy as np

from casacore.tables import table

from ms_fixture import make_synthetic_ms
from skarabina.dask_ms import DaskMS

NANTS = 4
BASELINES = [(i, j) for i in range(NANTS) for j in range(i + 1, NANTS)]
NBL = len(BASELINES)      # 6
NT = 10
INTERVAL = 8.0


def _build(path, nt=NT):
    """A time-major MS: NBL rows per timestamp, DATA distinct per (t, baseline)."""
    nrow = NBL * nt
    make_synthetic_ms(path, nchan=4, nrow=nrow, ncorr=1,
                      antenna_names=tuple(f"a{i}" for i in range(NANTS)),
                      interval=INTERVAL)
    a1 = np.empty(nrow, np.int32)
    a2 = np.empty(nrow, np.int32)
    times = np.empty(nrow)
    data = np.zeros((nrow, 4, 1), np.complex128)
    for t in range(nt):
        for b, (i, j) in enumerate(BASELINES):
            r = t * NBL + b
            a1[r], a2[r] = i, j
            times[r] = t * INTERVAL
            data[r, :, 0] = 1000.0 * (t + 1) + 100.0 * (b + 1)
    tab = table(path, readonly=False, ack=False)
    tab.putcol("TIME", times)
    tab.putcol("TIME_CENTROID", times)
    tab.putcol("ANTENNA1", a1)
    tab.putcol("ANTENNA2", a2)
    tab.putcol("DATA", data)
    tab.close()
    return times, a1, a2, data


def test_time_average_groups_by_baseline(tmp_path):
    """factor 2 on a time-major MS: 5 timestamps x 6 baselines, per-baseline means."""
    path = str(tmp_path / "tm.ms")
    times, a1, a2, data = _build(path)

    ms = DaskMS(path, row_chunk=1000)
    ms.time_average(2)

    out_t = ms.ds.TIME.data.compute()
    out_a1 = ms.ds.ANTENNA1.data.compute()
    out_a2 = ms.ds.ANTENNA2.data.compute()
    out_d = ms.ds.DATA.data.compute()
    out_i = ms.ds.INTERVAL.data.compute()

    # 5 timestamps x 6 baselines, no rows discarded (10 is even)
    assert len(out_t) == (NT // 2) * NBL, len(out_t)
    assert len(out_t) % NBL == 0

    # timestamps halved and 16 s apart
    ut = np.unique(out_t)
    assert len(ut) == NT // 2, ut
    assert np.allclose(np.diff(ut), 2 * INTERVAL), ut

    # every output timestamp carries the full baseline set
    for u in ut:
        sel = out_t == u
        pairs = sorted(zip(out_a1[sel].tolist(), out_a2[sel].tolist()))
        assert pairs == sorted(BASELINES), (u, pairs)

    # each output DATA is the mean of the SAME baseline over 2 timestamps
    for u_i in range(NT // 2):
        centre = (2 * u_i + 0.5) * INTERVAL    # mean of TIMEs 2u*8 and (2u+1)*8
        for b, (i, j) in enumerate(BASELINES):
            sel = np.flatnonzero((out_t == centre)
                                 & (out_a1 == i) & (out_a2 == j))
            assert len(sel) == 1, (u_i, b, sel)
            want = 0.5 * (data[u_i * 2 * NBL + b, 0, 0]
                          + data[(u_i * 2 + 1) * NBL + b, 0, 0])
            assert np.allclose(out_d[sel[0], 0, 0], want), (u_i, b)

    # no zeroed TIME, INTERVAL doubles
    assert not np.any(out_i == 0.0)
    assert np.allclose(np.unique(out_i), 2 * INTERVAL)


def test_all_flagged_bin_keeps_valid_time(tmp_path):
    """A fully-flagged bin keeps TIME/UVW and stays flagged (not zeroed)."""
    path = str(tmp_path / "flag.ms")
    times, a1, a2, data = _build(path)
    # rows 24 and 30 are the two samples of baseline (0,1) at t=4 and t=5:
    # exactly one factor-2 bin, fully flagged.
    tab = table(path, readonly=False, ack=False)
    flag = tab.getcol("FLAG")
    flag[24] = flag[30] = True
    fr = tab.getcol("FLAG_ROW")
    fr[24] = fr[30] = True
    tab.putcol("FLAG", flag)
    tab.putcol("FLAG_ROW", fr)
    tab.close()

    ms = DaskMS(path, row_chunk=1000)
    ms.time_average(2)
    out_t = ms.ds.TIME.data.compute()
    out_fr = ms.ds.FLAG_ROW.data.compute()
    out_d = ms.ds.DATA.data.compute()

    zeroed = np.flatnonzero(out_fr)
    assert len(zeroed) == 1
    k = zeroed[0]
    assert out_t[k] != 0.0, "an all-flagged bin must keep a valid TIME"
    assert np.isfinite(out_d[k]).all()
    # it stays flagged
    assert ms.ds.FLAG.data.compute()[k].all()
    # ...and the unflagged bins kept their own per-baseline means
    unflagged = np.flatnonzero(~out_fr)
    assert len(unflagged) == (NT // 2) * NBL - 1


def test_factor_not_dividing_nbl_keeps_grid(tmp_path):
    """factor 4 (does not divide nbl=6): bins never straddle timestamps."""
    path = str(tmp_path / "grid.ms")
    _build(path)

    ms = DaskMS(path, row_chunk=1000)
    ms.time_average(4)

    out_t = ms.ds.TIME.data.compute()
    out_a1 = ms.ds.ANTENNA1.data.compute()
    out_a2 = ms.ds.ANTENNA2.data.compute()

    # 10 timestamps -> 2 complete bins of 4 per baseline (8 samples), tails dropped
    assert len(out_t) == 2 * NBL, len(out_t)
    assert len(out_t) % NBL == 0
    ut = np.unique(out_t)
    assert len(ut) == 2, ut
    # bin centres sit on the cadence grid: mean(t0..t3)=12, mean(t4..t7)=44
    assert np.allclose(ut, [12.0, 44.0]), ut
    for u in ut:
        sel = out_t == u
        pairs = sorted(zip(out_a1[sel].tolist(), out_a2[sel].tolist()))
        assert pairs == sorted(BASELINES), (u, pairs)


def test_bins_never_cross_scan_boundaries(tmp_path):
    """factor 4 with two scans: no bin mixes scan 1 and scan 2 rows."""
    path = str(tmp_path / "scan.ms")
    _build(path)
    tab = table(path, readonly=False, ack=False)
    scan = tab.getcol("SCAN_NUMBER").astype(np.int32)
    scan[:] = 1
    scan[6 * 5:] = 2          # timestamps 5..9 belong to scan 2
    tab.putcol("SCAN_NUMBER", scan)
    tab.close()

    ms = DaskMS(path, row_chunk=1000)
    ms.time_average(2)

    out_t = ms.ds.TIME.data.compute()
    out_scan = ms.ds.SCAN_NUMBER.data.compute()
    out_a1 = ms.ds.ANTENNA1.data.compute()
    out_a2 = ms.ds.ANTENNA2.data.compute()

    # scan 1: timestamps 0..4 -> 2 full bins (+0 tail); scan 2: 5..9 -> 2 bins
    assert len(out_t) == 4 * NBL, len(out_t)
    assert len(out_t) % NBL == 0
    # bins stay inside their scan: scan 1 bins centre at 4 and 20,
    # scan 2 bins at 44 and 60 — never a mixed midpoint like 4.0/28.0.
    for s in (1, 2):
        sel = out_scan == s
        assert sel.sum() == 2 * NBL, (s, sel.sum())
        for u in np.unique(out_t[sel]):
            rows = sel & (out_t == u)
            pairs = sorted(zip(out_a1[rows].tolist(), out_a2[rows].tolist()))
            assert pairs == sorted(BASELINES), (s, u, pairs)
    assert not np.any((out_scan == 1) & (out_t > 32.0)), "a scan-1 bin leaked forward"


def test_odd_sample_count_drops_only_the_tail(tmp_path):
    """factor 2 with 9 timestamps: one trailing sample per baseline dropped."""
    path = str(tmp_path / "odd.ms")
    _build(path, nt=9)

    ms = DaskMS(path, row_chunk=1000)
    ms.time_average(2)

    out_t = ms.ds.TIME.data.compute()
    assert len(out_t) == 4 * NBL, len(out_t)   # 8 of 9 samples averaged
    assert len(out_t) % NBL == 0
    assert len(np.unique(out_t)) == 4
    assert np.allclose(np.diff(np.unique(out_t)), 2 * INTERVAL)
