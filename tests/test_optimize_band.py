# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""``--optimize`` and the band it leaves behind.

Removing a fully-flagged channel from the middle of a band leaves a hole that no
SPECTRAL_WINDOW column records: ``CHAN_WIDTH`` still describes each surviving
channel and ``TOTAL_BANDWIDTH`` still sums what is left.  These tests pin what
``optimize`` does about that -- it warns, and it can be told to keep the
channels instead.
"""
import numpy as np
import pytest
from casacore.tables import table

from skarabina.analyze import band_info
from skarabina.dask_ms import DaskMS
from ms_fixture import make_synthetic_ms

NCHAN = 6
WIDTH_HZ = 1.0e7


@pytest.fixture
def ms_with_dead_middle(tmp_path):
    """An MS whose channel 2 (interior) is flagged in every row."""
    path = str(tmp_path / "hole.ms")
    make_synthetic_ms(path, nchan=NCHAN, ncorr=1, nrow=6)
    t = table(path, readonly=False)
    flags = t.getcol("FLAG").copy()
    flags[:, 2, :] = True
    t.putcol("FLAG", flags)
    t.close()
    return path


def _freqs(path):
    sw = table(path + "/SPECTRAL_WINDOW", ack=False, readonly=True)
    try:
        return sw.getcol("CHAN_FREQ")[0]
    finally:
        sw.close()


def test_optimize_keeps_the_band_contiguous_when_asked(ms_with_dead_middle):
    """--keep-fully-flagged-channels leaves every channel in place."""
    ds = DaskMS(ms_with_dead_middle)
    ds.optimize(keep_fully_flagged_channels=True)
    assert ds.ds.FLAG.shape[1] == NCHAN, "no channel should have been dropped"
    # the bookkeeping must be untouched too
    assert len(ds.chan_freq_hz) == NCHAN
    assert band_info(ms_with_dead_middle).n_chan == NCHAN
    ds.write_new_ms("/tmp/keep_chan_out.ms", clobber=True)
    band = band_info("/tmp/keep_chan_out.ms")
    assert not band.has_gaps
    assert band.hole_hz == pytest.approx(0.0, abs=1.0)


def test_optimize_drops_the_dead_channel_by_default(ms_with_dead_middle):
    ds = DaskMS(ms_with_dead_middle)
    ds.optimize()
    assert ds.ds.FLAG.shape[1] == NCHAN - 1
    ds.write_new_ms("/tmp/drop_chan_out.ms", clobber=True)
    band = band_info("/tmp/drop_chan_out.ms")
    assert band.n_chan == NCHAN - 1
    assert band.has_gaps, "removing an interior channel must register as a hole"
    assert band.hole_hz == pytest.approx(WIDTH_HZ, rel=1e-6)
    # the surviving channels keep their own width; only the count changed
    assert band.channel_width_hz == pytest.approx(WIDTH_HZ, rel=1e-6)
    freqs = _freqs("/tmp/drop_chan_out.ms")
    assert np.diff(freqs).tolist() == pytest.approx(
        [WIDTH_HZ, 2 * WIDTH_HZ, WIDTH_HZ, WIDTH_HZ], rel=1e-6
    )


def test_optimize_warns_when_it_splits_the_band(ms_with_dead_middle, capsys):
    DaskMS(ms_with_dead_middle).optimize()
    out = capsys.readouterr().out
    assert "WARNING: dropping 1 fully flagged channel(s) split the band" in out
    assert "--keep-fully-flagged-channels" in out
    assert "not recorded in SPECTRAL_WINDOW" in out


def test_optimize_does_not_warn_when_the_band_stays_contiguous(tmp_path, capsys):
    """Dropping channels from the *edge* only shortens the band."""
    path = str(tmp_path / "edge.ms")
    make_synthetic_ms(path, nchan=NCHAN, ncorr=1, nrow=6)
    t = table(path, readonly=False)
    flags = t.getcol("FLAG").copy()
    flags[:, -1, :] = True          # last channel, not interior
    t.putcol("FLAG", flags)
    t.close()

    ds = DaskMS(path)
    ds.optimize()
    out = capsys.readouterr().out
    assert ds.ds.FLAG.shape[1] == NCHAN - 1
    assert "split the band" not in out
    ds.write_new_ms("/tmp/edge_chan_out.ms", clobber=True)
    band = band_info("/tmp/edge_chan_out.ms")
    assert not band.has_gaps
    assert band.hole_hz == pytest.approx(0.0, abs=1.0)


def test_keeping_channels_still_drops_the_rows(tmp_path, capsys):
    """The option is specifically about channels, not rows."""
    path = str(tmp_path / "rows.ms")
    make_synthetic_ms(path, nchan=4, ncorr=1, nrow=6)
    t = table(path, readonly=False)
    fr = t.getcol("FLAG_ROW").copy()
    fr[0] = True
    t.putcol("FLAG_ROW", fr)
    t.close()

    ds = DaskMS(path)
    ds.optimize(keep_fully_flagged_channels=True)
    assert ds.ds.FLAG.shape[0] == 5, "the FLAG_ROW row should still go"
    assert ds.ds.FLAG.shape[1] == 4


def test_fully_flagged_ms_trips_the_row_guard_first(tmp_path):
    """A wholly-flagged MS is refused by the row guard, not the channel guard.

    Every channel is dead exactly when every row is dead, so the
    "All channels fully flagged" guard below is unreachable for a single
    dataset -- worth pinning, because it means the channel guard's message can
    never be what a user sees for this input.
    """
    path = str(tmp_path / "all_dead.ms")
    make_synthetic_ms(path, nchan=3, ncorr=1, nrow=4)
    t = table(path, readonly=False)
    flags = t.getcol("FLAG").copy()
    flags[...] = True
    t.putcol("FLAG", flags)
    t.close()

    # the two conditions coincide
    f = np.asarray(DaskMS(path).ds.FLAG.data)
    assert f.all(axis=(0, 2)).all()          # every channel dead
    assert f.all(axis=(1, 2)).all()          # every row dead

    with pytest.raises(RuntimeError, match="No unflagged rows remain"):
        DaskMS(path).optimize()
    with pytest.raises(RuntimeError, match="No unflagged rows remain"):
        DaskMS(path).optimize(keep_fully_flagged_channels=True)
