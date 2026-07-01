# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import logging
import os
import shutil

import dask
import dask.array as da
import numpy as np
from casacore.tables import table
from dask.diagnostics import ProgressBar
from daskms import xds_from_ms, xds_to_table

logger = logging.getLogger(__name__)


class DaskMS:
    def __init__(self, ms_name):
        self.name = ms_name
        logger.info(f"Getting Data from MS file: {self.name}")

        if not os.path.exists(ms_name):
            raise RuntimeError(f"Measurement set {self.name} not found")

        # Some casacore bits here
        t = table(self.name)
        self.sub_table_names = t.getsubtables()
        t.close()
        logger.info("Sub-table Names:")
        for s in self.sub_table_names:
            logger.info(f"    {s}")

        self.datasets = xds_from_ms(self.name)
        logger.info(self.datasets)

        self.ds = self.datasets[0]
        self.flag = da.asarray(self.ds.FLAG)
        self.flag_row = da.asarray(self.ds.FLAG_ROW)
        logger.debug(f"FLAG_ROW = {self.ds.FLAG_ROW}")
        self.antenna1 = da.asarray(self.ds.ANTENNA1)
        self.antenna2 = da.asarray(self.ds.ANTENNA2)

        self.data = da.asarray(self.ds.DATA)

        self.uvw = da.asarray(self.ds.UVW)
        self.u_arr = self.uvw[:, 0].T
        self.v_arr = self.uvw[:, 1].T
        self.w_arr = self.uvw[:, 2].T
        self.time = da.asarray(self.ds.TIME)

        try:
            self.weight_spectrum = da.asarray(self.ds.WEIGHT_SPECTRUM)
        except AttributeError:
            self.weight_spectrum = da.ones_like(self.data)

        # self.flag_mask = da.where(da.logical_not(self.flag), 1, 0)

        # Use casacore table.get_subtables

        # self.sub_tables = {}
        # for s in self.sub_table_names:
        #     try:
        #         self.sub_tables[s] = xds_from_table(s)
        #     except:
        #         print(f"Failed to open {s} as a subtable")
        #         pass
        self.changed = {}

    def flag_uv_above(self, uv_limit):
        """
        Flag rows where sqrt(u^2 + v^2) exceeds uv_limit.
        """
        logger.info("flag_uv_above: %.1f wavelengths", uv_limit)

        abs_uv = self.u_arr * self.u_arr + self.v_arr * self.v_arr
        uv_flag_mask = da.greater(abs_uv, uv_limit * uv_limit)
        new_flag_row = da.logical_or(uv_flag_mask, self.flag_row)

        n_old = da.sum(self.flag_row)
        n_new = da.sum(new_flag_row)
        n_uv = da.sum(uv_flag_mask)
        max_uv = da.sqrt(da.max(abs_uv))

        n_old_v, n_new_v, n_uv_v, max_uv_v = dask.compute(n_old, n_new, n_uv, max_uv)

        n_added = int(n_new_v) - int(n_old_v)
        logger.info("flag_uv_above: max UV distance = %.1f wavelengths", max_uv_v)
        logger.info(
            "flag_uv_above: %d rows above uv limit, %d newly flagged (total: %d)",
            int(n_uv_v),
            n_added,
            int(n_new_v),
        )

        self.ds["FLAG_ROW"] = (self.ds.FLAG_ROW.dims, new_flag_row)
        self.changed["FLAG_ROW"] = True

    def flag_data(self, operations=None):
        """
        flag_data: Flag all NAN visibilities.
        """
        if operations is None:
            operations = {}
        abs_vis = da.abs(self.data)
        update = False
        n_nan = 0
        n_clip = 0

        old_flags = self.flag
        if "NAN" in operations:
            nan_flag_mask = da.isnan(abs_vis)
            n_nan = da.sum(nan_flag_mask)
            nan_updated_flags = da.logical_or(nan_flag_mask, old_flags)
            update = True
        else:
            nan_updated_flags = old_flags

        if "CLIP" in operations:
            clip_min, clip_max = operations["CLIP"]
            min_flag_mask = da.less_equal(abs_vis, clip_min)
            max_flag_mask = da.greater_equal(abs_vis, clip_max)
            clip_flag_mask = da.logical_or(min_flag_mask, max_flag_mask)
            n_clip = da.sum(clip_flag_mask)
            clip_updated_flags = da.logical_or(clip_flag_mask, nan_updated_flags)
            update = True
        else:
            clip_updated_flags = nan_updated_flags

        if update:
            self.ds["FLAG"].data = clip_updated_flags
            self.changed["FLAG"] = True
            total_vis = da.prod(da.array(self.ds.FLAG.shape))
            n_nan_v, n_clip_v, total_v = dask.compute(n_nan, n_clip, total_vis)
            if "NAN" in operations:
                logger.info(
                    "flag_data (NaN): flagged %d / %d visibilities (%.2f%%)",
                    int(n_nan_v),
                    int(total_v),
                    100.0 * int(n_nan_v) / int(total_v),
                )
            if "CLIP" in operations:
                logger.info(
                    "flag_data (clip [%s, %s]): flagged %d / %d visibilities (%.2f%%)",
                    clip_min,
                    clip_max,
                    int(n_clip_v),
                    int(total_v),
                    100.0 * int(n_clip_v) / int(total_v),
                )

    def summary(self):
        num_flagged = da.sum(self.ds.FLAG)
        rows_flagged = da.sum(self.ds.FLAG_ROW)
        total = da.prod(da.array(self.ds.FLAG.shape))
        rows_total = da.prod(da.array(self.ds.FLAG_ROW.shape))
        percent = 100.0 * (num_flagged / total)
        rows_percent = 100.0 * (rows_flagged / rows_total)

        abs_uv = da.sqrt(self.u_arr * self.u_arr + self.v_arr * self.v_arr)
        percentile_inputs = [25, 33, 50, 75, 95, 100]
        percentile_values = da.percentile(abs_uv.flatten(), percentile_inputs)

        with ProgressBar():
            (
                percentile_values,
                num_flagged,
                rows_flagged,
                total,
                rows_total,
                percent,
                rows_percent,
            ) = dask.compute(
                percentile_values,
                num_flagged,
                rows_flagged,
                total,
                rows_total,
                percent,
                rows_percent,
            )

        print(f"Flagging Summary ({self.name}): {percent} % - {num_flagged}/{total}.")
        print(f"    flags: {percent:4.2f} % - {num_flagged}/{total}.")
        print(f"    rows: {rows_percent:4.2f} % - {rows_flagged}/{rows_total}.")
        print(f"    max-uv: {percentile_values[-1]:4.2f}")
        print("    UV-Percentiles: ")
        for p, v in zip(percentile_inputs, percentile_values):
            print(f"        {p:6f}: \t{v:7.2f}")

    def optimize(self):
        """
        Run through the flags, and remove all completely flagged rows.

        A row is removed if either:
        - FLAG_ROW is True (explicitly marked as bad), or
        - Every individual visibility in FLAG is True (all channels × correlations flagged).

        All row-indexed columns are filtered consistently.
        """
        logger.info("Remove all flagged rows...")

        # Rows explicitly flagged via FLAG_ROW
        row_flagged = self.ds["FLAG_ROW"].data  # (nrow,)

        # Rows where every single visibility is individually flagged.
        # FLAG shape: (nrow, nchan, ncorr) → all over chan (axis=1) and corr (axis=2) → (nrow,)
        all_data_flagged = da.all(self.ds["FLAG"].data, axis=(1, 2))

        # Combined: a row is removed if EITHER condition is true
        is_flagged = da.logical_or(row_flagged, all_data_flagged)
        unflagged_rows = da.logical_not(is_flagged)

        # Compute all statistics in one pass
        n_overlap = da.sum(da.logical_and(row_flagged, all_data_flagged))
        (
            n_total,
            n_row_flagged,
            n_all_data_flagged,
            n_combined,
            n_unflagged,
            n_overlap,
        ) = dask.compute(
            row_flagged.size,
            da.sum(row_flagged),
            da.sum(all_data_flagged),
            da.sum(is_flagged),
            da.sum(unflagged_rows),
            n_overlap,
        )

        n_extra = int(n_all_data_flagged) - int(n_overlap)

        logger.info(f"Total rows:           {int(n_total):8d}")
        logger.info(f"  FLAG_ROW flagged:   {int(n_row_flagged):8d}")
        logger.info(f"  All-data-flagged:   {int(n_all_data_flagged):8d}")
        logger.info(f"  Combined to remove: {int(n_combined):8d}")
        logger.info(f"  Remaining:          {int(n_unflagged):8d}")
        logger.info(f"  Extra rows caught by all(FLAG) check: {n_extra}")

        if int(n_unflagged) == 0:
            raise RuntimeError(
                "No unflagged rows remain after optimize — nothing to write"
            )

        # Find the row dimension name from DATA (typically "row")
        row_dim = self.ds.DATA.dims[0]

        # Materialize the mask and convert to integer indices.
        # We use isel (integer indexing) rather than boolean indexing
        # because assigning variables one-by-one with boolean masks
        # causes xarray dimension conflicts: the first assigned variable
        # shrinks the row dimension, then subsequent variables fail.
        # isel filters all variables atomically in one operation.
        keep_mask = unflagged_rows.compute()
        keep_indices = np.nonzero(keep_mask)[0]

        self.ds = self.ds.isel({row_dim: keep_indices})

        # Mark all row-indexed variables as changed
        for var_name in self.ds.data_vars:
            if row_dim in self.ds[var_name].dims:
                self.changed[var_name] = True

        logger.info(f"Optimize complete. New row count: {int(n_unflagged)}")

    def write_new_ms(self, name, clobber):
        """
        Write a new MS, and make sure it doesn't already exist
        """
        all_tables = list(self.ds.keys())
        logger.info(f"Writing {all_tables} to {name}")

        if os.path.exists(name):
            if not clobber:
                raise RuntimeError(
                    f"Measurement set {name} already exists. Use --clobber to overwrite"
                )
            logger.warning(f"Overwriting {name}")
            shutil.rmtree(name)

        writes = xds_to_table(self.ds, name, "ALL")

        with ProgressBar():
            dask.compute(writes)

    def update_ms(self, name, clobber):
        """
        Update MS in place
        """
        if not clobber:
            raise RuntimeError(
                f"Measurement set {name} can't be changed. Use --clobber to overwrite"
            )
        logger.warning(f"Updating {name}")

        for to_update in self.changed.keys():
            if self.changed[to_update]:
                logger.info(f"Updating table: {to_update} in {name}")
                logger.debug(f"    ds={self.ds[to_update]}")
                writes = xds_to_table(self.ds, f"{name}", to_update)
                with ProgressBar():
                    dask.compute(writes)

                self.changed[to_update] = False
