# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Regression tests for multi-field measurement sets.

dask-ms groups by ``(FIELD_ID, DATA_DESC_ID)`` by default, so a calibrator +
target MS comes back as one dataset *per field*.  Taking ``datasets[0]``
therefore reduced the MS to its first field: flagging, averaging and the
write-out silently dropped every other field, and ``skarabina-analyze``
measured the longest baseline from one field only.  These tests pin the
grouping: all fields of a spectral window must travel together, with FIELD_ID
kept as a per-row variable.
"""
import numpy as np
from casacore.tables import table

from skarabina.analyze import max_uv_distance
from skarabina.dask_ms import DaskMS
from skarabina.main import main as skarabina_main  # noqa: F401  (import check)
from ms_fixture import make_synthetic_ms


def _field_counts(ms_path):
    t = table(ms_path, ack=False)
    try:
        field_ids = np.asarray(t.getcol("FIELD_ID"))
        return {int(f): int((field_ids == f).sum()) for f in np.unique(field_ids)}
    finally:
        t.close()


def test_multifield_ms_is_a_single_dataset(tmp_path):
    """One DATA_DESC_ID with four fields must load as one dataset."""
    in_ms = make_synthetic_ms(
        tmp_path / "in.ms",
        nchan=4,
        nrow=8,
        field_ids=[0, 0, 1, 1, 1, 2, 3, 3],
        field_names=["bpcal", "target-a", "pcal", "target-b"],
    )

    ms = DaskMS(in_ms)

    assert len(ms.datasets) == 1, "fields must not be split into separate datasets"
    assert ms.ds.DATA.shape[0] == 8, "every row must be present"
    # FIELD_ID stays available per row, which is what --split filters on.
    assert "FIELD_ID" in ms.ds.data_vars
    assert list(np.asarray(ms.ds.FIELD_ID.data)) == [0, 0, 1, 1, 1, 2, 3, 3]


def test_flag_average_and_write_keep_every_field(tmp_path):
    """The whole point: all fields survive flagging, averaging and the write."""
    in_ms = make_synthetic_ms(
        tmp_path / "in.ms",
        nchan=4,
        nrow=8,
        field_ids=[0, 0, 1, 1, 1, 2, 3, 3],
        field_names=["bpcal", "target-a", "pcal", "target-b"],
    )

    ms = DaskMS(in_ms)
    ms.flag_data({"NAN": True})
    ms.frequency_average(2)
    out_ms = str(tmp_path / "out.ms")
    ms.write_new_ms(out_ms, clobber=True)

    assert _field_counts(out_ms) == {0: 2, 1: 3, 2: 1, 3: 2}
    main = table(out_ms, ack=False)
    try:
        assert main.getcol("DATA").shape[1] == 2  # averaging still applied
    finally:
        main.close()

    names = table(f"{out_ms}/FIELD", ack=False)
    try:
        assert list(names.getcol("NAME")) == ["bpcal", "target-a", "pcal", "target-b"]
    finally:
        names.close()


def test_analyze_sees_baselines_from_every_field(tmp_path):
    """The longest baseline may live in a field other than the first."""
    # Row 3 (field 1) carries the longest baseline; grouping by field would
    # hide it from datasets[0].
    in_ms = make_synthetic_ms(
        tmp_path / "in.ms",
        nchan=4,
        nrow=4,
        field_ids=[0, 0, 1, 1],
        field_names=["bpcal", "target"],
    )
    data = table(in_ms, readonly=False)
    data.putcol(
        "UVW",
        np.array([[0.0, 0, 0], [100.0, 0, 0], [200.0, 0, 0], [9000.0, 0, 0]]),
    )
    data.close()

    ms = DaskMS(in_ms)
    assert max_uv_distance(ms.ds) == 9000.0


def test_split_still_selects_a_single_field(tmp_path):
    """--split filters rows of the merged dataset by FIELD_ID."""
    in_ms = make_synthetic_ms(
        tmp_path / "in.ms",
        nchan=4,
        nrow=8,
        field_ids=[0, 0, 1, 1, 1, 2, 3, 3],
        field_names=["bpcal", "target-a", "pcal", "target-b"],
    )

    ms = DaskMS(in_ms)
    ms.select_scans("")
    out_ms = str(tmp_path / "target-a.ms")
    ms.write_new_ms(out_ms, clobber=True, split="target-a")

    assert _field_counts(out_ms) == {1: 3}
