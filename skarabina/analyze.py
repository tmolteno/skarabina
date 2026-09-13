# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Analyze a measurement set and recommend an image size."""

import json

import click
import numpy as np
import dask.array as da
from angle_parser import parse_angle
from casacore.tables import table
from daskms import xds_from_ms

# Sentinel prefix for the single-line JSON emitted by --json-stdout.  Stimela
# cab definitions match on this exact string, so treat it as a public API.
JSON_STDOUT_PREFIX = "SKARABINA_ANALYZE_JSON "

C_MS = 299792458.0
SIDEREAL_DAY_S = 86164.0905
# TMS (Synthesis Imaging in Radio Astronomy II) p.246: the constant that keeps
# time-average smearing at the edge of the field below ~10%.
TMA_C = 0.1
# 0.939 is the usual coefficient in the bandwidth-smearing factor R_b.
BW_SMEARING_COEFF = 0.939


def max_uv_distance(ds):
    """Longest baseline in the dataset, in metres.

    Rows with ``FLAG_ROW`` set are ignored: they carry no usable data (a
    flagger such as ``skarabina --flag-uv-above`` marks exactly those rows),
    so counting them would recommend an image size for baselines that will
    never be imaged.  Raises :class:`click.ClickException` when every row is
    flagged.
    """
    uvw = da.asarray(ds.UVW)
    uv_sq = uvw[:, 0] * uvw[:, 0] + uvw[:, 1] * uvw[:, 1]

    if "FLAG_ROW" in ds:
        keep = da.logical_not(da.asarray(ds.FLAG_ROW))
        n_flagged = int(da.sum(da.asarray(ds.FLAG_ROW)).compute())
        n_total = int(ds.FLAG_ROW.shape[0])
        if n_flagged:
            print(
                f"  Ignoring {n_flagged} of {n_total} rows with FLAG_ROW set"
            )
        if n_flagged >= n_total:
            raise click.ClickException(
                "Every row is flagged (FLAG_ROW) — nothing left to image"
            )
        if n_flagged:
            uv_sq = da.where(keep, uv_sq, 0.0)

    return float(da.sqrt(da.max(uv_sq)).compute())


def band_info(ms):
    """(lowest, highest) channel frequency in Hz, and channels per spw.

    Returns ``(None, None, None)`` when the MS has no SPECTRAL_WINDOW subtable.
    """
    t = table(ms)
    try:
        for sub in t.getsubtables():
            if "SPECTRAL_WINDOW" in sub:
                sw = table(sub, ack=False)
                try:
                    freqs = sw.getcol("CHAN_FREQ")
                    return (
                        float(freqs.min()),
                        float(freqs.max()),
                        int(freqs.shape[-1]),
                    )
                finally:
                    sw.close()
    finally:
        t.close()
    return None, None, None


def max_channel_frequency(ms):
    """Highest channel frequency in the MS, in Hz."""
    return band_info(ms)[1]


def averaging_limits(max_uv, nu_min, nu_max, n_chan, fov_rad):
    """How coarsely the data can be averaged before smearing matters.

    All three limits are quoted at the *edge of the requested field of view*
    (``fov_rad / 2``), which is the part of the image that actually has to stay
    intact.  (The superseded set-image-parameters cab used a fixed 10 degree
    field edge for the channel limit, which is far stricter -- it protects the
    whole primary beam rather than the image.)

    Returns ``(max_channel_width_hz, min_channels, max_integration_time_s,
    bandwidth_smearing_factor)``:

    * white-light fringes: beyond ``max_channel_width_hz`` the fringes from the
      two ends of the band decorrelate at the field edge, so the data supports
      no fewer than ``min_channels`` channels;
    * time-average smearing: ``max_integration_time_s`` is the longest
      integration that keeps the loss at the edge below ~10% (TMS p.246);
    * ``bandwidth_smearing_factor`` is the resulting radial smearing factor R_b
      for the channels as they are *now*, i.e. before any averaging.
    """
    theta_edge = fov_rad / 2.0

    max_channel_width = C_MS / (max_uv * theta_edge)
    bandwidth = nu_max - nu_min
    min_channels = int(bandwidth / max_channel_width) + 1

    omega_e = 2.0 * np.pi / SIDEREAL_DAY_S
    max_integration_time = TMA_C / (omega_e * max_uv * theta_edge)

    channel_width = bandwidth / n_chan if n_chan else bandwidth
    l = np.sin(theta_edge) * np.cos(theta_edge)
    m = np.sin(theta_edge) * np.sin(theta_edge)
    r_1 = np.hypot(l, m)
    r_b = 1.0 / np.sqrt(
        1.0 + (BW_SMEARING_COEFF * r_1 * channel_width / (fov_rad * nu_max)) ** 2
    )

    return max_channel_width, min_channels, max_integration_time, r_b


@click.command("skarabina-analyze")
@click.option("--ms", required=True, help="Input measurement set")
@click.option(
    "--image-fov",
    type=str,
    required=True,
    help="Image field-of-view (value with unit: deg, arcmin, arcsec, rad)",
)
@click.option(
    "--oversampling-factor",
    type=float,
    default=5.0,
    show_default=True,
    help="Pixels per resolution element (synthesised beam)",
)
@click.option(
    "--output-json",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write analysis results as JSON to this file",
)
@click.option(
    "--json-stdout",
    is_flag=True,
    default=False,
    help="Print the analysis results as a single JSON line on stdout",
)
def main(ms, image_fov, oversampling_factor, output_json, json_stdout):
    """Analyze a measurement set and recommend an image size.

    Computes the angular resolution from the longest baseline and highest
    frequency, recommends image dimensions in pixels, and reports how coarsely
    the data may be averaged (channel width, integration time) before smearing
    shows at the edge of the field.
    """
    # Group by DATA_DESC_ID only: dask-ms's default (FIELD_ID, DATA_DESC_ID)
    # grouping would hide every field but the first from the baseline search.
    datasets = xds_from_ms(ms, group_cols=("DATA_DESC_ID",))
    ds = datasets[0]

    # Max UV distance (metres), ignoring rows flagged by a previous flagger
    max_uv = max_uv_distance(ds)

    # Band edges and channel count from SPECTRAL_WINDOW
    nu_min, nu_max, n_chan = band_info(ms)

    if nu_max is None or max_uv == 0:
        raise click.ClickException("Could not determine resolution from MS")

    fov_rad = parse_angle(image_fov)

    # Angular resolution (radians)
    theta_res = C_MS / (nu_max * max_uv)

    # Image size in pixels
    n_pix = int(oversampling_factor * fov_rad / theta_res)

    # Round up to even or nice number
    n_pix = ((n_pix + 1) // 2) * 2  # even

    theta_res_arcsec = theta_res * 180.0 / 3.14159265 * 3600.0

    (
        max_channel_width,
        min_channels,
        max_integration_time,
        r_b,
    ) = averaging_limits(max_uv, nu_min, nu_max, n_chan, fov_rad)

    print(f"Measurement set:  {ms}")
    print(f"  Max baseline:   {max_uv:.0f} m")
    print(f"  Max frequency:  {nu_max / 1e6:.3f} MHz")
    print(f"  Resolution:     {theta_res_arcsec:.2f} arcsec")
    print(f"  Field of view:  {image_fov}")
    print(f"Recommended image size: {n_pix} × {n_pix} pixels")
    print(f"Averaging limits at the field edge ({fov_rad / 2 * 180 / np.pi:.2f} deg):")
    print(f"  Max channel width: {max_channel_width / 1e3:.1f} kHz"
          f"  (no fewer than {min_channels} channels)")
    print(f"  Max integration:   {max_integration_time:.1f} s")
    print(f"  Bandwidth smearing factor (as-is): {r_b:.4f}")

    result = {
        "ms": ms,
        "max_baseline_m": max_uv,
        "min_frequency_hz": nu_min,
        "max_frequency_hz": nu_max,
        "max_frequency_mhz": nu_max / 1e6,
        "bandwidth_hz": nu_max - nu_min,
        "num_channels": n_chan,
        "resolution_arcsec": theta_res_arcsec,
        "field_of_view": image_fov,
        "oversampling_factor": oversampling_factor,
        "recommended_image_size_pixels": n_pix,
        "max_channel_width_hz": max_channel_width,
        "min_channels": min_channels,
        "max_integration_time_s": max_integration_time,
        "bandwidth_smearing_factor": r_b,
    }

    if output_json:
        with open(output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Wrote {output_json}")

    if json_stdout:
        # A single line, prefixed by a sentinel, so that a stimela output
        # wrangler can pick the machine-readable results out of this cab's
        # console output.  Keep this line intact: it is a public interface.
        print(f"{JSON_STDOUT_PREFIX}{json.dumps(result)}")
