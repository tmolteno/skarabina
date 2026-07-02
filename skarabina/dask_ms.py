# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import logging
import os
import shutil
import sys
from contextlib import contextmanager

import dask
import dask.array as da
import numpy as np
import yaml
from casacore.tables import table
from dask.diagnostics import ProgressBar
from daskms import xds_from_ms, xds_to_table

logger = logging.getLogger(__name__)


@contextmanager
def _maybe_quiet_stderr():
    """Suppress stderr unless root logger is at DEBUG level."""
    if logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
        yield
    else:
        with open(os.devnull, "w") as devnull:
            old_stderr = sys.stderr
            sys.stderr = devnull
            try:
                yield
            finally:
                sys.stderr = old_stderr


class DaskMS:
    def __init__(self, ms_name):
        self.name = ms_name
        print(f"Getting Data from MS file: {self.name}")

        if not os.path.exists(ms_name):
            raise RuntimeError(f"Measurement set {self.name} not found")

        # Some casacore bits here
        t = table(self.name)
        self.sub_table_names = t.getsubtables()
        t.close()
        logger.debug("Sub-table Names:")
        for s in self.sub_table_names:
            logger.debug(f"    {s}")

        self.datasets = xds_from_ms(self.name)
        logger.debug(self.datasets)

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

        # Load channel frequencies from SPECTRAL_WINDOW subtable (Hz)
        self.chan_freq_hz = None
        self.nspw = 0
        for s in self.sub_table_names:
            if "SPECTRAL_WINDOW" in s:
                try:
                    sw = table(s, ack=False)
                    chan_freq = sw.getcol("CHAN_FREQ")
                    self.nspw = chan_freq.shape[0]
                    self.chan_freq_hz = chan_freq[0]
                    sw.close()
                    # If the subtable has more channels than the actual
                    # data (e.g. from a pre-fix frequency-averaged MS),
                    # truncate to match.
                    nchan_ds = self.ds.FLAG.shape[1]
                    if len(self.chan_freq_hz) != nchan_ds:
                        logger.warning(
                            "CHAN_FREQ has %d entries but data has %d"
                            " channels — truncating",
                            len(self.chan_freq_hz),
                            nchan_ds,
                        )
                        self.chan_freq_hz = self.chan_freq_hz[:nchan_ds]
                except Exception:
                    logger.warning("Could not read CHAN_FREQ from %s", s)

    def flag_uv_above(self, uv_limit):
        """
        Flag rows where sqrt(u^2 + v^2) exceeds uv_limit (in meters).
        """
        print("flag_uv_above: %.1f m" % uv_limit)

        abs_uv = self.u_arr * self.u_arr + self.v_arr * self.v_arr
        uv_flag_mask = da.greater(abs_uv, uv_limit * uv_limit)
        new_flag_row = da.logical_or(uv_flag_mask, self.flag_row)

        n_old = da.sum(self.flag_row)
        n_new = da.sum(new_flag_row)
        n_uv = da.sum(uv_flag_mask)
        max_uv = da.sqrt(da.max(abs_uv))

        n_old_v, n_new_v, n_uv_v, max_uv_v = dask.compute(n_old, n_new, n_uv, max_uv)

        n_added = int(n_new_v) - int(n_old_v)
        print("flag_uv_above: max UV distance = %.1f m" % max_uv_v)
        print(
            "flag_uv_above: %d rows above uv limit, %d newly flagged (total: %d)"
            % (int(n_uv_v), n_added, int(n_new_v))
        )

        self.ds["FLAG_ROW"] = (self.ds.FLAG_ROW.dims, new_flag_row)
        self.changed["FLAG_ROW"] = True

    def flag_spectral_window(self, yaml_file):
        """
        Flag spectral windows from a YAML configuration file.

        YAML format — a list of entries, each with:
          spw: [[fmin_MHz, fmax_MHz], ...]   # frequency ranges to flag
          uv_below: <meters>                  # optional: only flag rows with UV < this
          uv_above: <meters>                  # optional: only flag rows with UV > this
        """
        if self.chan_freq_hz is None:
            raise RuntimeError(
                "No SPECTRAL_WINDOW/CHAN_FREQ found in MS — cannot flag by frequency"
            )

        with open(yaml_file) as f:
            entries = yaml.safe_load(f)

        if not isinstance(entries, list):
            raise RuntimeError("Spectral window YAML must be a list of entries")

        nchan = len(self.chan_freq_hz)
        uv_dist = da.sqrt(self.u_arr * self.u_arr + self.v_arr * self.v_arr)
        old_flags = self.flag
        new_flags = old_flags

        for idx, entry in enumerate(entries):
            spw_ranges = entry.get("spw", [])
            uv_below = entry.get("uv_below")
            uv_above = entry.get("uv_above")

            # Build channel mask: True where frequency falls in any range
            chan_mask = da.zeros(nchan, dtype=bool)
            for fmin_mhz, fmax_mhz in spw_ranges:
                fmin_hz = float(fmin_mhz) * 1e6
                fmax_hz = float(fmax_mhz) * 1e6
                chan_mask = da.logical_or(
                    chan_mask,
                    (self.chan_freq_hz >= fmin_hz) & (self.chan_freq_hz <= fmax_hz),
                )

            n_chan_flagged = da.sum(chan_mask)

            # Broadcast channel mask to (nrow, nchan, ncorr)
            # FLAG shape: (nrow, nchan, ncorr) — mask on axis=1
            spw_flag = da.broadcast_to(
                chan_mask[np.newaxis, :, np.newaxis],
                self.ds.FLAG.shape,
            )

            # Apply UV constraint if specified
            if uv_below is not None:
                uv_mask = uv_dist < float(uv_below)
                spw_flag = da.logical_and(spw_flag, uv_mask[:, np.newaxis, np.newaxis])
            if uv_above is not None:
                uv_mask = uv_dist > float(uv_above)
                spw_flag = da.logical_and(spw_flag, uv_mask[:, np.newaxis, np.newaxis])

            n_flagged = da.sum(spw_flag)
            n_chan_flagged_v, n_flagged_v = dask.compute(n_chan_flagged, n_flagged)

            uv_info = ""
            if uv_below is not None:
                uv_info += f", UV < {uv_below} m"
            if uv_above is not None:
                uv_info += f", UV > {uv_above} m"

            print(
                "flag_spectral_window[%d]: %d channels in %d range(s),"
                " flagged %d visibilities%s"
                % (
                    idx,
                    int(n_chan_flagged_v),
                    len(spw_ranges),
                    int(n_flagged_v),
                    uv_info,
                )
            )

            new_flags = da.logical_or(new_flags, spw_flag)

        self.ds["FLAG"].data = new_flags
        self.changed["FLAG"] = True

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
                print(
                    "flag_data (NaN): flagged %d / %d visibilities (%.2f%%)"
                    % (int(n_nan_v), int(total_v), 100.0 * int(n_nan_v) / int(total_v))
                )
            if "CLIP" in operations:
                print(
                    "flag_data (clip [%s, %s]): flagged %d / %d visibilities (%.2f%%)"
                    % (
                        clip_min,
                        clip_max,
                        int(n_clip_v),
                        int(total_v),
                        100.0 * int(n_clip_v) / int(total_v),
                    )
                )

    def summary(self):
        num_flagged = da.sum(self.ds.FLAG)
        rows_flagged = da.sum(self.ds.FLAG_ROW)
        total = da.prod(da.array(self.ds.FLAG.shape))
        rows_total = da.prod(da.array(self.ds.FLAG_ROW.shape))
        percent = 100.0 * (num_flagged / total)
        rows_percent = 100.0 * (rows_flagged / rows_total)

        # Histogram of unflagged visibilities per row
        n_unflagged = da.sum(da.logical_not(self.ds.FLAG.data), axis=(1, 2))
        n_flagged_per_row = da.sum(self.ds.FLAG.data, axis=(1, 2))
        max_per_row = da.prod(da.array(self.ds.FLAG.shape[1:]))
        frac = n_unflagged / max_per_row
        bins = [
            da.sum(frac == 0.0),
            da.sum((frac > 0.0) & (frac <= 0.25)),
            da.sum((frac > 0.25) & (frac <= 0.50)),
            da.sum((frac > 0.50) & (frac <= 0.75)),
            da.sum((frac > 0.75) & (frac < 1.0)),
            da.sum(frac == 1.0),
        ]
        min_unflagged = da.min(n_unflagged)
        max_unflagged = da.max(n_unflagged)
        min_flagged = da.min(n_flagged_per_row)
        max_flagged = da.max(n_flagged_per_row)
        total_per_row = n_unflagged + n_flagged_per_row
        min_total_per_row = da.min(total_per_row)
        max_total_per_row = da.max(total_per_row)

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
                bins,
                min_unflagged,
                max_unflagged,
                min_flagged,
                max_flagged,
                max_per_row,
                min_total_per_row,
                max_total_per_row,
            ) = dask.compute(
                percentile_values,
                num_flagged,
                rows_flagged,
                total,
                rows_total,
                percent,
                rows_percent,
                bins,
                min_unflagged,
                max_unflagged,
                min_flagged,
                max_flagged,
                max_per_row,
                min_total_per_row,
                max_total_per_row,
            )

        print(f"Flagging Summary ({self.name}): {percent} % - {num_flagged}/{total}.")
        print(f"    flags: {percent:4.2f} % - {num_flagged}/{total}.")
        print(f"    rows: {rows_percent:4.2f} % - {rows_flagged}/{rows_total}.")
        print(f"    max-uv: {percentile_values[-1]:4.2f}")
        print("    UV-Percentiles: ")
        for p, v in zip(percentile_inputs, percentile_values):
            print(f"        {p:6f}: \t{v:7.2f}")
        print("    Row flagging histogram (% of visibilities unflagged):")
        labels = ["   0%", " 1-25%", "26-50%", "51-75%", "76-99%", "  100%"]
        for label, count in zip(labels, bins):
            pct = 100.0 * int(count) / int(rows_total) if int(rows_total) > 0 else 0.0
            bar = "#" * max(1, int(pct / 2))
            print(f"        {label}: {int(count):8d} ({pct:5.1f}%) {bar}")
        print(
            f"    Visibilities per row: {int(max_per_row)} total"
            f" (unflagged: min={int(min_unflagged)}, max={int(max_unflagged)};"
            f" flagged: min={int(min_flagged)}, max={int(max_flagged)})",
        )
        if int(min_total_per_row) == int(max_total_per_row):
            print(
                f"    Row size check: all rows consistent"
                f" ({int(min_total_per_row)} elements each)"
            )
        else:
            print(
                f"    Row size check: INCONSISTENT —"
                f" min={int(min_total_per_row)}, max={int(max_total_per_row)}"
            )
        if self.chan_freq_hz is not None:
            nchan = len(self.chan_freq_hz)
            fmin = self.chan_freq_hz[0] / 1e6
            fmax = self.chan_freq_hz[-1] / 1e6
            bw = fmax - fmin
            print(
                f"    Spectral windows: {self.nspw}"
                f" (channels: {nchan},"
                f" {fmin:.3f}–{fmax:.3f} MHz,"
                f" bandwidth: {bw:.1f} MHz)"
            )

            # Fringe-rotation integration time limit (Wijnholds 2018, MNRAS).
            # Time averaging causes decorrelation that depends on baseline
            # length, frequency, and angular distance ℓ from the phase center.
            # The amplitude loss factor is:
            #
            #   ρ = sinc(π · ω_⊕ · Δt · B · ν · ℓ / c)
            #
            # For small loss L = 1 − |ρ|:
            #
            #   Δt_max = c · √(6L) / (π · ω_⊕ · B_max · ν_max · ℓ)
            #
            c_ms = 299792458.0
            omega_earth = 7.2921150e-5
            max_uv = percentile_values[-1]
            nu_max = fmax * 1e6

            # Distance from phase centre in radians (converted from
            # --field-of-view degrees).  Default ℓ ≈ 0.0175 rad (1°).
            ell = getattr(self, "_fov_rad", 0.0174533)

            def dt_max(loss):
                if max_uv <= 0 or nu_max <= 0 or ell <= 0:
                    return float("inf")
                return (
                    c_ms
                    * (6.0 * loss) ** 0.5
                    / (3.14159 * omega_earth * max_uv * nu_max * ell)
                )

            print("    Max integration time (fringe-rotation lim., ℓ=%.2f rad):" % ell)
            print("        1%% loss:  %5.1f s" % dt_max(0.01))
            print("        3%% loss:  %5.1f s" % dt_max(0.03))
            print("        5%% loss:  %5.1f s" % dt_max(0.05))

            if "INTERVAL" in self.ds.data_vars:
                dt_current = float(self.ds.INTERVAL.data[0].compute())
                print("    Current integration time: %.1f s" % dt_current)
            elif "EXPOSURE" in self.ds.data_vars:
                dt_current = float(self.ds.EXPOSURE.data[0].compute())
                print("    Current integration time: %.1f s" % dt_current)

        # Field listing
        print("    Fields:")
        field_names = {}
        for s in self.sub_table_names:
            if s.endswith("/FIELD"):
                try:
                    ft = table(s, ack=False)
                    names = ft.getcol("NAME")
                    ft.close()
                    for i, name in enumerate(names):
                        field_names[i] = name.strip()
                except Exception:
                    pass

        # FIELD_ID may be a data variable (multi-field MS) or an
        # attribute (single-field MS).
        if "FIELD_ID" in self.ds.data_vars:
            field_ids = self.ds.FIELD_ID.data
            unique_ids = da.unique(field_ids).compute()
        else:
            unique_ids = [int(self.ds.attrs.get("FIELD_ID", 0))]

        for fid in sorted(unique_ids):
            if "FIELD_ID" in self.ds.data_vars:
                n = int(da.sum(field_ids == fid).compute())
            else:
                n = int(self.ds.FLAG.shape[0])
            name = field_names.get(int(fid), f"FIELD_ID={fid}")
            print(f"        {fid}: {name:20s} {n:8d} rows")

    def time_average(self, factor):
        """
        Average every <factor> consecutive rows into a single row.

        DATA and WEIGHT_SPECTRUM average only unflagged visibilities
        (flagged entries are excluded from the mean).  UVW, TIME, and
        INTERVAL are simple averages (per-row metadata).  FLAG and
        FLAG_ROW are OR'd (any flagged → flagged).
        """
        if factor < 2:
            return

        nrow = self.ds.FLAG.shape[0]
        n_new = nrow // factor
        trim = n_new * factor

        print(
            "Time-averaging: factor %d"
            " → %d rows (discarding %d trailing rows)" % (factor, n_new, nrow - trim)
        )

        row_dim = self.ds.DATA.dims[0]
        shape_3d = (n_new, factor, self.ds.FLAG.shape[1], self.ds.FLAG.shape[2])
        shape_uvw = (n_new, factor, 3)
        shape_1d = (n_new, factor)

        def _reshape(arr, shape):
            return arr[:trim].reshape(shape)

        # Step 1: compute averaged arrays from the ORIGINAL data.
        # We do this before isel because reshaping needs n_new*factor rows.
        averaged = {}
        if "DATA" in self.ds.data_vars:
            d = _reshape(self.ds["DATA"].data, shape_3d)
            f = _reshape(self.ds["FLAG"].data, shape_3d)
            d_masked = da.where(f, 0j, d)
            n_unflagged = da.sum(da.logical_not(f), axis=1)
            n_safe = da.where(n_unflagged == 0, 1, n_unflagged)
            averaged["DATA"] = da.sum(d_masked, axis=1) / n_safe

        if "WEIGHT_SPECTRUM" in self.ds.data_vars:
            w = _reshape(self.ds["WEIGHT_SPECTRUM"].data, shape_3d)
            f = _reshape(self.ds["FLAG"].data, shape_3d)
            w_masked = da.where(f, 0, w)
            n_unflagged = da.sum(da.logical_not(f), axis=1)
            n_safe = da.where(n_unflagged == 0, 1, n_unflagged)
            averaged["WEIGHT_SPECTRUM"] = da.sum(w_masked, axis=1) / n_safe

        for col, s in [
            ("UVW", shape_uvw),
            ("TIME", shape_1d),
        ]:
            if col in self.ds.data_vars:
                averaged[col] = da.mean(_reshape(self.ds[col].data, s), axis=1)

        # INTERVAL and EXPOSURE are summed: combining N integrations
        # multiplies the integration time by N.
        for col, s in [
            ("INTERVAL", shape_1d),
            ("EXPOSURE", shape_1d),
        ]:
            if col in self.ds.data_vars:
                averaged[col] = da.sum(_reshape(self.ds[col].data, s), axis=1)

        for col, s in [
            ("FLAG", shape_3d),
            ("FLAG_ROW", shape_1d),
        ]:
            if col in self.ds.data_vars:
                averaged[col] = da.any(_reshape(self.ds[col].data, s), axis=1)

        for col, s in [
            ("ANTENNA1", shape_1d),
            ("ANTENNA2", shape_1d),
        ]:
            if col in self.ds.data_vars:
                averaged[col] = _reshape(self.ds[col].data, s)[:, 0]

        # Step 2: subsample the dataset to keep every <factor>-th row.
        # isel gives consistent dimensions and chunking (no conflicts).
        keep_idx = np.arange(0, trim, factor)
        self.ds = self.ds.isel({row_dim: keep_idx})

        # Step 3: replace the averaged columns.  Since isel already
        # reduced all variables to n_new rows, per-column assignment
        # against the same row count is safe.
        # Rechunk to match the existing row chunking from isel.
        row_chunks = self.ds.chunks.get(row_dim, None)
        for col, arr in averaged.items():
            if row_chunks is not None and len(arr.shape) >= 1:
                chunks = list(arr.chunks)
                chunks[0] = row_chunks
                arr = arr.rechunk(tuple(chunks))
            self.ds[col] = (self.ds[col].dims, arr)
            self.changed[col] = True

    def frequency_average(self, factor):
        """
        Average every <factor> consecutive frequency channels into one.

        DATA and WEIGHT_SPECTRUM average only unflagged visibilities.
        FLAG is OR'd (any flagged → flagged).
        Trailing channels (fewer than <factor>) are combined into a
        single narrower channel rather than discarded.
        """
        if factor < 2:
            return

        nrow = self.ds.FLAG.shape[0]
        nchan = self.ds.FLAG.shape[1]
        ncorr = self.ds.FLAG.shape[2]
        n_full = nchan // factor
        n_rem = nchan % factor
        n_new = n_full + (1 if n_rem > 0 else 0)
        trim = n_full * factor

        msg = "Frequency-averaging: factor %d → %d channels" % (factor, n_new)
        if n_rem > 0:
            msg += " (last channel from %d trailing)" % n_rem
        print(msg)

        chan_dim = self.ds.DATA.dims[1]
        shape_full = (nrow, n_full, factor, ncorr)

        # --- Compute averaged arrays ---
        averaged = {}

        for col in ["DATA", "WEIGHT_SPECTRUM"]:
            if col not in self.ds.data_vars:
                continue
            d = self.ds[col].data[:, :trim, :].reshape(shape_full)
            f = self.ds["FLAG"].data[:, :trim, :].reshape(shape_full)
            z = 0j if d.dtype.kind == "c" else 0
            d_masked = da.where(f, z, d)
            n_unf = da.sum(da.logical_not(f), axis=2)
            n_safe = da.where(n_unf == 0, 1, n_unf)
            avg = da.sum(d_masked, axis=2) / n_safe
            if n_rem > 0:
                d_rem = self.ds[col].data[:, trim:, :]
                f_rem = self.ds["FLAG"].data[:, trim:, :]
                z = 0j if d_rem.dtype.kind == "c" else 0
                d_rem_m = da.where(f_rem, z, d_rem)
                n_unf_r = da.sum(da.logical_not(f_rem), axis=1)
                n_safe_r = da.where(n_unf_r == 0, 1, n_unf_r)
                avg_rem = (da.sum(d_rem_m, axis=1) / n_safe_r)[:, None, :]
                avg = da.concatenate([avg, avg_rem], axis=1)
            averaged[col] = avg

        if "FLAG" in self.ds.data_vars:
            f = self.ds["FLAG"].data[:, :trim, :].reshape(shape_full)
            avg = da.any(f, axis=2)
            if n_rem > 0:
                f_rem = self.ds["FLAG"].data[:, trim:, :]
                avg_rem = da.any(f_rem, axis=1, keepdims=True)
                avg = da.concatenate([avg, avg_rem], axis=1)
            averaged["FLAG"] = avg

        if "SIGMA_SPECTRUM" in self.ds.data_vars:
            s = self.ds["SIGMA_SPECTRUM"].data[:, :trim, :].reshape(shape_full)
            avg = da.mean(s, axis=2)
            if n_rem > 0:
                s_rem = da.mean(
                    self.ds["SIGMA_SPECTRUM"].data[:, trim:, :],
                    axis=1,
                    keepdims=True,
                )
                avg = da.concatenate([avg, s_rem], axis=1)
            averaged["SIGMA_SPECTRUM"] = avg

        # --- Apply to dataset ---
        keep_idx = np.arange(0, trim, factor)
        if n_rem > 0:
            keep_idx = np.append(keep_idx, trim)  # one extra for tail
        self.ds = self.ds.isel({chan_dim: keep_idx})

        chan_chunks = self.ds.chunks.get(chan_dim, None)
        for col, arr in averaged.items():
            if chan_chunks is not None:
                new_chunks = list(arr.chunks)
                new_chunks[1] = chan_chunks
                arr = arr.rechunk(tuple(new_chunks))
            self.ds[col] = (self.ds[col].dims, arr)
            self.changed[col] = True

        # Update the SPECTRAL_WINDOW CHAN_FREQ to match
        if self.chan_freq_hz is not None:
            freq_reshaped = self.chan_freq_hz[:trim].reshape(n_full, factor)
            avg_freq = np.mean(freq_reshaped, axis=1)
            if n_rem > 0:
                avg_rem = np.mean(self.chan_freq_hz[trim:])
                avg_freq = np.append(avg_freq, avg_rem)
            self.chan_freq_hz = avg_freq

    def optimize(self):
        """
        Run through the flags, and remove all completely flagged rows
        and channels.

        A row is removed if either:
        - FLAG_ROW is True (explicitly marked as bad), or
        - Every individual visibility in FLAG is True (all channels ×
          correlations flagged).

        A channel is removed if all rows and all correlations are flagged
        for that channel (e.g. after flag_spectral_window).
        """
        print("Remove all flagged rows and channels...")

        # Rows explicitly flagged via FLAG_ROW
        row_flagged = self.ds["FLAG_ROW"].data  # (nrow,)

        # Rows where every single visibility is individually flagged.
        # FLAG shape: (nrow, nchan, ncorr) → all over chan (axis=1) and corr (axis=2) → (nrow,)
        all_data_flagged = da.all(self.ds["FLAG"].data, axis=(1, 2))

        # Channels where all rows and correlations are flagged.
        # FLAG shape: (nrow, nchan, ncorr) → all over row (axis=0) and corr (axis=2) → (nchan,)
        chan_fully_flagged = da.all(self.ds["FLAG"].data, axis=(0, 2))

        # Combined: a row is removed if EITHER condition is true
        is_flagged = da.logical_or(row_flagged, all_data_flagged)
        unflagged_rows = da.logical_not(is_flagged)
        keep_channels = da.logical_not(chan_fully_flagged)

        # Compute all statistics in one pass
        n_overlap = da.sum(da.logical_and(row_flagged, all_data_flagged))
        (
            n_total,
            n_row_flagged,
            n_all_data_flagged,
            n_combined,
            n_unflagged,
            n_overlap,
            n_chan_total,
            n_chan_flagged,
        ) = dask.compute(
            row_flagged.size,
            da.sum(row_flagged),
            da.sum(all_data_flagged),
            da.sum(is_flagged),
            da.sum(unflagged_rows),
            n_overlap,
            chan_fully_flagged.size,
            da.sum(chan_fully_flagged),
        )

        n_extra = int(n_all_data_flagged) - int(n_overlap)

        print(f"Total rows:           {int(n_total):8d}")
        print(f"  FLAG_ROW flagged:   {int(n_row_flagged):8d}")
        print(f"  All-data-flagged:   {int(n_all_data_flagged):8d}")
        print(f"  Combined to remove: {int(n_combined):8d}")
        print(f"  Remaining:          {int(n_unflagged):8d}")
        print(f"  Extra rows caught by all(FLAG) check: {n_extra}")
        print(f"  Fully-flagged channels: {int(n_chan_flagged)} / {int(n_chan_total)}")

        if int(n_unflagged) == 0:
            raise RuntimeError(
                "No unflagged rows remain after optimize — nothing to write"
            )

        if int(n_chan_flagged) == int(n_chan_total):
            raise RuntimeError("All channels fully flagged — nothing to write")

        # Find dimension names from DATA (typically "row", "chan")
        row_dim = self.ds.DATA.dims[0]
        chan_dim = self.ds.DATA.dims[1]

        # Build indexers for rows and channels
        keep_row_mask = unflagged_rows.compute()
        keep_row_idx = np.nonzero(keep_row_mask)[0]

        isel_indexers = {row_dim: keep_row_idx}

        if int(n_chan_flagged) > 0:
            keep_chan_mask = keep_channels.compute()
            keep_chan_idx = np.nonzero(keep_chan_mask)[0]
            isel_indexers[chan_dim] = keep_chan_idx

        self.ds = self.ds.isel(isel_indexers)

        # Mark all changed variables
        for var_name in self.ds.data_vars:
            if row_dim in self.ds[var_name].dims:
                self.changed[var_name] = True

        print(
            f"Optimize complete."
            f" Rows: {int(n_unflagged)}, Channels: {int(n_chan_total) - int(n_chan_flagged)}"
        )

    def write_new_ms(self, name, clobber):
        """
        Write a new MS, and make sure it doesn't already exist
        """
        all_tables = list(self.ds.keys())
        print(f"Writing {all_tables} to {name}")

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

        # Copy subtables (SPECTRAL_WINDOW, ANTENNA, FIELD, etc.) from
        # the input MS.  xds_to_table only writes the main table.
        # Suppress casacore C++ stderr noise (SORT_COLUMNS etc.) unless
        # --debug is set.
        with _maybe_quiet_stderr():
            for sub in self.sub_table_names:
                sub_name = os.path.basename(sub)
                dest = os.path.join(name, sub_name)
                if os.path.exists(dest):
                    shutil.rmtree(dest)
                t = table(sub, ack=False)
                t.copy(dest, deep=True)
                t.close()
                logger.debug("  copied subtable %s", sub_name)

        # If channels were reduced (frequency_average or optimize),
        # update the SPECTRAL_WINDOW CHAN_FREQ in the output MS.
        if self.chan_freq_hz is not None:
            nchan_in_ds = self.ds.FLAG.shape[1]
            if len(self.chan_freq_hz) != nchan_in_ds:
                for sub in self.sub_table_names:
                    if "SPECTRAL_WINDOW" in sub:
                        sub_name = os.path.basename(sub)
                        dest = os.path.join(name, sub_name)
                        sw = table(dest, ack=False, readonly=False)
                        sw.putcol("CHAN_FREQ", self.chan_freq_hz.reshape(1, -1))
                        sw.close()
                        break

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
                print(f"Updating table: {to_update} in {name}")
                logger.debug(f"    ds={self.ds[to_update]}")
                writes = xds_to_table(self.ds, f"{name}", to_update)
                with ProgressBar():
                    dask.compute(writes)

                self.changed[to_update] = False
