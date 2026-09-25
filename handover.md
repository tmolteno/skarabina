<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Handover — skarabina performance work (2026-09-25)

Read this, then `AGENTS.md` (repo conventions, release checklist, benchmark
notes) and `doc/RFLAG.md` (the algorithms, every measurement and its method).
Everything below is committed and pushed to `origin/main`.

## 1. State

| | |
|---|---|
| released | **1.0.7** (tag `v1.0.7`, PyPI `skarabina` + `skarabina-cargo`, Docker `1.0.7`) |
| unreleased on `main` | `760e0ee` — `--write-changed-only`/`--apply` share the flagging pass (plus this handover and the `bench/` scripts) |
| stale branches | `baseline-aware-flagging`, `tfcrop-local-scatter` — both merged into `main`; safe to delete (local and `origin`) |
| test suite | all pass except `tests/test_flag_versions.py::test_save_rejects_a_path_like_name`, which fails on 1.0.6 too (pre-existing, not investigated) |

What 1.0.7 contains is in `doc/CHANGES.md`.  In short:

- **tfcrop** ~10x faster (vectorised lane flagging, batched time fits, window
  views); pre-existing flags left out of its scatter.
- **Baseline-aware chunks** (`skarabina/baselines.py`, `dask_ms.AUTOFIT_BASELINES`,
  default on): a dask row chunk is ~1900 interleaved baselines per integration,
  not one baseline's time series.  Rows are grouped by (scan, baseline); rflag's
  windows stay inside a baseline; tfcrop fits a bandpass per baseline; both use
  a per-antenna noise model (`noise_ij ~ s_i s_j`, fits every baseline to 1.5 %
  on MeerKAT).  tfcrop's flagging of the RFI-free band on mergA_tim scan 1 fell
  from 30 % to 4-6 %.
- **rflag bug fixes**: complex masking (`nan+0j`, 1.0.6); window count divided
  by window length so flagged samples counted as zeros; single-sample windows
  gave scatter 0.
- **Memory**: `--memory-limit-GB` (0 = available RAM, cgroup-aware) and a
  printed per-run plan (`skarabina/memory.py`): the row chunk is the largest
  keeping every verb of the `--flag` list within the limit; `save:` and a full
  write are whole-table steps and only warned about.  `restore:` reads lazily.
- **I/O**: a run reads DATA once.  Every verb's statistics go through
  `DaskMS._report` (queued in a run), and the last step is the pass.

## 2. Machines

**schmalzburg** (`ssh tim@schmalzburg`, passwordless) — Ryzen 5 5600G, 12
threads, 62 GB, casacure 3.8.7, numpy 2.5, dask-ms 0.2.32.  **Shared and often
heavily loaded** (DDFacet imaging); check `uptime` before timing anything and
quote the load with every number.  At handover it was loaded: an I/O comparison
was stopped half way (§3.1).

- repo: `~/github/skarabina` (on `main`, `.venv` with casacure); always
  `export DASK_MS_BACKEND=casacure` — without it nothing imports.
- data: `~/github/meerkat_imaging/ms-orig/mergA_tim.ms` (124 GB, 1 639 497 rows
  x 2511 chan x 2 corr, MeerKAT L band; scan 1 = 143 716 rows, the bandpass
  calibrator).  **Never write to it**: `save:` puts flag versions beside it,
  `--apply` rewrites it.
- `~/github/skarabina/.bench/scan1.ms` (11 GB, gitignored): a written copy of
  scan 1, for anything that writes.  `scan1.ms.flagversions` holds test
  versions `memtest` and `imported` (625 MB) — deletable.
- `.bench/iocmp3.sh`, `.bench/iocmp3.log`: the interrupted comparison (§3.1).
- the tests there: run with `DASK_MS_BACKEND=casacure`; tests that import
  `casacore.tables` before `daskms` fail with `No module named casacore`
  (import order), and 15 write-path tests fail with casacure `PoisonError`
  panics — both happen on 1.0.6 too; the flagging tests pass.

**moist** (the local workstation, `/home/tim/github/skarabina`) — 8 cores,
30 GB, python-casacore 3.8.1 (not casacure), numpy 2.4.  `/tmp` is tmpfs.  No
MeerKAT data; `bench/make_synthetic_ms.py` writes a bpcal-shaped MS.

Spill directories: rflag/tfcrop and the flag materialisation write bit-packed
blocks to `.skarabina-spill-*` in `$TMPDIR` or **beside the input MS**.  A
killed process (SIGKILL, OOM) leaves one behind; check
`ls -a <ms dir> | grep spill` after an aborted run and delete it.

## 3. Next steps, in order

### 3.1 Finish verifying `760e0ee` on real data, then release 1.0.8

`760e0ee` makes `--write-changed-only` and `--apply` write every changed
column in one `dask.compute` with the run's queued reports
(`DaskMS._write_columns`), instead of materialising the flags first.  Verified
on synthetic data: output and reports identical to v1.0.7 (write-changed-only
and apply, with `--summary`, for nan/clip/autos/uv-above, rflag and tfcrop
lists) and DATA read once (`tests/test_single_pass.py`).  On schmalzburg the
stage-0 list with `--summary --write-changed-only` gave 1 DATA pass in both
versions (1.0.7 already read it once there, via the materialising pass) and
4.98 s vs 5.82 s at the same 3.2 GB peak; **the rflag half was stopped** because
the machine was loaded.  When it is idle:

```sh
ssh tim@schmalzburg 'uptime; bash ~/github/skarabina/.bench/iocmp3.sh'
# needs /tmp/ioprobe.py: scp bench/io_probe.py tim@schmalzburg:/tmp/ioprobe.py
```

Expect: 1 DATA pass for both, the new one without the spill write; peak RSS
about the same (the flags-only write holds FLAG/FLAG_ROW chunks, planned at
1 B/vis/worker, `memory.CONCURRENT_WRITE_COST["write-flags"]`).  Then add the
numbers to `doc/CHANGES.md` (Unreleased) and release 1.0.8 with the
`AGENTS.md` checklist (both `pyproject.toml`, `cargo/.../skarabina-cargo-base.yml`,
`uv.lock` line ~1076, changelog heading, `chore(release): X` commit, annotated
tag `vX` "skarabina X", push `main` then the tag; CI publishes).

### 3.2 casacure buffers whole tables on write (the largest memory problem left)

Measured (doc/RFLAG.md §7.4): `save:<name>` peaks at ~2.2 B per MS visibility
(18-19 GB for mergA_tim) and a full `--msout` write at **~56 B per output
visibility** (40.7 GB to write the 11 GB scan-1 copy; ~450 GB for the whole
MS), whatever the row chunk, because casacure keeps what it writes in a buffer
until the table is flushed.  The flags-only writes are cheap (1.8 GB).  Fix in
`../casacure` (Rust): flush tiled-storage-manager data per chunk / bounded
buffer.  AGENTS.md notes a known obstacle: per-chunk flushes regrow the
single-column flag-version table.  After a fix, re-measure and lower
`memory.TABLE_COST` (or move the writes to `CHUNK_COST`).

### 3.3 Validate the memory model on other shapes

`skarabina/memory.py` constants were measured on one MS shape (2511 chan x 2
corr, 12 workers) — `CHUNK_COST` per verb, `TABLE_COST`, `BASE_BYTES`,
`CONCURRENT_WRITE_COST`.  Check a 4k/32k-channel MS, 4 correlations, a few
workers, and averaging.  Tools: `bench/mem_run.py` (end-to-end peak RSS of a
flag list at a given chunk/workers) and `bench/mem_block.py` (per-block
working memory; must stay linear in rows).  Checked so far: the plan predicted
27.2 GB / 12.8 GB against 26.5 / 11.6 GB measured (stage-0 + rflag), and 33.8
against 31.0 GB with the full write in the pass.

### 3.4 Performance improvements not yet done

Roughly by expected value:

1. **rflag CPU** dominates any run with rflag (~260-300 s for scan 1 on 12
   threads, vs ~10 s for the other verbs).  Profile a real block (use
   `bench/mem_block.py` rows + cProfile).  Candidates: the spectral step's
   three neighbour-median widths (`rflag._neighbour_residual_rows`) — only
   unresolved samples need the wider ones; the time step's per-channel-group
   `local_rms` (prefix sums; could run in float32); `baseline_noise`'s
   strided medians.
2. **tfcrop baseline-aware path** (~90 s for scan 1): `robust_fit_columns`
   over ~1900 baselines per chunk is the bulk; the fit per baseline could be
   cached across the two correlations, or fewer attempts used for the
   time-averaged spectra (already smooth).
3. **`--optimize` still costs a second DATA pass** (it must see the flags to
   choose rows/channels before the write).  Possible: compute the row/channel
   keep-masks inside the materialising pass and write from spilled flags plus
   one DATA pass — the second DATA read is the write itself, so this is
   already minimal unless DATA is spilled too.  `--barber` likewise.
4. **Time series per baseline are short**: ~5 integrations of each MeerKAT
   baseline in a 10 000-row chunk; rflag's `winsize=3` sees little.  The
   memory plan now picks larger chunks when RAM allows (21k rows on
   schmalzburg); a chunking that follows baselines across integrations (sort
   by baseline, or read with dask-ms `group_cols`/`index_cols`) would remove
   the limit — a big change to `DaskMS.__init__` and every writer.
5. **Global thresholds** as CASA (one per field/spw over the whole selection,
   `computeThreshold` in `FlagAgentRFlag.cc`, see RFLAG.md §2): skarabina
   measures per chunk.  A two-pass mode would cost a second DATA pass; an
   alternative is to pool per-chunk statistics in the single pass and apply
   them in the write pass (needs the flags to be decided after the pool).
6. **`flag_spectral_window`** computes `uv_dist` eagerly (a small UVW pass per
   run); make the row gates lazy.
7. **Graph size**: the single-pass computes run with `optimize_graph=False`
   in `materialise_flags` and `_run_autofit` (mixing delayed and array
   collections defeats key sharing otherwise).  Fine at 15-150 chunks; check
   task-scheduling overhead on a whole 1.6M-row MS at small chunks.

### 3.5 Open algorithm questions (need the user's decision)

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
- `BENCHMARKS.md` has not been refreshed since the 3.8.6/3.8.7 comparison;
  `bench/flag_timing.py` on `echo` (bpcal.ms) would give the before/after for
  1.0.7 on the original bench host.
