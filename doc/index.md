<!-- Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz) -->
# Skarabina Documentation

Skarabina is a 1GC radio astronomy RFI flagger for efficient, low-memory
flagging of measurement sets.

- [Usage & CLI reference](usage.md)
- [Installation](INSTALL.md)
- [Time & frequency averaging](AVERAGING.md)
- [Splitting an MS by field](SPLITTING.md)
- [Measurement set analyzer](ANALYZE.md)
- [Plotting (skarabina-plotms, the casaplotms substitute)](PLOTTING.md)
- [RFlag, and how it differs from CASA's](RFLAG.md)
- [Changelog](CHANGES.md)

## Quick start

```sh
pip install skarabina

# Flag and write a cleaned MS
skarabina --ms raw.ms \
    --flag "nan, uv-above 4000, spectral-window spectral-flags.yml" \
    --time-average-factor 3 --optimize --msout clean.ms --clobber

# Analyze a measurement set
skarabina-analyze --ms raw.ms --image-fov 2.5

# Plot a gain table or an MS (casaplotms substitute; no display needed)
skarabina-plotms --ms raw.ms --plotfile amp.png --xaxis uvdist --overwrite
```
