# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Analyze a measurement set and recommend an image size."""

import click
import dask.array as da
from casacore.tables import table
from daskms import xds_from_ms


@click.command("skarabina-analyze")
@click.option("--ms", required=True, help="Input measurement set")
@click.option(
    "--image-fov",
    type=float,
    required=True,
    help="Image field-of-view (degrees)",
)
@click.option(
    "--oversampling-factor",
    type=float,
    default=5.0,
    show_default=True,
    help="Pixels per resolution element (synthesised beam)",
)
def main(ms, image_fov, oversampling_factor):
    """Analyze a measurement set and recommend an image size.

    Computes the angular resolution from the longest baseline and
    highest frequency, then recommends image dimensions in pixels.
    """
    datasets = xds_from_ms(ms)
    ds = datasets[0]

    # Max UV distance (metres)
    uvw = da.asarray(ds.UVW)
    u_arr = uvw[:, 0]
    v_arr = uvw[:, 1]
    max_uv = float(da.sqrt(da.max(u_arr * u_arr + v_arr * v_arr)).compute())

    # Max frequency (Hz) from SPECTRAL_WINDOW
    nu_max = None
    t = table(ms)
    for sub in t.getsubtables():
        if "SPECTRAL_WINDOW" in sub:
            sw = table(sub, ack=False)
            nu_max = float(sw.getcol("CHAN_FREQ").max())
            sw.close()
            break
    t.close()

    if nu_max is None or max_uv == 0:
        print("Could not determine resolution from MS")
        return

    c_ms = 299792458.0
    fov_rad = image_fov * 3.14159265 / 180.0

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
    print(f"  Field of view:  {image_fov:.2f}°")
    print(f"Recommended image size: {n_pix} × {n_pix} pixels")
