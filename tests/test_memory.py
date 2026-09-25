# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The memory plan: row chunk and per-step estimates (:mod:`skarabina.memory`)."""
from types import SimpleNamespace

import pytest

from skarabina import memory

GB = 2**30
MEERKAT = dict(nrow=1_639_497, nchan=2511, ncorr=2)
STAGE0 = ["save", "autos", "uv-above", "nan", "clip", "spectral-window"]


def test_the_chunk_fills_the_budget_for_the_most_expensive_verb():
    result = memory.plan(STAGE0 + ["rflag"], 62 * GB, 12, **MEERKAT)
    assert result.limiting_step == "rflag"
    rows = result.row_chunk
    fits = memory.chunk_bytes("rflag", rows, 12, 2511, 2)
    assert fits <= memory.SAFETY * 62 * GB
    assert memory.chunk_bytes("rflag", rows + 1, 12, 2511, 2) > memory.SAFETY * 62 * GB - 1


def test_cheap_verbs_get_a_larger_chunk_than_the_flaggers():
    cheap = memory.plan(["nan", "clip"], 16 * GB, 12, **MEERKAT).row_chunk
    tfcrop = memory.plan(["nan", "tfcrop"], 16 * GB, 12, **MEERKAT).row_chunk
    rflag = memory.plan(["nan", "rflag"], 16 * GB, 12, **MEERKAT).row_chunk
    assert cheap > tfcrop > rflag


def test_the_chunk_scales_inversely_with_workers_and_visibilities_per_row():
    kw = dict(nrow=10**8)
    one = memory.plan(["tfcrop"], 64 * GB, 4, nchan=1024, ncorr=2, **kw).row_chunk
    more_workers = memory.plan(["tfcrop"], 64 * GB, 8, nchan=1024, ncorr=2, **kw).row_chunk
    wider = memory.plan(["tfcrop"], 64 * GB, 4, nchan=2048, ncorr=2, **kw).row_chunk
    assert more_workers == pytest.approx(one / 2, abs=1)
    assert wider == pytest.approx(one / 2, abs=1)


def test_the_chunk_leaves_no_worker_idle():
    result = memory.plan(["nan"], 512 * GB, 8, nrow=40_000, nchan=64, ncorr=2)
    assert result.row_chunk == 5000
    assert "workers" in result.limiting_step


def test_a_tiny_budget_gets_the_minimum_and_a_warning():
    result = memory.plan(["rflag"], 2 * GB, 64, nrow=10**7, nchan=4096, ncorr=4)
    assert result.row_chunk == memory.MIN_ROW_CHUNK
    assert any("--workers" in w for w in result.warnings)


def test_save_and_the_write_are_whole_table_steps():
    """Their estimate does not depend on the chunk, and is warned about."""
    small = memory.plan(STAGE0, 16 * GB, 12, write="write", **MEERKAT)
    large = memory.plan(STAGE0, 16 * GB, 12, write="write", row_chunk=50_000, **MEERKAT)
    whole = [line for line in small.lines if "whole table" in line]
    assert whole == [line for line in large.lines if "whole table" in line]
    assert any(w.startswith("save:") for w in small.warnings)      # ~20 GB > 16 GB
    assert any(w.startswith("write:") and "--write-changed-only" in w
               for w in small.warnings)


def test_writing_only_the_flags_or_an_averaged_ms_fits():
    flags_only = memory.plan(["nan"], 62 * GB, 12, write="write-flags", **MEERKAT)
    averaged = memory.plan(["nan"], 62 * GB, 12, write="write",
                           out_visibilities=1_639_497 * 2511 * 2 // 32, **MEERKAT)
    assert not flags_only.warnings and not averaged.warnings


def test_an_explicit_chunk_is_kept_and_checked():
    result = memory.plan(["rflag"], 8 * GB, 12, row_chunk=50_000, **MEERKAT)
    assert result.row_chunk == 50_000
    assert any("--row-chunk 50000" in w for w in result.warnings)


def test_available_memory_is_positive():
    assert memory.available_memory() > 0


def test_a_cgroup_limit_caps_available_memory(monkeypatch):
    monkeypatch.setattr(memory, "_cgroup_limit", lambda: 3 * GB)
    assert memory.available_memory() <= 3 * GB


def test_effective_workers():
    assert memory.effective_workers(3) == 3
    assert memory.effective_workers(0) >= 1


def _opts(path, **kw):
    base = dict(ms=path, row_chunk=None, memory_limit_gb=0.0, workers=2, msout=None,
                apply=False, write_changed_only=False,
                frequency_average_factor=None, time_average_factor=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_cli_plans_the_chunk_from_the_flag_list(tmp_path, capsys):
    from ms_fixture import make_synthetic_ms

    from skarabina import flag_ops
    from skarabina.main import _row_chunk, _write_mode

    path = str(tmp_path / "m.ms")
    make_synthetic_ms(path, nchan=16, nrow=40, ncorr=2)
    ops = flag_ops.parse(["nan, rflag"])
    assert _row_chunk(_opts(path, row_chunk=123, memory_limit_gb=4), ops) == 123
    # 40 rows over 2 workers: the idle-worker cap wins over a 4 GB budget.
    assert _row_chunk(_opts(path, memory_limit_gb=4), ops) == memory.MIN_ROW_CHUNK
    out = capsys.readouterr().out
    assert "Memory plan" in out and "rflag" in out and "--memory-limit-GB" in out

    assert _write_mode(_opts(path)) is None
    assert _write_mode(_opts(path, msout="o.ms")) == "write"
    assert _write_mode(_opts(path, msout="o.ms", write_changed_only=True)) == "write-flags"
    assert _write_mode(_opts(path, msout="o.ms", write_changed_only=True,
                             frequency_average_factor=4)) == "write"
    assert _write_mode(_opts(path, apply=True)) == "write-flags"


def test_ms_shape(tmp_path):
    from ms_fixture import make_synthetic_ms

    from skarabina.dask_ms import ms_shape

    path = str(tmp_path / "m.ms")
    make_synthetic_ms(path, nchan=16, nrow=40, ncorr=2)
    assert ms_shape(path) == (40, 16, 2)
