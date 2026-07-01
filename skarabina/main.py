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

    level = logging.DEBUG if opts.debug else logging.ERROR
    logging.basicConfig(level=level)
    root = logging.getLogger()
    root.setLevel(level)

    if opts.debug:
        ts = datetime.datetime.now().timestamp()
        fh = logging.FileHandler(filename=f"skarabina.{ts}.log")
        fh.setLevel(level)
        root.addHandler(fh)
        root.debug(f"options: {dict(opts)}")

    ms = dask_ms.DaskMS(opts.ms)

    if opts.flag_uv_above is not None:
        # Set the flag Variable on first Dataset to it's inverse
        print(f"uv_above {opts.flag_uv_above}")
        ms.flag_uv_above(opts.flag_uv_above)

    flag_data_operations = {}
    if opts.flag_nan is not None:
        flag_data_operations["NAN"] = True

    if opts.flag_clip is not None:
        flag_data_operations["CLIP"] = opts.flag_clip
        print(f"flag_clip {opts.flag_clip}")

    ms.flag_data(flag_data_operations)

    if opts.summary:
        ms.summary()

    if opts.barber:
        barber.barber(ms, opts.barber_pol)

    if opts.optimize:
        ms.optimize()

    if opts.msout:
        ms.write_new_ms(opts.msout, opts.clobber)
    elif opts.apply:
        ms.update_ms(opts.ms, opts.clobber)
