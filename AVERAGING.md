<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Averaging in skarabina

## Fringe-rotation integration time limit

A radio interferometer measures visibilities by averaging (integrating) the
correlated electric field over an integration time Δt.  As the Earth rotates,
the geometric delay between antennas changes — this is *fringe rotation*.
If Δt is too long the visibility amplitude decorrelates (smears).

The fringe rate (rate of phase change) for a baseline *B* at frequency *ν* is:

$$ f_\text{fringe} = \frac{\omega_\oplus \cdot B \cdot \nu}{c} $$

where:

| Symbol         | Value / units               |
|----------------|-----------------------------|
| *ω*⊕           | 7.2921150 × 10⁻⁵ rad s⁻¹   |
| *B*            | baseline length (m)         |
| *ν*            | observing frequency (Hz)    |
| *c*            | 299 792 458 m s⁻¹           |

The phase accumulated over Δt is Δφ = 2π · *f*<sub>fringe</sub> · Δt.
To avoid significant decorrelation we require the visibility amplitude
|V|/|V₀| = sinc(ω⊕ B ν Δt / c) to stay close to unity, giving the
approximate upper limit:

$$ \Delta t_\text{max} \approx \frac{c}{\nu \cdot \omega_\oplus \cdot B} $$

### Example values

| Baseline | Frequency | Δt<sub>max</sub> |
|----------|-----------|-------------------|
| 100 m   | 1.4 GHz  | 29.4 s            |
| 1 km    | 1.4 GHz  | 2.94 s            |
| 10 km   | 1.4 GHz  | 0.294 s           |
| 1 km    | 150 MHz  | 27.4 s            |
| 1 km    | 5 GHz    | 0.82 s            |

### Usage in skarabina

The `summary()` function reports Δt<sub>max</sub> for the measurement set
using the maximum UV distance (longest baseline) and the highest channel
frequency.  The output is purely informational — no averaging is currently
performed.  A future `--average` option could combine visibilities over
consecutive time steps subject to this limit.
