<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Installing skarabina

## Standard install (x86_64, pre-built wheels)

    pip install skarabina

## Docker (any architecture)

Pre-built Docker images are available on GHCR.  Choose the one that
matches your architecture:

```sh
# x86_64
sudo docker run --rm -it -v $(pwd):/data \
    ghcr.io/tmolteno/skarabina:latest run --help

# aarch64 (DGX Spark, AWS Graviton, Raspberry Pi)
sudo docker run --rm -it -v $(pwd):/data \
    ghcr.io/tmolteno/skarabina:latest-conda analyze --help
```

The first argument to the container must be `run` (for `skarabina`) or
`analyze` (for `skarabina-analyze`).  All remaining arguments are
forwarded to that command.

To build the images locally:

```sh
# x86_64 (uses pre-built wheels)
docker build -t skarabina .

# aarch64 via conda (pre-built binaries)
docker build -f Dockerfile.conda -t skarabina .

# any architecture, build python-casacore from source (scikit-build-core)
docker build -f Dockerfile.source -t skarabina .
```

## aarch64 (NVIDIA DGX Spark, Raspberry Pi, AWS Graviton)

`python-casacore` has no pre-built aarch64 wheel.  The simplest
approach is to use conda (which has one), then pip-install skarabina.

First install conda if you don't have it:

    # Download and install Miniconda (lightweight conda)
    wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-aarch64.sh
    bash Miniconda3-latest-Linux-aarch64.sh  # follow prompts, accept defaults

Then:

    conda install -c conda-forge python-casacore
    pip install skarabina

If pip tries to build `numcodecs` or `psutil` from source and fails
with `command 'gcc' failed: No such file or directory`, install
compilers first:

    conda install -c conda-forge compilers

### Building from source (if conda is unavailable)

Install system dependencies:

```sh
sudo apt-get install casacore-dev python3-dev gcc g++ \
    libblas-dev liblapack-dev wcslib-dev libcfitsio-dev \
    libboost-python-dev
```

Then:

```sh
pip install skarabina
```

If the build fails with C++ allocator errors like:

```
error: no type named 'pointer' in 'casacore::casacore_allocator<...>::Super'
error: 'struct casacore::Allocator_private::BulkAllocator<...>' has no member named 'destroy'
```

your system casacore was compiled with an older C++ standard than the
one `python-casacore` needs (C++17 removed `pointer`, `reference`,
`rebind` etc. from `std::allocator`; they were deprecated in C++17 and
removed in C++20).  Work around it by forcing C++17:

```sh
CMAKE_ARGS="-DCMAKE_CXX_STANDARD=17" pip install python-casacore
pip install skarabina
```

This tells scikit-build-core (the build backend `python-casacore` uses)
to compile with C++17 where those allocator typedefs still exist.

Alternatively, use conda or the `Dockerfile.source` which sets this flag
automatically, or install casacore from source with `-std=c++17`.

## Development install

    git clone https://github.com/tmolteno/skarabina
    cd skarabina
    uv sync
    uv run skarabina --help
