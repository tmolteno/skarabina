# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Helpers for tests that need a real (small) measurement set on disk.

Most tests build a synthetic ``DaskMS`` with ``__new__`` and never touch
casacore.  The write path (``write_new_ms``) cannot be tested that way: it
creates a real MS and rewrites sub-tables, so those tests need a genuine MS to
start from.  ``default_ms`` plus a little sub-table filling gives us one
without needing CASA.
"""
import os
import shutil

import numpy as np
from casacore.tables import complete_ms_desc, default_ms, maketabdesc, table

CHANNEL_WIDTH_HZ = 1.0e7
BASE_FREQUENCY_HZ = 1.0e9


def channel_frequencies(nchan):
    """Channel centre frequencies for the synthetic MS."""
    return BASE_FREQUENCY_HZ + np.arange(nchan, dtype=float) * CHANNEL_WIDTH_HZ


def _fill(path, sub, row, nrows=1):
    """Replace the contents of a sub-table with ``nrows`` rows.

    Columns the default description does not provide are skipped, so the
    fixture does not depend on a particular casacore version's MS flavour.
    """
    t = table(os.path.join(path, sub), readonly=False)
    if t.nrows() > 0:
        t.removerows(list(range(t.nrows())))
    t.addrows(nrows)
    columns = set(t.colnames())
    for col, val in row.items():
        if col in columns:
            t.putcol(col, val)
    t.close()


def make_synthetic_ms(path, nchan=4, nrow=4, ncorr=1, scan_numbers=None):
    """Create a minimal single-SPW MS at ``path`` and return the path."""
    path = str(path)
    shutil.rmtree(path, ignore_errors=True)

    main_desc = complete_ms_desc()
    tabdesc = maketabdesc(
        [{"name": k, "desc": v} for k, v in main_desc.items() if not k.startswith("_")]
    )
    default_ms(path, tabdesc)

    freqs = channel_frequencies(nchan)
    _fill(path, "SPECTRAL_WINDOW", {
        "NUM_CHAN": np.array([nchan], dtype=np.int32),
        "CHAN_FREQ": freqs.reshape(1, -1),
        "CHAN_WIDTH": np.full((1, nchan), CHANNEL_WIDTH_HZ),
        "RESOLUTION": np.full((1, nchan), CHANNEL_WIDTH_HZ),
        "EFFECTIVE_BW": np.full((1, nchan), CHANNEL_WIDTH_HZ),
        "TOTAL_BANDWIDTH": np.array([nchan * CHANNEL_WIDTH_HZ]),
        "REF_FREQUENCY": np.array([freqs.mean()]),
    })
    _fill(path, "DATA_DESCRIPTION", {
        "SPECTRAL_WINDOW_ID": np.array([0], dtype=np.int32),
        "POLARIZATION_ID": np.array([0], dtype=np.int32),
        "FLAG_ROW": np.array([False]),
    })
    _fill(path, "POLARIZATION", {
        "NUM_CORR": np.array([ncorr], dtype=np.int32),
        "CORR_TYPE": np.array([[9]], dtype=np.int32),
        "CORR_PRODUCT": np.zeros((1, 1, 2), dtype=np.int32),
    })
    _fill(path, "FIELD", {
        "NAME": np.array(["TEST"]),
        "NUM_POLY": np.array([0], dtype=np.int32),
        "PHASE_DIR": np.zeros((1, 1, 2)),
        "DELAY_DIR": np.zeros((1, 1, 2)),
        "REFERENCE_DIR": np.zeros((1, 1, 2)),
        "SOURCE_ID": np.array([0], dtype=np.int32),
        "TIME": np.array([0.0]),
    })
    _fill(path, "ANTENNA", {
        "NAME": np.array(["a0", "a1"]),
        "STATION": np.array(["s0", "s1"]),
        "TYPE": np.array(["GROUND-BASED"] * 2),
        "MOUNT": np.array(["ALT-AZ"] * 2),
        "POSITION": np.zeros((2, 3)),
        "OFFSET": np.zeros((2, 3)),
        "DISH_DIAMETER": np.array([13.5, 13.5]),
        "FLAG_ROW": np.array([False, False]),
    }, nrows=2)
    _fill(path, "OBSERVATION", {
        "TELESCOPE_NAME": np.array(["MEERKAT"]),
        "TIME_RANGE": np.zeros((1, 2)),
        "OBSERVER": np.array(["nobody"]),
        "LOG": np.array([[""]]),
        "SCHEDULE": np.array([[""]]),
        "PROJECT": np.array([""]),
        "RELEASE_DATE": np.array([0.0]),
        "FLAG_ROW": np.array([False]),
    })
    _fill(path, "PROCESSOR", {
        "TYPE": np.array(["CORRELATOR"]),
        "SUBTYPE": np.array([""]),
        "TYPE_ID": np.array([0], dtype=np.int32),
        "MODE_ID": np.array([0], dtype=np.int32),
        "FLAG_ROW": np.array([False]),
    })

    antenna1 = np.resize(np.array([0, 1], dtype=np.int32), nrow)
    antenna2 = np.resize(np.array([1, 0], dtype=np.int32), nrow)
    scans = (
        np.zeros(nrow, dtype=np.int32)
        if scan_numbers is None
        else np.asarray(scan_numbers, dtype=np.int32)
    )

    t = table(path, readonly=False)
    t.addrows(nrow)
    times = np.arange(nrow, dtype=float) * 10.0
    t.putcol("TIME", times)
    t.putcol("TIME_CENTROID", times)
    t.putcol("ANTENNA1", antenna1)
    t.putcol("ANTENNA2", antenna2)
    t.putcol("DATA_DESC_ID", np.zeros(nrow, dtype=np.int32))
    t.putcol("FIELD_ID", np.zeros(nrow, dtype=np.int32))
    t.putcol("SCAN_NUMBER", scans)
    t.putcol("INTERVAL", np.full(nrow, 10.0))
    t.putcol("EXPOSURE", np.full(nrow, 10.0))
    t.putcol("UVW", np.stack([np.arange(nrow) * 500.0, np.zeros(nrow), np.zeros(nrow)], axis=1))
    t.putcol("DATA", np.ones((nrow, nchan, ncorr), dtype=complex))
    t.putcol("FLAG", np.zeros((nrow, nchan, ncorr), dtype=bool))
    # FLAG_CATEGORY is left empty: casacore's hypercolumn shape for it is tied
    # to the number of categories, which is zero for a fresh MS.
    t.putcol("FLAG_ROW", np.zeros(nrow, dtype=bool))
    t.putcol("WEIGHT", np.ones((nrow, ncorr)))
    t.putcol("SIGMA", np.ones((nrow, ncorr)) * 0.1)
    t.putcol("WEIGHT_SPECTRUM", np.ones((nrow, nchan, ncorr)))
    t.putcol("SIGMA_SPECTRUM", np.ones((nrow, nchan, ncorr)) * 0.1)
    t.close()
    return path
