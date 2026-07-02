# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
#
# x86_64 Dockerfile.  Uses system casacore packages and pip wheels.
#
# Build:  docker build -t skarabina .
# Run (flag):    docker run --rm -it -v $(pwd):/data skarabina run \
#                  --ms /data/foo.ms --summary
# Run (analyze): docker run --rm -it -v $(pwd):/data skarabina analyze \
#                  --ms /data/foo.ms --image-fov 2.5
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
CMD ["run", "--help"]
