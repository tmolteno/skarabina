# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Vectorised order statistics shared by the auto-flaggers.

Both flaggers take a median per row or per column of a plane, over and over;
done one lane at a time that is a numpy call per lane, and done with
``np.nanmedian`` it is a masked-array computation.  One sort per plane does the
same work, with exactly the same answer.
"""

import numpy as np


def nanmedian(values, axis=-1):
    """``np.nanmedian`` along ``axis``, from one sort.

    NaN sorts after every number, so after sorting the usable samples of each
    lane are its first ``n`` entries and the median is read off at ``(n-1)//2``
    and ``n//2`` -- the same two middle values, averaged the same way, as numpy's
    own median.  A lane with no usable sample comes out NaN, silently.

    This replaces ``np.nanmedian``, whose small-axis path goes through masked
    arrays: on a 10 000-row, 79-channel block it was 1.9 s of the 2.2 s the
    whole RFlag plane took, and it warns (not thread-safely) on every all-NaN
    lane, which on real, heavily flagged data is most of them.  It also stands
    in for a per-lane loop of ``np.median`` calls, which is how TFCrop spent
    most of its time.
    """
    ordered = np.moveaxis(np.sort(values, axis=axis), axis, -1)
    if ordered.shape[-1] == 0:
        return np.full(ordered.shape[:-1], np.nan)
    count = np.count_nonzero(~np.isnan(ordered), axis=-1)
    low = np.take_along_axis(
        ordered, np.maximum((count - 1) // 2, 0)[..., None], axis=-1
    )[..., 0]
    high = np.take_along_axis(ordered, (count // 2)[..., None], axis=-1)[..., 0]
    # Where the count is odd the two are the same sample, and (x + x) / 2 is x.
    return np.where(count > 0, (low + high) / 2, np.nan)
