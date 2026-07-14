<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Splitting an MS by field (`--split`)

## What it does

`--split` writes an output measurement set that contains **only the rows of a
single field**, discarding all others.  This is useful when an MS holds
observations of several fields (e.g. a target plus calibrators) and you want a
self-contained MS for just one of them.

The option takes either a field **name** or a numeric **FIELD_ID**:

```sh
skarabina --ms raw.ms --msout target.ms --split "Cyg A"
skarabina --ms raw.ms --msout target.ms --split 2          # FIELD_ID = 2
```

Field names are matched against the `NAME` column of the input MS's `FIELD`
subtable.  A purely numeric value is interpreted as a `FIELD_ID` rather than a
name.  If the field cannot be found, skarabina exits with an error that lists
the available fields.

## It only affects the output

Splitting is a **save-time** operation.  The full input MS is processed
normally first — all flagging, spectral-window flagging, averaging, and
optimization run across every field — and only the rows of the selected field
are written out:

```
flagging → frequency-average → time-average → optimize → summary → split → write
```

This means `--summary` (and `--barber`) still report on the entire input MS,
which is usually what you want when deciding what to keep.  `--split` is only
honoured when writing a new MS with `--msout`; it has no effect with `--apply`
(in-place updates can only change columns, not drop rows).

To discover the field names and IDs in an MS before splitting, run:

```sh
skarabina --ms raw.ms --summary
```

The summary's *Fields* section lists each `FIELD_ID`, its name, and row count.

## What is kept

Only the **row dimension** is filtered — every column belonging to the selected
rows (DATA, FLAG, UVW, WEIGHT_SPECTRUM, TIME, etc.) is carried over unchanged.
Flagging and averaging applied before the split are preserved in the output.

The subtables (`FIELD`, `SPECTRAL_WINDOW`, `ANTENNA`, `FEED`, …) are copied
from the input MS as-is.  An output MS may therefore contain `FIELD` entries
that no rows reference; this is harmless and normal — the rows simply point at
the single selected `FIELD_ID`.

## Single-field vs multi-field MS

skarabina handles two layouts transparently:

| Layout | Where FIELD_ID lives | `--split` behaviour |
|--------|----------------------|---------------------|
| Multi-field | Per-row `FIELD_ID` data variable | Selects the matching rows |
| Single-field | Dataset `FIELD_ID` attribute | Keeps the data if it matches, errors otherwise |

A multi-field MS (the common case for splitting) stores a `FIELD_ID` per row,
so the split selects exactly those rows.  A single-field MS has no per-row
`FIELD_ID` to filter on; in that case `--split` succeeds only if the requested
field is the one the MS already contains.

## Examples

Split a calibrator out of a multi-field MS, after flagging:

```sh
skarabina --ms raw.ms \
    --flag-nan --flag-uv-above 4000 \
    --msout calibrator.ms --split "3C 286" --clobber
```

Split by numeric field ID, then average and optimize the result:

```sh
skarabina --ms raw.ms \
    --time-average-factor 3 --optimize \
    --msout target.ms --split 0 --clobber
```
