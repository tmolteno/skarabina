# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Row-chunk sizing from a memory budget (:mod:`skarabina.memory`)."""
from types import SimpleNamespace

import pytest

from skarabina import memory

GB = 2**30


def test_the_chunk_fills_the_budget_and_no_more():
    rows, _ = memory.row_chunk_for(32 * GB, 8, 2048, 4)
    assert memory.planned_bytes(rows, 8, 2048, 4) <= memory.SAFETY * 32 * GB
    assert memory.planned_bytes(rows + 1, 8, 2048, 4) > memory.SAFETY * 32 * GB - 1


def test_the_chunk_scales_inversely_with_workers_and_visibilities_per_row():
    one, _ = memory.row_chunk_for(64 * GB, 4, 1024, 2)
    more_workers, _ = memory.row_chunk_for(64 * GB, 8, 1024, 2)
    wider, _ = memory.row_chunk_for(64 * GB, 4, 2048, 2)
    assert more_workers == pytest.approx(one / 2, abs=1)
    assert wider == pytest.approx(one / 2, abs=1)


def test_a_tiny_budget_gets_the_minimum_and_says_so():
    rows, reason = memory.row_chunk_for(1 * GB, 64, 4096, 4)
    assert rows == memory.MIN_ROW_CHUNK
    assert "--workers" in reason


def test_the_chunk_leaves_no_worker_idle():
    rows, reason = memory.row_chunk_for(512 * GB, 8, 64, 2, nrow=40_000)
    assert rows == 5000
    assert "capped" in reason


def test_available_memory_is_positive():
    assert memory.available_memory() > 0


def test_a_cgroup_limit_caps_available_memory(monkeypatch):
    monkeypatch.setattr(memory, "_cgroup_limit", lambda: 3 * GB)
    assert memory.available_memory() <= 3 * GB


def test_effective_workers():
    assert memory.effective_workers(3) == 3
    assert memory.effective_workers(0) >= 1


def _opts(path, **kw):
    base = dict(ms=path, row_chunk=None, memory_limit_gb=0.0, workers=2)
    base.update(kw)
    return SimpleNamespace(**base)


def test_cli_sizes_the_chunk_from_the_limit(tmp_path, capsys):
    from ms_fixture import make_synthetic_ms

    from skarabina.main import _row_chunk

    path = str(tmp_path / "m.ms")
    make_synthetic_ms(path, nchan=16, nrow=40, ncorr=2)
    assert _row_chunk(_opts(path, row_chunk=123, memory_limit_gb=4)) == 123
    rows = _row_chunk(_opts(path, memory_limit_gb=4))
    # 40 rows over 2 workers: the cap wins over a 4 GB budget.
    assert rows == memory.MIN_ROW_CHUNK
    assert "--memory-limit-GB" in capsys.readouterr().out


def test_ms_shape(tmp_path):
    from ms_fixture import make_synthetic_ms

    from skarabina.dask_ms import ms_shape

    path = str(tmp_path / "m.ms")
    make_synthetic_ms(path, nchan=16, nrow=40, ncorr=2)
    assert ms_shape(path) == (40, 16, 2)
