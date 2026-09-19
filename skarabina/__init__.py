# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)

"""Skarabina (dungbeetle): an all-purpose 1GC flagger."""

import os
import sys


def _activate_casacure_backend() -> None:
    """Use casacure as dask-ms's I/O backend whenever it is installed.

    casacure is the pure-Rust drop-in replacement for casacore. When it is
    importable it is selected two ways:

    - for dask-ms (the tmolteno/dask-ms fork): set ``DASK_MS_BACKEND=casacure``,
    - for this package's own ``from casacore.tables import ...`` imports:
      alias ``sys.modules['casacore']`` -> casacure directly.

    Falls back silently to real python-casacore when casacure is absent.
    """
    try:
        import casacure
        import casacure.tables as tables
    except ImportError:
        return
    os.environ.setdefault("DASK_MS_BACKEND", "casacure")
    sys.modules.setdefault("casacore", casacure)
    sys.modules.setdefault("casacore.tables", tables)


_activate_casacure_backend()
