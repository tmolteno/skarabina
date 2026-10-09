# Agent Instructions

## Package structure

The repo contains two Python packages:

| Package | Path | Purpose |
|---|---|---|
| `skarabina` | `./` (root) | CLI tool (`skarabina`, `skarabina-analyze`) |
| `skarabina-cargo` | `./cargo/` | Stimela cab definitions |

`skarabina-cargo` depends on `stimela` and packages the cab schema
(`skarabina.yml`), base vars (`genesis/skarabina-cargo-base.yml`),
and container config (`stimela.conf`).
`skarabina` has no dependency on `stimela` — all CLI parameters are
defined as explicit `@click.option` decorators in `main.py`.

## Releasing: `invoke release`

The release is one command from the repo root (`tasks.py`, the same
plumbum-based orchestration `../casacure` uses):

    invoke version                    # show what a release would do
    invoke test                       # the gate: pytest + flake8, via uv --frozen
    invoke release                    # gate, bump, stamp, commit, tag, push, wait

`invoke release` runs the gate first (a CI failure becomes a local failure
before any tag exists), then writes the release commit itself and pushes it
through to PyPI.  In detail:

1. bump the patch version in every file of the checklist below, move
   `doc/CHANGES.md`'s `## [Unreleased]` entries under a new `## [X.Y.Z]`
   heading (a fresh empty `## [Unreleased]` stays on top), and commit as
   `chore(release): X.Y.Z`.  `--no-bump` tags the current version as-is;
   `--version X.Y.Z` tags that exact version.
2. tag `vX.Y.Z` (annotated, message `skarabina X.Y.Z`) and push `main` plus
   the tag — refusing on a dirty tree, skipping a tag already on origin,
   and pushing a local tag whose push previously failed (a re-run after an
   interrupted release; a local tag at a different commit is refused with
   the commit to inspect).
3. wait for the tag's three workflows (`.github/workflows/`): PyPI
   `skarabina` (`deploy_module.yaml`), PyPI `skarabina-cargo`
   (`cargo-publish.yml`) and the Docker image (`docker-publish.yml`).
4. confirm both packages list the version on PyPI
   (`https://pypi.org/pypi/<package>/json`).

What the bump rewrites (all of it must carry the same version number, and
`tests/test_release_tasks.py` fails the gate when the tree disagrees):

| File | Field |
|---|---|
| `pyproject.toml` | `project.version` |
| `cargo/pyproject.toml` | `project.version` |
| `cargo/skarabina_cargo/genesis/skarabina-cargo-base.yml` | `vars.skarabina-cargo.images.version` |
| `uv.lock` | the `version` under `name = "skarabina"` and `name = "skarabina-cargo"` |

Do **not** include a `v` prefix in the YAML version — CI's
`docker/metadata-action` uses `type=semver` which strips the `v` from the
git tag, so the published Docker image tag is `0.6.2`, not `v0.6.2`.  The
YAML value must match the image tag exactly.

Do not create or use a `CHANGES.md` at the top level — the canonical
changelog lives under `doc/`, and entries accumulate under `## [Unreleased]`
between releases so `invoke release` can stamp them.

## Docker

- The single `Dockerfile` builds for `linux/amd64` and `linux/arm64`.
- CI (`.github/workflows/docker-publish.yml`) builds and pushes on
  `v*.*.*` tags only.

## Benchmarks (flagging timing)

`bench/` is the local test-bench for skarabina's flagging performance.  It is
tracked; the measurement set it runs on is not.

| File | Purpose |
|---|---|
| `bench/flag_timing.py` | the driver: times skarabina flag runs, writes JSON + Markdown |
| `bench/meerkat-flags.yml` | the flag list it runs, with the `../meerkat_imaging` provenance of every entry |
| `bench/spectral-flags-L.yml` | copy of `../meerkat_imaging/spectral-flags-L.yml`, used by the `spectral-window` entry |
| `bench/run_stage.py` | child process: fixes the casacure/casacore import order, then runs the CLI |
| `bench/make_synthetic_ms.py` | writes a bpcal-shaped synthetic MS (79 chan x 2 corr, ~69 % pre-flagged) for hosts without `bpcal.ms` |
| `bench/mem_recal.py` | times a flag list at fixed row chunks, beside the plan's estimate, to re-fit `skarabina.memory`'s constants |

The workload is the stage-0 flag sequence of `../meerkat_imaging`
(`white-belt-0-flagging.yml`, step `flag-average`) — `save:imported`, `autos`,
`uv-above 8000`, `nan`, `clip 0 100`, `spectral-window` — plus the `rflag` verb
that replaces the recipe's CASA autoflag loop over the calibrator fields.  It
runs against `.bench/data/bpcal.ms`, the local (gitignored, ~1.6 GB) copy of a
real MeerKAT bandpass-calibrator MS:

```
python bench/flag_timing.py                     # seq, casacure, bpcal.ms
python bench/flag_timing.py --mode both         # + one process per operation
python bench/flag_timing.py --ms .bench/ms_cure_2k.ms --drop-save-imported
python bench/flag_timing.py --backend casacore --rflag-args "winsize=5"
```

- `--mode seq` (default) runs the whole list in one process — the number the
  pipeline sees.  `--mode per-op` runs each operation in its own process from
  the same input flags, so the cost is attributable; `save:imported` there is
  the I/O floor of the table.
- Each run writes only the flag columns (`--write-changed-only` hard-links the
  unchanged blocks) and deletes its output MS afterwards.  A list containing
  `save:imported` rotates the input's saved flag version and leaves the old one
  in `<ms>.flagversions` (~18 MB for bpcal.ms); `--drop-save-imported` skips it.
- `--write-changed-only` had a sharp edge, fixed after 1.0.5 (issue #3,
  `_ensure_writable` in `dask_ms.py`): hard-linking a block and chmod'ing it
  read-only also made the *input's* `table.fN` read-only (same inode), and the
  next run copied such a block with `shutil.copy2` — which preserves the mode —
  so it died with `storage error: Permission denied` on the first column it
  wrote.  The bench still restores owner-write on the input's blocks before
  every run (`--repair-input-perms`, the default), which is what an MS left
  read-only by 1.0.5 or earlier needs; `--no-repair-input-perms` leaves the
  modes alone, and is how the failure was — and can still be — reproduced.
- Results land in `bench/results/` (gitignored) as JSON + Markdown.  The
  numbers worth quoting by hand go into `BENCHMARKS.md` at the repo root, with
  the host, date, backend and workload they were measured on.
- Re-run the bench and refresh `BENCHMARKS.md` whenever a change claims to make
  flagging faster, and keep `bench/meerkat-flags.yml` in step with the
  `flag-average` step of `../meerkat_imaging/white-belt-0-flagging.yml`.

## Benchmarking

The canonical end-to-end benchmark is a flag-and-frequency-average run over a
real MeerKAT MS, run from `../meerkat_imaging` because the input MS and the
spectral-flags file live there (`ms-orig` is in `../meerkat_imaging`):

```sh
cd ../meerkat_imaging
DASK_MS_BACKEND=casacure \
  /home/tim/github/skarabina/.venv/bin/skarabina \
    --ms ms-orig/mergA_tim.ms \
    --scan 1,12,14,19,21,28,29,33,41,43,53,54,56,58,63 \
    --summary --time-average-factor 1 --frequency-average-factor 32 \
    --clobber \
    --flag save:imported --flag autos --flag "uv-above 2500" --flag nan \
    --flag "clip 0 100" --flag "spectral-window spectral-flags-L.yml" \
    --field-of-view 3.3deg \
    --msout bench_ave.ms
```

Notes on this command:

- **`DASK_MS_BACKEND=casacure` is required** — dask-ms only aliases
  `casacore` -> `casacure` when this is set (see `daskms.casacure_backend`).
  Without it skarabina cannot import (there is no python-casacore in the
  benchmark venv, and `from casacore.tables import table` must resolve to
  casacure).
- **Quote the multi-token `--flag` entries.** `--flag "uv-above 2500"` etc.
  are each a single argument; unquoted, click sees the trailing tokens
  (`2500 0 100 ...`) as stray positional arguments and exits with
  `Got unexpected extra arguments`.
- The benchmark writes **`bench_ave.ms` in the run directory** (here
  `../meerkat_imaging`). It is a multi-GB output; **remove it after
  benchmarking** (`rm -rf ../meerkat_imaging/bench_ave.ms`). Do not commit
  it, and check it is cleaned up before sharing the results.
- The machine is usually busy (DDFacet imaging runs saturate all cores); a
  run `uptime` at start/end, and the note that timings are load-dependent,
  should accompany any reported numbers.

### Memory-bounding levers

The dask/task phases (flagging, averaging, writing the reduced MS) are row
chunked, so one row-chunk is materialised per dask worker at a time.  Two
options control the concurrent-chunk working set (mirroring tricolour's
`--row-chunks` / `--nworkers`):

- `--row-chunk N` — bytes per chunk are roughly `N * nchan * ncorr * 8` for
  DATA; lower it to shrink each chunk.  When not given it is chosen by
  `skarabina.memory.plan` from the `--flag` list and `--memory-limit-GB`
  (default 0 = the RAM available): the largest chunk keeping every verb's
  `fixed + workers * N * nchan * ncorr * bytes_per_vis` within 80 % of the
  limit.  The per-verb constants (`CHUNK_COST`, `TABLE_COST`) are measured
  (doc/RFLAG.md §7.3-7.4) and were re-measured on 2026-09-26 for casacure
  3.8.9's streamed writes (`bench/mem_recal.py`; BENCHMARKS.md has the table
  and the residuals -- the plan over-predicts at large chunks and
  under-predicts when `--workers` is lowered); re-measure them when a change
  alters a verb's working set.  `save:` and a full `--msout` write are
  whole-table steps only on a backend that buffers a written table (casacure
  <= 3.8.7); the plan prints which backend it assumed.
- A run reads DATA once: every verb's statistics are queued (`DaskMS._report`)
  and computed in the run's single pass -- the full write when there is one
  (averaging and the summary included), else `materialise_flags`, else
  `flush_reports`.  rflag/tfcrop join the flag program (below) or stay lazy.
  Keep new verbs on `_report`; `tests/test_single_pass.py` counts DATA reads
  (the flag program's child processes included) and fails on a second pass.
  One documented exception: a *full* `--msout` write with an auto-flagger in
  the list reads DATA twice (workers once, the write's own column once); a
  flags-only write never does.
- `--workers N` (default 0 = all cores) — the number of dask threads, i.e. the
  number of chunks materialised concurrently, **and** the number of the flag
  program's worker processes.  Cap it to the cores actually free: the threaded
  path self-capped at ~2-3 cores (the GIL), the program does not, and on a
  loaded host 8 default workers oversubscribe.

`rflag`/`tfcrop` (`DaskMS._run_autofit`) run once per dask row chunk.  On the
CLI path they normally join the **flag program**
(`skarabina/flag_program.py`): the block-replayable verbs record into it and
forked worker processes evaluate it per block -- each child opens the input
read-only with its own casacure handle (closed again at the end of the block:
a parked read handle holds the lock `--apply`'s write then waits on), reads
its rows, runs the same block functions, and packs its flags at one bit per
visibility into a shared-memory buffer (unlinked with the `DaskMS`; the
memory plan prints its size).  The GIL is per process, so this is what makes
the auto-flaggers scale with workers.  `SKARABINA_FLAG_POOL=0`, `extend`
before the auto-flagger, a single block or a single worker fall back to the
lazy in-process path; `tests/test_flag_program.py` asserts both write the
same flags.  Without the program, the eager path spills each block's flags to
`.skarabina-spill-*` in `$TMPDIR` if set, else beside the input MS (never
`/tmp` by default — often tmpfs), and later passes read them back; the
directory is removed with the `DaskMS` instance.  Inside a block,
`rflag.GROUP_VALUES` / `tfcrop.GROUP_VALUES` cap the vectorised temporaries
(the per-lane medians come from `skarabina/nanstats.py`).

The row chunk is applied at read time in `DaskMS.__init__`
(`xds_from_ms(..., chunks={"row": row_chunk})`) and the pool is set in
`main()` (`dask.config.set(pool=ThreadPool(workers))`).

Caveat (measured 2026-09-25, casacure 3.8.7): on the benchmark MS, *neither*
lever moved the overall peak RSS.  The peak was set by `--flag save:imported`:
casacure buffered the whole written table, so the backup of the 8 GB flag cube
peaked at ~18-19 GB, and a full `--msout` at ~56 B per output visibility.
Since casacure `d016b8d` (2026-09-26, unreleased after 3.8.7) grows a written
table in place, both are chunk-bounded.  `flag_versions.save_version_streaming`
flushes per 20 000-row chunk.  On scan 1 the stage-0 + averaged `--msout` run
peaks at 7.4 GB, set by the per-chunk working set (12 workers x 11 977 rows),
so the levers bound it again.  `memory.writes_stream()` tells the plan which
kind of backend is installed.

### Performance work (2026-09-25)

The benchmark originally could not finish under the casacure backend.  Fixes
landed in this repo and in `../casacure`:

- `skarabina/dask_ms.py` (+ `analyze.py`, `flag_versions.py`): import `daskms`
  **before** `casacore.tables` so the `DASK_MS_BACKEND=casacure` aliasing is
  installed by the time casacure is imported (previously
  `ModuleNotFoundError: casacore`).
- `casacure …/tsm.rs` `parse_header`: the TSM file length is read with the
  TSM-file object version (u64 when the tile file is >= 2 GiB), not the outer
  TiledStMan header version — the old code misaligned the header and allocated
  ~104 GB (307 bytes under `memory allocation of 103994205696 bytes failed`).
- `casacure …/helpers.rs` + `table.rs` `patch_copy_nrow`: `tablecopy` now
  rewrites the copied `table.dat` row count, so subtable copies no longer open
  as 0-row tables when the source carried its row count only in the lock
  file's sync record (broke the SPECTRAL_WINDOW rewrite at the end of the run).
- `casacure` performance (merged with `origin/main`, now 3.8.7): GIL released
  around reads, reads no longer serialise on the handle mutex, batched
  lock-once `putcol`, bulk array-cell encode, plus upstream's typed ISM/TSM
  reads and in-place tiled/SSM flush patches / sparse write buffer.
- `skarabina/flag_versions.py`: `save:imported` streams the source FLAG
  read in row chunks instead of materialising the whole cube up front.

### Results (scan 1 = 143 716 rows of `mergA_tim.ms`, single 12-core box)

| build | wall | user | sys | peak RSS |
|---|---|---|---|---|
| correctness fixes only | 128.6 s | 199.9 s | 88.2 s | ~27 GB |
| + GIL / batch / bulk encode | 113.9 s | 160.8 s | 53.9 s | ~27 GB |
| + upstream 3.8.7 typed reads & sparse flush (merged) | **74.4 s** | 119.8 s | 56.7 s | **21.7 GB** |
| + casacure grows tables in place (`d016b8d`), save flushes per chunk, averaging restored (`21ea072`) | **17.2 s** | 69.1 s | 23.1 s | **7.4 GB** |

The last row was measured 2026-09-26 on schmalzburg at load ~7, so its
timings are load-dependent.  It ran on `.bench/scan1.ms`, the copy of scan 1.
The rows above predate `760e0ee`, which made 1.0.8 ignore
`--frequency-average-factor` (fixed in `21ea072`), so all rows average 32x.
The same scan written at full resolution (11 GB, what 1.0.8 did) took 37.8 s
at a 4.1 GB peak.

Memory notes:

- Up to casacure 3.8.7, `--flag save:imported` was the dominant RAM consumer.
  It writes a CASA-compatible backup of the whole flag cube (1.6M x 2511 x 2
  booleans ~ 8 GB), and casacure buffered the whole written table, peaking
  around 18-19 GB.  casacure now grows tables in place, and the save flushes
  per chunk, so a flush costs the chunk.
- The dask/task phases (flagging, averaging, writing the reduced MS) are all
  row-chunked (10k rows) and now run well under 2-3 GB; `--workers` and
  `--row-chunk` bound the concurrent-chunk working set further if needed.
