# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for the averaging limits reported by ``skarabina-analyze``.

These are the numbers that used to come out of the superseded
``set-image-parameters`` cab in the white-belt pipeline: how wide a channel and
how long an integration the data supports before smearing shows at the edge of
the field of view.
"""
import numpy as np
import pytest

from skarabina.analyze import averaging_limits

C = 299792458.0

# A MeerKAT L-band observation like the pipeline's mergA_tim: 2500 m baselines,
# 856-1711 MHz, 2511 channels, 3.3 deg field.
MAX_UV = 2500.0
NU_MIN = 856e6
NU_MAX = 1711e6
N_CHAN = 2511
FOV_RAD = np.radians(3.3)


def test_channel_width_limit_is_the_white_light_fringe_condition():
    max_channel_width, _, _, _ = averaging_limits(
        MAX_UV, NU_MIN, NU_MAX, N_CHAN, FOV_RAD
    )
    # c / (B_max * theta_edge)
    assert max_channel_width == pytest.approx(
        C / (MAX_UV * FOV_RAD / 2), rel=1e-12
    )


def test_min_channels_is_the_bandwidth_divided_by_that_width():
    max_channel_width, min_channels, _, _ = averaging_limits(
        MAX_UV, NU_MIN, NU_MAX, N_CHAN, FOV_RAD
    )
    assert min_channels == int((NU_MAX - NU_MIN) / max_channel_width) + 1
    # The pipeline averages 2511 channels by 8 -> 314, so the observation must
    # have room for far fewer channels than that.
    assert min_channels < N_CHAN / 8


def test_longer_baselines_and_wider_fields_tighten_both_limits():
    wide = averaging_limits(MAX_UV, NU_MIN, NU_MAX, N_CHAN, np.radians(6.6))
    narrow = averaging_limits(MAX_UV, NU_MIN, NU_MAX, N_CHAN, FOV_RAD)
    long_baseline = averaging_limits(2 * MAX_UV, NU_MIN, NU_MAX, N_CHAN, FOV_RAD)

    # A wider field (bigger theta_edge) and longer baselines both smear sooner.
    assert wide[0] < narrow[0] and wide[2] < narrow[2]
    assert long_baseline[0] < narrow[0] and long_baseline[2] < narrow[2]


def test_integration_limit_matches_the_tms_formula():
    _, _, max_integration_time, _ = averaging_limits(
        MAX_UV, NU_MIN, NU_MAX, N_CHAN, FOV_RAD
    )
    omega_e = 2 * np.pi / 86164.0905
    assert max_integration_time == pytest.approx(
        0.1 / (omega_e * MAX_UV * FOV_RAD / 2), rel=1e-12
    )


def test_bandwidth_smearing_factor_is_between_zero_and_one():
    _, _, _, r_b = averaging_limits(MAX_UV, NU_MIN, NU_MAX, N_CHAN, FOV_RAD)
    assert 0.0 < r_b <= 1.0

    # Averaging the band down to the minimum channel count makes R_b worse
    # (smaller), while the unaveraged channels are close to ideal.
    channel_width = (NU_MAX - NU_MIN) / N_CHAN
    worst = averaging_limits(MAX_UV, NU_MIN, NU_MAX, 1, FOV_RAD)[3]
    assert worst < r_b <= 1.0
    assert channel_width < C / (MAX_UV * FOV_RAD / 2)
