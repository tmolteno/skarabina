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

# On non-x86 architectures (e.g. aarch64) numcodecs has no pre-built Linux
# wheel and must be compiled from source.  Under QEMU emulation (docker
# buildx), py-cpuinfo detects the host's x86_64 CPU features, causing
# setup.py to add -msse2/-mavx2 which the aarch64 compiler rejects.
# The DISABLE_NUMCODECS_* env vars do not help — they flip the flags to
# -mno-sse2/-mno-avx2 which are also x86-only.
#
# Fix: set CFLAGS.  numcodecs' setup.py skips its SIMD flag logic entirely
# when CFLAGS is present in the environment ("respect compiler options set
# by user").  Pre-install numcodecs first so subsequent pip install skarabina
# sees it already satisfied and does not rebuild it.
RUN CFLAGS="-O2" pip install --no-cache-dir numcodecs && \
    pip install --no-cache-dir python-casacore skarabina

COPY docker-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["run", "--help"]
