<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# skarabina-analyze

Analyses a measurement set and recommends an image size for synthesis
imaging.

## Usage

    skarabina-analyze --ms <measurement_set> --image-fov <degrees> [--oversampling-factor <N>]
                      [--output-json <file>] [--json-stdout]

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--ms` | (required) | Input measurement set |
| `--image-fov` | (required) | Desired image field-of-view, FULL width, in degrees (same convention as the `skarabina` cab's `--field-of-view`) |
| `--oversampling-factor` | 5.0 | Pixels per synthesised beam |
| `--output-json` | (none) | Write the analysis results to this file as JSON |
| `--json-stdout` | off | Print the analysis results as a single JSON line on stdout |

`--output-json` and `--json-stdout` may be used together or independently.  The
single-line form exists for stimela, which reads cab console output line by
line; the `SKARABINA_ANALYZE_JSON ` prefix on that line is a public interface
(a stimela output wrangler matches on it).

## How it works

1. Reads the UVW coordinates from the main table to find the longest
   baseline *B*<sub>max</sub> (maximum UV distance in metres).  Rows with
   `FLAG_ROW` set are ignored: a flagger such as
   `skarabina --flag "uv-above <metres>"` marks exactly the baselines that will never be
   imaged, and counting them would recommend a size for unusable data.  An MS
   whose every row is flagged is an error.

2. Reads the SPECTRAL_WINDOW subtable for the band edges
   *ν*<sub>min</sub> and *ν*<sub>max</sub>, the channel count, and the
   **actual per-channel width** from `CHAN_WIDTH` (falling back to
   `RESOLUTION`).  Three related quantities are reported, and they are not
   interchangeable:

   | Key | Meaning |
   |-----|---------|
   | `bandwidth_hz` | sum of the channel widths — the spectrum recorded, edge to edge |
   | `span_hz` | *ν*<sub>max</sub> − *ν*<sub>min</sub>, the distance between the first and last channel centres |
   | `channel_width_hz` | the width of one channel |

   For a contiguous band the channels tile the spectrum, so `bandwidth_hz` is
   `span_hz` plus half a channel at each end.  They come apart when the band has
   a **hole**: `skarabina --optimize` removes fully-flagged channels, and a dead
   channel in the middle of the band leaves a gap that no SPECTRAL_WINDOW column
   records — `CHAN_WIDTH` still describes each surviving channel and
   `TOTAL_BANDWIDTH` still sums what is left.  `band_has_gaps` reports that
   case.  Channel width is always taken from the subtable, never as
   `span / n_channels`, which would overstate it by the fraction of the band
   that is missing.

   Disjoint spectral windows are treated as one band, so `band_has_gaps` is then
   true as well, and the smearing limits below are the conservative ones.

3. Computes the angular resolution (synthesised beam width):

   $$\theta_\text{res} = \frac{\lambda_\text{min}}{B_\text{max}} =
     \frac{c}{\nu_\text{max} \cdot B_\text{max}} \quad\text{(radians)}$$

   where *c* = 299 792 458 m s⁻¹.  This is the **theoretical best
   case** for the array as measured: the top of the band, with no tapering or
   weighting.  A real imager's synthesised beam is equal or slightly broader, so
   the recommended pixel size errs on the side of oversampling.

4. Recommends the image size in pixels:

   $$N_\text{pix} = \text{oversampling} \times
     \frac{\text{FOV}}{\theta_\text{res}}$$

   The result is rounded up to the next even integer for FFT efficiency.

5. Reads the band edges and channel count from the same SPECTRAL_WINDOW
   subtable and works out how coarsely the data may be averaged before smearing
   shows **at the edge of the requested field of view**
   (``theta_edge = FOV/2``):

   * white-light fringes — the widest channel that keeps the fringes from the
     two ends of the band coherent at that angle, i.e. the width for which the
     phase change across the band, ``2π·Δν·B·θ_edge/c``, reaches 2π:

     $$\Delta\nu_\text{max} = \frac{c}{B_\text{max} \cdot \theta_\text{edge}}$$

     This is the single-channel form of the criterion WSClean applies as
     ``maxuv-l``.  From it the fewest usable channels follow,
     ``min_channels = bandwidth_hz / Δν_max`` (the pipeline uses this to check
     its channel averaging: a channel width above the limit smears the edge of
     the image).  It uses the *recorded* bandwidth, so a hole in the band
     correctly lowers the number of channels the data supports;
   * time-average smearing — the longest integration whose smearing loss at
     that angle stays within 10%.  The fringe-washing factor for an integration
     Δt at angular distance θ from the phase centre is

     $$\rho = \operatorname{sinc}\!\left(\frac{\pi\,\omega_E\,\Delta t\,
       B\,\nu\,\theta}{c}\right)$$

     and ``max_integration_time_s`` inverts it for a 10% loss
     (= ``TIME_AVERAGE_LOSS`` in ``skarabina.dask_ms``).  For small losses this is
     close to the familiar ``Δt_max = c·√(6L)/(π·ω_E·B·ν·θ)``, which is ~1.5%
     shorter at L = 0.1.  ``skarabina --summary`` uses the same function and
     prints the 10% row alongside 1%, 3% and 5%, so the two commands can be
     compared directly;
   * the resulting radial bandwidth-smearing factor for the channels as they are,

     $$R_b = \frac{1}{\sqrt{1 + \left(\frac{0.939\, r_1 \, \Delta\nu}
       {\text{FOV} \cdot \nu_\text{max}}\right)^2}}$$

     with *r*<sub>1</sub> = sin(θ<sub>edge</sub>), the radial distance from the phase
     centre to the field edge, evaluated at the edge (the worst case).

   These are the numbers the superseded `set-image-parameters` cab in the
   white-belt pipeline used to print; that cab has been retired.

## Example

A real MeerKAT measurement set (1.6 M rows, 58 antennas, 4096 channels over
856–1712 MHz, 7625 m longest baseline):

    $ skarabina-analyze --ms target.ms --image-fov 2.5 deg
    Measurement set:  target.ms
      Max baseline:   7625 m
      Max frequency:  1711.791 MHz
      Resolution:     4.74 arcsec
      Field of view:  2.5 deg
    Recommended image size: 9500 × 9500 pixels
      Channels:       4096 × 209.0 kHz  (856.000 MHz of spectrum)
    Averaging limits at the field edge (1.25 deg):
      Max channel width: 1802.0 kHz  (no fewer than 476 channels)
      Max integration:   3.6 s  (for 10% loss at the edge)
      Bandwidth smearing factor (as-is): 1.0000

## JSON output

With `--output-json`, the same results are written as a JSON record.  Note that
the keys are a stable interface — stimela cab outputs are named after them:

```json
{
  "ms": "target.ms",
  "max_baseline_m": 7625.494677046231,
  "min_frequency_hz": 856000000.0,
  "max_frequency_hz": 1711791015.625,
  "max_frequency_mhz": 1711.791015625,
  "bandwidth_hz": 856000000.0,
  "span_hz": 855791015.625,
  "channel_width_hz": 208984.375,
  "band_has_gaps": false,
  "num_channels": 4096,
  "resolution_arcsec": 4.737258363673577,
  "field_of_view": "2.5 deg",
  "oversampling_factor": 5.0,
  "recommended_image_size_pixels": 9500,
  "max_channel_width_hz": 1802043.6234735155,
  "min_channels": 476,
  "max_integration_time_s": 3.615022300578047,
  "bandwidth_smearing_factor": 0.9999999983575263
}
```

With `--json-stdout`, this record is printed on a single line prefixed by
`SKARABINA_ANALYZE_JSON `:

    $ skarabina-analyze --ms target.ms --image-fov 2.5 deg --json-stdout
    ...
    SKARABINA_ANALYZE_JSON {"ms": "target.ms", "max_baseline_m": 7697.0, ...}

## Use from stimela

The `skarabina-analyze` cab in
[skarabina-cargo](https://github.com/tmolteno/skarabina/tree/main/cargo) sets
`--json-stdout` and wrangles that line into typed outputs, so the recommendation
can drive a downstream imager:

```yaml
analyze:
  cab: skarabina-analyze
  params:
    ms: =recipe.ms
    image-fov: 2.5 deg
    json-stdout: true

image:
  cab: wsclean
  params:
    ms: =recipe.ms
    size: =steps.analyze.recommended_image_size_pixels
```

See the [cargo README](../cargo/README.md) and
[`cargo/examples/skarabina-demo-pipeline.yml`](../cargo/examples/skarabina-demo-pipeline.yml)
for a complete example.

## See also

- [AVERAGING.md](AVERAGING.md) — time and frequency averaging limits
