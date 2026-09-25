"""The CLI applies --frequency-average-factor, --time-average-factor and
--optimize to what it writes.

The DaskMS methods have their own tests, but 760e0ee dropped the calls from
``main()`` when it reworked the flagging pass.  1.0.8 then accepted the
options and silently wrote full-resolution, unoptimised output.  These tests
run the options through the command line and inspect the written MS.
"""
import numpy as np
import pytest
from click.testing import CliRunner

from skarabina import dask_ms  # noqa: F401  (daskms before casacore.tables)
from casacore.tables import table  # noqa: E402

from ms_fixture import make_synthetic_ms  # noqa: E402
from skarabina.main import main  # noqa: E402


def _run(*args):
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result


def _shape(path):
    t = table(path, ack=False)
    try:
        return t.nrows(), t.getcol("DATA").shape[1]
    finally:
        t.close()


@pytest.fixture
def ms(tmp_path):
    return make_synthetic_ms(str(tmp_path / "in.ms"), nchan=8, nrow=8)


def test_frequency_average_factor_reaches_the_output(ms, tmp_path):
    out = str(tmp_path / "out.ms")
    _run("--ms", ms, "--frequency-average-factor", "2", "--msout", out)
    assert _shape(out) == (8, 4)


def test_time_average_factor_reaches_the_output(ms, tmp_path):
    out = str(tmp_path / "out.ms")
    _run("--ms", ms, "--time-average-factor", "2", "--msout", out)
    nrow, nchan = _shape(out)
    assert nchan == 8
    assert nrow < 8


def test_optimize_reaches_the_output(ms, tmp_path):
    # Flag every visibility of the first two rows: --optimize drops them.
    t = table(ms, readonly=False, ack=False)
    flag = t.getcol("FLAG")
    flag[:2] = True
    t.putcol("FLAG", flag)
    t.close()
    out = str(tmp_path / "out.ms")
    _run("--ms", ms, "--optimize", "--msout", out)
    nrow, _ = _shape(out)
    assert nrow == 6
    t = table(out, ack=False)
    try:
        assert not np.asarray(t.getcol("FLAG")).all(axis=(1, 2)).any()
    finally:
        t.close()
