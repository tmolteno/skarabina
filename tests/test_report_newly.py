# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""``rflag``/``tfcrop``'s ``newly`` report count must never go negative (#7).

The printed line is
``flag_rflag: N of T visibilities flagged, X newly (P% of all)``, and ``X``
means *flags set by this verb that were not already set when it ran*
(``dask_ms._scope``: a verb would *newly* flag, never to flags that are
already set).  Issue #7 reports it going negative in a chained ``--apply``
run -- the two totals it was derived from disagreed -- and a negative
"newly" should never be printable.

Three tests here:

- an end-to-end run over a partially-flagged, multi-chunk MS through the
  CLI's single ``--apply`` pass (the shape of #7's run), checking both
  verbs' ``newly`` against ground truth from the FLAG column;
- one per report site (the eager spill path and the deferred path) driving
  the disagreement #7 hit -- a block whose output does not carry the flags
  that were already set -- from a partially-flagged input.  Before the fix
  these print a negative ``newly``; after it the count is the size of the
  set difference, i.e. the previously-unflagged visibilities the verb
  flagged.
"""
import re
import shutil

import numpy as np

REPORT = re.compile(
    r"^flag_rflag: (\d+) of (\d+) visibilities flagged, (-?\d+) newly"
    r" \((-?\d+\.\d+)% of all\)$",
    re.M,
)


def _newly(output):
    """``(total_flagged, universe, newly)`` of the flag_rflag line."""
    match = REPORT.search(output)
    assert match, "no flag_rflag report line in the output:\n" + output
    return tuple(int(match.group(i)) for i in (1, 2, 3))


def _dropping_block(data, existing, params, rows=None):
    """A flagger whose output does NOT carry the flags already set.

    No verb should ever do this -- but issue #7 is what the report looks
    like when the two totals it subtracts disagree, and the report must
    stay non-negative either way.  It flags the last channel's
    visibilities (whole-band, so the result does not depend on the row
    chunking), some of them previously unflagged, so ``newly`` is not
    trivially zero.
    """
    flags = np.zeros_like(np.asarray(existing), dtype=bool)
    flags[:, -1, :] = True
    return flags


# --- the guard, one test per report site ------------------------------------


def test_eager_report_newly_is_the_set_difference(capsys, monkeypatch):
    """The spill path's counts: ``newly`` is never ``total - already``."""
    from skarabina import dask_ms
    from skarabina.rflag import RFlagParams

    nrow, nchan, ncorr = 10, 5, 1
    existing = np.zeros((nrow, nchan, ncorr), dtype=bool)
    existing[:4] = True                     # partially flagged: 40 of 50

    from test_rflag import _synthetic_ms, _cube

    ms = _synthetic_ms(_cube(np.ones((nrow, nchan))), row_chunk=4)
    ms.ds["FLAG"] = (ms.ds.FLAG.dims, existing)

    # defer_reports is False, so flag_rflag takes the eager (_flags_to_spill)
    # site: dask_ms.py's first "newly" report.
    assert ms.defer_reports is False
    monkeypatch.setattr(dask_ms, "_rflag_block", _dropping_block)
    ms.flag_rflag(RFlagParams())

    total, universe, newly = _newly(capsys.readouterr().out)
    assert universe == nrow * nchan * ncorr
    # the flags that ARE set after the verb...
    assert total == np.count_nonzero(_dropping_block(None, existing, None))
    # ... and the count of previously-unflagged visibilities it flagged
    expected = np.count_nonzero(
        _dropping_block(None, existing, None) & ~existing
    )
    assert expected > 0, "the injected block flags nothing new"
    assert newly >= 0, "the report printed a negative newly"
    assert newly == expected


def test_deferred_report_newly_is_the_set_difference(capsys, monkeypatch):
    """The deferred site: same guard for a run that queues its reports."""
    from skarabina import dask_ms
    from skarabina.rflag import RFlagParams

    nrow, nchan, ncorr = 10, 5, 1
    existing = np.zeros((nrow, nchan, ncorr), dtype=bool)
    existing[:4] = True

    from test_rflag import _synthetic_ms, _cube

    ms = _synthetic_ms(_cube(np.ones((nrow, nchan))), row_chunk=4)
    ms.ds["FLAG"] = (ms.ds.FLAG.dims, existing)
    ms.defer_reports = True

    monkeypatch.setattr(dask_ms, "_rflag_block", _dropping_block)
    ms.flag_rflag(RFlagParams())
    ms.flush_reports()

    total, universe, newly = _newly(capsys.readouterr().out)
    assert universe == nrow * nchan * ncorr
    expected = np.count_nonzero(
        _dropping_block(None, existing, None) & ~existing
    )
    assert expected > 0
    assert newly >= 0, "the report printed a negative newly"
    assert newly == expected


# --- end to end, the shape of the run #7 was filed from ---------------------


def _make_ms(path, nrow=2000, nchan=32, ncorr=2):
    """A partially-flagged MS with RFI for rflag and a transient for tfcrop."""
    from casacore.tables import table

    from ms_fixture import make_synthetic_ms

    make_synthetic_ms(path, nrow=nrow, nchan=nchan, ncorr=ncorr)
    rng = np.random.default_rng(7)
    data = (
        rng.normal(size=(nrow, nchan, ncorr))
        + 1j * rng.normal(size=(nrow, nchan, ncorr))
    ).astype(np.complex64)
    data[700:740, 5, :] *= 200.0           # a burst rflag must find
    data[1500:1530, :, :] *= 60.0          # a transient tfcrop must find
    flag = np.zeros((nrow, nchan, ncorr), dtype=bool)
    flag[:600] = True                      # a large pre-flagged region
    flag[700:706, 5, :] = True             # ... including rows rflag re-flags
    with table(str(path), readonly=False, ack=False) as t:
        t.putcol("DATA", data)
        t.putcol("FLAG", flag)
    return flag


def _run(args):
    from skarabina.main import main

    main(args, standalone_mode=False)


def test_chained_apply_reports_newly_exactly(tmp_path, capsys):
    """``rflag, tfcrop --apply`` over a partially-flagged MS reports each
    verb's ``newly`` as the previously-unflagged rows it flagged."""
    from casacore.tables import table

    initial = _make_ms(tmp_path / "in.ms")
    solo = str(tmp_path / "solo.ms")
    chained = str(tmp_path / "chained.ms")
    shutil.copytree(str(tmp_path / "in.ms"), solo)
    shutil.copytree(str(tmp_path / "in.ms"), chained)

    # Ground truth for rflag: the same verb alone, same pinned row chunk.
    _run(["--ms", solo, "--row-chunk", "300", "--flag", "rflag",
          "--apply", "--clobber"])
    with table(solo, ack=False) as t:
        after_rflag = t.getcol("FLAG")

    _run(["--ms", chained, "--row-chunk", "300", "--flag", "rflag, tfcrop",
          "--apply", "--clobber"])
    output = capsys.readouterr().out
    with table(chained, ack=False) as t:
        final = t.getcol("FLAG")

    total, universe, newly = _newly(output)
    assert universe == initial.size
    assert total == after_rflag.sum()
    assert newly >= 0, "flag_rflag printed a negative newly"
    assert newly == np.count_nonzero(after_rflag & ~initial), (
        "flag_rflag's newly must count only the previously-unflagged"
        " visibilities it flagged"
    )

    match = re.search(
        r"^flag_tfcrop: (\d+) of (\d+) visibilities flagged, (-?\d+) newly",
        output, re.M,
    )
    assert match, "no flag_tfcrop report line:\n" + output
    tf_total, tf_universe, tf_newly = (int(match.group(i)) for i in (1, 2, 3))
    assert tf_universe == initial.size
    assert tf_total == final.sum()
    assert tf_newly >= 0, "flag_tfcrop printed a negative newly"
    # tfcrop's input state is rflag's output (the solo run's, same chunks)
    assert tf_newly == np.count_nonzero(final & ~after_rflag), (
        "flag_tfcrop's newly must count only the previously-unflagged"
        " visibilities it flagged"
    )
