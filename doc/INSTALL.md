<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Installing skarabina

## Standard install (x86_64, pre-built wheels)

    pip install skarabina

### The Rust I/O backend (default)

casacore is the C++ table library measurement sets are formatted with.
[casacure](https://github.com/tmolteno/casacure) is a pure-Rust drop-in for
it, and it is what skarabina uses by default: importing `skarabina` sets
`DASK_MS_BACKEND=casacure` when the variable is unset, so every entry point
(`skarabina`, `skarabina-analyze`, `skarabina-plotms`) and the test suite
run on it with no environment setup — on x86_64 and arm64 alike, where
python-casacore does not work.  casacure itself comes with the package.

Driving it through dask-ms needs the
[tmolteno/dask-ms](https://github.com/tmolteno/dask-ms) fork — which
`uv sync` in this repository resolves, per `[tool.uv.sources]`.  A pip user
installing skarabina from PyPI should install the fork explicitly:

    pip install "dask-ms[casacure,xarray,zarr] @ git+https://github.com/tmolteno/dask-ms.git"
    pip install skarabina

Set `DASK_MS_BACKEND` yourself only to *override* the default: any value
other than `casacure` selects real python-casacore instead (x86_64 only).
The [`bench/flag_timing.py`](../bench/flag_timing.py) harness passes
`casacure` explicitly for its `casacure` backend.

## Docker (any architecture)

Pre-built multi-arch Docker images are published to the GitHub Container
Registry (GHCR) on every tagged release.  They work on x86_64 and aarch64
(DGX Spark, AWS Graviton, Raspberry Pi) from a single tag.

### Pull the image

```sh
docker pull ghcr.io/tmolteno/skarabina:latest
```

Or pin a specific version:

```sh
docker pull ghcr.io/tmolteno/skarabina:v0.5.1
```

### Run skarabina (flag, summarize, optimize)

```sh
docker run --rm -it -v $(pwd):/data \
    ghcr.io/tmolteno/skarabina:latest run \
    --ms /data/myobs.ms --summary
```

### Run skarabina-analyze (image analysis)

```sh
docker run --rm -it -v $(pwd):/data \
    ghcr.io/tmolteno/skarabina:latest analyze \
    --ms /data/myobs.ms --image-fov 2.5
```

### Typical flagging workflow

```sh
# 1. Summarise the measurement set
docker run --rm -it -v $(pwd):/data \
    ghcr.io/tmolteno/skarabina:latest run \
    --ms /data/myobs.ms --summary

# 2. Flag and write a new measurement set
docker run --rm -it -v $(pwd):/data \
    ghcr.io/tmolteno/skarabina:latest run \
    --ms /data/myobs.ms \
    --flag "nan, clip 0 10, uv-above 250" \
    --msout /data/myobs_flagged.ms --clobber
```

The first argument must be `run` (for `skarabina`) or `analyze` (for
`skarabina-analyze`).  All remaining arguments are forwarded to that
command.  Mount your data directory with `-v` so paths inside the
container can reach your measurement sets.

### Build locally

```sh
docker build -t skarabina .
```

A single Dockerfile supports all architectures — on x86_64 it uses a
pre-built `python-casacore` wheel; on aarch64 it builds from source
via scikit-build-core with C++17.  The runtime backend is casacure either
way (see above); the image's python-casacore only matters on x86_64, and
only if you override `DASK_MS_BACKEND`.

## aarch64 (NVIDIA DGX Spark, Raspberry Pi, AWS Graviton)

Nothing extra to install: casacure is the default backend and installs
from wheels on every architecture, so `pip install skarabina` — or the
multi-arch Docker image — is all an arm64 host needs.

python-casacore (the C++ binding) does not work on arm64, so there is
deliberately no python-casacore path here anymore: on arm64 leave
`DASK_MS_BACKEND` at its default (`casacure`).  (This section used to
describe building python-casacore from source with C++17 flags; that build
is unsupported and the backend it would select is not the one skarabina
runs.)

## Development install

    git clone https://github.com/tmolteno/skarabina
    cd skarabina
    uv sync
    uv run skarabina --help

`uv sync` installs the casacure pinned in `uv.lock`.  The casacure fixes
`CHANGES.md` lists (`table.removerows`, `addcols(dminfo)`, and the
read-open fallback for an unflushable writer) are in the casacure checkout
but not yet in a released wheel, so until that release exists install it
into the venv as well:

    uv pip install ../casacure
