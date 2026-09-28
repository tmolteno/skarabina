# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The MS content summary in ``skarabina-analyze``.

``ms_content()`` reports what is in the measurement set -- rows, fields with
their names and row counts, scans, antennas, correlations, time range -- from
the small index columns and subtables only.  This is the summary the
``quartical-summary``/``listobs`` cabs used to provide; the pipeline now gets
it from ``skarabina-analyze`` instead.  It must never read the visibility
cubes (a large MS), and it must reach the JSON record unchanged.
"""
import json

import pytest
from click.testing import CliRunner

from skarabina import dask_ms  # noqa: F401  (daskms before casacore.tables)
from skarabina.analyze import JSON_STDOUT_PREFIX, main, ms_content

from ms_fixture import make_synthetic_ms  # noqa: E402


@pytest.fixture
def ms(tmp_path):
    return make_synthetic_ms(
        str(tmp_path / "content.ms"),
        nchan=6, nrow=8, ncorr=2,
        scan_numbers=[1, 1, 2, 2, 3, 3, 4, 4],
        field_ids=[0, 0, 0, 1, 1, 1, 2, 2],
        field_names=("CAL", "PCAL", "TGT"),
    )


def test_content_reports_shape_fields_scans_and_time(ms):
    content = ms_content(ms)

    assert content["n_rows"] == 8
    assert content["n_rows_flagged"] == 0
    assert content["n_corr"] == 2
    assert content["n_antennas"] == 2
    assert content["antenna_names"] == ["a0", "a1"]
    assert content["n_fields"] == 3
    assert [(f["field_id"], f["name"], f["n_rows"]) for f in content["fields"]] == [
        (0, "CAL", 3),
        (1, "PCAL", 3),
        (2, "TGT", 2),
    ]
    assert content["n_scans"] == 4
    assert content["scan_numbers"] == [1, 2, 3, 4]
    assert content["time_start_s"] == pytest.approx(0.0)
    assert content["time_end_s"] == pytest.approx(70.0)
    assert content["duration_s"] == pytest.approx(70.0)


def test_content_counts_flagged_rows(ms):
    """FLAG_ROW rows are counted, not hidden -- the row count of the raw
    MS is a headline of the summary."""
    from casacore.tables import table

    t = table(ms, readonly=False, ack=False)
    flag_row = t.getcol("FLAG_ROW").copy()
    flag_row[:3] = True
    t.putcol("FLAG_ROW", flag_row)
    t.close()

    content = ms_content(ms)
    assert content["n_rows"] == 8
    assert content["n_rows_flagged"] == 3


def test_content_handles_a_single_field_single_scan(tmp_path):
    path = make_synthetic_ms(str(tmp_path / "plain.ms"), nchan=3, nrow=4)
    content = ms_content(str(path))

    assert content["n_fields"] == 1
    assert content["fields"] == [{"field_id": 0, "name": "TEST", "n_rows": 4}]
    assert content["n_scans"] == 1
    assert content["scan_numbers"] == [0]


def test_content_reaches_the_json_record(ms, tmp_path):
    """The console block and the JSON record both carry the summary; the
    single-line --json-stdout form (the stimela wrangler interface) must
    contain it too."""
    out = str(tmp_path / "image-parameters.json")
    result = CliRunner().invoke(
        main,
        ["--ms", ms, "--image-fov", "1deg", "--output-json", out, "--json-stdout"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    assert "MS content: 8 rows" in result.output
    assert "'CAL': 3 rows" in result.output

    record = json.loads(open(out).read())
    assert record["ms_content"]["n_rows"] == 8
    assert record["ms_content"]["fields"][2] == {
        "field_id": 2, "name": "TGT", "n_rows": 2,
    }

    line = next(
        line for line in result.output.splitlines()
        if line.startswith(JSON_STDOUT_PREFIX)
    )
    parsed = json.loads(line[len(JSON_STDOUT_PREFIX):])
    assert parsed["ms_content"]["scan_numbers"] == [1, 2, 3, 4]
