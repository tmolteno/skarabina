# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""skarabina: efficient, low-memory 1GC flagging of measurement sets.

Importing the package defaults the table backend to **casacure**: with no
explicit ``DASK_MS_BACKEND``, ``casacure`` is selected.  python-casacore
does not work on arm64 (the DGX Spark / pipeline hosts), casacure is a hard
dependency of the package, and the Rust backend is what every entry point
(``skarabina``, ``skarabina-analyze``, ``skarabina-plotms``) and the test
suite should run on by default.  An explicit ``DASK_MS_BACKEND`` still wins,
so a deliberate python-casacore run (x86_64 only) keeps working.

The default has to live here, at package import: dask-ms activates the
``casacore`` -> ``casacure`` alias when *it* is first imported, so the
variable must be set before any module of this package imports dask-ms —
and before a consumer (a test fixture, a script) imports ``casacore``
directly.
"""

import os

if not os.environ.get("DASK_MS_BACKEND"):
    os.environ["DASK_MS_BACKEND"] = "casacure"
