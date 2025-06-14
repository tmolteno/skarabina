FROM python:3.11-slim AS base
RUN apt update && apt install -y curl

ENV PYTHONFAULTHANDLER=1 \
  PYTHONUNBUFFERED=1 \
  PYTHONHASHSEED=random \
  PIP_NO_CACHE_DIR=off \
  PIP_DISABLE_PIP_VERSION_CHECK=on \
  PIP_DEFAULT_TIMEOUT=100 \
  # Poetry's configuration:
  POETRY_NO_INTERACTION=1 \
  POETRY_VIRTUALENVS_CREATE=false \
  POETRY_CACHE_DIR='/var/cache/pypoetry' \
  POETRY_HOME='/usr/local' \
  POETRY_VERSION=2.1.3

RUN curl -sSL https://install.python-poetry.org | python3 -

WORKDIR /code
COPY poetry.lock pyproject.toml .
RUN poetry install --no-root

ADD skarabina/ skarabina/
COPY README.md ./
RUN ls -l
RUN poetry install

ENTRYPOINT ["/bin/bash"]
