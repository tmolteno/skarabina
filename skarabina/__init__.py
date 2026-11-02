# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""skarabina: efficient, low-memory 1GC flagging of measurement sets.

skarabina runs on the **casacure** backend of **dask-ms-2** (the
`tmolteno/dask-ms <https://github.com/tmolteno/dask-ms>`_ fork, PyPI
``dask-ms-2``), permanently: importing the package pins
``DASK_MS_BACKEND=casacure``, overriding anything the environment had set.
python-casacore is not a skarabina backend — it does not work on arm64 (the
DGX Spark / pipeline hosts), casacure is a hard dependency of the package,
and the Rust backend is what every entry point (``skarabina``,
``skarabina-analyze``, ``skarabina-plotms``) and the test suite runs on.

The pin has to live here, at package import: dask-ms activates the
``casacore`` -> ``casacure`` alias when *it* is first imported, so the
variable must be set before any module of this package imports dask-ms —
and before a consumer (a test fixture, a script) imports ``casacore``
directly.
"""

import os

os.environ["DASK_MS_BACKEND"] = "casacure"
