<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Installing skarabina

## Standard install (x86_64, pre-built wheels)

    pip install skarabina

## aarch64 (NVIDIA DGX Spark, Raspberry Pi, AWS Graviton)

`python-casacore` has no pre-built aarch64 wheel.  The simplest
approach is to use conda (which has one), then pip-install skarabina:

    conda install -c conda-forge python-casacore
    pip install skarabina

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

If the build fails with C++ allocator errors (`no type named 'pointer'`),
your system casacore was compiled with a different C++ standard than
`python-casacore` expects.  This is an upstream incompatibility — use
conda instead.

## Development install

    git clone https://github.com/tmolteno/skarabina
    cd skarabina
    uv sync
    uv run skarabina --help
