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

$$ \Delta t_\text{max} = \frac{c \cdot \sqrt{6L}}{\pi \cdot \omega_\oplus \cdot B_\text{max} \cdot \nu_\text{max} \cdot \ell} $$

| Symbol      | Value / units               |
|-------------|-----------------------------|
| *c*         | 299 792 458 m s⁻¹          |
| *ω*⊕        | 7.292 115 0 × 10⁻⁵ rad s⁻¹|
| *ℓ*         | Distance from phase centre (rad). Derived from `--field-of-view` in degrees (default 1° → 0.0175 rad). |
| *B*<sub>max</sub> | Longest baseline (m)   |
| *ν*<sub>max</sub> | Highest channel frequency (Hz) |
| *L*         | Allowed amplitude loss (1% → 0.01, 3% → 0.03, 5% → 0.05) |

### Example values (ℓ = 1 rad, 1% loss)

| Baseline | Frequency | Δt<sub>max</sub> |
|----------|-----------|-------------------|
| 100 m   | 1.4 GHz  | 2.29 s            |
| 1 km    | 1.4 GHz  | 0.229 s           |
| 10 km   | 1.4 GHz  | 22.9 ms           |
| 1 km    | 150 MHz  | 2.14 s            |
| 1 km    | 5 GHz    | 64.2 ms           |

### Usage

The `summary()` function reports:
- Δt<sub>max</sub> for 1%, 3%, and 5% loss using the MS's maximum UV distance
  and highest channel frequency.
- The current integration time from the MS's `INTERVAL` or `EXPOSURE` column.

The field-of-view half-width ℓ defaults to 1° (≈ 0.0175 rad) and can be set
via `--field-of-view`.

The `--time-average-factor N` option combines every N consecutive rows,
averaging DATA/UVW/TIME, summing WEIGHT_SPECTRUM/INTERVAL/EXPOSURE,
inverse-variance combining SIGMA_SPECTRUM, and OR-ing FLAG columns —
excluding flagged data from each.  This reduces data volume before
`--optimize`.

## Time averaging (`--time-average-factor`)

Averages every *N* consecutive rows into a single row, reducing the
measurement set size by a factor of *N* (≈ *N*).

| Column          | Operation | Notes |
|-----------------|-----------|-------|
| DATA            | Masked mean | Flagged visibilities excluded |
| WEIGHT_SPECTRUM | Sum         | w = 1/σ², combined: Σ wᵢ (flagged excluded) |
| SIGMA_SPECTRUM  | 1/√(Σ 1/σ²) | Inverse-variance weighting (flagged excluded) |
| FLAG, FLAG_ROW  | OR          | Any flagged → flagged |
| UVW             | Masked mean | Fully-flagged rows excluded |
| TIME            | Masked mean | Fully-flagged rows excluded |
| INTERVAL        | Masked sum  | Fully-flagged rows excluded |
| EXPOSURE        | Masked sum  | Fully-flagged rows excluded |
| ANTENNA1/2      | First       | Same baseline in block |

A row is "fully flagged" when `FLAG_ROW` is True or every visibility in
`FLAG` is True; partially-flagged rows still contribute to the per-row
metadata.  Trailing rows (fewer than *N*) are discarded.  Run `--summary`
afterward to see the updated integration time.

## Frequency averaging (`--frequency-average-factor`)

Averages every *N* consecutive frequency channels into one, reducing the
channel count by a factor of *N*.

| Column          | Operation | Notes |
|-----------------|-----------|-------|
| DATA            | Masked mean | Flagged visibilities excluded |
| WEIGHT_SPECTRUM | Sum         | w = 1/σ², combined: Σ wᵢ |
| SIGMA_SPECTRUM  | 1/√(Σ 1/σ²) | Inverse-variance weighting |
| FLAG            | OR          | Any flagged → flagged |
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
