# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import datetime
import logging

import dask
import dask.array as da
from dask.diagnostics import ProgressBar

logger = logging.getLogger(__name__)


def barber(ms, pol):

    # Read all columns from the live dataset so the report reflects
    # any flagging/averaging/optimize that has run.  The ms.flag /
    # ms.data / ... attributes are __init__ snapshots that go stale
    # once the dataset is mutated.
    flag = ms.ds.FLAG.data
    data = ms.ds.DATA.data
    time = ms.ds.TIME.data
    antenna1 = ms.ds.ANTENNA1.data
    antenna2 = ms.ds.ANTENNA2.data
    weight_spectrum = ms.ds.WEIGHT_SPECTRUM.data

    unflagged = da.logical_not(flag)
    absvis = da.asarray(da.abs(data * unflagged))

    if pol is None:
        max_index = da.unravel_index(da.argmax(absvis, axis=None), shape=absvis.shape)
        pol_index = max_index[2]
    else:
        pol_index = pol
        max_index = da.unravel_index(
            da.argmax(absvis[:, :, pol_index], axis=None), shape=absvis.shape
        )

    max_vis = absvis[max_index]
    mean_vis = da.mean(absvis)
    percentile_inputs = [50, 95, 99, 99.99, 99.999]
    percentile_values = da.percentile(absvis.flatten(), percentile_inputs)
    # with dask.config.set(**{'array.slicing.split_large_chunks': True}):

    dump_index = max_index[0]
    channel_index = max_index[1]
    dt = time[dump_index]
    max_flag = flag[max_index]

    ant1 = antenna1[dump_index]
    ant2 = antenna2[dump_index]

    with ProgressBar():
        (
            max_vis,
            mean_vis,
            dump_index,
            channel_index,
            pol_index,
            dt,
            max_index,
            max_flag,
            percentile_values,
        ) = dask.compute(
            max_vis,
            mean_vis,
            dump_index,
            channel_index,
            pol_index,
            dt,
            max_index,
            max_flag,
            percentile_values,
        )

    ant1, ant2 = dask.compute(ant1, ant2)
    # ts = inverse[dump_index]
    #
    # # Convert from reduced Julian Date to timestamp.
    timestamp = datetime.datetime(
        1858, 11, 17, 0, 0, 0, tzinfo=datetime.timezone.utc
    ) + datetime.timedelta(seconds=float(dt))

    print(f"Max Vis Report ({absvis.shape})")
    print(f"    Mean |v| = {mean_vis}")
    # One scheduler pass for the two trailing per-index lookups.
    flag_at_max, weight_at_max = dask.compute(
        flag[max_index], weight_spectrum[max_index]
    )

    print(f"    Max |v| = {max_vis}")
    print(f"        at vis_index = {dump_index}")
    print(f"        at channel_index = {channel_index}")
    print(f"        at pol_index = {pol_index}")
    print(f"    flags[{max_index}] = {flag_at_max}")
    print(f"    weights[{max_index}] = {weight_at_max}")
    print(f"    Time = {timestamp}")

    print(f"    ANT1 = {ant1}")
    print(f"    ANT2 = {ant2}")

    print("    Percentiles: ")
    for p, v in zip(percentile_inputs, percentile_values):
        print(f"        {p:6.4f}: \t{v:4.2f}")
