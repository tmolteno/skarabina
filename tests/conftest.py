# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The suite runs on casacure, in every import order.

Importing ``skarabina`` installs the backend default
(``DASK_MS_BACKEND=casacure``), and importing ``daskms`` activates the
``casacore`` -> ``casacure`` alias right here — at session start, before any
test module imports ``casacore`` directly (the fixtures do, some before they
touch skarabina at all).  python-casacore is not used (it does not work on
arm64) and need not be installed.
"""

import skarabina  # noqa: F401
import daskms  # noqa: F401
