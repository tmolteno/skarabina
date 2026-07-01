# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-07-01

### Changed

- Switched dependency management and packaging from Poetry to [uv](https://docs.astral.sh/uv/).
  - Build backend changed from `poetry-core` to `hatchling`.
  - `poetry.lock` replaced by `uv.lock`.
  - `dask-ms` is now declared as a direct git dependency (`git+https://github.com/tmolteno/dask-ms`) with the `xarray` and `zarr` extras.
  - Dev dependency (`pytest`) moved to a `dev` dependency group.
- Console-script entry point moved from `[tool.poetry.scripts]` to standard `[project.scripts]`.
- Bumped version to 0.2.0 (including the Stimela image version in `genesis/skarabina-base.yml`).

### Build / CI

- `Dockerfile` rewritten to use the `ghcr.io/astral-sh/uv` image; installs into the system interpreter so the `skarabina` console script remains on `PATH`.
- `Makefile` install target now runs `uv sync`.
- Release workflow (`.github/workflows/deploy_module.yaml`) now uses `astral-sh/setup-uv` and `uv build` instead of Poetry.
- `README.md` build instructions updated for uv.

### Fixed

- `Dockerfile`: moved an inline `#` comment out of a backslash-continued `ENV` block where it was being appended to `PIP_DEFAULT_TIMEOUT` as a literal value.
- `skarabina/dask_ms.py`: replaced the incorrect `da.array(...)` with `da.asarray(...)` when reading the `UVW` column.
- `skarabina/dask_ms.py`: narrowed a bare `except:` to `except AttributeError` for the `WEIGHT_SPECTRUM` fallback.
- `skarabina/dask_ms.py`: removed the mutable default argument (`operations={}`) from `flag_data`.
- `skarabina/dask_ms.py`: converted operational `print` statements to `logging` calls (`summary` report output is unchanged).
- `skarabina/main.py`: moved `logging.basicConfig()` out of module scope into `main()` and scoped the module logger to `__name__`; debug-only output (`opts`, kwargs) is now emitted at debug level instead of always printing.

### Removed

- `skarabina/hello.py`: deleted orphaned template module with broken imports.
- `skarabina/barber.py`: removed a dead `if False:` block and stale commented-out code.
- `skarabina/skarabina.yml`: removed unimplemented, silently-ignored input options (`flag.zero`, `freq`, `chan-bin`).

### Tests

- `tests/test_null_flagger.py`: rewrote the previously broken stub (wrong import, empty body) into runnable tests that load the cab schema and exercise `barber()` against synthetic in-memory dask arrays (no Measurement Set required).
