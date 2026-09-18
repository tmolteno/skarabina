<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# The ordered `--flag` option, and selective writes

Status:

| Part | State |
|---|---|
| §2–§4, the ordered `--flag` option and its read cost | **implemented in 1.0.0** |
| §5.2, `--apply` as the recommendation for large/network-mounted MSes | **documentation only** — the behaviour already existed |
| §5.3, `--write-changed-only` | **implemented in 1.0.0** |

This document specifies two changes:

1. a single `--flag` option that carries the flagging operations *and* their
   parameters as an ordered list, and
2. a write path that stops rewriting columns a flagging run never touched.

They are independent, but both address the same complaint: the command line
hides the run order, and the write path hides a large amount of I/O.

## 1. Motivation

The order of the flagging operations is currently fixed in `main.py`:

```
autos → uv-above → (nan, clip) → spectral-window
```

That order is invisible from the command line, and two of the steps are
genuinely order-sensitive:

- `flag_spectral_window` reads the live `FLAG` column to compute per-channel
  flagged counts, so everything before it changes what it does.
- `flag_data` chains NAN and CLIP internally (CLIP ORs onto NAN's result), so
  they cannot be reordered at all today.

There is no way to ask for a different order, and no way to record the order a
run used. The proposal is to make the order explicit in one place: a single
option whose entries are the list, in order.

Scope of the change: this lands in a **major release** which removes the
existing flagging options (§3), and it covers flagging operations only.
`barber` is deliberately excluded — it does not write `FLAG` and so is not a
flagging operation (§2.2).

## 2. The `--flag` option

### 2.1 Syntax

```
--flag "ENTRY, ENTRY, ..."
```

Repeatable; occurrences are concatenated in command-line order, so both of
these are the same run:

```
--flag "save:before, uv-above 2000, save:after"
--flag "save:before" --flag "uv-above 2000, save:after"
```

Entries are `verb [args...]`, separated by top-level commas. Commas inside
square brackets are **not** separators, so a spectral-window rule file is always
a file and ranges keep their commas:

```
--flag "spectral-window rules.yml, uv-above 4000, save:after-uv"
```

Quoting is honoured, so paths containing spaces work:

```
--flag "spectral-window 'my rules.yml', nan"
```

### 2.2 Verbs

| Verb | Args | Equivalent legacy option |
|---|---|---|
| `nan` | — | `--flag-nan` |
| `clip` | `lo hi` | `--flag-clip lo hi` |
| `uv-above` | `metres` | `--flag-uv-above metres` |
| `tfcrop` | `[key=value ...]` | — (new) |
| `rflag` | `[key=value ...]` | — (new) |
| `spectral-window` | `file.yml` | `--flag-spectral-window file.yml` |
| `autos` | — | `--flag-autos` |
| `save:NAME` | — | `--flag-save-before NAME` |
| `restore:NAME` | — | `--flag-restore-before NAME` |

`save:` and `restore:` keep the colon form because they take a bare name.  Every
other verb but the two auto-flaggers takes space-separated positional values;
`tfcrop` and `rflag` take `key=value` pairs, because they have nine and seven
parameters and positional order for that many values would be a trap.  See §9
and §10.

Accepted spellings for the hyphenated verb: `uv-above`, `uvabove`, `uv_above`.
`uv-above` is the documented form; the others are accepted so that a shell
cannot silently change the meaning of a run.

**`barber` is deliberately not a verb, and `--flag barber` is an error.**
`barber` does not flag anything: `skarabina/barber.py` computes statistics over
the currently unflagged data and prints a report, and never writes `FLAG`. It
is a read-only diagnostic, so it has no place in an ordering of flagging
operations, and accepting a `barber` token would imply an ordering relationship
that does not exist. It stays a separate CLI option, `--barber` /
`--barber-pol`, with its current behaviour and its current position (after
`--summary`). The parser rejects `barber` as an unknown verb rather than
ignoring it, so a run that expects it to be ordered fails loudly.

### 2.3 What does not move

Unchanged, and explicitly outside `--flag`:

| Option | Why |
|---|---|
| `--ms`, `--msout`, `--clobber`, `--apply`, `--split` | I/O, not flagging |
| `--scan` | row selection; runs before flagging by construction |
| `--frequency-average-factor`, `--time-average-factor` | change row/channel structure; must see the final flags |
| `--optimize`, `--keep-fully-flagged-channels` | removes rows/channels; must run after all flagging |
| `--summary`, `--barber`, `--barber-pol` | read-only reports |
| `--field-of-view`, `--debug` | diagnostics |

### 2.4 Ordering

The list **is** the run. Every entry that appears runs, exactly once, in the
order written; every verb that does not appear does not run. There is no
"enabled but unlisted" operation to place, because the list is the only way to
request an operation — which is why this interface has no canonical-order
machinery at all.

```
--flag "save:before, uv-above 2000, save:after"
```

runs `save:before`, then `uv-above 2000`, then `save:after` — and nothing else.
If the run also needs `nan` and `clip`, they must be in the list:

```
--flag "save:before, uv-above 2000, clip 0 100, nan, save:after"
```

Two properties worth stating, because they are what make the ordering real:

- **Position is the only order.** There is no hidden canonical sequence to
  reason about: `clip 0 100, nan` runs clip before nan, because that is the
  order written, and the reverse list runs them the other way.
- **Markers keep their place.** `save:` and `restore:` are not verbs and have
  no ordering relationship to each other or to the operations; they act on the
  flag state at the point they appear, which is what makes
  `save:X, <ops>, restore:X` a meaningful snapshot and rollback.

A consequence to accept consciously: writing a long run means naming every
operation, including the ones in the "obvious" order. The canonical order the
0.8.x flagger used is documented in §3 as the migration reference, but the new
interface does not assume it.

### 2.5 Validation

Fail loudly rather than guess:

- Unknown verb → error listing the valid verbs.
- Wrong argument count (`clip 100`, `uv-above`, `save:`) → error showing the
  verb's expected form.
- Empty entry (a stray comma) → error.
- A `save:NAME` / `restore:NAME` name that is empty or contains whitespace or a
  path separator → error.
- A repeatable verb may appear more than once; the operations accumulate (two
  `spectral-window` entries with different rule files are legitimate).
- Repeating `save:` with the same name is allowed and follows the existing
  rename-aside behaviour (`<name>.old.<timestamp>`).

### 2.6 `--flag-file`

For long sequences, `--flag-file FILE` reads the same grammar from a YAML list
or a plain text file, one entry per line, with `#` comments. `--flag` and
`--flag-file` concatenate in command-line order. This exists because a
twelve-entry sequence is unreadable inside a shell string, and because a rule
set that matters should be reviewable and version-controllable.

## 3. Breaking change (major release)

This ships in a **major release** and removes the old flagging options outright.
There is no deprecation window and no dual interface: `--flag` (and
`--flag-file`) become the only way to request flagging, so there is exactly one
code path and one way to express a run.

| Removed | Replacement |
|---|---|
| `--flag-nan` | `--flag nan` |
| `--flag-clip lo hi` | `--flag "clip lo hi"` |
| `--flag-uv-above m` | `--flag "uv-above m"` |
| `--flag-spectral-window f` | `--flag "spectral-window f"` |
| `--flag-autos` | `--flag autos` |
| `--flag-save-before N` | `--flag save:N` |
| `--flag-restore-before N` | `--flag restore:N` |

Not removed, and unchanged: `--barber` and `--barber-pol` (§2.2 — barber is a
read-only diagnostic, not a flagging operation), plus every option listed in
§2.3.

The `skarabina` cab loses the corresponding inputs —
`flag-nan`, `flag-clip`, `flag-uv-above`, `flag-spectral-window`, `flag-autos`,
`flag-save-before`, `flag-restore-before` — and gains `flag: List[str]` and
`flag-file`. `barber` and `barber.pol` stay on the cab unchanged.

Because the removal is silent at the CLI level (an unknown option is a Click
error, not a migration hint), the release must carry:

- a **migration section in `doc/CHANGES.md`** with the table above, flagged as
  breaking;
- a **major version bump** in all three version files per `AGENTS.md`. The
  current version is 0.8.9, so this is **1.0.0** — the first major release, and
  the flagging rewrite is what earns it. `AGENTS.md` requires
  `pyproject.toml`, `cargo/pyproject.toml` and
  `cargo/skarabina_cargo/genesis/skarabina-cargo-base.yml` to agree, and the
  image tag must have no `v` prefix;
- the table repeated in `doc/usage.md`, whose option list currently documents
  every legacy option;
- a note in the cargo README, since existing stimela recipes naming the removed
  inputs will fail validation on upgrade and need rewriting.

The rationale for taking the break rather than deprecating: the legacy options
cannot express an order, so any recipe using them would keep running the old
fixed order; keeping both interfaces would preserve the confusion the change
exists to remove, and the translation shim would have to be maintained and
tested for a release that is already rewriting the flagging path.

## 4. Efficiency requirement

Splitting NAN and CLIP into separately ordered operations costs a dask pass if
statistics are computed eagerly after each step. Measured on a 200 000-row ×
256-channel MS:

| Approach | Passes | Time |
|---|---|---|
| today (NAN+CLIP in one `flag_data` call) | 4 | 0.88 s |
| per-operation stats computed eagerly | 5 | 1.28 s (+45%) |

**Requirement: defer statistics.** Each operation appends its reduction to a
list instead of computing it; a single `dask.compute` at the end of the
sequence evaluates them together. Dask shares identical subexpressions within a
single compute, so the expensive `abs(DATA)` is built once however many
operations consume it. Measured:

| Approach | Time |
|---|---|
| both operations' stats consolidated into one compute | 0.36 s |
| the same two computed separately | 0.59 s |
| one operation's stats alone (for comparison) | 0.28 s |

So consolidation costs ~1.3× a single operation rather than 2×, and the
refactor must not compute statistics inside each step.

### 4.1 Reads, not just passes

The same requirement governs **read** volume, which matters more than CPU when
the MS is network mounted. Counting chunk-level getters in the graphs (a
cache-independent measure, so the numbers transfer to a network mount), a
10-chunk column reads:

| Graph | Getters |
|---|---|
| `FLAG` after nan+clip | `DATA=10` `FLAG=10` |
| `FLAG` after `uv-above` | `DATA=10` `FLAG=10` |
| the `UVW` column | `UVW=10` |

`DATA=10` for a 10-chunk column means **one pass**, even though NAN and CLIP
between them reference `abs(DATA)` four times: dask deduplicates by task key
within a graph. So a sequence of N operations reads each column once *provided
they are built into one graph*. The eager statistics are what break that, since
each `flag_data` call becomes its own compute and therefore its own full pass
over `DATA`.

Requirement, stated for the implementation: **one pass per column per run.** A
sequence of N operations must read `DATA` once, not N times.

### 4.2 Only two operations force a `DATA` pass

Which columns each operation actually depends on:

| Operation | Reads | Forces a `DATA` pass? |
|---|---|---|
| `flag_autocorrelations` | `FLAG`, `ANTENNA1/2` | no |
| `flag_uv_above` | `UVW` | no |
| `flag_data` (`nan`, `clip`) | `DATA`, `FLAG` | **yes** — its statistics |
| `flag_spectral_window` | `FLAG`, `UVW` | no |
| `save:` / `restore:` | `FLAG` | no |

So deferring **only** the `nan`/`clip` reductions is sufficient. Every other
operation can keep printing its per-step counts as it does today, and the
per-step console output — which §6.1 requires — stays meaningful.

### 4.3 What dask cannot do

Dask shares work **within** a single `compute` call. It does not share across
separate calls unless the shared subgraph is `.persist()`ed, and pinning
`abs(DATA)` in memory for a 124 GB MS is exactly the blow-up to avoid. There is
therefore no "dask will sort it out" option: the sequencing must be explicit in
the implementation. A plausible-looking implementation that computes each
operation's counts as it goes silently multiplies the read cost, which is why
§7 counts getters in a test.

## 5. Minimising I/O when flagging

### 5.1 The problem: both read and write amplification

`write_new_ms` calls `xds_to_table(ds, name, "ALL")`, which **re-reads every
column from the input MS and rewrites it**. A flagging run changes only `FLAG`
(and `FLAG_ROW`), so on a network-mounted MS the cost is dominated by copying
data that did not change — paid twice, once reading and once writing.

Logical column sizes on the real MT0 MS (745 996 rows, 4096 channels, 2
correlations):

| Column | Size | Changed by flagging? |
|---|---|---|
| `DATA` | 48.89 GB | no |
| `WEIGHT_SPECTRUM` | 24.44 GB | no |
| `SIGMA_SPECTRUM` | 24.44 GB | no |
| `FLAG` | **6.11 GB** | **yes** |
| `UVW` | 0.02 GB | no |
| `WEIGHT`, `SIGMA` | 0.02 GB | no |
| **total** | **103.92 GB** | **6.11 GB required** |

So the `--msout` path moves roughly **208 GB through the mount (104 GB read +
104 GB written) to change 6 GB of flags** — a ~34× round-trip amplification, and
about 17× on each of the read and write sides separately. On a benchmark MS the
same effect appears at small scale: a full copy wrote 1685 MB where the flags
alone are 102 MB.

The read side also includes the flagging pass itself (§4): ~48.9 GB of `DATA`
plus ~6.1 GB of `FLAG` on MT0. That part is irreducible — the data must be read
to be flagged — but it must happen **once**, not once per operation.

### 5.2 Recommendation: use `--apply`

**For any MS large enough for I/O to matter — and in particular for a
network-mounted one — use `--apply`.** It is not a compromise or a workaround:
it is the only path today that touches nothing it did not change.

| Path | Reads | Writes | Notes |
|---|---|---|---|
| `--msout` (today) | ~104 GB | ~104 GB | copies every column |
| `--apply` | `FLAG` only | `FLAG` only | in place |
| `--apply` + `save:before` | `FLAG` twice | `FLAG` twice | in place, with rollback |

The mechanism already exists: `update_ms` iterates `self.changed` and writes
only those columns, and `changed` is populated precisely and only by operations
that modify a column — after a flagging-only run it contains `['FLAG']` (plus
`FLAG_ROW` for `autos`). Measured on a benchmark MS, `--apply` of a flagging
run writes 102 MB against 1685 MB for the full copy.

So `--apply` needs no new code, only documentation and a rollback pairing:

- **`--apply` needs `--clobber`**, the existing guard against accidental
  in-place modification.
- **In-place editing needs a rollback**, because there is no untouched copy to
  fall back on. `save:before` (or `--flag-save-before before`) is that
  rollback, and adds only one write and one read of `FLAG` — 6 GB on MT0,
  against the ~208 GB a copy costs. The recommended invocation is therefore:

  ```
  skarabina --ms obs.ms --flag "save:before, nan, clip 0 100" --apply --clobber
  ```

  Restoring is then `--flag "restore:before"`, or CASA's
  `flagmanager(mode='restore')` since the layout is CASA-compatible.
- **The trade-off is real, not free.** A copy produced by `--msout` is
  standalone; an in-place edit changes the only copy, which is why the rollback
  is part of the recommendation rather than an afterthought, and why
  `--write-changed-only` below exists for cases where the input must be left
  untouched.

This should be documented in `doc/usage.md` as the recommended pattern for
large or network-mounted MSes, with `--msout` described as the choice when an
independent copy is wanted for its own sake.

### 5.3 Implemented: `--write-changed-only`

`--apply` is unavailable when the input must be left untouched — read-only
mounts, provenance requirements, or a pipeline that wants the flagged MS
alongside the raw one. `--write-changed-only` is the opt-in mode for the
`--msout` path that mirrors `update_ms` there:

1. Copy the input MS to the output path **without** re-reading and rewriting
   the columns that did not change, then
2. write only the columns named in `self.changed`, exactly as `update_ms` does.

Step 1 is a hard link of each unchanged column's storage block, not a copy, so
the sharing is exact rather than a re-read: the output and the input are the
same inode for those blocks.

**Effect on writes** — measured on the 92 GB MT0 MS (745,996 rows, 4096
channels), flagging with `nan` and `uv-above 6000`:

| | full write | `--write-changed-only` |
|---|---|---|
| bytes written via casacore | 103.0 GB | **6.11 GB** |
| blocks copied or written | 31 | 18 |
| blocks shared with the input (same inode) | 0 | **97.8 GB** |

The write side is a 17× reduction and is the point of the mode. **The read side
is not reduced, and cannot be on this path.** `write_new_ms` reads its input
through dask-ms, and dask-ms builds the whole measurement set's read graph
around the table handle it opens to write: a run that changed only `FLAG` still
executes the full set of `read~DATA` tasks. Measured, `flag_data`'s statistics
execute 75 `read~DATA` tasks and the write executes the same 75 again. Passing
`xds_to_table` a dataset holding only the column being written makes no
difference — the same 75 tasks run — so the read cost is a property of the write
path, not of which variables are named.

Two consequences:

- Where the *read* dominates, `--apply` is the better answer, because
  `update_ms` writes columns in place through casacore and never builds that
  read graph.
- The earlier estimate in this document, that the mode would cost ~6 GB read on
  MT0, was wrong: it assumed the unchanged columns were the only thing not read.
  The honest figure is ~6 GB written and a full DATA pass read.

Constraints, and the implementation's answer to each:

- **It writes the same columns `--apply` would.** A run that also averages must
  still write `DATA`/`WEIGHT_SPECTRUM`/`SIGMA_SPECTRUM`, so the mode is "write
  what changed", not "write flags only". It is never worse than a full write,
  and the saving shrinks as more columns change — an averaging run rewrites
  `DATA` and gains nothing.
- **`--split` and averaging are incompatible with sharing**, because they change
  the row or channel dimension and so every column's layout. Both fall back to a
  full write with a warning naming the reason, rather than sharing blocks whose
  shape no longer matches. This is checked up front by `_changed_only_blocker`,
  not discovered halfway through the write.
- **Default stays off.** Changing the default write path silently would be a
  large behavioural change; make it opt-in, measure it, and revisit.
- **A dry run is not included.** `_write_changed_only` does report what it did
  ("sharing 4 unchanged column group(s), rewriting 1: FLAG", then the block
  counts), but it reports after the fact rather than predicting the column sizes
  beforehand. The need is smaller than it looked: `self.changed` is known before
  any I/O, so the only unknown the report would add is which *blocks* back each
  column, which does not change the decision to use the mode.

**Safety.** A hard link has one inode, so a column shared with the input is
writable through either path. The shared blocks are therefore `chmod`ed
read-only once written, which means a later write to a shared column fails — on
the input as well as the output — instead of silently corrupting the raw MS.
Reading is unaffected. The rewritten columns keep their own fresh blocks and
stay writable, so the output is a normal MS for the columns that actually
changed. `tests/test_write_changed_only.py` asserts each of these properties
separately, including that the input's `FLAG` is untouched by the output write.

One consequence worth stating: the output is not independent of the input. The
shared blocks are the input's blocks, so deleting or editing the input's
*unchanged* columns later will damage the output. The flagged column is
genuinely separate; the rest is a link. Where an independent MS is required for
its own sake, `--apply` onto a copy is the honest pattern.
### 5.4 Not proposed

Rewriting the flag array in place with casacore (`putcol`) instead of
dask-ms's `xds_to_table` would avoid materialising the entire `FLAG` array, and
has been used elsewhere in this project for the subtable bookkeeping. It is a
larger change, needs its own measurement, and is out of scope here.

## 6. Acceptance criteria

1. `--flag "save:before, uv-above 2000, save:after"` runs `restore`/`save`
   semantics at the stated positions, and the console output names each step in
   the order it ran.
2. `--flag "clip 0 100, nan"` runs clip before nan — the ordering is real, not
   cosmetic. Concretely: the console output shows clip before nan, and a test
   asserts the effective order, since for these two operations the final ORed
   `FLAG` happens to be the same in either order and so cannot itself detect a
   no-op reordering.
3. The removed options are gone: each of `--flag-nan`, `--flag-clip`,
   `--flag-uv-above`, `--flag-spectral-window`, `--flag-autos`,
   `--flag-save-before` and `--flag-restore-before` produces a Click "no such
   option" error, and the cab schema no longer declares the corresponding
   inputs. `--barber` and `--barber-pol` still work exactly as before, and
   `--flag barber` is rejected as an unknown verb.
4. The number of dask compute passes over the data is unchanged from today for
   an equivalent run (statistics consolidated, not per-step).
5. **Read amplification is gone**: a sequence of N flagging operations reads
   `DATA` once, not N times, and the set of columns any run reads is the set it
   needs. Asserted by counting chunk-getters in the graphs, which is
   cache-independent.
6. `--write-changed-only` reads and writes only the changed columns; on MT0 a
   flagging-only run drops from ~104 GB each way to ~6 GB each way, verified by
   instrumenting what `xds_to_table` is asked to read and write.
7. `--apply --clobber` with `save:NAME` complete and restorable, and are
   documented as the recommended pattern for large or network-mounted MSes.
8. `--barber` and `--barber-pol` behave exactly as before.

## 7. Test plan

Implemented in `tests/test_flag_ops.py` (grammar, ordering, cost),
`tests/test_flag_operations.py` (operation independence, deferred statistics)
and `tests/test_flag_versions.py` (save/restore).

- **Parser**: every verb, the `uv-above` aliases, quoted paths, bracketed
  commas, the YAML-list form, repeated `--flag`, `--flag-file`, and each error
  case in §2.5 — including that `barber` is rejected with a message naming
  `--barber`.
- **Ordering**: the list order is the run order, and `nan, clip` versus
  `clip, nan` really do swap. Asserted on the parsed order and on the sequence
  announced by `flag_ops.run`, since for these two operations the final ORed
  `FLAG` is the same either way and so cannot detect a no-op reordering.
- **Removal**: each removed option errors, and the cab schema no longer
  declares the removed inputs.
- **Read count**: the acceptance test for §4.  Run the flagging sequence under
  an instrumented dask scheduler and count executions of tasks that load
  `DATA`; a two-operation sequence must execute **one**, not two.  Measured
  against the eager path, which executes two, so the test fails if per-step
  statistics are reintroduced.  Counting executed tasks is used rather than
  graph keys — keys are shared in both cases and so cannot tell the two apart —
  and rather than bytes, so the test is cache-independent.
- **Deferred statistics are reporting only**: the same fixture flagged with and
  without `defer` must produce byte-identical `FLAG`.
- **Rollback**: `save:NAME` then flag then `restore:NAME` returns the MS to its
  pre-flagging flags (covered in `tests/test_flag_versions.py`).
- **Write path**: `tests/test_write_changed_only.py`.  The output is compared
  column by column against a full write; sharing is asserted on storage-block
  *inodes*, not on sizes or timings, so the test cannot pass by accident; and
  the input's own `FLAG` is checked to be untouched afterwards.
- **TFCrop**: `tests/test_tfcrop.py`, described in §9.5.

## 8. Open questions

1. `--summary` currently runs before `--barber`, so a run with both prints a
   summary that the barber report does not contribute to. Barber is read-only so
   this is cosmetic, but the order should be settled when this lands.
2. Should `--barber-pol` be folded into `--barber` as an optional value
   (`--barber 0`), matching the way verb arguments are given? It is a separate
   option today and this change does not touch it.
3. The major release will also carry the `--keep-fully-flagged-channels` and
   band-hole work from 0.8.9; confirm the changelog presents the flagging
   rewrite as the headline breaking change rather than burying it among fixes.


## 9. The `tfcrop` verb

A reimplementation of CASA's `flagdata(mode='tfcrop')`, described in the CASA
User Reference §3.4.2.7 and NCRA Technical Report 202 (Oct 2003).  The
implementation is `skarabina/tfcrop.py` and is deliberately free of dask: it
operates on one `(time, chan)` plane of numpy and knows nothing about how the
data is stored.

### 9.1 Why a bandpass fit is needed at all

RFI appears as outliers in the time-frequency plane of a single baseline and
correlation.  A plain amplitude clip cannot separate it from the bandpass,
because the bandpass is itself a large, smooth, frequency-dependent gain: a
threshold that catches a weak spike at the band edge also flags the whole bright
end of the band.  So TFCrop fits the bandpass first and flags the *residuals*:

1. average the chunk over time to get the mean bandpass, and fit a robust
   piece-wise polynomial to it.  "Robust" matters: the fit must follow the base
   of the RFI spikes, not be dragged up by them;
2. divide that fit out of every timestep.  The result is flat -- near 1 wherever
   the band is clean -- so one threshold means the same thing at the band edge
   and in the middle;
3. flag points deviating from 1, iterating so that the scatter estimate is
   itself computed from the surviving points;
4. repeat the whole thing the other way: average over frequency, take each
   column's own baseline, and flag deviations from that.

### 9.2 Grammar

Parameters are `key=value`, in any order, and any subset may be given.  The
brackets are optional and purely for grouping, and `:` may be used for the
separator instead of `=`:

```
--flag "tfcrop"
--flag "tfcrop timecutoff=5 freqcutoff=2.5"
--flag "tfcrop [timecutoff=5, freqcutoff=2.5, maxnpieces=3]"
--flag "tfcrop timecutoff: 5 freqcutoff: 2.5"
```

A colon is only a separator when a real parameter name precedes it.  `save:` and
`restore:` are the grammar's other colon syntax and a rule file path may itself
contain a colon, so treating a colon as a separator in general would break both.

**In a recipe, the entry must be one string.**  A stimela input of type
`List[str]` requires every element to be a string, so a nested mapping is
rejected before the cab runs:

```yaml
flag:
  - tfcrop:            # WRONG: "Input should be a valid string"
      - timefit: line
```

An unquoted `: ` inside a YAML sequence item is also invalid YAML.  Quote the
whole entry, and put the parameters inside it:

```yaml
flag:
  - save:before
  - "tfcrop timefit: line usewindowstats: both"
  - "tfcrop [flagdimension=freqtime, maxnpieces=5]"
```

The same applies to a path with spaces in a `spectral-window` entry.

The names are CASA's, so a recipe written for `flagdata(mode='tfcrop')`
transfers unchanged.  A comma *between parameters* requires the brackets,
because at the top level a comma separates `--flag` entries; inside brackets it
does not, which is the same rule that lets `spectral-window` take a file.

`TFCropParams` validates every value at parse time, so `maxnpices=3` is an error
naming `maxnpieces` rather than a silently ignored setting.  That check is the
reason the parameter list is not simply passed through to the algorithm.

Stimela escapes `[` and `]` when it hands a parameter to a container, so a
bracketed entry arrives as `\[...\]`.  The parser accepts that form, because
otherwise the bracket syntax would break in exactly the case it was introduced
for.

### 9.3 Parameters

| Name | Default | Meaning |
|---|---|---|
| `timecutoff` | 4.0 | threshold in robust sigmas, time direction |
| `freqcutoff` | 3.0 | threshold in robust sigmas, frequency direction |
| `timefit` | `line` | fit function along time (`line`/`poly`) |
| `freqfit` | `poly` | fit function along frequency (`line`/`poly`) |
| `maxnpieces` | 7 | most pieces in a piece-wise fit (1-7) |
| `flagdimension` | `freqtime` | `freqtime`/`timefreq`/`freq`/`time` |
| `usewindowstats` | `none` | `none`/`sum`/`std`/`both` |
| `halfwin` | 1 | half-width of the sliding window (1-3) |
| `combinescans` | `false` | accepted for compatibility; see §9.4 |

`ntime` is deliberately **not** offered.  In CASA it chooses the chunk of time
the bandpass is averaged over; here the dask chunk plays that role, so the
chunk length *is* `ntime` and a separate parameter could only contradict it.

### 9.4 Deliberate deviations from the published algorithm

Each of these is a place where the description does not determine an
implementation, and the choice is recorded here rather than left implicit.

1. **The piece count grows from 1 to `maxnpieces`.**  This follows the
   published description, and the first implementation got it wrong by fixing
   the count from the start.  With seven pieces from the outset and no rejection
   yet performed, a cubic will happily bend to follow an RFI spike, so the spike
   never looks like an outlier and is never removed.  Starting at one piece
   makes the first fit a low-order curve that RFI cannot bend, so the outliers
   are obvious immediately, and the extra pieces then refine the band shape
   around them.  Measured on a band with spikes straddling a piece boundary, the
   fixed-count version mis-fitted by 0.31 in a band whose clean points fit to
   0.0001; growing the count removed the error while still rejecting every
   spike (0.095, against 0.097 for the best possible fit to the known-clean
   points).
2. **The fit is tapered at the ends of each piece.**  A polynomial fitted to a
   span is least trustworthy at its outermost samples, and a boxcar weight made
   the robust iteration reject the *first and last channel of a clean band*.
3. **The rejection threshold has an absolute floor**, set to 0.1% of the data's
   own scale.  A noiseless or nearly-noiseless plane -- a deterministic model,
   or data already calibrated and averaged -- fits its own polynomial so exactly
   that the residuals underflow, and a purely relative 3-sigma rule then rejects
   every point.  The floor is scaled to the data so it means the same in Jy and
   in K.
4. **A piece is only trusted near its own surviving samples.**  The rejection
   iterations can strip a piece down to a cluster of channels at one end --
   exactly what happens to a piece containing RFI at the other end -- and the
   polynomial then has nothing to say about the empty part.  Extrapolating there
   is meaningless: measured, one such piece reached 395 on a band whose values
   run 7 to 16, and the next rejection pass removed almost everything.  Those
   channels are interpolated between the neighbouring fitted regions instead.
5. **The two directions are computed independently.**  The published
   description runs the second direction after the first, so the first
   direction's flags are already excluded from the second's average.  Here both
   are computed from the *input* flags, so neither biases the other.  The four
   `flagdimension` spellings therefore reduce to: union (`freqtime`,
   `timefreq`), frequency only (`freq`), time only (`time`).  The order within
   the name carries no meaning.
6. **`combinescans` is accepted but does nothing.**  The chunk is the fit unit
   and a chunk does not cross a scan boundary in the data this tool reads, so
   the parameter has nothing to control.  It is accepted so that a CASA recipe
   does not fail on an unknown name, and rejecting it as unsupported would be
   worse than accepting it as a no-op.  This is the one parameter whose
   acceptance is not backed by behaviour.
7. **Window statistics are approximate.**  `sum` and `std` are CASA's own
   approximations to the LOFAR sum-threshold and AIPS `rflag` statistics, and
   are marked experimental there.  They are kept for parity, not because either
   is well founded, and the sliding window is clipped at the plane edges rather
   than wrapped or shrunk.

One consequence worth stating: a channel that is bright in *every* integration
is invisible to the time direction by construction, because that direction
averages over frequency and a constant-in-time channel is part of the mean
rather than a deviation from it.  Only a `freq`-containing mode can find
narrow-band RFI; only a `time`-containing mode can find a bad integration.  The
default `freqtime` is the union of both, which is why it is the default.

### 9.5 Tests

`tests/test_tfcrop.py`, on synthetic planes with RFI at known positions, so that
recall and false-positive rate can both be measured.  A flagger that flags
everything scores 100% recall and is useless, so every recall assertion is
paired with a false-positive bound.  The properties pinned are:

- the robust fit tracks a smooth bandpass (max error 0.0001 on a band of width
  ~8) and beats a plain polynomial fit by ~4x on a band with spikes, rejecting
  every spike;
- the fit converges, and no pass diverges -- the specific regression above;
- the adaptive scatter shrinks as outliers are removed, so a single pass
  estimates a larger scatter than five;
- a clean plane is left almost untouched (under 1% flagged);
- narrow-band RFI and bad integrations are both found, with the false-positive
  rate bounded;
- pre-existing flags are excluded from the fits and preserved in the result;
- the band and time axes are not transposed.  This is the regression test for a
  bug that survived every other test, because they all fitted a single block and
  so never depended on how a block is sliced.  Transposing the two axes made
  each block a plane of two channels rather than the whole band, and *every*
  visibility in the MS came back flagged -- 100%, from a change that looked like
  tidying.


## 10. The `rflag` verb

A reimplementation of CASA's `flagdata(mode='rflag')`, which Eric Greisen
developed in AIPS.  The implementation is `skarabina/rflag.py` and, like
`tfcrop`, is free of dask: it works on one `(time, chan)` plane of complex
visibilities.

### 10.1 What it does that tfcrop does not

TFCrop fits the bandpass and flags what does not follow it.  RFlag asks a
different question -- is the *scatter* here unusual? -- and needs no model of the
band at all:

1. **Time analysis, per channel.**  Slide a window of `winsize` integrations
   along time and measure the local scatter.  Take the median of those, and the
   median absolute deviation from it, then flag where the local scatter sits
   more than `timedevscale` deviations above.
2. **Spectral analysis, per sample.**  Compare each sample with the median of
   its neighbouring *channels*, and flag where it departs by more than
   `freqdevscale` times the typical such departure.

Both steps are medians, which is the point: a mean would be dragged around by
the very RFI being looked for, and the algorithm would then miss it.

The two steps are complementary, and the split falls out of the data:

| RFI | found by |
|---|---|
| a burst in a few integrations | the time step -- a channel bright in 5 rows of 200 is invisible in any average |
| a narrow-band feature present throughout | the spectral step |
| a broadband burst | the time step |

### 10.2 Grammar

The same as `tfcrop` (§9.2): `key=value` pairs, `:` also accepted, brackets
optional, validated at parse time so a typo is an error naming the real
parameter.  Delivered from a recipe, the entry must be one quoted string, for
the reason given in §9.2.

### 10.3 Parameters

| Name | Default | Meaning |
|---|---|---|
| `winsize` | 3 | integrations in the sliding time window |
| `timedev` | unset | time-series noise estimate; measured from the data when unset |
| `freqdev` | unset | spectral noise estimate; measured when unset |
| `timedevscale` | 5.0 | threshold multiplier for the time step |
| `freqdevscale` | 5.0 | threshold multiplier for the spectral step |
| `spectralmax` | 1e6 | flag the whole spectrum if the measured deviation exceeds this |
| `spectralmin` | 0.0 | flag the whole spectrum if it falls below this |

`ntime` and `combinescans` are absent for the same reason as in `tfcrop`: the
chunk the statistics are gathered over is the dask chunk, so `ntime` is the
chunk length and there is no separate control to contradict it.

Supplying `timedev`/`freqdev` is what makes the two-pass workflow work -- CASA's
`action='calculate'` writes the measured thresholds out, a user reviews them,
and a second pass supplies them.  A supplied value is used as-is rather than
mixed with anything measured.

### 10.4 Deliberate deviations and hard-won details

1. **The local statistic is the scatter about the window's own mean**, not the
   r.m.s. about zero.  The distinction is what makes a supplied `timedev`
   meaningful at all: the r.m.s. about zero of a 10 Jy source in a 0.05 Jy noise
   floor is 10, so a threshold of `timedevscale * 0.05` would flag everything.
   Measured about the window mean, the same data gives 0.05.  Getting this
   wrong flagged 91 % of a clean plane.
2. **The robust scale is `median(|x|)`, not the MAD about the median.**  They
   agree for centred data, but the MAD is measured about the median and so is
   inflated by the outliers themselves once they are more than a small fraction
   of the sample -- which is exactly the case for a residual whose typical value
   is zero.  Measured on a time burst three channels wide, `median(|x|)` is
   0.0011 against a burst of 0.72 where the MAD about the median gives 0.0021;
   scaled by five, the first flags the burst and the second flags nothing.
3. **The spectral step compares each sample with its neighbouring channels**
   rather than with a smoothed band.  A running median was tried first and is
   degenerate on this data: it sits *exactly* on a smooth band, so most
   residuals are identically zero, every quantile-based scale for them is zero,
   and the threshold collapses -- which flagged the four channels at the band
   ends, where the clipped kernel does leave a blip.  A neighbour difference
   always carries the channel-to-channel noise, so its scale is well defined.
   It is also what keeps a burst confined to its own rows: a channel bright for
   five integrations of two hundred is invisible in the time average.
4. **A deviation of exactly zero is left alone rather than given an invented
   threshold.**  An earlier version fell back to a fraction of the signal level,
   which on a clean plane put the threshold *between* the noise and the
   numerical blip at the band ends and flagged exactly those channels.
5. **`spectralmin`/`spectralmax` are compared with the measured deviation** and
   flag the whole spectrum on an excursion, as described.  Below `spectralmin`
   the band is smoother than it should be -- a correlator or a model gone flat
   -- and above `spectralmax` it is too rough for any channel to be trusted.

### 10.5 The spectral step's characteristic, which is worth knowing

Because each channel is compared with its neighbours, the band's own slope
enters the scale and a smooth band passes untouched -- the test suite runs clean
planes at 64 % peak-to-peak with zero flags.  What the step cannot distinguish
from RFI is a *step* in the band: a channel standing above its neighbours by
more than a few times the channel-to-channel noise is flagged, and nothing in
the data separates that from a genuine narrow feature.  Measured, a 5 % step is
flagged at every channel count tried, 64 to 4096.

This is why CASA says the spectral step "depends on having a relatively-flat
bandshape", and it has a practical consequence: on a coarse channel grid with a
steep band shape, the adjacent-channel difference of the band itself is large
compared with the noise, and a user should supply `freqdev` rather than let it
be measured.  On a fine grid -- thousands of channels, where the band changes
little from channel to channel -- measuring it is well behaved.

### 10.6 Tests

`tests/test_rflag.py`, with the same structure as the tfcrop tests: recall and
false-positive rate both measured, and a recall assertion never left without a
false-positive bound.

- the local scatter recovers the **noise**, not the signal, for a 10 Jy source in
  a 0.05 Jy floor -- the property a supplied `timedev` depends on;
- the robust scale is not inflated by a few large outliers, and is not the MAD
  about the median;
- a smooth band registers no neighbour deviation, and the deviation is never
  identically zero -- the degenerate case above;
- **five clean seeds** flag nothing, because a threshold slightly too tight
  flags a handful of pixels and one seed can miss it;
- a time burst is flagged in its own rows and nowhere else;
- a narrow-band spike is flagged across time, together with exactly its two
  neighbouring channels and no more;
- spikes down to 1.2x, which a plain amplitude clip cannot see without also
  flagging the bright end of a band that spans 64 %;
- a supplied noise estimate flags nothing on a clean plane and still finds a
  spike;
- pre-existing flags are preserved, counted, and excluded from the statistics;
- the band and time axes are not transposed, on a cube spanning several blocks.
