<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Usage

## Command-line options

```
  --ms MS                       Input measurement set (required)
  --flag ENTRY[, ENTRY...]      A flagging operation, or a comma-separated run
                                of them, IN THE ORDER THEY SHOULD RUN.
                                Repeatable; occurrences concatenate. Verbs:
                                  autos
                                  uv-above <metres>
                                  nan
                                  clip <lo> <hi>
                                  spectral-window <rules.yml>
                                  save:<name> / restore:<name>
                                Commas inside brackets are not separators, so
                                a rule file stays a file; quote a path that
                                contains spaces. An operation not listed does
                                not run. See doc/NEW_FLAGGING.md.
  --flag-file FILE              Read flagging operations from a file: a YAML
                                list, or one entry per line with '#' comments.
                                Runs before the entries in --flag.
  --scan TEXT                   Keep only these scans: comma-separated
                                numbers and lo~hi ranges (e.g. "1,12,14"
                                or "0~5"). Default is all scans. Applied
                                before the --flag sequence.
  --summary / --no-summary      Print flagging summary with histogram,
                                field list, spectral window info, and
                                fringe-rotation integration time limits
  --barber / --no-barber        Run barber flagging report (a read-only
                                diagnostic; NOT a --flag verb)
  --barber-pol INTEGER          Polarization for barber
  --time-average-factor INTEGER Average every N rows (see AVERAGING.md)
  --frequency-average-factor INTEGER Average every N channels (see AVERAGING.md)
  --field-of-view FLOAT         Field-of-view FULL width (degrees,
                                default 1.0). Same convention as
                                skarabina-analyze --image-fov: the distance
                                from the phase centre to the edge is half
                                of it.
  --optimize / --no-optimize    Remove fully-flagged rows and channels.
                                Must run after --time-average-factor and
                                --frequency-average-factor (averaging
                                precedes optimization), and requires
                                --msout or --apply to write the result
  --keep-fully-flagged-channels Keep dead channels instead of removing them,
                                so the band stays contiguous (see AVERAGING.md)
  --apply / --no-apply          Modify input MS in place. Writes ONLY the
                                columns that changed, so it is the path to use
                                for large or network-mounted measurement sets
  --clobber / --no-clobber      Overwrite existing output
  --msout MS                    Output measurement set path. Copies every
                                column, so it costs far more I/O than --apply
  --write-changed-only          With --msout, share the unchanged columns with
                                the input instead of copying them, so the output
                                costs about as little I/O as --apply while still
                                being a separate measurement set (see
                                NEW_FLAGGING.md §5.3)
  --split TEXT                  When writing (--msout), keep only this
                                field's rows (field name or numeric
                                FIELD_ID; see SPLITTING.md)
  --debug / --no-debug          Verbose debug output
  --version                     Print version and exit
```

## Examples

### Barber flagging

Generate a report (in the style of barber):

    skarabina --ms foo.ms --barber

### Autocorrelation flagging

Auto baselines measure the total power of a single antenna: no fringes, so they
are useless for imaging and are normally excluded from calibration.

    skarabina --ms test.ms --flag "autos" --msout clean.ms --clobber

`--optimize` then removes the auto rows entirely (their `FLAG_ROW` bits are
set).

### Clip and NaN flagging

Flag in-place:

    skarabina --ms test.ms --flag "nan, clip 0 100" --apply --clobber

Write a new MS:

    skarabina --ms test.ms --flag "clip 0 100, nan" --msout bar.ms --clobber

### TFCrop (automatic RFI flagging)

`tfcrop` finds outliers in the time-frequency plane the way CASA's
`flagdata(mode='tfcrop')` does: it fits the bandpass, divides it out, and flags
what is left over. That is what lets it catch a weak narrow-band spike without
also flagging the bright end of the band, which a plain `clip` cannot do.

    skarabina --ms test.ms --flag "tfcrop" --apply --clobber

With no parameters it uses CASA's defaults. Parameters are named after CASA's,
in any order and any subset, written with `=` or `:`:

    skarabina --ms test.ms --flag "tfcrop [timecutoff=5, freqcutoff=2.5]" --apply
    skarabina --ms test.ms --flag "tfcrop timecutoff: 5, freqcutoff: 2.5" --apply

| Parameter | Default | Meaning |
|---|---|---|
| `timecutoff` | 4.0 | threshold in robust sigmas, time direction |
| `freqcutoff` | 3.0 | threshold in robust sigmas, frequency direction |
| `timefit` | `line` | `line` or `poly` along time |
| `freqfit` | `poly` | `line` or `poly` along frequency |
| `maxnpieces` | 7 | most pieces in a piece-wise fit (1-7) |
| `flagdimension` | `freqtime` | `freqtime`, `timefreq`, `freq` or `time` |
| `usewindowstats` | `none` | `none`, `sum`, `std` or `both` |
| `halfwin` | 1 | sliding-window half-width (1-3) |

A typo is an error rather than a silently ignored setting, so `maxnpices=3`
fails with a message naming `maxnpieces`.

`flagdimension` decides which directions are searched. Narrow-band RFI -- a
channel that is bright at every time -- can only be found by a `freq`-containing
mode, and a bad integration can only be found by a `time`-containing one. The
default searches both and unions the results. TFCrop is usually worth running
after `uv-above`, and before `clip`:

    skarabina --ms test.ms \
        --flag "autos, uv-above 4000, tfcrop, clip 0 100" \
        --msout cleaned.ms --clobber --write-changed-only

### Spectral-window flagging

Flag known RFI frequency ranges from a YAML file:

    skarabina --ms test.ms --flag "spectral-window spectral-flags.example.yml" --msout cleaned.ms

See `spectral-flags.example.yml` for the format — a list of entries with
`spw` frequency ranges in MHz and optional `uv_below` / `uv_above` constraints.

### Time averaging

    skarabina --ms test.ms --time-average-factor 4 --optimize --msout averaged.ms

See [Time & frequency averaging](AVERAGING.md) for details.

### Frequency averaging

    skarabina --ms test.ms --frequency-average-factor 4 --optimize --msout averaged.ms

See [Time & frequency averaging](AVERAGING.md) for details.

### Splitting by field

Write an MS containing only one field (by name or numeric FIELD_ID).
Flagging and averaging run on the full MS first; only the selected
field's rows are written:

    skarabina --ms raw.ms --flag "nan" --msout target.ms --split "Cyg A" --clobber

See [Splitting an MS by field](SPLITTING.md) for details.

### Selecting scans

Keep only a subset of scans. The selection is applied when the MS is read,
so flagging, averaging and optimization all see the selected scans only:

    skarabina --ms raw.ms --scan 1,12,14 --flag "nan" --msout kept.ms --clobber
    skarabina --ms raw.ms --scan 0~5,20 --frequency-average-factor 8 --msout avg.ms

A selection that matches no rows is an error, so a typo cannot silently
produce an empty MS.

### Flag versions (backups)

Back up and restore flags the way CASA's `flagmanager` does. Versions live
beside the MS in `<ms>.flagversions/`, and the layout is CASA's, so a version
written by skarabina can be listed and restored by CASA, and one written by
CASA can be restored here. `save:` and `restore:` are entries in the flagging
list, so a backup is placed exactly where it is wanted in the sequence:

    # back up, flag, and take a second snapshot after the first pass
    skarabina --ms raw.ms --flag "save:before, uv-above 2000, save:after-uv" \
        --apply --clobber

    # undo it later, writing the restored flags back
    skarabina --ms raw.ms --flag "restore:before" --apply --clobber

Because a `save:` entry acts on the flag state where it appears, ordering the
markers is what makes a sequence of snapshots meaningful — `restore:X` then
`save:Y` re-labels a version.

The backup is taken from the MS on disk, so a version stays restorable even if
the run itself is working on a row selection (`--scan`, `--split`). Saving a
version name that already exists moves the old one aside as
`<name>.old.<timestamp>`, matching `flagmanager`.

List what is available with CASA:

    flagmanager('raw.ms', mode='list')

Restoring a version from a different MS, or one whose row count no longer
matches, is an error rather than a silently misaligned restore.

### Full pipeline

For a large or network-mounted MS, `--apply` is the cheapest path: it writes only
the columns that changed (the flags), whereas a plain `--msout` copies every
column. On a 92 GB measurement set a flagging run writes 103 GB that way and
`--apply` writes the flags alone. Pair `--apply` with `save:` so the edit is
reversible.

    skarabina --ms raw.ms \
        --flag "save:pre-flagging, autos, uv-above 4000, nan, clip 0 100, spectral-window spectral-flags.yml" \
        --time-average-factor 3 \
        --frequency-average-factor 5 \
        --optimize \
        --field-of-view 1.5 \
        --summary \
        --apply --clobber

When the input must stay untouched — a read-only mount, provenance rules, or a
pipeline that wants the raw MS alongside the flagged one — add
`--write-changed-only` to the `--msout` path. It writes the same columns
`--apply` does and shares the rest with the input (measured: 6.1 GB written
instead of 103 GB), while still producing a separate MS:

    skarabina --ms raw.ms --flag "autos, nan, clip 0 100" \
        --msout flagged.ms --clobber --write-changed-only

Three caveats. Averaging or `--optimize` changes `DATA`, so those columns are
rewritten too and the saving is smaller — the mode only avoids copying what
genuinely did not change. `--split` selects rows, which changes every column's
shape, so it falls back to a full copy with a warning. And the mode saves
*writes*, not reads: the output is read through dask-ms, which loads the whole
MS to write it, so `--apply` remains the better choice when reading dominates.
