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
| `--image-fov` | (required) | Desired image field-of-view in degrees |
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

## Example

    $ skarabina-analyze --ms target.ms --image-fov 2.5
    Measurement set:  target.ms
      Max baseline:   7697 m
      Max frequency:  1800.000 MHz
      Resolution:     4.47 arcsec
      Field of view:  2.50°
    Recommended image size: 10066 × 10066 pixels

## JSON output

With `--output-json`, the same results are written as a JSON record.  Note that
the keys are a stable interface — stimela cab outputs are named after them:

```json
{
  "ms": "target.ms",
  "max_baseline_m": 7697.0,
  "max_frequency_hz": 1800000000.0,
  "max_frequency_mhz": 1800.0,
  "resolution_arcsec": 4.4689,
  "field_of_view": "2.5 deg",
  "oversampling_factor": 5.0,
  "recommended_image_size_pixels": 10066
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
