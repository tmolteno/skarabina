# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""rflag/tfcrop flag the same whatever the chunking (issue #13).

Their thresholds are measured per fit block.  Once that block was the dask row
chunk, whose default length is rows/workers, so the same MS flagged
differently on hosts with different core counts.  These tests run the CLI --
the real chunking path -- with different ``--workers`` / ``--row-chunk`` and
require identical FLAG output.
"""
import shutil

import numpy as np
import pytest
from casacore.tables import table
from click.testing import CliRunner

from ms_fixture import make_synthetic_ms

from skarabina import dask_ms
from skarabina.main import main

NROW, NCHAN, NCORR = 3000, 32, 2


@pytest.fixture(autouse=True)
def _restore_fit_rows():
    saved = getattr(dask_ms, "FIT_ROWS", None)
    yield
    if saved is not None:
        dask_ms.FIT_ROWS = saved


@pytest.fixture
def noisy_ms(tmp_path):
    """Noise whose level drifts down the table, plus sparse RFI.

    The drift makes the threshold of one block differ from another's, so a
    different cut of the rows into blocks changes what is flagged.
    """
    path = str(tmp_path / "in.ms")
    make_synthetic_ms(path, nchan=NCHAN, nrow=NROW, ncorr=NCORR)
    rng = np.random.default_rng(13)
    level = np.linspace(1.0, 6.0, NROW)[:, None, None]
    data = level * (rng.normal(size=(NROW, NCHAN, NCORR))
                    + 1j * rng.normal(size=(NROW, NCHAN, NCORR)))
    data[rng.random(data.shape[:2]) < 0.01] *= 8.0
    t = table(path, readonly=False, ack=False)
    t.putcol("DATA", data.astype(np.complex64))
    t.putcol("FLAG", np.zeros((NROW, NCHAN, NCORR), bool))
    t.close()
    return path


def _flags(ms, tmp_path, label, flags, *extra):
    shutil.rmtree(ms + ".flagversions", ignore_errors=True)
    out = str(tmp_path / f"out-{label}.ms")
    result = CliRunner().invoke(
        main,
        ["--ms", ms, "--flag", flags, "--msout", out, "--clobber", *extra],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    with table(out, ack=False) as t:
        return np.asarray(t.getcol("FLAG"))


CHUNKINGS = [
    ("--workers", "1", "--row-chunk", "3000"),
    ("--workers", "4", "--row-chunk", "300"),
    ("--workers", "2", "--row-chunk", "777"),
]


@pytest.mark.parametrize("verb", ["rflag", "tfcrop"])
def test_default_flags_do_not_depend_on_workers_or_row_chunk(
        tmp_path, noisy_ms, verb):
    runs = [_flags(noisy_ms, tmp_path, str(i), verb, *c)
            for i, c in enumerate(CHUNKINGS)]
    assert runs[0].any()
    for other in runs[1:]:
        assert np.array_equal(runs[0], other)


@pytest.mark.parametrize("verb", ["rflag", "tfcrop"])
def test_several_fit_blocks_do_not_depend_on_chunking(tmp_path, noisy_ms, verb):
    """Fit blocks of 600 rows (five of them), chunks that are not multiples."""
    runs = [_flags(noisy_ms, tmp_path, str(i), verb, "--fit-rows", "600", *c)
            for i, c in enumerate(CHUNKINGS)]
    for other in runs[1:]:
        assert np.array_equal(runs[0], other)


def test_fit_rows_zero_is_the_old_per_chunk_behaviour(tmp_path, noisy_ms):
    whole = _flags(noisy_ms, tmp_path, "a", "rflag", "--fit-rows", "0",
                   "--row-chunk", "3000")
    small = _flags(noisy_ms, tmp_path, "b", "rflag", "--fit-rows", "0",
                   "--row-chunk", "300")
    assert not np.array_equal(whole, small)


def test_row_chunks_are_whole_fit_blocks():
    assert dask_ms.aligned_row_chunk(777, 600) == 600
    assert dask_ms.aligned_row_chunk(1300, 600) == 1200
    assert dask_ms.aligned_row_chunk(10, 600) == 600
    assert dask_ms.aligned_row_chunk(777, 0) == 777
