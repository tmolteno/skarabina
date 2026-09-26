<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# RFlag in skarabina, and how it differs from CASA's

Status:

| Part | State |
|---|---|
| `rflag` verb, single-baseline algorithm (§3) | **implemented in 1.0.4**, window statistics fixed on branch `baseline-aware-flagging` |
| Baseline-aware chunks and the per-antenna noise model (§4) | **prototype**, branch `baseline-aware-flagging`, on by default there (`dask_ms.AUTOFIT_BASELINES`) |
| The same for `tfcrop` (§6) | **prototype**, same branch |

skarabina's `rflag` verb is a reimplementation of the algorithm CASA's
`flagdata(mode='rflag')` uses, which Eric Greisen developed in AIPS.  The
parameter names and defaults are CASA's, so a recipe transfers, but the
implementation is not a port, and it differs from CASA's in ways that change
which visibilities are flagged.  This document sets out what CASA does (from
its source), what skarabina does, the differences, and the measurements that
motivated them.  `doc/NEW_FLAGGING.md` §10 covers the verb's grammar and its
tests.

## 1. Where CASA's rflag lives

Not in casacore.  casacore -- the table, measures and MS library that casacure
reimplements, pinned as the `casacore` submodule of `../casacure` at
`56a917a` -- contains no auto-flagging at all: its full tree (3420 files) has
`ms/MSOper/MSFlagger` (clipping by value) and the flag-command and bit-flag
classes, nothing else.  RFlag is CASA's own C++:

- `casatools/src/code/flagging/Flagging/FlagAgentRFlag.cc` in
  [casa6](https://open-bitbucket.nrao.edu/projects/CASA/repos/casa6), class
  `FlagAgentRFlag`, a `FlagAgentBase` run by `FlagDataHandler`.

Line numbers below refer to that file on `master` (1304 lines) as fetched
2026-09-25.

## 2. CASA's algorithm, from the source

**Unit of work: one baseline.**  The agent is constructed with the
`ANTENNA_PAIRS` iteration mode (l. 32), so `computeAntennaPairFlags`
(l. 1122) is called once per baseline with that baseline's
`(pol, chan, time)` cube over the flag chunk -- `ntime` of `flagdata`,
by default a scan.  Nothing in a call ever mixes baselines.

**Time analysis** (`computeAntennaPairFlagsCore`, l. 805-883).  For every
channel and polarisation, a window of `winsize` timesteps (default 3) slides
along time.  In each window the population variance of the real and of the
imaginary part about the window mean is taken from sums and sums of squares,
and `StdTotal = sqrt(var_re + var_im)`.  Where `StdTotal > noise`, every
timestep of the window is flagged.  Only complete windows are evaluated: the
first and last `(winsize-1)/2` timesteps of the chunk get the spectral
analysis only (l. 1217-1236).  Samples flagged before the run are left out of
the sums (`getOriginalFlags`, l. 829).

**Spectral analysis** (l. 885-970, `simpleMedian` l. 1065).  For every
timestep and polarisation, the reference is the median over *all channels* of
the real part, and separately of the imaginary part.  A channel is flagged
where `|re - median_re| > scutoff` or `|im - median_im| > scutoff`.

**Thresholds.**  `noise = timedev * timedevscale` and
`scutoff = freqdev * freqdevscale` (l. 1142-1190).  `timedev`/`freqdev` may be
given as one number or as a `[field, spw, dev]` matrix (l. 259-400).  When
not given, a first pass (`prepass_p`) accumulates, per (field, spw) and per
channel, the mean over **all baselines, polarisations and timesteps** of
`StdTotal` (time) and of the absolute deviations from the spectrum median
(spectral).  `computeThreshold` (l. 461-491) then reduces the per-channel
means to one number, `median + 1.4826 * MAD` taken **over channels**
(`passIntermediate`, l. 1242).  So CASA flags with **one time threshold and
one spectral threshold per field and spectral window**, pooled over every
baseline.  `action='calculate'` stops after the first pass and returns them.

**`spectralmax`/`spectralmin`** (l. 933-949) flag a whole spectrum (one
timestep of one baseline) when `StdReal` or `StdImag` fall outside the bounds.
In the default `optype='MEDIAN'` those are computed as
`1.4826 * median(x - median(x))` (l. 1112-1116) -- without an absolute value,
so they are zero to rounding.  `spectralmax` (default 1e6) therefore never
fires, and `spectralmin` fires on every spectrum when set above zero -- and on
any spectrum whose rounding comes out negative even at its default of zero.
This looks like a bug in CASA; it is noted here because skarabina does not
reproduce it.

**`optype`** selects the spectral reference.  Only `MEDIAN` (default) and
`RMEAN` (`robustMean`, 12 iterations of clipping at 6, 5, 4, 3.6, ... sigma,
l. 40-54 and 975) change anything: `RMEDIAN` and `MEAN` log their name but leave
the median in place (l. 189-199).

## 3. skarabina's algorithm

`skarabina/rflag.py`, run by `DaskMS.flag_rflag` over each dask row chunk
(`--row-chunk`, default 10 000 rows) and each correlation separately.

**Unit of work: a row chunk.**  An MS is written time-major, so a chunk's rows
are the *different baselines* of consecutive integrations -- ~1900 per
integration for MeerKAT, about five integrations of each baseline in a
10 000-row chunk.  Up to 1.0.6 the rows of a chunk were treated as one
baseline's time series (§5).  With baseline-aware chunks (§4) the rows are
grouped by `(SCAN_NUMBER, ANTENNA1, ANTENNA2)`, each group kept in time order.

**Time analysis.**  Per channel, a centred window of `winsize` rows slides
along each baseline's rows.  The scatter is the same quantity as CASA's --
`sqrt(var_re + var_im)` about the window mean, from prefix sums -- with flagged
samples left out and a window of fewer than two usable samples giving no
estimate.  Where the scatter exceeds the threshold, every row of the window is
flagged.  The threshold is `timedevscale * median(scatter / noise)` per
channel, the median taken over every baseline and window in the chunk and
`noise` each baseline's modelled noise (§4); a supplied `timedev` is used as
`timedevscale * timedev` in data units, as in CASA.

**Spectral analysis.**  Each sample is compared with the median of its
neighbouring channels (±1, widened to ±3 where neighbours are flagged), in the
real and imaginary parts separately, and the larger departure is kept.  The
threshold is `freqdevscale * median(|departure| / noise)` over the chunk; a
supplied `freqdev` is used in data units.

**`spectralmax`/`spectralmin`** are compared with the chunk's measured
deviation, `median(|departure|)` in data units, and an excursion flags the
whole plane (chunk x correlation).

## 4. Baseline-aware chunks and the per-antenna noise model

`skarabina/baselines.py`.

- `Baselines` groups a chunk's rows by `(scan, antenna1, antenna2)`, keeping
  each group's rows in their original (time) order, and records each row's
  group span so a sliding window can be clipped to it.  `DaskMS._run_autofit`
  hands each block its `ANTENNA1`, `ANTENNA2` and `SCAN_NUMBER` rows.
- `row_noise` estimates each row's noise from the robust scatter of
  adjacent-channel differences (at most 256 strided pairs per row).  A
  one-channel difference cancels the bandpass and the source and leaves the
  noise, `sqrt(2)` times over.
- `antenna_noise_model` fits `log noise_ij = s_i + s_j` over the baselines of
  the chunk, drops those more than 3 robust sigmas from the fit, refits, and
  predicts every baseline's noise.  Thermal noise on a baseline scales as
  `sqrt(SEFD_i SEFD_j)`, times `|g_i g_j|` for uncalibrated data, so it
  factorises by antenna; baseline length is not a factor.

Measured on `mergA_tim.ms` scan 1 (MeerKAT L band, bandpass calibrator,
1830 cross baselines of 15-7578 m, correlation 0, RFI-free channels only,
noise from adjacent-channel differences):

| predictor of a baseline's noise | spread left between baselines (robust sigma of log noise) |
|---|---|
| none | 7.8 % |
| baseline length, 10 bins | 7.0 % |
| **one factor per antenna** | **1.5 %** |
| antennas and length | 1.2 % |

Longer baselines were slightly quieter (Spearman -0.33), and the antenna
factors absorb nearly all of it: short baselines are mostly core antennas.
The per-antenna factors spanned 0.91-1.06, and no baseline departed from the
model by more than 1.11x.

Two things follow.  Noise differs little between baselines, so pooling a
threshold over them -- as CASA does -- is sound; and a model fitted over ~1800
baselines is not fooled by one baseline whose own estimate is inflated by RFI.
In the synthetic test (`tests/test_baselines.py`) a baseline with injected RFI
had its own noise estimate at 1.84x the truth and its modelled noise at 0.99x.

The cost of the second property: a baseline that is genuinely noisier than its
antennas predict -- a bad correlator input, a 3x noise excess in the synthetic
test -- is judged against the model and loses 20-40 % of its samples.  CASA's
single pooled threshold flags such a baseline too.

## 5. Why: rows of a chunk are not a time series

A diagnostic on the same scan, running tfcrop two ways on correlation 0 after
autos, NaN and clip, measured the new flags in the channels outside every RFI
window of `spectral-flags-L.yml` ("clean") and inside them:

| plane | new/live, clean band | new/live, RFI windows | new flags in whole rows (>90 % flagged) |
|---|---|---|---|
| a 10 000-row chunk, rows as one series (≤ 1.0.6) | **27.3 %** | 44.3 % | 38.8 % |
| one baseline x 76 integrations, 40 baselines | **4.3 %** | 41.2 % | 0 % |

Most of the clean-band flagging was the chunk layout: divided by one bandpass
averaged over the chunk, every baseline sits at its own level (its source
amplitude, its antennas' gains), and the test against 1 flags whole rows of
every baseline brighter or fainter than average.  For rflag the layout does the
opposite: a window of three consecutive rows spans three baselines, whose
level differences dominate the scatter, the median scatter and the threshold
are inflated, and the time step finds little.

## 6. The same for tfcrop

`tfcrop_plane(..., baselines=...)`: each baseline's rows are averaged over time
and that spectrum gets its own robust piece-wise fit (all baselines in one
batched fit); the frequency test is on `plane - fit` in units of the
antenna-model scatter of those residuals, at `freqcutoff`; the time test is on
each sample's departure from its baseline's per-channel median over the chunk,
in modelled units, flagged per channel over the chunk at `timecutoff`.  The
scatter is modelled in data units, not as a ratio to the fit: the noise
factorises by antenna, the level it would be divided by includes the source,
which does not except for a point source.

## 7. Measurements

### 7.1 Synthetic interleaved chunks

`bench/autoflag_interleaved_eval.py`: 9500 rows written time-major over 190
baselines (20 antennas, 50 integrations, 128 channels), each baseline at its
own complex level (amplitude 5-15, random phase) on a sloping band,
per-antenna noise, 30 % pre-flagged, 2-integration bursts on 20 baselines and
narrow-band spikes.  Two seeds each (FP = new flags on clean live samples):

| | FP | recall | bursts | narrow-band |
|---|---|---|---|---|
| tfcrop, rows as one plane | 62-65 % | 80-84 % | 70-80 % | 81-85 % |
| tfcrop, baseline-aware | **0.45 %** | 70-72 % | 69-72 % | 70-72 % |
| rflag, rows as one series | 0.25-0.29 % | 56-58 % | 65 % | 55-57 % |
| rflag, baseline-aware | 0.33 % | 58-59 % | **77-83 %** | 57-58 % |

### 7.2 mergA_tim scan 1

143 716 rows (76 integrations x 1891 baselines incl. autos) x 2511 channels x 2
correlations, MeerKAT L band, bandpass calibrator; after `autos`, `nan` and
`clip 0 100`, with **no** `spectral-window` step, so the RFI windows of
`spectral-flags-L.yml` are still live.  "Clean" is every channel outside those
windows and the band edges; "short" is |uv| < 600 m, the file's own boundary.
New flags as a fraction of the live samples in each region; schmalzburg (Ryzen
5 5600G, 12 threads, 62 GB), casacure 3.8.7, default `--row-chunk 10000`,
single runs.

| | clean, short | clean, long | RFI windows, short | RFI windows, long | total new | flagger time | peak RSS |
|---|---|---|---|---|---|---|---|
| tfcrop, rows as one series | 30.4 % | 30.2 % | 52.8 % | 40.6 % | 240.0 M | 210 s | 17.7 GB |
| **tfcrop, baseline-aware** | **5.6 %** | **3.7 %** | 53.2 % | 30.8 % | 116.0 M | **89-98 s** | 18.2 GB |
| rflag, rows as one series | 0.78 % | 0.52 % | 37.5 % | 24.9 % | 73.3 M | 260 s | 20.1 GB |
| rflag, baseline-aware | 1.39 % | 0.56 % | 40.4 % | 25.4 % | 78.1 M | 271 s | 22.9 GB |

Both rflag rows include the window-statistics fixes of §8 (they ran on the
same branch); rflag as released in 1.0.6 was not measured here.

- **tfcrop**: the clean-band flagging falls from 30 % to 4-6 %, matching the
  4.3 % of the single-baseline diagnostic in §5, while the RFI windows on short
  baselines -- where `spectral-flags-L.yml` says the RFI is -- are flagged as
  before (53 %).  On long baselines the RFI windows lose 10 points, the side
  the file does not flag at all.  The run is twice as fast: one batched
  bandpass fit per baseline replaces the per-column time fits.
- **rflag** changes less, because its spectral step already compared each
  sample with neighbouring channels of the *same row*, which is
  layout-independent.  The time step now looks along a baseline and adds
  3 points in the short-baseline RFI windows and 0.6 in the short-baseline
  clean band -- consistent with RFI outside the listed windows, which is
  strongest on short spacings, but without ground truth that is a reading,
  not a measurement.
- Memory: the first baseline-aware tfcrop run peaked at 28.2 GB; reusing one
  working plane in place (commit `2b05c8a`) brought it to 18.2 GB with the
  same flags, level with the old path.  Either way ~1.5 GB per dask worker at
  2511 channels: `--workers`/`--row-chunk` bound it.
- The band edges (18 channels here) are flagged harder by baseline-aware
  tfcrop, 45-50 % against 32-34 %: a per-baseline fit follows each roll-off
  less well than one averaged over the chunk.  They are flagged by the
  `spectral-window` step of the pipeline in any case.

### 7.3 Memory against the chunk size

Both flaggers work one row chunk at a time per dask worker, and every
full-plane temporary is the size of the chunk, so memory follows the chunk.
Per block, single-threaded, peak working memory beyond the block's inputs
(`tracemalloc`; rows of scan 1, 2511 channels x 2 correlations):

| rows per chunk | 2 500 | 5 000 | 10 000 | 20 000 |
|---|---|---|---|---|
| rflag, baseline-aware | 307 MB | 589 MB | 1152 MB | 2278 MB |
| tfcrop, baseline-aware | 502 MB | 648 MB | 1069 MB | 1955 MB |

Linear: 114-123 MB per 1000 rows for rflag, and 98 MB per 1000 rows for
tfcrop on top of ~250 MB of work capped by `GROUP_VALUES`.  rflag was 186 MB
per 1000 rows, 4.9x the chunk's DATA, until its spectral step combined its
parts in place (commit `154f144`); it is now 3.0x.

End to end, peak RSS of a whole flagging pass (autos, nan, clip, then the
flagger) over scan 1 with 12 workers, against the rows in flight (row chunk x
the workers that get one -- at 20 000 rows the scan is only 8 chunks):

| flagger | row chunk x workers | rows in flight | peak RSS |
|---|---|---|---|
| tfcrop | 5 000 x 12 | 60 000 | 10.5 GB |
| tfcrop | 10 000 x 6 | 60 000 | 9.6 GB |
| tfcrop | 10 000 x 12 | 120 000 | 17.9 GB |
| tfcrop | 20 000 x 12 (8 chunks) | 143 716 | 20.0 GB |
| rflag | 5 000 x 12 | 60 000 | 11.3 GB |
| rflag | 10 000 x 6 | 60 000 | 11.0 GB |
| rflag | 10 000 x 12 | 120 000 | 20.6 GB |
| rflag | 20 000 x 12 (8 chunks) | 143 716 | 23.7 GB |

That is `base + rows in flight x nchan x ncorr x b`, with b = 26 bytes per
visibility and a 3.2 GB base for tfcrop, 33 bytes and 2.0 GB for rflag: the
same rows in flight cost the same whether they come from more workers or
larger chunks, and the table's length does not enter.

### 7.4 Memory of the other steps, and the plan

The other verbs, each alone on `.bench/scan1.ms` (a written copy of scan 1),
12 workers, followed by one chunked pass over FLAG (what the summary or the
write would do):

| step | 5 000 rows | 10 000 rows | kind |
|---|---|---|---|
| no verb (the pass alone) | 0.4 GB | 0.2 GB | per chunk, ~1 B/vis |
| `autos` | 0.6 GB | 0.7 GB | per chunk |
| `uv-above 8000` | 0.4 GB | 0.2 GB | per chunk |
| `nan` | 1.3 GB | 1.8 GB | per chunk |
| `clip 0 100` | 1.9 GB | 2.2 GB | per chunk |
| `spectral-window` | 1.1 GB | 1.8 GB | per chunk (1.8 / 2.6 GB before 1.0.8, when each rule held a table-sized array) |
| `restore:` | 0.8 GB | 1.2 GB | per chunk (read lazily since `d1e1190`; it held the whole cube before) |
| `save:` | 1.9 GB | 1.8 GB | **whole table**: 2.2 B per MS visibility (19.7 GB predicted for the full MS, 18-19 GB measured) |
| stage-0 list + rflag | -- | 21.1 GB | = rflag alone (20.6 GB) + 0.5 |
| stage-0 list + tfcrop | -- | 18.1 GB | = tfcrop alone (17.9 GB) + 0.2 |
| write: `--write-changed-only`, `--apply` | -- | 1.8 GB | flag columns only |
| write: full `--msout` | 38.3 GB | 40.7 GB | **whole table**: ~56 B per output visibility |

A run peaks at its most expensive step, not at the sum of its steps.  Two
steps hold a whole table whatever the chunk, because casacure buffers what it
writes until the table is flushed: `save:` (the flag cube) and a full
`--msout` write, which needed 40.7 GB to write the 11 GB copy -- about 450 GB
for the whole 124 GB MS, so a full write of a large MS is only possible
averaged or as flags alone.

`skarabina.memory.plan` turns this into the run's plan: per-chunk costs
(bytes per visibility per worker: read 0.5 and `uv-above` 1, `autos` 2, `nan`
and `clip` 4, `spectral-window` 4 (5 before 1.0.8), `restore:` 3, tfcrop 30 +
3.5 GB, rflag 36 + 2.5 GB), per-table costs (`save:` 2.5 B per MS visibility,
full write 56 B and flags-only write 1.5 B per output visibility -- these
apply only to a backend that buffers the table it writes), and the largest row
chunk keeping every per-chunk step of the `--flag` list within 80 % of
`--memory-limit-GB` (default: the RAM available).  The run prints the plan --
naming the backend and casacure version it assumed -- and warns about a
whole-table step that does not fit.  Checked on the scan-1
copy with the stage-0 list and rflag:

| limit | chosen chunk (set by) | planned rflag peak | measured peak |
|---|---|---|---|
| available RAM, 46.9 GB | 11 977 (12 workers over 143 716 rows) | 27.2 GB | 26.5 GB |
| `--memory-limit-GB 16` | 4 850 (rflag) | 12.8 GB | 11.6 GB |

Since commit `0eecfa1` a full `--msout` write shares the flagging pass (one
read of DATA), so its chunks -- DATA, WEIGHT_SPECTRUM, SIGMA_SPECTRUM -- are in
flight with the flaggers'.  The same scan with the stage-0 list, rflag,
`--frequency-average-factor 32`, `--summary` and a full write peaked at
31.0 GB against 25.6 GB with the write as a second pass: 7.5 bytes per
visibility per worker more, which is where `CONCURRENT_WRITE_COST`'s 8 came
from.  That measurement was made while casacure buffered a written table, so
the plan reserved the write's whole-table estimate as well.

**Re-measured 2026-09-26** (casacure 3.8.9, which streams its writes, so
nothing is buffered) with `bench/mem_recal.py`; the numbers and the residuals
are in `BENCHMARKS.md`.  A full write now costs 4.5 B per input visibility in
flight, a flags-only write 0.5, and a write whose output is *smaller* than the
input -- an averaging write -- pays `CONCURRENT_AVERAGING_COST` 4 on top,
because the averaging step holds the input chunk it is reading while the
averaged chunk is written.  `save:`'s streamed estimate (20 000 rows x 3 B per
visibility) was checked on scan 1 alone: 800 MiB planned, 784 MiB measured.
The plan's linear form over-predicts at large chunks (up to 2.7x at 40 000
rows, where a run's transients are bigger than the chunks it has) and
under-predicts by ~25 % at chunks well below `nrow / --workers`; a per-chunk
term rather than only a per-worker one would fit both, and is not in yet.

## 8. Bugs found on the way

Both were in the single-baseline algorithm and are fixed for it too:

1. **Flagged samples counted as zeros in the window scatter.**  `local_rms`
   filled flagged samples with zero and divided by the window's *length*, not
   by the number of usable samples.  Against a signal of ~10, a window holding
   one flagged sample showed a scatter of ~5; with 30 % of samples flagged two
   windows in three hold one, which lifted the median and the threshold until
   the time step flagged nothing.  (1.0.6 fixed the related `nan+0j` masking,
   which zeroed only the imaginary part.)
2. **A window with one usable sample has a scatter of exactly zero**, and on
   heavily flagged data enough of those zeros pull the threshold down onto the
   noise.  Such windows now give no estimate.

## 9. Differences at a glance

| | CASA `flagdata(mode='rflag')` | skarabina `rflag` |
|---|---|---|
| unit of work | one baseline, `ntime` chunk (default: scan) | one dask row chunk (~5 integrations of every MeerKAT baseline at 10 000 rows), grouped by baseline |
| thresholds from | first pass over the whole selection, per (field, spw) | each chunk, per correlation |
| time threshold | one per (field, spw): `(median + 1.4826 MAD over channels of per-channel mean scatter) * timedevscale` | per channel: `timedevscale * median(scatter / modelled noise)` over the chunk |
| spectral reference | median over **all** channels of the spectrum | median of the **neighbouring** channels |
| spectral threshold | one per (field, spw), as for time | `freqdevscale * median(|departure| / modelled noise)` over the chunk |
| per-baseline noise | none (one pooled value) | per-antenna model, `s_i s_j` |
| window at chunk edges | skipped (spectral only) | clipped, every row gets a window |
| supplied `timedev`/`freqdev` | number or `[field, spw, dev]` matrix | number |
| `spectralmax`/`spectralmin` | per spectrum; inert in MEDIAN mode (§2) | per plane, on the measured deviation |
| `optype` | MEDIAN, RMEAN (RMEDIAN, MEAN fall back to MEDIAN) | none (neighbour median) |
| pre-existing flags | excluded from statistics | excluded from statistics |

## 10. Open points

- A baseline's time series in a chunk is short: `winsize=3` over ~5
  integrations.  A larger `--row-chunk` lengthens it at the cost of memory, and
  a chunking that follows baselines across integrations would remove the
  limit.
- Scan boundaries split the groups; field changes follow scans.
- The thresholds are measured per chunk, not over the whole selection as in
  CASA.  A two-pass mode (measure, then flag) would need a second pass over
  `DATA`.
