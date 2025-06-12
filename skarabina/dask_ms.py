import logging
import os
import shutil

import dask
from casacore.tables import table
import dask.array as da

from daskms import xds_to_table, xds_from_ms
from dask.diagnostics import ProgressBar

logger = logging.getLogger(__name__)


class DaskMS:
    def __init__(self, ms_name):
        self.name = ms_name
        print(f"Getting Data from MS file: {self.name}")

        if not os.path.exists(ms_name):
            raise RuntimeError(f"Measurement set {self.name} not found")

        ## Some casacore bits here
        t = table(self.name)
        self.sub_table_names = t.getsubtables()
        t.close()
        logger.info("Sub-table Names:")
        for s in self.sub_table_names:
            logger.info(f"    {s}")

        self.datasets = xds_from_ms(self.name)
        logger.info(self.datasets)

        self.ds = self.datasets[0]
        self.flag = self.ds.FLAG
        self.flag_row = da.asarray(self.ds.FLAG_ROW)
        print(f"FLAG_ROW = {self.ds.FLAG_ROW}")
        self.antenna1 = da.asarray(self.ds.ANTENNA1)
        self.antenna2 = da.asarray(self.ds.ANTENNA2)

        self.data = da.asarray(self.ds.DATA)

        self.uvw = da.array(self.ds.UVW)
        self.u_arr = self.uvw[:, 0].T
        self.v_arr = self.uvw[:, 1].T
        self.w_arr = self.uvw[:, 2].T
        self.time = da.asarray(self.ds.TIME)

        try:
            self.weight_spectrum = da.asarray(self.ds.WEIGHT_SPECTRUM)
        except:
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
        self.changed = []

    def flag_uv_above(self, uv_limit):
        '''
            uv_limit: Remove uv values below this threshold.
        '''
        print(f"flag_uv_above: {uv_limit}")
        abs_uv = self.u_arr*self.u_arr + self.v_arr*self.v_arr
        logger.info(f" flags: {self.flag.shape} dims: {self.flag.dims}")
        logger.info(f" row_flags: {self.flag_row.shape} dims: {self.ds.FLAG_ROW.dims}")

        uv_flag_mask = da.greater(abs_uv, uv_limit*uv_limit)
        new_flag_row = da.logical_or(uv_flag_mask, self.flag_row)
        print(f"abs_uv: {da.sqrt(da.max(abs_uv)).compute()}")
        print(f"uv_flag_mask: {da.sum(new_flag_row).compute()}")
        self.ds['FLAG_ROW'] = (self.ds.FLAG_ROW.dims, new_flag_row)

        self.changed.append('FLAG_ROW')

    def summary(self):
        num_flagged = da.sum(self.ds.FLAG)
        rows_flagged = da.sum(self.ds.FLAG_ROW)
        total = da.prod(da.array(self.ds.FLAG.shape))
        rows_total = da.prod(da.array(self.ds.FLAG_ROW.shape))
        percent = 100.0 * (num_flagged/total)
        rows_percent = 100.0 * (rows_flagged/rows_total)

        with ProgressBar():
            num_flagged, rows_flagged, \
                total, rows_total, percent, \
                rows_percent = dask.compute(num_flagged, rows_flagged,
                                            total, rows_total, percent,
                                            rows_percent)

        print(f"Flagging Summary ({self.name}): {percent} % - {num_flagged}/{total}.")
        print(f"    flags: {percent:4.2f} % - {num_flagged}/{total}.")
        print(f"    rows: {rows_percent:4.2f} % - {rows_flagged}/{rows_total}.")

    def write_new_ms(self, name, clobber):
        '''
            Write a new MS, and make sure it doesn't already exist
        '''
        all_tables = list(self.ds.keys())
        logger.info(f"Writing {all_tables} to {name}")

        if os.path.exists(name):
            if not clobber:
                raise RuntimeError(f"Measurement set {name} already exists. Use --clobber to overwrite")
            logger.warning(f"Overwriting {name}")
            shutil.rmtree(name)

        writes = xds_to_table(self.ds, name, "ALL")

        with ProgressBar():
            dask.compute(writes)

    def update_ms(self, name, clobber):
        '''
            Update MS in place
        '''
        if not clobber:
            raise RuntimeError(f"Measurement set {name} can't be changed. Use --clobber to overwrite")
        logger.warning(f"Updating {name}")

        for to_update in self.changed:

            print(f"Updating table: {to_update} in {name}")
            print(f"    ds={self.ds[to_update]}")
            writes = xds_to_table(self.ds, f"{name}", to_update)
            with ProgressBar():
                dask.compute(writes)

