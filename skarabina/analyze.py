# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""Analyze a measurement set and recommend an image size."""

import json
import math
from dataclasses import dataclass

import click
import dask.array as da
import numpy as np
from angle_parser import parse_angle

# Import dask-ms before casacore.tables so that, when the casacure backend is
# selected (DASK_MS_BACKEND=casacure), daskms's casacore->casacure aliasing is
# installed before the `casacore` import resolves (see skarabina/dask_ms.py).
import daskms  # noqa: F401,E402
from casacore.tables import table  # noqa: E402
from daskms import xds_from_ms  # noqa: E402

from skarabina.dask_ms import C_MS, TIME_AVERAGE_LOSS, max_integration_time

# Sentinel prefix for the single-line JSON emitted by --json-stdout.  Stimela
# cab definitions match on this exact string, so treat it as a public API.
JSON_STDOUT_PREFIX = "SKARABINA_ANALYZE_JSON "

# 0.939 is the coefficient of the standard bandwidth-smearing factor R_b
# (CASA/Wieringa convention): with r_1 the radial distance from the phase centre,
# R_b = 1/sqrt(1 + (0.939 * r_1 * channel_width / (nu * FOV))^2), evaluated at
# the edge of the field.
BW_SMEARING_COEFF = 0.939


def max_uv_distance(ds):
    """Longest baseline in the dataset, in metres.

    Rows with ``FLAG_ROW`` set are ignored: they carry no usable data (a
    flagger such as ``skarabina --flag-uv-above`` marks exactly those rows),
    so counting them would recommend an image size for baselines that will
    never be imaged.  Raises :class:`click.ClickException` when every row is
    flagged.
    """
    uvw = da.asarray(ds.UVW)
    uv_sq = uvw[:, 0] * uvw[:, 0] + uvw[:, 1] * uvw[:, 1]

    if "FLAG_ROW" in ds:
        keep = da.logical_not(da.asarray(ds.FLAG_ROW))
        n_flagged = int(da.sum(da.asarray(ds.FLAG_ROW)).compute())
        n_total = int(ds.FLAG_ROW.shape[0])
        if n_flagged:
            print(
                f"  Ignoring {n_flagged} of {n_total} rows with FLAG_ROW set"
            )
        if n_flagged >= n_total:
            raise click.ClickException(
                "Every row is flagged (FLAG_ROW) — nothing left to image"
            )
        if n_flagged:
            uv_sq = da.where(keep, uv_sq, 0.0)

    return float(da.sqrt(da.max(uv_sq)).compute())


@dataclass(frozen=True)
class BandInfo:
    """The band described by the SPECTRAL_WINDOW subtable.

    ``channel_width_hz`` is the actual per-channel width read from the subtable
    (``CHAN_WIDTH``, falling back to ``RESOLUTION``), *not* the band span
    divided by the channel count.  The two differ whenever the channels are not
    uniformly spaced -- notably when ``skarabina --optimize`` has dropped
    fully-flagged channels from the middle of the band, which leaves a hole that
    no column records (see ``has_gaps``).

    ``bandwidth_hz`` is the sum of the individual channel widths: the width of
    spectrum recorded, edge to edge.  ``span_hz`` is ``nu_max - nu_min``, the
    distance between the first and last channel centres.  ``hole_hz`` is the
    spectrum missing *between* channels -- zero for a contiguous band, positive
    when channels have been removed from the middle.  (``bandwidth_hz`` alone
    cannot tell you this: for a contiguous band the channels tile the spectrum
    and it equals ``span_hz`` plus half a channel at each end.)
    """

    nu_min_hz: float
    nu_max_hz: float
    n_chan: int
    channel_width_hz: float
    bandwidth_hz: float
    span_hz: float
    hole_hz: float
    has_gaps: bool


def _per_channel_widths(sw):
    """Per-channel widths from a SPECTRAL_WINDOW table, or ``None``.

    ``CHAN_WIDTH`` is the standard column; ``RESOLUTION`` is the fallback some
    writers populate instead.  Returns ``None`` when neither is present or when
    the column does not match the channel count.
    """
    for name in ("CHAN_WIDTH", "RESOLUTION"):
        if name not in sw.colnames():
            continue
        values = np.atleast_1d(sw.getcol(name)).astype(float)
        if values.size:
            return values.reshape(-1)
    return None


def band_info(ms):
    """Describe the band of an MS from its SPECTRAL_WINDOW subtable.

    Returns a :class:`BandInfo`, or ``None`` when the MS has no SPECTRAL_WINDOW
    subtable or no channels.

    The band edges are the extreme channel frequencies across *all* spectral
    windows and the channel count is the last axis of ``CHAN_FREQ``, so a
    multi-window MS is described by one span.  ``has_gaps`` is then true, and
    the smearing limits derived from the band are the conservative ones.
    """
    t = table(ms)
    try:
        for sub in t.getsubtables():
            if "SPECTRAL_WINDOW" in sub:
                sw = table(sub, ack=False)
                try:
                    freqs = np.atleast_1d(sw.getcol("CHAN_FREQ")).astype(float)
                    if freqs.size == 0:
                        return None
                    freqs = freqs.reshape(-1)
                    n_chan = int(freqs.size)
                    widths = _per_channel_widths(sw)
                    if widths is None or widths.size != n_chan:
                        # No usable width column: fall back to the nominal
                        # spacing between the first two channels.
                        nominal = (
                            abs(freqs[1] - freqs[0])
                            if n_chan > 1
                            else 0.0
                        )
                        widths = np.full(n_chan, nominal)
                finally:
                    sw.close()

                bandwidth = float(widths.sum())
                span = float(freqs[-1] - freqs[0])
                # A gap is where neighbouring channels are further apart than
                # the channels themselves are wide.  Comparing against half the
                # sum of the two widths tolerates the sub-Hz rounding that
                # CHAN_FREQ carries in real files.
                gap_hz = np.diff(freqs) - 0.5 * (widths[1:] + widths[:-1])
                gaps = gap_hz > 0.0
                return BandInfo(
                    nu_min_hz=float(freqs.min()),
                    nu_max_hz=float(freqs.max()),
                    n_chan=n_chan,
                    channel_width_hz=float(np.median(widths)),
                    bandwidth_hz=bandwidth,
                    span_hz=span,
                    hole_hz=float(gap_hz[gaps].sum()) if gaps.any() else 0.0,
                    has_gaps=bool(gaps.any()),
                )
    finally:
        t.close()
    return None


def ms_content(ms):
    """What is in the MS: shape, fields, scans, antennas, time range.

    The content summary a ``listobs``/``goquartical-summary`` cab used to
    provide, computed here from the small index columns only (TIME,
    INTERVAL, FIELD_ID, SCAN_NUMBER, FLAG_ROW) and the
    FIELD/ANTENNA/POLARIZATION subtables -- never the visibility cubes, so
    this stays cheap on a large MS.  Flag *fractions* are ``skarabina
    --summary``'s job.

    The ``scans`` list carries one record per scan, in time order (field,
    row count, start/end/duration): the table a scheduling analysis needs to
    tell targets from calibrators, and the replacement for the listobs
    dimension summary a pipeline config header used to carry.  Antenna
    positions (ITRF metres, same order as ``antenna_names``) are included so
    a consumer can pick, say, a reference antenna nearest the array centre.

    Returns a plain dict, JSON-ready.
    """
    t = table(ms, ack=False)
    try:
        n_rows = int(t.nrows())
        field_ids = (
            np.asarray(t.getcol("FIELD_ID")) if "FIELD_ID" in t.colnames()
            else np.zeros(n_rows, dtype=int)
        )
        scan_numbers = (
            np.asarray(t.getcol("SCAN_NUMBER")) if "SCAN_NUMBER" in t.colnames()
            else np.zeros(n_rows, dtype=int)
        )
        times = np.asarray(t.getcol("TIME"), dtype=float) if n_rows else np.zeros(0)
        intervals = (
            np.asarray(t.getcol("INTERVAL"), dtype=float)
            if n_rows and "INTERVAL" in t.colnames()
            else np.zeros(n_rows)
        )
        n_rows_flagged = (
            int(np.asarray(t.getcol("FLAG_ROW")).sum())
            if "FLAG_ROW" in t.colnames() else 0
        )

        subtables = t.getsubtables()
    finally:
        t.close()

    def _sub(sub_suffix):
        for sub in subtables:
            if sub.endswith(sub_suffix):
                return sub
        return None

    field_names = []
    field_sub = _sub("/FIELD")
    if field_sub is not None:
        ft = table(field_sub, ack=False)
        try:
            field_names = [str(name).strip() for name in ft.getcol("NAME")]
        finally:
            ft.close()

    antenna_names = []
    antenna_positions = []
    antenna_sub = _sub("/ANTENNA")
    if antenna_sub is not None:
        at = table(antenna_sub, ack=False)
        try:
            antenna_names = [str(name).strip() for name in at.getcol("NAME")]
            if "POSITION" in at.colnames() and len(antenna_names):
                pos = np.atleast_2d(
                    np.asarray(at.getcol("POSITION"), dtype=float)
                )
                antenna_positions = [
                    [float(x) for x in row] for row in pos[: len(antenna_names)]
                ]
        finally:
            at.close()

    n_corr = None
    pol_sub = _sub("/POLARIZATION")
    if pol_sub is not None:
        pt = table(pol_sub, ack=False)
        try:
            num_corr = np.atleast_1d(pt.getcol("NUM_CORR"))
            if num_corr.size:
                n_corr = int(num_corr[0])
        finally:
            pt.close()

    # Per-field row counts over the ids actually present in the data.
    fields = []
    for fid in sorted(set(int(f) for f in field_ids)):
        fields.append({
            "field_id": fid,
            "name": field_names[fid] if fid < len(field_names) else "",
            "n_rows": int((field_ids == fid).sum()),
        })

    def _field_name(fid):
        return field_names[fid] if fid < len(field_names) else ""

    # Per-scan records, in time order.  A scan observes one field; if the ids
    # disagree inside a scan (a corrupted index or a mid-scan field change)
    # the majority id wins, ties to the lower id.  The duration is the span
    # between the first and last integration plus one integration (TIME is
    # the integration centroid), so a single-integration scan has a nonzero
    # duration -- that is what separates it from an empty one.
    scans = []
    for scan in sorted(set(int(s) for s in scan_numbers)):
        sel = scan_numbers == scan
        n_scan_rows = int(sel.sum())
        stimes = times[sel]
        fids, fid_counts = np.unique(field_ids[sel], return_counts=True)
        fid = int(fids[np.argmax(fid_counts)])
        scans.append({
            "scan_number": scan,
            "field_id": fid,
            "name": _field_name(fid),
            "n_rows": n_scan_rows,
            "time_start_s": float(stimes.min()) if n_scan_rows else None,
            "time_end_s": float(stimes.max()) if n_scan_rows else None,
            "duration_s": (
                float(stimes.max() - stimes.min() + np.median(intervals[sel]))
                if n_scan_rows else 0.0
            ),
        })
    scans.sort(key=lambda s: s["time_start_s"] or 0.0)

    return {
        "n_rows": n_rows,
        "n_rows_flagged": n_rows_flagged,
        "n_corr": n_corr,
        "n_antennas": len(antenna_names),
        "antenna_names": antenna_names,
        "antenna_positions_m": antenna_positions,
        "n_fields": len(fields),
        "fields": fields,
        "n_scans": len(scans),
        "scan_numbers": [s["scan_number"] for s in scans],
        "scans": scans,
        "time_start_s": float(times.min()) if times.size else None,
        "time_end_s": float(times.max()) if times.size else None,
        "duration_s": float(times.max() - times.min()) if times.size else None,
    }


def averaging_limits(band, max_uv, fov_rad):
    """How coarsely the data can be averaged before smearing matters.

    Takes a :class:`BandInfo` so the band is described once, in one place.  All
    limits are quoted at the *edge of the requested field of view*
    (``fov_rad / 2``), which is the part of the image that actually has to stay
    intact -- smearing grows with distance from the phase centre, so the edge is
    the worst case.  (The superseded set-image-parameters cab used a fixed 10
    degree field edge for the channel limit, which is far stricter: it protects
    the whole primary beam rather than the image.)

    Returns ``(max_channel_width_hz, min_channels, max_integration_time_s,
    bandwidth_smearing_factor)``:

    * white-light fringes: beyond ``max_channel_width_hz`` the fringes from the
      two ends of the band decorrelate at the field edge.  The criterion is that
      the phase change across the band, ``2π·Δν·B·θ_edge/c``, reaches 2π, i.e.
      ``Δν_max = c/(B·θ_edge)`` -- the same limit WSClean applies as
      ``maxuv-l``, and the data supports no fewer than ``min_channels``
      channels;
    * time-average smearing: ``max_integration_time_s`` is the longest
      integration whose smearing loss at the edge stays within
      ``dask_ms.TIME_AVERAGE_LOSS`` (currently 10%).  It is computed by
      :func:`skarabina.dask_ms.max_integration_time`, which inverts
      ``ρ = sinc(π·ω_⊕·Δt·B·ν·θ/c)``, so the flagger's summary and this cab
      cannot quote different limits for the same criterion;
    * ``bandwidth_smearing_factor`` is the resulting radial smearing factor R_b
      for the channels as they are *now*, i.e. before any averaging, at the
      field edge.  It uses the **actual** channel width from the subtable, so a
      band with holes is not described as if its channels were wider than they
      are.
    """
    theta_edge = fov_rad / 2.0
    nu_max = band.nu_max_hz

    max_channel_width = C_MS / (max_uv * theta_edge)
    # The fewest channels the data supports is the true bandwidth divided by
    # the widest channel that survives at the field edge.  Using the sum of the
    # channel widths (rather than the span) keeps this honest for a band with
    # holes: the hole is spectrum that is not there and cannot be imaged.
    min_channels = int(band.bandwidth_hz / max_channel_width) + 1

    # Shared with `skarabina --summary`: one criterion, one implementation.
    max_integration_time_s = max_integration_time(
        nu_max, max_uv, fov_rad, loss=TIME_AVERAGE_LOSS
    )

    channel_width = band.channel_width_hz
    # Radial distance from the phase centre to the field edge is sin(theta_edge);
    # see the R_b relation quoted above.
    r_1 = math.sin(theta_edge)
    r_b = 1.0 / math.sqrt(
        1.0 + (BW_SMEARING_COEFF * r_1 * channel_width / (fov_rad * nu_max)) ** 2
    )

    return max_channel_width, min_channels, max_integration_time_s, r_b


@click.command("skarabina-analyze")
@click.option("--ms", required=True, help="Input measurement set")
@click.option(
    "--image-fov",
    type=str,
    required=True,
    help="Image field-of-view (value with unit: deg, arcmin, arcsec, rad)",
)
@click.option(
    "--oversampling-factor",
    type=float,
    default=5.0,
    show_default=True,
    help="Pixels per resolution element (synthesised beam)",
)
@click.option(
    "--output-json",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Write analysis results as JSON to this file",
)
@click.option(
    "--json-stdout",
    is_flag=True,
    default=False,
    help="Print the analysis results as a single JSON line on stdout",
)
def main(ms, image_fov, oversampling_factor, output_json, json_stdout):
    """Analyze a measurement set and recommend an image size.

    Computes the angular resolution from the longest baseline and the highest
    channel frequency, recommends image dimensions in pixels, and reports how
    coarsely the data may be averaged (channel width, integration time) before
    smearing shows at the edge of the field.

    The resolution is the theoretical best case for the array as measured:
    ``c / (nu_max * B_max)``, i.e. at the top of the band and with no tapering or
    weighting.  A real imager's synthesised beam is therefore equal or slightly
    broader, so the recommended pixel size errs on the side of oversampling.
    """
    # Group by DATA_DESC_ID only: dask-ms's default (FIELD_ID, DATA_DESC_ID)
    # grouping would hide every field but the first from the baseline search.
    datasets = xds_from_ms(ms, group_cols=("DATA_DESC_ID",))
    ds = datasets[0]

    # Max UV distance (metres), ignoring rows flagged by a previous flagger
    max_uv = max_uv_distance(ds)

    # Band description from SPECTRAL_WINDOW: edges, channel count, the actual
    # per-channel width and whether the band has holes in it.
    band = band_info(ms)

    # What is in the MS: rows, fields, scans, antennas, time range.
    content = ms_content(ms)

    if band is None or max_uv == 0:
        raise click.ClickException("Could not determine resolution from MS")

    nu_min, nu_max = band.nu_min_hz, band.nu_max_hz
    fov_rad = parse_angle(image_fov)

    # Angular resolution (radians)
    theta_res = C_MS / (nu_max * max_uv)

    # Image size in pixels
    n_pix = int(oversampling_factor * fov_rad / theta_res)

    # Round up to even or nice number
    n_pix = ((n_pix + 1) // 2) * 2  # even

    theta_res_arcsec = theta_res * 180.0 / math.pi * 3600.0

    (
        max_channel_width,
        min_channels,
        max_integration_time_s,
        r_b,
    ) = averaging_limits(band, max_uv, fov_rad)

    print(f"Measurement set:  {ms}")
    print(f"  Max baseline:   {max_uv:.0f} m")
    print(f"  Max frequency:  {nu_max / 1e6:.3f} MHz")
    print(f"  Resolution:     {theta_res_arcsec:.2f} arcsec")
    print(f"  Field of view:  {image_fov}")
    print(
        f"MS content: {content['n_rows']} rows"
        f" ({content['n_rows_flagged']} with FLAG_ROW set),"
        f" {content['n_antennas']} antennas, {content['n_corr']} correlations"
    )
    for field in content["fields"]:
        print(f"  Field {field['field_id']} {field['name']!r}: {field['n_rows']} rows")
    print(
        f"  Scans: {content['n_scans']} ({', '.join(str(s) for s in content['scan_numbers'])})"
    )
    for scan in content["scans"]:
        print(
            f"    scan {scan['scan_number']}: field {scan['field_id']}"
            f" {scan['name']!r}, {scan['n_rows']} rows, {scan['duration_s']:.1f} s"
        )
    if content["time_start_s"] is not None:
        print(
            f"  Time: {content['time_start_s']:.1f} .. {content['time_end_s']:.1f} s"
            f" ({content['duration_s']:.1f} s elapsed)"
        )
    print(f"Recommended image size: {n_pix} × {n_pix} pixels")
    print(
        f"  Channels:       {band.n_chan} × {band.channel_width_hz / 1e3:.1f} kHz"
        f"  ({band.bandwidth_hz / 1e6:.3f} MHz of spectrum)"
    )
    if band.has_gaps:
        print(
            f"  Band has holes: {band.hole_hz / 1e6:.3f} MHz missing between the"
            " band edges"
            " (channels are not contiguous; a narrowband hole is not recorded"
            " in the subtable)"
        )
    print(f"Averaging limits at the field edge ({math.degrees(fov_rad / 2):.2f} deg):")
    print(f"  Max channel width: {max_channel_width / 1e3:.1f} kHz"
          f"  (no fewer than {min_channels} channels)")
    print(f"  Max integration:   {max_integration_time_s:.1f} s"
          f"  (for {TIME_AVERAGE_LOSS:.0%} loss at the edge)")
    print(f"  Bandwidth smearing factor (as-is): {r_b:.4f}")

    result = {
        "ms": ms,
        "max_baseline_m": max_uv,
        # The content summary (shape, fields, scans, antennas, time range)
        # that replaces the quartical-summary/listobs cabs in the pipeline.
        "ms_content": content,
        "min_frequency_hz": nu_min,
        "max_frequency_hz": nu_max,
        "max_frequency_mhz": nu_max / 1e6,
        # bandwidth_hz is the spectrum actually present (sum of channel
        # widths); span_hz includes any holes.  They differ only for a band
        # with holes, e.g. after --optimize has dropped middle channels.
        "bandwidth_hz": band.bandwidth_hz,
        "span_hz": band.span_hz,
        "channel_width_hz": band.channel_width_hz,
        "band_has_gaps": band.has_gaps,
        "num_channels": band.n_chan,
        "resolution_arcsec": theta_res_arcsec,
        "field_of_view": image_fov,
        "oversampling_factor": oversampling_factor,
        "recommended_image_size_pixels": n_pix,
        "max_channel_width_hz": max_channel_width,
        "min_channels": min_channels,
        "max_integration_time_s": max_integration_time_s,
        "bandwidth_smearing_factor": r_b,
    }

    if output_json:
        with open(output_json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Wrote {output_json}")

    if json_stdout:
        # A single line, prefixed by a sentinel, so that a stimela output
        # wrangler can pick the machine-readable results out of this cab's
        # console output.  Keep this line intact: it is a public interface.
        print(f"{JSON_STDOUT_PREFIX}{json.dumps(result)}")
