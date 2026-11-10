<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Averaging in skarabina

## Fringe-rotation integration time limit

A radio interferometer measures visibilities by averaging (integrating) the
correlated electric field over an integration time Δt.  As the Earth rotates,
the geometric delay between antennas changes — this is *fringe rotation*.
If Δt is too long the visibility amplitude decorrelates (smears).

Following Wijnholds (2018, MNRAS), the amplitude loss for time averaging
at an angular distance ℓ from the phase centre is:

$$ \rho = \text{sinc}\left( \frac{\pi \cdot \omega_\oplus \cdot \Delta t \cdot B \cdot \nu \cdot \ell}{c} \right) $$

For small amplitude loss $L = 1 - |\rho|$:

$$ \Delta t_\text{max} \approx \frac{c \cdot \sqrt{6L}}{\pi \cdot \omega_\oplus \cdot B_\text{max} \cdot \nu_\text{max} \cdot \ell} $$

That is the small-angle form of the relation.  The code inverts
$\rho = \text{sinc}(\pi x)$ **exactly** rather than through this approximation
(`skarabina.dask_ms.time_average_loss_to_dt`, a bisection on the first
crossing), so a limit delivers the loss it claims at any value of *L*.  The two
agree closely over the range used here; the small-angle form is the shorter,
i.e. the more conservative:

| *L*  | sqrt(6L)/pi | exact  | difference |
|------|-------------|--------|------------|
| 0.01 | 0.0780      | 0.0781 | +0.2%      |
| 0.10 | 0.2466      | 0.2504 | +1.6%      |
| 0.20 | 0.3487      | 0.3600 | +3.3%      |

(coefficients of $1/(\omega_\oplus B \nu \ell)$.)

| Symbol      | Value / units               |
|-------------|-----------------------------|
| *c*         | 299 792 458 m s⁻¹          |
| *ω*⊕        | 7.292 115 0 × 10⁻⁵ rad s⁻¹|
| *ℓ*         | Distance from the phase centre to the edge of the field (rad): half of `--field-of-view`, which is a FULL width (default 1° → ℓ = 0.5° ≈ 0.0087 rad). |
| *B*<sub>max</sub> | Longest baseline (m)   |
| *ν*<sub>max</sub> | Highest channel frequency (Hz) |
| *L*         | Allowed amplitude loss (1% → 0.01, 3% → 0.03, 5% → 0.05, 10% → 0.10) |

### Example values (ℓ = 1 rad)

| Baseline | Frequency | 1% loss  | 10% loss |
|----------|-----------|----------|----------|
| 100 m    | 1.4 GHz   | 2.29 s   | 7.35 s   |
| 1 km     | 1.4 GHz   | 229.3 ms | 735.3 ms |
| 10 km    | 1.4 GHz   | 22.9 ms  | 73.5 ms  |
| 1 km     | 150 MHz   | 2.14 s   | 6.86 s   |
| 1 km     | 5 GHz     | 64.2 ms  | 205.9 ms |

The 10% column is the criterion `skarabina-analyze` reports as
`max_integration_time_s`.

### Usage

The `summary()` function reports:
- Δt<sub>max</sub> for 1%, 3%, 5% and 10% loss using the MS's maximum UV
  distance and highest channel frequency.  The 10% row is
  `dask_ms.TIME_AVERAGE_LOSS` — the same criterion, and the same function, that
  `skarabina-analyze` uses for its `max_integration_time_s` output, so the two
  commands can be compared directly (they used to compute that limit from
  different formulas).
- The current integration time: the nominal (most common) value of the MS's
  `INTERVAL` or `EXPOSURE` column.  It is deliberately not taken from the first
  row, because an MS whose writer split an integration can carry a shortened
  interval there (6.0 s instead of 8.0 s on a real MeerKAT file).
- The number of integrations and the observing cadence, grouped gap-tolerantly:
  some writers stamp one integration's rows with more than one `TIME` value, so
  counting distinct `TIME` values over-counts.  See the 0.8.6 changelog entry.
  Integrations short of a complete baseline set are reported as
  `Incomplete integration groups`, which is worth checking before time
  averaging.

`--field-of-view` is the **full width** of the field of view (default 1°), the same
convention as `skarabina-analyze --image-fov`.  ℓ -- the distance from the phase
centre to its edge -- is half of it (0.5° ≈ 0.0087 rad by default).

The `--time-average-factor N` option combines every N consecutive time
samples **of each baseline**, averaging DATA/UVW/TIME, summing
WEIGHT_SPECTRUM/INTERVAL/EXPOSURE, inverse-variance combining
SIGMA_SPECTRUM — excluding flagged data from the visibilities — and
flagging an output sample only when all of its inputs are flagged, as CASA's
`mstransform` does (see [Flags](#flags-mstransform-semantics)).  This reduces
data volume before `--optimize`.

## Flags (mstransform semantics)

Both averages treat flags the way CASA's `mstransform` does for `timeaverage`
and `chanaverage` (skarabina#16, 1.0.27):

* an output sample with **at least one unflagged input** is the average of the
  unflagged inputs (DATA mean, WEIGHT_SPECTRUM sum, SIGMA_SPECTRUM
  inverse-variance) and is **unflagged**;
* an output sample whose **every input is flagged** is flagged, and its DATA /
  WEIGHT_SPECTRUM / SIGMA_SPECTRUM are taken over *all* of its inputs, so the
  data are kept rather than zeroed;
* in time averaging, a row with `FLAG_ROW` set counts as flagged in every
  channel and correlation (CASA's flag cube), and an output row gets
  `FLAG_ROW` only when every one of its samples is flagged.

Before 1.0.27 the output flag was the **OR** of the inputs: one flagged
integration or channel flagged the output sample even though its unflagged
average had just been computed.  On MeerKAT L-band data that discarded 0.5–7
percentage points of visibilities, growing with the factor, and far more
where flags are sparse in time.

## Time averaging (`--time-average-factor`)

Averages every *N* consecutive time samples **per baseline** into a single
row, reducing the measurement set size by a factor of *N*.

Rows are grouped by `(FIELD_ID, SCAN_NUMBER, ANTENNA1, ANTENNA2)` — so a bin
never mixes baselines, scans or fields — and each group's rows are binned by
time rank.  The output is ordered time-major (bin centre, then baseline):
every output timestamp carries the full baseline set and the row count stays
a multiple of the baseline count, which is what killMS's conformance check
requires.  (Averaging *consecutive rows* instead — the behaviour before
skarabina#15 — merged different baselines on a time-ordered MS, lost half the
baseline labels and zeroed TIME on fully-flagged groups.)

| Column          | Operation | Notes |
|-----------------|-----------|-------|
| DATA            | Masked mean | Flagged visibilities excluded (all of them if all are flagged) |
| WEIGHT_SPECTRUM | Sum         | w = 1/σ², combined: Σ wᵢ (flagged excluded) |
| SIGMA_SPECTRUM  | 1/√(Σ 1/σ²) | Inverse-variance weighting (flagged excluded) |
| FLAG            | AND         | Flagged only if every input sample is (mstransform) |
| FLAG_ROW        | AND         | Set only if every output sample of the row is flagged |
| UVW             | Mean       | Bin centre — unmasked, so a fully-flagged bin keeps a valid uvw |
| TIME            | Mean       | Bin centre (mean of the bin's timestamps) — unmasked |
| INTERVAL        | Sum        | The bin's integrations — unmasked |
| EXPOSURE        | Sum        | The bin's integrations — unmasked |
| ANTENNA1/2      | Group key  | Constant within the bin |

Geometry (UVW/TIME/INTERVAL/EXPOSURE) is deliberately averaged **unmasked**:
flags mask the visibilities only, so a fully-flagged bin keeps a valid
timestamp and uvw instead of being zeroed, and stays flagged.  Incomplete trailing bins (fewer than *N* samples left in a
baseline's group) are discarded, matching the old row trim.  Run `--summary`
afterward to see the updated integration time.

## Frequency averaging (`--frequency-average-factor`)

Averages every *N* consecutive frequency channels into one, reducing the
channel count by a factor of *N*.

| Column          | Operation | Notes |
|-----------------|-----------|-------|
| DATA            | Masked mean | Flagged visibilities excluded (all of them if all are flagged) |
| WEIGHT_SPECTRUM | Sum         | w = 1/σ², combined: Σ wᵢ (flagged excluded) |
| SIGMA_SPECTRUM  | 1/√(Σ 1/σ²) | Inverse-variance weighting (flagged excluded) |
| FLAG            | AND         | Flagged only if every input channel is (mstransform) |
| CHAN_FREQ       | Mean        | SPECTRAL_WINDOW updated |

Trailing channels (fewer than *N*) are combined into a final narrower
channel rather than discarded.  The SPECTRAL_WINDOW `CHAN_FREQ` column
in the output MS is updated to reflect the new channel count.

### Pipeline order

Frequency averaging runs before time averaging and optimization, so all
operations see the reduced channel count:

```
flagging → frequency-average → time-average → optimize → summary → write
```

## Optimization (`--optimize`)

Removes rows and channels that carry no usable data: a row goes if `FLAG_ROW` is
set or every visibility in it is flagged; a channel goes if every visibility in
it, across all rows and correlations, is flagged.  Both criteria are lossless —
what is removed is precisely what a flagger has already marked as bad, and
flagging alone (without `--optimize`) never removes anything.

### Removing a channel can split the band

Dropping a dead channel at the *edge* of the band just shortens it.  Dropping
one from the *middle* leaves a **hole**, and nothing in SPECTRAL_WINDOW records
it: `CHAN_FREQ` keeps the true (now non-uniform) channel frequencies, but
`CHAN_WIDTH` still describes each surviving channel and `TOTAL_BANDWIDTH` still
sums what is left.  A consumer that assumes contiguous channels will read the
band as wider per channel than it really is.

This is the normal outcome of `spectral-window <rules.yml>` followed by `--optimize`
— flagging a contiguous RFI notch and then removing the flagged channels:

```
input  CHAN_FREQ: 1000 1010 1020 1030 1040 1050 1060 1070 MHz  (10 MHz channels)
after  CHAN_FREQ: 1000 1010 1020            1050 1060 1070 MHz
gaps:              10   10   30   10   10 MHz   <- 20 MHz hole, unrecorded
```

`--summary` and `skarabina-analyze` both detect this and say so:
`--summary` prints a `Band has holes:` line, and `analyze` reports
`band_has_gaps` with the true `channel_width_hz` read from `CHAN_WIDTH` rather
than derived from the band span.  `--optimize` itself warns when dropping
channels splits the band.

### Keeping the channels instead

`--keep-fully-flagged-channels` (used with `--optimize`) retains those channels
instead of removing them.  Flagging already excludes them from imaging, so
keeping them costs file size and nothing else — but it keeps the band
contiguous, which is the safer choice when the output feeds other tools.  The
rows are still optimized away.

### Cost

`--optimize` is in-memory until the MS is written, and that now requires
`--msout`: an in-place `--apply` cannot remove rows or channels (it
overwrites columns at their existing rows and channel widths), so
`--optimize --apply` and averaging with `--apply` are refused.  Removing
*rows* leaves the row chunking intact, so writing is cheap.  Removing
non-contiguous *channels* forces the data array to be re-chunked and rewritten
in full, which on a large MS costs a complete pass over DATA — another reason to
consider `--keep-fully-flagged-channels` when the band would otherwise be split.
