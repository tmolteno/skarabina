<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-07-02

### Added

- `skarabina/dask_ms.py` — `summary()` now reports spectral window count, channel count, frequency range, and bandwidth.
- `skarabina/dask_ms.py` — `summary()` now lists all fields with row counts, reading field names from the FIELD subtable. Handles both single-field (attribute) and multi-field (data variable) MS layouts.

### Changed

- `skarabina/dask_ms.py` — `optimize()` now removes fully-flagged channels (all rows × all correlations flagged) in addition to fully-flagged rows. This reduces the channel dimension after `--flag-spectral-window`.
- All `logger.info()` calls replaced with `print()` for clean CLI output without the `INFO:module:` prefix. `logger.debug()` still requires `--debug`.
- Verbose xarray dataset dump and sub-table name listing moved to `debug` level.

### Fixed

- `skarabina/dask_ms.py` — `write_new_ms()` now deep-copies all subtables (SPECTRAL_WINDOW, ANTENNA, etc.) to the output MS. Previously only the main table was written, so `CHAN_FREQ` and other subtable metadata were missing from `--msout`.
- Suppressed casacore C++ stderr noise (`SORT_COLUMNS`, `SORT_ORDER`) during subtable copy unless `--debug` is set.

## [0.5.1] - 2026-07-02

### Changed

- Documentation reorganised into `doc/` directory with cross-linked markdown files (`index.md`, `usage.md`, `AVERAGING.md`, `ANALYZE.md`, `CHANGES.md`).

### Fixed

- `skarabina/dask_ms.py` — `WEIGHT_SPECTRUM` now uses **sum** (not masked mean) when time- or frequency-averaging. Weight w = 1/σ²; combined weight = Σ wᵢ for unflagged visibilities.
- `skarabina/dask_ms.py` — `SIGMA_SPECTRUM` now uses inverse-variance weighting (σ̄ = 1/√(Σ 1/σ²)) rather than a simple mean when time- or frequency-averaging.
- Unit tests added for WEIGHT_SPECTRUM sum (1 test) and SIGMA_SPECTRUM inverse-variance (2 tests).

## [0.5.0] - 2026-07-02

### Added

- `skarabina-analyze` CLI command: analyzes a measurement set and recommends an image size in pixels. Computes angular resolution from max baseline and highest frequency, then recommends dimensions given `--image-fov` (degrees) and `--oversampling-factor` (pixels per synthesised beam, default 5).

## [0.4.1] - 2026-07-02

### Added

- `--frequency-average-factor N` CLI option: averages groups of N consecutive frequency channels. Flagged visibilities are excluded from the mean; FLAG is OR'd. Trailing channels (< N) are combined into a final narrower channel. CHAN_FREQ in the SPECTRAL_WINDOW subtable is updated accordingly.
- Unit tests for frequency averaging logic (7 tests): exact division, trailing channels, flagged exclusion, all-flagged groups, FLAG OR, and CHAN_FREQ averaging.

### Changed

- `--summary` and `--barber` now run after all flagging, averaging, and optimization, so they always reflect the final state of the data.
- Tests split into logical files: `test_schema.py`, `test_barber.py`, `test_integration_time.py`, `test_frequency_average.py`.

### Fixed

- `skarabina/dask_ms.py` — `write_new_ms()` now updates the output SPECTRAL_WINDOW's `CHAN_FREQ` column when channels have been reduced by `--frequency-average-factor` or `--optimize`.
- `skarabina/dask_ms.py` — `__init__()` truncates `CHAN_FREQ` to match the actual data channels if the subtable has more entries (handles pre-fix averaged MS files).
- `skarabina/dask_ms.py` — `frequency_average()` trailing channels are combined into a final narrower channel rather than discarded.

## [0.4.0] - 2026-07-02

### Added

- `--flag-spectral-window` CLI option: YAML-driven frequency flagging with optional per-entry UV constraints. Includes `spectral-flags.example.yml` with band-edge, Galactic HI, and short-baseline RFI rules.
- `--time-average-factor N` CLI option: averages every N consecutive rows (mean for DATA/UVW using only unflagged visibilities, OR for FLAG, sum for INTERVAL). Runs before `--optimize`.
- `--field-of-view` CLI option in degrees (default 1°): sets the half-width from phase centre used in the fringe-rotation integration time limit.
- `skarabina/dask_ms.py` — `summary()` now reports the fringe-rotation max integration time at 1%, 3%, and 5% amplitude loss, using the Wijnholds (2018, MNRAS) formula: Δt_max = c·√(6L) / (π·ω⊕·B_max·ν_max·ℓ).
- `skarabina/dask_ms.py` — `summary()` now reports the current integration time from the MS `INTERVAL` or `EXPOSURE` column.
- `skarabina/dask_ms.py` — `summary()` now includes a row-level flagging histogram (% of unflagged visibilities per row) and a row-size consistency check.
- `AVERAGING.md` documenting the fringe-rotation formula with example values.
- Unit tests for the fringe-rotation integration time formula (11 tests).

### Changed

- `skarabina/dask_ms.py` — `time_average()` averages only unflagged visibilities (flagged entries are excluded from the mean). INTERVAL and EXPOSURE are summed (not averaged).
- `skarabina/main.py` — pipeline reorganized with explicit section markers; `optimize()` and `time_average()` are guaranteed to run after all flagging.

### Fixed

- `skarabina/dask_ms.py` — `time_average()`: multiple fixes for xarray dimension conflicts (isel-first then column replacement with rechunking) and ROWID coordinate subsampling.

## [0.2.4] - 2026-07-02

### Added

- `--version` CLI option that prints the package version and exits.
- Copyright headers on all source files (Tim Molteno, 2025-2026).
- `flake8` linting: dev dependency, `.flake8` config (100-char lines, E203/W503 ignored), and `make lint` target.
- `--flag-spectral-window` CLI option: takes a YAML file defining frequency ranges and optional UV constraints to flag. Includes `spectral-flags.example.yml` with band-edge, Galactic HI, and short-baseline RFI rules.

### Changed

- Default log level changed from `ERROR` to `INFO`. All operational output (`flag_uv_above`, `flag_data`, `flag_spectral_window`, `optimize`, etc.) is now visible without `--debug`.
- `skarabina/dask_ms.py` — `flag_data()` now reports a flag-count summary (flagged / total visibilities with percentage) for NaN and clip operations.
- `skarabina/dask_ms.py` — `flag_uv_above()` now reports max UV distance, rows above the limit, and how many were newly flagged vs already flagged. Labels units as meters.
- `skarabina/dask_ms.py` — `summary()` now includes a row-level flagging histogram (% of unflagged visibilities per row) and a row-size consistency check.
- `skarabina/main.py` — pipeline reorganized with explicit section markers; `optimize()` is guaranteed to run after all flagging operations.

### Fixed

- `skarabina/dask_ms.py` — `optimize()`: switched from per-variable boolean-index assignment to a single `isel` call. The per-variable approach caused xarray dimension conflicts: after the first variable shrank the row dimension, subsequent variables with the old row count were rejected.
- `skarabina/main.py` — suppressed noisy dask-ms `WARNING`/`ERROR` log output for unpopulated MS columns (`MODEL_DATA`, `FLAG_CATEGORY`) by setting the `daskms` logger to `ERROR` level.

## [0.2.3] - 2026-07-02

### Fixed

- `skarabina/dask_ms.py` — `optimize()`: materialize the row keep-mask before filtering columns. Dask boolean indexing produces unknown chunk sizes (`nan`), which xarray rejects at assignment time with "conflicting sizes for dimension 'row'".

## [0.2.2] - 2026-07-02

### Changed

- Switched `dask-ms` dependency from git fork (`tmolteno/dask-ms`) to the standard PyPI release (`dask-ms[xarray,zarr]`).

## [0.2.1] - 2026-07-02

### Fixed

- `skarabina/dask_ms.py` — `optimize()` was broken in two ways:
  - Only `DATA` and `FLAG_ROW` were filtered when removing flagged rows; all other row-indexed columns (`UVW`, `TIME`, `ANTENNA1`, `ANTENNA2`, `FLAG`, `WEIGHT_SPECTRUM`, etc.) were left at their original length, causing a dimension mismatch when writing the output MS.
  - The new `FLAG_ROW` array was created with `da.zeros_like(self.ds.FLAG_ROW)` (original row count) instead of matching the filtered row count.
- `skarabina/dask_ms.py` — `optimize()` now also removes rows where every individual visibility in `FLAG` is set (all channels × correlations flagged), even if `FLAG_ROW` is not explicitly `True`. Previously, rows with `FLAG_ROW=False` but `FLAG=True` everywhere would be retained as noise-only garbage.

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
