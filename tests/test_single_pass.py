# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""A run reads each big column once, however many verbs its --flag list has.

Every chunk read of an MS array column goes through
``daskms.reads.ndarray_getcol``; the tests wrap it and count the bytes read
per column over a whole CLI run.
"""
import collections

import numpy as np
import pytest


@pytest.fixture
def column_reads(monkeypatch):
    import daskms.reads as reads

    counts = collections.Counter()
    original = reads.ndarray_getcol

    def counting(row_runs, table_future, column, result, dtype):
        out = original(row_runs, table_future, column, result, dtype)
        counts[column] += result.nbytes
        return out

    monkeypatch.setattr(reads, "ndarray_getcol", counting)
    return counts


def _ms(tmp_path, nrow=2000, nchan=32, ncorr=2):
    from ms_fixture import make_synthetic_ms

    path = str(tmp_path / "in.ms")
    make_synthetic_ms(path, nchan=nchan, nrow=nrow, ncorr=ncorr, auto_rows=50)
    from casacore.tables import table

    t = table(path, ack=False)
    itemsize = t.getcell("DATA", 0).dtype.itemsize
    t.close()
    return path, nrow * nchan * ncorr * itemsize    # one read of DATA


def _run(args):
    from skarabina.main import main

    main(args, standalone_mode=False)


@pytest.mark.parametrize("extra", [
    ["--write-changed-only"],
    ["--write-changed-only", "--summary"],
])
def test_nan_clip_autos_and_the_write_read_data_once(tmp_path, column_reads, extra):
    path, data_bytes = _ms(tmp_path)
    _run(["--ms", path, "--row-chunk", "300", "--flag", "nan, clip 0 100, autos",
          "--msout", str(tmp_path / "out.ms"), "--clobber"] + extra)
    assert column_reads["DATA"] == data_bytes, "DATA was read more than once"
    assert 0 < column_reads["FLAG"] <= data_bytes


def test_apply_reads_data_once(tmp_path, column_reads):
    path, data_bytes = _ms(tmp_path)
    _run(["--ms", path, "--row-chunk", "300", "--flag", "nan, clip 0 100",
          "--apply", "--clobber"])
    assert column_reads["DATA"] == data_bytes


def test_rflag_shares_its_pass_with_the_verbs_before_it(tmp_path, column_reads):
    path, data_bytes = _ms(tmp_path)
    _run(["--ms", path, "--row-chunk", "300", "--flag", "nan, clip 0 100, autos, rflag",
          "--msout", str(tmp_path / "out.ms"), "--clobber", "--write-changed-only"])
    assert column_reads["DATA"] == data_bytes


@pytest.mark.parametrize("flags, extra", [
    ("nan, clip 0 100, autos", []),
    ("nan, clip 0 100, autos", ["--summary"]),
    ("nan, clip 0 100, autos", ["--summary", "--time-average-factor", "2",
                                "--frequency-average-factor", "4"]),
    ("nan, clip 0 100, autos, rflag", ["--summary", "--frequency-average-factor", "4"]),
    ("nan, autos, tfcrop, uv-above 1000", ["--time-average-factor", "2"]),
])
def test_a_full_write_and_averaging_share_the_pass(tmp_path, column_reads, flags, extra):
    """The write reads DATA for its own column; the flags, rflag/tfcrop, the
    averaging and the summary are computed in that same pass."""
    path, data_bytes = _ms(tmp_path)
    _run(["--ms", path, "--row-chunk", "300", "--flag", flags,
          "--msout", str(tmp_path / "out.ms"), "--clobber"] + extra)
    assert column_reads["DATA"] == data_bytes, "DATA was read more than once"


def test_a_flag_list_with_nothing_after_it_reads_data_once(tmp_path, column_reads):
    path, data_bytes = _ms(tmp_path)
    _run(["--ms", path, "--row-chunk", "300", "--flag", "nan, rflag, clip 0 100"])
    assert column_reads["DATA"] == data_bytes


def test_the_single_pass_writes_the_same_flags(tmp_path):
    """Materialising the flags must not change them."""
    from casacore.tables import table

    from skarabina import flag_ops
    from skarabina.dask_ms import DaskMS

    path, _ = _ms(tmp_path)
    t = table(path, readonly=False, ack=False)
    data = t.getcol("DATA")
    data[::7, 3] = 500.0
    data[5, :, 0] = np.nan
    t.putcol("DATA", data)
    t.close()
    ops = flag_ops.parse(["nan, clip 0 100, autos, uv-above 1000"])

    lazy = DaskMS(path, row_chunk=300)
    flag_ops.run(lazy, ops, log=lambda *_: None)
    expected = np.asarray(lazy.ds.FLAG.data), np.asarray(lazy.ds.FLAG_ROW.data)

    once = DaskMS(path, row_chunk=300)
    flag_ops.run(once, ops, log=lambda *_: None, flush=False)
    once.materialise_flags()
    np.testing.assert_array_equal(np.asarray(once.ds.FLAG.data), expected[0])
    np.testing.assert_array_equal(np.asarray(once.ds.FLAG_ROW.data), expected[1])
    assert once.ds.FLAG.data.chunks == lazy.ds.FLAG.data.chunks
