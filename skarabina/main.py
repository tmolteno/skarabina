# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import datetime
import logging
from importlib.metadata import version as get_version
from types import SimpleNamespace

import click

from skarabina import barber, dask_ms

logger = logging.getLogger(__name__)


@click.command("skarabina")
@click.option("--ms", required=True, help="Input measurement set")
@click.option("--msout", default=None, help="Output measurement set")
@click.option(
    "--summary", is_flag=True, default=False, help="Print the flagging summary"
)
@click.option(
    "--optimize",
    is_flag=True,
    default=False,
    help="Optimize measurement set size while keeping rows of equal length",
)
@click.option(
    "--time-average-factor",
    type=int,
    default=1,
    help="Combine every N consecutive rows by averaging UVW and visibility data",
)
@click.option(
    "--frequency-average-factor",
    type=int,
    default=1,
    help="Combine every N consecutive frequency channels by averaging",
)
@click.option(
    "--clobber", is_flag=True, default=False, help="Replace the output measurement set"
)
@click.option("--barber", is_flag=True, default=False, help="Perform barber flagging")
@click.option(
    "--barber-pol", type=int, default=None, help="Polarization selection for barber"
)
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Apply flags in-place (update the input MS)",
)
@click.option(
    "--flag-uv-above",
    type=float,
    default=None,
    help="Flag UVW above this limit (in meters)",
)
@click.option("--flag-nan", is_flag=True, default=False, help="Flag NaN visibilities")
@click.option(
    "--flag-clip-lo", type=float, default=None, help="Lower amplitude clip threshold"
)
@click.option(
    "--flag-clip-hi", type=float, default=None, help="Upper amplitude clip threshold"
)
@click.option(
    "--flag-spectral-window",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML file with spectral window flagging rules",
)
@click.option("--debug", is_flag=True, default=False, help="Switch on debugging output")
@click.option(
    "--field-of-view",
    type=float,
    default=1.0,
    show_default=True,
    help="Field-of-view half-width from phase centre (degrees)",
)
@click.version_option(
    version=get_version("skarabina"),
    prog_name="skarabina",
    message="%(prog)s %(version)s",
)
def main(**kw):
    print("Mupati (skarabina): The 1GC flagger")
    opts = SimpleNamespace(**kw)

    level = logging.DEBUG if opts.debug else logging.INFO
    logging.basicConfig(level=level)
    root = logging.getLogger()
    root.setLevel(level)

    # dask-ms emits noisy warnings/tracebacks for unpopulated MS columns
    # (MODEL_DATA shape guessing, FLAG_CATEGORY with no rows). These are
    # harmless — the columns exist in schema but were never written to.
    logging.getLogger("daskms").setLevel(logging.ERROR)

    if opts.debug:
        ts = datetime.datetime.now().timestamp()
        fh = logging.FileHandler(filename=f"skarabina.{ts}.log")
        fh.setLevel(level)
        root.addHandler(fh)
        root.debug(f"options: {vars(opts)}")

    ms = dask_ms.DaskMS(opts.ms)
    fov_deg = opts.field_of_view if opts.field_of_view is not None else 1.0
    ms._fov_rad = fov_deg * 3.14159265 / 180.0

    # --- Flagging operations (order-independent) ---

    if opts.flag_uv_above is not None:
        print(f"uv_above {opts.flag_uv_above} m")
        ms.flag_uv_above(opts.flag_uv_above)

    flag_data_operations = {}
    if opts.flag_nan is not None:
        flag_data_operations["NAN"] = True

    if opts.flag_clip_lo is not None and opts.flag_clip_hi is not None:
        flag_data_operations["CLIP"] = (opts.flag_clip_lo, opts.flag_clip_hi)
        print(f"flag_clip [{opts.flag_clip_lo}, {opts.flag_clip_hi}]")

    ms.flag_data(flag_data_operations)

    if opts.flag_spectral_window is not None:
        print(f"flag_spectral_window: {opts.flag_spectral_window}")
        ms.flag_spectral_window(opts.flag_spectral_window)

    # --- Row removal / averaging (MUST be last before writing) ---

    if opts.frequency_average_factor is not None and opts.frequency_average_factor > 1:
        ms.frequency_average(opts.frequency_average_factor)

    if opts.time_average_factor is not None and opts.time_average_factor > 1:
        ms.time_average(opts.time_average_factor)

    if opts.optimize:
        ms.optimize()

    # --- Read-only reports (after all processing) ---

    if opts.summary:
        ms.summary()

    if opts.barber:
        barber.barber(ms, opts.barber_pol)

    # --- Write output ---

    if opts.msout:
        ms.write_new_ms(opts.msout, opts.clobber)
    elif opts.apply:
        ms.update_ms(opts.ms, opts.clobber)
