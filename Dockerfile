# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
#
# Single Dockerfile for all architectures (x86_64, aarch64 / DGX Spark).
#
# On x86_64, python-casacore installs from a pre-built wheel.
# On aarch64, it builds from source via scikit-build-core with C++17
# (needed because system casacore headers use std::allocator typedefs
#  that C++20 removed).
#
# The package is built from this repository, NOT installed from PyPI.  This is
# deliberate: an image tagged for release vX.Y.Z must contain the code at that
# tag.  Installing `skarabina` from PyPI silently produced images carrying
# whatever version PyPI last had, so a released tag could ship an image without
# the new code (see the 0.7.2 changelog).
#
# Pre-built images: docker pull ghcr.io/tmolteno/skarabina:latest
# Build:  docker build -t skarabina .
# Run (flag):    docker run --rm -it -v $(pwd):/data skarabina skarabina \
#                  --ms /data/foo.ms --summary
# Run (analyze): docker run --rm -it -v $(pwd):/data skarabina \
#                  skarabina-analyze --ms /data/foo.ms --image-fov 2.5

FROM python:3.13-slim

# System dependencies for casacore and building python-casacore from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    casacore-dev \
    gcc g++ \
    libblas-dev liblapack-dev \
    wcslib-dev libcfitsio-dev \
    libboost-python-dev \
    cmake ninja-build curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Force C++17 for architectures where python-casacore builds from source.
# On x86_64 (pre-built wheel) this is ignored.
ENV CMAKE_ARGS="-DCMAKE_CXX_STANDARD=17"

# uv resolves and installs the dependencies
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Metadata first, so the dependency layer is cached across source changes
COPY pyproject.toml README.md /app/
COPY skarabina /app/skarabina

# Build a wheel from this checkout, then install it
RUN uv build --wheel --out-dir /tmp/dist /app \
    && uv pip install --system /tmp/dist/skarabina-*.whl \
    && rm -rf /tmp/dist
