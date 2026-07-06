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

# On arm64, numcodecs has no pre-built wheel and must build from source.
# Under QEMU (docker buildx), py-cpuinfo detects the host x86_64 CPU flags
# (sse2/avx2), causing setup.py to include x86-specific compiler flags,
# macros, and source files — all rejected by aarch64 gcc.
#
# Fix requires three things together:
#   1. CFLAGS   — prevents setup.py from adding -msse2/-mavx2/-mno-* flags
#   2. DISABLE_NUMCODECS_SSE2/AVX2 — prevents -DSHUFFLE_* macros and
#      x86-specific source files (bitshuffle-avx2.c etc.)
#   3. --no-build-isolation — pip's isolated build env does not pass through
#      arbitrary env vars like DISABLE_NUMCODECS_*, so we pre-install build
#      deps and disable isolation.
#
# On x86_64, numcodecs has a pre-built wheel so this path is never hit.
RUN pip install --no-cache-dir cython "numpy>=2" py-cpuinfo "setuptools>=64" && \
    CFLAGS="-O2" DISABLE_NUMCODECS_SSE2=1 DISABLE_NUMCODECS_AVX2=1 \
    pip install --no-cache-dir --no-build-isolation numcodecs && \
    pip install --no-cache-dir python-casacore skarabina

COPY docker-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["run", "--help"]
