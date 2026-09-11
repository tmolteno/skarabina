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
      image-fov: 2.5 deg
      oversampling-factor: 5.0
      json-stdout: true
      output-json: analysis.json
```

Run it:

    stimela run recipe.yml ms=~/data/observation.ms

The cab measures the longest baseline and the highest channel frequency, then
recommends an image size. It publishes its results in two ways:

| Output | Type | Contents |
|---|---|---|
| `output-json` | File | The full analysis record, as JSON |
| `recommended_image_size_pixels` | int | Recommended square image size, in pixels |
| `resolution_arcsec` | float | Synthesised beam (angular resolution), in arcsec |
| `max_baseline_m` | float | Longest baseline (maximum uv distance), in metres |
| `max_frequency_hz` | float | Highest channel frequency, in Hz |

`output-json` is a *named file output*: stimela supplies the filename, passes
it to the cab as `--output-json <path>`, and makes the resulting file available
to later steps.  The scalar outputs are *wrangled* from the cab's console
output, which is why `json-stdout: true` must be set for them to be produced.

The JSON record contains all of the above plus the fields the cab does not
expose as outputs:

```json
{
  "ms": "observation.ms",
  "max_baseline_m": 34427.18,
  "max_frequency_hz": 2052500000.0,
  "max_frequency_mhz": 2052.5,
  "resolution_arcsec": 5.5,
  "field_of_view": "2.5 deg",
  "oversampling_factor": 5.0,
  "recommended_image_size_pixels": 8192
}
```

The `resolution_arcsec` is the synthesised beam width — divide it by
`oversampling-factor` to get a cell size that oversamples the beam.

To run the command directly, without stimela:

    skarabina-analyze --ms observation.ms --image-fov 2.5 --output-json analysis.json
    skarabina-analyze --ms observation.ms --image-fov 2.5 --json-stdout

### Driving an imaging pipeline from the analysis

This is the reason `skarabina-analyze` exists: the numbers it measures set
parameters for a downstream imager (WSClean, CASA, DDFacet, ...).  Bind the
wrangled scalar outputs directly onto the imaging step:

```yaml
steps:
  flag:
    cab: skarabina
    params:
      ms: =recipe.ms
      flag-nan: true
      msout: cleaned.ms
      clobber: true

  analyze:
    cab: skarabina-analyze
    params:
      ms: =steps.flag.msout
      image-fov: 2.5 deg
      json-stdout: true
      output-json: analysis.json

  image:
    cab: wsclean                  # or your imager of choice
    params:
      ms: =steps.flag.msout
      prefix: image
      size: =steps.analyze.recommended_image_size_pixels
      scale: "=steps.analyze.resolution_arcsec / 3600.0"   # wsclean wants degrees
```

Expose the recommendation to the caller by aliasing it at recipe level, which
also gets the type checked when the recipe is prevalidated:

```yaml
my-recipe:
  inputs:
    ms: MS
  outputs:
    image-size: int
  aliases:
    image-size: [analyze.recommended_image_size_pixels]
```

Three things are worth knowing about the scalar outputs:

-   **They are evaluated late.**  Formulas such as
    `=steps.analyze.resolution_arcsec` are resolved at run time, so a typo in
    the output name surfaces when the step runs.  Aliasing the value to a
    typed recipe output (above) moves that check up to prevalidation.
-   **Their names are Python identifiers.**  Stimela's
    `PARSE_JSON_OUTPUT_DICT` wrangler assigns JSON keys straight onto output
    names, so a kebab-case name like `image-size` could never be populated.
    The CLI-facing *inputs* keep the usual kebab-case names.
-   **Units are not converted for you.**  `resolution_arcsec` is in arcsec;
    most imagers want degrees, radians, or a multiple of the beam.  Convert in
    the consuming step so the choice is visible in the recipe.

A complete, runnable example is in
[`examples/skarabina-demo-pipeline.yml`](examples/skarabina-demo-pipeline.yml):

    stimela run skarabina-demo-pipeline.yml demo-imaging-pipeline ms=observation.ms

### Running in containers

Both cabs run in the skarabina container image, which stimela pulls on first
use, so nothing here changes under a container backend.  Two points are worth
knowing:

-   **A bare (binary) cab must name an image.**  Since stimela 2.2 a cab with
    no `image` is rejected by container backends with *"container image not
    specified by cab"*.  The demo's `report` step therefore uses the `python`
    flavour, which picks up stimela's default image.  If you add an `echo`-style
    binary step, give it an `image:`.
-   **`stimela` has no working `docker` backend.**  Stimela 2.2.0rc1 declares
    `docker` in its backend enum, but `backends/docker.py` is a stub
    (`is_available()` returns `False`, `get_status()` returns `"not
    implemented"`); `podman` is likewise unimplemented.  For containerised runs,
    use the `singularity`/`apptainer` backend:

        stimela run -b singularity recipe.yml ms=observation.ms

    The demo pipeline was verified end-to-end this way, with the skarabina image
    and stimela's default python image.

To use the image directly, without stimela:

    docker run --rm -v "$PWD":/work -w /work ghcr.io/tmolteno/skarabina:latest \
        skarabina-analyze --ms /work/observation.ms --image-fov "2.5 deg" \
        --json-stdout --output-json analysis.json

Mount the directory containing your measurement set (and remember that, as with
any container, the `--ms` path is the path *inside* the container).

### Printing outputs from a previous step

Both cabs expose outputs that can be consumed by downstream steps.  A `python`
flavour step is the simplest container-friendly way to print one, because
stimela substitutes each parameter into a local variable:

```yaml
steps:
  flag-summary:
    cab: skarabina
    params:
      ms: =recipe.ms
      summary: true

  print-max-uv:
    cab:
      flavour: python-code
      command: |
        print(f"Max UV: {max_uv}")
      inputs:
        max-uv: float
    params:
      max-uv: =previous.max-uv
```

(`echo` is *not* a stimela built-in, so there is nothing to point a binary cab
at unless you name an image for it.)
