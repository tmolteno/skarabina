<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Skarabina

```
 =,                .=
=.|      ,---.      |.=
=.|   "-(:::::)-"   |.=
   \\__/`-.|.-'\__//
    `-| .::| .::|-'      Pillendreher
     _|`-._|_.-'|_       (Scarabaeus sacer)
   /.-|    | .::|-.\
  // ,| .::|::::|. \\
 || //\::::|::' /\\ ||
 /'\|| `.__|__.' ||/'\
^    \\         //    ^
     /'\       /'\
    ^             ^
```

skarabina: a basic 1GC radio astronomy RFI flagger

Author: Tim Molteno (tim@elec.ac.nz)

Intended to reduce I/O costs by performing the standard flagging during
1GC efficiently, and with low memory requirements. Skarabina can also do averaging to reduce data volume, and measurement set analysis to recommend image size.

## Documentation

See [doc/](doc/index.md) for full documentation:

- [Usage & CLI reference](doc/usage.md)
- [Time & frequency averaging](doc/AVERAGING.md)
- [Measurement set analyzer](doc/ANALYZE.md)
- [Changelog](doc/CHANGES.md)

## Install

    pip install skarabina

On **aarch64** (e.g. NVIDIA DGX Spark, Raspberry Pi, AWS Graviton),
`stimela` and `python-casacore` pull in packages that need compilation.
Install build tools and casacore development headers first:

    sudo apt-get install python3-dev gcc casacore-dev
    pip install skarabina

If `casacore-dev` is not available for your distribution, build
casacore from source: https://github.com/casacore/casacore

## Quick start

```sh
# Flag and clean
skarabina --ms raw.ms --flag-nan --flag-uv-above 4000 \
    --time-average-factor 3 --optimize --msout clean.ms --clobber

# Analyze
skarabina-analyze --ms raw.ms --image-fov 2.5
```

## Build

Install [uv](https://docs.astral.sh/uv/), then:

    uv sync
    uv build

## Stimela

```yaml
_include:
    - (skarabina):
        - skarabina.yml

my-recipe:
    info: "Print a flagging summary using skarabina"
    inputs:
        ms: MS
    steps:
        flag-summary:
            cab: skarabina
            params:
                ms: =recipe.ms
                summary: true
```
