<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Usage

## Command-line options

```
  --ms MS                       Input measurement set (required)
  --summary / --no-summary      Print flagging summary with histogram,
                                field list, spectral window info, and
                                fringe-rotation integration time limits
  --barber / --no-barber        Run barber flagging report
  --barber-pol INTEGER          Polarization for barber
  --flag-uv-above FLOAT         Flag baselines longer than this (metres)
  --flag-nan / --no-flag-nan    Flag NaN visibilities
  --flag-clip TUPLE             Flag visibilities outside [min, max]
  --flag-spectral-window FILE   YAML file with frequency ranges and
                                optional UV constraints to flag
  --time-average-factor INTEGER Average every N rows (see averaging.md)
  --frequency-average-factor INTEGER Average every N channels (see averaging.md)
  --field-of-view FLOAT         Half-width from phase centre (degrees,
                                default 1.0)
  --optimize / --no-optimize    Remove fully-flagged rows and channels.
                                Must run after --time-average-factor and
                                --frequency-average-factor (averaging
                                precedes optimization), and requires
                                --msout or --apply to write the result
  --apply / --no-apply          Modify input MS in place
  --clobber / --no-clobber      Overwrite existing output
  --msout MS                    Output measurement set path
  --split TEXT                  When writing (--msout), keep only this
                                field's rows (field name or numeric
                                FIELD_ID; see splitting.md)
  --debug / --no-debug          Verbose debug output
  --version                     Print version and exit
```

## Examples

### Barber flagging

Generate a report (in the style of barber):

    skarabina --ms foo.ms --barber

### Clip and NaN flagging

Flag in-place:

    skarabina --ms test.ms --flag-nan --flag-clip [0,100] --apply --clobber

Write a new MS:

    skarabina --ms test.ms --flag-nan --flag-clip [0,100] --msout bar.ms --clobber

### Spectral-window flagging

Flag known RFI frequency ranges from a YAML file:

    skarabina --ms test.ms --flag-spectral-window spectral-flags.example.yml --msout cleaned.ms

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

    skarabina --ms raw.ms --flag-nan --msout target.ms --split "Cyg A" --clobber

See [Splitting an MS by field](SPLITTING.md) for details.

### Full pipeline

    skarabina --ms raw.ms \
        --flag-uv-above 4000 \
        --flag-nan \
        --flag-spectral-window spectral-flags.yml \
        --time-average-factor 3 \
        --frequency-average-factor 5 \
        --optimize \
        --field-of-view 1.5 \
        --summary \
        --msout clean.ms \
        --clobber
