# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for the image-size recommendation.

``skarabina-analyze`` measures the longest baseline from the UVW column.
Rows marked ``FLAG_ROW`` carry no usable data -- in the white-belt pipeline the
longest baselines are flagged by ``--flag-uv-above`` -- so they must not drive
the recommended image size.
"""
import numpy as np
import pytest
import xarray as xr
from click import ClickException

from skarabina.analyze import max_uv_distance


def _make_ds(uv_distances, flag_row=None, with_flag_row=True):
    nrow = len(uv_distances)
    uvw = np.stack(
        [np.asarray(uv_distances, dtype=float), np.zeros(nrow), np.zeros(nrow)],
        axis=1,
    )
    variables = {"UVW": (("row", "uvw"), uvw)}
    if with_flag_row:
        variables["FLAG_ROW"] = (
            ("row",),
            np.zeros(nrow, dtype=bool) if flag_row is None else np.asarray(flag_row),
        )
    return xr.Dataset(variables)


def test_max_uv_uses_all_rows_when_nothing_is_flagged():
    ds = _make_ds([100.0, 200.0, 300.0])
    assert max_uv_distance(ds) == pytest.approx(300.0)


def test_max_uv_ignores_flagged_rows():
    ds = _make_ds([100.0, 200.0, 300.0], flag_row=[False, False, True])
    assert max_uv_distance(ds) == pytest.approx(200.0)


def test_max_uv_without_flag_row_column():
    ds = _make_ds([100.0, 500.0], with_flag_row=False)
    assert max_uv_distance(ds) == pytest.approx(500.0)


def test_max_uv_raises_when_every_row_is_flagged():
    ds = _make_ds([100.0, 200.0], flag_row=[True, True])
    with pytest.raises(ClickException, match="Every row is flagged"):
        max_uv_distance(ds)
