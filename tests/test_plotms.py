# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""``skarabina-plotms``: the matplotlib stand-in for casaplotms.

The meerkat_imaging pipeline's stage-1 plots run ``casa.plotms``, whose
casaplotms is an x86_64-only AppImage -- so an arm64 host skips them.  These
tests pin the replacement's contract: plotms defaults (x = time,
y = amplitude, flagged data left out), the axes a recipe may ask for, and the
cab/CLI wiring that lets a recipe swap ``cab: casa.plotms`` for
``cab: skarabina-plotms`` with its params unchanged -- and that it plots
headless (Agg forced before pyplot, read-only HOME handled), since neither
the pipeline image nor an arm64 CI host has a display.
"""
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

import click
import numpy as np
import pytest
from click.testing import CliRunner
from omegaconf import OmegaConf

# skarabina.plotms imports dask-ms before casacore.tables (the casacore ->
# casacure alias must be installed first), so it must come before the fixture
# modules below, which import casacore directly.
from skarabina.plotms import collect, collect_and_render, main

from cal_fixture import PHASE_STEP_DEG, make_synthetic_caltable
from ms_fixture import make_synthetic_ms


@pytest.fixture
def cal(tmp_path):
    """An 8-row, 2-correlation, 4-channel gain table (nchan rows of phase)."""
    return make_synthetic_caltable(str(tmp_path / "multi.G0"))


@pytest.fixture
def ms(tmp_path):
    return make_synthetic_ms(str(tmp_path / "t.ms"), nchan=8, nrow=40, ncorr=2)


@pytest.fixture
def ms_multi(tmp_path):
    """Two fields, two scans -- for the --field/--scan selections."""
    return make_synthetic_ms(
        str(tmp_path / "multi.ms"), nchan=8, nrow=40, ncorr=2,
        field_ids=[0] * 20 + [1] * 20, field_names=("CAL", "TARGET"),
        scan_numbers=[1] * 20 + [2] * 20,
    )


def _cab():
    schema = OmegaConf.load(
        resources.files("skarabina_cargo").joinpath("skarabina.yml"))
    return schema.cabs["skarabina-plotms"]


def test_cli_writes_png(ms, tmp_path):
    """The pipeline's shape: ms + plotfile + overwrite, one PNG out."""
    out = tmp_path / "plot.png"
    result = CliRunner().invoke(main, ["--ms", ms, "--plotfile", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists() and out.stat().st_size > 1000
    assert "Wrote" in result.output
    assert "Amplitude vs Time" in result.output


def test_cli_refuses_to_overwrite_without_the_flag(ms, tmp_path):
    """plotms's overwrite= semantics: an existing plot file is an error."""
    out = tmp_path / "plot.png"
    first = CliRunner().invoke(main, ["--ms", ms, "--plotfile", str(out)])
    assert first.exit_code == 0, first.output
    second = CliRunner().invoke(main, ["--ms", ms, "--plotfile", str(out)])
    assert second.exit_code != 0
    assert "overwrite" in second.output
    third = CliRunner().invoke(
        main, ["--ms", ms, "--plotfile", str(out), "--overwrite"])
    assert third.exit_code == 0, third.output


def test_cli_writes_pdf_for_caltable(cal, tmp_path):
    """The stage-1 gain-table plot: a caltable to a PDF, as the pipeline asks."""
    out = tmp_path / "cal_G0.pdf"
    result = CliRunner().invoke(
        main, ["--ms", cal, "--plotfile", str(out), "--overwrite"])
    assert result.exit_code == 0, result.output
    assert out.read_bytes()[:4] == b"%PDF"


def test_cli_missing_input_is_a_clean_error(tmp_path):
    result = CliRunner().invoke(
        main, ["--ms", str(tmp_path / "nope.ms"),
               "--plotfile", str(tmp_path / "out.png")])
    assert result.exit_code != 0
    assert "no such" in result.output


def test_default_axes_are_plotms_defaults(cal):
    """Blank xaxis/yaxis means plotms' own default: time vs amplitude."""
    plot = collect(cal)
    assert plot.xlabel == "Time"
    assert plot.ylabel == "Amplitude"
    assert plot.x_is_time
    assert plot.n_points == 8 * 2 * 4  # rows x corr x chan
    assert plot.n_flagged == 0
    # Both correlations plotted, named from POLARIZATION.CORR_TYPE (9, 12).
    assert [label for label, _, _ in plot.series] == ["XX", "YY"]


def test_phase_and_amplitude_values_are_the_table_values(cal):
    """yaxis=phase over chan, yaxis=amp over row: what is in CPARAM."""
    chans = np.arange(4)
    phase = collect(cal, xaxis="chan", yaxis="phase")
    for corr, (label, x, y) in enumerate(phase.series):
        np.testing.assert_array_equal(x, np.tile(chans, 8))
        expected = PHASE_STEP_DEG * (corr + chans)
        np.testing.assert_allclose(y, np.tile(expected, 8))

    rows = np.arange(8)
    amp = collect(cal, xaxis="row", yaxis="amp")
    for _, x, y in amp.series:
        # (row, chan) cells flatten row-major: the row index runs slowest.
        np.testing.assert_array_equal(x, np.repeat(rows, 4))
        np.testing.assert_allclose(y, np.repeat(1.0 + rows, 4))


def test_flagged_data_is_left_out_by_default(tmp_path):
    """plotms does not display flagged points; --show-flagged does."""
    path = make_synthetic_caltable(str(tmp_path / "multi.B0"), flagged=2)
    plot = collect(path)
    assert plot.n_flagged == 2 * 2 * 4
    assert plot.n_points == (8 - 2) * 2 * 4
    shown = collect(path, show_flagged=True)
    assert shown.n_points == 8 * 2 * 4
    assert shown.n_flagged == 0


def test_plot_with_everything_flagged_still_writes_a_file(tmp_path):
    path = make_synthetic_caltable(str(tmp_path / "multi.K0"), flagged=8)
    out = tmp_path / "empty.png"
    plot = collect_and_render(path, str(out))
    assert plot.n_points == 0
    assert out.exists() and out.stat().st_size > 0


def test_correlation_selection(cal):
    """--corr takes names or 0-based indices, and bounds the series."""
    for spec in ("YY", "1"):
        plot = collect(cal, corr=spec, xaxis="chan", yaxis="phase")
        assert len(plot.series) == 1
        _, x, y = plot.series[0]
        expected = PHASE_STEP_DEG * (1 + np.arange(4))
        np.testing.assert_allclose(y, np.tile(expected, 8))
        np.testing.assert_array_equal(x, np.tile(np.arange(4), 8))
    with pytest.raises(click.ClickException, match="unknown correlation"):
        collect(cal, corr="ZZ")
    with pytest.raises(click.ClickException, match="out of range"):
        collect(cal, corr="7")


def test_unknown_axis_is_an_error_listing_the_choices(cal, ms):
    for path in (cal, ms):
        with pytest.raises(click.ClickException, match="choices"):
            collect(path, xaxis="wibble")
    # uv axes exist for an MS but not for a caltable.
    assert collect(ms, xaxis="uvdist").xlabel == "UV distance (m)"
    with pytest.raises(click.ClickException, match="choices"):
        collect(cal, xaxis="uvdist")


def test_scan_and_field_selections(ms_multi):
    rows = 20 * 8 * 2
    assert collect(ms_multi, scan="2").n_points == rows
    assert collect(ms_multi, field="TARGET").n_points == rows
    assert collect(ms_multi, field="1").n_points == rows
    with pytest.raises(click.ClickException, match="unknown field name"):
        collect(ms_multi, field="NOPE")
    # A caltable has no scans to select.
    cal = make_synthetic_caltable(str(ms_multi).replace("multi.ms", "multi.G0"))
    with pytest.raises(click.ClickException, match="no scans"):
        collect(cal, scan="1")


def test_decimation_keeps_the_point_budget(ms):
    """--max-points bounds what is read and plotted (0 disables it)."""
    assert collect(ms, max_points=100).n_points == 96 <= 100
    assert collect(ms, max_points=0).n_points == 40 * 8 * 2


def test_ms_data_column_selection(ms):
    """A column that exists but holds no data fails with a clean error."""
    with pytest.raises(click.ClickException, match="CORRECTED_DATA"):
        collect(ms, data_column="CORRECTED")
    with pytest.raises(click.ClickException, match="unknown data column"):
        collect(ms, data_column="SCRATCH")


def test_ms_uv_axes_come_from_uvw(ms):
    uvdist = collect(ms, xaxis="uvdist", yaxis="phase")
    # The fixture's UVW is (row * 500, 0, 0), so uvdist steps by 500 m.
    u = np.arange(40) * 500.0
    # (row, chan) cells flatten row-major: uvdist repeats across channels.
    np.testing.assert_allclose(uvdist.series[0][1], np.repeat(u, 8))
    # DATA is all ones, so every phase is 0.
    np.testing.assert_allclose(uvdist.series[0][2], 0.0)


def test_cab_and_cli_are_one_interface():
    """Every cab input must be a CLI option, and the bools must be flags.

    stimela passes a bool as ``--option`` when true and omits it when false
    (see stimela/kitchen/cab.py), which only works for a click flag.
    """
    cab = _cab()
    options = {p.name: p for p in main.params if isinstance(p, click.Option)}
    for name in cab.inputs:
        assert name.replace("-", "_") in options, (
            f"cab input '{name}' has no CLI option"
        )
    for name, schema in cab.inputs.items():
        if str(schema.get("dtype")) == "bool":
            assert options[name.replace("-", "_")].is_flag, (
                f"bool input '{name}' must map to a click flag"
            )
    # The pipeline's three params, required as the CLI requires them.
    assert cab.inputs["ms"].get("required") is True
    assert cab.inputs["plotfile"].get("required") is True
    for name in ("ms", "plotfile"):
        assert options[name].required


def test_cab_runs_in_the_skarabina_image():
    """The cab must reuse the published skarabina image -- nothing new to
    build for arm64."""
    schema = OmegaConf.load(
        resources.files("skarabina_cargo").joinpath("skarabina.yml"))
    cab = schema.cabs["skarabina-plotms"]
    assert cab.command == "skarabina-plotms"
    assert OmegaConf.to_container(cab.image) == \
        OmegaConf.to_container(schema.cabs["skarabina"].image)


# ---------------------------------------------------------------------------
# Headless operation: Agg no matter what, and no writable HOME needed.
# ---------------------------------------------------------------------------


def test_plotms_selects_agg_at_import():
    """``skarabina.plotms`` must select the Agg backend at import, before
    pyplot is imported anywhere.

    On a headless host where tkinter is installed (as here), matplotlib's
    own default would be TkAgg, and the first figure would die trying to
    connect to a display that does not exist -- the casaplotms cab needed
    ``xvfb-run`` for exactly this reason.
    """
    import matplotlib

    assert matplotlib.get_backend().lower() == "agg"


def _hostile_headless_env(tmp_path):
    """Environment for a subprocess that must plot with no display.

    ``DISPLAY``/``WAYLAND_DISPLAY`` are stripped, ``MPLBACKEND`` demands an
    interactive backend, HOME is a read-only directory, and the
    ``MPLCONFIGDIR``/``XDG_CONFIG_HOME`` hints are removed -- so both the
    backend choice and matplotlib's cache directory have to come from
    ``skarabina.plotms`` itself.
    """
    ro_home = tmp_path / "ro-home"
    ro_home.mkdir(exist_ok=True)
    ro_home.chmod(0o555)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DISPLAY", "WAYLAND_DISPLAY", "MPLBACKEND",
                       "MPLCONFIGDIR", "XDG_CONFIG_HOME")
    }
    env["HOME"] = str(ro_home)
    env["MPLBACKEND"] = "TkAgg"  # would need a display; plotms must win
    repo = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    return env, ro_home


def _run_headless(env, code, args, timeout=120):
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def test_cli_plots_headless_in_a_hostile_environment(ms, tmp_path):
    """The CLI's own import order (the console script's), end to end:
    Agg wins over ``MPLBACKEND``, matplotlib's cache dir is sorted out
    *before* matplotlib imports (no fallback warning), and the plot is
    written -- with no display and a read-only HOME."""
    env, ro_home = _hostile_headless_env(tmp_path)
    plotfile = tmp_path / "headless.png"
    code = (
        "import os, sys\n"
        "from skarabina.plotms import main\n"
        "import matplotlib\n"
        "print('BACKEND=' + matplotlib.get_backend().lower(), file=sys.stderr)\n"
        "print('MPLCONFIGDIR_SET=' + str(bool(os.environ.get('MPLCONFIGDIR'))),"
        " file=sys.stderr)\n"
        "main(sys.argv[1:])\n"
    )
    try:
        proc = _run_headless(
            env, code,
            ["--ms", ms, "--plotfile", str(plotfile), "--overwrite"],
        )
    finally:
        ro_home.chmod(0o755)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BACKEND=agg" in proc.stderr, proc.stderr
    assert "MPLCONFIGDIR_SET=True" in proc.stderr, proc.stderr
    # The guard runs before matplotlib is imported, so matplotlib never has
    # to fall back to a fresh temp cache with a warning per run.
    assert "temporary cache directory" not in proc.stderr, proc.stderr
    assert "Wrote" in proc.stdout, proc.stdout
    assert plotfile.exists() and plotfile.stat().st_size > 0


def test_cli_plots_headless_when_pyplot_was_imported_first(ms, tmp_path):
    """A host process that imported matplotlib/pyplot before
    ``skarabina.plotms`` (a caller's script, an interactive session) still
    ends up on Agg: ``matplotlib.use`` switches the configured backend, and
    pyplot defers loading it until the first figure."""
    env, ro_home = _hostile_headless_env(tmp_path)
    plotfile = tmp_path / "preimport.png"
    code = (
        "import sys\n"
        "import matplotlib\n"
        "import matplotlib.pyplot as plt\n"
        "import skarabina.plotms as m\n"
        "print('BACKEND=' + matplotlib.get_backend().lower(), file=sys.stderr)\n"
        "m.main(sys.argv[1:])\n"
    )
    try:
        proc = _run_headless(
            env, code,
            ["--ms", ms, "--plotfile", str(plotfile), "--overwrite"],
        )
    finally:
        ro_home.chmod(0o755)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BACKEND=agg" in proc.stderr, proc.stderr
    assert "Wrote" in proc.stdout, proc.stdout
    assert plotfile.exists() and plotfile.stat().st_size > 0
