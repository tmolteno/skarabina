# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Analyze a measurement set and recommend an image size."""

import json

import click
import dask.array as da
from angle_parser import parse_angle
from casacore.tables import table
from daskms import xds_from_ms

# Sentinel prefix for the single-line JSON emitted by --json-stdout.  Stimela
# cab definitions match on this exact string, so treat it as a public API.
JSON_STDOUT_PREFIX = "SKARABINA_ANALYZE_JSON "


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


def max_channel_frequency(ms):
    """Highest channel frequency in the MS, in Hz."""
    nu_max = None
    t = table(ms)
    try:
        for sub in t.getsubtables():
            if "SPECTRAL_WINDOW" in sub:
                sw = table(sub, ack=False)
                nu_max = float(sw.getcol("CHAN_FREQ").max())
                sw.close()
                break
    finally:
        t.close()
    return nu_max


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

    Computes the angular resolution from the longest baseline and
    highest frequency, then recommends image dimensions in pixels.
    """
    datasets = xds_from_ms(ms)
    ds = datasets[0]

    # Max UV distance (metres), ignoring rows flagged by a previous flagger
    max_uv = max_uv_distance(ds)

    # Max frequency (Hz) from SPECTRAL_WINDOW
    nu_max = max_channel_frequency(ms)

    if nu_max is None or max_uv == 0:
        raise click.ClickException("Could not determine resolution from MS")

    c_ms = 299792458.0
    fov_rad = parse_angle(image_fov)

    # Angular resolution (radians)
    theta_res = c_ms / (nu_max * max_uv)

    # Image size in pixels
    n_pix = int(oversampling_factor * fov_rad / theta_res)

    # Round up to even or nice number
    n_pix = ((n_pix + 1) // 2) * 2  # even

    theta_res_arcsec = theta_res * 180.0 / 3.14159265 * 3600.0

    print(f"Measurement set:  {ms}")
    print(f"  Max baseline:   {max_uv:.0f} m")
    print(f"  Max frequency:  {nu_max / 1e6:.3f} MHz")
    print(f"  Resolution:     {theta_res_arcsec:.2f} arcsec")
    print(f"  Field of view:  {image_fov}")
    print(f"Recommended image size: {n_pix} × {n_pix} pixels")

    result = {
        "ms": ms,
        "max_baseline_m": max_uv,
        "max_frequency_hz": nu_max,
        "max_frequency_mhz": nu_max / 1e6,
        "resolution_arcsec": theta_res_arcsec,
        "field_of_view": image_fov,
        "oversampling_factor": oversampling_factor,
        "recommended_image_size_pixels": n_pix,
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
