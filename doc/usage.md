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
                                --msout to write the result (an in-place
                                --apply cannot remove rows or channels)
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
  --field TEXT                  With --flag, confine the verbs' new flags to
                                these fields' rows: comma-separated field
                                names or numeric FIELD_IDs (like --split,
                                but any number). CASA's field= selection:
                                existing flags are never cleared,
                                save:/restore: stay whole-table, and
                                averaging, --optimize and the write keep
                                every field
  --data-column TEXT            What nan/clip/rflag/tfcrop measure: DATA,
                                CORRECTED (CORRECTED_DATA), MODEL
                                (MODEL_DATA), RESIDUAL (CORRECTED_DATA -
                                MODEL_DATA) or RESIDUAL_DATA (DATA -
                                MODEL_DATA). CASA's flagdata datacolumn;
                                the flags always land on FLAG [default:
                                DATA]
  --data-from TEXT              Which column the written DATA holds: DATA,
                                CORRECTED (CORRECTED_DATA) or MODEL
                                (MODEL_DATA). mstransform's datacolumn
                                semantics for the write: e.g. --data-from
                                CORRECTED writes the corrected visibilities
                                as DATA, which is what imaging reads. Runs
                                before the averaging factors [default:
                                DATA]
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

### Neural flagger (`nn-flagger`)

`nn-flagger` streams each data block to a served neural flagger (radio-nn's
`nn-flag-server`) over Arrow Flight and unions its decisions into `FLAG`.
Written after another verb it adds the model's flags to that verb's, with
`tfcrop`'s own parameters still at your disposal:

    skarabina --ms test.ms --flag "tfcrop [timecutoff=5], nn-flagger grpc://flagger.example:8815" --apply

The server URL is the only parameter. Order matters: write `nn-flagger`
*after* the classical verbs it unions with. It reads DATA only and is
unaffected by existing flags, but `tfcrop` honours FLAG on read exactly as
CASA's `flagdata` does, so running it on a pre-flagged table changes its
statistics. Each block is streamed as it is processed, so memory stays
bounded like `tfcrop`'s; the composed form takes one pass over the data per
verb, while `tf-nn` (below) fuses both flaggers into a single pass and adds
the `and` mode.

The served model thresholds its own probability server-side (`flag_threshold`
in the bundle manifest); `nn-flagger` applies the decision it gets back. For
the union to beat `tfcrop` alone the served threshold must be the model's
saturated tail (~0.9997) -- see radio-nn's `SUMMARY.md`.

Requirements: `pyarrow` (`pip install 'skarabina[nn]'`), a reachable
`nn-flag-server`, and an MS whose antenna rows match the served bundle's
antenna map (a MeerKAT `m0xx` set in the same row order). Baselines the
bundle does not know keep their generic flags only. The verb is union-only
by construction -- an intersection would clear flags an earlier verb set;
for that combination see `tf-nn` below.

### TFCrop + neural flagger (`tf-nn`)

`tf-nn` runs `tfcrop` **and** a served neural flagger (radio-nn's
`nn-flag-server`) over the same data block and combines the two decisions
before the block's flags are written:

    skarabina --ms test.ms --flag "tf-nn grpc://flagger.example:8815" --apply

The server URL is the only required parameter. The combine mode is `or`
(the union, the default) or `and` (the intersection):

    skarabina --ms test.ms --flag "tf-nn grpc://flagger.example:8815 and" --apply

Each data block is streamed to the server as it is processed, so the run's
memory is bounded exactly as `tfcrop`'s is, however large the table. The
two decisions are independent: the neural flagger reads DATA only, and
tfcrop sees the incoming FLAG exactly as CASA's `flagdata` would.

The served model thresholds its own probability server-side
(`flag_threshold` in the bundle manifest); `tf-nn` applies the decision it
gets back. For the union to beat `tfcrop` alone the served threshold must
be the model's saturated tail (~0.9997) -- see radio-nn's `SUMMARY.md`.

Requirements: `pyarrow` (`pip install 'skarabina[nn]'`), a reachable
`nn-flag-server`, and an MS whose antenna rows match the served bundle's
antenna map (a MeerKAT `m0xx` set in the same row order). Baselines the
bundle does not know keep their generic flags only.

`flagdimension` decides which directions are searched. Narrow-band RFI -- a
channel that is bright at every time -- can only be found by a `freq`-containing
mode, and a bad integration can only be found by a `time`-containing one. The
default searches both and unions the results. TFCrop is usually worth running
after `uv-above`, and before `clip`:

    skarabina --ms test.ms \
        --flag "autos, uv-above 4000, tfcrop, clip 0 100" \
        --msout cleaned.ms --clobber --write-changed-only

### RFlag (sliding-window statistics)

`rflag` is the other CASA auto-flagger. Where `tfcrop` fits the bandpass and
flags what does not follow it, `rflag` asks whether the *scatter* is unusual,
and needs no model of the band. It catches a short burst and a persistent
narrow-band feature by different steps, so one pass finds both:

    skarabina --ms test.ms --flag "rflag" --apply --clobber

Parameters are `key=value` or `key: value`, named after CASA's:

    skarabina --ms test.ms --flag "rflag [winsize=5, timedevscale=4.0]" --apply

| Parameter | Default | Meaning |
|---|---|---|
| `winsize` | 3 | integrations in the sliding time window |
| `timedev` | unset | time-series noise estimate (measured if unset) |
| `freqdev` | unset | spectral noise estimate (measured if unset) |
| `timedevscale` | 5.0 | threshold multiplier, time step |
| `freqdevscale` | 5.0 | threshold multiplier, spectral step |
| `spectralmax` | 1e6 | flag the whole spectrum above this deviation |
| `spectralmin` | 0.0 | flag the whole spectrum below this deviation |

Supplying `timedev` and `freqdev` replaces the measured thresholds with your
own, which is the two-pass workflow CASA supports: measure on one pass, review,
then apply with the numbers you chose. A good starting point for a MeerKAT
L-band MS is the noise per visibility.

Note that the spectral step compares each channel with its neighbours, so a
channel that merely has *higher gain* looks like a narrow feature and will be
flagged. On a fine channel grid this is harmless; on a coarse grid with a steep
band shape, supply `freqdev` instead of letting it be measured.

Typical use is `rflag` after the cheap selection-based flags and before a clip:

    skarabina --ms test.ms \
        --flag "autos, uv-above 4000, tfcrop, rflag, clip 0 100" \
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

Because a `save:` entry backs up the flag state where it appears, ordering
the markers is what makes a sequence of snapshots meaningful — `restore:X`
then `save:Y` re-labels a version. A leading `save:` (nothing before it that
could change the flags) reads the flags from the MS on disk, where the run
has not touched them yet; a `save:` further down the list snapshots the run's
in-memory flags — flagging stays in memory until `--apply`/`--msout` writes
it — so it holds what the operations before it produced rather than the
pre-run state.

Whichever it reads, the version covers the whole MS: the in-memory snapshot
is merged over the flags on disk for the rows the run is not holding (a
`--scan` selection), so a version stays restorable even when the run itself
is working on a row selection. Saving a version name that already exists
moves the old one aside as `<name>.old.<timestamp>`, matching `flagmanager`.

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
