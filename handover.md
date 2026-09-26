<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Handover — skarabina performance work (2026-09-25, updated 2026-09-26)

Read this, then `AGENTS.md` (repo conventions, release checklist, benchmark
notes) and `doc/RFLAG.md` (the algorithms, every measurement and its method).
Everything below is committed and pushed to `origin/main` of both repos
(`tmolteno/skarabina`, `tmolteno/casacure`).

## 1. State

| | |
|---|---|
| released skarabina | **1.0.10** (tag `v1.0.10`; PyPI `skarabina` + `skarabina-cargo`, Docker `1.0.10`).  Requires `casacure>=3.8.9`.  1.0.9 restored `--frequency-average-factor` / `--time-average-factor` / `--optimize` (1.0.8 silently ignored them), made `save:` flush per chunk and taught the memory plan about streaming backends |
| released casacure | **3.8.9** (tag `v3.8.9`; PyPI, crates.io).  3.8.8: tables grow in place on write (dask-ms writes are chunk-bounded), IncrementalStMan writer fix.  3.8.9: StandardStMan Direct arrays (ANTENNA POSITION/OFFSET) in casacore's inline layout, zero-length array cells |
| unreleased on `main` | nothing, in either repo |
| branches | only `main` in both repos |
| skarabina tests | moist (python-casacore): 458 pass, 2 skipped.  schmalzburg (casacure 3.8.9, `DASK_MS_BACKEND=casacure`): 455 pass, **5 fail**, all in `tests/test_write_changed_only.py` (§3.4) |
| casacure tests | `cargo test --release --workspace` (157 lib + 15 + 5 + 6) and `PYTHONPATH=tests/shim python -m pytest tests` (152 pass, 1 skipped) in `~/.venvs/ccdev` on moist; fmt/clippy clean |

**Working agreements** (as practised with the user in this work):

- Commit straight to `main` and push; no PRs.  Conventional subjects
  (`perf(io):`, `feat(memory):`, `fix(tfcrop):`, `docs:`, `bench:`), and a
  body that says what was measured.  (Commit trailers: follow the current
  session's instructions; the user's later sessions asked for none.)
- **Release only when the user asks** ("release it as X"); then follow the
  `AGENTS.md` checklist without further questions.  casacure releases follow
  the same pattern: bump `Cargo.toml` (workspace version and the `casacure`
  dependency), `pyproject.toml` and `Cargo.lock`; move `CHANGELOG.md`
  [Unreleased] under `## [X.Y.Z] - date`; commit `release: bump to X.Y.Z (...)`;
  make an annotated tag `vX.Y.Z` with message `casacure X.Y.Z (...)`; push main,
  then the tag.  CI publishes to PyPI (~8 min) and crates.io.
  **Wait for all the cp3xx wheels on PyPI before tagging a skarabina release
  that raises the casacure pin.**  1.0.9's Docker build failed on the first
  try because PyPI served casacure 3.8.8 without its cp313 wheel yet; a
  `gh run rerun --failed` fixed it.
- Ask before deleting branches, remote files, or anything under
  `~/github/meerkat_imaging`.
- Every performance claim comes with a measurement: numbers, host, load,
  command; old-vs-new outputs compared for identity where the change should
  not alter results.

**Running the tests** (moist, local): `.venv/bin/python -m pytest -q` from the
repo root — no environment variable needed (python-casacore); ~17 s; all pass.
`.venv/bin/flake8 skarabina tests/test_*.py bench` for lint (two pre-existing
E501s in `bench/baseline_noise.py` and `bench/mem_block.py`).  The cab schema:
`cd cargo && ../.venv/bin/python -m pytest -q tests`.  With casacure on moist:
`DASK_MS_BACKEND=casacure PYTHONPATH=../casacure/tests/shim
~/.venvs/ccdev/bin/python -m pytest -q tests`.  That venv has the casacure dev
build, python-casacore 3.8.1, `../dask-ms` and skarabina, all editable.  It
fails 14 tests that assume python-casacore storage layouts or read counts.

**How the single pass fits together** (read `DaskMS._report`'s docstring and
the comment block before `ms.materialise_flags()` in `skarabina/main.py`):
`flag_ops.run` sets `ms.defer_reports`; every verb, `summary()` and
rflag/tfcrop (`_defer_autofit`) queue their reductions through `_report`
instead of computing; whichever compute comes last takes the queue with
`_take_pending` — the write (`write_new_ms` full path, or `_write_columns`
for `--write-changed-only`/`--apply`), `materialise_flags` (only with
`--optimize`/`--barber`), or `flush_reports` at the end of `main`.  The
averaging and `--optimize` calls in `main()` come after it, lazily (removing
them is what broke 1.0.8; `tests/test_cli_transforms.py` guards them).

**What the work so far achieved** (details in `doc/CHANGES.md`,
`BENCHMARKS.md`, `../casacure/BENCHMARK.md`, `../casacure/MEMORY.md`):

- **Flaggers:** tfcrop ~10x faster, rflag ~2.5x faster
  (bit-identical), baseline-aware chunks with a per-antenna noise model
  (`doc/RFLAG.md`).
- **I/O:** a run reads DATA once, whatever the verbs, averaging and write.
- **Memory:** a printed per-run plan (`skarabina/memory.py`,
  `--memory-limit-GB`).  Since casacure 3.8.8 every write is chunk-bounded:
  on mergA_tim scan 1, stage-0 + `save:` + 32x-averaged `--msout` went from
  74.4 s / 21.7 GB to 17.2 s / 7.4 GB.  A full-resolution 11 GB `--msout`
  peaks at 4.1 GB.
- **casacure against python-casacore** (casacure's time / casacore's time):
  - skarabina on scan 1: 0.80x for the averaged write and 0.62x for
    `--write-changed-only`, in 23-46 % less memory.
  - dask-ms chunked reads: at parity, or 0.40x at 1000-row chunks.
  - Small cached-table calls (`casacure-bench`): 3-5x slower.
  - Correctness: an MS written through casacure reads back in casacore
    identically to python-casacore's own output, in all 132 columns of main
    and subtables.

## 2. Machines

**schmalzburg** (`ssh tim@schmalzburg`, passwordless) — Ryzen 5 5600G, 12
threads, 62 GB.  **Shared and often heavily loaded** (DDFacet imaging); check
`uptime` before timing anything and quote the load with every number.

- repo: `~/github/skarabina` (on `main`).  Two venvs:
  - `.venv`: casacure **editable** from `~/github/casacure` (3.8.9,
    `f9e19e0`), dask-ms 0.2.32.  Always `export DASK_MS_BACKEND=casacure`,
    or nothing imports.  After a casacure change, rebuild it with
    `export PATH=$HOME/.cargo/bin:$HOME/.local/bin:$PATH; cd ~/github/casacure
    && source ~/github/skarabina/.venv/bin/activate && maturin develop --uv
    --release` (plain `maturin develop` fails: the venv has no pip).
  - `.venv-casacore`: real python-casacore 3.8.1, for casacure-vs-casacore
    comparisons.  Run skarabina **without** `DASK_MS_BACKEND` there; it also
    holds a PyPI casacure 3.8.7 (the lock at creation), which is unused.
  - The editable skarabina metadata in both reports an old version (1.0.5 /
    1.0.8); the code is the checkout.
- data: `~/github/meerkat_imaging/ms-orig/mergA_tim.ms` (124 GB, 1 639 497 rows
  x 2511 chan x 2 corr, MeerKAT L band; scan 1 = 143 716 rows, the bandpass
  calibrator).  **Never write to it**: `save:` puts flag versions beside it,
  `--apply` rewrites it.
- `~/github/skarabina/.bench/scan1.ms` (11 GB, gitignored): a written copy of
  scan 1, for anything that writes.  `scan1.ms.flagversions` holds test
  versions `memtest`, `imported` and a few `imported.old.*` — all deletable
  (edit `FLAG_VERSION_LIST` too when removing one).
- `.bench/*.sh`, `*.log`, `*.out` are ad-hoc drivers and their output from
  this work (`run_grow.sh`: the canonical benchmark; `ab.sh`: casacure vs
  casacore, alternating); deletable.

**moist** (the local workstation, `/home/tim/github/skarabina`) — Intel Core
Ultra 7 258V, 8 cores, 30 GB.  `.venv`: python-casacore 3.8.1 (not casacure),
numpy 2.4.6.  `~/.venvs/ccdev`: the casacure dev venv (see §1); build casacure
into it with `maturin develop --release` in `../casacure`.
`/tmp` is tmpfs.  No MeerKAT data; `bench/make_synthetic_ms.py` writes a
bpcal-shaped MS (79 x 2 fixed); `tests/ms_fixture.make_synthetic_ms(path,
nchan=, nrow=, ncorr=)` makes any shape.  The bench scripts set
`DASK_MS_BACKEND=casacure` by default; on moist they fall back to
python-casacore, so memory numbers from moist are not comparable with the
casacure calibration.

**echo** — the laptop `BENCHMARKS.md` was measured on (i5-8365U, bpcal.ms in
its `.bench/data/`).  Not used in this work; ask the user for access before
relying on it.

Spill directories: rflag/tfcrop and the flag materialisation write bit-packed
blocks to `.skarabina-spill-*` in `$TMPDIR` or **beside the input MS**.  A
killed process (SIGKILL, OOM) leaves one behind; check
`ls -a <ms dir> | grep spill` after an aborted run and delete it.

## 3. Next steps, in order

### 3.1 Recalibrate the memory plan now that writes stream

With casacure >= 3.8.9 installed, `memory.writes_stream()` is true.  `save:`
and the writes are then per-chunk steps, and `TABLE_COST` only applies to
casacure <= 3.8.7.  Two constants need measuring against this:

- `CONCURRENT_WRITE_COST["write"]` (8 B/vis, measured when casacure still
  buffered).  The averaged run planned 9.7 GB and peaked at 7.4 GB (scan 1,
  12 workers x 11 977 rows).  The full-resolution write peaked at 4.1 GB.
  Measure both, with `bench/mem_run.py` or `/usr/bin/time -v` on
  `.bench/scan1.ms`, at two chunk sizes (e.g. 5 000 and 12 000 rows), and
  refit.  The averaged output is smaller than the input, so the cost may
  need splitting into input-side and output-side parts.
- The streamed `save:` estimate (`SAVE_CHUNK_ROWS` x `SAVE_CHUNK_COST` = 3 B/vis)
  was measured on a synthetic 256 x 4 cube only; check it on scan 1 (2511 x 2)
  with `--flag save:x` alone.

**Done when** the plan is at or above the measured peak and within ~25 % of
it for these runs.  Say in the plan's printout which casacure was assumed.

### 3.2 Validate the memory model on other shapes

`skarabina/memory.py` constants were measured on one MS shape (2511 chan x 2
corr, 12 workers) — `CHUNK_COST` per verb, `BASE_BYTES`,
`CONCURRENT_WRITE_COST`.  Check a 4k/32k-channel MS, 4 correlations, a few
workers, and averaging -- under casacure (schmalzburg), which is what the
constants describe.  Make shapes with `tests/ms_fixture.make_synthetic_ms`
plus noise (as `bench/make_synthetic_ms.py` does for 79 x 2).  That works
under casacure since 3.8.9, whose Direct-array fix lets it read the UVW
`default_ms` makes.  No real MS of another shape is known locally -- ask the
user.  Tools: `bench/mem_run.py` (end-to-end peak RSS of a flag list at a
given chunk/workers) and `bench/mem_block.py` (per-block working memory;
must stay linear in rows).  **Done when** the plan's estimate is at or above
the measured peak and within ~25 % of it for each shape tried; if not, refit
the constant for the verb that misses.  Checked so far (scan 1): stage-0 +
rflag planned 27.2 / 12.8 GB against 26.5 / 11.6 GB measured, and 33.8
against 31.0 GB with the full write in the pass.

### 3.3 Re-run the flag bench casacure vs casacore on the synthetic MS

`bench/flag_timing.py --ms .bench/data/synth.ms --backend casacure|casacore`
(moist; `.bench/data/synth.ms`, 430 000 rows, is there).  It failed under
casacure before 3.8.9 (the UVW Direct array); with 3.8.9 it should run.
python-casacore took 7.8 s / 2.8 GB for the stage-0 list + rflag (2026-09-26,
load < 1).  Build casacure 3.8.9 into `~/.venvs/ccdev`, pass
`--python ~/.venvs/ccdev/bin/python`, and add both rows to `BENCHMARKS.md`.

### 3.4 The 5 `test_write_changed_only.py` failures under casacure

On schmalzburg (casacure 3.8.9) these fail:
- `test_the_fixture_has_sharable_columns`
- `test_unchanged_columns_are_shared_with_the_input`
- `test_shared_blocks_are_read_only_so_the_input_cannot_be_edited`
- `test_a_block_left_read_only_by_an_earlier_run_can_still_be_rewritten`
- `test_a_copied_shared_block_is_left_writable`

They say the fixture MS has no column in a storage manager of its own
("expected at least one non-FLAG column in its own storage manager"), so the
block sharing of `--write-changed-only` has nothing to hard-link.  The fixture
is built by the backend under test, and casacure's `default_ms`/`maketabdesc`
path seems to put the columns into shared managers.  Confirm that first.  The
flags themselves are written correctly; only the hard links are missing.  Two ways to go:
- **(a)** have casacure honour the per-column `dataManagerGroup`s that
  casacore's `default_ms` assigns (compare `getdminfo()` of the same fixture
  under both backends).  Then the sharing works under casacure too.
- **(b)** mark the tests as python-casacore-layout tests.

(a) is the real fix; agree it with the user, it is casacure work.

### 3.5 casacure's per-call overhead on small tables

`casacure-bench` (20k-row cached table): putcol 3.0x, getcol 3.5x, taql 4.8x
python-casacore.  Invisible in skarabina, whose reads and writes are large and
chunked, but it matters to anything that makes many small calls, such as
subtable edits and TaQL-heavy tools.  The cost is the per-cell `RecordValue`
packaging in `crates/casacure-python/src/table.rs` (putcol/getcol of scalar
columns) and TaQL result materialisation; the typed `getcol_raw` path
already exists for reads.  See `../casacure/BENCHMARK.md` "Where the
remaining gap lives".  casacure work; agree with the user first.

### 3.6 Outputs written with old casacure

Users of skarabina <= 1.0.9 with casacure <= 3.8.8 may hold `--msout` outputs
with:
- **(1)** wrong SCAN_NUMBER / FIELD_ID (the IncrementalStMan writer bug fixed
  in 3.8.8), where a value recurred after the first bucket, e.g. FIELD_ID
  alternating calibrator/target;
- **(2)** an ANTENNA (and FEED) table casacore misreads (the Direct layout).
  casacure 3.8.9 still reads it correctly and converts it on its first write.

A small check command could compare an output's SCAN_NUMBER / FIELD_ID /
TIME with its input, and rewrite the subtables through casacure 3.8.9.  A
`skarabina-check` entry point, or a `bench/` script, would do.  Only if the
user wants it.

### 3.7 Performance improvements not yet done

Roughly by expected value:

1. **rflag CPU** dominates any run with rflag: scan 1 with the stage-0 list +
   rflag is ~95 s (was 232 s before the neighbour-median fix), against ~10 s
   for the other verbs.  A plane is ~4 s single-threaded, split evenly
   between the spectral step and the time step's prefix sums.  Profile a real
   block (`bench/mem_block.py` rows + cProfile).  Candidates: the time step's
   per-channel-group `local_rms` (prefix sums; could run in float32);
   `baseline_noise`'s strided medians.
2. **tfcrop baseline-aware path** (~90 s for scan 1): `robust_fit_columns`
   over ~1900 baselines per chunk is the bulk; the fit per baseline could be
   cached across the two correlations, or fewer attempts used for the
   time-averaged spectra (already smooth).
3. **`--optimize` / `--barber` cost a second DATA pass** (they must see the
   flags before the write).  Already minimal unless DATA is spilled too.
4. **Time series per baseline are short**: ~5 integrations of each MeerKAT
   baseline in a 10 000-row chunk; rflag's `winsize=3` sees little.  A
   chunking that follows baselines across integrations (sort by baseline, or
   dask-ms `group_cols`/`index_cols`) would remove the limit — a big change
   to `DaskMS.__init__` and every writer.
5. **Global thresholds** as CASA (one per field/spw over the whole selection,
   `computeThreshold` in `FlagAgentRFlag.cc`, see RFLAG.md §2): skarabina
   measures per chunk.  Pooling per-chunk statistics in the single pass and
   applying them in the write pass would avoid a second DATA pass.
6. **Graph size**: `materialise_flags` and `_run_autofit` compute with
   `optimize_graph=False` (mixed delayed/array collections; optimising them
   separately renamed the shared read tasks and read DATA twice).  Fine at
   15-150 chunks; check task-scheduling overhead on the whole 1.6M-row MS at
   small chunks.

### 3.8 Open algorithm questions (need the user's decision)

- **Short-lane scatter fallback** (`tfcrop.MIN_LANE_SAMPLES`, `LANE_POOL`,
  `POOL_SAMPLES`; default off): helps narrow-band data only; on mergA_tim no
  row is short.  Evaluate on a narrow-band MS or remove.
  `bench/tfcrop_scatter_eval.py` is its synthetic harness.
- **`tfcrop._edge_taper` tapers only the leading end** of each piece although
  its docstring says both ends; left alone (changes results).
- **A baseline genuinely noisier than its antennas predict** (bad correlator
  input) loses 20-40 % of its samples under the antenna model — CASA's pooled
  threshold flags it too; documented, not changed.
- **Baseline-aware tfcrop on target fields**: validated on a point-source
  calibrator only.  The noise model is in data units (antenna-separable); the
  level (source structure) is per baseline via the per-baseline bandpass fit.
  Check on a target scan (e.g. scan 12 of mergA_tim) with `bench/flag_regions.py`.
- **Band edges**: baseline-aware tfcrop flags the 18 edge channels harder
  (45-50 % vs 32-34 %); the pipeline's spectral-window step flags them anyway.

## 4. Tools (all in `bench/`, all take paths as arguments)

| script | what |
|---|---|
| `compare_versions.sh <ms> <old-ref> "<flags>" [args]` | two versions side by side through `io_probe.py` (temporary worktree for the old ref; output only to `.bench/cmp_out.ms`) |
| `io_probe.py <skarabina args>` | runs the CLI and counts MS column reads per `dask.compute` (wraps `daskms.reads.ndarray_getcol`); DATA MB / DATA size = passes.  For an old version: run from a `git worktree` with `PYTHONPATH=<worktree>` (it prints which code ran) |
| `mem_run.py <ms> <rows> <workers> "<flags>" [--scan S]` | end-to-end peak RSS of a flag list + one final pass; how `memory.CHUNK_COST` was measured |
| `mem_block.py <ms> <scan> 2500,5000,10000 [aware,classic]` | per-block working memory of tfcrop/rflag under tracemalloc |
| `flag_regions.py <ms> <scan> tfcrop\|rflag aware\|classic` | new flags in the clean band vs the RFI windows, short/long baselines (the real-data proxy of RFLAG.md §7.2) |
| `baseline_noise.py <ms> <scan>` | what predicts a baseline's noise (length vs antennas; RFLAG.md §4) |
| `autoflag_interleaved_eval.py` | classic vs baseline-aware tfcrop/rflag on synthetic MS-style chunks (RFLAG.md §7.1) |
| `tfcrop_scatter_eval.py <scenarios>` | the short-lane fallback's synthetic evaluation |
| `flag_timing.py`, `make_synthetic_ms.py` | the older flag-timing bench (AGENTS.md) and a bpcal-shaped synthetic MS |

Reference for the CASA comparison: `FlagAgentRFlag.cc` from casa6
(`https://open-bitbucket.nrao.edu/projects/CASA/repos/casa6/raw/casatools/src/code/flagging/Flagging/FlagAgentRFlag.cc?at=refs/heads/master`).
The `casacore` submodule of `../casacure` (pinned `56a917a`, not checked out)
has no rflag.

## 5. Conventions that bit during this work

- `--flag "uv-above 8000"`: multi-token entries must be one quoted argument.
- A new flagging verb must report through `DaskMS._report`, never
  `dask.compute` on its own, or it adds a pass; `tests/test_single_pass.py`
  counts DATA reads and will fail.
- Keep `self.ds.X.data` (dask) in reductions — `da.sum(self.ds.FLAG)` on the
  xarray DataArray evaluated the whole column eagerly (fixed in `summary`).
- Old-vs-new comparisons: compare DATA with `equal_nan=True`; `pkill -f` over
  ssh can match the ssh command itself — kill by PID.
- A new CLI option must be exercised through the CLI (`click.testing.CliRunner`
  on `skarabina.main.main`), not only through `DaskMS` methods: 1.0.8 shipped
  with three options silently disconnected (`tests/test_cli_transforms.py`).
- Measuring peak RSS of a child process: poll `/proc/<pid>/status` VmHWM from
  the parent.  The child's `ru_maxrss` inherits the parent's peak across
  fork+exec, and made every chunk size of casacure's
  `bench_daskms_chunking.py` report the same number.
- `bench/flag_timing.py` on `echo` (bpcal.ms) would give before/after numbers
  on the original bench host; not run in this work.
