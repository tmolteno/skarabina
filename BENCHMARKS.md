# Skarabina benchmarks

Local timing measurements of skarabina.  The harness that produces them is
`bench/` (see [AGENTS.md](AGENTS.md) for how to run it); each section records
the host, date, backend and workload with its numbers, because a timing without
those is not comparable to anything.

Transcribe a new measurement here when a change claims to make flagging faster,
and keep `bench/meerkat-flags.yml` in step with the `flag-average` step of
`../meerkat_imaging/white-belt-0-flagging.yml`.

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
SSD; skarabina 1.0.5 (git `be38252`), Python 3.14.7, dask-ms `casacure`
backend.  Single measurement, no repetition.

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
