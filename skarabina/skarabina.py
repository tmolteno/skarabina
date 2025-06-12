import logging
import dask.array as da

from . import barber


logger = logging.getLogger(__name__)


def skarabina(ms, opts):

    if opts.uv_above is not None:
        # Set the flag Variable on first Dataset to it's inverse
        print(f"uv_above {opts.uv_above}")
        ms.flag_uv_above(opts.uv_above)

    if opts.summary:
        ms.summary()

    if opts.barber:
        barber.barber(ms, opts.barber_pol)
