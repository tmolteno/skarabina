# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Agreement between the two commands that quote a max integration time.

One measurement set, two CLIs: ``skarabina --summary`` prints a table of
fringe-rotation limits (1/3/5/10% loss) and ``skarabina-analyze`` prints
``max_integration_time_s``, which flows through the stimela cab as the same
key.  For one MS and one loss criterion the two must quote the same number.

The regression pinned here (found by external review against the v0.8.8
tree): analyze computed its integration limit from a local
``TMA_C / (omega_e * B * theta)`` heuristic with **no observing frequency**
in it, so the same baselines gave the same answer at 300 MHz and at 3 GHz
-- 8.2 s for the doc example where the shared sinc inversion says 3.4 s,
i.e. ~50% loss at the field edge where the docstring promised ~10%.  The
fix routed analyze through :func:`skarabina.dask_ms.max_integration_time`
(commit 6142622, released in 1.0.0).  The tests below guard each layer of
that fix, so a reintroduced local formula, a dropped frequency factor, or a
FOV-convention slip between the two commands fails loudly:

* the two modules share one function *object* (a local redefinition would
  shadow the import);
* analyze's limit really scales with the observing frequency;
* the limit analyze reports really achieves the loss it claims;
* hard golden values for the reviewed example (independent of the docs);
* the two CLIs end to end on one synthetic MS.

Note ``max_channel_width_hz`` legitimately has no frequency dependence --
the white-light fringe condition is ``c / (B · θ_edge)``.  Only the
*time* limit carries ν.
"""
import io
import json
import math
import contextlib
import re

import pytest
from click.testing import CliRunner

import skarabina.analyze as analyze_module
from skarabina import dask_ms
from skarabina.analyze import (
    JSON_STDOUT_PREFIX,
    BandInfo,
    averaging_limits,
    main as analyze_cli,
)
from skarabina.dask_ms import (
    TIME_AVERAGE_LOSS,
    DaskMS,
    max_integration_time,
    time_average_smearing_loss,
)
from ms_fixture import make_synthetic_ms

# The example the review was computed against: 7697 m longest baseline,
# 1800 MHz top of band, 2.5 deg full-width field of view.
LETTER_B_MAX_M = 7697.0
LETTER_NU_MAX_HZ = 1800e6
LETTER_FOV_RAD = math.radians(2.5)

# Exact-inverse values of ρ = sinc(π·ω·Δt·B·ν·ℓ/c) for that example, at
# 10% and 1% loss.  Independently bisectioned; matches doc/AVERAGING.md's
# coefficient table (0.2504 / 0.0781) · λ/(ω·B·ℓ).
LETTER_T_10PC_S = 3.4059302826910813
LETTER_T_1PC_S = 1.0620990634549696
# What the v0.8.8-local formula printed for the same inputs: 0.1/(ω·B·θ).
# It must never come back.
V088_T_S = 8.166531165528172


def contiguous_band(nu_min_hz, nu_max_hz, n_chan=10):
    """A gap-free :class:`BandInfo`, as in test_analyze_smearing."""
    width = (nu_max_hz - nu_min_hz) / n_chan
    return BandInfo(
        nu_min_hz=nu_min_hz,
        nu_max_hz=nu_max_hz,
        n_chan=n_chan,
        channel_width_hz=width,
        bandwidth_hz=width * n_chan,
        span_hz=nu_max_hz - nu_min_hz,
        hole_hz=0.0,
        has_gaps=False,
    )


# --- one implementation, two entry points -----------------------------------


def test_analyze_and_dask_ms_share_one_function_object():
    """``analyze`` must use the *same* ``max_integration_time`` object.

    If analyze ever defines its own function of that name (how the v0.8.8
    divergence arose), the module attribute shadows the import and the two
    commands drift apart again.
    """
    assert analyze_module.max_integration_time is dask_ms.max_integration_time


def test_analyze_has_no_local_time_average_formula():
    """Source guard: the limit comes from dask_ms, not from a local copy.

    The old heuristic was literally ``TMA_C / (omega_e * max_uv *
    theta_edge)`` at analyze.py:112 -- no ν, hence frequency-independent.
    Neither that constant nor an omega-based expression belongs in
    analyze.py; the physics lives in dask_ms.py, once.
    """
    src = (
        __import__("pathlib").Path(analyze_module.__file__).read_text()
    ).lower()
    assert "omega" not in src, "analyze.py grew a local ω-based formula"
    assert "tma_c" not in src, "analyze.py grew a local TMS constant"
    assert "max_integration_time" in src, "analyze stopped quoting the limit"


# --- the limit depends on the observing frequency ---------------------------


@pytest.mark.parametrize(
    "nu_low_hz, nu_high_hz",
    [(300e6, 3e9), (856e6, 1711e6)],  # the review's example pair
)
def test_limit_scales_with_observing_frequency(nu_low_hz, nu_high_hz):
    """Same baselines, different bands: the limits must differ by ν.

    The v0.8.8 formula had no frequency in it at all, so this ratio was
    exactly 1.0 for every pair of bands.
    """
    max_uv, fov = 2500.0, math.radians(2.5)
    t_low = averaging_limits(
        contiguous_band(nu_low_hz - 20e6, nu_low_hz), max_uv, fov
    )[2]
    t_high = averaging_limits(
        contiguous_band(nu_high_hz - 20e6, nu_high_hz), max_uv, fov
    )[2]
    assert t_low / t_high == pytest.approx(nu_high_hz / nu_low_hz, rel=1e-9)
    # and the channel limit, which correctly has no ν, does not move
    assert averaging_limits(
        contiguous_band(nu_low_hz - 20e6, nu_low_hz), max_uv, fov
    )[0] == averaging_limits(
        contiguous_band(nu_high_hz - 20e6, nu_high_hz), max_uv, fov
    )[0]


@pytest.mark.parametrize("fov_deg", [1.0, 2.5, 6.6])
def test_analyze_limit_achieves_the_loss_it_claims(fov_deg):
    """Round trip: the limit analyze prints really yields TIME_AVERAGE_LOSS
    at the field edge under the sinc relation -- not ~50% like v0.8.8."""
    band = contiguous_band(856e6, 1711e6, n_chan=16)
    max_uv = 7697.0
    fov = math.radians(fov_deg)
    t_max = averaging_limits(band, max_uv, fov)[2]
    assert time_average_smearing_loss(
        t_max, band.nu_max_hz, max_uv, fov / 2.0
    ) == pytest.approx(TIME_AVERAGE_LOSS, abs=1e-9)


# --- golden values for the reviewed example ---------------------------------


@pytest.mark.parametrize(
    "loss, want_s",
    [(0.10, LETTER_T_10PC_S), (0.01, LETTER_T_1PC_S)],
)
def test_reviewed_example_golden_values(loss, want_s):
    """Pin literal numbers for the reviewed example (7697 m, 1800 MHz,
    2.5 deg), independent of any doc file."""
    got = max_integration_time(
        LETTER_NU_MAX_HZ, LETTER_B_MAX_M, LETTER_FOV_RAD, loss=loss
    )
    assert got == pytest.approx(want_s, rel=1e-9)


def test_v088_frequency_free_value_cannot_recur():
    """The v0.8.8 answer (8.17 s, ~50% loss) must never be returned.

    If a future refactor drops the ν factor or reintroduces the local
    ``0.1/(ω·B·θ)`` heuristic, this fails even if every other test is
    adjusted by accident.
    """
    got_10 = max_integration_time(
        LETTER_NU_MAX_HZ, LETTER_B_MAX_M, LETTER_FOV_RAD, loss=0.10
    )
    assert got_10 != pytest.approx(V088_T_S, rel=1e-3)
    got_1 = max_integration_time(
        LETTER_NU_MAX_HZ, LETTER_B_MAX_M, LETTER_FOV_RAD, loss=0.01
    )
    assert got_1 != pytest.approx(V088_T_S, rel=1e-3)
    # the reviewed losses at the quoted values
    assert time_average_smearing_loss(
        V088_T_S, LETTER_NU_MAX_HZ, LETTER_B_MAX_M, LETTER_FOV_RAD / 2
    ) == pytest.approx(0.496, abs=5e-3)  # "down about 50%"


# --- the two CLIs, one MS, end to end ---------------------------------------


def test_two_clis_quote_the_same_limit_for_one_ms(tmp_path, capsys):
    """Headline check: one MS, two CLIs, one number.

    Runs ``skarabina-analyze --json-stdout`` and ``DaskMS.summary()`` on
    the same synthetic measurement set with the same field of view, then
    compares (a) analyze's JSON against its own printed line, (b)
    summary's printed 10% row against analyze's JSON, and (c) every
    printed summary row against the shared function evaluated on the
    inputs analyze reported.  A divergence anywhere in either plumbing
    fails one of the three.
    """
    ms = str(tmp_path / "agree.ms")
    make_synthetic_ms(ms, nchan=4, nrow=4)

    result = CliRunner().invoke(
        analyze_cli,
        ["--ms", ms, "--image-fov", "2.5 deg", "--json-stdout"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    lines = [
        line
        for line in result.output.splitlines()
        if line.startswith(JSON_STDOUT_PREFIX)
    ]
    assert len(lines) == 1, "analyze must emit exactly one JSON line"
    payload = json.loads(lines[0][len(JSON_STDOUT_PREFIX):])
    t_analyze = payload["max_integration_time_s"]

    # (a) analyze's console line agrees with its JSON (printed to %.1f)
    printed = re.search(
        r"Max integration:\s+([0-9.]+) s", result.output
    )
    assert printed, "analyze did not print its Max integration line"
    assert float(printed.group(1)) == pytest.approx(t_analyze, abs=0.051)

    # summary on the same MS, same field of view
    daskms = DaskMS(ms)
    daskms._fov_rad = math.radians(2.5)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        daskms.summary()
    out = buf.getvalue()

    # (b) summary's 10% row (the TIME_AVERAGE_LOSS criterion) == analyze
    match = re.search(r"10% loss:\s+([0-9.]+) s", out)
    assert match, "summary did not print a 10% loss row"
    assert float(match.group(1)) == pytest.approx(t_analyze, abs=0.051)

    # (c) every summary row is the shared function on analyze's inputs
    fov_deg = 2.5
    for loss_pc in (1, 3, 5, 10):
        row = re.search(
            rf"{loss_pc}% loss:\s+([0-9.]+) s", out
        )
        assert row, f"summary did not print a {loss_pc}% loss row"
        want = max_integration_time(
            payload["max_frequency_hz"],
            payload["max_baseline_m"],
            math.radians(fov_deg),
            loss=loss_pc / 100.0,
        )
        assert float(row.group(1)) == pytest.approx(want, abs=0.051)
