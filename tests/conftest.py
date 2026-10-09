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

# Raise the file-descriptor soft limit to the hard limit for this session.
#
# The suite opens thousands of distinct table paths (every test builds its
# own MS), and the backend holds a `table.lock` fd per path for the life of
# the process; a full run's high-water is ~3 000 fds, which dies with
# "OSError: [Errno 24] Too many open files" at the default soft limit of
# 1024 (the hard limit is 1024*1024 on a typical host, so the raise is
# always available there).  Measured 2026-10-10; the per-path fd lifetime
# itself is a backend matter (see doc/CHANGES.md) -- a production run is
# one process over one MS and holds a bounded number either way.
import resource

try:
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if _soft < _hard:
        resource.setrlimit(resource.RLIMIT_NOFILE, (_hard, _hard))
except (OSError, ValueError):
    pass  # a host that caps the hard limit keeps today's behaviour

