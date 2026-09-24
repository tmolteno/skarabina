#!/usr/bin/env python3
"""Child process for the skarabina timing bench: run one CLI stage, or describe
a measurement set.

``daskms`` is imported *before* skarabina on purpose: with
``DASK_MS_BACKEND=casacure`` that import installs the ``casacore -> casacure``
alias, and skarabina's own ``from casacore.tables import table`` has to resolve
after it.  With the variable unset the real python-casacore is used.  This is
the same shim `.bench/run_skarabina.py` uses for the local runs; it lives here
so the bench directory is self-contained.

Usage:
    run_stage.py flag  <skarabina flag args...>
    run_stage.py analyze <skarabina-analyze args...>
    run_stage.py describe <ms>          # one JSON line of workload stats
"""

import json
import os
import sys

import daskms  # noqa: F401  (activates the casacure alias when asked)

import casacore

back = os.environ.get("DASK_MS_BACKEND") or (
    "casacure" if "casacure" in (casacore.__file__ or "") else "casacore"
)
print(f"BACKEND={back} ({casacore.__file__})", flush=True)

stage = sys.argv[1]
rest = sys.argv[2:]

if stage == "describe":
    from casacore.tables import table

    ms = rest[0]
    t = table(ms, readonly=True, ack=False)
    shape = t.getcell("DATA", 0).shape if t.nrows() else (0, 0)
    times = t.getcol("TIME") if t.nrows() else []
    fields = t.getcol("FIELD_ID") if t.nrows() else []
    nrows = t.nrows()
    t.close()
    size = 0
    for root, _dirs, files in os.walk(ms):
        for name in files:
            try:
                size += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    print("DESCRIBE=" + json.dumps({
        "ms": os.path.abspath(ms),
        "rows": nrows,
        "channels": int(shape[0]),
        "correlations": int(shape[1]) if len(shape) > 1 else 1,
        "integrations": int(len(set(times))),
        "fields": int(len(set(fields))),
        "size_bytes": size,
    }), flush=True)
    sys.exit(0)

if stage == "analyze":
    from skarabina.analyze import main as cmd
else:
    from skarabina.main import main as cmd

sys.argv = ["skarabina-" + stage] + rest
cmd()
