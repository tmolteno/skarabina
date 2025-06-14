import click
from scabha.schema_utils import clickify_parameters
from omegaconf import OmegaConf
from importlib import resources

import datetime

import logging

from skarabina import recipes
from skarabina import dask_ms

recipe = resources.files(recipes) / "skarabina.yml"
schemas = OmegaConf.load(recipe)

logging.basicConfig()
logger = logging.getLogger()


@click.command("skarabina")
@clickify_parameters(schemas.cabs.get('skarabina'))
def main(**kw):
    print("Mupati (dungbeetle): The 1GC flagger")
    opts = OmegaConf.create(kw)

    if opts.debug:
        level = logging.DEBUG
    else:
        level = logging.ERROR

    logger.setLevel(level)

    if opts.debug:
        ts = datetime.datetime.now().timestamp()
        fh = logging.FileHandler(filename=f"skarabina.{ts}.log")
        fh.setLevel(level)

        logger.addHandler(fh)

    ms = dask_ms.DaskMS(opts.ms)

    if opts.uv_above is not None:
        # Set the flag Variable on first Dataset to it's inverse
        print(f"uv_above {opts.uv_above}")
        ms.flag_uv_above(opts.uv_above)

    if opts.summary:
        ms.summary()

    if opts.barber:
        barber.barber(ms, opts.barber_pol)

    if opts.msout:
        ms.write_new_ms(opts.msout, opts.clobber)
    elif opts.apply:
        ms.update_ms(opts.ms, opts.clobber)
