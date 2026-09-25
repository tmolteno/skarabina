# Skarabina benchmarks

Local timing measurements of skarabina.  The harness that produces them is
`bench/` (see [AGENTS.md](AGENTS.md) for how to run it); each section records
the host, date, backend and workload with its numbers, because a timing without
those is not comparable to anything.

Transcribe a new measurement here when a change claims to make flagging faster,
and keep `bench/meerkat-flags.yml` in step with the `flag-average` step of
`../meerkat_imaging/white-belt-0-flagging.yml`.

---

## Writing: casacure grows tables in place, 2026-09-26

Host schmalzburg (12 cores, 62 GB), load 6-8 (shared: timings are
load-dependent), casacure `d016b8d` (released as 3.8.8), skarabina
`21ea072`.  The input is `.bench/scan1.ms`, a copy of scan 1 of mergA_tim:
143 716 rows x 2511 channels x 2 correlations.  The command is the
AGENTS.md canonical benchmark:

```
DASK_MS_BACKEND=casacure /usr/bin/time -v .venv/bin/skarabina --ms scan1.ms \
  --summary --time-average-factor 1 --frequency-average-factor 32 --clobber \
  --flag save:imported --flag autos --flag "uv-above 2500" --flag nan \
  --flag "clip 0 100" --flag "spectral-window ../bench/spectral-flags-L.yml" \
  --field-of-view 3.3deg --msout bench_ave.ms
```

| build | output | wall | user | sys | peak RSS |
|---|---|---|---|---|---|
| casacure 3.8.7 (2026-09-25, AGENTS.md) | 79 chan | 74.4 s | 119.8 s | 56.7 s | 21.7 GB |
| casacure `d016b8d`, skarabina `ca9264e` (averaging still dropped) | 2511 chan, 11 GB | 37.8 s | 27.7 s | 28.0 s | 4.1 GB |
| casacure `d016b8d`, skarabina `21ea072` | 79 chan, 375 MB | **17.2 s** | 69.1 s | 23.1 s | **7.4 GB** |

Checks:
- The output's TIME, ANTENNA1/2, SCAN_NUMBER, FIELD_ID, DATA_DESC_ID,
  STATE_ID, UVW, INTERVAL and EXPOSURE are identical to the input.
- The saved `flags.imported` FLAG and FLAG_ROW equal the input's.

### casacure 3.8.8 against python-casacore

Same host, input and flag list, with the two backends run alternately, twice
each (load 7 rising to 11).  python-casacore 3.8.1 runs from a separate venv
(`.venv-casacore` on schmalzburg: `uv venv -p 3.13` + `uv pip install -e .
python-casacore`), without `DASK_MS_BACKEND`.

| workload | casacure 3.8.8 | python-casacore 3.8.1 | ratio (time / peak) |
|---|---|---|---|
| + 32x frequency average, `--msout` | 17.3 s, 7.40 GB / 17.5 s, 7.41 GB | 21.3 s, 10.1 GB / 22.5 s, 8.99 GB | **0.80 / 0.77** |
| + `--write-changed-only --msout` | 7.7 s, 2.51 GB / 7.6 s, 2.23 GB | 13.8 s, 4.16 GB / 10.6 s, 4.49 GB | **0.62 / 0.54** |

The two backends' averaged outputs are identical in every readable
main-table column and in SPECTRAL_WINDOW, and their printed reports match.
(FLAG_CATEGORY is unreadable by casacore in the input too.)

### Reading the result

- casacure 3.8.7 buffered every table it wrote until complete.  The 21.7 GB
  was the `save:` backup of the 8 GB flag cube; a full-resolution `--msout`
  was ~56 B per visibility, about 40 GB for this scan.
- Now each flush appends default rows to the storage managers and patches
  the chunk in (`../casacure/MEMORY.md`).  The full-resolution write, 11 GB
  of output, peaks at 4.1 GB.
- The averaged run peaks higher (7.4 GB) because the averaging and the
  flaggers' chunks are in flight together.  That is the per-chunk working set
  of 12 workers x 11 977 rows, so `--row-chunk` / `--workers` bound it.
- In casacure's own suite (`tests/test_write_scaling.py`, a dask-ms write in
  2000-row chunks), 256k rows (a 375 MiB table) took 1865 MiB and 57.6 s
  before.  It now takes 188 MiB and 1.45 s, against python-casacore's
  234 MiB and 2.4 s.  1.02M rows (1.5 GiB) take 224 MiB and 6.5 s.

---

## Flagging the bandpass calibrator, 2026-09-24

**Workload** — `.bench/data/bpcal.ms`, the local copy of a real MeerKAT L-band
bandpass-calibrator MS: 429,257 rows, 79 channels, 2 correlations, 227
integrations (8 s cadence), one field (`J0408-6545`), 1.56 GiB,
67,822,606 visibilities.  It **arrives 71.21 % flagged** (48,296,656
visibilities, 158,528 rows or 36.9 % fully row-flagged), which is the state the
imported flags leave it in.  No averaging is applied
(`--frequency-average-factor 1 --time-average-factor 1`), so the run sees the
data exactly as they arrived.

**Flag list** — the stage-0 sequence of `../meerkat_imaging`
(`white-belt-0-flagging.yml`, step `flag-average`) with the `rflag` verb
appended, which is the CASA autoflag loop that recipe still delegates to CASA
(`recipe-loop-autocal`, `flagdata(mode='rflag')`):

```
save:imported, autos, uv-above 8000, nan, clip 0 100,
spectral-window (spectral-flags-L.yml), rflag
```

**Host** — `echo`: Intel Core i5-8365U @ 1.60 GHz, 8 cores, 15.4 GiB RAM, SATA
SSD; skarabina 1.0.5 (git `be38252`), Python 3.14.7, dask-ms 0.2.32 with a
locally built casacure (its version string said 3.8.3; PyPI's 3.8.3 cannot even
open this MS, see the next section).  Single measurement, no repetition.

**Commands**

```
python bench/flag_timing.py --tag bpcal-seq-casacure
python bench/flag_timing.py --mode per-op --tag bpcal-perop-casacure-repaired
```

### Results

| mode | operation(s) | wall (s) | peak RSS (MiB) | flags reported | rc |
|---|---|---|---|---|---|
| seq | all 7, one process | 956.7 | 2598 | 100.00% | 0 |
| per-op | `save:imported` | 16.6 | 1860 | 71.21% | 0 |
| per-op | `autos` | 27.0 | 1854 | 71.21% | 0 |
| per-op | `uv-above 8000` | 19.6 | 1833 | 71.21% | 0 |
| per-op | `nan` | 23.6 | 2022 | 71.21% | 0 |
| per-op | `clip 0 100` | 24.0 | 2084 | 71.21% | 0 |
| per-op | `spectral-window` (L band) | 17.2 | 1918 | 71.21% | 0 |
| per-op | `rflag` (CASA defaults) | 828.9 | 2524 | 100.00% | 0 |

### Reading the result

- The whole pipeline-style pass costs **16 minutes** wall clock and **2.6 GiB**
  peak RSS on this laptop CPU.  **`rflag` is 87 % of it** (828.9 s of
  956.7 s); the other six operations together are 128 s.
- The seven isolated runs sum to 957.0 s against the 956.7 s single-process
  run, so the operations share no work here — and the six non-rflag ones run at
  essentially the cost of reading the MS and rewriting the flag columns.
  `save:imported` measures that floor: 16.6 s to read the MS, snapshot the
  flags and write the (unchanged) flag columns back.
- On this MS the meerkat stage-0 list does nothing: each of `autos`,
  `uv-above 8000`, `nan`, `clip 0 100` and `spectral-window` ends with the
  input's own flag fraction, 71.21 %, in the run's `Flagging Summary`.
  `uv-above 8000` cannot fire at all — the longest baseline is 7652.7 m — and
  the others' hits are already covered by the imported flags.  The remaining
  28.79 % of the data goes only when `rflag` runs: it flags **every remaining
  unflagged visibility**, 19.5 M of them, leaving the MS 100 % flagged.
- That outcome is the coarse-grid failure mode `doc/NEW_FLAGGING.md` §10.5
  warns about.  The spectral step compares each channel with its neighbours, so
  on a 79-channel grid with 6.7 MHz channels the band's own slope enters the
  measured `freqdev`; the documentation's advice for such data is to supply
  `freqdev` rather than let it be measured.  With CASA's defaults on this MS
  the threshold collapses and nothing survives.  Heavily flagged input does not
  help either: `skarabina/rflag.py` emits `RuntimeWarning: All-NaN slice
  encountered` for every window whose samples were all flagged already.  The
  timing above is the honest cost of the operation as configured; a run that
  leaves a sane fraction of the data unflagged is what `--rflag-args "..."` is
  for.
- The output is written with `--write-changed-only`, so only `FLAG` and
  `FLAG_ROW` are rewritten (the run reports `13 block(s) shared with the input,
  20 copied or written`).  The bench restores owner-write on the input's blocks
  before each run — see the issue below; without that repair, five of the seven
  per-op runs fail, which is how the issue was found.
- Sequencing note: `nan` and `clip` are deferred by skarabina into one dask
  pass, so although they appear before `spectral-window` and `rflag` in the
  list, their work is reported after them in the log (`flag_data (NaN)`, then
  `flag_data (clip [0.0, 100.0])`).

---

## casacure 3.8.6 vs 3.8.7, 2026-09-25

**Why** — casacure 3.8.7 reads ISM and tiled columns straight into the numpy
buffer, patches tiled and StandardStMan cells in place on flush, and keeps only
the written rows in its write buffer.  Same workload and flag list as the
section above, measured twice **in one sitting on the same machine**, so the
I/O gain and the CPU-bound flagging can be told apart.

**Setup** — `.bench/data/bpcal.ms` and the meerkat stage-0 list as above;
casacure 3.8.6 and 3.8.7 (PyPI wheels), Python 3.14.7, numpy 2.5.3, dask-ms
0.2.32, skarabina 1.0.5 (git `cca9ea4`).  Both versions were swapped in and out
of the bench venv between runs; 3.8.7 ran first, 3.8.6 second, which is what
the `rflag` control below measures.

**Commands**

```
uv pip install --python .venv-bench/bin/python "casacure==3.8.6"
python bench/flag_timing.py --mode seq    --tag ab-386-seq
python bench/flag_timing.py --mode per-op --tag ab-386-perop
# …then the same two with casacure==3.8.7 (tags ab-387-seq / ab-387-perop)
```

### Results

| mode | operation | 3.8.6 wall (s) | 3.8.7 wall (s) | ratio | 3.8.6 RSS (MiB) | 3.8.7 RSS (MiB) |
|---|---|---|---|---|---|---|
| seq | all 7, one process | 1154.9 | 1087.0 | **1.06×** | 1761 | 1985 |
| per-op | `save:imported` | 21.0 | 13.0 | 1.62× | 596 | 671 |
| per-op | `autos` | 25.6 | 15.6 | 1.64× | 362 | 363 |
| per-op | `uv-above 8000` | 20.6 | 10.8 | 1.91× | 352 | 352 |
| per-op | `nan` | 35.5 | 19.4 | 1.83× | 557 | 529 |
| per-op | `clip 0 100` | 37.4 | 17.8 | 2.10× | 562 | 581 |
| per-op | `spectral-window` | 25.2 | 13.0 | 1.94× | 452 | 452 |
| per-op | `rflag` (CASA defaults) | 1058.0 | 1096.8 | 0.96× | 1694 | 1844 |

### Reading the result

- **The I/O the release targets is 1.6–2.1× faster.**  Every operation whose
  cost is dominated by reading the MS and writing the flag columns gained:
  `clip` 2.1×, `spectral-window` 1.9×, `uv-above` 1.9×, `nan` 1.8×, `autos`
  1.6×, and the I/O floor `save:imported` 1.6× (21.0 s → 13.0 s).
- **`rflag` is the control, and it did not move**: 1058.0 s vs 1096.8 s — the
  baseline ran 3.7 % *quicker*, on the version that ran second.  It is
  numpy-bound, casacure never enters its hot path, so that difference is machine
  drift between the two blocks rather than an effect of the release.  Read the
  I/O ratios as ±4 %, and note which way the drift ran: 3.8.6 was measured
  second, when the machine was quicker, so the gains above are if anything
  understated.
- **End to end the pass gains only 6 %** (1154.9 s → 1087.0 s), because `rflag`
  is the whole run: 1096.8 s of the 1087.0 s single-process total, the other six
  operations 90 s together.  A faster casacure cannot help a workload that
  spends 90 %+ of its time in the sliding-window median.
- **Peak RSS is not better on this workload** (seq 1761 → 1985 MiB, per-op
  `rflag` 1694 → 1844).  The sparse write buffer should show on a large table
  with many columns; here the flag columns are the only ones written and the
  read side dominates the footprint.
- **Do not compare this table with the 2026-09-24 one without a factor.**
  `rflag` alone took 828.9 s then and ~1058–1097 s now — the same code, ~1.3×
  slower on today's machine (thermal/load).  That factor is larger than the
  improvement being measured, which is exactly why the A/B above was run as one
  sitting rather than against yesterday's numbers.
- The 2026-09-24 runs used a **locally built** casacure (its version string read
  3.8.3).  PyPI's 3.8.3 wheel cannot open this MS at all — it dies with
  `RuntimeError: storage error: unsupported data-manager type TiledShapeStMan`
  — so that local build was newer code than its version string admits, and
  3.8.6 is the correct "before" for the 3.8.7 comparison.

---

## rflag: sort-based medians and a spilled result, 2026-09-25

**Why** — `rflag` was 87 % of the bpcal run above (828.9 s of 956.7 s).
Profiling one 10 000-row, 79-channel block put 1.9 s of its 2.2 s in
`np.nanmedian`, whose small-axis path goes through masked arrays (the
neighbour medians of the spectral step); the time step made ~10 numpy calls per
channel.  Separately, `_run_autofit` `persist()`ed the whole flag cube (memory
that grows with the table) and then ran a second pass over the incoming FLAG
graph just to count the pre-existing flags.

**Change** — a sort-based `_nanmedian` (same two middle values, same average,
so bit-identical flags), the time step vectorised over groups of channels and
the neighbour medians over groups of rows (`rflag.GROUP_VALUES` bounds the
temporaries), and each block's flags written to a spill directory at one bit
per visibility instead of persisted.  One `compute` now runs the algorithm,
writes the spill and returns both counts; later passes read the flags back
block by block.  The spill lives in `$TMPDIR` if set, else beside the input MS
(not `/tmp`, which is often tmpfs), and is removed with the `DaskMS` instance.

**Workload** — `bpcal.ms` is not on this host, so a synthetic MS of the same
shape stands in: 430 000 rows (and 860 000 for the scaling check) × 79
channels × 2 correlations, Gaussian noise about 10+0j, ~0.1 % of rows ×30,
FLAG ~69 % set (37 % whole rows, the first 8 channels, 45 % random), built
with `bench/make_synthetic_ms.py` (on `tests/ms_fixture.make_synthetic_ms`).  Each run:
`skarabina --ms synth.ms --flag rflag --msout out.ms --clobber --write-changed-only`.
"old" is git `351e8d6` run from a worktree; "new" is this change.

**Host** — `moist`: Intel Core Ultra 7 258V, 8 cores, 30 GiB RAM; Python
3.11, numpy 2.4.6, dask-ms 0.2.23, **python-casacore 3.8.1** (not casacure).
Single measurement each; load average 1.4–3.3 during the runs (other work on
the box), so the wall times are load-dependent.

| rows | flag list | build | wall (s) | user (s) | peak RSS (MiB) |
|---|---|---|---|---|---|
| 430k | `rflag` | old | 65.1 | 139.0 | 1775 |
| 430k | `rflag` | new | **7.95** | 40.7 | **882** |
| 860k | `rflag` | old | 133.0 | 282.8 | 2027 |
| 860k | `rflag` | new | **15.0** | 81.2 | **892** |
| 430k | `rflag, autos, nan, clip 0 100, spectral-window` + `--summary` | old | 68.0 | 141.6 | 1821 |
| 430k | same | new | **10.4** | 43.4 | **916** |

- The written `FLAG` and `FLAG_ROW` are byte-identical between old and new in
  both the 430k runs, and so are the reported counts (66 407 144 flagged,
  19 600 219 newly).
- Peak RSS no longer follows the table: doubling the rows moved the new build
  by 10 MiB, the old one by 252 MiB.
- The rows above were measured *before* the fix below, so old and new could
  be compared flag for flag; both flag 97.7 % of the MS.  That is the same
  collapse as the 100 % on bpcal.  Its cause: `np.where(flagged, np.nan,
  plane)` on complex data gives `nan+0j`, so every flagged sample's imaginary
  part entered the statistics as a zero, and on data this heavily pre-flagged
  the spectral deviation came out 0.09 against a noise of 1.0.  With flagged
  samples NaN in both parts (`rflag._MISSING`), the same 430k run flags
  **43 540 new visibilities (0.06 %)** — the injected bursts — instead of
  19.6 M, in 8.12 s and 886 MiB.  bpcal's own result needs re-measuring on
  `echo`.

---

## tfcrop: vectorised flagging, batched time fits, 2026-09-25

**Why** — with the dask side already shared with `rflag` (spilled per-block
flags, one pass), tfcrop's own arithmetic was the cost: 0.78 s per
10 000-row × 79-channel plane, two thirds of it `flag_1d` called once per row
(two `np.median` calls per iteration), and most of the rest ~20 small `lstsq`
fits per column in the time direction.  That work is interpreter-bound, so
dask's threads fought over the GIL: 16 planes took 3.7 s on one thread and
6.1 s on eight.

**Change** — `flag_lanes` flags every row (or column) at once with the
sort-based median now in `skarabina/nanstats.py` (bit-identical to the
`flag_1d` loop); `robust_fit_columns` fits all columns' time series together,
solving each piece's weighted least squares through its normal equations; the
optional `usewindowstats` pass reduces strided window views instead of looping
per point; `tfcrop.GROUP_VALUES` bounds the temporaries.

**Workload / host** — as the rflag section above: the `bench/make_synthetic_ms.py`
MS (79 chan × 2 corr, ~69 % pre-flagged), `--flag tfcrop` with CASA defaults,
`--write-changed-only`; `moist`, python-casacore 3.8.1, load average 0.5–2.5.
"1.0.5"/"1.0.6" are those tags run from worktrees; single measurements.

| rows | build | wall (s) | user (s) | sys (s) | peak RSS (MiB) |
|---|---|---|---|---|---|
| 430k | 1.0.5 | 83.9 | 102.0 | 40.0 | 571 |
| 430k | 1.0.6 | 82.0 | 100.1 | 39.7 | 529 |
| 430k | new | **8.41** | 36.5 | 2.6 | 718 |
| 860k | 1.0.6 | 163.1 | 200.4 | 77.9 | 582 |
| 860k | new | **15.6** | 73.0 | 4.3 | 761–803 |
| 1.72M | new | **31.0** | 144.9 | 8.2 | 778 |

- The written `FLAG` is identical to 1.0.6's: **0 of 67 940 000** flags differ
  at 430k (168 182 new flags in both).  1.0.5 and 1.0.6 also agree with each
  other.
- The per-plane pieces are bit-identical to the loops they replace except the
  batched time fit, which agrees to rounding (`test_robust_fit_columns_agrees_with_robust_fit`).
  The one difference found in the old-vs-new sweep is a degenerate plane of
  exact constants: `lstsq` fitted a column's baseline as 0.9999999999999998,
  and the noiseless fallback (flag any deviation from 1) then flagged the whole
  column; the normal equations give exactly 1 and flag nothing.
- The single-thread cost per plane fell from 0.78 s to 0.29 s; the rest of the
  gain is that the work is now GIL-free numpy and runs on all eight workers
  (16 planes: 1.24 s on eight threads).  The 1.0.x `sys` time was lock
  contention between those threads.
- Peak RSS is higher than 1.0.6's by ~200 MiB because eight workers now really
  run concurrently, each with bounded temporaries; it does not grow with the
  table (718 → ~780 → 778 MiB over a 4× longer MS; repeated 860k runs spread by
  40 MiB).  For comparison `rflag` at 1.72M rows peaked at 903 MiB (882 at 430k).
- **Afterwards, pre-existing flags were taken out of tfcrop's scatter**
  (`flag_1d`/`flag_lanes`, see `doc/CHANGES.md`), which changes the flags.
  Same 430k MS: 8.00 s, 708 MiB; 271 190 new flags instead of 168 182.  All
  18 958 live samples of the ~0.1 % injected-RFI rows are caught either way;
  the difference is false positives on clean data, 0.71 % → 1.19 % of the live
  samples.  This MS's pre-flags are random picks of clean noise, so counting
  them gave each row a larger, equally clean sample -- on real data those
  samples are dead or RFI, which is what the fix is for.

---

## Issue: `--write-changed-only` leaves the input read-only, then fails on it

Found while building this bench (2026-09-24, skarabina 1.0.5), reported as
[issue #3](https://github.com/tmolteno/skarabina/issues/3), and **fixed** after
1.0.5: blocks *copied* into the output — every rewritten column, and every
block of a cross-filesystem write — are now left writable, so a run no longer
dies on an input that an earlier run made read-only.  The reproduction below
therefore applies to 1.0.5 and earlier; on a fixed build the second command
succeeds.  The analysis is kept because the shape of the bug is worth
recognising: a mode-preserving copy of a file that is about to be written.

**Symptom.**  A `--write-changed-only` run that has to write a flag column dies
with

```
RuntimeError: storage error: Permission denied (os error 13)
```

raised from `daskms/writes.py:46` (`ndarray_putcol` → `table.flush()`), after
printing `Updating table: FLAG in <msout>`.

**Cause.**  `skarabina/dask_ms.py::_write_changed_only` hard-links every block
it does not rewrite into the output and then chmods those links read-only, "so
that rewriting an unchanged column in the output fails loudly rather than
altering the input".  A hard link is the same inode, so that chmod also lands on
the **input's** own `table.fN`: after one such run the input MS has read-only
blocks (in `bpcal.ms`: `table.f17_TSM1` … `table.f24_TSM1`, `-r--r--r--`, among
them the block `_column_file_groups` attributes to `FLAG`).  The next run that
must rewrite such a column copies it with

```python
for base, members, _ in rewritten:
    for member in members:
        source = os.path.join(self.name, member)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(name, member))
```

and `shutil.copy2` **preserves the mode**, so the fresh copy in the output is
read-only as well.  The column write that follows targets that copy and is
refused by the filesystem.

The sharing — and so the failure — only happens when the output MS is on the
same filesystem as the input.  Across devices `os.link` raises `EXDEV` and the
code falls back to `shutil.copy2` for the shared list too, so nothing is
chmod'ed and nothing breaks.

**Reproduction** (on `bpcal.ms`, about a minute — start from writable blocks so
that the first run can succeed and spoil them; the output must share the
input's filesystem):

```
chmod u+w .bench/data/bpcal.ms/table.f*
printf 'ops:\n  - save:imported\n  - autos\n' > /tmp/repro-readonly.yml
python bench/flag_timing.py --mode per-op --no-repair-input-perms \
    --ops-file /tmp/repro-readonly.yml --tag repro-readonly
```

Equivalently, without the bench:

```
skarabina --ms .bench/data/bpcal.ms --msout .bench/scratch/o1.ms --clobber \
    --write-changed-only --flag save:imported     # 15 blocks shared, input now read-only
skarabina --ms .bench/data/bpcal.ms --msout .bench/scratch/o2.ms --clobber \
    --write-changed-only --flag autos             # EACCES writing FLAG
```

The first run, `save:imported`, changes no column, so `FLAG`'s block is shared
and chmod'ed read-only — through the hard link, on the input too.  The second,
`autos`, then has to rewrite `FLAG` and fails with EACCES.  In the full per-op
pass the same happens to `nan`, `clip`, `spectral-window` and `rflag`; only
`uv-above 8000` succeeds, because on this MS it touches `FLAG_ROW` alone.  The
first attempt's results are in `bench/results/bpcal-perop-casacure.json` (rc=1
rows); running `chmod u+w .bench/data/bpcal.ms/table.f*` and repeating the same
per-op pass gives rc=0 throughout (`bpcal-perop-casacure-repaired.json`), which
is what `bench/flag_timing.py --repair-input-perms` (the default) does before
each run.

**Suggested fix.**  Make the rewritten block writable before writing it — copy
the mode over, then `os.chmod(target, os.stat(target).st_mode | 0o200)` — or
copy the block with the content but without the permission bits (plain
`shutil.copyfile`).  Separately, the read-only protection on a hard-linked
block cannot be confined to the output: it always reaches the input's inode, so
a pipeline that flags an MS, writes it and flags it again will meet this on the
second run.
