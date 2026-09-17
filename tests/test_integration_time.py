# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The fringe-rotation integration-time limit.

These tests exercise the real ``skarabina.dask_ms`` implementation.  (An
earlier version of this module defined its own local copy of the formula, so it
passed regardless of what the package did.)

The physics is the fringe-washing factor for time averaging:

    ρ = sinc(π · ω_⊕ · Δt · B · ν · ℓ / c)

so the tests below do two independent things: check that the value returned for
a requested loss really produces that loss under the relation, and check the
relation itself against a brute-force average of the visibility phasor.
"""
import math

import pytest

from skarabina.dask_ms import (
    C_MS,
    OMEGA_EARTH,
    max_integration_time,
    time_average_loss_to_dt,
    time_average_smearing_loss,
)

NU_HZ = 1.4e9
UV_M = 1000.0
ELL_RAD = 1.0


def _small_angle(loss, nu_hz, uv_m, ell_rad):
    """The historical form: Δt = c·√(6L)/(π·ω_⊕·B·ν·ℓ)."""
    return (
        C_MS * math.sqrt(6.0 * loss)
        / (math.pi * OMEGA_EARTH * uv_m * nu_hz * ell_rad)
    )


def _brute_force_x(loss):
    """First root of sinc(π·x) = 1 − loss, found by bisection on (0, 1)."""
    target = 1.0 - loss
    lo, hi = 1e-12, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if math.sin(math.pi * mid) / (math.pi * mid) > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --- the returned limit really achieves the requested loss -------------------


@pytest.mark.parametrize("loss", [0.001, 0.01, 0.03, 0.05, 0.1, 0.2, 0.5])
def test_returned_limit_achieves_the_requested_loss(loss):
    """This is the property the old (local-formula) tests could not check."""
    dt = max_integration_time(NU_HZ, UV_M, 2 * ELL_RAD, loss=loss)
    assert time_average_smearing_loss(dt, NU_HZ, UV_M, ELL_RAD) == pytest.approx(
        loss, abs=1e-9
    )


@pytest.mark.parametrize("loss", [0.001, 0.01, 0.05, 0.1, 0.2])
def test_loss_inverse_is_exact(loss):
    """``loss_to_dt`` and ``smearing_loss`` are exact inverses."""
    dt = time_average_loss_to_dt(loss, NU_HZ, UV_M, ELL_RAD)
    assert time_average_smearing_loss(dt, NU_HZ, UV_M, ELL_RAD) == pytest.approx(
        loss, abs=1e-9
    )


def test_matches_brute_force_root_of_the_relation():
    """Anchor the closed form against a bisection of sinc(πx) = 1 − L."""
    for loss in (0.01, 0.05, 0.1, 0.3):
        got = max_integration_time(NU_HZ, UV_M, 2 * ELL_RAD, loss=loss)
        want = _brute_force_x(loss) * C_MS / (OMEGA_EARTH * UV_M * NU_HZ * ELL_RAD)
        assert got == pytest.approx(want, rel=1e-9)


def test_small_angle_form_is_close_but_slightly_shorter():
    """The historical √(6L) form is a small-angle approximation of this one."""
    for loss in (0.01, 0.1, 0.2):
        exact = max_integration_time(NU_HZ, UV_M, 2 * ELL_RAD, loss=loss)
        small = _small_angle(loss, NU_HZ, UV_M, ELL_RAD)
        assert small < exact, "the small-angle form is the conservative one"
        assert exact == pytest.approx(small, rel=0.05)
    # and the gap grows with loss
    gaps = [
        max_integration_time(NU_HZ, UV_M, 2 * ELL_RAD, loss=L)
        / _small_angle(L, NU_HZ, UV_M, ELL_RAD)
        for L in (0.01, 0.1, 0.2)
    ]
    assert gaps == sorted(gaps)


# --- the relation itself -----------------------------------------------------


@pytest.mark.parametrize("dt", [0.05, 0.1, 0.2, 0.5, 1.0])
def test_smearing_relation_matches_a_direct_phasor_average(dt):
    """Independent check of ρ = sinc(πx) against averaging the phasor.

    A source 1 rad from the phase centre on a 1 km baseline at 1.4 GHz: the
    Earth's rotation sweeps the delay, and the correlator averages the resulting
    phasor over the integration.  This recomputes that average numerically in
    real units and compares it with the closed form.
    """
    uv_m, nu_hz, theta = 1000.0, 1.4e9, 1.0
    tau0 = uv_m * theta / C_MS
    n = 40001
    re = im = 0.0
    for i in range(n):
        t = -dt / 2 + dt * i / (n - 1)
        tau = uv_m * (theta + OMEGA_EARTH * t) / C_MS
        phi = 2 * math.pi * nu_hz * (tau - tau0)
        re += math.cos(phi)
        im += math.sin(phi)
    simulated = 1.0 - math.hypot(re, im) / n
    assert time_average_smearing_loss(dt, nu_hz, uv_m, theta) == pytest.approx(
        simulated, abs=2e-5
    )


def test_loss_grows_with_integration_time():
    losses = [
        time_average_smearing_loss(dt, NU_HZ, UV_M, ELL_RAD)
        for dt in (0.01, 0.1, 0.5, 1.0)
    ]
    assert losses == sorted(losses)
    # a 10 ms integration is essentially lossless; 1 s is not
    assert losses[0] < 1e-4
    assert losses[-1] > 0.1


# --- the field-of-view convention -------------------------------------------


def test_field_of_view_is_full_width():
    """A 2° field means ℓ = 1°, so the limit matches ℓ = 1° directly."""
    full_width = math.radians(2.0)
    got = max_integration_time(NU_HZ, UV_M, full_width, loss=0.01)
    assert got == pytest.approx(
        time_average_loss_to_dt(0.01, NU_HZ, UV_M, math.radians(1.0))
    )


def test_halving_the_field_doubles_the_limit():
    """Below the first zero of the sinc, Δt_max ∝ 1/ℓ ∝ 1/FOV."""
    one_deg = max_integration_time(NU_HZ, UV_M, math.radians(1.0), loss=0.01)
    two_deg = max_integration_time(NU_HZ, UV_M, math.radians(2.0), loss=0.01)
    assert one_deg == pytest.approx(2.0 * two_deg)


def test_scaling_with_baseline_and_frequency():
    """Longer baselines and higher frequencies both tighten the limit."""
    assert max_integration_time(NU_HZ, 100.0, 1.0) == pytest.approx(
        10.0 * max_integration_time(NU_HZ, 1000.0, 1.0)
    )
    assert max_integration_time(150e6, UV_M, 1.0) == pytest.approx(
        (1.4e9 / 150e6) * max_integration_time(NU_HZ, UV_M, 1.0), rel=1e-6
    )


# --- degenerate inputs -------------------------------------------------------


@pytest.mark.parametrize(
    "nu_hz,uv_m,fov_rad",
    [(0.0, 1000.0, 0.02), (1.4e9, 0.0, 0.02), (1.4e9, 1000.0, 0.0)],
)
def test_degenerate_inputs_give_infinity(nu_hz, uv_m, fov_rad):
    assert max_integration_time(nu_hz, uv_m, fov_rad) == float("inf")
    assert time_average_loss_to_dt(0.01, nu_hz, uv_m, fov_rad) == float("inf")


def test_zero_loss_gives_zero_time():
    """No loss tolerated means no averaging at all."""
    assert time_average_loss_to_dt(0.0, NU_HZ, UV_M, ELL_RAD) == 0.0
    assert time_average_loss_to_dt(-0.5, NU_HZ, UV_M, ELL_RAD) == 0.0


def test_total_loss_gives_infinity():
    """A loss of 100% is the first zero of the sinc; beyond it, unbounded."""
    assert time_average_loss_to_dt(1.0, NU_HZ, UV_M, ELL_RAD) == float("inf")
    assert time_average_loss_to_dt(1.5, NU_HZ, UV_M, ELL_RAD) == float("inf")


def test_zero_integration_time_loses_nothing():
    assert time_average_smearing_loss(0.0, NU_HZ, UV_M, ELL_RAD) == 0.0


def test_documented_coefficient_table_matches_the_code():
    """Pin doc/AVERAGING.md's small-angle-vs-exact table to the code.

    The doc justifies the exact inversion by quoting how far the small-angle
    form sits from it; if either changes, the table must change with it.
    """
    from pathlib import Path

    doc = (
        Path(__file__).resolve().parent.parent / "doc" / "AVERAGING.md"
    ).read_text()
    for loss in (0.01, 0.10, 0.20):
        exact = time_average_loss_to_dt(loss, NU_HZ, UV_M, ELL_RAD)
        exact_coef = exact * OMEGA_EARTH * UV_M * NU_HZ * ELL_RAD / C_MS
        small_coef = math.sqrt(6.0 * loss) / math.pi
        assert f"| {loss:.2f} | {small_coef:.4f}" in doc, loss
        assert f"{exact_coef:.4f}" in doc, loss


def test_documented_example_table_matches_the_code():
    """Pin doc/AVERAGING.md's worked example values to the code."""
    import re
    from pathlib import Path

    doc = (
        Path(__file__).resolve().parent.parent / "doc" / "AVERAGING.md"
    ).read_text()
    assert "### Example values" in doc

    def cell(x):
        return f"{x * 1000:.1f} ms" if x < 1 else f"{x:.2f} s"

    # quoted as "| 1 km     | 1.4 GHz   | 229.3 ms | 735.3 ms |"
    for baseline, nu, label in (
        (100.0, 1.4e9, "100 m"),
        (1000.0, 1.4e9, "1 km"),
        (10000.0, 1.4e9, "10 km"),
    ):
        row = re.compile(
            r"\|\s*" + re.escape(label) + r"\s*\|\s*1\.4 GHz\s*\|\s*"
            + re.escape(cell(max_integration_time(nu, baseline, 2.0, loss=0.01)))
            + r"\s*\|\s*"
            + re.escape(cell(max_integration_time(nu, baseline, 2.0, loss=0.10)))
            + r"\s*\|"
        )
        assert row.search(doc), f"no doc row matches {label} at 1.4 GHz"
    # the two other frequency rows
    for nu, freq in ((150e6, "150 MHz"), (5e9, "5 GHz")):
        row = re.compile(
            r"\|\s*1 km\s*\|\s*" + re.escape(freq) + r"\s*\|\s*"
            + re.escape(cell(max_integration_time(nu, 1000.0, 2.0, loss=0.01)))
            + r"\s*\|\s*"
            + re.escape(cell(max_integration_time(nu, 1000.0, 2.0, loss=0.10)))
            + r"\s*\|"
        )
        assert row.search(doc), f"no doc row matches 1 km at {freq}"
