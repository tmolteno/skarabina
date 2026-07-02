<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Skarabina

```
=,             .=
=.|    ,---.    |.=
=.| "-(:::::)-" |.=
\\__/`-.|.-'\__//
 `-| .::| .::|-'      Pillendreher
  _|`-._|_.-'|_       (Scarabaeus sacer)
/.-|    | .::|-.\
// ,| .::|::::|. \\
|| //\::::|::' /\\ ||
/'\|| `.__|__.' ||/'\
^    \\         //    ^
jrei   /'\       /'\
 ^             ^
```

skarabina: a basic 1GC radio astronomy RFI flagger

Author: Tim Molteno (tim@elec.ac.nz)

Intended to reduce I/O costs by performing the standard flagging during 1GC efficiently, and with low memory requirements. Skarabina is named after the latin name Skarabinae, the genus of many interesting beetles (uncluding the dungbeetle) in Africa.

## Install

    pip install skarabina

## skarabina-analyze

Analyze a measurement set and recommend an image size:

    skarabina-analyze --ms foo.ms --image-fov 2.5 --oversampling-factor 5

Computes the angular resolution from the longest baseline and highest
frequency, then recommends pixel dimensions given the field-of-view and
oversampling factor (pixels per synthesised beam).

## Usage

### Command-line options

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
  --time-average-factor INTEGER Combine every N consecutive rows by
                                averaging (before --optimize)
  --frequency-average-factor INTEGER Combine every N consecutive frequency
                                channels by averaging
  --field-of-view FLOAT         Half-width from phase centre in degrees
                                (default 1.0).  Used for fringe-rotation
                                integration time limit.
  --optimize / --no-optimize    Remove fully-flagged rows and channels
  --apply / --no-apply          Modify input MS in place
  --clobber / --no-clobber      Overwrite existing output
  --msout MS                    Output measurement set path
  --debug / --no-debug          Verbose debug output
  --version                     Print version and exit
```

### Barber flagging

Generate a report (in the style of barber).

    skarabina --ms foo.ms --barber
    
### Clip and Nan Flagging

This will flag a measurement set, and modify it in-place.

    skarabina --ms test.ms --flag-nan --flag-clip [0,100] --apply --clobber
    
The following will write a new measurement set.

    skarabina --ms test.ms --flag-nan --flag-clip [0,100] --apply --clobber --msout bar.ms

### Spectral-window flagging

Flag known RFI frequency ranges from a YAML file:

    skarabina --ms test.ms --flag-spectral-window spectral-flags.example.yml --msout cleaned.ms

See `spectral-flags.example.yml` for the format — a list of entries with
`spw` frequency ranges in MHz and optional `uv_below` / `uv_above` constraints.

### Time averaging

Reduce data volume by averaging consecutive integrations:

    skarabina --ms test.ms --time-average-factor 4 --optimize --msout averaged.ms

This averages every 4 rows (mean for DATA/UVW, OR for FLAG, sum for INTERVAL),
discarding any trailing rows that don't form a complete block.

### Full pipeline example

    skarabina --ms raw.ms \
        --flag-uv-above 4000 \
        --flag-nan \
        --flag-spectral-window spectral-flags.yml \
        --time-average-factor 3 \
        --optimize \
        --field-of-view 1.5 \
        --summary \
        --msout clean.ms \
        --clobber

    
## Build

Install [uv](https://docs.astral.sh/uv/), then sync the project (creates a virtual
environment and installs skarabina in editable mode):

    uv sync

Build distributables:

    uv build

## Stimela

skarabina is available as a stimela package. You can include it in your pipeline (after installing skarabina) thusly

    _include:
        - (skarabina):
            - skarabina.yml
            
    my-recpipe:
        info: "Print a flagging summary using skarabina"
        inputs:
            ms: MS

        steps:
            flag-summary:
                cab: skarabina
                params:
                    ms: =recipe.ms
                    summary: true
