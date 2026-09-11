# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""End-to-end tests for the MS write path.

These use a real (tiny) measurement set: ``write_new_ms`` copies the sub-tables
from the input MS and then has to rewrite the SPECTRAL_WINDOW columns so they
describe the data that was actually written.  Getting that wrong produces an MS
that no CASA task can read correctly, so it is worth testing against casacore
rather than only against the bookkeeping arrays.
"""
import dask.array as da
import numpy as np
import pytest
from casacore.tables import table
from daskms import xds_from_table

from skarabina.dask_ms import DaskMS
from ms_fixture import CHANNEL_WIDTH_HZ, channel_frequencies, make_synthetic_ms


def _spw_columns(ms_path, columns):
    sw = table(f"{ms_path}/SPECTRAL_WINDOW", ack=False)
    try:
        return {col: sw.getcol(col) for col in columns}
    finally:
        sw.close()


PER_CHANNEL_COLUMNS = ("CHAN_FREQ", "CHAN_WIDTH", "EFFECTIVE_BW", "RESOLUTION")


def test_averaged_subtable_is_self_consistent(tmp_path):
    """Every per-channel SPECTRAL_WINDOW column must match NUM_CHAN.

    Regression test: CHAN_WIDTH and EFFECTIVE_BW were left describing the
    input channel count, so dask-ms refused the averaged MS with
    "conflicting sizes for dimension 'chan'" -- which is how
    quartical-summary failed on the first real-data run.
    """
    in_ms = make_synthetic_ms(tmp_path / "in.ms", nchan=8, nrow=4)

    ms = DaskMS(in_ms)
    ms.frequency_average(4)
    out_ms = str(tmp_path / "out.ms")
    ms.write_new_ms(out_ms, clobber=True)

    cols = _spw_columns(out_ms, ("NUM_CHAN",) + PER_CHANNEL_COLUMNS)
    nchan = int(cols["NUM_CHAN"][0])
    assert nchan == 2
    for col in PER_CHANNEL_COLUMNS:
        assert cols[col].shape == (1, nchan), f"{col} has {cols[col].shape}"

    # The same read that quartical-summary performs must succeed.
    datasets = xds_from_table(f"{out_ms}/SPECTRAL_WINDOW")
    assert len(datasets) == 1
    assert datasets[0].CHAN_WIDTH.shape[-1] == nchan
    assert datasets[0].EFFECTIVE_BW.shape[-1] == nchan
    assert datasets[0].RESOLUTION.shape[-1] == nchan


def test_written_ms_keeps_every_subtable_link(tmp_path):
    """The written MS must link every sub-table the input had.

    Regression test: dask-ms writes the main table without the SOURCE keyword,
    so the copied SOURCE sub-table was orphaned -- getsubtables() did not list
    it and CASA's Calibrater failed with "NullTable::lock - Table object is
    empty".
    """
    import os

    in_ms = make_synthetic_ms(tmp_path / "in.ms", nchan=4, nrow=4)

    ms = DaskMS(in_ms)
    out_ms = str(tmp_path / "out.ms")
    ms.write_new_ms(out_ms, clobber=True)

    def subtables(path):
        t = table(path, ack=False)
        try:
            return {os.path.basename(s) for s in t.getsubtables()}
        finally:
            t.close()

    assert subtables(in_ms) <= subtables(out_ms), "a sub-table link was lost"

    out = table(out_ms, ack=False)
    try:
        keywords = set(out.getkeywords())
        assert "SOURCE" in keywords
        # ...and the link points inside the new MS, not back at the input.
        link = out.getkeyword("SOURCE")
        assert os.path.abspath(out_ms) in link
        assert os.path.abspath(in_ms) not in link
    finally:
        out.close()


def test_frequency_average_rewrites_spectral_window(tmp_path):
    """Averaging must leave the sub-table describing the averaged channels."""
    in_ms = make_synthetic_ms(tmp_path / "in.ms", nchan=4, nrow=4)

    ms = DaskMS(in_ms)
    ms.frequency_average(2)
    out_ms = str(tmp_path / "out.ms")
    ms.write_new_ms(out_ms, clobber=True)

    # Main table: 2 channels, as averaged.
    main = table(out_ms, ack=False)
    assert main.getcol("DATA").shape[1] == 2
    main.close()

    # Sub-table: 2 channels, with the averaged centres and summed widths.
    cols = _spw_columns(out_ms, ["NUM_CHAN", "CHAN_FREQ", "RESOLUTION", "TOTAL_BANDWIDTH"])
    assert list(cols["NUM_CHAN"]) == [2]
    assert cols["CHAN_FREQ"].shape == (1, 2)
    expected_freq = channel_frequencies(4).reshape(2, 2).mean(axis=1)
    assert np.allclose(cols["CHAN_FREQ"][0], expected_freq)
    assert np.allclose(cols["RESOLUTION"][0], 2 * CHANNEL_WIDTH_HZ)
    assert np.allclose(cols["TOTAL_BANDWIDTH"], [4 * CHANNEL_WIDTH_HZ])


def test_averaging_with_trailing_channels_rewrites_spectral_window(tmp_path):
    """5 channels at factor 2 -> 3 channels (2 pairs + 1 narrower tail)."""
    in_ms = make_synthetic_ms(tmp_path / "in.ms", nchan=5, nrow=4)

    ms = DaskMS(in_ms)
    ms.frequency_average(2)
    out_ms = str(tmp_path / "out.ms")
    ms.write_new_ms(out_ms, clobber=True)

    cols = _spw_columns(out_ms, ["NUM_CHAN", "CHAN_FREQ", "RESOLUTION"])
    assert list(cols["NUM_CHAN"]) == [3]
    freqs = channel_frequencies(5)
    assert np.allclose(cols["CHAN_FREQ"][0], [freqs[0:2].mean(), freqs[2:4].mean(), freqs[4]])
    assert np.allclose(cols["RESOLUTION"][0], [2e7, 2e7, 1e7])


def test_spectral_window_untouched_without_channel_changes(tmp_path):
    """No averaging, no rewrite: the copied sub-table already matches."""
    in_ms = make_synthetic_ms(tmp_path / "in.ms", nchan=4, nrow=4)

    ms = DaskMS(in_ms)
    ms.flag_data({"NAN": True})
    out_ms = str(tmp_path / "out.ms")
    ms.write_new_ms(out_ms, clobber=True)

    cols = _spw_columns(out_ms, ["NUM_CHAN", "CHAN_FREQ"])
    assert list(cols["NUM_CHAN"]) == [4]
    assert np.allclose(cols["CHAN_FREQ"][0], channel_frequencies(4))


def test_scan_selection_writes_only_selected_scans(tmp_path):
    """--scan must be reflected in the written MS (rows and their scans)."""
    in_ms = make_synthetic_ms(
        tmp_path / "in.ms", nchan=4, nrow=4, scan_numbers=[0, 0, 1, 1]
    )

    ms = DaskMS(in_ms)
    ms.select_scans("1")
    out_ms = str(tmp_path / "out.ms")
    ms.write_new_ms(out_ms, clobber=True)

    t = table(out_ms, ack=False)
    try:
        assert t.nrows() == 2
        assert list(t.getcol("SCAN_NUMBER")) == [1, 1]
    finally:
        t.close()


def test_scan_selection_and_averaging_together(tmp_path):
    """The two options compose: select rows, average channels, write once."""
    in_ms = make_synthetic_ms(
        tmp_path / "in.ms", nchan=4, nrow=4, scan_numbers=[0, 0, 1, 1]
    )

    ms = DaskMS(in_ms)
    ms.select_scans("0~1")
    ms.flag_uv_above(700)  # rows with uv > 700 m: the 3rd and 4th rows
    ms.frequency_average(2)
    out_ms = str(tmp_path / "out.ms")
    ms.write_new_ms(out_ms, clobber=True)

    t = table(out_ms, ack=False)
    try:
        assert t.nrows() == 4
        assert t.getcol("DATA").shape[1] == 2
        # uv distances are 0, 500, 1000, 1500 m -> the last two rows are flagged
        assert list(t.getcol("FLAG_ROW")) == [False, False, True, True]
    finally:
        t.close()

    cols = _spw_columns(out_ms, ["NUM_CHAN"])
    assert list(cols["NUM_CHAN"]) == [2]


def test_optimize_drops_fully_flagged_channels_from_subtable(tmp_path):
    """optimize removes channels; the sub-table must follow."""
    in_ms = make_synthetic_ms(tmp_path / "in.ms", nchan=4, nrow=4)

    ms = DaskMS(in_ms)
    # Flag channel 1 everywhere, then optimize it away.
    flags = np.zeros((4, 4, 1), dtype=bool)
    flags[:, 1, :] = True
    ms.ds["FLAG"].data = da.from_array(flags, chunks=flags.shape)
    ms.optimize()
    out_ms = str(tmp_path / "out.ms")
    ms.write_new_ms(out_ms, clobber=True)

    main = table(out_ms, ack=False)
    assert main.getcol("DATA").shape[1] == 3
    main.close()

    cols = _spw_columns(out_ms, ["NUM_CHAN", "CHAN_FREQ"])
    assert list(cols["NUM_CHAN"]) == [3]
    expected = np.delete(channel_frequencies(4), 1)
    assert np.allclose(cols["CHAN_FREQ"][0], expected)


def test_multi_ddid_ms_is_rejected(tmp_path):
    """A second DATA_DESC_ID must be an error, not silent truncation."""
    in_ms = make_synthetic_ms(tmp_path / "in.ms", nchan=4, nrow=4)
    dd = table(f"{in_ms}/DATA_DESCRIPTION", readonly=False)
    dd.addrows(1)
    dd.putcol("SPECTRAL_WINDOW_ID", np.array([0, 0], dtype=np.int32))
    dd.putcol("POLARIZATION_ID", np.array([0, 0], dtype=np.int32))
    dd.close()

    data = table(in_ms, readonly=False)
    data.putcol("DATA_DESC_ID", np.array([0, 0, 1, 1], dtype=np.int32))
    data.close()

    with pytest.raises(RuntimeError, match="DATA_DESC_ID"):
        DaskMS(in_ms)
