# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Integration grouping: tolerate MS writers that split one integration.

Some writers stamp a single integration's rows with more than one TIME value.
Grouping rows by "TIME changed" then reports incomplete integrations even
though the parts together hold every baseline.  These tests pin the
gap-tolerant grouping and the summary report built on it.
"""
import numpy as np
import pytest
from casacore.tables import table

from skarabina.dask_ms import (
    INTEGRATION_TOLERANCE,
    DaskMS,
    group_integrations,
    integration_interval,
)
from ms_fixture import make_synthetic_ms

CADENCE = 8.0
NROW_PER_INTEGRATION = 36
NANT = 8


def _make_ms(path, n_integrations=3, split_first=True):
    """Build an MS whose first integration is stamped with two TIME values.

    The row count per integration is ``NROW_PER_INTEGRATION``; when
    ``split_first`` is set, the first integration has a single row moved
    1 s ahead of its group -- the artefact seen on real data (offsets of
    exactly 1.000 s against an 8 s cadence).
    """
    nrow = n_integrations * NROW_PER_INTEGRATION
    make_synthetic_ms(path, nchan=1, nrow=nrow)
    time = np.repeat(np.arange(n_integrations, dtype=float) * CADENCE, NROW_PER_INTEGRATION)
    if split_first:
        time[NROW_PER_INTEGRATION - 1] += 1.0
    t = table(path, readonly=False)
    t.putcol("TIME", time)
    t.close()
    return path


def _count_naive_groups(time):
    """The naive grouping: one group per distinct TIME value."""
    return int(np.count_nonzero(np.diff(time) != 0) + 1)


def test_group_integrations_unites_split_rows(tmp_path):
    ms = _make_ms(str(tmp_path / "split.ms"))
    ds = DaskMS(ms)
    time = ds.ds["TIME"].data.compute()

    # the artefact is really there: naive grouping sees an extra group
    assert _count_naive_groups(time) == 4

    groups = group_integrations(time)
    assert len(groups) == 3, "split integration should count once"
    assert [end - start for start, end in groups] == [NROW_PER_INTEGRATION] * 3
    # groups tile the rows exactly, in order
    assert groups[0][0] == 0
    assert groups[-1][1] == len(time)
    for (_, end), (start, _) in zip(groups, groups[1:]):
        assert end == start


def test_group_integrations_keeps_genuine_integrations_separate(tmp_path):
    """Rows one full cadence apart are different integrations."""
    ms = _make_ms(str(tmp_path / "clean.ms"), split_first=False)
    time = DaskMS(ms).ds["TIME"].data.compute()
    groups = group_integrations(time)
    assert len(groups) == 3


def test_group_integrations_handles_split_part_before_its_group(tmp_path):
    """The stray rows may lead the rest of the integration, not trail it.

    On the real MT0 file the short part comes second, but an early part must
    work too: the first group then starts at the offset TIME.
    """
    make_synthetic_ms(str(tmp_path / "lead.ms"), nchan=1, nrow=NROW_PER_INTEGRATION * 2)
    time = np.concatenate(
        [
            np.full(4, CADENCE + 1.0),                 # stray leading part
            np.zeros(NROW_PER_INTEGRATION - 4),        # rest of integration 0
            np.full(NROW_PER_INTEGRATION, 2 * CADENCE),  # integration 1
        ]
    )
    t = table(str(tmp_path / "lead.ms"), readonly=False)
    t.putcol("TIME", time)
    t.close()

    groups = group_integrations(time)
    assert len(groups) == 2
    assert [end - start for start, end in groups] == [NROW_PER_INTEGRATION] * 2


def test_group_integrations_edge_cases():
    assert group_integrations(np.array([])) == []
    assert group_integrations(np.zeros(5)) == [(0, 5)]
    # a single TIME value gives no cadence, so nothing is merged
    assert group_integrations(np.array([0.0, 8.0, 16.0])) == [(0, 1), (1, 2), (2, 3)]


@pytest.mark.parametrize("tol_fraction", [0.0, INTEGRATION_TOLERANCE, 1.0])
def test_group_integrations_tolerance_is_a_fraction_of_cadence(tmp_path, tol_fraction):
    ms = _make_ms(str(tmp_path / f"tol{tol_fraction}.ms"))
    time = DaskMS(ms).ds["TIME"].data.compute()
    groups = group_integrations(time, tol_fraction=tol_fraction)
    if tol_fraction == 0.0:
        assert len(groups) == 4, "zero tolerance reproduces the naive grouping"
    else:
        # 1.0 s offset < 0.5 * 8 s and < 1.0 * 8 s cadence
        assert len(groups) == 3


def test_integration_interval_prefers_the_nominal_value():
    """A shortened first row must not set the reported integration time."""
    interval = np.array([5.9974424] + [7.99662701] * 9)
    assert integration_interval(interval) == pytest.approx(7.99662701)
    # falls back to the median TIME spacing when there is no interval column
    time = np.array([0.0, 0.0, 8.0, 8.0, 16.0, 16.0])
    assert integration_interval(None, time) == pytest.approx(CADENCE)
    assert integration_interval(None) is None


def test_summary_counts_split_integration_once(tmp_path, capsys):
    ms = _make_ms(str(tmp_path / "summary.ms"))
    DaskMS(ms).summary()
    out = capsys.readouterr().out
    assert "Integrations: 3" in out
    # the parts add up to complete integrations, so nothing to warn about
    assert "Incomplete integration groups" not in out
    # and the nominal (not shortened) interval is reported
    assert "Current integration time: 1.0 s" not in out


def test_summary_warns_about_genuinely_short_groups(tmp_path, capsys):
    """A group that really has lost baselines is called out."""
    nrow = NROW_PER_INTEGRATION * 2
    make_synthetic_ms(str(tmp_path / "short.ms"), nchan=1, nrow=nrow)
    time = np.concatenate([np.full(nrow - 1, 0.0), np.full(1, CADENCE)])
    t = table(str(tmp_path / "short.ms"), readonly=False)
    t.putcol("TIME", time)
    t.close()

    DaskMS(str(tmp_path / "short.ms")).summary()
    out = capsys.readouterr().out
    assert "Integrations: 2" in out
    assert "Incomplete integration groups: 1/2" in out


def test_summary_does_not_warn_for_a_reduced_subset(tmp_path, capsys):
    """Selecting a field shrinks every group; that is not an incomplete MS."""
    nrow = 12
    path = str(tmp_path / "subset.ms")
    # field 0 spans two integrations of 4 rows; field 1 is one integration
    make_synthetic_ms(
        path, nchan=1, nrow=nrow,
        field_ids=[0] * 8 + [1] * 4,
    )
    t = table(path, readonly=False)
    t.putcol("TIME", np.array([0.0] * 4 + [8.0] * 4 + [0.0] * 4))
    t.putcol("ANTENNA1", np.array([0, 0, 1, 2] * 3, dtype=np.int32))
    t.putcol("ANTENNA2", np.array([0, 1, 2, 2] * 3, dtype=np.int32))
    t.close()

    ds = DaskMS(path)
    ds.ds = ds._select_field(ds.ds, 0)
    ds._refresh_cached_columns()
    ds._report_integrations()
    out = capsys.readouterr().out
    assert "Integrations: 2" in out
    assert "Incomplete integration groups" not in out, (
        "a deliberately reduced MS should not be reported as incomplete"
    )
