# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""A synthetic CASA caltable for tests: gain-table shaped, no CASA needed.

The table mimics what ``gaincal``/``bandpass`` write (``CPARAM``/``FLAG``/
``SNR``/``WEIGHT`` with the ANTENNA/SPECTRAL_WINDOW/POLARIZATION/FIELD
subtables), with deterministic values so a plot test can assert what was
collected:

* ``amp == 1.0 + row`` (the row index),
* ``phase == 10 * (corr + chan)`` degrees,
* rows ``[:flagged]`` flagged in full.
"""
import os
import shutil

import numpy as np
from casacore.tables import (
    default_ms_subtable,
    makearrcoldesc,
    maketabdesc,
    makescacoldesc,
    table,
)

from ms_fixture import CHANNEL_WIDTH_HZ, _fill, BASE_FREQUENCY_HZ

# The phase pattern: degrees = PHASE_STEP * (corr_index + channel).
PHASE_STEP_DEG = 10.0

SUBTABLES = ("ANTENNA", "SPECTRAL_WINDOW", "POLARIZATION", "FIELD")


def make_synthetic_caltable(path, nrow=8, nchan=4, ncorr=2, nant=4,
                            flagged=0, field_names=("BPCAL",)):
    """Create a caltable at ``path`` and return the path.

    ``flagged`` rows are flagged in their entirety; the remaining rows are
    unflagged.  ``ncorr`` polarizations sit on one spectral window of
    ``nchan`` channels and ``nant`` antennas (ANTENNA1 cycles, ANTENNA2 is
    -1, as CASA writes for single-antenna gain solutions).
    """
    path = str(path)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)

    columns = [
        makescacoldesc("TIME", 0.0, keywords={"UNIT": "s"}),
        makescacoldesc("INTERVAL", 0.0),
        makescacoldesc("FIELD_ID", 0),
        makescacoldesc("SPECTRAL_WINDOW_ID", 0),
        makescacoldesc("ANTENNA1", 0),
        makescacoldesc("ANTENNA2", 0),
        makearrcoldesc("CPARAM", 0j, ndim=2, shape=[ncorr, nchan]),
        makearrcoldesc("FLAG", False, ndim=2, shape=[ncorr, nchan]),
        makearrcoldesc("SNR", 0.0, ndim=1, shape=[ncorr]),
        makearrcoldesc("WEIGHT", 0.0, ndim=1, shape=[ncorr]),
    ]
    tab = table(path, maketabdesc(columns), nrow=nrow)

    phase = PHASE_STEP_DEG * (
        np.arange(ncorr)[:, None] + np.arange(nchan)[None, :]
    )
    amp = (1.0 + np.arange(nrow))[:, None, None]
    cparam = (amp * np.exp(1j * np.radians(phase))[None, :, :]).astype(np.complex128)
    tab.putcol("TIME", 58000.0 * 86400.0 + np.arange(nrow) * 10.0)
    tab.putcol("INTERVAL", np.full(nrow, 10.0))
    tab.putcol("FIELD_ID", np.zeros(nrow, dtype=np.int32))
    tab.putcol("SPECTRAL_WINDOW_ID", np.zeros(nrow, dtype=np.int32))
    tab.putcol("ANTENNA1", (np.arange(nrow, dtype=np.int32)) % nant)
    tab.putcol("ANTENNA2", np.full(nrow, -1, dtype=np.int32))
    tab.putcol("CPARAM", cparam)
    flag = np.zeros((nrow, ncorr, nchan), dtype=bool)
    flag[:flagged] = True
    tab.putcol("FLAG", flag)
    tab.putcol("SNR", np.full((nrow, ncorr), 5.0, dtype=np.float32))
    tab.putcol("WEIGHT", np.ones((nrow, ncorr), dtype=np.float32))
    tab.close()

    # Subtables, as a real caltable carries them.
    for name in SUBTABLES:
        default_ms_subtable(name, os.path.join(path, name))
    _fill(path, "ANTENNA", {
        "NAME": np.array([f"m{i:03d}" for i in range(nant)]),
        "STATION": np.array([f"s{i}" for i in range(nant)]),
        "POSITION": np.zeros((nant, 3)),
        "FLAG_ROW": np.zeros(nant, dtype=bool),
    }, nrows=nant)
    freqs = BASE_FREQUENCY_HZ + np.arange(nchan) * CHANNEL_WIDTH_HZ
    _fill(path, "SPECTRAL_WINDOW", {
        "NUM_CHAN": np.array([nchan], dtype=np.int32),
        "CHAN_FREQ": freqs.reshape(1, -1),
        "CHAN_WIDTH": np.full((1, nchan), CHANNEL_WIDTH_HZ),
        "REF_FREQUENCY": np.array([freqs.mean()]),
        "TOTAL_BANDWIDTH": np.array([nchan * CHANNEL_WIDTH_HZ]),
    }, nrows=1)
    corr_types = {1: [9], 2: [9, 12], 4: [9, 10, 11, 12]}[ncorr]
    _fill(path, "POLARIZATION", {
        "CORR_TYPE": np.array([corr_types], dtype=np.int32),
        "NUM_CORR": np.array([ncorr], dtype=np.int32),
    }, nrows=1)
    _fill(path, "FIELD", {
        "NAME": np.array(list(field_names)),
        "NUM_POLY": np.zeros(len(field_names), dtype=np.int32),
        "PHASE_DIR": np.zeros((len(field_names), 1, 2)),
        "DELAY_DIR": np.zeros((len(field_names), 1, 2)),
        "REFERENCE_DIR": np.zeros((len(field_names), 1, 2)),
    }, nrows=len(field_names))

    tab = table(path, readonly=False, ack=False)
    for name in SUBTABLES:
        tab.putkeyword(name, f"Table: {os.path.abspath(os.path.join(path, name))}")
    tab.close()
    return path
