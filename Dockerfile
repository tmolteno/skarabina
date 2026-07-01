FROM python:3.11-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# uv for dependency management and packaging
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# uv configuration:
#   Install into the system interpreter so the `skarabina` console script is on PATH.
ENV PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_LINK_MODE=copy

WORKDIR /code

# Resolve and install dependencies first for better layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Add the project source and install it.
ADD skarabina/ skarabina/
COPY README.md ./
RUN uv sync --frozen

ENTRYPOINT ["/bin/bash"]
