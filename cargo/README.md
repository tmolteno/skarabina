<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# skarabina-cargo

Stimela cab definitions for [skarabina](https://github.com/tmolteno/skarabina),
the 1GC radio astronomy RFI flagger.

Provides two cabs:

| Cab | Command | Purpose |
|---|---|---|
| `skarabina` | `skarabina` | Flag, average, and clean measurement sets |
| `skarabina-analyze` | `skarabina-analyze` | Recommend image size for synthesis imaging |

## Install

    pip install skarabina-cargo

Requires [stimela](https://github.com/caracal-pipeline/stimela) ≥ 2.1.2
and the skarabina container image (pulled automatically by stimela on first
use, or build from the [Dockerfile](https://github.com/tmolteno/skarabina/blob/main/Dockerfile)).

## Usage

### skarabina cab

```yaml
_include:
  - (cargo):
      - skarabina.yml

my-recipe:
  info: "Flag, average, and optimize a measurement set"
  inputs:
    ms: MS

  steps:
    flag-n-clean:
      cab: skarabina
      params:
        ms: =recipe.ms
        flag-nan: true
        flag-uv-above: 4000
        time-average-factor: 3
        optimize: true
        msout: cleaned.ms
        clobber: true
        summary: true
```

Run it:

    stimela run recipe.yml ms=~/data/observation.ms

#### Spectral window flagging

```yaml
steps:
  spw-flag:
    cab: skarabina
    params:
      ms: =recipe.ms
      flag-spectral-window: spectral-flags.yml
      msout: spw-flagged.ms
```

Where `spectral-flags.yml` defines frequency ranges to flag:

```yaml
# Flag all baselines
- spw:
    - [850, 900]
    - [1419.8, 1421.3]

# Flag short baselines only (uv < 600 m)
- spw:
    - [1166, 1186]
    - [1217, 1237]
  uv_below: 600
```

### skarabina-analyze cab

```yaml
steps:
  analyze:
    cab: skarabina-analyze
    params:
      ms: =recipe.ms
      image-fov: 2.5
      oversampling-factor: 5.0
      output-json: analysis.json
```

Run it:

    stimela run recipe.yml ms=~/data/observation.ms

The `output-json` option writes machine-readable analysis results for
scripting or downstream processing:

```json
{
  "image_size": 8192,
  "pixel_size_arcsec": 1.1,
  "field_of_view_deg": 2.5,
  "max_uv": 34427.18,
  "synthesized_beam_arcsec": 5.5,
  "observation_frequency_hz": 2052500000.0
}
```

To run directly without stimela:

    skarabina-analyze --ms observation.ms --image-fov 2.5 --output-json analysis.json

### Printing outputs from a previous step

Both cabs expose outputs that can be consumed by downstream steps:

```yaml
steps:
  flag-summary:
    cab: skarabina
    params:
      ms: =recipe.ms
      summary: true

  print-max-uv:
    cab: echo
    params:
      args:
        - "Max UV:"
        - =previous.max-uv

  print-ref-ant:
    cab: echo
    params:
      args:
        - "Reference antenna:"
        - =previous.reference-antenna
```
