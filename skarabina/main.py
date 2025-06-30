import click
from scabha.schema_utils import clickify_parameters
from omegaconf import OmegaConf
from importlib import resources

import datetime

import logging

from skarabina import dask_ms
from skarabina import barber
recipe = resources.files('skarabina').joinpath('skarabina.yml')
schemas = OmegaConf.load(recipe)

logging.basicConfig()
logger = logging.getLogger()


@click.command("skarabina")
@clickify_parameters(schemas.cabs.get('skarabina'))
def main(**kw):
    print("Mupati (skarabina): The 1GC flagger")
    opts = OmegaConf.create(kw)
    print(opts.keys())
    print(kw)

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

    if opts.flag_uv_above is not None:
        # Set the flag Variable on first Dataset to it's inverse
        print(f"uv_above {opts.flag_uv_above}")
        ms.flag_uv_above(opts.flag_uv_above)

    flag_data_operations = {}
    if opts.flag_nan is not None:
        flag_data_operations['NAN'] = True

    if opts.flag_clip is not None:
        flag_data_operations['CLIP'] = opts.flag_clip
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

