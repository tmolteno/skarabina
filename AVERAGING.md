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

The `--time-average-factor N` option combines every N consecutive rows by
averaging DATA, UVW, and scalar columns, and OR-ing FLAG columns.  This
reduces data volume before `--optimize`.
