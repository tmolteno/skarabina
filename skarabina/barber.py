import dask
import dask.array as da
import datetime
import logging

logger = logging.getLogger(__name__)


def barber(ms, pol):

    unflagged = da.logical_not(ms.flag)
    absvis = da.asarray(da.abs(ms.data*unflagged))

    if pol is None:
        max_index = da.unravel_index(da.argmax(absvis, axis=None),
                                     shape=absvis.shape)
        pol_index = max_index[2]
    else:
        pol_index = pol
        max_index = da.unravel_index(da.argmax(absvis[:,:,pol_index], axis=None), shape=absvis.shape)

    max_vis = absvis[max_index]
    mean_vis = da.mean(absvis)
    percentile_inputs = [50, 95, 99, 99.99, 99.999]
    percentile_values = da.percentile(absvis.flatten(), percentile_inputs)
    # with dask.config.set(**{'array.slicing.split_large_chunks': True}):

    dump_index = max_index[0]
    channel_index = max_index[1]
    dt = ms.time[dump_index]
    max_flag = ms.flag[max_index]

    ant1 = ms.antenna1[dump_index]
    ant2 = ms.antenna2[dump_index]

    max_vis, mean_vis, dump_index, channel_index, pol_index, \
        dt, max_index, max_flag, percentile_values = \
        dask.compute(max_vis, mean_vis, dump_index, channel_index,
                     pol_index, dt, max_index, max_flag, percentile_values)

    ant1, ant2 = dask.compute(ant1, ant2)
    # ts = inverse[dump_index]
    #
    # # Convert from reduced Julian Date to timestamp.
    timestamp = datetime.datetime(
            1858, 11, 17, 0, 0, 0, tzinfo=datetime.timezone.utc
    ) + datetime.timedelta(seconds=float(dt))

    print(f"Max Vis Report ({absvis.shape})")
    print(f"    Mean |v| = {mean_vis}")
    print(f"    Max |v| = {max_vis}")
    print(f"        at vis_index = {dump_index}")
    print(f"        at channel_index = {channel_index}")
    print(f"        at pol_index = {pol_index}")
    print(f"    flags[{max_index}] = {ms.flag[max_index].compute().to_numpy()}")
    print(f"    weights[{max_index}] = {ms.weight_spectrum[max_index].compute()}")
    print(f"    Time = {timestamp}")

    print(f"    ANT1 = {ant1}")
    print(f"    ANT2 = {ant2}")

    print("    Percentiles: ")
    for p, v in zip(percentile_inputs, percentile_values):
        print(f"        {p:6.4f}: \t{v:4.2f}")
    # print(f"    u = {u_arr[dump_index]}")
    # print(f"    v = {v_arr[dump_index]}")
    # print(f"    w = {w_arr[dump_index]}")

    if False:
        min_index = da.unravel_index(da.argmin(absvis, axis=None), shape=absvis.shape)
        print(f"min_index: {min_index}")
        dump_index = min_index[0]

        print("\n DEBUG: Min Vis Report")
        print(f"    Min |v| = {absvis[min_index].compute()}")
        print(f"    flags[{min_index}] = {ms.flag[min_index].compute()}")
        print(f"    ANT1 = {ms.antenna1[dump_index].compute()}")
        print(f"    ANT2 = {ms.antenna2[dump_index].compute()}")
        print(f"    u = {ms.u_arr[dump_index].compute()}")
        print(f"    v = {ms.v_arr[dump_index].compute()}")
        print(f"    w = {ms.w_arr[dump_index].compute()}")

    #
    # # Set the flag Variable on first Dataset to it's inverse
    # # ds[0]['flag'] = (ds[0].flag.dims, da.logical_not(ds[0].flag))
    # # Write the flag column back to the Measurement Set
    # # xds_to_table(ds, "WSRT.MS", "FLAG").compute()


