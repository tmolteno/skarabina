<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Changelog

## [Unreleased]

## [1.0.7]

### Added

- **A run reads DATA and FLAG once, however many ``--flag`` verbs it has.**
  Each verb used to compute its own counts, so "nan, clip 0 100, autos" with
  ``--write-changed-only`` read DATA twice and the stage-0 list with
  ``--summary`` four times (one of them through an xarray ``__array__`` that
  also held the whole flag cube in memory).  The counts are now queued and
  computed in one pass that also materialises the final flags to the
  bit-packed spill -- or in rflag/tfcrop's own pass -- and the summary,
  averaging and the write read the spilled flags.  On a 143 716-row,
  2511-channel MeerKAT scan, the stage-0 list with ``--summary`` and
  ``--write-changed-only`` went from 22.0 GB of DATA read (4 passes), 27.3 s
  and 6.3 GB peak to 5.5 GB (1 pass), 10.0 s and 3.2 GB.  Measured with a
  probe on dask-ms's column reads; ``tests/test_single_pass.py`` counts them.
  A full ``--msout`` write -- with or without averaging -- is that pass too:
  the flags, rflag/tfcrop, the averaging, the summary and the write are one
  ``dask.compute``.  The stage-0 list with rflag, 32x frequency averaging,
  ``--summary`` and a full write went from 11.0 GB of DATA read (2 passes) and
  350 s to 5.5 GB (1 pass) and 312 s; it holds the write's chunks alongside
  rflag's (31.0 GB peak against 25.6 GB), which the memory plan now counts.
  Only ``--optimize``, which must see the flags before choosing the rows to
  write, keeps a second pass.  The per-verb report lines now print when the
  pass completes.

- **``--memory-limit-GB`` and a memory plan per run.**  Without
  ``--row-chunk``, skarabina picks the largest row chunk that keeps every step
  of the ``--flag`` list within the limit -- by default the RAM available now
  (MemAvailable, capped by a container's cgroup limit) -- so a list of cheap
  verbs gets a large chunk and one with rflag or tfcrop a smaller one, and
  never so large that a worker goes idle.  Each verb's cost is measured
  (``doc/RFLAG.md`` §7.4).  ``save:<name>`` and a full ``--msout`` write hold a
  whole table whatever the chunk (casacure buffers what it writes); the plan
  estimates them and warns when they do not fit.  The run prints the plan.  An
  explicit ``--row-chunk`` is used as given, and checked.  The stimela cab
  gains ``memory-limit-GB``, ``row-chunk`` and ``workers``.
- **``restore:`` reads the flag version lazily**, in the data's row chunks,
  instead of holding the whole flag cube for the rest of the run.

### Changed

- **rflag's spectral step needs 40 % less memory**: it held the real and
  imaginary residuals, a stacked copy and their absolute values at once
  (4.9x the chunk's DATA per worker, now 3.0x).  Results unchanged.

- **``tfcrop`` and ``rflag`` separate the baselines of a row chunk.**  An MS is
  written time-major, so a dask row chunk is ~1900 baselines per integration,
  not one baseline's time series, which is what both flaggers assumed.  On a
  MeerKAT L-band calibrator scan that made tfcrop flag 27 % of the RFI-free
  band (4.3 % on single-baseline planes), and left rflag's time step comparing
  unrelated baselines.  Rows are now grouped by (scan, baseline); rflag's
  windows stay inside a baseline, tfcrop fits a bandpass per baseline, and both
  measure their thresholds in units of each baseline's noise from a
  per-antenna model (noise_ij ~ s_i s_j), which on that scan predicted every
  baseline's noise to 1.5 %.  ``dask_ms.AUTOFIT_BASELINES = False`` restores
  the old behaviour.  See ``doc/RFLAG.md``, which also compares skarabina's
  rflag with CASA's source.

- **``tfcrop`` is about 10x faster.**  The per-row and per-column flagging
  (``flag_1d``, looped over every row and column of a block with two
  ``np.median`` calls per iteration) is vectorised as ``flag_lanes`` on the
  sort-based median, bit for bit the same.  The time-direction baselines are
  fitted for all columns at once (``robust_fit_columns``), solving each piece's
  weighted least squares through its normal equations instead of one SVD
  ``lstsq`` per column and piece; results agree to rounding.  That work was
  interpreter-bound and serialised dask's threads on the GIL, so the run now
  also scales with ``--workers``.  The ``usewindowstats`` pass, a per-point
  Python loop (~5 s per 10 000 x 79 plane with ``both``), reduces strided
  window views instead.  On a 430k-row, 79-channel synthetic MS: 82 s before,
  8.4 s after, with identical written flags (``BENCHMARKS.md``).  A plane of
  exact constants no longer has a column flagged by ``lstsq`` rounding.

### Fixed

- **rflag's time step no longer goes blind on flagged data.**  The window
  scatter divided by the window's length rather than its usable samples, so
  every flagged sample counted as a zero visibility; with 30 % of samples
  flagged the threshold rose until nothing was flagged.  A window with a
  single usable sample now gives no estimate instead of zero.
- **tfcrop fits narrow bands.**  A piece now keeps at least ``degree + 3``
  channels: an 8-channel band was split into unfittable 1-2 channel pieces and
  "fitted" as a step.
- **``restore:`` followed by ``rflag``/``tfcrop`` no longer fails** with an
  ``IndexError`` on a multi-chunk MS; the restored flags are rechunked to the
  data.

- **``tfcrop`` no longer measures its scatter from samples that are already
  flagged.**  ``flag_1d`` -- and so both directions of every plane -- took the
  robust sigma over every finite sample, flagged or not.  A row that arrives
  mostly flagged is usually mostly dead: with 62 % of each row at zero the
  zeros became the median, the measured scatter collapsed to 0, and the
  noiseless fallback flagged *every* live sample (5760 of 5760 in the new
  test).  Pre-existing flags are now left out of the scatter and stay
  flagged, and each direction reports only its own new flags, so the window
  statistics no longer count the pre-existing ones either.  The cost is a
  smaller sample per lane: on the synthetic bench MS, whose pre-flagged
  samples happen to be clean noise, false positives on clean data went from
  0.71 % to 1.19 % of the live samples, with all injected RFI still caught.

## [1.0.6]

### Added

- **A ``casacure`` extra: ``pip install skarabina[casacure]``.**  casacure is
  the pure-Rust drop-in replacement for casacore.  The dependency list already
  asks for dask-ms's own ``casacure`` extra, but that extra exists only in the
  tmolteno/dask-ms fork, so this extra installs casacure itself, which is what
  a published wheel needs.  The dask-ms that drives it is the fork, resolved by
  ``uv sync`` in this repository; from PyPI, pip pulls upstream dask-ms, so
  ``doc/INSTALL.md`` gives the explicit fork route for that case.  Runs select
  the backend with ``DASK_MS_BACKEND=casacure``.

### Changed

- **``rflag`` is about 8x faster, and its memory no longer grows with the
  table.**  The neighbour medians used ``np.nanmedian``, whose small-axis path
  goes through masked arrays and was ~90 % of the algorithm's time; they now
  come from one sort (same result, bit for bit).  The time step is vectorised
  over groups of channels rather than looped per channel.  ``rflag`` and
  ``tfcrop`` no longer ``persist`` the whole flag cube: each block's flags are
  written to a spill directory (``$TMPDIR`` if set, otherwise beside the input
  MS) at one bit per visibility, and the counts come back from the same pass,
  so the incoming flags are no longer evaluated a second time just to count
  them.  On a 430k-row, 79-channel synthetic MS: 65 s / 1.8 GB before, 8 s /
  0.9 GB after, with identical flags (``BENCHMARKS.md``).

### Fixed

- **``rflag`` no longer flags nearly everything on heavily flagged data.**
  Flagged samples were masked with ``np.where(flagged, np.nan, plane)``, which
  on complex data gives ``nan+0j``: the imaginary part of every flagged sample
  entered the statistics as a zero.  On a calibrator scan that arrives ~70 %
  flagged this pulled the spectral step's measured deviation down tenfold, and
  every remaining sample then exceeded the threshold -- the 100 % recorded in
  ``BENCHMARKS.md`` for bpcal.ms.  Flagged samples are now NaN in both parts.
  On a synthetic MS of the same shape with ~0.1 % of rows carrying RFI, the
  run went from 19.6 M new flags (97.7 % of the MS in total) to 43 540.

- **A second ``--write-changed-only`` run over the same input no longer fails
  with ``storage error: Permission denied``.**  Sharing the unchanged columns
  makes their blocks read-only, and since a hard link is one inode that lands on
  the *input's* blocks as well; the next run had to rewrite one of those
  columns, copied it into its output with ``shutil.copy2`` -- which preserves
  the mode -- and then could not write the copy.  Blocks copied into the output
  are now left writable (they belong to the output alone, only the linked ones
  are shared), so flagging the same measurement set twice works, while the
  read-only protection on the shared blocks is unchanged.  Found while timing
  the flagging bench; reported as issue #3.

## [1.0.5]

### Fixed

- **The ``tfcrop``/``rflag`` write no longer runs the flagging algorithm
  twice.**  The report counts were reductions of the same lazy blocks that the
  write's ``compute`` then evaluated again -- ``delayed`` results are not
  cached between ``compute`` calls, so every block ran twice per run, for a
  number already in hand.  The flags are now ``persist``-ed once, so the report
  and the write (and any downstream computation fused into the same dask graph)
  share a single evaluation.

## [1.0.4]

### Added

- **An ``rflag`` flagging verb**, a reimplementation of CASA's
  ``flagdata(mode='rflag')``, the algorithm Eric Greisen developed in AIPS.
  Where ``tfcrop`` fits the bandpass and flags what does not follow it,
  ``rflag`` asks whether the local *scatter* is unusual, so it models nothing.
  Two steps run over each chunk -- a sliding-window scatter along time for every
  channel, and a per-sample comparison with the neighbouring channels -- and
  they catch different RFI: a burst lasting a few integrations is invisible in
  any average and only the time step finds it, while a narrow feature present
  throughout is the spectral step's.

  ```
  skarabina --ms obs.ms --flag "autos, uv-above 4000, rflag, clip 0 100"
  ```

  Parameters are CASA's names -- ``winsize``, ``timedev``, ``freqdev``,
  ``timedevscale``, ``freqdevscale``, ``spectralmax``, ``spectralmin`` -- so a
  ``flagdata(mode='rflag')`` recipe transfers unchanged, and supplying
  ``timedev``/``freqdev`` gives the two-pass workflow CASA supports: measure the
  thresholds on one pass, review them, apply them on the next.

  Three details were each worth more than the rest of the implementation:

  - the local statistic is the scatter about the window's own mean, not the
    r.m.s. about zero.  The r.m.s. about zero of a 10 Jy source in a 0.05 Jy
    noise floor is 10, so a threshold of ``timedevscale * 0.05`` would flag
    everything; measured about the window mean it is 0.05.  Getting this wrong
    flagged 91 % of a clean plane;
  - the robust scale is ``median(|x|)``, not the MAD about the median.  The MAD
    is measured about the median and so is inflated by the outliers themselves;
    on a three-channel-wide burst the two differ by a factor of two, and scaled
    by five one flags the burst and the other flags nothing;
  - the spectral step compares each sample with its neighbouring channels rather
    than with a smoothed band.  A running median sits exactly on a smooth band,
    so its residuals are all zero, every quantile-based scale for them is zero,
    and the threshold collapses.

  ``doc/NEW_FLAGGING.md`` §10 records these and the rest, including the step's
  characteristic that a channel with merely higher gain is flagged as a narrow
  feature -- which is why CASA warns the step wants a nearly-flat bandshape, and
  when to supply ``freqdev`` rather than let it be measured.

  On the real MT0 MS (2000 rows x 4096 channels, 0.01 % pre-flagged) it flags
  20 % of the data, with a per-channel rate from 0 % to 100 % and a median of
  6 % -- the algorithm is discriminating between channels, not flagging
  uniformly.  On the 340k-row averaged MS it takes 292 s.

  Two performance defects were found by running it rather than by reading it,
  both of which had left the implementation about 40x slower than it needed to
  be.  ``local_rms`` built two small arrays per window -- 25 000 of them per
  block -- and was 71 s of the 71 s a plane took; computed from prefix sums it
  is 0.3 s.  ``_time_step`` rebuilt the window bounds *inside* the loop over
  suspect samples, so a real block allocated 142 000 x 25 641 tuples and took
  390 s against 5.3 s once the bounds were hoisted.  A third defect doubled the
  runtime outright: counting the pre-existing flags forced every block to be
  computed twice, for a number already in hand.

## [1.0.3]

### Added

- **``tfcrop`` parameters accept ``:`` as well as ``=``.**  So a recipe can
  write several of them readably inside one quoted YAML list entry::

      flag:
        - save:before
        - "tfcrop timefit: line usewindowstats: both"

  A nested mapping cannot be used.  A stimela input of type ``List[str]``
  requires every element to be a string, so ``- tfcrop: [{timefit: line}]`` is
  rejected with "Input should be a valid string" before the cab runs, and an
  unquoted ``: `` inside a YAML sequence item is invalid YAML besides.  Both
  the cab README and ``doc/NEW_FLAGGING.md`` §9.2 now show the working form and
  the one that cannot work.

  A colon is only read as a separator when a real parameter name precedes it:
  ``save:``/``restore:`` are the grammar's other colon syntax, and a rule-file
  path may contain a colon.

## [1.0.2]

### Added

- **A ``tfcrop`` flagging verb**, a reimplementation of CASA's
  ``flagdata(mode='tfcrop')``: outlier detection on the 2-D time-frequency
  plane, by fitting the bandpass robustly, dividing it out, and flagging the
  residuals.  That is what lets it catch a weak narrow-band spike without also
  flagging the bright end of the band, which a plain ``clip`` cannot do.

  ```
  skarabina --ms obs.ms --flag "autos, uv-above 4000, tfcrop, clip 0 100"
  ```

  Parameters are `key=value`, named after CASA's so that a
  ``flagdata(mode='tfcrop')`` recipe transfers unchanged, and validated at parse
  time -- ``maxnpices=3`` is an error naming ``maxnpieces`` rather than a
  silently ignored setting:

  ```
  --flag "tfcrop [timecutoff=5, freqcutoff=2.5, maxnpieces=3]"
  ```

  The bracketed form is optional; the brackets exist so that commas can separate
  parameters, since at the top level a comma separates ``--flag`` entries.
  Stimela escapes brackets on the way to a container, so that form is accepted
  too -- otherwise the syntax would break in exactly the case it was added for.
  ``:`` may be used instead of ``=``, so that several parameters can be written
  readably inside one quoted YAML list entry::

      flag:
        - save:before
        - "tfcrop timefit: line usewindowstats: both"

  A colon is only a separator when a real parameter name precedes it, since
  ``save:``/``restore:`` and rule-file paths also contain colons.  A nested
  mapping cannot be used -- a stimela ``List[str]`` input requires every element
  to be a string -- and an unquoted ``: `` inside a YAML sequence item is itself
  invalid YAML, so the entry has to be quoted.

  ``ntime`` is deliberately not offered: the chunk the bandpass is averaged over
  is the dask chunk, so the chunk length *is* ``ntime`` and a separate parameter
  could only contradict it.  ``combinescans`` is accepted for compatibility and
  does nothing, for the same reason.

  The published algorithm is followed, including growing the piece count from
  one to ``maxnpieces`` as it iterates -- and that growth is load-bearing, as
  the first implementation showed.  Fixing the piece count from the start let a
  cubic bend to follow an RFI spike, so the spike never looked like an outlier
  and was never removed: measured on a band with spikes straddling a piece
  boundary, it mis-fitted by 0.31 in a band whose clean points fit to 0.0001.
  Growing the count brought that to 0.095, against 0.097 for the best possible
  fit to the known-clean points.  `doc/NEW_FLAGGING.md` §9.4 records that and
  the other places where the published description does not determine an
  implementation.

### Fixed

- **``--flag`` accepts stimela's escaped brackets.**  Stimela escapes ``[`` and
  ``]`` when passing a parameter to a container, so a bracketed entry arrives as
  ``\\[...\\]`` and the backslashes reached the parameter names.  The parser now
  treats a backslash-escaped bracket as the bracket itself.

## [1.0.1]

### Fixed

- **The ``skarabina`` cab's ``flag`` input is usable.**  It was published in
  1.0.0 without a repeat policy, and scabha refuses a list-typed input that has
  none — so ``stimela run`` on any recipe that passed ``flag`` failed with
  "list-type parameter 'flag' does not have a repeat policy set".  The schema
  loaded, ``stimela doc`` rendered it, and the whole test suite passed, because
  scabha raises only when a recipe actually supplies the parameter; the bug
  survived every check short of running the cab.  ``flag`` and ``flag-file`` now
  declare ``policies.repeat: repeat``, matching a CLI that takes the option once
  per element:

  ```yaml
  flag:
    - autos
    - nan
    - uv-above 4000
  ```

  ``flag-file`` is also corrected from ``Union[File, None]`` to ``List[File]``,
  since the CLI accepts it more than once and reads the files in order.

  1.0.0 cannot be repaired in place: PyPI rejects a re-upload of an existing
  version, so the broken ``skarabina-cargo`` 1.0.0 stays as published and this
  release supersedes it.  Only the cab was affected — the ``skarabina`` package
  and the container image from 1.0.0 are unchanged and correct, and recipes that
  never passed ``flag`` were never affected.

  ``cargo/tests/test_schema.py`` now asserts that every list-typed input on
  every cab carries a repeat policy, and pins the documented YAML form of
  ``flag``.  The new check was confirmed to fail with the policy removed.

## [1.0.0]

**Breaking release.**  Flagging is now expressed as one ordered list, and the
0.8.x flagging options are removed.  This also carries the analysis, band-hole
and `--optimize` work previously staged for 0.8.9, which is unchanged in
substance and is recorded below it.

### Changed

- **Flagging is one ordered `--flag` list.**  The order the flagging operations
  run in is now the order they are written, instead of a fixed sequence buried
  in the code:

  ```
  skarabina --ms obs.ms --flag "save:before, uv-above 2000, nan, clip 0 100, save:after"
  ```

  Entries are ``verb [args...]`` separated by top-level commas; commas inside
  brackets are not separators, so a spectral-window rule file stays a file, and
  quoting is honoured for paths with spaces.  The option is repeatable and
  occurrences concatenate in the order given, and ``--flag-file`` reads entries
  from a YAML list or one entry per line with ``#`` comments.
- **Removed:** ``--flag-nan``, ``--flag-clip``, ``--flag-uv-above``,
  ``--flag-spectral-window``, ``--flag-autos``, ``--flag-save-before`` and
  ``--flag-restore-before``.  Their replacements are ``nan``, ``clip <lo> <hi>``,
  ``uv-above <m>``, ``spectral-window <file>``, ``autos``, ``save:<name>`` and
  ``restore:<name>``.  There is no deprecation window and no dual interface:
  each removed option now errors with a hint at ``--flag``.
- **``barber`` is deliberately not a verb.**  ``--barber`` and ``--barber-pol``
  are unchanged, and ``--flag barber`` is rejected: barber never writes ``FLAG``
  — it computes statistics over the unflagged data and prints a report — so it
  is a read-only diagnostic with no ordering relationship to flagging.
- **Ordering means more operations over the same data, so statistics are
  deferred.**  Each data-flagging step appends its reduction instead of
  computing it, and one ``dask.compute`` at the end of the sequence evaluates
  them together.  Dask shares the single ``abs(DATA)`` subgraph, so a sequence
  of N operations still loads the data column once: measured, a two-operation
  sequence executes **one** DATA-loading task rather than two.  Without this the
  read cost would grow with the number of operations.

### Added

- **``--flag-file``** for long sequences, and the ``flag`` / ``flag-file``
  inputs on the ``skarabina`` stimela cab, replacing its removed flagging
  inputs.  ``barber`` and ``barber.pol`` are unchanged on the cab.  (The cab's
  ``flag`` input as published in this release could not be passed by a recipe;
  see 1.0.1.)
- **``--write-changed-only``** for the ``--msout`` path.  A flagging run changes
  ``FLAG`` and nothing else, yet writing an output MS re-reads and rewrites every
  column: on a 92 GB measurement set a flagging run writes 103 GB through the
  full-copy path.  With this option the unchanged columns' storage blocks are
  hard-linked into the output and left read-only, and only ``self.changed`` is
  written: measured, 6.1 GB written instead of 103 GB, with 97.8 GB of the output
  shared with the input.  The *read* cost is unchanged -- the write path reads
  its input through dask-ms, which attaches the whole MS read graph to the table
  it writes -- so use ``--apply`` when the read dominates and the input may be
  modified.  This option is for read-only mounts, provenance requirements, or a
  pipeline that needs the raw MS alongside the flagged one.  Also an input on the
  ``skarabina`` cab.

  The sharing is exact, not a re-read: unchanged blocks are the *same inode*, so
  the mode is only offered when the output has the input's row and channel shape.
  ``--split`` and averaging change that shape, so they fall back to a full write
  with a warning naming the reason.  Because a hard link has one inode, the
  shared blocks are made read-only, which means a later write to a shared column
  through *either* path fails rather than silently corrupting the input.

## [0.8.9]

### Added

- **`--keep-fully-flagged-channels`.**  With `--optimize`, keeps channels whose
  visibilities are all flagged instead of removing them.  Flagging already
  excludes them from imaging, so keeping them costs file size and nothing else,
  but it avoids the band-splitting described under *Fixed* below.  Also an
  input on the `skarabina` cab.
- **`band_has_gaps`, `span_hz` and `channel_width_hz` on the `skarabina-analyze`
  cab**, reporting whether the band is contiguous and, if not, how wide the
  channels really are.

### Changed

- **`skarabina-analyze` reports the averaging limits at an explicit loss.**
  ``max_integration_time_s`` is now the limit for a 10% loss at the field edge
  (``TIME_AVERAGE_LOSS``) rather than for an unstated criterion, and the console
  line says so.  ``max_channel_width_hz`` and ``bandwidth_smearing_factor`` are
  unchanged in value.
- **The resolution's assumptions are documented.**  ``resolution_arcsec`` is
  ``c/(ν_max·B_max)``: the top of the band, unweighted and untapered, i.e. the
  best case for the array as measured, so the recommended pixel size errs on the
  side of oversampling.  `band_info`'s treatment of disjoint spectral windows as
  one band is likewise documented as a deliberate, conservative simplification.
- **The direction cosine in the bandwidth-smearing factor is computed
  directly.**  ``r_1`` was ``hypot(sinθcosθ, sin²θ)``, which equals ``sin θ``
  only by an identity; it is now ``sin θ_edge``, with the relation it comes from
  quoted in the code.

### Fixed

- **`skarabina-analyze` no longer reports a band with holes as if its channels
  were wider than they are.**  The channel width was derived as
  ``(nu_max - nu_min) / n_channels`` and never read from the subtable, so an MS
  whose channels are not uniformly spaced -- the shape `--flag-spectral-window`
  followed by `--optimize` leaves behind -- had its per-channel width
  overstated by the fraction of the band missing (17% in a worked case).  The
  width now comes from `CHAN_WIDTH` (falling back to `RESOLUTION`), the
  reported `bandwidth_hz` is the sum of the channel widths (the spectrum
  recorded), and the new `span_hz`/`band_has_gaps` make the difference
  explicit.  `min_channels` follows the recorded bandwidth, so a hole
  correctly lowers the number of channels the data supports.
- **`--optimize` warns when it splits the band.**  Removing a fully-flagged
  channel from the *middle* of a band leaves a hole that no SPECTRAL_WINDOW
  column records: `CHAN_WIDTH` still describes each surviving channel and
  `TOTAL_BANDWIDTH` still sums what is left, so a consumer assuming contiguous
  channels reads the band as wider per channel than it is.  `optimize` now
  reports the number of pieces and the width of the hole, and points at
  `--keep-fully-flagged-channels`.
- **`--summary` reports the spectrum present, not the span.**  The
  `Spectral windows:` line now shows the per-channel width and the total
  spectrum recorded, and adds a `Band has holes:` line when channels are not
  contiguous.  Previously it printed the band span as "bandwidth", which
  overstates a band with holes.

- **`skarabina-analyze` and `skarabina --summary` now agree on the
  integration-time limit.**  The two quoted the same physical quantity from
  different formulas: `analyze` used a bare ``0.1/(ω_E·B·θ)`` while the summary
  used the small-angle form ``c·√(6L)/(π·ω_E·B·ν·ℓ)``.  For the mergA_tim field
  that was 19.0 s against 21.8 s for the same observation and criterion — close,
  but there was no statement of *which* loss either corresponded to, and
  ``analyze``'s constant was labelled "~10% loss" in the code and in
  `doc/ANALYZE.md` while actually allowing ~0.4%.  Both now call
  `skarabina.dask_ms.max_integration_time`, which inverts
  ``ρ = sinc(π·ω_⊕·Δt·B·ν·θ/c)`` for a stated loss.  `--summary` prints the 10%
  row (the criterion `analyze` reports) alongside 1%, 3% and 5%.  Verified on a
  real MS: both commands report 3.6 s.
- **The smearing relation behind those limits is now documented correctly.**
  The fringe-washing factor was written as ``sinc(π·x/2)`` with
  ``x = ω·Δt·B·ν·θ/c``, which is a factor of two away from the relation the code
  actually needs; the correct form is ``sinc(π·x)``.  The limit itself was
  verified against a direct numerical average of the visibility phasor in real
  units (agreement to ~1e-6), and the small-angle ``√(6L)`` form below is
  confirmed as its approximation rather than its definition.
- **`doc/ANALYZE.md`'s worked example was stale and wrong.**  It claimed a
  7697 m baseline gives 4.47 arcsec and a 10066-pixel image; it actually gives
  5.68 arcsec and 7922 pixels, and the example predated both the full-width FOV
  convention and the averaging limits.  The example is now a real run (7625 m,
  1711.791 MHz → 4.74 arcsec, 9500 px) and a test pins its numbers to the code
  so it cannot silently drift again.

### Tests

- **`tests/test_integration_time.py` no longer tests a copy of the formula.**
  It defined its own local ``max_integration_time``, so it passed regardless of
  what the package did — the reason the divergence above went unnoticed.  It now
  imports and exercises the real implementation, asserting that the returned
  limit really achieves the requested loss and comparing the relation against a
  brute-force phasor average.
- **`tests/test_fov_convention.py`** compares against the small-angle form with
  an explicit tolerance, since the exact inverse now sits slightly above it
  (0.15% at L = 0.01, 0.76% at L = 0.05).
- **`tests/test_analyze_smearing.py`** pins `analyze`'s limit to the shared
  function, pins the documented worked example in `doc/ANALYZE.md`, reads
  `CHAN_WIDTH` off a real MS, detects a hole written into a subtable, and checks
  that R_b uses the real channel width rather than the band average.
- **`tests/test_optimize_band.py`** covers the band `--optimize` leaves behind:
  the hole and its warning, that an edge channel does not warn, that
  `--keep-fully-flagged-channels` keeps the band contiguous while still
  dropping rows, and that a wholly-flagged MS trips the row guard (every
  channel is dead exactly when every row is, so the channel guard is
  unreachable for a single dataset).
- **`tests/test_integration_time.py`** likewise pins `doc/AVERAGING.md`: the
  small-angle-vs-exact coefficient table and the worked example values are
  checked against the code.  `doc/AVERAGING.md` itself was updated: its
  Δt<sub>max</sub> was presented as an exact formula when the code inverts the
  relation exactly instead (the small-angle form is now shown as the
  approximation it is, with the size of the gap), its example table gained the
  10% column that `skarabina-analyze` reports, and its summary description now
  lists the 10% row.

## [0.8.8]

### Added

- **`skarabina-analyze` reports the averaging limits.**  Reads the band edges
  and channel count from the SPECTRAL_WINDOW subtable it already used for
  ``nu_max`` and reports, at the edge of the *requested* field of view
  (``theta_edge = FOV/2``): ``max_channel_width_hz`` — the white-light fringe
  limit ``c/(B_max·theta_edge)``, and ``min_channels``, the fewest channels the
  data supports; ``max_integration_time_s`` — the longest integration before
  time-average smearing matters; and ``bandwidth_smearing_factor`` — the radial
  R_b for the channels as they are.  These are the numbers the superseded
  ``set-image-parameters`` cab in the white-belt pipeline printed.  All are
  exposed as scalar outputs on the cab.  (Recorded after the fact: this release
  was tagged without a changelog entry, and its
  ``cargo/skarabina_cargo/genesis/skarabina-cargo-base.yml`` was left at image
  version 0.8.7 while both ``pyproject.toml`` files said 0.8.8, so the published
  cab declared an image built from the previous release's code.  Both are
  corrected in 0.8.9; the published 0.8.8 artifacts are immutable.)

## [0.8.7]

### Added

- **`--flag-save-before <versionname>` and `--flag-restore-before
  <versionname>`.**  Back up and restore flags the way CASA's `flagmanager`
  does.  Versions live beside the MS in `<ms>.flagversions/`, in CASA's exact
  layout: a plain-text `FLAG_VERSION_LIST` holding one `<name> : <comment>`
  line per version, and a `flags.<versionname>` casacore table per version
  containing `FLAG` and `FLAG_ROW`.  The layout was taken from versions written
  by CASA itself, so the two tools interoperate: skarabina reads a CASA-written
  version byte-for-byte (verified on a 745,996-row `mt0_e45_casa_rflag`
  version), and a version written by skarabina matches CASA's schema
  column-for-column and uses the same `TiledShapeStMan` for `FLAG` (737 MB
  against CASA's 730 MB for the same flags).

  Both options act before any flagging runs.  `--flag-restore-before` is
  applied first, so the two combine to re-label a version
  (`--flag-restore-before Original --flag-save-before pre-autos`).  The backup
  is read from the MS on disk rather than from the in-memory dataset, so a
  version stays restorable even when the run works on a row selection
  (`--scan`, `--split`); CASA's flagmanager likewise backs up the whole MS.
  Saving an existing name moves the old version aside as
  `<name>.old.<timestamp>`, as CASA does.  Restoring a version whose row count
  no longer matches the MS is an error, not a silently misaligned restore.

  Both options are also exposed as `flag-save-before` / `flag-restore-before`
  inputs on the `skarabina` stimela cab.

## [0.8.6]

### Added

- **Gap-tolerant integration grouping (the default).**  Some MS writers stamp a
  single integration's rows with more than one `TIME` value, splitting one
  integration into two partial groups.  Grouping rows by "`TIME` changed" then
  reports integrations that look incomplete — on a MeerKAT MT0 file, 4 of 440
  integrations were split, with offsets of exactly 1.000 s and part sizes of
  333+1378, 1480+231, 1539+172 and 1276+435 rows, each pair summing to the full
  1711 baselines for 58 antennas.  No data is missing: the parts hold disjoint
  baseline sets that together are the whole integration, and the second part is
  the baselines *within* a k-antenna subset.  `--summary` now groups
  integrations with `group_integrations()`, which estimates the cadence from
  the median spacing of distinct `TIME` values and extends a group across any
  `TIME` change closer than half that cadence.  Intra-integration offsets are
  far below the cadence (1.0 s against 8.0 s), so the parts are re-united while
  genuine integrations stay separate.  On the MT0 file the reported count
  becomes 436 rather than 440.
- **Integration structure reported by `--summary`.**  The summary now prints the
  number of integrations (`Integrations: 436`) and the cadence (`Integration
  cadence: 7.997 s`), and calls out `Incomplete integration groups` when an MS
  that does hold full integrations has groups short of a complete baseline set.
  A deliberately reduced MS — a single field, say — is not flagged, because its
  groups are smaller by construction.

### Fixed

- **`--summary` no longer takes the integration time from row 0.**
  `INTERVAL`/`EXPOSURE` was read from the first row, so a split integration
  whose first row carried a shortened interval reported 6.0 s instead of the
  nominal 8.0 s — which changes whether the observation appears to exceed the
  fringe-rotation limit.  The nominal (most common) value is now used, falling
  back to the median spacing of distinct `TIME` values.

## [0.8.5]

### Added

- **Autocorrelation flagging (`--flag-autos`).**  Flags every visibility of an
  auto baseline (`ANTENNA1 == ANTENNA2`) and sets their `FLAG_ROW` bits, so
  `--optimize` can then drop the rows.  Auto baselines measure the total power
  of a single antenna, carry no fringe information, and are normally excluded
  from imaging and calibration; the `flag-autos` input is exposed on the
  `skarabina` cab so a pipeline no longer needs a CASA `flagdata` pass for it.

## [0.8.4]

### Changed

- **`--field-of-view` is now the FULL width of the field of view**, matching
  `skarabina-analyze --image-fov`.  The flagging cab used to call its value a
  half-width, so the same number handed to both cabs described fields a factor
  of two apart -- and a pipeline had to pass two different values.  The
  fringe-rotation integration-time limit reported by `summary` is unchanged in
  meaning: it depends on the distance ℓ from the phase centre to the edge of the
  field, which is half the full width, and that is what is now computed
  internally (`max_integration_time`).  **Behaviour change:** for the same
  numeric `--field-of-view`, the reported Δt_max values double, because the
  field it describes is now twice as wide.

## [0.8.3]

### Fixed

- **The written MS links every sub-table the input had.**  Sub-tables are
  reached through keywords on the main table (`SOURCE`, `FIELD`, ... each
  holding `Table: <path>`), and the table dask-ms writes carries most of them
  but not `SOURCE`.  The copied SOURCE sub-table was therefore orphaned:
  `getsubtables()` did not list it and CASA's Calibrater refused to open the
  MS with *"NullTable::lock - Table object is empty"*, which is how
  `clearcal` -- the first calibration step of the white-belt pipeline -- failed
  on the first real-data run.  Any keyword present in the input and missing
  from the output is now copied across, with sub-table links repointed at the
  newly written MS.  The test fixture builds a SOURCE sub-table, and a
  regression test checks that the written MS exposes every sub-table the input
  had and that `SOURCE` points inside the new MS rather than back at the input.

## [0.8.2]

### Fixed

- **All per-channel SPECTRAL_WINDOW columns are rewritten, not just
  `CHAN_FREQ`/`RESOLUTION`.**  `CHAN_WIDTH` and `EFFECTIVE_BW` also have
  `NUM_CHAN` entries and were left describing the *input* channel count after
  frequency averaging, so the written MS had a subtable that contradicted
  itself (e.g. `RESOLUTION` of length 314 next to `CHAN_WIDTH` of length 2511).
  dask-ms rejected it with *"conflicting sizes for dimension 'chan'"*, which is
  how `quartical-summary` failed on the first real-data run of the white-belt
  pipeline.  The channel-axis bookkeeping now tracks every width column present
  in the subtable, averages them with the data (widths sum within a group),
  drops removed channels from them under `optimize`, and writes them back.
  A write-time check now refuses to leave a subtable with a stale per-channel
  column, naming it in the error.

## [0.8.1]

### Fixed

- **Multi-field measurement sets are no longer reduced to their first field.**
  dask-ms groups by `(FIELD_ID, DATA_DESC_ID)` by default, so `xds_from_ms`
  returned one dataset *per field* and the code took the first one: flagging,
  averaging and the write-out silently kept only the first field of a
  calibrator + target MS (for a typical observation, just the bandpass
  calibrator), and `skarabina-analyze` measured the longest baseline from that
  field alone.  The reader now groups by `DATA_DESC_ID` only, so all fields
  travel together with `FIELD_ID` as a per-row variable; `--split` still
  selects a single field.  An MS with more than one DATA_DESC_ID (i.e. more
  than one spectral window) is rejected with a clear error rather than being
  partially processed, and `skarabina-analyze` sees the baselines of every
  field.  Regression tests build multi-field measurement sets and check that
  all fields survive flagging, averaging, the write-out and `--split`.

## [0.8.0]

### Added

- **Scan selection (`--scan`).** Keeps only the rows of the requested scans:
  a comma-separated list of scan numbers and inclusive `lo~hi` ranges, e.g.
  `--scan 1,12,14` or `--scan 0~5`. Filtering happens at read time, so
  flagging, averaging, `--optimize` and the written output all see the
  selected scans only; an empty or absent specification keeps every scan, and
  a selection that matches no rows is an error. The `scan` input is exposed on
  the `skarabina` cab, so a recipe can keep a subset of scans without a
  separate `mstransform` pass.
- **`split` input on the `skarabina` cab.** `--split` (keep one field's rows
  when writing) existed in the CLI but was not reachable from a recipe.

### Fixed

- **SPECTRAL_WINDOW is rewritten to match the data.** The sub-table is copied
  verbatim from the input MS, so after frequency averaging the output MS kept
  the *input* channel count in `NUM_CHAN`/`CHAN_FREQ` (e.g. 2511 channels
  described for a 314-channel main table), and after `--optimize` removed
  fully-flagged channels it was left describing channels that no longer
  existed. `NUM_CHAN`, `CHAN_FREQ`, `RESOLUTION` and `TOTAL_BANDWIDTH` are now
  rewritten whenever the channel count changes, channel widths follow the
  averaging (they add up within a group), and `--optimize` drops removed
  channels from the bookkeeping. A mismatch between the bookkeeping and the
  data is now a hard error instead of a silently inconsistent MS.
- **`skarabina-analyze` ignores rows flagged by a flagger.** The recommended
  image size was driven by the longest baseline in the MS even when that row
  was flagged (which is exactly what `skarabina --flag-uv-above` does), so it
  recommended a size for baselines that would never be imaged. Rows with
  `FLAG_ROW` set are now excluded, and an MS whose every row is flagged is an
  error rather than a nonsense recommendation.
- **`--flag-nan` is honoured.** The switch was tested with `is not None`
  against a flag whose default is `False`, so NaN flagging ran even when it
  was not requested. It is now off unless asked for.
- **Multi-`DATA_DESC_ID` measurement sets are rejected.** dask-ms returns one
  dataset per DDID and only the first was processed, so a multi-DDID MS was
  silently truncated on write. This is now a clear error telling the user to
  split by spectral window first.
- **Scan selection and other row/channel changes refresh the cached column
  snapshots.** `flag_uv_above`, `flag_data` and `flag_spectral_window` read
  the live dataset rather than the `__init__` snapshots, which went stale once
  rows had been selected.

### Changed

- **The `skarabina` cab no longer declares the `reference-antenna` and
  `max-uv` outputs.** Nothing ever populated them (they had no wrangler and
  no matching print), so a recipe binding to them silently received nothing.
- **The documented include path is `(skarabina_cargo)`.** The two READMEs
  disagreed (`(skarabina)` in the root README, `(cargo)` in the cargo README);
  only `(skarabina_cargo)` matches the installed package, which is what the
  demo pipeline and the tests use.
- **Dev environment**: the root project now installs `skarabina-cargo`
  (editable, from `cargo/`) so the cab-schema and analyze-contract tests can
  actually run; previously `uv run pytest` failed to collect them.

## [0.7.2] — 2026-09-11

### Added

- **`--json-stdout` for `skarabina-analyze`.**  Prints the analysis record as
  a single JSON line on stdout, prefixed by `SKARABINA_ANALYZE_JSON `, so that
  Stimela can wrangle it into output values (see below).  `--output-json` is
  unchanged and the two may be combined.
- **`skarabina-analyze` cab publishes typed outputs.**  The cab now declares
  `recommended_image_size_pixels`, `resolution_arcsec`, `max_baseline_m`, and
  `max_frequency_hz` (plus the `output-json` file), extracted from the
  `--json-stdout` line by a `PARSE_JSON_OUTPUT_DICT` output wrangler.  A larger
  imaging pipeline can now bind `=steps.analyze.recommended_image_size_pixels`
  onto an imager's `size` parameter, instead of hard-coding it.
- **Demo imaging pipeline.**  `cargo/examples/skarabina-demo-pipeline.yml`
  flags an MS, analyzes it, and shows the analysis driving imager arguments.
  It demonstrates both consumption routes — wrangled scalars bound onto a cab's
  parameters, and the `output-json` file read by a `python-code` cab — and
  aliases the recommendation to typed recipe outputs.  Verified end-to-end with
  `stimela run` against both the native and `singularity` (container) backends.
- **Contract tests.**  `tests/test_analyze_contract.py` and additions to
  `cargo/tests/test_schema.py` verify that the CLI's JSON keys, the cab's
  scalar output names, and the wrangler regex cannot drift apart.

### Changed

- **`output-json` on the `skarabina-analyze` cab is now an output, not an
  input.**  It was declared among `inputs`, so a recipe had to invent a
  filename and the resulting file could not be referenced by later steps.  It
  is now a *named file output*: Stimela supplies the path and passes it as
  `--output-json`, and downstream steps consume it as
  `=steps.<step>.output-json`.  Recipes that set `output-json: <path>`
  explicitly keep working; leaving it unset now lets Stimela manage the name.
- **`skarabina-analyze` now fails loudly when it cannot measure the MS.**  If
  the maximum frequency or baseline cannot be determined it raised nothing —
  it printed "Could not determine resolution from MS" and exited 0, so a
  pipeline carried on with no outputs.  It now raises a `ClickException` and
  exits non-zero.
- **The Docker image is built from this repository, not from PyPI.**
  `Dockerfile` ran `pip install skarabina`, which silently installed whatever
  version PyPI last held — so an image tagged for a release could carry code
  that did not match the tag.  That is not hypothetical: PyPI's 0.7.1 predates
  `--json-stdout`, so an image built from the v0.7.2 tag by the old Dockerfile
  would not have had the flag, and the demo pipeline would have failed with it.
  The image is now built from the checkout (`uv build` + install the resulting
  wheel), so an image always matches its tag.
- **Demo steps are container-portable.**  Since stimela 2.2, a container backend
  rejects a cab that does not name an `image` ("container image not specified
  by cab").  The demo's `report` step was an inline *binary* cab with no image,
  so it failed under a container backend; it now uses the `python` flavour,
  which picks up stimela's default image.  Note that stimela has no working
  `docker` backend (`backends/docker.py` is a stub whose `is_available()`
  returns `False`, and `podman` is likewise unimplemented) — use the
  `singularity`/`apptainer` backend for containerised runs.

### Fixed

- **`cargo/README.md` "printing outputs" examples used `cab: echo`.**  `echo`
  is not a stimela built-in (it fails on 2.1.4 with `unknown cab 'echo'`), so
  the examples now define the cab inline.  Documented alongside it: stimela
  rejects a recipe alias whose name also appears under `inputs`/`outputs` — the
  `aliases:` section is itself the declaration.
- **`--time-average-factor` now excludes fully-flagged rows from
  per-row metadata.**  Previously UVW, TIME, INTERVAL, and EXPOSURE
  were averaged/summed over *all* rows, including flagged ones — the
  only quantities that excluded flagged data were DATA, WEIGHT_SPECTRUM,
  and SIGMA_SPECTRUM.  A row is now treated as bad (and excluded from
  UVW/TIME masked means and INTERVAL/EXPOSURE masked sums) when
  `FLAG_ROW` is True **or** every visibility in `FLAG` is True — the
  same definition `optimize()` already uses.  A partially-flagged row
  still carries a valid timestamp and baseline, so it continues to
  contribute.  This is a scientific change to averaged output.
- **`time_average` / `frequency_average` docstrings corrected.**  The
  `time_average` docstring previously claimed INTERVAL was "averaged"
  (it is summed); SIGMA_SPECTRUM and EXPOSURE were undocumented.  Both
  docstrings now accurately describe which columns exclude flagged
  data and the combining operation for each.
- **Fewer dask scheduler passes in flagging and averaging.**
  `flag_spectral_window`, `optimize`, `time_average`, and `barber` now
  batch their dask computations into fewer passes, reducing overhead on
  large measurement sets.  Most notably, `flag_spectral_window` now
  builds the combined flag update for a whole YAML in a single graph
  (previously one scheduler round-trip per entry) and computes per-entry
  visibility counts by factoring 1D channel/row gates instead of summing
  a materialized `(nrow, nchan, ncorr)` array.  Results are unchanged.

- **`--flag-spectral-window` no longer discards `--flag-nan`/`--flag-clip`
  flags.**  `flag_spectral_window` read the stale `FLAG` snapshot cached
  in `__init__` and OR'd onto it, so when it wrote the result back it
  silently dropped the NaN/clip flags that `flag_data` had just written.
  It now reads the current `FLAG` from the live dataset.
- **`--summary` UV statistics now reflect post-averaging data.**  The
  max-uv, UV percentiles, and fringe-rotation integration-time limit
  were computed from the `__init__` UVW snapshot, so after
  `--time-average-factor`/`--frequency-average-factor`/`--optimize` they
  described the original, pre-processing rows.  They now derive UV from
  the live dataset.
- **`--barber` report now reflects post-processing data.**  It read the
  cached `FLAG`/`DATA`/`TIME`/`WEIGHT_SPECTRUM`/`ANTENNA1/2` snapshots,
  so after flagging/averaging/optimize it reported on the original
  unflagged, un-averaged dataset (shapes even mismatched `self.ds`).
  It now reads all columns from the live dataset.

## [0.7.1] — 2026-07-14

### Changed

- **Averaging uses `dask.array.coarsen`.**  `--time-average-factor` and
  `--frequency-average-factor` now block-reduce with `coarsen` instead of a
  manual reshape.  When row/channel chunk sizes are not divisible by the
  factor (the common case), reshape fragmented chunks and forced an expensive
  rechunk; `coarsen` keeps chunks regular and builds a ~28% smaller task graph,
  which matters for very large measurement sets.  Results are numerically
  identical.

### Fixed

- **Frequency averaging with `factor > nchan` (e.g. a single-channel MS)
  no longer crashes** — it is now a no-op with a clear message.

## [0.7.0] — 2026-07-14

### Added

- **`--split` option.**  When writing a new MS (`--msout`), keep only the rows
  of a single field, given by field name or numeric `FIELD_ID`.  Flagging,
  averaging, and optimization still run on the full input MS; only the output
  is reduced to the selected field.  See [Splitting an MS by field](SPLITTING.md).

## [0.6.18] — 2026-07-07

### Changed

- **`--field-of-view` uses `angle-parser`.**  Accepts unit-suffixed strings:
  `1.0 deg`, `30 arcmin`, `5 arcsec`, `0.5 rad`.  Dependency added.

## [0.6.17] — 2026-07-07

### Fixed

- **`flag-clip` tuple serialization: `repeat: "list"`.**  `repeat: false` parsed
  as Python `False` and was used as a join separator (`0.0False100.0`).
  `repeat: "list"` correctly passes tuple elements as separate positional args.
- **Removed `default: false` from bool params.**  Stimela serialized default
  `False` values as `--param False`, causing extra args to spill into `nargs=2`.

### Added

- **`flag-only` stimela recipe.**  Flag and summarize without averaging.

### Changed

- **Gitignore `cargo/uv.lock`.**  Root `uv.lock` is the single lock file.

## [0.6.16] — 2026-07-07

### Changed

- **Flattened `flag.*` schema params.**  `flag.nan`, `flag.clip`, etc. are now
  flat `flag-nan`, `flag-clip` — matching Click option names exactly.  Stimela
  uses dots for nested param separators, which conflicts with Click's hyphens.
  Flattening avoids the mismatch.

## [0.6.15] — 2026-07-07

### Fixed

- **`flag.clip` schema: added `repeat: false` policy.**  Without it, stimela
  rejects the YAML list `[0, 100]` for `Tuple[float, float]` dtype.

## [0.6.14] — 2026-07-07

### Added

- **`stimela.conf`** with container image config for stimela.
- **`MANIFEST.in`** for the cargo package.

### Changed

- **Renamed genesis vars** from `vars.skarabina` to `vars.skarabina-cargo`
  to avoid namespace collision with the CLI package.
- **Renamed genesis file** to `skarabina-cargo-base.yml`.
- **Moved stimela examples** from `example/` to `cargo/examples/`.
- **Updated examples** with correct `--flag-clip` syntax and stimela recipes.

### Fixed

- **`--flag-clip` in examples** — uses space-separated values per Click's
  `nargs=2`, not comma-separated.

## [0.6.13] — 2026-07-07

### Changed

- **Rename `skarabina_cargo` → `cargo` directory.**  The cargo package lives
  in `cargo/` (Python package name remains `skarabina_cargo`).  Stimela
  recipes use `_include: (cargo): skarabina.yml`.

## [0.6.12] — 2026-07-07

### Added

- **CI: PyPI publish workflow for `skarabina-cargo`.**  Tagged releases
  now publish the cab definitions package to PyPI via trusted publishing.
- **`skarabina-cargo` README** with stimela recipe examples for `skarabina`
  and `skarabina-analyze` cabs, spectral window flagging, and output
  consumption between steps.

### Changed

- **Example `stimela_run.sh`** for running a skarabina flagging summary
  through stimela with a local measurement set.

## [0.6.11] — 2026-07-06

### Changed

- **Split into two packages.**  The repo now contains `skarabina` (CLI tool)
  and `skarabina-cargo` (Stimela cab definitions).  `skarabina` no longer
  depends on `stimela` — all CLI parameters are explicit `@click.option`
  decorators in `main.py`.  The cab schema (`skarabina.yml`) and base vars
  (`genesis/skarabina-base.yml`) live in `skarabina_cargo/`.  Stimela recipes
  should now `_include: (skarabina_cargo): skarabina.yml`.
- **Docker: removed custom entrypoint.**  The `docker-entrypoint.sh` dispatcher
  (`run`/`analyze`) is gone.  The container now runs any command directly.
  Use `skarabina ...` or `skarabina-analyze ...` as the container command.
  This lets Stimela use the containerized backend identically to the native
  backend.

## [0.6.10] — 2026-07-06

### Fixed

- **Stimela image version: drop `v` prefix.**  The `vars.skarabina.images.version`
  in `skarabina-base.yml` must match the Docker image tag published by CI.
  CI's `docker/metadata-action` with `type=semver` strips the `v` from the
  git tag, so the image is `ghcr.io/tmolteno/skarabina:0.6.10`, not
  `:v0.6.10`.  Updated `AGENTS.md` to document this convention.

## [0.6.9] — 2026-07-06

### Added

- **`--output-json` for `skarabina-analyze`.**  Writes analysis results
  (max baseline, max frequency, resolution, recommended image size,
  input parameters) to a JSON file.  Also available in the Stimela cab
  as `output-json` input.

## [0.6.8] — 2026-07-06

### Added

- **Stimela cab for `skarabina-analyze`.**  Added a `skarabina-analyze` cab
  definition to `skarabina/skarabina.yml` so the analyze command can be used
  in Stimela workflows.  Reuses the same Docker image as the main cab.

## [0.6.7] — 2026-07-06

### Removed

- **Docker: dropped numcodecs arm64 workaround.**  The `CFLAGS`/`DISABLE_NUMCODECS_*`/
  `--no-build-isolation` workaround was confusing and never worked correctly
  under QEMU.  With native arm64 runners (0.6.6) it is no longer needed:
  `py-cpuinfo` correctly reports no SSE2/AVX2 on aarch64 hardware and
  numcodecs compiles cleanly.

## [0.6.6] — 2026-07-06

### Changed

- **CI: build on native arm64 runners.**  Switched from QEMU-emulated arm64
  builds on x86_64 to native `ubuntu-24.04-arm` runners.  Each platform
  now builds on its own architecture in a matrix (`ubuntu-latest` for amd64,
  `ubuntu-24.04-arm` for arm64), then a merge job combines them into a
  multi-arch manifest with `docker buildx imagetools create`.

## [0.6.5] — 2026-07-06

### Fixed

- **Docker: numcodecs arm64 build fix (third attempt).**  Under QEMU, `py-cpuinfo`
  detects host x86_64 features, causing setup.py to include `-DSHUFFLE_*`
  macros and x86 source files even when `CFLAGS` is set.  The fix requires
  three things together: (1) `CFLAGS="-O2"` prevents `-msse2`/`-mavx2` flags,
  (2) `DISABLE_NUMCODECS_SSE2=1 DISABLE_NUMCODECS_AVX2=1` prevents
  `-DSHUFFLE_*` macros and x86 source files, (3) `--no-build-isolation`
  ensures those env vars reach setup.py through pip's isolated build.  Build
  deps (cython, numpy, py-cpuinfo, setuptools) are pre-installed so isolation
  can be safely disabled.

## [0.6.4] — 2026-07-06

### Fixed

- **Docker: attempted numcodecs arm64 fix (CFLAGS only).**  Set `CFLAGS="-O2"`
  during numcodecs install.  This prevented `-msse2`/`-mavx2` flags but did
  not prevent `-DSHUFFLE_*` macros and x86 source files (see 0.6.5).

## [0.6.3] — 2026-07-06

### Fixed

- **Docker: attempted numcodecs arm64 fix.**  Set `DISABLE_NUMCODECS_SSE2` and
  `DISABLE_NUMCODECS_AVX2` globally via `ENV`.  This did not resolve the
  problem (see 0.6.4 for the proper fix).

## [0.6.2] — 2026-07-06

### Changed

- **CI: Docker builds only on tag pushes.**  Removed the per-push/PR test-build
  job — images are built and pushed only when a `v*.*.*` tag is pushed.

## [0.6.1] — 2026-07-03

### Fixed

- **CI: add QEMU for multi-arch builds.**  `docker/setup-qemu-action` is required to build `linux/arm64` on x86_64 runners.  Without it, the `:latest` tag contained only an amd64 manifest, causing `no matching manifest for linux/arm64/v8` on aarch64.
- **CI: add `:latest` tag.**  The metadata action now emits `type=raw,value=latest` so `ghcr.io/tmolteno/skarabina:latest` always points to the most recent release.

## [0.6.0] — 2026-07-03

### Docker

- **Single multi-arch Dockerfile.**  One `Dockerfile` now builds on x86_64 and aarch64 (DGX Spark, AWS Graviton, Raspberry Pi).  On x86_64 `python-casacore` installs from a pre-built wheel; on aarch64 it builds from source via scikit-build-core with `CMAKE_CXX_STANDARD=17` to avoid C++20 `std::allocator` incompatibilities.
- **Entrypoint requires explicit command.**  `docker run ...` now requires `run` or `analyze` as the first argument.  Example: `docker run ... run --ms /data/obs.ms --summary`.  The old bare-arg dispatch (no `run` prefix) is no longer supported.
- **Removed conda-based Dockerfile.**  `Dockerfile.conda` and `Dockerfile.source` are deleted; the main `Dockerfile` handles all architectures.
- **CI overhaul.**  Docker builds are tested on every push and PR.  Tagged releases publish a single multi-arch image (`linux/amd64`, `linux/arm64`) to `ghcr.io/tmolteno/skarabina` with a unified `:latest` tag (no `-conda` suffix).

### Documentation

- `INSTALL.md` rewritten: Docker pull/run/workflow examples, `CMAKE_ARGS` workaround for bare-metal aarch64, C++ allocator error explanation.

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
