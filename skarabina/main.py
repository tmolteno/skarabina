# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
import datetime
import logging
from importlib.metadata import version as get_version
from types import SimpleNamespace

import click
from angle_parser import parse_angle

from skarabina import barber, dask_ms, flag_ops

logger = logging.getLogger(__name__)


@click.command("skarabina")
@click.option("--ms", required=True, help="Input measurement set")
@click.option(
    "--scan",
    default=None,
    help="Keep only these scans: comma-separated numbers and lo~hi ranges"
    " (e.g. '1,12,14' or '0~5'). Default is all scans.",
)
@click.option("--msout", default=None, help="Output measurement set")
@click.option(
    "--write-changed-only",
    is_flag=True,
    default=False,
    help="With --msout, share the input's unchanged column blocks with the"
    " output and write only the columns that changed. Avoids re-reading and"
    " rewriting the whole MS for a flagging run. Requires the same row and"
    " channel shape as the input, and the same filesystem for the sharing to"
    " take effect",
)
@click.option(
    "--summary", is_flag=True, default=False, help="Print the flagging summary"
)
@click.option(
    "--optimize",
    is_flag=True,
    default=False,
    help="Optimize measurement set size while keeping rows of equal length",
)
@click.option(
    "--keep-fully-flagged-channels",
    is_flag=True,
    default=False,
    help="With --optimize, keep channels whose visibilities are all flagged"
    " instead of removing them. Flagging already excludes them from imaging,"
    " so keeping them only costs file size -- but it avoids splitting the band"
    " with a hole that SPECTRAL_WINDOW cannot record",
)
@click.option(
    "--time-average-factor",
    type=int,
    default=1,
    help="Combine every N consecutive rows by averaging UVW and visibility data",
)
@click.option(
    "--frequency-average-factor",
    type=int,
    default=1,
    help="Combine every N consecutive frequency channels by averaging",
)
@click.option(
    "--clobber", is_flag=True, default=False, help="Replace the output measurement set"
)
@click.option("--barber", is_flag=True, default=False, help="Perform barber flagging")
@click.option(
    "--barber-pol", type=int, default=None, help="Polarization selection for barber"
)
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Apply flags in-place (update the input MS)",
)
@click.option(
    "--flag",
    "flag_specs",
    multiple=True,
    metavar="ENTRY[, ENTRY...]",
    help="A flagging operation, or a comma-separated run of them, in the"
    " order they should run. Repeatable; occurrences are concatenated."
    " Verbs: autos, uv-above <metres>, nan, clip <lo> <hi>, tfcrop"
    " [key=value ...], rflag [key=value ...], spectral-window <file.yml>, and"
    " the markers save:<name> / restore:<name>. tfcrop and rflag take CASA's"
    " flagdata parameters, e.g. 'tfcrop [timecutoff=4, maxnpieces=7]' or"
    " 'rflag [winsize=5, timedevscale=4]'. See doc/NEW_FLAGGING.md.",
)
@click.option(
    "--flag-file",
    "flag_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    metavar="FILE",
    help="Read --flag entries from a file: a YAML list, or one entry per line"
    " with '#' comments. Concatenated with --flag in the order given.",
)
@click.option("--debug", is_flag=True, default=False, help="Switch on debugging output")
@click.option(
    "--field-of-view",
    type=str,
    default="1.0 deg",
    show_default=True,
    help="Field-of-view full width (value with unit: deg, arcmin, arcsec, rad)."
    " The same convention as skarabina-analyze --image-fov; the distance from"
    " the phase centre to the edge is half of it.",
)
@click.option(
    "--split",
    type=str,
    default=None,
    help="When writing (--msout), keep only this field's rows"
    " (field name or numeric FIELD_ID)",
)
@click.option(
    "--row-chunk",
    type=int,
    default=dask_ms.ROW_CHUNK_ROWS,
    show_default=True,
    help="Number of rows read/written per dask array chunk. Each DATA chunk"
    " holds row_chunk x nchan x ncorr x 8 bytes, so lowering it (and"
    " --workers) bounds peak memory on large MSes at the cost of a larger"
    " task graph. Mirrors tricolour's --row-chunks.",
)
@click.option(
    "--workers",
    type=int,
    default=0,
    help="Number of dask threads (0 = dask default, all CPU cores). Running"
    " flagging with many workers materialises one chunk per thread at once,"
    " so capping this (e.g. --workers 4) is the main lever for bounding peak"
    " RAM on a large MS. Mirrors tricolour's --nworkers.",
)
@click.version_option(
    version=get_version("skarabina"),
    prog_name="skarabina",
    message="%(prog)s %(version)s",
)
def main(**kw):
    print("Mupati (skarabina): The 1GC flagger")
    opts = SimpleNamespace(**kw)

    level = logging.DEBUG if opts.debug else logging.INFO
    logging.basicConfig(level=level)
    root = logging.getLogger()
    root.setLevel(level)

    # Bound dask's thread pool (and with it the number of chunks materialised
    # concurrently). dask's default is one thread per CPU core; on a large MS
    # that means dozens of concurrent chunk reads, each holding a numpy buffer
    # plus (via the I/O layer) a decoded cell copy — the dominant source of
    # peak RSS. An explicit, capped pool keeps memory predictable.
    # See tricolour.apps.tricolour.app.main (ThreadPool(nworkers)).
    if opts.workers is not None and opts.workers > 0:
        from multiprocessing.pool import ThreadPool
        import dask

        dask.config.set(pool=ThreadPool(opts.workers))
        logger.debug("dask thread pool set to %d workers", opts.workers)

    # dask-ms emits noisy warnings/tracebacks for unpopulated MS columns
    # (MODEL_DATA shape guessing, FLAG_CATEGORY with no rows). These are
    # harmless — the columns exist in schema but were never written to.
    logging.getLogger("daskms").setLevel(logging.ERROR)

    if opts.debug:
        ts = datetime.datetime.now().timestamp()
        fh = logging.FileHandler(filename=f"skarabina.{ts}.log")
        fh.setLevel(level)
        root.addHandler(fh)
        root.debug(f"options: {vars(opts)}")

    ms = dask_ms.DaskMS(opts.ms, row_chunk=opts.row_chunk)
    fov_str = opts.field_of_view if opts.field_of_view is not None else "1.0 deg"
    # Full width, in radians; summary() halves it to get the distance from the
    # phase centre to the edge of the field.
    ms._fov_rad = parse_angle(fov_str)

    # --- Flagging: the --flag list IS the run ---
    #
    # The entries are the operations, in the order they will run, so there is no
    # separate "enable" step and no hidden canonical order.  Statistics for the
    # data-flagging steps are deferred into one dask pass (see flag_ops.run), so
    # a long sequence reads the data column once rather than once per step.

    flag_specs = list(opts.flag_specs)
    for path in opts.flag_files:
        flag_specs.extend(flag_ops.load_file(path))
    ops = flag_ops.parse(flag_specs)

    if not ops:
        print("No flagging operations requested (--flag was not given)")

    # Row selection precedes the sequence: a version saved after a scan
    # selection would hold only the selected rows and could never be restored.
    # With no 'scan' verb in --flag, doing it here keeps --scan's documented
    # meaning ("keep only these scans") without needing a token for it.
    if opts.scan is not None:
        print(f"scan selection: {opts.scan!r}")
        ms.select_scans(opts.scan)

    flag_ops.run(ms, ops)

    # --- Row removal / averaging (MUST be last before writing) ---

    if opts.frequency_average_factor is not None and opts.frequency_average_factor > 1:
        ms.frequency_average(opts.frequency_average_factor)

    if opts.time_average_factor is not None and opts.time_average_factor > 1:
        ms.time_average(opts.time_average_factor)

    if opts.optimize:
        if opts.msout is None and not opts.apply:
            raise RuntimeError(
                "--optimize has no effect without --msout or --apply:"
                " it only removes fully-flagged rows and channels in"
                " memory, so the result is discarded unless written."
                " Add --msout PATH to write a new MS or --apply to"
                " update the input MS in place."
            )
        ms.optimize(keep_fully_flagged_channels=opts.keep_fully_flagged_channels)

    # --- Read-only reports (after all processing) ---

    if opts.summary:
        ms.summary()

    if opts.barber:
        barber.barber(ms, opts.barber_pol)

    # --- Write output ---

    if opts.msout:
        ms.write_new_ms(
            opts.msout,
            opts.clobber,
            split=opts.split,
            changed_only=opts.write_changed_only,
        )
    elif opts.apply:
        ms.update_ms(opts.ms, opts.clobber)
