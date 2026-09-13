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
   `skarabina --flag-uv-above` marks exactly the baselines that will never be
   imaged, and counting them would recommend a size for unusable data.  An MS
   whose every row is flagged is an error.

2. Reads the SPECTRAL_WINDOW subtable's `CHAN_FREQ` to find the highest
   frequency *ν*<sub>max</sub>.

3. Computes the angular resolution (synthesised beam width):

   $$\theta_\text{res} = \frac{\lambda_\text{min}}{B_\text{max}} =
     \frac{c}{\nu_\text{max} \cdot B_\text{max}} \quad\text{(radians)}$$

   where *c* = 299 792 458 m s⁻¹.

4. Recommends the image size in pixels:

   $$N_\text{pix} = \text{oversampling} \times
     \frac{\text{FOV}}{\theta_\text{res}}$$

   The result is rounded up to the next even integer for FFT efficiency.

5. Reads the band edges and channel count from the same SPECTRAL_WINDOW
   subtable and works out how coarsely the data may be averaged before smearing
   shows **at the edge of the requested field of view**
   (``theta_edge = FOV/2``):

   * white-light fringes — the widest channel that keeps the fringes from the
     two ends of the band coherent at that angle:

     $$\Delta\nu_\text{max} = \frac{c}{B_\text{max} \cdot \theta_\text{edge}}$$

     from which the fewest usable channels follow,
     ``min_channels = bandwidth / Δν_max`` (the pipeline uses this to check its
     channel averaging: a channel width above the limit smears the edge of the
     image);
   * time-average smearing — the longest integration that keeps the loss at that
     angle below ~10% (TMS, *Synthesis Imaging in Radio Astronomy II*, p.246):

     $$t_\text{max} = \frac{0.1}{\omega_E \cdot B_\text{max} \cdot \theta_\text{edge}}$$

     with ω<sub>E</sub> the sidereal rotation rate;
   * the resulting radial bandwidth-smearing factor for the channels as they are,

     $$R_b = \frac{1}{\sqrt{1 + \left(\frac{0.939\, r_1 \, \Delta\nu}
       {\text{FOV} \cdot \nu_\text{max}}\right)^2}}$$

     with *r*<sub>1</sub> evaluated at the field edge.

   These are the numbers the superseded `set-image-parameters` cab in the
   white-belt pipeline used to print; that cab has been retired.

## Example

    $ skarabina-analyze --ms target.ms --image-fov 2.5
    Measurement set:  target.ms
      Max baseline:   7697 m
      Max frequency:  1800.000 MHz
      Resolution:     4.47 arcsec
      Field of view:  2.50°
    Recommended image size: 10066 × 10066 pixels
    Averaging limits at the field edge (1.25 deg):
      Max channel width: 3590.9 kHz  (no fewer than 263 channels)
      Max integration:   2.9 s
      Bandwidth smearing factor (as-is): 1.0000

## JSON output

With `--output-json`, the same results are written as a JSON record.  Note that
the keys are a stable interface — stimela cab outputs are named after them:

```json
{
  "ms": "target.ms",
  "max_baseline_m": 7697.0,
  "min_frequency_hz": 856000000.0,
  "max_frequency_hz": 1800000000.0,
  "max_frequency_mhz": 1800.0,
  "bandwidth_hz": 944000000.0,
  "num_channels": 4096,
  "resolution_arcsec": 4.4689,
  "field_of_view": "2.5 deg",
  "oversampling_factor": 5.0,
  "recommended_image_size_pixels": 10066,
  "max_channel_width_hz": 3590900.0,
  "min_channels": 263,
  "max_integration_time_s": 2.9,
  "bandwidth_smearing_factor": 0.9999
}
```

With `--json-stdout`, this record is printed on a single line prefixed by
`SKARABINA_ANALYZE_JSON `:

    $ skarabina-analyze --ms target.ms --image-fov 2.5 --json-stdout
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
