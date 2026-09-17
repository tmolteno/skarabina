# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Tests for the averaging limits reported by ``skarabina-analyze``.

These are the numbers that used to come out of the superseded
``set-image-parameters`` cab in the white-belt pipeline: how wide a channel and
how long an integration the data supports before smearing shows at the edge of
the field of view.
"""
import math

import numpy as np
import pytest
from casacore.tables import table

from skarabina.analyze import BandInfo, averaging_limits, band_info
from ms_fixture import make_synthetic_ms

C = 299792458.0

# A MeerKAT L-band observation like the pipeline's mergA_tim: 2500 m baselines,
# 856-1711 MHz, 2511 channels, 3.3 deg field.
MAX_UV = 2500.0
NU_MIN = 856e6
NU_MAX = 1711e6
N_CHAN = 2511
FOV_RAD = np.radians(3.3)


def contiguous_band(nu_min=NU_MIN, nu_max=NU_MAX, n_chan=N_CHAN):
    """A BandInfo for a uniform, gap-free band."""
    width = (nu_max - nu_min) / n_chan
    return BandInfo(
        nu_min_hz=nu_min,
        nu_max_hz=nu_max,
        n_chan=n_chan,
        channel_width_hz=width,
        bandwidth_hz=width * n_chan,
        span_hz=nu_max - nu_min,
        hole_hz=0.0,
        has_gaps=False,
    )


BAND = contiguous_band()


def test_channel_width_limit_is_the_white_light_fringe_condition():
    max_channel_width, _, _, _ = averaging_limits(BAND, MAX_UV, FOV_RAD)
    # c / (B_max * theta_edge)
    assert max_channel_width == pytest.approx(
        C / (MAX_UV * FOV_RAD / 2), rel=1e-12
    )


def test_channel_width_limit_matches_the_wsclean_criterion():
    """WSClean's `maxuv-l` asks for the same thing expressed per pixel.

    Its test is ``max_baseline * max_wavelength / bandwidth < maxuv-l``; in
    pixels that is ``bandwidth < c / (B * theta_pixel)``.  Our limit is the
    single-channel form of that with theta_pixel = the field edge.
    """
    max_channel_width, _, _, _ = averaging_limits(BAND, MAX_UV, FOV_RAD)
    wsclean_equivalent = C / (MAX_UV * (FOV_RAD / 2))
    assert max_channel_width == pytest.approx(wsclean_equivalent, rel=1e-12)


def test_min_channels_is_the_bandwidth_divided_by_that_width():
    max_channel_width, min_channels, _, _ = averaging_limits(BAND, MAX_UV, FOV_RAD)
    assert min_channels == int(BAND.bandwidth_hz / max_channel_width) + 1
    # The pipeline averages 2511 channels by 8 -> 314, so the observation must
    # have room for far fewer channels than that.
    assert min_channels < N_CHAN / 8


def test_min_channels_uses_the_true_bandwidth_not_the_span():
    """A band with a hole supports fewer channels than its span suggests."""
    holed = BandInfo(
        nu_min_hz=NU_MIN,
        nu_max_hz=NU_MAX,
        n_chan=10,
        channel_width_hz=(NU_MAX - NU_MIN) / 12,
        bandwidth_hz=(NU_MAX - NU_MIN) / 12 * 10,
        span_hz=NU_MAX - NU_MIN,
        hole_hz=(NU_MAX - NU_MIN) / 6,
        has_gaps=True,
    )
    _, min_holed, _, _ = averaging_limits(holed, MAX_UV, FOV_RAD)
    _, min_full, _, _ = averaging_limits(
        contiguous_band(NU_MIN, NU_MAX, 10), MAX_UV, FOV_RAD
    )
    assert min_holed < min_full


def test_longer_baselines_and_wider_fields_tighten_both_limits():
    wide = averaging_limits(BAND, MAX_UV, np.radians(6.6))
    narrow = averaging_limits(BAND, MAX_UV, FOV_RAD)
    long_baseline = averaging_limits(BAND, 2 * MAX_UV, FOV_RAD)

    # A wider field (bigger theta_edge) and longer baselines both smear sooner.
    assert wide[0] < narrow[0] and wide[2] < narrow[2]
    assert long_baseline[0] < narrow[0] and long_baseline[2] < narrow[2]


def test_integration_limit_is_the_shared_criterion():
    """``analyze`` must quote the same limit as the flagger's summary.

    Both go through ``dask_ms.max_integration_time`` at the same loss, so the
    two commands cannot report different integration limits for one MS (they
    did: analyze used a bare 0.1/(omega*B*theta) while summary used the
    small-angle form of the same relation).
    """
    from skarabina.dask_ms import (
        TIME_AVERAGE_LOSS,
        max_integration_time,
        time_average_smearing_loss,
    )

    _, _, max_integration_time_s, _ = averaging_limits(BAND, MAX_UV, FOV_RAD)
    assert max_integration_time_s == pytest.approx(
        max_integration_time(NU_MAX, MAX_UV, FOV_RAD, loss=TIME_AVERAGE_LOSS)
    )
    # and it really is the limit for that loss, at the edge of the field
    assert time_average_smearing_loss(
        max_integration_time_s, NU_MAX, MAX_UV, FOV_RAD / 2
    ) == pytest.approx(TIME_AVERAGE_LOSS)


def test_bandwidth_smearing_factor_is_between_zero_and_one():
    _, _, _, r_b = averaging_limits(BAND, MAX_UV, FOV_RAD)
    assert 0.0 < r_b <= 1.0

    # Averaging the band down to the minimum channel count makes R_b worse
    # (smaller), while the unaveraged channels are close to ideal.
    wide_channel = contiguous_band(nu_min=NU_MIN, nu_max=NU_MAX, n_chan=1)
    worst = averaging_limits(wide_channel, MAX_UV, FOV_RAD)[3]
    assert worst < r_b <= 1.0
    assert BAND.channel_width_hz < C / (MAX_UV * FOV_RAD / 2)


def test_bandwidth_smearing_factor_matches_the_relation():
    """R_b is evaluated at the field edge, with r_1 = sin(theta_edge)."""
    r_1 = math.sin(FOV_RAD / 2)
    want = 1.0 / math.sqrt(
        1.0
        + (
            0.939
            * r_1
            * BAND.channel_width_hz
            / (FOV_RAD * NU_MAX)
        )
        ** 2
    )
    assert averaging_limits(BAND, MAX_UV, FOV_RAD)[3] == pytest.approx(
        want, rel=1e-12
    )


def test_bandwidth_smearing_factor_uses_the_real_channel_width():
    """A band described with wider channels must smear more.

    The regression this guards: R_b was computed from ``span / nchan``, so a
    band whose channels are narrower than that ratio implied (i.e. one with
    holes) was under-smearing.
    """
    n_chan = 100
    span = NU_MAX - NU_MIN
    true_width = span / (2 * n_chan)          # half the naive average
    narrow = BandInfo(
        nu_min_hz=NU_MIN, nu_max_hz=NU_MAX, n_chan=n_chan,
        channel_width_hz=true_width,
        bandwidth_hz=true_width * n_chan,
        span_hz=span, hole_hz=span / 2, has_gaps=True,
    )
    naive = contiguous_band(NU_MIN, NU_MAX, n_chan)  # width = span/n_chan
    assert averaging_limits(naive, MAX_UV, FOV_RAD)[3] < (
        averaging_limits(narrow, MAX_UV, FOV_RAD)[3]
    )


# --- reading the band off a real MS -----------------------------------------


def test_band_info_reads_the_real_channel_width(tmp_path):
    """The width comes from CHAN_WIDTH, not from the spacing of the channels."""
    path = str(tmp_path / "band.ms")
    make_synthetic_ms(path, nchan=4, ncorr=1, nrow=4)
    band = band_info(path)
    assert band.n_chan == 4
    assert not band.has_gaps
    # the fixture writes channels 10 MHz apart, with matching CHAN_WIDTH
    assert band.channel_width_hz == pytest.approx(1.0e7, rel=1e-9)
    assert band.bandwidth_hz == pytest.approx(4.0e7, rel=1e-9)
    assert band.span_hz == pytest.approx(3.0e7, rel=1e-9)
    assert band.hole_hz == pytest.approx(0.0, abs=1.0)


def test_band_info_detects_a_hole_from_dropped_channels(tmp_path):
    """The shape --optimize leaves behind: a gap inside the band."""
    path = str(tmp_path / "holed.ms")
    make_synthetic_ms(path, nchan=6, ncorr=1, nrow=4)

    # simulate --optimize dropping channels 2 and 3
    sw = table(path + "/SPECTRAL_WINDOW", readonly=False)
    keep = [0, 1, 4, 5]
    sw.putcol("NUM_CHAN", np.array([len(keep)], dtype=np.int32))
    for col in ("CHAN_FREQ", "CHAN_WIDTH", "RESOLUTION", "EFFECTIVE_BW"):
        if col in sw.colnames():
            sw.putcol(col, sw.getcol(col)[:, keep])
    sw.close()

    band = band_info(path)
    assert band.n_chan == 4
    assert band.has_gaps, "the 30 MHz spacing should register as a hole"
    assert band.channel_width_hz == pytest.approx(1.0e7, rel=1e-9)
    assert band.bandwidth_hz == pytest.approx(4.0e7, rel=1e-9)
    # span includes the two missing channels
    assert band.span_hz > band.bandwidth_hz
    assert band.hole_hz == pytest.approx(2.0e7, rel=1e-6)


def test_band_info_of_a_real_ms_is_contiguous(tmp_path):
    """A freshly written MS has no holes and a width equal to its spacing."""
    path = str(tmp_path / "plain.ms")
    make_synthetic_ms(path, nchan=5, ncorr=1, nrow=4)
    band = band_info(path)
    assert not band.has_gaps
    assert band.hole_hz == pytest.approx(0.0, abs=1.0)
    # uniform channels: span == (n-1) * width, bandwidth == n * width
    assert band.span_hz == pytest.approx(4 * band.channel_width_hz, rel=1e-9)
    assert band.bandwidth_hz == pytest.approx(5 * band.channel_width_hz, rel=1e-9)


def test_documented_worked_example_matches_the_code():
    """Pin doc/ANALYZE.md's worked example to the implementation.

    The example is a real run on a MeerKAT MS (the MT0 observation).  It had
    gone stale once already -- it predated both the full-width FOV convention
    and the averaging limits -- so its numbers are checked here rather than
    trusted.
    """
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent / "doc" / "ANALYZE.md").read_text()

    # inputs of the documented run
    max_uv = 7625.494677046231
    nu_min, nu_max, nchan = 856e6, 1711791015.625, 4096
    fov_rad = math.radians(2.5)
    oversampling = 5.0
    # the run's channels are exactly 208 984.375 Hz wide: 4096 of them make up
    # the 856 MHz band (this is the value CHAN_WIDTH holds on that MS)
    chan_width = 208984.375

    assert f"{max_uv:.0f} m" in doc
    assert f"{nu_max / 1e6:.3f} MHz" in doc

    theta_res = C / (nu_max * max_uv)
    assert f"{theta_res * 180 / math.pi * 3600:.2f} arcsec" in doc
    assert f"{math.degrees(fov_rad / 2):.2f} deg" in doc

    n_pix = ((int(oversampling * fov_rad / theta_res) + 1) // 2) * 2
    assert n_pix == 9500
    assert f"{n_pix} \u00d7 {n_pix} pixels" in doc

    band = BandInfo(
        nu_min_hz=nu_min,
        nu_max_hz=nu_max,
        n_chan=nchan,
        channel_width_hz=chan_width,
        bandwidth_hz=chan_width * nchan,
        span_hz=nu_max - nu_min,
        hole_hz=0.0,
        has_gaps=False,
    )
    max_channel_width, min_channels, t_max, _ = averaging_limits(
        band, max_uv, fov_rad
    )
    assert f"{max_channel_width / 1e3:.1f} kHz" in doc
    assert f"(no fewer than {min_channels} channels)" in doc
    assert f"{t_max:.1f} s" in doc

    # the JSON block quotes the same run
    assert f'"max_baseline_m": {max_uv!r}' in doc
    assert f'"max_frequency_hz": {nu_max!r}' in doc
    assert f'"recommended_image_size_pixels": {n_pix}' in doc
    assert f'"min_channels": {min_channels}' in doc
    assert f'"max_integration_time_s": {t_max!r}' in doc
