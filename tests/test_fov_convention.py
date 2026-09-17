# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for the field-of-view convention.

Both cabs take the field of view as a **full width**: `skarabina-analyze
--image-fov` spans the image, and `skarabina --field-of-view` feeds the
fringe-rotation integration-time limit through the distance ℓ from the phase
centre to the edge, which is half the full width.  The two used to disagree
(the flagger called its value a half-width), so the same number handed to both
cabs described fields a factor of two apart.
"""
import math

import pytest

from skarabina.dask_ms import max_integration_time

C_MS = 299792458.0
OMEGA_EARTH = 7.2921150e-5


def _small_angle(nu_max_hz, uv_max_m, ell_rad, loss):
    """The small-angle form of the Wijnholds (2018) limit, in terms of ℓ.

    ``max_integration_time`` inverts ``ρ = sinc(π·ω·Δt·B·ν·ℓ/c)`` exactly; this
    is the historical ``√(6L)`` approximation of that inverse, which it
    approaches as the loss falls.  The two are compared with a relative
    tolerance below rather than for equality.
    """
    return (
        C_MS
        * (6.0 * loss) ** 0.5
        / (math.pi * OMEGA_EARTH * uv_max_m * nu_max_hz * ell_rad)
    )


def test_field_of_view_is_full_width():
    """A 2° field means ℓ = 1°, so the limit matches the reference at ℓ=1°."""
    fov_rad = math.radians(2.0)
    got = max_integration_time(1.4e9, 1000.0, fov_rad, loss=0.01)
    assert got == pytest.approx(
        _small_angle(1.4e9, 1000.0, math.radians(1.0), 0.01), rel=2e-3
    )


def test_halving_the_field_doubles_the_limit():
    """Below the sinc's first zero, Δt_max ∝ 1/ℓ ∝ 1/FOV."""
    one_deg = max_integration_time(1.4e9, 1000.0, math.radians(1.0), 0.01)
    two_deg = max_integration_time(1.4e9, 1000.0, math.radians(2.0), 0.01)
    assert one_deg == pytest.approx(2.0 * two_deg)


@pytest.mark.parametrize("loss", [0.01, 0.03, 0.05])
def test_loss_scaling(loss):
    got = max_integration_time(1.4e9, 1000.0, math.radians(2.0), loss)
    want = _small_angle(1.4e9, 1000.0, math.radians(1.0), loss)
    # the exact inverse sits just above the small-angle form, by a gap that
    # grows with the tolerated loss (0.15% at L=0.01, 0.76% at L=0.05)
    assert got > want
    assert got == pytest.approx(want, rel=0.01)


@pytest.mark.parametrize(
    "nu_max_hz,uv_max_m,fov_rad",
    [(0.0, 1000.0, 0.02), (1.4e9, 0.0, 0.02), (1.4e9, 1000.0, 0.0)],
)
def test_degenerate_inputs_give_infinity(nu_max_hz, uv_max_m, fov_rad):
    assert max_integration_time(nu_max_hz, uv_max_m, fov_rad) == float("inf")
