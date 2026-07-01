# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import datetime
import logging
from importlib import resources
from importlib.metadata import version as get_version

import click
from omegaconf import OmegaConf
from scabha.schema_utils import clickify_parameters

from skarabina import barber, dask_ms

recipe = resources.files("skarabina").joinpath("skarabina.yml")
schemas = OmegaConf.load(recipe)

logger = logging.getLogger(__name__)


@click.command("skarabina")
@clickify_parameters(schemas.cabs.get("skarabina"))
@click.version_option(
    version=get_version("skarabina"),
    prog_name="skarabina",
    message="%(prog)s %(version)s",
)
def main(**kw):
    print("Mupati (skarabina): The 1GC flagger")
    opts = OmegaConf.create(kw)

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
        root.debug(f"options: {dict(opts)}")

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

    if opts.flag_clip is not None:
        flag_data_operations["CLIP"] = opts.flag_clip
        print(f"flag_clip {opts.flag_clip}")

    ms.flag_data(flag_data_operations)

    if opts.flag_spectral_window is not None:
        print(f"flag_spectral_window: {opts.flag_spectral_window}")
        ms.flag_spectral_window(opts.flag_spectral_window)

    # --- Read-only operations ---

    if opts.summary:
        ms.summary()

    if opts.barber:
        barber.barber(ms, opts.barber_pol)

    # --- Row removal / averaging (MUST be last before writing) ---

    if opts.frequency_average_factor is not None and opts.frequency_average_factor > 1:
        ms.frequency_average(opts.frequency_average_factor)

    if opts.time_average_factor is not None and opts.time_average_factor > 1:
        ms.time_average(opts.time_average_factor)

    if opts.optimize:
        ms.optimize()

    # --- Write output ---

    if opts.msout:
        ms.write_new_ms(opts.msout, opts.clobber)
    elif opts.apply:
        ms.update_ms(opts.ms, opts.clobber)
