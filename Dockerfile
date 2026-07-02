# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
#
# Build:  docker build -t skarabina .
# Run:    docker run --rm -it -v $(pwd):/data skarabina \
#           --ms /data/foo.ms --summary
#
# On aarch64 (DGX Spark, Graviton), use the conda-based Dockerfile instead:
#   docker build -f Dockerfile.conda -t skarabina .

FROM python:3.11-slim

# System dependencies for casacore and its Python bindings
RUN apt-get update && apt-get install -y --no-install-recommends \
    casacore-dev \
    gcc g++ \
    libblas-dev liblapack-dev \
    wcslib-dev libcfitsio-dev \
    libboost-python-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir python-casacore skarabina

COPY docker-entrypoint.sh /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]
