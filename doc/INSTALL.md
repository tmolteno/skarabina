<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Installing skarabina

## Standard install (x86_64, pre-built wheels)

    pip install skarabina

## Docker (any architecture)

Pre-built multi-arch Docker images are available on GHCR:

```sh
docker run --rm -it -v $(pwd):/data \
    ghcr.io/tmolteno/skarabina:latest run --help
```

The first argument must be `run` (for `skarabina`) or `analyze` (for
`skarabina-analyze`).  All remaining arguments are forwarded.

To build locally:

```sh
docker build -t skarabina .
```

A single Dockerfile supports all architectures — on x86_64 it uses a
pre-built `python-casacore` wheel; on aarch64 it builds from source
via scikit-build-core with C++17.

## aarch64 (NVIDIA DGX Spark, Raspberry Pi, AWS Graviton)

`python-casacore` has no pre-built aarch64 wheel, but builds from
source successfully.  Install system dependencies first:

```sh
sudo apt-get install casacore-dev python3-dev gcc g++ \
    libblas-dev liblapack-dev wcslib-dev libcfitsio-dev \
    libboost-python-dev cmake ninja-build
```

Then:

```sh
CMAKE_ARGS="-DCMAKE_CXX_STANDARD=17" pip install python-casacore
pip install skarabina
```

The `CMAKE_ARGS` tells scikit-build-core (the CMake build backend) to
compile with C++17.  This is needed because system casacore headers
reference `std::allocator::pointer` / `::const_pointer` / `::reference`
typedefs that were deprecated in C++17 and removed in C++20.

### If the C++ allocator build fails

If you see errors like:

```
error: no type named 'pointer' in 'casacore::casacore_allocator<...>::Super'
error: 'struct casacore::Allocator_private::BulkAllocator<...>' has no member named 'destroy'
```

the `CMAKE_ARGS` flag above resolves them.  If it persists, your
casacore package may need updating (`apt-get update`), or you can
build casacore from source with `-std=c++17`.

## Development install

    git clone https://github.com/tmolteno/skarabina
    cd skarabina
    uv sync
    uv run skarabina --help
