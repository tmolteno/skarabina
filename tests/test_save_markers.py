# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""``save:`` markers back up the flags as they stand where they appear (#5).

A run's flags live in memory until ``--apply``/``--msout`` writes at the
end, so a marker that is not the leading entry must snapshot the in-memory
state -- merged over the on-disk flags at their ROWID positions, so the
version stays whole-MS and passes the row-count check on restore -- rather
than re-read the pre-run MS.  A leading marker (nothing before it that could
change the flags) still reads the MS on disk, exactly as before.

The main test mirrors the issue's repro shape: a small synthetic MS, one
marker list around a clip, and the question of whether the saved version's
flag percentage matches the post-``--apply`` MS instead of 0.
"""
import numpy as np
import pytest
from click.testing import CliRunner

from skarabina import dask_ms  # noqa: F401  (daskms before casacore.tables)
from casacore.tables import table  # noqa: E402

from ms_fixture import make_synthetic_ms  # noqa: E402
from skarabina import flag_versions  # noqa: E402
from skarabina.dask_ms import DaskMS  # noqa: E402
from skarabina.main import main  # noqa: E402

# Shape of the issue's repro: a 9990-row MS where clip 0 100 flags 0.480 %
# of the visibilities (384 of 9990 x 4 x 2).
NROW, NCHAN, NCORR = 9990, 4, 2
CLIP_ROWS, CLIP_CHAN, CLIP_CORR = 384, 1, 0
NAN_ROWS = slice(9000, 9048)
NAN_CHAN, NAN_CORR = 2, 1


def _run(*args):
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result


def _ms_flags(path):
    """The MS's FLAG column on disk."""
    t = table(path, ack=False, readonly=True)
    try:
        return t.getcol("FLAG")
    finally:
        t.close()


def _pct(flag):
    """The flag percentage, as the issue's output reports it."""
    return 100.0 * np.count_nonzero(flag) / flag.size if flag.size else 0.0


@pytest.fixture
def ms(tmp_path):
    """A flagless 9990-row MS where ``clip 0 100`` flags 0.480 % of the
    visibilities and a handful of visibilities are NaN for ``nan`` to find.
    """
    path = make_synthetic_ms(str(tmp_path / "obs.ms"), nchan=NCHAN, nrow=NROW,
                             ncorr=NCORR)
    t = table(path, readonly=False, ack=False)
    data = t.getcol("DATA")
    data[:CLIP_ROWS, CLIP_CHAN, CLIP_CORR] = 500.0
    data[NAN_ROWS, NAN_CHAN, NAN_CORR] = np.nan
    t.putcol("DATA", data)
    t.close()
    return path


def test_non_leading_save_captures_the_post_run_flags(ms):
    """The issue's repro: the marker after the clip holds the clipped flags.

    ``flags.start`` is the pre-run state (0 %), ``flags.after-clip`` the
    state at its position in the list -- equal to the post-``--apply`` MS,
    not to 0.
    """
    _run("--ms", ms, "--flag", "save:start, clip 0 100, save:after-clip",
         "--apply", "--clobber")

    applied = _ms_flags(ms)
    start, _ = flag_versions.load_version(ms, "start")
    after, _ = flag_versions.load_version(ms, "after-clip")

    assert np.count_nonzero(applied) == CLIP_ROWS, "the clip must flag something"
    assert _pct(after) == _pct(applied), "the snapshot equals the post-apply MS"
    assert _pct(after) == pytest.approx(0.480, abs=0.001), "the issue's 0.480 %"
    assert np.array_equal(after, applied), "exactly, visibility for visibility"
    # The leading marker is untouched: it still holds the pre-run flags.
    assert not start.any()
    assert _pct(start) == 0.0


def test_leading_save_still_backs_up_the_ondisk_flags(ms, monkeypatch):
    """A leading marker keeps the plain streamed read of the MS on disk.

    The MS is pre-flagged on disk so the assertion cannot pass by accident,
    and the marker's path is pinned: ``overlay=None`` is the byte-identical
    code path the leading marker used before the fix.
    """
    pre = np.zeros((NROW, NCHAN, NCORR), bool)
    pre[:100] = True  # 100 fully flagged rows on disk, before the run
    t = table(ms, readonly=False, ack=False)
    t.putcol("FLAG", pre)
    t.close()

    recorded = {}
    real = flag_versions.save_version_streaming

    def spy(ms_path, versionname, comment="", overlay=None):
        recorded[versionname] = overlay
        return real(ms_path, versionname, comment=comment, overlay=overlay)

    monkeypatch.setattr(flag_versions, "save_version_streaming", spy)

    _run("--ms", ms, "--flag", "save:start, clip 0 100, save:after-clip",
         "--apply", "--clobber")

    assert recorded["start"] is None, "the leading marker must take the disk path"
    assert recorded["after-clip"] is not None, "a later marker snapshots memory"
    start, _ = flag_versions.load_version(ms, "start")
    assert np.array_equal(start, pre), "the leading marker holds the on-disk flags"
    applied = _ms_flags(ms)
    assert not np.array_equal(start, applied), "and not the post-run state"
    after, _ = flag_versions.load_version(ms, "after-clip")
    assert np.array_equal(after, applied), "the later marker holds the run's flags"


def test_each_marker_holds_the_state_at_its_own_position(ms):
    """Two snapshots in one run: each captures its own point in the list.

    The second marker also exercises the re-materialise path (the flags have
    changed since the first snapshot spilled them).
    """
    _run("--ms", ms, "--flag",
         "clip 0 100, save:after-clip, nan, save:after-nan", "--apply",
         "--clobber")

    clip_only, _ = flag_versions.load_version(ms, "after-clip")
    with_nan, _ = flag_versions.load_version(ms, "after-nan")
    applied = _ms_flags(ms)

    assert np.count_nonzero(clip_only) == CLIP_ROWS, "the first marker: clip only"
    assert not clip_only[NAN_ROWS, NAN_CHAN, NAN_CORR].any(), "NaNs not flagged yet"
    assert with_nan[NAN_ROWS, NAN_CHAN, NAN_CORR].all(), "the second marker: + nan"
    assert np.count_nonzero(with_nan) == CLIP_ROWS + (NAN_ROWS.stop - NAN_ROWS.start)
    assert np.array_equal(with_nan, applied), "and the run's final state"


def test_save_after_restore_snapshots_the_restored_flags(ms):
    """The documented re-labelling idiom: ``restore:X, save:Y`` copies X.

    The MS on disk holds flags; ``start`` holds none.  ``save:relabel`` must
    copy what the restore put in memory, not re-read the flagged MS (#5's
    second reproducer).
    """
    on_disk, _ = flag_versions.read_ms_flags(ms)
    assert not on_disk.any(), "the fixture MS starts flagless"
    flag_versions.save_version(
        ms, "start", np.zeros_like(on_disk),
        np.zeros(on_disk.shape[0], bool), comment="unflagged",
    )
    t = table(ms, readonly=False, ack=False)
    t.putcol("FLAG", np.ones_like(on_disk))
    t.close()

    _run("--ms", ms, "--flag", "restore:start, save:relabel", "--apply",
         "--clobber")

    start, _ = flag_versions.load_version(ms, "start")
    relabel, _ = flag_versions.load_version(ms, "relabel")
    applied = _ms_flags(ms)
    assert np.array_equal(relabel, start), "relabel is a copy of start"
    assert not relabel.any()
    assert np.array_equal(applied, relabel), "--apply wrote the restored state"


def test_row_selection_snapshot_merges_into_a_whole_ms_version(tmp_path):
    """Under ``--scan`` the snapshot merges over the rows the run does not
    hold: the version keeps the MS's row count (the row-count check a
    restore performs survives), holds the run's flags on the selected rows
    and the on-disk flags everywhere else -- exactly what ``--apply``
    writes back.
    """
    path = make_synthetic_ms(str(tmp_path / "scan.ms"), nrow=8, nchan=4,
                             scan_numbers=[1, 2, 1, 2, 1, 2, 1, 2])
    t = table(path, readonly=False, ack=False)
    data = t.getcol("DATA")
    data[:, 0, 0] = 500.0  # the first visibility of every row clips
    t.putcol("DATA", data)
    t.close()

    _run("--ms", path, "--scan", "2", "--flag", "clip 0 100, save:after",
         "--apply", "--clobber")

    version, version_row = flag_versions.load_version(path, "after")
    assert version.shape[0] == 8, "the version covers the whole MS"
    applied = _ms_flags(path)
    # --apply writes the selection back at its ROWID positions; the merge
    # places the snapshot's flags the same way, so the two agree exactly --
    # selected rows included, unselected rows kept as they were on disk.
    assert np.array_equal(version, applied)
    assert not version[::2].any(), "rows outside --scan keep their on-disk flags"
    assert version[1::2].any(), "the selected rows carry the snapshot's flags"
    assert not version_row.any()

    # The row-count invariant: a restore against the whole MS succeeds.
    fresh = DaskMS(path)
    fresh.restore_flag_version("after")
    assert np.array_equal(np.asarray(fresh.ds.FLAG.data), version)


def test_overlay_rows_are_validated_before_anything_is_written(ms):
    """A malformed overlay fails loudly, before an existing version moves."""
    flag_versions.save_version(
        ms, "existing", np.zeros((NROW, NCHAN, NCORR), bool),
        np.zeros(NROW, bool), comment="kept",
    )
    with pytest.raises(ValueError, match="strictly increasing"):
        flag_versions.save_version_streaming(
            ms, "existing", overlay=(
                np.array([0, 5, 5]), np.zeros((3, NCHAN, NCORR), bool),
                np.zeros(3, bool),
            )
        )
    assert [n for n, _ in flag_versions.list_versions(ms)] == ["existing"], (
        "the existing version must not be moved aside"
    )
    flag, _ = flag_versions.load_version(ms, "existing")
    assert flag.shape == (NROW, NCHAN, NCORR)
