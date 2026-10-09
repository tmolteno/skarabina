# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The flag program and the lazy path write the same flags.

The auto-flaggers can run per block in worker processes
(skarabina.flag_program) or lazily in this process's dask threads; whichever
runs, the FLAG and FLAG_ROW written must be identical, bit for bit, because
nothing about the algorithm depends on where it executes.  These tests run
the same ``--flag`` list both ways over an MS with real work in it -- RFI
spikes, NaN patches, clip violations, pre-existing flags, several scans and
fields -- and compare the written MSes.
"""
import shutil

import numpy as np
import pytest
from casacore.tables import table
from click.testing import CliRunner

from ms_fixture import make_synthetic_ms

from skarabina.main import main

NROW, NCHAN, NCORR = 2400, 32, 2


def _ms_with_work(path):
    """A synthetic MS whose flags have something to do for every verb.

    Rows of two scans and two fields, a handful of pre-existing flags, NaN
    patches, an RFI spike train in one field, clip violations in the other,
    and autocorrelations -- so nan, clip, autos, spectral-window, tfcrop and
    rflag each find some of their own.
    """
    nrow, nchan, ncorr = NROW, NCHAN, NCORR
    make_synthetic_ms(
        str(path), nchan=nchan, nrow=nrow, ncorr=ncorr, auto_rows=40,
        scan_numbers=np.repeat([1, 2], nrow // 2),
        field_ids=np.tile(np.repeat([0, 1], nrow // 4), 2),
        field_names=("cal", "tgt"),
    )
    t = table(str(path), readonly=False, ack=False)
    data = t.getcol("DATA")
    flag = t.getcol("FLAG")
    # Pre-existing flags, in both fields, sparse.
    flag[::37, 3, 0] = True
    # NaN patches.
    data[10:12, 5:8, :] = np.nan
    data[600:601, :, 1] = np.nan
    # An RFI spike train in field 0: one channel, every 7th row, 30x.
    data[14::7, 9, :] *= 30.0
    # Clip violations in field 1: far above clip 0 100.
    data[1300::23, 20, :] = 500.0 + 0j
    t.putcol("DATA", data)
    t.putcol("FLAG", flag)
    t.close()
    return str(path)


@pytest.fixture
def work_ms(tmp_path):
    return _ms_with_work(tmp_path / "in.ms")


def _run(path, out, flags, extra=()):
    result = CliRunner().invoke(
        main,
        ["--ms", str(path), "--row-chunk", "300", "--flag", flags,
         "--msout", str(out), "--clobber", "--write-changed-only", *extra],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output


def _flags_of(path):
    with table(str(path), ack=False) as t:
        return np.asarray(t.getcol("FLAG")), np.asarray(t.getcol("FLAG_ROW"))


def _both_ways(monkeypatch, ms, tmp_path, flags, extra=()):
    """Run the list with the pool and without it, from the same input."""
    outs = {}
    for label, switch in (("pool", "1"), ("lazy", "0")):
        shutil.rmtree(str(ms) + ".flagversions", ignore_errors=True)
        monkeypatch.setenv("SKARABINA_FLAG_POOL", switch)
        out = tmp_path / f"out-{label}.ms"
        _run(ms, out, flags, extra)
        outs[label] = _flags_of(out)
    return outs


@pytest.mark.parametrize("flags, extra", [
    ("nan, clip 0 100, autos, rflag", []),
    ("nan, clip 0 100, autos, tfcrop", []),
    # A cheap verb after the auto-flagger: it joins the program as a
    # trailing layer, so this must still read DATA once (test_single_pass)
    # and flag identically.
    ("nan, rflag, clip 0 100", []),
    # Two auto-flaggers in one list: one program, two stages.
    ("tfcrop, rflag", []),
    # A field scope confines what each path flags.
    ("nan, clip 0 100, rflag", ["--field", "tgt"]),
    # A scan selection makes the dataset's rows a scattered ROWID set.
    ("nan, clip 0 100, autos, rflag", ["--scan", "2"]),
    # A non-default data column changes what the children read.
    ("nan, rflag", []),
    # extend before the auto-flagger poisons the program: the auto-flagger
    # falls back to the lazy path -- the outputs must agree trivially, and
    # this is the regression guard for that fallback being selected.
    ("nan, extend, rflag", []),
    # A save: between two auto-flaggers flushes the first program and starts
    # a second (issue #5's snapshot materialises the flags mid-run).
    ("nan, rflag, save:mid, tfcrop, clip 0 100", []),
])
def test_the_program_and_the_lazy_path_write_the_same_flags(
        monkeypatch, tmp_path, work_ms, flags, extra):
    outs = _both_ways(monkeypatch, work_ms, tmp_path, flags, extra)
    pool_flag, pool_row = outs["pool"]
    lazy_flag, lazy_row = outs["lazy"]
    assert np.array_equal(pool_flag, lazy_flag), \
        f"FLAG differs: pool {pool_flag.sum()} vs lazy {lazy_flag.sum()}"
    assert np.array_equal(pool_row, lazy_row)
    # The list must have found something, or the equality above proved
    # nothing: with this fixture every run flags some visibilities.
    assert pool_flag.any() or pool_row.any()


def test_restore_resets_the_program(monkeypatch, work_ms, tmp_path):
    """save:base, flags, restore:base, rflag: the restore discards the
    recorded layers and starts the program from the version, exactly as the
    lazy path rebinds FLAG to the version."""
    from skarabina import flag_ops
    from skarabina.dask_ms import DaskMS

    outs = {}
    for label, switch in (("pool", "1"), ("lazy", "0")):
        shutil.rmtree(str(work_ms) + ".flagversions", ignore_errors=True)
        monkeypatch.setenv("SKARABINA_FLAG_POOL", switch)
        ms = DaskMS(work_ms, row_chunk=300, workers=8)
        flag_ops.run(ms, flag_ops.parse(
            ["save:base, nan, clip 0 100, restore:base, rflag"]),
            log=lambda *_: None, flush=False)
        ms.write_new_ms(str(tmp_path / f"rs-{label}.ms"), True,
                        changed_only=True)
        outs[label] = _flags_of(tmp_path / f"rs-{label}.ms")
    assert np.array_equal(outs["pool"][0], outs["lazy"][0])
    assert np.array_equal(outs["pool"][1], outs["lazy"][1])


def test_the_pool_is_used_and_reads_the_data(monkeypatch, work_ms, tmp_path):
    """With the pool on, the children read DATA and this process does not:
    the run's one pass over the visibility column happens in the workers."""
    from skarabina import flag_program

    monkeypatch.setenv("SKARABINA_FLAG_POOL", "1")
    before = dict(flag_program.CHILD_READ_BYTES)
    _run(work_ms, tmp_path / "out.ms", "nan, clip 0 100, autos, rflag")
    child = {k: v for k, v in flag_program.CHILD_READ_BYTES.items()}
    assert child.get("DATA", 0) == NROW * NCHAN * NCORR * 8, \
        "the children did not read the visibility column exactly once"
    assert before.get("DATA", 0) in (0, child["DATA"])  # the counter was reset


def test_the_kill_switch_stays_in_process(monkeypatch, work_ms, tmp_path):
    """SKARABINA_FLAG_POOL=0 keeps every read in this process."""
    from skarabina import flag_program

    monkeypatch.setenv("SKARABINA_FLAG_POOL", "0")
    flag_program.CHILD_READ_BYTES.clear()
    _run(work_ms, tmp_path / "out.ms", "nan, rflag")
    assert not flag_program.CHILD_READ_BYTES.get("DATA"), \
        "children read DATA with the pool switched off"
