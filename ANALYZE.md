<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# skarabina-analyze

Analyses a measurement set and recommends an image size for synthesis
imaging.

## Usage

    skarabina-analyze --ms <measurement_set> --image-fov <degrees> [--oversampling-factor <N>]

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--ms` | (required) | Input measurement set |
| `--image-fov` | (required) | Desired image field-of-view in degrees |
| `--oversampling-factor` | 5.0 | Pixels per synthesised beam |

## How it works

1. Reads the UVW coordinates from the main table to find the longest
   baseline *B*<sub>max</sub> (maximum UV distance in metres).

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

## See also

- [AVERAGING.md](AVERAGING.md) — time and frequency averaging limits
