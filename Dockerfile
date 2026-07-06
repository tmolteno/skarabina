# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
#
# Single Dockerfile for all architectures (x86_64, aarch64 / DGX Spark).
#
# On x86_64, python-casacore installs from a pre-built wheel.
# On aarch64, it builds from source via scikit-build-core with C++17
# (needed because system casacore headers use std::allocator typedefs
#  that C++20 removed).
#
# Pre-built images: docker pull ghcr.io/tmolteno/skarabina:latest
# Build:  docker build -t skarabina .
# Run (flag):    docker run --rm -it -v $(pwd):/data skarabina run \
#                  --ms /data/foo.ms --summary
# Run (analyze): docker run --rm -it -v $(pwd):/data skarabina analyze \
#                  --ms /data/foo.ms --image-fov 2.5

FROM python:3.13-slim

# System dependencies for casacore and building python-casacore from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    casacore-dev \
    gcc g++ \
    libblas-dev liblapack-dev \
    wcslib-dev libcfitsio-dev \
    libboost-python-dev \
    cmake ninja-build \
    && rm -rf /var/lib/apt/lists/*

# Force C++17 for architectures where python-casacore builds from source.
# On x86_64 (pre-built wheel) this is ignored.
ENV CMAKE_ARGS="-DCMAKE_CXX_STANDARD=17"

RUN pip install --no-cache-dir python-casacore skarabina

COPY docker-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["run", "--help"]
