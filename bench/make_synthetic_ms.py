#!/usr/bin/env python3
"""Write a synthetic MS shaped like bench's bpcal.ms, for hosts without it.

79 channels x 2 correlations, Gaussian noise about 10+0j, ~0.1 % of rows x30
(the "RFI"), and FLAG ~69 % set the way a calibrator scan arrives: 37 % of rows
wholly, the first 8 channels, and 45 % of the rest at random.  Built on
``tests/ms_fixture.make_synthetic_ms``; written in row blocks.

    python bench/make_synthetic_ms.py .bench/data/synth.ms 430000
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from ms_fixture import make_synthetic_ms  # noqa: E402
from casacore.tables import table  # noqa: E402

NCHAN, NCORR, STEP = 79, 2, 20000


def main(path, nrow):
    make_synthetic_ms(path, nchan=NCHAN, nrow=nrow, ncorr=NCORR)
    t = table(path, readonly=False, ack=False)
    rng = np.random.default_rng(0)
    shape = (NCHAN, NCORR)
    for start in range(0, nrow, STEP):
        n = min(STEP, nrow - start)
        data = 10 + rng.normal(0, 1, (n,) + shape) + 1j * rng.normal(0, 1, (n,) + shape)
        data[rng.random(n) < 0.001] *= 30
        flag = rng.random((n,) + shape) < 0.45
        flag[rng.random(n) < 0.37] = True
        flag[:, :8] = True
        t.putcol("DATA", data, startrow=start, nrow=n)
        t.putcol("FLAG", flag, startrow=start, nrow=n)
    t.close()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
