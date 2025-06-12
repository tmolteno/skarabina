import click
from scabha.schema_utils import clickify_parameters
from omegaconf import OmegaConf
from importlib import resources

import datetime

import logging

from . import recipes
from . import dask_ms
from . import skarabina

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

    skarabina.skarabina(ms, opts)

    if opts.msout:
        ms.write_new_ms(opts.msout, opts.clobber)
    elif opts.apply:
        ms.update_ms(opts.ms, opts.clobber)
