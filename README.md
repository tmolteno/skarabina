<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Skarabina

[![PyPI - dask-ms-2](https://img.shields.io/pypi/v/dask-ms-2)](https://pypi.org/project/dask-ms-2/)

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
- [Plotting (skarabina-plotms)](doc/PLOTTING.md)
- [Changelog](doc/CHANGES.md)

## Dependencies

skarabina depends on two packages from the same author:

- [casacure](https://github.com/tmolteno/casacure) — a pure-Rust drop-in for
  python-casacore's table system, and skarabina's default I/O backend.  It
  comes with the package (no system casacore needed), on x86_64 and arm64
  alike.
- [dask-ms-2](https://pypi.org/project/dask-ms-2/) — the
  [tmolteno/dask-ms](https://github.com/tmolteno/dask-ms) fork of dask-ms
  (import package `daskms`, same as upstream), which carries the casacure
  I/O backend and the `DASK_MS_BACKEND` backend selection.

`pip install skarabina` pulls both in automatically (as
`dask-ms-2[casacure,xarray,zarr]` plus `casacure`); the backend is pinned
at import and python-casacore is not a skarabina backend.  See
[INSTALL.md](doc/INSTALL.md) for details.

## Install

    pip install skarabina

See [INSTALL.md](doc/INSTALL.md) for detailed instructions, including
aarch64 / NVIDIA DGX Spark setup.

## Quick start

```sh
# Flag and clean (including autocorrelations)
skarabina --ms raw.ms --flag-autos --flag-nan --flag-uv-above 4000 \
    --time-average-factor 3 --optimize --msout clean.ms --clobber

# Keep a subset of scans, then average
skarabina --ms raw.ms --scan 1,12,14 --flag-nan \
    --frequency-average-factor 8 --msout kept.ms --clobber

# Analyze
skarabina-analyze --ms raw.ms --image-fov 2.5

# Plot a gain table or an MS (casaplotms substitute, arm64-friendly)
skarabina-plotms --ms raw.ms --plotfile amp_vs_time.png --overwrite
```

`--field-of-view` (flagging/summary) and `--image-fov` (analysis) both take the
field of view as a **full width**, so the same value can be passed to both.  The
summary's fringe-rotation limit uses the distance from the phase centre to the
edge, i.e. half of it.

## Build

Install [uv](https://docs.astral.sh/uv/), then:

    uv sync
    uv build

## Stimela

```yaml
_include:
    - (skarabina_cargo):
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
