# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for flagging a calibration table (skarabina.caltable).

The pipeline's stage 1 flags the bandpass solutions (``multi.B0``/``B1``)
that ``casa.bandpass`` writes, the last ``flagdata`` calls it still makes.
These test the CalTable path end to end through the CLI: the same
``--flag`` list the MS path takes, on a synthetic caltable with RFI-shaped
solutions at known positions.

The RFI shapes respect the flagging rule's occupancy bound: a plain-std
cutoff at 3 sigmas can only flag outliers that occupy under 1/9 of their
lane (the outlier inflates the very std it is judged against), so the
spikes here are narrow -- a channel or two of 16, at a solution time of
ten.
"""
import traceback

import numpy as np
import pytest
from casacore.tables import table
from click.testing import CliRunner

from cal_fixture import make_synthetic_caltable
from ms_fixture import make_synthetic_ms

from skarabina.caltable import CalTable, table_kind
from skarabina.main import main

NROW, NCHAN, NCORR, NANT = 80, 16, 2, 8


@pytest.fixture
def cal(tmp_path):
    """A bandpass-shaped caltable: (chan, corr) cells, as CASA writes.

    8 antennas x 10 solution times, 16 channels; one channel of three
    solution times of three antennas carries an 8x RFI spike.
    """
    path = make_synthetic_caltable(
        str(tmp_path / "multi.B0"), nrow=NROW, nchan=NCHAN, ncorr=NCORR,
        nant=NANT, chan_first=True,
    )
    with table(path, readonly=False, ack=False) as tab:
        cparam = tab.getcol("CPARAM")
        a1 = np.asarray(tab.getcol("ANTENNA1"))
        rfi = np.zeros(cparam.shape, dtype=bool)
        for antenna in (1, 4, 7):
            rows = np.flatnonzero(a1 == antenna)
            rfi[rows[3], 6, :] = True
            rfi[rows[6], 6, :] = True
        cparam[rfi] = (np.abs(cparam[rfi]) * 8.0).astype(np.complex128)
        tab.putcol("CPARAM", cparam)
    return path


def _read_flag(path, nchan=NCHAN, ncorr=NCORR):
    """The FLAG column, normalised to (row, chan, corr): the cells are
    (chan, corr) for bandpass-shaped tables and (corr, chan) otherwise."""
    with table(path, ack=False) as tab:
        flag = np.asarray(tab.getcol("FLAG"), dtype=bool)
    if nchan != ncorr and flag.shape[1] == ncorr and flag.shape[2] == nchan:
        flag = np.swapaxes(flag, 1, 2)
    return flag


def _invoke(*args):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def _expect_error(result, match):
    """A RuntimeError raised inside the command: CliRunner stores it on
    the result rather than in the output."""
    text = result.output
    if result.exception is not None and \
            not isinstance(result.exception, SystemExit):
        text += "".join(traceback.format_exception(result.exception))
    assert result.exit_code != 0, text
    assert match in text


def test_table_kind_distinguishes_cal_and_ms(tmp_path):
    cal = make_synthetic_caltable(str(tmp_path / "t.bim"))
    ms = make_synthetic_ms(str(tmp_path / "t.ms"))
    assert table_kind(cal) == "cal"
    assert table_kind(ms) == "ms"
    junk = tmp_path / "junk"
    junk.mkdir()
    # The message for a non-table directory comes from the backend
    # (python-casacore: "does not exist"; casacure >= 3.8.18:
    # "No such file or directory (os error 2)").
    with pytest.raises(RuntimeError,
                       match="neither a measurement set|does not exist"
                             "|No such file or directory"):
        table_kind(str(junk))


def test_tfcrop_finds_the_rfi_solutions_and_writes_flags(cal):
    assert _read_flag(cal).mean() == 0.0
    result = _invoke("--ms", cal, "--flag", "tfcrop", "--apply", "--clobber")
    assert result.exit_code == 0, result.output
    after = _read_flag(cal)
    with table(cal, ack=False) as tab:
        a1 = np.asarray(tab.getcol("ANTENNA1"))
    for antenna in (1, 4, 7):
        rows = np.flatnonzero(a1 == antenna)
        for row in (rows[3], rows[6]):
            assert after[row, 6, :].all(), \
                f"antenna {antenna}'s spike survived"
    # The clean solutions are untouched: a flagger that flags everything
    # scores 100% recall and is useless.
    assert after.mean() < 0.05
    # "flag_tfcrop:" report, MS-path shape.
    assert "flag_tfcrop:" in result.output
    assert "newly" in result.output


def test_both_cell_orientations_read_the_same(tmp_path):
    """(corr, chan) cells (gain tables, the fixture default) and (chan,
    corr) cells (the bandpass writer) flag identically."""
    # Identical values for both orientations (so the flags must match):
    # one smooth band shared by every row, small noise, and an 8x spike on
    # channel 6 of every 3rd row.
    rng = np.random.default_rng(1)
    band = 10.0 * (1.0 + 0.1 * np.cos(np.linspace(0.0, 3.0, NCHAN)))
    values = (band[None, :, None]
              * (1.0 + 0.01 * rng.standard_normal((NROW, NCHAN, NCORR)))
              * np.exp(1j * rng.uniform(0.0, 2.0 * np.pi,
                                        (NROW, NCHAN, NCORR))))
    # One spiked solution time per antenna (rows cycle mod NANT), sparse
    # enough that the per-antenna template cannot absorb it; x20, so the
    # margin against the plain-std threshold is not a close call.
    spike_rows = np.array([16, 33, 10, 27, 44, 61, 78, 71])
    values[spike_rows, 6, :] *= 20.0
    flags = []
    for chan_first, name in ((False, "a.G0"), (True, "b.bim")):
        path = make_synthetic_caltable(
            str(tmp_path / name), nrow=NROW, nchan=NCHAN, ncorr=NCORR,
            nant=NANT, chan_first=chan_first)
        with table(path, readonly=False, ack=False) as tab:
            cparam = values if chan_first \
                else np.swapaxes(values, 1, 2)
            tab.putcol("CPARAM", cparam)
        result = _invoke("--ms", path, "--flag", "tfcrop", "--apply",
                         "--clobber")
        assert result.exit_code == 0, result.output
        flags.append(_read_flag(path))
        assert flags[-1][spike_rows, 6, :].all(), \
            f"{name}: spike not flagged"
    assert np.array_equal(flags[0], flags[1])


def test_preexisting_flags_are_kept(cal):
    with table(cal, readonly=False, ack=False) as tab:
        flag = np.zeros(tab.getcol("FLAG").shape, dtype=bool)
        flag[0] = True
        tab.putcol("FLAG", flag)
    _invoke("--ms", cal, "--flag", "tfcrop", "--apply", "--clobber")
    after = _read_flag(cal)
    assert after[0].all()
    assert after.mean() > 1.0 / after.size


def test_rflag_and_clip_run_on_cparam(cal):
    result = _invoke("--ms", cal, "--flag", "rflag",
                     "--flag", "clip 0 1000", "--apply", "--clobber")
    assert result.exit_code == 0, result.output
    assert "flag_rflag:" in result.output
    assert "flag_data (clip" in result.output


def test_unsorted_rows_are_sorted_for_the_verbs(tmp_path):
    """A table whose rows are out of time order is judged in time order
    and written back in place."""
    path = make_synthetic_caltable(
        str(tmp_path / "u.bim"), nrow=8, nchan=8, ncorr=1, nant=2)
    with table(path, readonly=False, ack=False) as tab:
        times = np.asarray(tab.getcol("TIME")).ravel()
        order = np.array([1, 0, 3, 2, 5, 4, 7, 6])
        tab.putcol("TIME", times[order])
    result = _invoke("--ms", path, "--flag", "tfcrop", "--apply", "--clobber")
    assert result.exit_code == 0, result.output
    with table(path, ack=False) as tab:
        assert np.allclose(np.asarray(tab.getcol("TIME")).ravel(), times)


def test_write_needs_apply(cal):
    result = _invoke("--ms", cal, "--flag", "tfcrop")
    assert result.exit_code == 0, result.output
    assert "Nothing written" in result.output
    assert _read_flag(cal).mean() == 0.0


def test_update_requires_clobber(cal):
    result = CliRunner().invoke(
        main, ["--ms", cal, "--flag", "tfcrop", "--apply"],
        catch_exceptions=True)
    _expect_error(result, "--clobber")


@pytest.mark.parametrize("extra,match", [
    (["--flag", "save:v1"], "flag versions"),
    (["--flag", "restore:v1"], "flag versions"),
    (["--flag", "autos"], "autocorrelations"),
    (["--flag", "uv-above 100"], "no UVW"),
    (["--flag", "extend"], "not implemented for caltables"),
    (["--flag", "spectral-window x.yml"], "one spectral window"),
    (["--field", "BPCAL"], "not supported: --field"),
    (["--scan", "1"], "not supported: --scan"),
])
def test_ms_only_features_are_rejected(cal, tmp_path, extra, match):
    (tmp_path / "x.yml").write_text("flag: []\n")
    result = CliRunner().invoke(
        main, ["--ms", cal, "--apply", "--clobber"] + extra,
        catch_exceptions=True)
    _expect_error(result, match)


def test_msout_is_rejected(cal):
    result = CliRunner().invoke(
        main, ["--ms", cal, "--flag", "nan", "--apply", "--clobber",
               "--msout", "/tmp/whatever.ms"],
        catch_exceptions=True)
    _expect_error(result, "--msout")


def test_data_column_spellings(cal):
    # The MS default and the explicit name both mean CPARAM; an MS column
    # name is refused.
    for spelling in ("DATA", "CPARAM"):
        path = make_synthetic_caltable(str(cal) + "." + spelling)
        result = _invoke("--ms", path, "--data-column", spelling,
                         "--flag", "tfcrop")
        assert result.exit_code == 0, result.output
    result = CliRunner().invoke(
        main, ["--ms", cal, "--data-column", "RESIDUAL", "--flag", "tfcrop"],
        catch_exceptions=True)
    _expect_error(result, "CPARAM")


def test_summary_reports_per_antenna(cal):
    result = _invoke("--ms", cal, "--flag", "tfcrop", "--apply", "--clobber",
                     "--summary")
    assert result.exit_code == 0, result.output
    assert "summary:" in result.output
    assert "antenna 1:" in result.output
